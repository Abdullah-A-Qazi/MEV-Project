"""Sandwich-profit maths: checked against a brute-force search and basic monotonicity."""

from mitigation.safe_threshold import _profit, max_sandwich_profit, safe_tolerance
from profit.uniswap_v2 import get_amount_out

E18 = 10**18
R_IN, R_OUT = 100 * E18, 300_000 * E18  # 100 WETH / 300,000 tokens
VICTIM = 2 * E18


def test_zero_tolerance_leaves_nothing_to_take():
    assert max_sandwich_profit(VICTIM, R_IN, R_OUT, 0.0) == 0


def test_profit_grows_with_tolerance():
    profits = [max_sandwich_profit(VICTIM, R_IN, R_OUT, s) for s in (0.001, 0.005, 0.02, 0.05)]
    assert profits == sorted(profits) and profits[-1] > 0


def test_matches_brute_force_search():
    tolerance = 0.02
    min_out = int(get_amount_out(VICTIM, R_IN, R_OUT) * (1 - tolerance))
    brute = max(
        p for a in range(0, 30 * E18, E18 // 20)
        if (p := _profit(a, VICTIM, R_IN, R_OUT, min_out)) is not None
    )  # fmt: skip
    found = max_sandwich_profit(VICTIM, R_IN, R_OUT, tolerance)
    assert found >= brute * 0.995  # at least as good as a 0.05-WETH grid, within 0.5%


def test_victim_never_breaks_its_limit_at_the_best_front_run():
    tolerance = 0.01
    min_out = int(get_amount_out(VICTIM, R_IN, R_OUT) * (1 - tolerance))
    assert _profit(10**30, VICTIM, R_IN, R_OUT, min_out) is None  # too big: victim reverts


def test_safe_tolerance_is_the_break_even_point():
    cost = E18 // 100  # 0.01 WETH of gas
    s = safe_tolerance(VICTIM, R_IN, R_OUT, cost)
    assert 0 < s < 0.5
    assert max_sandwich_profit(VICTIM, R_IN, R_OUT, s) <= cost
    assert max_sandwich_profit(VICTIM, R_IN, R_OUT, s * 1.05) > cost


def test_bigger_trades_have_lower_safe_thresholds():
    cost = E18 // 100
    small = safe_tolerance(VICTIM // 4, R_IN, R_OUT, cost)
    large = safe_tolerance(VICTIM * 4, R_IN, R_OUT, cost)
    assert large < small


def test_checker_assessment_is_consistent():
    from mitigation.checker import assess

    cost = E18 // 100
    loose = assess(VICTIM, R_IN, R_OUT, 0.05, cost)
    safe = assess(VICTIM, R_IN, R_OUT, loose["safe_slippage"], cost)
    assert loose["worth_attacking"] and not safe["worth_attacking"]
    assert loose["max_attacker_profit"] > safe["max_attacker_profit"]
    assert abs(loose["size_vs_pool"] - VICTIM / R_IN) < 1e-12


def test_checker_gas_default_follows_the_results(tmp_path):
    import json

    from mitigation.checker import DEFAULT_ATTACK_GAS, default_attack_gas

    assert default_attack_gas(tmp_path) == DEFAULT_ATTACK_GAS  # no results yet
    summary = {"safe_threshold": {"attack_gas_median": 401_234.0}}
    (tmp_path / "rq3_summary.json").write_text(json.dumps(summary))
    assert default_attack_gas(tmp_path) == 401_234
