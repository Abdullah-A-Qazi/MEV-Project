"""Scoring of the hand-labelled audit sheets."""

import pandas as pd

from validation.audit import score


def test_score_from_labelled_sheets(tmp_path):
    labels, results = tmp_path / "labels", tmp_path / "results"
    labels.mkdir()
    results.mkdir()
    pd.DataFrame({"label": ["y"] * 9 + ["n"] + [""] * 5}).to_csv(
        labels / "sandwich_detections.csv", index=False
    )
    pd.DataFrame({"label": ["y"] * 19 + ["n"]}).to_csv(
        labels / "user_swap_nondetections.csv", index=False
    )
    # 100 detected victims, 1,000 non-detected user swaps
    pd.DataFrame({"sandwiched": [True] * 100 + [False] * 1000}).to_parquet(
        results / "user_swaps.parquet"
    )
    out = score(labels, results)
    assert out["sandwich_precision"]["labelled"] == 10  # blanks ignored
    assert out["sandwich_precision"]["value"] == 0.9
    assert out["miss_rate_among_nondetections"]["value"] == 0.05
    # 90 true detections; missed = 0.05 x 1000 = 50 -> recall 90 / 140
    assert abs(out["victim_recall_estimate"] - 90 / 140) < 1e-12
    assert out["arbitrage_precision"]["labelled"] == 0  # sheet absent
