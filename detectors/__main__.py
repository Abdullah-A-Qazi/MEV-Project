"""Runs the detectors over the Parquet tables built by `python -m collector build`.

    python -m detectors            detect arbitrages and sandwiches -> data/results/

Pool metadata (token0/token1 + DEX check) is the only thing that may need the network:
pools seen for the first time are looked up once and stored in data/processed/pools.parquet.
Pass --offline to forbid that (unknown pools are then skipped and counted).

Outputs (uint256 amounts as decimal strings):
  swaps.parquet       every decoded V2 swap, with its role (arbitrage / front / back / victim)
  arbitrages.parquet  one row per detected arbitrage cycle
  sandwiches.parquet  one row per (sandwich, victim), with the victim's loss
"""

import argparse
import json
import logging
from collections import defaultdict
from dataclasses import asdict

import pandas as pd

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.rpc import RpcClient
from common.config import Config, load_config
from detectors.arbitrage import Arbitrage, detect_arbitrages
from detectors.load import build_market, load_swaps
from detectors.sandwich import Sandwich, detect_sandwiches
from detectors.swaps import Swap
from mitigation.victim_loss import victim_losses

log = logging.getLogger("detectors")


def arbitrage_event_id(a: Arbitrage) -> str:
    """Unique per cycle: one transaction can hold several cycles."""
    return f"arb:{a.tx_hash}:{a.log_indices[0]}"


def sandwich_event_id(s: Sandwich) -> str:
    """Unique per sandwich: its front-run swap and back-run swap."""
    return f"sw:{s.front.tx_hash}:{s.front.log_index}:{s.back.tx_hash}:{s.back.log_index}"


def _swap_row(swap: Swap, role: str) -> dict:
    row = {k: v for k, v in asdict(swap).items() if k != "reserves_after"}
    # uint256 / uint160 / uint128 values overflow Parquet's int64: store them as text.
    for key in ("amount0_in", "amount1_in", "amount0_out", "amount1_out",
                "sqrt_price_after", "liquidity_after"):  # fmt: skip
        row[key] = None if row[key] is None else str(row[key])
    row["role"] = role
    return row


def run(config: Config, rpc: RpcClient, allow_missing_pools: bool = False) -> dict[str, int]:
    swaps, logs, _, pools = load_swaps(config, rpc, allow_missing_pools)
    market = build_market(swaps, logs, pools, rpc)
    arbitrages = detect_arbitrages(swaps)
    tolerance = config.detectors.get("sandwich", {}).get("amount_tolerance", 0.2)
    sandwiches = detect_sandwiches(swaps, tolerance)

    # A swap can be both a sandwich leg and part of an arbitrage cycle in the same tx (bots
    # batch them). The swap keeps its sandwich role; the arbitrage is flagged so analyses can
    # avoid counting that swap twice.
    sandwich_legs = {(s.front.tx_hash, s.front.log_index) for s in sandwiches} | {
        (s.back.tx_hash, s.back.log_index) for s in sandwiches
    }
    roles: dict[tuple[str, int], str] = {}
    for arb in arbitrages:
        for idx in arb.log_indices:
            roles[(arb.tx_hash, idx)] = "arbitrage"
    by_block_pool: dict[tuple[int, str], list[Swap]] = defaultdict(list)
    for swap in swaps:
        by_block_pool[(swap.block_number, swap.pool)].append(swap)

    sandwich_rows = []
    for s in sandwiches:
        roles[(s.front.tx_hash, s.front.log_index)] = "sandwich_front"
        roles[(s.back.tx_hash, s.back.log_index)] = "sandwich_back"
        losses = {
            v.victim_tx_hash: v
            for v in victim_losses(s, by_block_pool[(s.block_number, s.pool)], market)
        }
        for victim in s.victims:
            roles[(victim.tx_hash, victim.log_index)] = "sandwich_victim"
            loss = losses.get(victim.tx_hash)
            sandwich_rows.append(
                {
                    "block_number": s.block_number,
                    "pool": s.pool,
                    "attacker": s.attacker,
                    "event_id": sandwich_event_id(s),
                    "front_tx": s.front.tx_hash,
                    "front_log_index": s.front.log_index,
                    "back_tx": s.back.tx_hash,
                    "back_log_index": s.back.log_index,
                    "victim_tx": victim.tx_hash,
                    "victim_log_index": victim.log_index,
                    "victim_from": victim.tx_from,
                    "token_x": s.token_x,
                    "token_y": s.token_y,
                    "attacker_gross_profit_x": str(s.gross_profit_x),
                    "attacker_residual_y": str(s.residual_y),
                    "victim_token_out": victim.token_out,
                    "victim_actual_out": str(victim.amount_out),
                    "victim_counterfactual_out": None
                    if loss is None
                    else str(loss.counterfactual_out),
                    "victim_loss": None if loss is None else str(loss.loss),
                    "victim_loss_fraction": None if loss is None else loss.loss_fraction,
                }
            )

    out = config.data_dir / "results"
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [_swap_row(s, roles.get((s.tx_hash, s.log_index), "other")) for s in swaps]
    ).to_parquet(out / "swaps.parquet", index=False)
    pd.DataFrame(
        [
            {
                "event_id": arbitrage_event_id(a),
                **{k: v for k, v in asdict(a).items() if k not in ("pools", "log_indices")},
                "log_indices": list(a.log_indices),
                "amount_in": str(a.amount_in),
                "amount_out": str(a.amount_out),
                "gross_profit": str(a.gross_profit),
                "pools": list(a.pools),
                "n_swaps": len(a.pools),
                "overlaps_sandwich": any((a.tx_hash, i) in sandwich_legs for i in a.log_indices),
            }
            for a in arbitrages
        ]
    ).to_parquet(out / "arbitrages.parquet", index=False)
    pd.DataFrame(sandwich_rows).to_parquet(out / "sandwiches.parquet", index=False)

    return {
        "blocks": int(logs["block_number"].nunique()),
        "swaps": len(swaps),
        "v3_swaps": sum(s.is_v3 for s in swaps),
        "pools_verified": sum(p.dex != "unknown" for p in pools.values()),
        "pools_unknown": sum(p.dex == "unknown" for p in pools.values()),
        "arbitrages": len(arbitrages),
        "sandwiches": len(sandwiches),
        "sandwich_victims": len(sandwich_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m detectors")
    parser.add_argument("--offline", action="store_true", help="never call the network")
    parser.add_argument("--provider", nargs="+", help="provider names for pool lookups")
    parser.add_argument(
        "--allow-missing-pools",
        action="store_true",
        help="run even if some pools could not be looked up (undercounts MEV)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    providers = [] if args.offline else config.providers(args.provider)
    rpc = RpcClient(
        providers,
        RpcCache(config.cache_path),
        CreditLedger(config.ledger_path),
        offline=args.offline or not providers,
    )
    print(json.dumps(run(config, rpc, args.allow_missing_pools), indent=1))


if __name__ == "__main__":
    main()
