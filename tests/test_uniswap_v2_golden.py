"""Golden tests: our Uniswap V2 maths must reproduce real on-chain swaps to the wei.

Cases come from tests/fixtures/uniswap_v2_golden.json, built by validation/build_v2_golden.py
from mainnet swaps made through Uniswap V2 Router02 / the SushiSwap router (single hop).
"""

import json
from pathlib import Path

import pytest

from profit.uniswap_v2 import (
    Reserves,
    get_amount_in,
    get_amount_out,
    reserves_before_swap,
    satisfies_k_invariant,
)

FIXTURE = Path(__file__).parent / "fixtures" / "uniswap_v2_golden.json"
DATA = json.loads(FIXTURE.read_text())
CASES = [
    {k: (int(v) if k.startswith(("reserve", "amount")) else v) for k, v in c.items()}
    for c in DATA["cases"]
]


def _ids(case):
    return f"{case['dex']}-{case['block_number']}-{case['log_index']}"


def _direction(case):
    """(amount_in, amount_out, reserve_in, reserve_out) in the swap's direction."""
    if case["amount0_in"] > 0:
        return (
            case["amount0_in"],
            case["amount1_out"],
            case["reserve0_before"],
            case["reserve1_before"],
        )
    return (
        case["amount1_in"],
        case["amount0_out"],
        case["reserve1_before"],
        case["reserve0_before"],
    )


def test_fixture_is_substantial():
    kinds = {c["kind"] for c in CASES}
    dexes = {c["dex"] for c in CASES}
    assert len(CASES) >= 30
    assert kinds == {"exact_input", "exact_output"}
    assert dexes == {"uniswap_v2", "sushiswap"}


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "exact_input"], ids=_ids)
def test_exact_input_output_matches_chain(case):
    amount_in, amount_out, reserve_in, reserve_out = _direction(case)
    assert get_amount_out(amount_in, reserve_in, reserve_out) == amount_out


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "exact_output"], ids=_ids)
def test_exact_output_input_matches_chain(case):
    amount_in, amount_out, reserve_in, reserve_out = _direction(case)
    assert get_amount_in(amount_out, reserve_in, reserve_out) == amount_in


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_reserves_before_rebuilt_from_own_sync(case):
    """Pre-swap reserves from (Sync after, Swap amounts) equal the previous Sync event."""
    rebuilt = reserves_before_swap(
        Reserves(case["reserve0_after"], case["reserve1_after"]),
        case["amount0_in"],
        case["amount1_in"],
        case["amount0_out"],
        case["amount1_out"],
    )
    assert rebuilt == Reserves(case["reserve0_before"], case["reserve1_before"])


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_k_invariant_holds(case):
    assert satisfies_k_invariant(
        Reserves(case["reserve0_before"], case["reserve1_before"]),
        Reserves(case["reserve0_after"], case["reserve1_after"]),
        case["amount0_in"],
        case["amount1_in"],
    )
