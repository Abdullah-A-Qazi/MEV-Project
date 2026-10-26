"""Golden tests: our V3 single-range maths reproduces real V3 swaps to the wei.

Each real swap must match one of the three ways a V3 swap can execute: exact input, exact
output, or stopped at a price limit.

Cases: tests/fixtures/uniswap_v3_golden.json (validation/build_v3_golden.py). Each swap's
starting state is the state the previous swap in the same pool and block left behind.
"""

import json
from pathlib import Path

import pytest

from profit.uniswap_v3 import V3State, compute_swap_step, swap_exact_input, swap_exact_output

FIXTURE = Path(__file__).parent / "fixtures" / "uniswap_v3_golden.json"
CASES = json.loads(FIXTURE.read_text())["cases"] if FIXTURE.exists() else []


def _ids(case):
    return f"{case['block_number']}-{case['log_index']}"


def test_fixture_is_substantial():
    assert len(CASES) >= 30
    assert len({c["fee"] for c in CASES}) >= 2  # several fee tiers


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_v3_swap_reproduced_exactly(case):
    state = V3State(int(case["sqrt_price_before"]), int(case["liquidity"]))
    a0, a1 = int(case["amount0"]), int(case["amount1"])
    zero_for_one = a0 > 0
    amount_in, amount_out = (a0, -a1) if zero_for_one else (a1, -a0)
    after = int(case["sqrt_price_after"])

    ex_in_state, ex_in_out = swap_exact_input(state, amount_in, zero_for_one, case["fee"])
    ex_out_state, ex_out_in = swap_exact_output(state, amount_out, zero_for_one, case["fee"])
    exact_input_matches = ex_in_state.sqrt_price_x96 == after and ex_in_out == amount_out
    exact_output_matches = ex_out_state.sqrt_price_x96 == after and ex_out_in == amount_in
    # A trader may also cap the price (sqrtPriceLimitX96): the swap then stops exactly at
    # the limit, which is the recorded price after, and uses only part of its input.
    nxt, used_in, out, fee = compute_swap_step(
        state.sqrt_price_x96, after, state.liquidity, amount_in + 10**30, case["fee"]
    )
    price_limited = nxt == after and used_in + fee == amount_in and out == amount_out
    assert exact_input_matches or exact_output_matches or price_limited
