"""Sandwich attack detector, with victim identification.

Definition (after Qin et al. 2022 and Torres et al., "Frontrunner Jones", 2021). In one block
and one pool, a sandwich is three or more swaps

    front-run F  <  victim(s) V  <  back-run B     (ordered by transaction index)

such that
  * F and B are in different transactions by the same attacker,
  * F and every V trade in the same direction (e.g. all buy token Y with token X),
  * B trades in the opposite direction, selling roughly what F bought:
    |B.amount_in - F.amount_out| <= tolerance * F.amount_out,
  * each V is in a transaction that is not the attacker's.

Attacker identity. A swap's "actors" are the EOA that signed the transaction plus the
contract it called, unless that contract is a public router (many unrelated users call the
same router, so it says nothing about who they are). Two swaps belong to the same attacker
if they share an actor; this catches bots that rotate EOAs but reuse one contract.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from common.constants import PUBLIC_ROUTERS
from detectors.swaps import Swap


@dataclass(frozen=True)
class Sandwich:
    block_number: int
    pool: str
    front: Swap
    back: Swap
    victims: tuple[Swap, ...]

    @property
    def attacker(self) -> str:
        """Bot contract if there is one, else the signing EOA."""
        to = self.front.tx_to
        return to if to and to not in PUBLIC_ROUTERS else self.front.tx_from

    @property
    def token_x(self) -> str:
        """Token the attacker spends in the front-run and gets back in the back-run."""
        return self.front.token_in

    @property
    def token_y(self) -> str:
        """Token the attacker buys in the front-run and sells in the back-run."""
        return self.front.token_out

    @property
    def gross_profit_x(self) -> int:
        """Back-run output minus front-run input, in raw units of token_x.

        Exact when the back-run sells everything the front-run bought; otherwise the
        attacker also keeps (or sells extra) `residual_y` of token_y.
        """
        return self.back.amount_out - self.front.amount_in

    @property
    def residual_y(self) -> int:
        return self.front.amount_out - self.back.amount_in


def actors(swap: Swap) -> frozenset[str]:
    ids = {swap.tx_from} if swap.tx_from else set()
    if swap.tx_to and swap.tx_to not in PUBLIC_ROUTERS:
        ids.add(swap.tx_to)
    return frozenset(ids)


def _amounts_match(front: Swap, back: Swap, tolerance: float) -> bool:
    return abs(back.amount_in - front.amount_out) <= tolerance * front.amount_out


def find_sandwiches_in_pool(pool_swaps: list[Swap], tolerance: float = 0.2) -> list[Sandwich]:
    """Sandwiches among the swaps of one pool within one block."""
    ordered = sorted((s for s in pool_swaps if s.is_simple), key=lambda s: s.position)
    used: set[tuple[str, int]] = set()  # (tx_hash, log_index) of swaps already in a sandwich
    found: list[Sandwich] = []

    for i, front in enumerate(ordered):
        if (front.tx_hash, front.log_index) in used:
            continue
        attacker = actors(front)
        if not attacker:
            continue
        for j in range(i + 1, len(ordered)):
            back = ordered[j]
            if back.tx_index <= front.tx_index or (back.tx_hash, back.log_index) in used:
                continue
            if back.zero_for_one == front.zero_for_one or not (actors(back) & attacker):
                continue
            if not _amounts_match(front, back, tolerance):
                continue
            victims = tuple(
                v
                for v in ordered[i + 1 : j]
                if front.tx_index < v.tx_index < back.tx_index
                and v.zero_for_one == front.zero_for_one
                and not (actors(v) & attacker)
            )
            if not victims:
                continue
            found.append(Sandwich(front.block_number, front.pool, front, back, victims))
            used.add((front.tx_hash, front.log_index))
            used.add((back.tx_hash, back.log_index))
            break  # this front-run is matched with its nearest valid back-run
    return found


def detect_sandwiches(swaps: Iterable[Swap], tolerance: float = 0.2) -> list[Sandwich]:
    by_block_pool: dict[tuple[int, str], list[Swap]] = defaultdict(list)
    for swap in swaps:
        by_block_pool[(swap.block_number, swap.pool)].append(swap)
    result = []
    for pool_swaps in by_block_pool.values():
        result.extend(find_sandwiches_in_pool(pool_swaps, tolerance))
    return sorted(result, key=lambda s: (s.block_number, s.front.tx_index, s.pool))
