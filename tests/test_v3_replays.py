"""V3 through profit.market: estimates and victim loss are exact when nothing interferes."""

from detectors.sandwich import detect_sandwiches
from mitigation.victim_loss import victim_losses
from profit.analytical import (
    estimate_sandwich,
    market_estimate_sandwich,
    market_formula_sandwich,
    pre_block_reserves,
)
from profit.market import Market
from profit.uniswap_v3 import V3State
from tests.sim import USDC, WETH, Pool, V3Pool, addr

E18 = 10**18
BOT, BOT_CONTRACT, VICTIM = addr(0xB07), addr(0xC0DE), addr(0xA11CE)
SQRT_P = 79228162514264337593543950336 * 50  # price 2,500 token1 per token0
L = 10**22


class PreBlock(Market):
    """Market whose V3 pre-block state is given directly (no RPC)."""

    def __init__(self, swaps, fees, start):
        super().__init__(swaps, fees)
        self.v3.pre_block = lambda block, pool: start


def v3_sandwich():
    pool = V3Pool(addr(0x3003), WETH, USDC, SQRT_P, L)
    start = pool.state
    front = pool.swap(1, 10 * E18, WETH, BOT, BOT_CONTRACT)
    victim = pool.swap(2, 5 * E18, WETH, VICTIM)
    back = pool.swap(3, front.amount_out, USDC, BOT, BOT_CONTRACT)
    swaps = [front, victim, back]
    [s] = detect_sandwiches(swaps)
    return s, swaps, PreBlock(swaps, {pool.address: pool.fee}, start), start, pool


def test_v3_sandwich_estimates_are_exact_without_interference():
    s, _, market, _, _ = v3_sandwich()
    assert market_estimate_sandwich(market, s.front, list(s.victims), s.back) == s.gross_profit_x
    assert market_formula_sandwich(market, s.front, s.back) == s.gross_profit_x


def test_v3_victim_loss_matches_counterfactual():
    s, swaps, market, start, pool = v3_sandwich()
    untouched = V3Pool(addr(0x3004), WETH, USDC, start.sqrt_price_x96, start.liquidity, pool.fee)
    expected = untouched.swap(2, 5 * E18, WETH, VICTIM).amount_out
    [loss] = victim_losses(s, swaps, market)
    assert loss.counterfactual_out == expected and loss.loss > 0


def test_v3_replay_refused_when_liquidity_changed():
    s, swaps, market, start, _ = v3_sandwich()
    other = PreBlock(swaps, {s.pool: 3000}, V3State(start.sqrt_price_x96, start.liquidity + 1))
    assert market_estimate_sandwich(other, s.front, list(s.victims), s.back) is None
    assert victim_losses(s, swaps, other) == []


def test_market_matches_v2_functions():
    pool = Pool(addr(0x9001), WETH, USDC, 2_000 * E18, 5_000_000 * E18)
    front = pool.swap(1, 100_000 * E18, USDC, BOT, BOT_CONTRACT)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(3, front.amount_out, WETH, BOT, BOT_CONTRACT)
    swaps = [front, victim, back]
    market = Market(swaps, {})
    old = estimate_sandwich(front, [victim], back, pre_block_reserves(swaps))
    assert market_estimate_sandwich(market, front, [victim], back) == old


def test_pre_swap_state_is_worked_back_from_the_event():
    from profit.v3_state import infer_pre_swap_state

    for token_in in (WETH, USDC):
        pool = V3Pool(addr(0x3005), WETH, USDC, SQRT_P, L)
        start = pool.state
        swap = pool.swap(1, 3 * E18 if token_in == WETH else 7_000 * E18, token_in, VICTIM)
        inferred = infer_pre_swap_state(swap, pool.fee)
        assert inferred is not None and inferred.liquidity == start.liquidity
        # Contract rounding leaves a tiny error: relative 1e-20 or better.
        assert abs(inferred.sqrt_price_x96 - start.sqrt_price_x96) <= start.sqrt_price_x96 * 1e-20


def test_inconsistent_amounts_mean_a_range_was_crossed():
    from dataclasses import replace

    from profit.v3_state import infer_pre_swap_state

    pool = V3Pool(addr(0x3006), WETH, USDC, SQRT_P, L)
    swap = pool.swap(1, 3 * E18, WETH, VICTIM)
    crossed = replace(swap, amount0_in=swap.amount0_in * 2)  # input too large for that move
    assert infer_pre_swap_state(crossed, pool.fee) is None
