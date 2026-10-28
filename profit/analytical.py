"""RQ1's analytical estimates: the profit a searcher could predict before its block was built.

Two estimates per event, both in the event's own token (start token / token X):

* `estimate_*` (pre-block): the searcher's plan, priced on what was public at the end of the
  previous block, i.e. each pool's reserves before its first swap in the block. Earlier
  transactions in the same block are ignored, because a searcher bidding for a position
  cannot know them. The bot's own trade sizes are used as executed.
* `formula_*` (at execution): the same trades priced with the pool's own formula (V2, or
  single-range V3 via profit/market.py) on the state each swap actually met.

Together with the actual gross (from the swap events) they split the gap exactly:

    estimate - actual = (estimate - formula)  state drift: other transactions moved the pool
                      + (formula - actual)    execution shortfall: the trade took less than
                                              the formula allows (a conservative amountOut,
                                              transfer-taxed tokens)

When nothing else touches the pools, drift is exactly zero (tests/test_analytical.py).
"""

from collections.abc import Iterable, Mapping

from detectors.swaps import Swap
from profit.uniswap_v2 import InsufficientLiquidity, Reserves, apply_swap

PoolKey = tuple[int, str]  # (block_number, pool)


def pre_block_reserves(swaps: Iterable[Swap]) -> dict[PoolKey, Reserves]:
    """Each pool's reserves before its first swap in the block."""
    earliest: dict[PoolKey, Swap] = {}
    for swap in swaps:
        key = (swap.block_number, swap.pool)
        if key not in earliest or swap.position < earliest[key].position:
            earliest[key] = swap
    return {k: s.reserves_before for k, s in earliest.items() if s.reserves_before is not None}


def _executed(reserves: Reserves, swap: Swap) -> Reserves:
    """Reserves after `swap`, using the amounts it actually moved."""
    return Reserves(
        reserves.reserve0 + swap.amount0_in - swap.amount0_out,
        reserves.reserve1 + swap.amount1_in - swap.amount1_out,
    )


def estimate_arbitrage(cycle: list[Swap], pre: Mapping[PoolKey, Reserves]) -> int | None:
    """Pre-block estimate: the cycle's first input chained through each pool's pre-block state."""
    state: dict[str, Reserves] = {}
    amount = cycle[0].amount_in
    try:
        for swap in cycle:
            reserves = state.get(swap.pool) or pre.get((swap.block_number, swap.pool))
            if reserves is None:
                return None
            state[swap.pool], amount = apply_swap(reserves, amount, swap.zero_for_one)
    except (InsufficientLiquidity, ValueError):
        return None
    return amount - cycle[0].amount_in


def formula_arbitrage(cycle: list[Swap]) -> int | None:
    """At-execution formula: the same chain on the reserves each swap actually met."""
    amount = cycle[0].amount_in
    try:
        for swap in cycle:
            if swap.reserves_before is None:
                return None
            _, amount = apply_swap(swap.reserves_before, amount, swap.zero_for_one)
    except (InsufficientLiquidity, ValueError):
        return None
    return amount - cycle[0].amount_in


def estimate_sandwich(
    front: Swap, victims: list[Swap], back: Swap, pre: Mapping[PoolKey, Reserves]
) -> int | None:
    """Pre-block estimate: front-run and victims as executed on the pre-block state, then the
    back-run's actual input priced on the resulting state. Other transactions are left out."""
    reserves = pre.get((front.block_number, front.pool))
    if reserves is None:
        return None
    reserves = _executed(reserves, front)
    for victim in sorted(victims, key=lambda v: v.position):
        reserves = _executed(reserves, victim)
    try:
        _, back_out = apply_swap(reserves, back.amount_in, back.zero_for_one)
    except (InsufficientLiquidity, ValueError):
        return None
    return back_out - front.amount_in


def formula_sandwich(front: Swap, back: Swap) -> int | None:
    """At-execution formula: the back-run's input priced on the reserves it actually met."""
    if back.reserves_before is None:
        return None
    try:
        _, back_out = apply_swap(back.reserves_before, back.amount_in, back.zero_for_one)
    except (InsufficientLiquidity, ValueError):
        return None
    return back_out - front.amount_in


# --- Both protocols ----------------------------------------------------------------------------
# The functions above take V2 reserves directly (and are pinned by the tests). The ones below
# do the same through profit.market.Market, so V3 pools are priced with V3 maths. A V3 replay
# is only trusted when the real swaps it rests on stayed inside one liquidity range.


def _v3_path(market, swap: Swap) -> list[Swap]:
    """V3 swaps in `swap`'s pool and block, up to and including `swap`."""
    group = market.v3._by_pool.get((swap.block_number, swap.pool), [])
    return [s for s in group if s.position <= swap.position]


def market_estimate_arbitrage(market, cycle: list[Swap]) -> int | None:
    state: dict[str, object] = {}
    amount = cycle[0].amount_in
    try:
        for swap in cycle:
            current = state.get(swap.pool)
            if current is None:
                current = market.pre_block(swap.block_number, swap.pool)
                if current is None or not market.trusted_v3_path(current, _v3_path(market, swap)):
                    return None
            state[swap.pool], amount = market.trade(swap.pool, current, amount, swap.zero_for_one)
    except ValueError:
        return None
    return amount - cycle[0].amount_in


def market_formula_arbitrage(market, cycle: list[Swap]) -> int | None:
    amount = cycle[0].amount_in
    try:
        for swap in cycle:
            before = market.before(swap)
            if before is None or not market.trusted_v3_path(before, [swap]):
                return None
            _, amount = market.trade(swap.pool, before, amount, swap.zero_for_one)
    except ValueError:
        return None
    return amount - cycle[0].amount_in


def market_estimate_sandwich(market, front: Swap, victims: list[Swap], back: Swap) -> int | None:
    start = market.pre_block(front.block_number, front.pool)
    if start is None:
        return None
    if isinstance(start, Reserves):  # V2: as estimate_sandwich, front and victims as executed
        return estimate_sandwich(front, victims, back, {(front.block_number, front.pool): start})
    if not market.trusted_v3_path(start, _v3_path(market, back)):
        return None
    try:
        state, _ = market.trade(front.pool, start, front.amount_in, front.zero_for_one)
        for victim in sorted(victims, key=lambda v: v.position):
            state, _ = market.trade(front.pool, state, victim.amount_in, victim.zero_for_one)
        _, back_out = market.trade(front.pool, state, back.amount_in, back.zero_for_one)
    except ValueError:
        return None
    return back_out - front.amount_in


def market_formula_sandwich(market, front: Swap, back: Swap) -> int | None:
    before = market.before(back)
    if before is None or not market.trusted_v3_path(before, [back]):
        return None
    try:
        _, back_out = market.trade(back.pool, before, back.amount_in, back.zero_for_one)
    except ValueError:
        return None
    return back_out - front.amount_in
