"""Uniswap V3 swap maths within one liquidity range, in exact integer arithmetic.

A V3 pool's state is (sqrtPriceX96, liquidity L). Inside one range (between two initialised
ticks) a swap follows these exact formulas; at a range boundary L changes and the swap
continues in the next range. We port the single-range step from Uniswap's v3-core
(SqrtPriceMath.sol and SwapMath.computeSwapStep), including every rounding direction, so a
swap that stays inside one range is reproduced to the wei (tests/test_uniswap_v3_golden.py).

We do not have the pools' tick maps, so a swap that would cross a boundary cannot be priced.
Callers check this: a trade whose real execution kept L unchanged stayed inside one range,
and any smaller move in the same direction from the same start does too.

Reference: https://github.com/Uniswap/v3-core/tree/main/contracts/libraries
"""

from dataclasses import dataclass

Q96 = 1 << 96
FEE_DENOMINATOR = 1_000_000
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342


@dataclass(frozen=True)
class V3State:
    sqrt_price_x96: int
    liquidity: int


def _div_up(a: int, b: int) -> int:
    return -(-a // b)


def _mul_div(a: int, b: int, d: int) -> int:
    return a * b // d


def _mul_div_up(a: int, b: int, d: int) -> int:
    return _div_up(a * b, d)


def amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount0Delta."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    numerator1, numerator2 = liquidity << 96, sqrt_b - sqrt_a
    if round_up:
        return _div_up(_mul_div_up(numerator1, numerator2, sqrt_b), sqrt_a)
    return _mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """SqrtPriceMath.getAmount1Delta."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return _mul_div_up(liquidity, sqrt_b - sqrt_a, Q96)
    return _mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def _next_from_amount0_up(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    """SqrtPriceMath.getNextSqrtPriceFromAmount0RoundingUp (both overflow branches)."""
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    product = amount * sqrt_p
    if add:
        if product < 1 << 256 and numerator1 + product >= numerator1:
            return _mul_div_up(numerator1, sqrt_p, numerator1 + product)
        return _div_up(numerator1, numerator1 // sqrt_p + amount)
    if product >= 1 << 256 or numerator1 <= product:
        raise ValueError("not enough liquidity for that output")
    return _mul_div_up(numerator1, sqrt_p, numerator1 - product)


def _next_from_amount1_down(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    """SqrtPriceMath.getNextSqrtPriceFromAmount1RoundingDown."""
    if add:
        quotient = (
            (amount << 96) // liquidity if amount < 1 << 160 else _mul_div(amount, Q96, liquidity)
        )
        return sqrt_p + quotient
    quotient = (
        _div_up(amount << 96, liquidity)
        if amount < 1 << 160
        else _mul_div_up(amount, Q96, liquidity)
    )
    if sqrt_p <= quotient:
        raise ValueError("not enough liquidity for that output")
    return sqrt_p - quotient


def compute_swap_step(
    sqrt_current: int, sqrt_target: int, liquidity: int, amount_remaining: int, fee_pips: int
) -> tuple[int, int, int, int]:
    """SwapMath.computeSwapStep: (sqrt_next, amount_in, amount_out, fee_amount).

    amount_remaining > 0 = exact input, < 0 = exact output (as in the contract).
    """
    zero_for_one = sqrt_current >= sqrt_target
    exact_in = amount_remaining >= 0
    if exact_in:
        remaining_less_fee = _mul_div(amount_remaining, FEE_DENOMINATOR - fee_pips, FEE_DENOMINATOR)
        amount_in = (
            amount0_delta(sqrt_target, sqrt_current, liquidity, True)
            if zero_for_one
            else amount1_delta(sqrt_current, sqrt_target, liquidity, True)
        )
        if remaining_less_fee >= amount_in:
            sqrt_next = sqrt_target
        elif zero_for_one:
            sqrt_next = _next_from_amount0_up(sqrt_current, liquidity, remaining_less_fee, True)
        else:
            sqrt_next = _next_from_amount1_down(sqrt_current, liquidity, remaining_less_fee, True)
    else:
        amount_out = (
            amount1_delta(sqrt_target, sqrt_current, liquidity, False)
            if zero_for_one
            else amount0_delta(sqrt_current, sqrt_target, liquidity, False)
        )
        if -amount_remaining >= amount_out:
            sqrt_next = sqrt_target
        elif zero_for_one:
            sqrt_next = _next_from_amount1_down(sqrt_current, liquidity, -amount_remaining, False)
        else:
            sqrt_next = _next_from_amount0_up(sqrt_current, liquidity, -amount_remaining, False)

    reached = sqrt_target == sqrt_next
    if zero_for_one:
        if not (reached and exact_in):
            amount_in = amount0_delta(sqrt_next, sqrt_current, liquidity, True)
        if not (reached and not exact_in):
            amount_out = amount1_delta(sqrt_next, sqrt_current, liquidity, False)
    else:
        if not (reached and exact_in):
            amount_in = amount1_delta(sqrt_current, sqrt_next, liquidity, True)
        if not (reached and not exact_in):
            amount_out = amount0_delta(sqrt_current, sqrt_next, liquidity, False)
    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining
    if exact_in and sqrt_next != sqrt_target:
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = _mul_div_up(amount_in, fee_pips, FEE_DENOMINATOR - fee_pips)
    return sqrt_next, amount_in, amount_out, fee_amount


def swap_exact_input(
    state: V3State, amount_in: int, zero_for_one: bool, fee_pips: int
) -> tuple[V3State, int]:
    """Exact-input swap that stays inside the current range: (new state, amount out).

    The contract's price limit is the extreme ratio, so the single step consumes the whole
    input (the caller is responsible for knowing that no range boundary is crossed).
    """
    if amount_in <= 0 or state.liquidity <= 0:
        raise ValueError("amount and liquidity must be positive")
    target = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    sqrt_next, _, amount_out, _ = compute_swap_step(
        state.sqrt_price_x96, target, state.liquidity, amount_in, fee_pips
    )
    return V3State(sqrt_next, state.liquidity), amount_out


def swap_exact_output(
    state: V3State, amount_out: int, zero_for_one: bool, fee_pips: int
) -> tuple[V3State, int]:
    """Exact-output swap inside the current range: (new state, amount in incl. fee)."""
    if amount_out <= 0 or state.liquidity <= 0:
        raise ValueError("amount and liquidity must be positive")
    target = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    sqrt_next, amount_in, _, fee = compute_swap_step(
        state.sqrt_price_x96, target, state.liquidity, -amount_out, fee_pips
    )
    return V3State(sqrt_next, state.liquidity), amount_in + fee


def price_token1_per_token0(state: V3State) -> float:
    """Mid price in raw units of token1 per raw unit of token0 (for valuation only)."""
    return (state.sqrt_price_x96 / Q96) ** 2
