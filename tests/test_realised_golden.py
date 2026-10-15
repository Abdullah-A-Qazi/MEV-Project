"""Golden tests: realised-profit accounting equals real balance changes of searchers.

For each (block, searcher) case in tests/fixtures/realised_golden_*.json, built by
validation/build_realised_golden.py from mainnet blocks in April 2023 and January 2024:

  * ETH + WETH: eth_flow - gas_fee over every tx touching the searcher must equal the change
    in the searcher's ETH + WETH balances across the block, to the wei (with traces).
  * Other tokens: each token's net flow must equal the change in the searcher's balance.
  * Without traces, the only possible error is internal ETH (e.g. coinbase transfers), so the
    untraced result must overstate profit by exactly the traced coinbase payments.
"""

import json
from pathlib import Path

import pytest

from profit.realised import TxData, account

FIXTURES = sorted((Path(__file__).parent / "fixtures").glob("realised_golden_*.json"))
CASES = [case for path in FIXTURES for case in json.loads(path.read_text())["cases"]]


def _ids(case):
    return f"{case['block_number']}-{case['searcher_contract'][:10]}"


def _run(case, traced: bool):
    txs = [
        TxData(
            tx_hash=t["tx_hash"],
            tx_from=t["tx_from"],
            tx_to=t["tx_to"],
            value=int(t["value"]),
            gas_used=t["gas_used"],
            effective_gas_price=int(t["effective_gas_price"]),
            logs=tuple(t["logs"]),
            status=t["status"],
        )
        for t in case["txs"]
    ]
    traces = {t["tx_hash"]: t["trace"] for t in case["txs"]} if traced else None
    return account(
        txs, case["actors"], case["fee_recipient"], int(case["base_fee_per_gas"]), traces
    )


def _true_eth_weth_change(case) -> int:
    return sum(
        int(b["eth_after"]) - int(b["eth_before"]) + int(b["weth_after"]) - int(b["weth_before"])
        for b in case["balances"].values()
    )


def test_fixtures_cover_several_searchers_and_a_coinbase_transfer():
    assert len(CASES) >= 15
    assert len({c["searcher_contract"] for c in CASES}) >= 5
    assert any(_run(c, traced=True).coinbase_transfers > 0 for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_traced_eth_accounting_matches_balances(case):
    result = _run(case, traced=True)
    assert result.traced
    assert result.eth_flow - result.gas_fee == _true_eth_weth_change(case)


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_token_flows_match_balances(case):
    result = _run(case, traced=False)
    for token, change in case["token_balance_changes"].items():
        assert result.token_flows.get(token, 0) == int(change), token


@pytest.mark.parametrize("case", CASES, ids=_ids)
def test_untraced_error_is_exactly_the_hidden_coinbase_transfers(case):
    traced, untraced = _run(case, traced=True), _run(case, traced=False)
    assert not untraced.traced
    overstatement = (untraced.eth_flow - untraced.gas_fee) - _true_eth_weth_change(case)
    assert overstatement == traced.coinbase_transfers - untraced.coinbase_transfers
