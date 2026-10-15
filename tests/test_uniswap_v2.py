import pytest

from profit.uniswap_v2 import (
    InsufficientLiquidity,
    Reserves,
    apply_swap,
    get_amount_in,
    get_amount_out,
    satisfies_k_invariant,
)


def test_get_amount_out_known_value():
    # 1 token into a 1000/1000 pool: 997 * 1000 // (1000 * 1000 + 997) = 0 (integer floor)
    assert get_amount_out(1, 1000, 1000) == 0
    # 100 in: 99700 * 1000 // (1_000_000 + 99_700) = 90
    assert get_amount_out(100, 1000, 1000) == 90


def test_get_amount_in_is_minimal_inverse():
    reserve_in, reserve_out = 10**21, 3 * 10**24
    for amount_out in [1, 10**6, 10**18, 10**22]:
        amount_in = get_amount_in(amount_out, reserve_in, reserve_out)
        assert get_amount_out(amount_in, reserve_in, reserve_out) >= amount_out
        if amount_in > 1:
            assert get_amount_out(amount_in - 1, reserve_in, reserve_out) < amount_out


def test_output_never_exceeds_reserve():
    assert get_amount_out(10**40, 10**18, 10**18) < 10**18


@pytest.mark.parametrize("args", [(0, 1, 1), (1, 0, 1), (1, 1, 0)])
def test_get_amount_out_rejects_bad_input(args):
    with pytest.raises(ValueError):
        get_amount_out(*args)


def test_get_amount_in_rejects_draining_the_pool():
    with pytest.raises(InsufficientLiquidity):
        get_amount_in(1000, 1000, 1000)


def test_apply_swap_both_directions_keep_k():
    start = Reserves(5 * 10**20, 10**24)
    after, out = apply_swap(start, 10**18, zero_for_one=True)
    assert after == Reserves(start.reserve0 + 10**18, start.reserve1 - out)
    assert satisfies_k_invariant(start, after, 10**18, 0)

    after2, out2 = apply_swap(start, 10**21, zero_for_one=False)
    assert after2 == Reserves(start.reserve0 - out2, start.reserve1 + 10**21)
    assert satisfies_k_invariant(start, after2, 0, 10**21)


def test_k_invariant_rejects_overpayment():
    start = Reserves(1000, 1000)
    # Taking 100 out for 100 in violates K once the fee is charged.
    assert not satisfies_k_invariant(start, Reserves(1100, 900), 100, 0)
