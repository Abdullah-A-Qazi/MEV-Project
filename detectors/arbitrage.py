"""Atomic arbitrage detector.

Definition (following Qin, Zhou & Gervais, "Quantifying Blockchain Extractable Value", 2022):
a transaction is an arbitrage if a run of its swaps forms a cycle

    token A -> token B -> ... -> token A

across at least two distinct pools, where each swap's output token is the next swap's input
token, and the final output amount of A is larger than the first input amount of A.

Only data already saved locally is used (no RPC calls). Known limitations, to be quantified
in validation: cycles that interleave with unrelated swaps, or that split the trade across
parallel paths, are not found; swaps on DEXs we do not decode yet (e.g. Uniswap V3) break
the cycle and hide the arbitrage.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from detectors.swaps import Swap


@dataclass(frozen=True)
class Arbitrage:
    block_number: int
    tx_index: int
    tx_hash: str
    tx_from: str
    tx_to: str | None
    start_token: str
    amount_in: int  # of start_token, into the first swap
    amount_out: int  # of start_token, out of the last swap
    pools: tuple[str, ...]
    log_indices: tuple[int, ...]

    @property
    def gross_profit(self) -> int:
        """Profit in raw units of start_token, before gas and builder payments."""
        return self.amount_out - self.amount_in


def _cycle_from(swaps: list[Swap], start: int) -> int | None:
    """Index of the swap that closes a cycle beginning at swaps[start], or None."""
    start_token = swaps[start].token_in
    for j in range(start + 1, len(swaps)):
        if swaps[j].token_in != swaps[j - 1].token_out:
            return None
        if swaps[j].token_out == start_token:
            return j
    return None


def find_arbitrages_in_tx(swaps: list[Swap]) -> list[Arbitrage]:
    """Profitable swap cycles inside one transaction's swaps (in log order)."""
    ordered = sorted((s for s in swaps if s.is_simple), key=lambda s: s.log_index)
    found: list[Arbitrage] = []
    i = 0
    while i < len(ordered):
        end = _cycle_from(ordered, i)
        if end is None:
            i += 1
            continue
        cycle = ordered[i : end + 1]
        first, last = cycle[0], cycle[-1]
        pools = tuple(s.pool for s in cycle)
        if len(set(pools)) >= 2 and last.amount_out > first.amount_in:
            found.append(
                Arbitrage(
                    block_number=first.block_number,
                    tx_index=first.tx_index,
                    tx_hash=first.tx_hash,
                    tx_from=first.tx_from,
                    tx_to=first.tx_to,
                    start_token=first.token_in,
                    amount_in=first.amount_in,
                    amount_out=last.amount_out,
                    pools=pools,
                    log_indices=tuple(s.log_index for s in cycle),
                )
            )
        i = end + 1
    return found


def detect_arbitrages(swaps: Iterable[Swap]) -> list[Arbitrage]:
    by_tx: dict[str, list[Swap]] = defaultdict(list)
    for swap in swaps:
        by_tx[swap.tx_hash].append(swap)
    result = []
    for tx_swaps in by_tx.values():
        result.extend(find_arbitrages_in_tx(tx_swaps))
    return sorted(result, key=lambda a: (a.block_number, a.tx_index, a.log_indices))
