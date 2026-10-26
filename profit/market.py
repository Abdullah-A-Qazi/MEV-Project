"""One interface over Uniswap V2 and V3 pool state, for replaying swaps.

The analytical estimates (RQ1), victim losses and pricing all need the same few things for
a pool: its state before the block, its state right before a given swap, and the result of
an exact-input trade on a state. V2 states are reserves (Reserves), V3 states are
(sqrtPriceX96, liquidity) (V3State); this module hides the difference.

V3 replays are single-range (profit/uniswap_v3.py). `trusted_v3_path` says whether the real
swaps a replay is based on stayed in one range; when they did not, results are None rather
than approximate.
"""

from collections.abc import Iterable, Mapping

from collector.rpc import RpcClient
from detectors.swaps import Swap
from profit.uniswap_v2 import InsufficientLiquidity, Reserves, apply_swap
from profit.uniswap_v3 import Q96, V3State, swap_exact_input
from profit.v3_state import V3StateBook

State = Reserves | V3State


class Market:
    def __init__(
        self,
        swaps: list[Swap],
        fees: Mapping[str, int],
        liquidity_events: Iterable[tuple[int, str, int]] = (),
        rpc: RpcClient | None = None,
    ):
        """fees: V3 pool -> fee tier. rpc: for V3 pre-block states (cached eth_calls)."""
        self.fees = dict(fees)
        self.v3 = V3StateBook(swaps, liquidity_events, rpc, self.fees)
        earliest: dict[tuple[int, str], Swap] = {}
        for swap in swaps:
            key = (swap.block_number, swap.pool)
            if key not in earliest or swap.position < earliest[key].position:
                earliest[key] = swap
        self._earliest = earliest

    # -- states ------------------------------------------------------------------------------

    def pre_block(self, block: int, pool: str) -> State | None:
        """Pool state before its first swap in `block`."""
        first = self._earliest.get((block, pool))
        if first is None:
            return None
        if first.is_v3:
            return self.v3.before(first)
        return first.reserves_before

    def before(self, swap: Swap) -> State | None:
        """Pool state right before `swap` actually executed."""
        return self.v3.before(swap) if swap.is_v3 else swap.reserves_before

    def first_in_block(self, swap: Swap) -> bool:
        return self._earliest.get((swap.block_number, swap.pool)) == swap

    # -- trading -----------------------------------------------------------------------------

    def trade(self, pool: str, state: State, amount_in: int, zero_for_one: bool):
        """Exact-input trade on `state`: (new state, amount out). Raises ValueError."""
        if isinstance(state, V3State):
            fee = self.fees.get(pool)
            if fee is None:
                raise ValueError(f"no fee tier for V3 pool {pool}")
            return swap_exact_input(state, amount_in, zero_for_one, fee)
        try:
            return apply_swap(state, amount_in, zero_for_one)
        except InsufficientLiquidity as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def depth(state: State, zero_for_one: bool) -> tuple[int, int]:
        """(input-side, output-side) reserves; for V3 the active range's virtual reserves."""
        if isinstance(state, V3State):
            x = state.liquidity * Q96 // state.sqrt_price_x96  # virtual token0
            y = state.liquidity * state.sqrt_price_x96 // Q96  # virtual token1
            return (x, y) if zero_for_one else (y, x)
        return (
            (state.reserve0, state.reserve1) if zero_for_one else (state.reserve1, state.reserve0)
        )

    @staticmethod
    def trusted_v3_path(start: State | None, swaps: Iterable[Swap]) -> bool:
        """For V3: every real swap involved kept the active liquidity of `start`."""
        if not isinstance(start, V3State):
            return start is not None
        return all(s.liquidity_after == start.liquidity for s in swaps if s.is_v3)
