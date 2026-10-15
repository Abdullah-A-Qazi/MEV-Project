"""Uniswap V2 constant-product maths, in exact integer arithmetic.

These functions mirror UniswapV2Library.getAmountOut / getAmountIn and the reserve update
in UniswapV2Pair.swap, so that a swap's on-chain output can be reproduced to the wei. All
amounts are raw token units (no decimals) and plain Python ints, which never overflow.

Reference: https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol
"""

from dataclasses import dataclass

# Uniswap V2 charges a 0.3% fee: 997/1000 of the input is used for pricing.
# SushiSwap uses the same fee. Other forks may differ, so the fee is a parameter.
FEE_NUMERATOR = 997
FEE_DENOMINATOR = 1000


class InsufficientLiquidity(ValueError):
    """Raised when a pool cannot fill the requested trade (mirrors the contract's revert)."""


def get_amount_out(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    fee_numerator: int = FEE_NUMERATOR,
    fee_denominator: int = FEE_DENOMINATOR,
) -> int:
    """Maximum output for an exact input, as computed by UniswapV2Library.getAmountOut."""
    if amount_in <= 0:
        raise ValueError("amount_in must be positive")
    if reserve_in <= 0 or reserve_out <= 0:
        raise InsufficientLiquidity("reserves must be positive")
    amount_in_with_fee = amount_in * fee_numerator
    numerator = amount_in_with_fee * reserve_out
    denominator = reserve_in * fee_denominator + amount_in_with_fee
    return numerator // denominator


def get_amount_in(
    amount_out: int,
    reserve_in: int,
    reserve_out: int,
    fee_numerator: int = FEE_NUMERATOR,
    fee_denominator: int = FEE_DENOMINATOR,
) -> int:
    """Minimum input for an exact output, as computed by UniswapV2Library.getAmountIn."""
    if amount_out <= 0:
        raise ValueError("amount_out must be positive")
    if reserve_in <= 0 or reserve_out <= 0 or amount_out >= reserve_out:
        raise InsufficientLiquidity("pool cannot provide that much output")
    numerator = reserve_in * amount_out * fee_denominator
    denominator = (reserve_out - amount_out) * fee_numerator
    return numerator // denominator + 1


@dataclass(frozen=True)
class Reserves:
    reserve0: int
    reserve1: int


def reserves_before_swap(
    reserves_after: Reserves,
    amount0_in: int,
    amount1_in: int,
    amount0_out: int,
    amount1_out: int,
) -> Reserves:
    """Pool reserves immediately before a swap, rebuilt from the swap's own events.

    In UniswapV2Pair.swap the contract emits Sync(reserve0, reserve1) with the new
    balances, then Swap(amount0In, amount1In, amount0Out, amount1Out). amountIn is defined
    as balance - (reserve - amountOut), so reserve_before = reserve_after - in + out holds
    exactly. The only exception is a pair whose balance fell below reserve - amountOut
    (amountIn is then clamped to 0), which needs a token whose balance shrinks on its own,
    e.g. a negatively rebasing token. tests/test_uniswap_v2_golden.py checks the identity
    against the previous Sync event on real swaps.
    """
    return Reserves(
        reserve0=reserves_after.reserve0 - amount0_in + amount0_out,
        reserve1=reserves_after.reserve1 - amount1_in + amount1_out,
    )


def satisfies_k_invariant(
    before: Reserves,
    after: Reserves,
    amount0_in: int,
    amount1_in: int,
    fee_numerator: int = FEE_NUMERATOR,
    fee_denominator: int = FEE_DENOMINATOR,
) -> bool:
    """The fee-adjusted constant-product check from UniswapV2Pair.swap ("UniswapV2: K")."""
    fee = fee_denominator - fee_numerator
    balance0_adjusted = after.reserve0 * fee_denominator - amount0_in * fee
    balance1_adjusted = after.reserve1 * fee_denominator - amount1_in * fee
    return balance0_adjusted * balance1_adjusted >= (
        before.reserve0 * before.reserve1 * fee_denominator**2
    )


def apply_swap(reserves: Reserves, amount_in: int, zero_for_one: bool) -> tuple[Reserves, int]:
    """Simulate an exact-input swap on given reserves; return (new reserves, amount out).

    zero_for_one=True means token0 in, token1 out.
    """
    if zero_for_one:
        out = get_amount_out(amount_in, reserves.reserve0, reserves.reserve1)
        return Reserves(reserves.reserve0 + amount_in, reserves.reserve1 - out), out
    out = get_amount_out(amount_in, reserves.reserve1, reserves.reserve0)
    return Reserves(reserves.reserve0 - out, reserves.reserve1 + amount_in), out
