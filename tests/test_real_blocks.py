"""Regression test on real mainnet blocks 17,200,000-17,200,002 (April 2023).

These blocks contain sandwiches by well-known bots (jaredfromsubway.eth and others). The
expected output was recorded from the detector when the fixture was built (see
validation/build_real_fixture.py); hand-checked cases are listed in the fixture's
`hand_checked` field.
"""

import json
from pathlib import Path

from collector.pools import PoolInfo
from detectors.arbitrage import detect_arbitrages
from detectors.sandwich import detect_sandwiches
from detectors.swaps import decode_v2_swaps
from profit.uniswap_v2 import get_amount_out

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "real_blocks_17200000_17200002.json").read_text()
)
JARED = "0x6b75d8af000000e20b7a7ddf000ba900b4009a80"


def decoded_swaps():
    pools = {p["address"]: PoolInfo(**p) for p in FIXTURE["pools"]}
    return decode_v2_swaps(FIXTURE["logs"], FIXTURE["txs"], pools, ["uniswap_v2", "sushiswap"])


def test_every_real_swap_has_reserves_and_is_priced_by_the_v2_formula():
    swaps = decoded_swaps()
    assert len(swaps) > 50
    for s in swaps:
        assert s.reserves_after is not None
        if not s.is_simple:
            continue
        before = s.reserves_before
        r_in, r_out = (
            (before.reserve0, before.reserve1)
            if s.zero_for_one
            else (before.reserve1, before.reserve0)
        )
        # A pair never pays out more than the formula allows (it may pay less, e.g. when a
        # bot requests less than the maximum or a token charges a transfer fee).
        assert s.amount_out <= get_amount_out(s.amount_in, r_in, r_out)


def test_detects_the_recorded_sandwiches():
    found = {
        (s.front.tx_hash, s.back.tx_hash): (s.attacker, sorted(v.tx_hash for v in s.victims))
        for s in detect_sandwiches(decoded_swaps())
    }
    expected = {
        (e["front_tx"], e["back_tx"]): (e["attacker"], e["victim_tx"])
        for e in FIXTURE["expected_sandwiches"]
    }
    assert found == expected
    assert JARED in {attacker for attacker, _ in found.values()}


def test_batched_sandwich_legs_are_not_mistaken_for_arbitrage():
    """jaredfromsubway batches several pools' front-runs in one tx, WETH in and WETH out.
    Those legs do not chain into a cycle, so they must not be reported as arbitrage."""
    assert detect_arbitrages(decoded_swaps()) == []
