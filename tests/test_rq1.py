"""RQ1's decomposition adds up exactly, and shares are summarised without a mean of ratios."""

import pandas as pd
import pytest

from analysis.rq1 import PARTS, decompose, describe_share

E18 = 10**18


def bundle(i, estimate, formula, gross, revenue, base, priority, coinbase, excluded=False):
    return {
        "bundle_id": i, "n_sandwiches": 1, "n_arbitrages": 0,
        "undetected_swaps": int(excluded), "unexplained_transfers": 0,
        "analytical_estimate": estimate, "formula_at_execution": formula,
        "detector_gross": gross, "revenue": revenue, "base_fee_burnt": base,
        "priority_fee": priority, "coinbase_transfers_top_level": coinbase,
        "realised_untraced": revenue - base - priority - coinbase,
        "traced": False, "realised_traced": None, "coinbase_transfers_traced": None,
    }  # fmt: skip


def test_parts_add_up_to_the_gap_and_incomplete_bundles_are_left_out():
    frame = pd.DataFrame([
        bundle(1, 5 * E18, 4 * E18, 3 * E18, 3 * E18 + E18 // 2, E18 // 4, 2 * E18, E18 // 10),
        bundle(2, E18, E18, E18, E18, E18 // 10, E18 // 2, 0),
        bundle(3, E18, E18, E18, E18, 0, 0, 0, excluded=True),
    ])  # fmt: skip
    rows = decompose(frame)
    assert list(rows["bundle_id"]) == [1, 2]
    gap = rows["estimate"] - rows["realised"]
    assert (gap - rows[PARTS].sum(axis=1)).abs().max() < 1e-12


def test_share_reports_median_and_aggregate_not_a_mean():
    kept = pd.Series([1.0, 0.5, 2.0, 3.0])
    base = pd.Series([10.0, 10.0, 1e-9, 0.0])  # a near-zero and a zero estimate
    out = describe_share(kept, base, seed=1)
    assert "mean" not in out
    assert out["n"] == 3  # the zero estimate has no share
    assert out["aggregate"] == pytest.approx(3.5 / (20 + 1e-9))
