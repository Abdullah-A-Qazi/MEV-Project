"""RQ1: how far is the pre-execution profit estimate from what searchers actually keep?

Unit of analysis: a bundle (profit/bundles.py) that is fully explained (no undetected swaps)
and fully priced, with an analytical estimate. All values in ETH. For each bundle:

    estimate - realised = drift + shortfall + unmodelled + base_fee + priority_fee + coinbase

    drift       = estimate - formula         (other transactions moved the pools first)
    shortfall   = formula - actual gross     (trades took less than the formula allows)
    unmodelled  = actual gross - revenue     (flows the swap maths does not see: leftovers,
                                              token residuals valued differently, transfers)
    base_fee    = gas burnt
    priority_fee, coinbase = paid to the block builder

This is an identity (realised = revenue - gas - coinbase), so the parts add up exactly; the
test suite checks it. Writes data/results/rq1_summary.json and figures/rq1_waterfall.png.
"""

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from analysis.stats import summarise  # noqa: E402
from common.config import load_config  # noqa: E402

WEI = 1e18
PARTS = ["drift", "shortfall", "unmodelled", "base_fee", "priority_fee", "coinbase"]
LABELS = {
    "drift": "State drift",
    "shortfall": "Execution shortfall",
    "unmodelled": "Unmodelled flows",
    "base_fee": "Base fee (burnt)",
    "priority_fee": "Priority fee (builder)",
    "coinbase": "Direct tips (builder)",
}


def decompose(frame: pd.DataFrame, traced: bool = False) -> pd.DataFrame:
    """One row per usable bundle with the estimate, realised profit and the five parts."""
    usable = frame[
        (frame["undetected_swaps"] == 0)
        & frame["realised_untraced"].notna()
        & frame["analytical_estimate"].notna()
        & frame["detector_gross"].notna()
        & frame["formula_at_execution"].notna()
    ]
    if traced:
        usable = usable[usable["traced"] & usable["realised_traced"].notna()]
    e = lambda col: usable[col].astype(float) / WEI  # noqa: E731
    coinbase = e("coinbase_transfers_traced" if traced else "coinbase_transfers_top_level")
    realised = e("realised_traced" if traced else "realised_untraced")
    out = pd.DataFrame({
        "bundle_id": usable["bundle_id"],
        "kind": np.where(usable["n_arbitrages"] == 0, "sandwich",
                         np.where(usable["n_sandwiches"] == 0, "arbitrage", "mixed")),
        "estimate": e("analytical_estimate"),
        "formula": e("formula_at_execution"),
        "gross": e("detector_gross"),
        "revenue": e("revenue"),
        "realised": realised,
    })  # fmt: skip
    out["drift"] = out["estimate"] - out["formula"]
    out["shortfall"] = out["formula"] - out["gross"]
    out["unmodelled"] = out["gross"] - out["revenue"]
    out["base_fee"] = e("base_fee_burnt").values
    out["priority_fee"] = e("priority_fee").values
    # Traced realised already nets out traced tips; revenue was computed untraced, so the
    # traced coinbase gap is carried by `coinbase` and `unmodelled` stays untraced.
    out["coinbase"] = coinbase.values
    if traced:
        out["unmodelled"] = out["gross"] - (
            out["realised"] + out["base_fee"] + out["priority_fee"] + out["coinbase"]
        )
    return out.reset_index(drop=True)


def summary(rows: pd.DataFrame, seed: int) -> dict:
    error = rows["estimate"] - rows["realised"]
    kept = rows["realised"] / rows["estimate"].where(rows["estimate"] > 0)

    def s(values):
        x = summarise(values.dropna(), seed=seed, n_resamples=4000)
        return {"n": x.n, "median": x.median, "median_ci": x.median_ci,
                "mean": x.mean, "mean_ci": x.mean_ci}  # fmt: skip

    totals = {p: float(rows[p].sum()) for p in PARTS}
    estimate_total = float(rows["estimate"].sum())
    return {
        "bundles": len(rows),
        "by_kind": rows["kind"].value_counts().to_dict(),
        "estimate_total_eth": estimate_total,
        "realised_total_eth": float(rows["realised"].sum()),
        "parts_total_eth": totals,
        "parts_share_of_estimate": {p: v / estimate_total for p, v in totals.items()},
        "error_eth": s(error),
        "share_of_estimate_kept": s(kept),
        "share_with_zero_drift": float((rows["drift"].abs() < 1e-12).mean()),
    }


def waterfall(rows: pd.DataFrame, path) -> None:
    steps = [("Estimate", rows["estimate"].sum(), "total")]
    steps += [(LABELS[p], -rows[p].sum(), "step") for p in PARTS]
    steps += [("Realised", rows["realised"].sum(), "total")]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    level = 0.0
    for i, (_label, value, kind) in enumerate(steps):
        if kind == "total":
            ax.bar(i, value, color="#888780", width=0.6)
            level = value
            ax.text(i, value, f"{value:.1f}", ha="center", va="bottom", fontsize=9)
        else:
            bottom = level + min(value, 0)
            ax.bar(i, abs(value), bottom=bottom, width=0.6,
                   color="#E24B4A" if value < 0 else "#639922")  # fmt: skip
            ax.text(i, level + max(value, 0), f"{value:+.1f}", ha="center", va="bottom", fontsize=9)
            level += value
    ax.set_xticks(range(len(steps)), [s[0] for s in steps], rotation=20, ha="right")
    ax.set_ylabel("ETH, summed over bundles")
    ax.set_title("From predicted profit to what searchers kept (red: costs, green: gains)")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    seed = int(config.sample["seed"])
    frame = pd.read_parquet(results / "realised_profit.parquet")
    rows, traced = decompose(frame), decompose(frame, traced=True)
    out = {"all_usable": summary(rows, seed), "traced_sample": summary(traced, seed)}
    for kind in ("sandwich", "arbitrage"):
        part = rows[rows["kind"] == kind]
        if len(part):
            out[f"{kind}_only"] = summary(part, seed)
    (results / "figures").mkdir(parents=True, exist_ok=True)
    waterfall(rows, results / "figures" / "rq1_waterfall.png")
    (results / "rq1_summary.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    print(json.dumps(out["all_usable"], indent=1, default=float))


if __name__ == "__main__":
    main()
