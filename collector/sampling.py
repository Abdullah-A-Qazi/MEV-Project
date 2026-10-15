"""Turns the sampling rule in config.yaml into an explicit, committed list of block numbers.

Rule: one UTC day every `every_n_days`, and `blocks_per_day` blocks drawn uniformly at random
from each sampled day. Sandwiches and atomic arbitrages happen within a single block, so a
random sample of blocks gives unbiased per-block rates, while whole UTC days line up with
the daily files of Mempool Dumpster (RQ2).

Each day's draw is seeded with (seed, date), so adding or removing days never changes the
blocks chosen for the other days.
"""

import csv
import random
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from collector.rpc import RpcClient


def sampled_days(start: date, end: date, every_n_days: int) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=every_n_days)


def block_timestamp(rpc: RpcClient, number: int) -> int:
    header = rpc.call("eth_getBlockByNumber", [hex(number), False])
    return int(header["timestamp"], 16)


def first_block_at_or_after(rpc: RpcClient, timestamp: int, lo: int, hi: int) -> int:
    """Smallest block number in [lo, hi] whose timestamp is >= `timestamp` (binary search).

    About log2(hi - lo) header lookups; every lookup is cached, so repeated runs are free.
    """
    if block_timestamp(rpc, hi) < timestamp:
        raise ValueError(f"timestamp {timestamp} is after block {hi}")
    while lo < hi:
        mid = (lo + hi) // 2
        if block_timestamp(rpc, mid) < timestamp:
            lo = mid + 1
        else:
            hi = mid
    return lo


def day_block_range(rpc: RpcClient, day: date, head: int) -> tuple[int, int]:
    """Inclusive (first, last) block numbers produced on a UTC day."""
    start_ts = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    end_ts = start_ts + 86_400
    first = first_block_at_or_after(rpc, start_ts, 0, head)
    last = first_block_at_or_after(rpc, end_ts, first, head) - 1
    return first, last


def draw_blocks(first: int, last: int, n: int | None, seed: int, day: date) -> list[int]:
    blocks = list(range(first, last + 1))
    if n is None or n >= len(blocks):
        return blocks
    rng = random.Random(f"{seed}-{day.isoformat()}")
    return sorted(rng.sample(blocks, n))


def build_sample(rpc: RpcClient, sample_cfg: dict[str, Any]) -> list[tuple[str, int]]:
    """Resolve the configured sampling rule to [(utc_date, block_number), ...]."""
    head = rpc.head() - 64  # stay on finalized blocks so that every lookup is cacheable
    rows = []
    for day in sampled_days(
        date.fromisoformat(sample_cfg["start_date"]),
        date.fromisoformat(sample_cfg["end_date"]),
        int(sample_cfg["every_n_days"]),
    ):
        first, last = day_block_range(rpc, day, head)
        for block in draw_blocks(
            first, last, sample_cfg.get("blocks_per_day"), sample_cfg["seed"], day
        ):
            rows.append((day.isoformat(), block))
    return rows


def write_sample(path: Path, rows: list[tuple[str, int]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["utc_date", "block_number"])
        writer.writerows(rows)


def read_sample(path: Path) -> list[int]:
    with open(path, newline="") as f:
        return [int(row["block_number"]) for row in csv.DictReader(f)]
