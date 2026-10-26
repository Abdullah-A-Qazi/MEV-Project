"""Sandwich detector on hand-built positive and negative cases."""

from detectors.sandwich import detect_sandwiches
from mitigation.victim_loss import victim_losses
from tests.sim import USDC, WETH, Pool, addr

BOT_EOA, BOT_CONTRACT = addr(0xB07), addr(0xC0DE)
VICTIM, OTHER_USER = addr(0xA11CE), addr(0xB0B)
E18 = 10**18


def fresh_pool() -> Pool:
    # 2,000 WETH / 5,000,000 USDC (USDC as an 18-decimal stand-in; units do not matter).
    return Pool(addr(0x9001), WETH, USDC, 2_000 * E18, 5_000_000 * E18)


def classic_sandwich(pool: Pool):
    """Bot buys WETH, victim buys WETH via the router, bot sells the WETH back."""
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(3, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    return front, victim, back


def test_detects_classic_sandwich_and_victim():
    pool = fresh_pool()
    front, victim, back = classic_sandwich(pool)
    [s] = detect_sandwiches([front, victim, back])
    assert s.front == front and s.back == back
    assert s.victims == (victim,)
    assert s.attacker == BOT_CONTRACT
    assert s.token_x == USDC and s.token_y == WETH
    assert s.gross_profit_x > 0
    assert s.residual_y == 0


def test_victim_loss_matches_counterfactual():
    pool = fresh_pool()
    untouched = fresh_pool()
    front, victim, back = classic_sandwich(pool)
    # What the victim would have got with no front-run:
    expected = untouched.swap(2, 50_000 * E18, USDC, VICTIM).amount_out
    [s] = detect_sandwiches([front, victim, back])
    [loss] = victim_losses(s, [front, victim, back])
    assert loss.counterfactual_out == expected
    assert loss.actual_out == victim.amount_out
    assert loss.loss > 0 and 0 < loss.loss_fraction < 1


def test_bot_rotating_eoas_but_same_contract_is_detected():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, addr(0x1), BOT_CONTRACT)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(3, front.amount_out, WETH, addr(0x2), BOT_CONTRACT)
    assert len(detect_sandwiches([front, victim, back])) == 1


def test_multiple_victims_are_all_reported():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    v1 = pool.swap(2, 20_000 * E18, USDC, VICTIM)
    v2 = pool.swap(3, 30_000 * E18, USDC, OTHER_USER)
    back = pool.swap(4, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    [s] = detect_sandwiches([front, v1, v2, back])
    assert s.victims == (v1, v2)
    losses = victim_losses(s, [front, v1, v2, back])
    assert [x.victim_tx_hash for x in losses] == [v1.tx_hash, v2.tx_hash]
    assert all(x.loss > 0 for x in losses)


# --- Negative cases -----------------------------------------------------------------------


def test_no_victim_in_between_is_not_a_sandwich():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    back = pool.swap(2, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    assert detect_sandwiches([front, back]) == []


def test_middle_swap_in_opposite_direction_is_not_a_victim():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    seller = pool.swap(2, 10 * E18, WETH, OTHER_USER)  # sells WETH: helped, not harmed
    back = pool.swap(3, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    assert detect_sandwiches([front, seller, back]) == []


def test_two_unrelated_router_users_are_not_an_attacker():
    """Opposite trades by different EOAs via the same public router must not match."""
    pool = fresh_pool()
    buyer = pool.swap(1, 100_000 * E18, USDC, addr(0x10))
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    seller = pool.swap(3, buyer.amount_out, WETH, addr(0x20))
    assert detect_sandwiches([buyer, victim, seller]) == []


def test_back_run_amount_far_from_front_run_is_rejected():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(3, front.amount_out // 10, WETH, BOT_EOA, BOT_CONTRACT)
    assert detect_sandwiches([front, victim, back], tolerance=0.2) == []
    assert len(detect_sandwiches([front, victim, back], tolerance=0.95)) == 1


def test_different_pools_are_not_combined():
    pool_a, pool_b = fresh_pool(), Pool(addr(0x9002), WETH, USDC, 2_000 * E18, 5_000_000 * E18)
    front = pool_a.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    victim = pool_a.swap(2, 50_000 * E18, USDC, VICTIM)
    back = pool_b.swap(3, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    assert detect_sandwiches([front, victim, back]) == []


def test_different_blocks_are_not_combined():
    pool = fresh_pool()
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT, block=100)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM, block=100)
    back = pool.swap(0, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT, block=101)
    assert detect_sandwiches([front, victim, back]) == []


def test_round_trip_inside_one_transaction_is_not_a_sandwich():
    pool = fresh_pool()
    tx = "0x" + "ab" * 32
    front = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT, tx_hash=tx)
    back = pool.swap(1, front.amount_out, WETH, BOT_EOA, BOT_CONTRACT, tx_hash=tx)
    assert detect_sandwiches([front, back]) == []


def test_each_back_run_is_used_once():
    """Two front-runs by the same bot cannot share one back-run."""
    pool = fresh_pool()
    f1 = pool.swap(1, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    f2 = pool.swap(2, 100_000 * E18, USDC, BOT_EOA, BOT_CONTRACT)
    victim = pool.swap(3, 50_000 * E18, USDC, VICTIM)
    back = pool.swap(4, f2.amount_out, WETH, BOT_EOA, BOT_CONTRACT)
    found = detect_sandwiches([f1, f2, victim, back])
    assert len(found) == 1


def test_shared_router_is_not_an_attacker_identity():
    """Two strangers trading through the same trading-bot router are not one attacker."""
    shared = addr(0xBA7A7A)
    pool = fresh_pool()
    buyer = pool.swap(1, 100_000 * E18, USDC, addr(0x10), shared)
    victim = pool.swap(2, 50_000 * E18, USDC, VICTIM)
    seller = pool.swap(3, buyer.amount_out, WETH, addr(0x20), shared)
    swaps = [buyer, victim, seller]
    assert len(detect_sandwiches(swaps)) == 1  # unknown contract: looks like one bot
    assert detect_sandwiches(swaps, routers=frozenset({shared})) == []
