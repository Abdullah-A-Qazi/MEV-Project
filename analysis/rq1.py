"""RQ1: how far is the pre-execution profit estimate from what searchers actually keep?

Unit of analysis: a bundle (profit/bundles.py) that is fully explained (no undetected swaps
and no transfers to anything but a detected pool) and fully priced, with an analytical
estimate. All values in ETH. For each bundle:

    estimate - realised = drift + shortfall + unmodelled + base_fee + priority_fee + coinbase

    drift       = estimate - formula         (other transactions moved the pools first)
    shortfall   = formula - actual gross     (trades took less than the formula allows)
    unmodelled  = actual gross - revenue     (flows the swap maths does not see: leftovers,
                                              token residuals valued differently, transfers)
    base_fee    = gas burnt
    priority_fee, coinbase = paid to the block builder

This is an identity (realised = revenue - gas - coinbase), so the parts add up exactly;
tests/test_rq1.py checks it. Untraced, `coinbase` holds only top-level ETH sent to the fee
recipient: tips paid inside a contract need a trace, so the all-bundles figure leaves them
in `realised` and the traced sample measures them.

Shares (what fraction of the estimate a bot kept) are reported as median and as the
aggregate ratio (sum kept / sum estimated), not as a mean: a few estimates sit close to zero
and blow a mean of ratios up to meaningless values.

If the Anvil replay has been run (profit/simulate.py), its sample is compared with the other
estimates too. Writes data/results/rq1_summary.json and figures/rq1_waterfall.png.
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
UNTRACED_COINBASE_LABEL = "Top-level tips\n(internal: traced only)"


def describe(values: pd.Series, seed: int) -> dict:
    x = summarise(values.dropna(), seed=seed, n_resamples=4000)
    return {"n": x.n, "median": x.median, "median_ci": x.median_ci,
            "mean": x.mean, "mean_ci": x.mean_ci}  # fmt: skip


def describe_share(kept: pd.Series, base: pd.Series, seed: int) -> dict:
    """Median of kept / base over bundles with base > 0, and the aggregate ratio over them."""
    defined = base > 0
    x = summarise((kept[defined] / base[defined]).dropna(), seed=seed, n_resamples=4000)
    both = defined & kept.notna()
    return {
        "n": x.n,
        "median": x.median,
        "median_ci": x.median_ci,
        "aggregate": float(kept[both].sum() / base[both].sum()),
    }


def decompose(frame: pd.DataFrame, traced: bool = False) -> pd.DataFrame:
    """One row per usable bundle with the estimate, realised profit and the five parts."""
    usable = frame[
        (frame["undetected_swaps"] == 0)
        & (frame.get("unexplained_transfers", 0) == 0)
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
    totals = {p: float(rows[p].sum()) for p in PARTS}
    estimate_total = float(rows["estimate"].sum())
    return {
        "bundles": len(rows),
        "by_kind": rows["kind"].value_counts().to_dict(),
        "estimate_total_eth": estimate_total,
        "realised_total_eth": float(rows["realised"].sum()),
        "parts_total_eth": totals,
        "parts_share_of_estimate": {p: v / estimate_total for p, v in totals.items()},
        "error_eth": describe(error, seed),
        "share_of_estimate_kept": describe_share(rows["realised"], rows["estimate"], seed),
        "share_with_zero_drift": float((rows["drift"].abs() < 1e-12).mean()),
    }


def waterfall(rows: pd.DataFrame, path, traced: pd.DataFrame | None = None) -> None:
    """Estimate to realised, summed over untraced bundles.

    Internal tips are invisible without traces, so the tips bar is labelled as top-level
    only, and the traced sample's share of tips is printed under the chart.
    """
    labels = {**LABELS, "coinbase": UNTRACED_COINBASE_LABEL}
    steps = [("Estimate", rows["estimate"].sum(), "total")]
    steps += [(labels[p], -rows[p].sum(), "step") for p in PARTS]
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
            label = f"{value or 0.0:+.1f}"  # `or 0.0` turns -0.0 into 0.0
            ax.text(i, level + max(value, 0), label, ha="center", va="bottom", fontsize=9)
            level += value
    ax.set_xticks(range(len(steps)), [s[0] for s in steps], rotation=20, ha="right")
    ax.set_ylabel("ETH, summed over bundles")
    ax.set_title("From predicted profit to what searchers kept (red: costs, green: gains)")
    ax.spines[["top", "right"]].set_visible(False)
    if traced is not None and len(traced) and traced["estimate"].sum() > 0:
        share = traced["coinbase"].sum() / traced["estimate"].sum()
        fig.text(0.01, 0.01, f"Untraced bundles cannot show tips paid inside a contract. In the "
                 f"traced sample ({len(traced)} bundles) direct tips were {share:.0%} of the "
                 f"estimate.", fontsize=8, color="#5F5E5A")  # fmt: skip
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def simulation_summary(sim: pd.DataFrame, frame: pd.DataFrame, seed: int) -> dict:
    """Compare the Anvil replay (profit/simulate.py) with the other estimates, per bundle.

    sim_revenue = the bot's ETH + WETH change on the fork, plus the gas it paid there: the
    same quantity as the actual gross for a WETH sandwich, measured independently of the swap
    maths. So sim vs gross checks the replay, sim vs analytical shows what the formula
    misses, and sim vs realised repeats RQ1's gap with a second estimator.
    """
    ok = sim[sim["ok"] & sim["sim_revenue"].notna()].merge(frame, on="bundle_id")
    e = lambda col: ok[col].astype(float) / WEI  # noqa: E731
    simulated = e("sim_revenue")
    exact = (ok["sim_revenue"].astype(int) - ok["detector_gross"].astype(int)).abs()
    realised = e("realised_untraced")  # NaN where a leftover token could not be priced
    gap = simulated - realised
    statuses = sim["statuses"].map(lambda v: json.loads(v) if isinstance(v, str) else None)
    reverted = {"front": 0, "victim": 0, "back": 0, "no_receipts": 0}
    for st in statuses[~sim["ok"]]:
        if not isinstance(st, list):
            reverted["no_receipts"] += 1
            continue
        reverted["front"] += st[0] == 0
        reverted["victim"] += 0 in st[1:-1]
        reverted["back"] += st[-1] == 0
    return {
        "sampled": len(sim),
        "failed_legs": reverted,
        "replayed_ok": len(ok),
        "failed": int((~sim["ok"]).sum()),
        "sim_equals_actual_gross_exactly": float((exact == 0).mean()),
        "sim_within_1e-9_eth_of_gross": float((exact <= 10**9).mean()),
        "abs_error_analytical_vs_sim_eth": describe(
            (e("analytical_estimate") - simulated).abs(), seed
        ),
        "abs_error_formula_vs_sim_eth": describe(
            (e("formula_at_execution") - simulated).abs(), seed
        ),
        "sim_minus_realised_eth": describe(gap, seed),
        "share_of_sim_kept": describe_share(realised, simulated, seed),
    }


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    seed = int(config.sample["seed"])
    frame = pd.read_parquet(results / "realised_profit.parquet")
    rows, traced = decompose(frame), decompose(frame, traced=True)
    out = {"all_usable": summary(rows, seed), "traced_sample": summary(traced, seed)}
    sim_path = results / "rq1_simulation.parquet"
    if sim_path.exists():
        out["simulation"] = simulation_summary(pd.read_parquet(sim_path), frame, seed)
    for kind in ("sandwich", "arbitrage"):
        part = rows[rows["kind"] == kind]
        if len(part):
            out[f"{kind}_only"] = summary(part, seed)
    (results / "figures").mkdir(parents=True, exist_ok=True)
    waterfall(rows, results / "figures" / "rq1_waterfall.png", traced)
    (results / "rq1_summary.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    print(json.dumps(out["all_usable"], indent=1, default=float))


if __name__ == "__main__":
    main()
