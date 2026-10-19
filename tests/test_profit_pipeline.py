"""Bundle grouping and the glue in `python -m profit`, on hand-built result tables."""

import pandas as pd
import pytest

from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V3_SWAP_TOPIC, WETH
from profit.__main__ import build_events, detector_gross_eth, price_flows, undetected_swap_counts
from profit.bundles import Event, group_into_bundles
from profit.pricing import EthPricer
from profit.realised import RealisedProfit

A, B, C, D = ("0x" + c * 64 for c in "abcd")
TOKEN = "0x" + "77" * 20


def test_events_sharing_a_transaction_form_one_bundle():
    events = [
        Event("sandwich", "s1", 10, frozenset({A, B})),  # front A, back B
        Event("sandwich", "s2", 10, frozenset({A, C})),  # same batched front-run A
        Event("arbitrage", "a1", 10, frozenset({C})),  # cycle inside back-run C
        Event("arbitrage", "a2", 10, frozenset({D})),  # unrelated
    ]
    bundles = group_into_bundles(events)
    assert [b.tx_hashes for b in bundles] == [(A, B, C), (D,)]
    assert bundles[0].count("sandwich") == 2 and bundles[0].count("arbitrage") == 1
    assert bundles[0].bundle_id == A


def test_events_sharing_a_transaction_across_blocks_is_an_error():
    with pytest.raises(ValueError):
        group_into_bundles([Event("arbitrage", "x", 1, frozenset({A})),
                            Event("arbitrage", "y", 2, frozenset({A}))])  # fmt: skip


def sandwich_rows():
    # One sandwich with two victims -> two rows that must become one event.
    base = {"block_number": 10, "pool": "0xp", "front_tx": A, "back_tx": B, "token_x": WETH,
            "attacker_gross_profit_x": "500"}  # fmt: skip
    return pd.DataFrame([{**base, "victim_tx": C}, {**base, "victim_tx": D}])


def arbitrage_rows(overlaps: bool):
    return pd.DataFrame([{"block_number": 10, "tx_hash": B, "start_token": WETH,
                          "gross_profit": "300", "overlaps_sandwich": overlaps}])  # fmt: skip


def test_build_events_dedupes_victim_rows():
    events = build_events(arbitrage_rows(False), sandwich_rows())
    assert sorted(e.kind for e in events) == ["arbitrage", "sandwich"]


@pytest.mark.parametrize(("overlaps", "expected"), [(False, 800), (True, 500)])
def test_detector_gross_skips_arbitrages_that_reuse_a_sandwich_leg(overlaps, expected):
    arbs, sands = arbitrage_rows(overlaps), sandwich_rows()
    [bundle] = group_into_bundles(build_events(arbs, sands))
    assert detector_gross_eth(bundle, arbs, sands, EthPricer()) == expected


def test_price_flows_marks_unpriced_tokens():
    result = RealisedProfit(eth_flow=10, token_flows={TOKEN: 5}, gas_fee=3, base_fee_burnt=1,
                            priority_fee=2, coinbase_transfers=0, traced=False)  # fmt: skip
    priced = price_flows(result, EthPricer(), block=10)
    assert priced.unpriced_tokens == (TOKEN,)
    assert priced.realised is None and priced.revenue is None
    assert priced.realised_eth_only == 7
    no_tokens = price_flows(RealisedProfit(10, {}, 3, 1, 2, 0, False), EthPricer(), block=10)
    assert no_tokens.realised == 7 and no_tokens.revenue == 10


def test_undetected_swaps_are_counted_per_transaction():
    logs = pd.DataFrame([
        {"tx_hash": A, "log_index": 1, "topic0": UNISWAP_V2_SWAP_TOPIC},  # detected leg
        {"tx_hash": A, "log_index": 4, "topic0": UNISWAP_V3_SWAP_TOPIC},  # V3: not decoded
        {"tx_hash": B, "log_index": 2, "topic0": UNISWAP_V2_SWAP_TOPIC},  # V2, not a leg
        {"tx_hash": B, "log_index": 3, "topic0": "0xddf2"},  # a Transfer: ignored
    ])  # fmt: skip
    assert undetected_swap_counts(logs, {(A, 1)}) == {A: 1, B: 1}
    assert undetected_swap_counts(logs, {(A, 1), (A, 4), (B, 2)}) == {}
