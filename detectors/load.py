"""Loads decoded swaps from the Parquet tables, shared by the detectors and the profit step."""

import logging
from dataclasses import asdict

import pandas as pd

from collector.credits import BudgetExhausted
from collector.pipeline import read_table
from collector.pools import PoolInfo, fetch_pool_info, fetch_v3_pool_info
from collector.rpc import OfflineMiss, RpcClient
from common.config import Config
from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC, UNISWAP_V3_SWAP_TOPIC
from common.nulls import is_missing
from detectors.swaps import Swap, decode_swaps

log = logging.getLogger(__name__)

SWAP_TOPICS = [UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC, UNISWAP_V3_SWAP_TOPIC]


class IncompletePools(RuntimeError):
    """Some pools in the sample could not be looked up, so their swaps would be missed."""


def load_pools(
    config: Config, logs: pd.DataFrame, rpc: RpcClient, allow_missing: bool = False
) -> dict[str, PoolInfo]:
    """Pool metadata, looking up (once) any pool not yet in data/processed/pools.parquet.

    Raises IncompletePools if any lookup could not be made, unless `allow_missing`.
    """
    path = config.processed_dir / "pools.parquet"
    known: dict[str, PoolInfo] = {}
    if path.exists():
        for row in pd.read_parquet(path).to_dict("records"):
            clean = {k: (None if is_missing(v) else v) for k, v in row.items()}
            if clean.get("fee") is not None:
                clean["fee"] = int(clean["fee"])  # Parquet stores it as float when NaNs exist
            known[row["address"]] = PoolInfo(**clean)

    new = []
    for topic, kind in ((UNISWAP_V2_SWAP_TOPIC, "v2"), (UNISWAP_V3_SWAP_TOPIC, "v3")):
        swaps = logs[logs["topic0"] == topic]
        first_seen = swaps.groupby("address")["block_number"].min()
        new += [(a, int(b), kind) for a, b in first_seen.items() if a not in known]
    log.info("%d pools known, %d new", len(known), len(new))
    missing = 0
    for i, (address, block, kind) in enumerate(new, 1):
        fetch = fetch_v3_pool_info if kind == "v3" else fetch_pool_info
        try:
            known[address] = fetch(rpc, address, block)
        except (BudgetExhausted, OfflineMiss) as exc:
            missing = len(new) - (i - 1)
            log.warning("pool lookup stopped after %d/%d: %s", i - 1, len(new), exc)
            break
    pd.DataFrame([asdict(p) for p in known.values()]).to_parquet(path, index=False)
    if missing and not allow_missing:
        # Swaps on unresolved pools would be silently dropped, undercounting MEV. A first
        # partial run missed about a third of sandwiches this way, so refuse instead.
        raise IncompletePools(
            f"{missing} pools could not be looked up (budget or offline). Rerun when credits "
            "are available, or pass allow_missing=True to accept an undercount."
        )
    return known


def load_swaps(
    config: Config, rpc: RpcClient, allow_missing_pools: bool = False
) -> tuple[list[Swap], pd.DataFrame, pd.DataFrame, dict[str, PoolInfo]]:
    """(decoded V2 and V3 swaps, logs table, transactions table, pool metadata)."""
    logs = read_table(config.processed_dir, "logs")
    txs = read_table(config.processed_dir, "transactions")
    if logs.empty:
        raise SystemExit("no tables yet: run `python -m collector build` first")
    tx_lookup = txs.set_index("tx_hash")[["from", "to"]].to_dict("index")
    pools = load_pools(config, logs, rpc, allow_missing_pools)
    relevant = logs[logs["topic0"].isin(SWAP_TOPICS)].to_dict("records")
    swaps = decode_swaps(relevant, tx_lookup, pools, config.detectors.get("allowed_dexes"))
    return swaps, logs, txs, pools


def build_market(swaps: list[Swap], logs: pd.DataFrame, pools: dict[str, PoolInfo], rpc: RpcClient):
    """profit.market.Market for these swaps: V3 fee tiers, V3 Mint/Burn positions, and `rpc`
    for V3 pre-block states (two cached eth_calls per pool and block, only when asked)."""
    from profit.market import Market
    from profit.v3_state import V3_BURN, V3_MINT

    fees = {a: p.fee for a, p in pools.items() if p.dex == "uniswap_v3" and p.fee is not None}
    changes = logs[logs["topic0"].isin([V3_MINT, V3_BURN])]
    events = zip(changes["block_number"], changes["address"], changes["log_index"], strict=True)
    return Market(swaps, fees, [(int(b), a, int(i)) for b, a, i in events], rpc)
