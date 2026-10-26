"""Uniswap V3 pool state (sqrtPriceX96, liquidity) just before a given swap.

* Not the first swap of its pool in the block: the state the previous swap left behind (from
  that swap's event), provided no Mint or Burn on the pool happened in between.
* The first swap of its pool in the block: the state at the end of the previous block, read
  with slot0() and liquidity() at block - 1 (two cached eth_calls per pool and block), or,
  without RPC, worked back from that swap's own event (`infer_pre_swap_state`).

`single_range` tells whether a sequence of real swaps stayed inside one liquidity range
(active liquidity unchanged throughout); only then is the single-range maths in
profit/uniswap_v3.py exact for it, and for any smaller move in the same direction.
"""

from collections import defaultdict
from collections.abc import Iterable

from collector.rpc import OfflineMiss, RpcClient, RpcError
from common.constants import event_topic, function_selector
from detectors.swaps import Swap
from profit.uniswap_v3 import FEE_DENOMINATOR, Q96, V3State, amount0_delta, amount1_delta

SLOT0 = function_selector("slot0()")
LIQUIDITY = function_selector("liquidity()")
V3_MINT = event_topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
V3_BURN = event_topic("Burn(address,int24,int24,uint128,uint256,uint256)")

PoolKey = tuple[int, str]  # (block_number, pool)


def fetch_pre_block_state(rpc: RpcClient, pool: str, block: int) -> V3State | None:
    """Pool state at the end of block - 1, or None if it cannot be read."""
    try:
        slot0 = rpc.call("eth_call", [{"to": pool, "data": SLOT0}, hex(block - 1)])
        liquidity = rpc.call("eth_call", [{"to": pool, "data": LIQUIDITY}, hex(block - 1)])
    except (RpcError, OfflineMiss, ConnectionError):
        return None
    if not slot0 or len(slot0) < 66 or not liquidity or len(liquidity) < 66:
        return None
    return V3State(int(slot0[2:66], 16), int(liquidity[2:66], 16))


def infer_pre_swap_state(swap: Swap, fee: int, rel_tol: float = 1e-9) -> V3State | None:
    """The state before `swap`, worked back from its event, if it stayed in one range.

    Inside one range the price moves by the output amount alone (token1 out: the square-root
    price falls by amount1 * Q96 / L; token0 out: 1/sqrt-price rises by amount0 / (L * Q96)).
    The input amount, net of the fee, must then agree with that move; if it does not, the
    swap crossed a range boundary and None is returned. Exact up to the contract's rounding
    (relative error around 1e-24 in tests), which is ample for tolerance and size, though
    not for to-the-wei replays.
    """
    if not swap.is_v3 or not swap.liquidity_after or not swap.is_simple:
        return None
    L, after = swap.liquidity_after, swap.sqrt_price_after
    if swap.zero_for_one:  # token0 in, token1 out: price falls
        before = after + swap.amount_out * Q96 // L
        needed = amount0_delta(after, before, L, True)
    else:  # token1 in, token0 out: price rises
        before = L * Q96 * after // (L * Q96 + swap.amount_out * after)
        needed = amount1_delta(before, after, L, True)
    paid = swap.amount_in * (FEE_DENOMINATOR - fee) // FEE_DENOMINATOR
    if needed <= 0 or abs(paid - needed) > rel_tol * needed + 2:
        return None
    return V3State(before, L)


class V3StateBook:
    """Answers `before(swap)` for V3 swaps in the sample."""

    def __init__(
        self,
        swaps: Iterable[Swap],
        liquidity_events: Iterable[tuple[int, str, int]] = (),
        rpc: RpcClient | None = None,
        fees: dict[str, int] | None = None,
    ):
        """liquidity_events: (block, pool, log_index) of every V3 Mint/Burn in the sample."""
        self.rpc = rpc
        self.fees = fees or {}
        self._by_pool: dict[PoolKey, list[Swap]] = defaultdict(list)
        for s in swaps:
            if s.is_v3:
                self._by_pool[(s.block_number, s.pool)].append(s)
        for group in self._by_pool.values():
            group.sort(key=lambda s: s.position)
        self._liquidity: dict[PoolKey, list[int]] = defaultdict(list)
        for block, pool, log_index in liquidity_events:
            self._liquidity[(block, pool)].append(log_index)
        self._pre_block: dict[PoolKey, V3State | None] = {}

    def pre_block(self, block: int, pool: str) -> V3State | None:
        """State at the end of the previous block: read over RPC when one is given (exact),
        else worked back from the pool's first swap in the block (to rounding)."""
        key = (block, pool)
        if key not in self._pre_block:
            state = None
            if self.rpc is not None:
                state = fetch_pre_block_state(self.rpc, pool, block)
            if state is None:
                group = self._by_pool.get(key, [])
                first = group[0] if group else None
                changes = self._liquidity.get(key, [])
                if (first is not None and pool in self.fees
                        and not any(i < first.log_index for i in changes)):  # fmt: skip
                    state = infer_pre_swap_state(first, self.fees[pool])
            self._pre_block[key] = state
        return self._pre_block[key]

    def before(self, swap: Swap) -> V3State | None:
        """State right before `swap`, or None if it cannot be known."""
        group = self._by_pool.get((swap.block_number, swap.pool), [])
        earlier = [s for s in group if s.position < swap.position]
        changes = self._liquidity.get((swap.block_number, swap.pool), [])
        if not earlier:
            if any(i < swap.log_index for i in changes):
                return None  # liquidity changed earlier in the block, before this first swap
            return self.pre_block(swap.block_number, swap.pool)
        prev = earlier[-1]
        if any(prev.log_index < i < swap.log_index for i in changes):
            return None
        return V3State(prev.sqrt_price_after, prev.liquidity_after)


def single_range(start: V3State, swaps: Iterable[Swap]) -> bool:
    """True if every swap left the active liquidity where `start` had it."""
    return all(s.liquidity_after == start.liquidity for s in swaps)
