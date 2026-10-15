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
from collector.credits import BudgetExhausted, CreditLedger
from collector.pipeline import read_table
from collector.pools import PoolInfo, fetch_pool_info
from collector.rpc import OfflineMiss, RpcClient
from common.config import Config, load_config
from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC
from detectors.arbitrage import detect_arbitrages
from detectors.sandwich import detect_sandwiches
from detectors.swaps import Swap, decode_v2_swaps
from mitigation.victim_loss import victim_losses

log = logging.getLogger("detectors")

V2_TOPICS = [UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC]


def load_pools(config: Config, logs: pd.DataFrame, rpc: RpcClient) -> dict[str, PoolInfo]:
    path = config.processed_dir / "pools.parquet"
    known: dict[str, PoolInfo] = {}
    if path.exists():
        for row in pd.read_parquet(path).to_dict("records"):
            known[row["address"]] = PoolInfo(**row)

    swaps = logs[logs["topic0"] == UNISWAP_V2_SWAP_TOPIC]
    first_seen = swaps.groupby("address")["block_number"].min()
    new = [(a, int(b)) for a, b in first_seen.items() if a not in known]
    log.info("%d pools known, %d new", len(known), len(new))
    for i, (address, block) in enumerate(new, 1):
        try:
            known[address] = fetch_pool_info(rpc, address, block)
        except (BudgetExhausted, OfflineMiss) as exc:
            log.warning("pool lookup stopped after %d/%d: %s", i - 1, len(new), exc)
            break
    pd.DataFrame([asdict(p) for p in known.values()]).to_parquet(path, index=False)
    return known


def _swap_row(swap: Swap, role: str) -> dict:
    row = {k: v for k, v in asdict(swap).items() if k != "reserves_after"}
    for key in ("amount0_in", "amount1_in", "amount0_out", "amount1_out"):
        row[key] = str(row[key])
    row["role"] = role
    return row


def run(config: Config, rpc: RpcClient) -> dict[str, int]:
    logs = read_table(config.processed_dir, "logs")
    txs = read_table(config.processed_dir, "transactions")
    if logs.empty:
        raise SystemExit("no tables yet: run `python -m collector build` first")
    tx_lookup = txs.set_index("tx_hash")[["from", "to"]].to_dict("index")
    pools = load_pools(config, logs, rpc)

    relevant = logs[logs["topic0"].isin(V2_TOPICS)].to_dict("records")
    swaps = decode_v2_swaps(relevant, tx_lookup, pools, config.detectors.get("allowed_dexes"))
    arbitrages = detect_arbitrages(swaps)
    tolerance = config.detectors.get("sandwich", {}).get("amount_tolerance", 0.2)
    sandwiches = detect_sandwiches(swaps, tolerance)

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
            v.victim_tx_hash: v for v in victim_losses(s, by_block_pool[(s.block_number, s.pool)])
        }
        for victim in s.victims:
            roles[(victim.tx_hash, victim.log_index)] = "sandwich_victim"
            loss = losses.get(victim.tx_hash)
            sandwich_rows.append(
                {
                    "block_number": s.block_number,
                    "pool": s.pool,
                    "attacker": s.attacker,
                    "front_tx": s.front.tx_hash,
                    "back_tx": s.back.tx_hash,
                    "victim_tx": victim.tx_hash,
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
                **{k: v for k, v in asdict(a).items() if k not in ("pools", "log_indices")},
                "amount_in": str(a.amount_in),
                "amount_out": str(a.amount_out),
                "gross_profit": str(a.gross_profit),
                "pools": list(a.pools),
                "n_swaps": len(a.pools),
            }
            for a in arbitrages
        ]
    ).to_parquet(out / "arbitrages.parquet", index=False)
    pd.DataFrame(sandwich_rows).to_parquet(out / "sandwiches.parquet", index=False)

    return {
        "blocks": int(logs["block_number"].nunique()),
        "v2_swaps": len(swaps),
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
    print(json.dumps(run(config, rpc), indent=1))


if __name__ == "__main__":
    main()
