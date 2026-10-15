"""Arbitrage detector on hand-built positive and negative cases."""

from detectors.arbitrage import detect_arbitrages
from tests.sim import DAI, USDC, WETH, Pool, addr

E18 = 10**18
BOT, BOT_CONTRACT, USER = addr(0xB07), addr(0xC0DE), addr(0xA11CE)
TX = "0x" + "aa" * 32


def mispriced_pools():
    """Pool A prices WETH at 2,500 USDC, pool B at 2,600: buy in A, sell in B."""
    a = Pool(addr(0xA), WETH, USDC, 1_000 * E18, 2_500_000 * E18)
    b = Pool(addr(0xB), WETH, USDC, 1_000 * E18, 2_600_000 * E18)
    return a, b


def test_two_pool_cycle_is_arbitrage():
    a, b = mispriced_pools()
    s1 = a.swap(5, 10_000 * E18, USDC, BOT, BOT_CONTRACT, tx_hash=TX, log_index=10)
    s2 = b.swap(5, s1.amount_out, WETH, BOT, BOT_CONTRACT, tx_hash=TX, log_index=11)
    [arb] = detect_arbitrages([s1, s2])
    assert arb.start_token == USDC
    assert arb.pools == (a.address, b.address)
    assert arb.gross_profit == s2.amount_out - s1.amount_in > 0


def test_three_pool_triangle_is_arbitrage():
    a = Pool(addr(0xA), WETH, USDC, 1_000 * E18, 2_500_000 * E18)
    b = Pool(addr(0xB), USDC, DAI, 5_200_000 * E18, 5_000_000 * E18)  # DAI dear vs USDC
    c = Pool(addr(0xC), WETH, DAI, 1_000 * E18, 2_500_000 * E18)
    s1 = c.swap(1, 1 * E18, WETH, BOT, BOT_CONTRACT, tx_hash=TX, log_index=0)  # WETH->DAI
    s2 = b.swap(1, s1.amount_out, DAI, BOT, BOT_CONTRACT, tx_hash=TX, log_index=1)  # DAI->USDC
    s3 = a.swap(1, s2.amount_out, USDC, BOT, BOT_CONTRACT, tx_hash=TX, log_index=2)  # USDC->WETH
    found = detect_arbitrages([s3, s1, s2])  # input order must not matter
    assert len(found) == 1
    assert found[0].start_token == WETH and len(found[0].pools) == 3


def test_unprofitable_cycle_is_not_arbitrage():
    a, b = mispriced_pools()
    # Wrong way round: buy WETH in the expensive pool, sell in the cheap one.
    s1 = b.swap(5, 10_000 * E18, USDC, USER, tx_hash=TX, log_index=0)
    s2 = a.swap(5, s1.amount_out, WETH, USER, tx_hash=TX, log_index=1)
    assert detect_arbitrages([s1, s2]) == []


def test_ordinary_two_hop_swap_is_not_arbitrage():
    """USDC -> WETH -> DAI does not return to the start token."""
    a = Pool(addr(0xA), WETH, USDC, 1_000 * E18, 2_500_000 * E18)
    c = Pool(addr(0xC), WETH, DAI, 1_000 * E18, 2_500_000 * E18)
    s1 = a.swap(1, 1_000 * E18, USDC, USER, tx_hash=TX, log_index=0)
    s2 = c.swap(1, s1.amount_out, WETH, USER, tx_hash=TX, log_index=1)
    assert detect_arbitrages([s1, s2]) == []


def test_buy_and_sell_in_same_pool_is_not_arbitrage():
    a, _ = mispriced_pools()
    s1 = a.swap(1, 10_000 * E18, USDC, USER, tx_hash=TX, log_index=0)
    s2 = a.swap(1, s1.amount_out, WETH, USER, tx_hash=TX, log_index=1)
    assert detect_arbitrages([s1, s2]) == []


def test_swaps_in_different_transactions_are_not_combined():
    a, b = mispriced_pools()
    s1 = a.swap(1, 10_000 * E18, USDC, BOT, BOT_CONTRACT, tx_hash="0x" + "01" * 32)
    s2 = b.swap(2, s1.amount_out, WETH, BOT, BOT_CONTRACT, tx_hash="0x" + "02" * 32)
    assert detect_arbitrages([s1, s2]) == []


def test_two_cycles_in_one_transaction():
    a, b = mispriced_pools()
    s1 = a.swap(1, 5_000 * E18, USDC, BOT, BOT_CONTRACT, tx_hash=TX, log_index=0)
    s2 = b.swap(1, s1.amount_out, WETH, BOT, BOT_CONTRACT, tx_hash=TX, log_index=1)
    s3 = a.swap(1, 5_000 * E18, USDC, BOT, BOT_CONTRACT, tx_hash=TX, log_index=2)
    s4 = b.swap(1, s3.amount_out, WETH, BOT, BOT_CONTRACT, tx_hash=TX, log_index=3)
    assert len(detect_arbitrages([s1, s2, s3, s4])) == 2
