"""Collection pipeline: fetch sampled blocks into the cache, then flatten them into Parquet.

Two stages, kept separate on purpose:

* `collect` spends credits. For each sampled block it fetches the block (with full
  transactions) and all its receipts: 80 + 1000 Infura credits per block. Blocks already in
  the cache cost nothing, so the stage can be stopped at any time (e.g. when the daily
  budget runs out) and simply rerun to resume.
* `build_tables` spends nothing. It reads only from the cache (offline mode) and writes
  blocks / transactions / receipts / logs tables. Rebuilding is deterministic.

uint256 values (amounts, values, gas prices) are stored as decimal strings, because they
do not fit in 64-bit Parquet integers.
"""

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from collector.credits import BudgetExhausted
from collector.rpc import RpcClient

log = logging.getLogger(__name__)


def _int(value: str | None) -> int | None:
    return None if value is None else int(value, 16)


def _dec(value: str | None) -> str | None:
    """Hex quantity -> decimal string (for uint256 columns)."""
    return None if value is None else str(int(value, 16))


def _addr(value: str | None) -> str | None:
    return None if value is None else value.lower()


def fetch_block(rpc: RpcClient, number: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    block = rpc.call("eth_getBlockByNumber", [hex(number), True])
    receipts = rpc.call("eth_getBlockReceipts", [hex(number)])
    if block is None or receipts is None:
        raise LookupError(f"block {number} not available from provider")
    if len(receipts) != len(block["transactions"]):
        raise ValueError(
            f"block {number}: {len(block['transactions'])} txs but {len(receipts)} receipts"
        )
    return block, receipts


def collect(rpc: RpcClient, blocks: Iterable[int]) -> dict[str, int]:
    """Fetch every block in `blocks` that is not cached yet. Stops cleanly when out of budget."""
    blocks = list(blocks)
    done = 0
    for i, number in enumerate(blocks, 1):
        try:
            fetch_block(rpc, number)
        except BudgetExhausted as exc:
            log.warning("stopping at block %d (%d/%d): %s", number, i, len(blocks), exc)
            return {"done": done, "total": len(blocks), "stopped_early": 1}
        done += 1
        if i % 100 == 0:
            log.info("%d/%d blocks (network calls: %d)", i, len(blocks), rpc.network_calls)
    return {"done": done, "total": len(blocks), "stopped_early": 0}


def normalise_block(
    block: dict[str, Any], receipts: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Flatten one raw block + receipts into rows for each table."""
    number = _int(block["number"])
    timestamp = _int(block["timestamp"])
    rows: dict[str, list[dict[str, Any]]] = {
        "blocks": [
            {
                "block_number": number,
                "block_hash": block["hash"].lower(),
                "timestamp": timestamp,
                "fee_recipient": _addr(block["miner"]),
                "base_fee_per_gas": _dec(block.get("baseFeePerGas")),
                "gas_used": _int(block["gasUsed"]),
                "gas_limit": _int(block["gasLimit"]),
                "tx_count": len(block["transactions"]),
            }
        ],
        "transactions": [],
        "receipts": [],
        "logs": [],
    }
    for tx in block["transactions"]:
        rows["transactions"].append(
            {
                "block_number": number,
                "tx_index": _int(tx["transactionIndex"]),
                "tx_hash": tx["hash"].lower(),
                "from": _addr(tx["from"]),
                "to": _addr(tx.get("to")),
                "value": _dec(tx["value"]),
                "nonce": _int(tx["nonce"]),
                "tx_type": _int(tx.get("type", "0x0")),
                "gas_limit": _int(tx["gas"]),
                "gas_price": _dec(tx.get("gasPrice")),
                "max_fee_per_gas": _dec(tx.get("maxFeePerGas")),
                "max_priority_fee_per_gas": _dec(tx.get("maxPriorityFeePerGas")),
                "input": tx["input"].lower(),
            }
        )
    for receipt in receipts:
        rows["receipts"].append(
            {
                "block_number": number,
                "tx_index": _int(receipt["transactionIndex"]),
                "tx_hash": receipt["transactionHash"].lower(),
                "status": _int(receipt.get("status")),
                "gas_used": _int(receipt["gasUsed"]),
                "effective_gas_price": _dec(receipt.get("effectiveGasPrice")),
                "contract_address": _addr(receipt.get("contractAddress")),
            }
        )
        for entry in receipt["logs"]:
            topics = [t.lower() for t in entry["topics"]] + [None] * 4
            rows["logs"].append(
                {
                    "block_number": number,
                    "tx_index": _int(entry["transactionIndex"]),
                    "tx_hash": entry["transactionHash"].lower(),
                    "log_index": _int(entry["logIndex"]),
                    "address": _addr(entry["address"]),
                    "topic0": topics[0],
                    "topic1": topics[1],
                    "topic2": topics[2],
                    "topic3": topics[3],
                    "data": entry["data"].lower(),
                }
            )
    return rows


TABLES = ("blocks", "transactions", "receipts", "logs")


def build_tables(
    rpc: RpcClient, blocks: Iterable[int], out_dir: Path, chunk_blocks: int = 250
) -> dict[str, int]:
    """Write Parquet tables for all cached blocks in `blocks`. Returns row counts.

    Each table is a directory of part files (`<table>/part-00000.parquet`, ...), written every
    `chunk_blocks` blocks to bound memory use; pandas and DuckDB read such a directory as one
    table. Existing part files are replaced, so a rebuild is deterministic.

    Uses `rpc` only as a cache reader; pass an offline client so that this can never spend
    credits. Blocks that are not cached yet are skipped and counted under "missing_blocks".
    """
    for name in TABLES:
        table_dir = out_dir / name
        table_dir.mkdir(parents=True, exist_ok=True)
        for old in table_dir.glob("part-*.parquet"):
            old.unlink()

    counts = {"missing_blocks": 0, **{name: 0 for name in TABLES}}
    buffer: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
    part = 0

    def flush() -> None:
        nonlocal part
        if not buffer["blocks"]:
            return
        for name in TABLES:
            pd.DataFrame(buffer[name]).to_parquet(
                out_dir / name / f"part-{part:05d}.parquet", index=False
            )
            counts[name] += len(buffer[name])
            buffer[name] = []
        part += 1

    for number in sorted(set(blocks)):
        if not (
            rpc.cache.contains("eth_getBlockByNumber", [hex(number), True])
            and rpc.cache.contains("eth_getBlockReceipts", [hex(number)])
        ):
            counts["missing_blocks"] += 1
            continue
        for name, table_rows in normalise_block(*fetch_block(rpc, number)).items():
            buffer[name].extend(table_rows)
        if len(buffer["blocks"]) >= chunk_blocks:
            flush()
    flush()
    return counts


def read_table(processed_dir: Path, name: str) -> pd.DataFrame:
    """Load one table written by build_tables (empty frame if nothing was built yet)."""
    parts = sorted((processed_dir / name).glob("part-*.parquet"))
    if not parts:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
