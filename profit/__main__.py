"""Realised profit for every detected MEV bundle (build step 5; feeds RQ1).

    python -m profit                       untraced accounting for all bundles
    python -m profit --trace-provider drpc ... plus traced accounting on a seeded sample

Reads data/results/{arbitrages,sandwiches}.parquet and the Parquet tables, writes
data/results/realised_profit.parquet with one row per bundle (see profit/bundles.py).
All values are wei of ETH as decimal strings unless named otherwise.

Untraced realised profit misses internal ETH transfers (e.g. coinbase tips) and so is an
upper bound; the traced sample measures that gap. Traces cost one debug_traceTransaction per
transaction, so they are fetched only for `profit.trace_sample_size` random bundles.
"""

import argparse
import json
import logging
import random
from dataclasses import replace

import pandas as pd

from collector.cache import RpcCache
from collector.credits import BudgetExhausted, CreditLedger
from collector.pipeline import read_table
from collector.rpc import RpcClient, RpcError
from common.config import Config, load_config
from common.constants import (
    ERC20_TRANSFER_TOPIC,
    PUBLIC_ROUTERS,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V3_SWAP_TOPIC,
)
from common.nulls import is_missing
from common.provenance import write_provenance
from detectors.load import load_swaps
from profit.bundles import Bundle, Event, group_into_bundles
from profit.pricing import DEFAULT_MIN_WETH_RESERVE, EthPricer
from profit.realised import RealisedProfit, TxData, account

log = logging.getLogger("profit")

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def build_events(arbitrages: pd.DataFrame, sandwiches: pd.DataFrame) -> list[Event]:
    events = [
        Event("arbitrage", f"{a['tx_hash']}:{a['start_token']}", int(a["block_number"]),
              frozenset({a["tx_hash"]}))
        for a in arbitrages.to_dict("records")
    ]  # fmt: skip
    if not sandwiches.empty:
        unique = sandwiches.drop_duplicates(["front_tx", "back_tx", "pool"])
        events += [
            Event("sandwich", f"{s['front_tx']}:{s['back_tx']}:{s['pool']}",
                  int(s["block_number"]), frozenset({s["front_tx"], s["back_tx"]}))
            for s in unique.to_dict("records")
        ]  # fmt: skip
    return events


def detector_gross_eth(
    bundle: Bundle, arbitrages: pd.DataFrame, sandwiches: pd.DataFrame, pricer: EthPricer
) -> int | None:
    """Gross profit implied by the detected swaps alone, in wei (None if unpriceable).

    Arbitrages that reuse a sandwich leg are left out, so no swap is counted twice.
    """
    total = 0
    for event in bundle.events:
        if event.kind == "arbitrage":
            tx_hash, token = event.event_id.split(":")
            rows = arbitrages[
                (arbitrages["tx_hash"] == tx_hash) & (arbitrages["start_token"] == token)
            ]
            amounts = [(token, int(r["gross_profit"])) for r in rows.to_dict("records")
                       if not r["overlaps_sandwich"]]  # fmt: skip
        else:
            front, back, pool = event.event_id.split(":")
            row = sandwiches[(sandwiches["front_tx"] == front) & (sandwiches["back_tx"] == back)
                             & (sandwiches["pool"] == pool)].iloc[0]  # fmt: skip
            amounts = [(row["token_x"], int(row["attacker_gross_profit_x"]))]
        for token, amount in amounts:
            value = pricer.wei_value(token, amount, bundle.block_number)
            if value is None:
                return None
            total += value
    return total


def price_flows(result: RealisedProfit, pricer: EthPricer, block: int) -> RealisedProfit:
    total, unpriced = 0, []
    for token, amount in result.token_flows.items():
        value = pricer.wei_value(token, amount, block)
        if value is None:
            unpriced.append(token)
        else:
            total += value
    return replace(
        result, token_value_eth=None if unpriced else total, unpriced_tokens=tuple(unpriced)
    )


MEV_ROLES = ("arbitrage", "sandwich_front", "sandwich_back")
SWAP_TOPICS = (UNISWAP_V2_SWAP_TOPIC, UNISWAP_V3_SWAP_TOPIC)


def undetected_swap_counts(
    logs: pd.DataFrame, detected_legs: set[tuple[str, int]]
) -> dict[str, int]:
    """Per tx: swap events (V2 or V3) that are not a leg of a detected MEV event.

    A bundle is only fully explained when this is zero for all its transactions. Otherwise
    its token flows include trades we did not decode (e.g. a sandwich leg on a Uniswap V3
    pool whose other half sits in a transaction outside the bundle), so its realised profit
    is not comparable and is left out of headline figures.
    """
    swaps = logs[logs["topic0"].isin(SWAP_TOPICS)]
    counts: dict[str, int] = {}
    for tx_hash, log_index in zip(swaps["tx_hash"], swaps["log_index"], strict=True):
        if (tx_hash, int(log_index)) not in detected_legs:
            counts[tx_hash] = counts.get(tx_hash, 0) + 1
    return counts


def _str(value: int | None) -> str | None:
    return None if value is None else str(value)


def run(config: Config, rpc: RpcClient, trace_rpc: RpcClient | None) -> dict:
    results = config.data_dir / "results"
    arbitrages = pd.read_parquet(results / "arbitrages.parquet")
    sandwiches = pd.read_parquet(results / "sandwiches.parquet")
    bundles = group_into_bundles(build_events(arbitrages, sandwiches))
    bundle_txs = {h for b in bundles for h in b.tx_hashes}

    swaps, logs, txs, _ = load_swaps(config, rpc)
    min_reserve = int(config.raw.get("profit", {}).get("min_weth_reserve_eth", 10) * 10**18)
    pricer = EthPricer(
        swaps, rpc if not rpc.offline else None, min_reserve or DEFAULT_MIN_WETH_RESERVE
    )
    blocks = read_table(config.processed_dir, "blocks").set_index("block_number")
    receipts = read_table(config.processed_dir, "receipts").set_index("tx_hash")
    txs = txs[txs["tx_hash"].isin(bundle_txs)].set_index("tx_hash")
    swap_roles = pd.read_parquet(
        results / "swaps.parquet", columns=["tx_hash", "log_index", "role"]
    )
    mev = swap_roles[swap_roles["role"].isin(MEV_ROLES)]
    detected_legs = set(zip(mev["tx_hash"], mev["log_index"].astype(int), strict=True))
    logs = logs[logs["tx_hash"].isin(bundle_txs)]
    undetected = undetected_swap_counts(logs, detected_legs)
    logs = logs[logs["topic0"] == ERC20_TRANSFER_TOPIC]
    logs_by_tx = {h: tuple(g.to_dict("records")) for h, g in logs.groupby("tx_hash")}

    profit_cfg = config.raw.get("profit", {})
    rng = random.Random(int(config.sample["seed"]))
    sample_size = int(profit_cfg.get("trace_sample_size", 300)) if trace_rpc else 0
    traced_ids = {b.bundle_id for b in rng.sample(bundles, min(sample_size, len(bundles)))}

    rows, trace_failures = [], 0
    for bundle in bundles:
        tx_data, actors = [], set()
        for h in bundle.tx_hashes:
            tx, receipt = txs.loc[h], receipts.loc[h]
            to = None if is_missing(tx["to"]) else tx["to"]
            actors.add(tx["from"])
            if to and to not in PUBLIC_ROUTERS:
                actors.add(to)
            tx_data.append(TxData(h, tx["from"], to, int(tx["value"]), int(receipt["gas_used"]),
                                  int(receipt["effective_gas_price"]), logs_by_tx.get(h, ()),
                                  int(receipt["status"])))  # fmt: skip
        block = blocks.loc[bundle.block_number]
        args = (tx_data, actors, block["fee_recipient"], int(block["base_fee_per_gas"]))
        untraced = price_flows(account(*args), pricer, bundle.block_number)

        traced = None
        if bundle.bundle_id in traced_ids:
            try:
                traces = {
                    h: trace_rpc.call("debug_traceTransaction", [h, {"tracer": "callTracer"}],
                                      block=bundle.block_number)
                    for h in bundle.tx_hashes
                }  # fmt: skip
                traced = price_flows(account(*args, traces=traces), pricer, bundle.block_number)
            except (RpcError, ConnectionError, BudgetExhausted) as exc:
                trace_failures += 1
                log.warning("trace failed for bundle %s: %s", bundle.bundle_id, exc)

        usd_per_wei = pricer.wei_value(USDC, 10**6, bundle.block_number)
        rows.append({
            "bundle_id": bundle.bundle_id,
            "block_number": bundle.block_number,
            "n_txs": len(bundle.tx_hashes),
            "n_sandwiches": bundle.count("sandwich"),
            "n_arbitrages": bundle.count("arbitrage"),
            "undetected_swaps": sum(undetected.get(h, 0) for h in bundle.tx_hashes),
            "tx_hashes": list(bundle.tx_hashes),
            "actors": sorted(actors),
            "detector_gross": _str(detector_gross_eth(bundle, arbitrages, sandwiches, pricer)),
            "eth_flow": str(untraced.eth_flow),
            "token_flows": json.dumps({k: str(v) for k, v in untraced.token_flows.items()}),
            "token_value": _str(untraced.token_value_eth),
            "unpriced_tokens": list(untraced.unpriced_tokens),
            "gas_fee": str(untraced.gas_fee),
            "base_fee_burnt": str(untraced.base_fee_burnt),
            "priority_fee": str(untraced.priority_fee),
            "coinbase_transfers_top_level": str(untraced.coinbase_transfers),
            "revenue": _str(untraced.revenue),
            "realised_untraced": _str(untraced.realised),
            "realised_eth_only_untraced": str(untraced.realised_eth_only),
            "traced": traced is not None,
            "coinbase_transfers_traced": None if traced is None else str(traced.coinbase_transfers),
            "realised_traced": None if traced is None else _str(traced.realised),
            "realised_eth_only_traced": None if traced is None else str(traced.realised_eth_only),
            "eth_usd": None if not usd_per_wei else 10**18 / usd_per_wei,
        })  # fmt: skip

    out = results / "realised_profit.parquet"
    pd.DataFrame(rows).to_parquet(out, index=False)
    write_provenance(
        results / "realised_profit.PROVENANCE.json",
        dataset="realised profit per MEV bundle",
        blocks=[b.block_number for b in bundles],
        extra={"trace_provider": trace_rpc.providers[0].name if trace_rpc else None,
               "trace_sample_size": sample_size, "trace_failures": trace_failures,
               "price_lookup_failures": pricer.lookup_failures},
    )  # fmt: skip
    frame = pd.DataFrame(rows)
    return {
        "bundles": len(bundles),
        "fully_priced": int(frame["realised_untraced"].notna().sum()) if rows else 0,
        "fully_explained": int((frame["undetected_swaps"] == 0).sum()) if rows else 0,
        "traced": int(frame["traced"].sum()) if rows else 0,
        "trace_failures": trace_failures,
        "price_lookup_failures": pricer.lookup_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m profit")
    parser.add_argument("--provider", nargs="+", help="providers for price lookups (eth_call)")
    parser.add_argument("--offline", action="store_true", help="price only from cached data")
    parser.add_argument("--trace-provider", help="provider for the traced sample, e.g. drpc")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config()
    cache, ledger = RpcCache(config.cache_path), CreditLedger(config.ledger_path)
    providers = [] if args.offline else config.providers(args.provider)
    rpc = RpcClient(providers, cache, ledger, offline=args.offline or not providers, max_retries=8)
    trace_rpc = (
        RpcClient(config.providers([args.trace_provider]), cache, ledger, max_retries=8)
        if args.trace_provider
        else None
    )
    print(json.dumps(run(config, rpc, trace_rpc), indent=1))


if __name__ == "__main__":
    main()
