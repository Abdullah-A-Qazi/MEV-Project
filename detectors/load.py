"""Loads decoded swaps from the Parquet tables, shared by the detectors and the profit step."""

import logging
from dataclasses import asdict

import pandas as pd

from collector.credits import BudgetExhausted
from collector.pipeline import read_table
from collector.pools import PoolInfo, fetch_pool_info
from collector.rpc import OfflineMiss, RpcClient
from common.config import Config
from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC
from common.nulls import is_missing
from detectors.swaps import Swap, decode_v2_swaps

log = logging.getLogger(__name__)

V2_TOPICS = [UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC]


def load_pools(config: Config, logs: pd.DataFrame, rpc: RpcClient) -> dict[str, PoolInfo]:
    """Pool metadata, looking up (once) any pool not yet in data/processed/pools.parquet."""
    path = config.processed_dir / "pools.parquet"
    known: dict[str, PoolInfo] = {}
    if path.exists():
        for row in pd.read_parquet(path).to_dict("records"):
            known[row["address"]] = PoolInfo(
                **{k: (None if is_missing(v) else v) for k, v in row.items()}
            )

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


def load_swaps(
    config: Config, rpc: RpcClient
) -> tuple[list[Swap], pd.DataFrame, pd.DataFrame, dict[str, PoolInfo]]:
    """(decoded V2 swaps, logs table, transactions table, pool metadata)."""
    logs = read_table(config.processed_dir, "logs")
    txs = read_table(config.processed_dir, "transactions")
    if logs.empty:
        raise SystemExit("no tables yet: run `python -m collector build` first")
    tx_lookup = txs.set_index("tx_hash")[["from", "to"]].to_dict("index")
    pools = load_pools(config, logs, rpc)
    relevant = logs[logs["topic0"].isin(V2_TOPICS)].to_dict("records")
    swaps = decode_v2_swaps(relevant, tx_lookup, pools, config.detectors.get("allowed_dexes"))
    return swaps, logs, txs, pools
