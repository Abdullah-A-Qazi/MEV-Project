"""RQ1 analytical estimate: exact when nothing else touched the pools, off when something did."""

from detectors.arbitrage import detect_arbitrages
from detectors.sandwich import detect_sandwiches
from profit.analytical import (
    estimate_arbitrage,
    estimate_sandwich,
    formula_arbitrage,
    formula_sandwich,
    pre_block_reserves,
)
from tests.sim import USDC, WETH, Pool, addr

E18 = 10**18
BOT, BOT_CONTRACT, VICTIM, OTHER = addr(0xB07), addr(0xC0DE), addr(0xA11CE), addr(0x0B)
TX = "0x" + "aa" * 32


def fresh_pool(n: int = 0x9001) -> Pool:
    return Pool(addr(n), WETH, USDC, 2_000 * E18, 5_000_000 * E18)


def sandwich(pool, first_tx=1, earlier=()):
    front = pool.swap(first_tx, 100_000 * E18, USDC, BOT, BOT_CONTRACT)
    victim = pool.swap(first_tx + 1, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(first_tx + 2, front.amount_out, WETH, BOT, BOT_CONTRACT)
    swaps = [*earlier, front, victim, back]
    [s] = detect_sandwiches(swaps)
    return s, swaps


def test_sandwich_estimate_is_exact_when_front_run_comes_first():
    s, swaps = sandwich(fresh_pool())
    pre = pre_block_reserves(swaps)
    assert estimate_sandwich(s.front, list(s.victims), s.back, pre) == s.gross_profit_x
    assert formula_sandwich(s.front, s.back) == s.gross_profit_x


def test_earlier_trade_in_the_block_makes_the_estimate_differ():
    pool = fresh_pool()
    earlier = pool.swap(0, 200_000 * E18, USDC, OTHER)  # someone buys WETH first
    s, swaps = sandwich(pool, first_tx=1, earlier=[earlier])
    estimate = estimate_sandwich(s.front, list(s.victims), s.back, pre_block_reserves(swaps))
    # The formula on the state the trades really met still matches: the gap is pure drift.
    assert formula_sandwich(s.front, s.back) == s.gross_profit_x
    assert estimate is not None and estimate != s.gross_profit_x


def test_arbitrage_estimate_is_exact_on_untouched_pools():
    a = Pool(addr(0xA), WETH, USDC, 1_000 * E18, 2_500_000 * E18)
    b = Pool(addr(0xB), WETH, USDC, 1_000 * E18, 2_600_000 * E18)
    s1 = a.swap(5, 10_000 * E18, USDC, BOT, BOT_CONTRACT, tx_hash=TX, log_index=10)
    s2 = b.swap(5, s1.amount_out, WETH, BOT, BOT_CONTRACT, tx_hash=TX, log_index=11)
    [arb] = detect_arbitrages([s1, s2])
    assert estimate_arbitrage([s1, s2], pre_block_reserves([s1, s2])) == arb.gross_profit
    assert formula_arbitrage([s1, s2]) == arb.gross_profit


def test_unknown_pool_gives_no_estimate():
    s, _ = sandwich(fresh_pool())
    assert estimate_sandwich(s.front, list(s.victims), s.back, {}) is None


def test_shortfall_when_the_bot_takes_less_than_the_formula():
    """A front-run that asks for less output leaves the pool richer; the formula on the
    real state then differs from the actual gross, and that gap is shortfall, not drift."""
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT, BOT_CONTRACT, take=0.99)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(3, front.amount_out, WETH, BOT, BOT_CONTRACT, take=0.99)
    swaps = [front, victim, back]
    [s] = detect_sandwiches(swaps)
    estimate = estimate_sandwich(s.front, list(s.victims), s.back, pre_block_reserves(swaps))
    formula = formula_sandwich(s.front, s.back)
    assert estimate == formula  # nothing else touched the pool: zero drift
    assert formula > s.gross_profit_x  # the back-run's 1% haircut is the shortfall
