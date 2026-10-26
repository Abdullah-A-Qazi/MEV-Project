"""Decodes Uniswap V2-style and V3 Swap events into one Swap record type.

V2: a pair emits Sync(reserve0, reserve1) and then Swap(...) in the same call, so each Swap is
paired with the closest preceding Sync from the same pool in the same transaction. That Sync
gives the reserves right after the swap, and the reserves right before it follow exactly
(see profit.uniswap_v2.reserves_before_swap). No extra RPC calls are needed.

V3: Swap(sender, recipient, amount0, amount1, sqrtPriceX96, liquidity, tick) carries signed
amounts (positive = into the pool) and the pool's price, active liquidity and tick after the
swap. They are mapped to the same in/out fields as V2, so the detectors treat both alike;
the V3 state fields feed profit.uniswap_v3.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from collector.pools import PoolInfo
from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC, UNISWAP_V3_SWAP_TOPIC
from common.nulls import is_missing
from profit.uniswap_v2 import Reserves, reserves_before_swap


@dataclass(frozen=True)
class Swap:
    block_number: int
    tx_index: int
    tx_hash: str
    log_index: int
    pool: str
    dex: str
    token0: str
    token1: str
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int
    sender: str  # msg.sender of pair.swap (usually a router or a bot contract)
    recipient: str  # `to` argument of pair.swap
    tx_from: str  # EOA that signed the transaction
    tx_to: str | None  # contract the transaction called
    reserves_after: Reserves | None
    # Uniswap V3 only: pool state right after the swap (None for V2)
    sqrt_price_after: int | None = None
    liquidity_after: int | None = None
    tick_after: int | None = None

    @property
    def is_v3(self) -> bool:
        return self.sqrt_price_after is not None

    @property
    def zero_for_one(self) -> bool:
        """True if the swap sells token0 for token1."""
        return self.amount0_in > 0 and self.amount1_out > 0

    @property
    def is_simple(self) -> bool:
        """Exactly one token in and the other out. Other shapes (flash-swap repayments with
        both amounts in, or zero-amount swaps) are skipped by the detectors."""
        one_for_zero = self.amount1_in > 0 and self.amount0_out > 0
        return (self.zero_for_one and self.amount1_in == 0 and self.amount0_out == 0) or (
            one_for_zero and self.amount0_in == 0 and self.amount1_out == 0
        )

    @property
    def token_in(self) -> str:
        return self.token0 if self.zero_for_one else self.token1

    @property
    def token_out(self) -> str:
        return self.token1 if self.zero_for_one else self.token0

    @property
    def amount_in(self) -> int:
        return self.amount0_in if self.zero_for_one else self.amount1_in

    @property
    def amount_out(self) -> int:
        return self.amount1_out if self.zero_for_one else self.amount0_out

    @property
    def reserves_before(self) -> Reserves | None:
        if self.reserves_after is None:
            return None
        return reserves_before_swap(
            self.reserves_after,
            self.amount0_in,
            self.amount1_in,
            self.amount0_out,
            self.amount1_out,
        )

    @property
    def position(self) -> tuple[int, int, int]:
        """Sort key: chronological order within the chain."""
        return (self.block_number, self.tx_index, self.log_index)


def _words(data: str) -> list[int]:
    payload = data[2:] if data.startswith("0x") else data
    return [int(payload[i : i + 64], 16) for i in range(0, len(payload), 64)]


def _signed(word: int) -> int:
    """Two's-complement int256 from a 256-bit word."""
    return word - (1 << 256) if word >= 1 << 255 else word


def _topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def decode_swaps(
    logs: Iterable[Mapping[str, Any]],
    txs: Mapping[str, Mapping[str, Any]],
    pools: Mapping[str, PoolInfo],
    allowed_dexes: Iterable[str] | None = None,
) -> list[Swap]:
    """Decode V2 and V3 swaps from log rows (as produced by collector.pipeline).

    `txs` maps tx_hash -> {"from": ..., "to": ...}; `pools` maps pool address -> PoolInfo.
    Swaps on pools whose DEX is not in `allowed_dexes` are dropped (None = keep all known).
    """
    allowed = set(allowed_dexes) if allowed_dexes is not None else None
    ordered = sorted(logs, key=lambda r: (r["block_number"], r["log_index"]))
    last_sync: dict[tuple[str, str], Reserves] = {}
    swaps: list[Swap] = []
    for row in ordered:
        topic0 = row["topic0"]
        pool = row["address"].lower()
        if topic0 == UNISWAP_V2_SYNC_TOPIC:
            words = _words(row["data"])
            if len(words) == 2:
                last_sync[(row["tx_hash"], pool)] = Reserves(words[0], words[1])
            continue
        if topic0 not in (UNISWAP_V2_SWAP_TOPIC, UNISWAP_V3_SWAP_TOPIC):
            continue
        info = pools.get(pool)
        if info is None or info.token0 is None or info.token1 is None:
            continue
        if info.dex == "unknown" or (allowed is not None and info.dex not in allowed):
            continue
        if is_missing(row["topic1"]) or is_missing(row["topic2"]):
            continue
        words = _words(row["data"])
        tx = txs.get(row["tx_hash"], {})
        common = {
            "block_number": int(row["block_number"]),
            "tx_index": int(row["tx_index"]),
            "tx_hash": row["tx_hash"],
            "log_index": int(row["log_index"]),
            "pool": pool,
            "dex": info.dex,
            "token0": info.token0,
            "token1": info.token1,
            "sender": _topic_address(row["topic1"]),
            "recipient": _topic_address(row["topic2"]),
            "tx_from": "" if is_missing(tx.get("from")) else tx["from"].lower(),
            "tx_to": None if is_missing(tx.get("to")) else tx["to"].lower(),
        }
        if topic0 == UNISWAP_V2_SWAP_TOPIC:
            if len(words) != 4:
                continue
            swaps.append(
                Swap(
                    **common,
                    amount0_in=words[0],
                    amount1_in=words[1],
                    amount0_out=words[2],
                    amount1_out=words[3],
                    # pop: a Sync belongs to exactly one Swap
                    reserves_after=last_sync.pop((row["tx_hash"], pool), None),
                )
            )
        else:
            if len(words) != 5:
                continue
            amount0, amount1 = _signed(words[0]), _signed(words[1])
            swaps.append(
                Swap(
                    **common,
                    amount0_in=max(amount0, 0),
                    amount1_in=max(amount1, 0),
                    amount0_out=max(-amount0, 0),
                    amount1_out=max(-amount1, 0),
                    reserves_after=None,
                    sqrt_price_after=words[2],
                    liquidity_after=words[3],
                    tick_after=_signed(words[4]),
                )
            )
    return swaps
