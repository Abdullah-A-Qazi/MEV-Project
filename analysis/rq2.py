"""RQ2: what share of detected MEV never appeared in the public mempool before inclusion?

Inputs: data/results/visibility.parquet (python -m mempool.visibility) and the detector
results. "Private" = not seen by Mempool Dumpster strictly before the block's timestamp.

* Coverage baseline: the share of plain ETH transfers Dumpster did not see first estimates
  its miss rate m. Every private share is reported raw and corrected:
  (observed - m) / (1 - m) (analysis/stats.py: coverage_corrected).
* Events: a sandwich is private when neither its front-run nor its back-run was seen; an
  arbitrage when its transaction was not seen. By value: events whose profit is in WETH,
  weighted by their gross profit.
* Victims and ordinary user swaps are reported too: RQ3 needs their public/private split.

Writes data/results/rq2_summary.json and figures/rq2_private_share.png.
"""

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from analysis.stats import coverage_corrected, proportion_ci  # noqa: E402
from common.config import load_config  # noqa: E402
from common.constants import WETH  # noqa: E402


def share(unseen: pd.Series, miss_rate: float) -> dict:
    n, k = int(unseen.size), int(unseen.sum())
    low, high = proportion_ci(k, n)
    observed = k / n if n else float("nan")
    return {
        "n": n,
        "private_observed": observed,
        "private_observed_ci": (low, high),
        "private_corrected": coverage_corrected(observed, miss_rate),
        "private_corrected_ci": (coverage_corrected(low, miss_rate),
                                 coverage_corrected(high, miss_rate)),
    }  # fmt: skip


def _weth_values(tokens: pd.Series, amounts: pd.Series) -> list[float | None]:
    """Gross profit in ETH where it is denominated in WETH, else None."""
    return [int(v) / 1e18 if t == WETH else None for t, v in zip(tokens, amounts, strict=True)]


def _was_seen(hashes: pd.Series, seen_before: pd.Series) -> pd.Series:
    return hashes.map(seen_before).fillna(False).astype(bool)


def event_table(vis: pd.DataFrame, sandwiches: pd.DataFrame, arbitrages: pd.DataFrame):
    seen_before = vis.set_index("tx_hash")["seen_before"]
    month = vis.drop_duplicates("block_number").set_index("block_number")["block_ts_ms"]
    sw = sandwiches.drop_duplicates("event_id")
    events = pd.concat([
        pd.DataFrame({
            "kind": "sandwich",
            "block_number": sw["block_number"].values,
            "private": (~_was_seen(sw["front_tx"], seen_before)
                        & ~_was_seen(sw["back_tx"], seen_before)).values,
            "weth_value": _weth_values(sw["token_x"], sw["attacker_gross_profit_x"]),
        }),
        pd.DataFrame({
            "kind": "arbitrage",
            "block_number": arbitrages["block_number"].values,
            "private": (~_was_seen(arbitrages["tx_hash"], seen_before)).values,
            "weth_value": _weth_values(arbitrages["start_token"], arbitrages["gross_profit"]),
        }),
    ], ignore_index=True)  # fmt: skip
    events["month"] = pd.to_datetime(events["block_number"].map(month), unit="ms").dt.strftime(
        "%Y-%m"
    )
    return events


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    vis = pd.read_parquet(results / "visibility.parquet")
    sandwiches = pd.read_parquet(results / "sandwiches.parquet")
    arbitrages = pd.read_parquet(results / "arbitrages.parquet")

    transfers = vis[vis["category"] == "eth_transfer"]
    miss = float((~transfers["seen_before"]).mean())
    miss_ci = proportion_ci(int((~transfers["seen_before"]).sum()), len(transfers))
    out = {
        "coverage_baseline": {"n_eth_transfers": len(transfers), "miss_rate": miss,
                              "miss_rate_ci": miss_ci},
        "transactions": {c: share(~g["seen_before"], miss)
                         for c, g in vis.groupby("category")},
    }  # fmt: skip

    events = event_table(vis, sandwiches, arbitrages)
    out["events"] = {k: share(g["private"], miss) for k, g in events.groupby("kind")}
    valued = events.dropna(subset=["weth_value"])
    valued = valued[valued["weth_value"] > 0]
    out["events_by_value"] = {
        k: {"n": len(g), "eth_total": float(g["weth_value"].sum()),
            "private_share_of_value": float(g.loc[g["private"], "weth_value"].sum()
                                            / g["weth_value"].sum())}
        for k, g in valued.groupby("kind")
    }  # fmt: skip
    out["sandwiches_by_month"] = {
        m: share(g["private"], miss)
        for m, g in events[events["kind"] == "sandwich"].groupby("month")
    }
    victims = vis[vis["category"] == "sandwich_victim"]
    public_victims = victims[victims["seen_before"]]
    out["victim_lead_time_s"] = {
        "median": float(public_victims["lead_ms"].median() / 1000),
        "p90": float(public_victims["lead_ms"].quantile(0.9) / 1000),
    }

    (results / "figures").mkdir(parents=True, exist_ok=True)
    cats = ["sandwich_front", "sandwich_back", "arbitrage", "sandwich_victim", "user_swap"]
    names = ["Sandwich front-runs", "Sandwich back-runs", "Arbitrages", "Sandwich victims",
             "Other user swaps"]  # fmt: skip
    vals = [out["transactions"][c]["private_corrected"] * 100 for c in cats]
    cis = [out["transactions"][c]["private_corrected_ci"] for c in cats]
    errs = [
        [v - ci[0] * 100 for v, ci in zip(vals, cis, strict=True)],
        [ci[1] * 100 - v for v, ci in zip(vals, cis, strict=True)],
    ]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.barh(names[::-1], vals[::-1], xerr=[e[::-1] for e in errs], color="#888780", capsize=3)
    for i, (v, upper) in enumerate(zip(vals[::-1], errs[1][::-1], strict=True)):
        ax.text(v + upper + 1.5, i, f"{v:.0f}%", va="center", fontsize=9)
    ax.set_xlim(0, 112)
    ax.set_xlabel(
        "Private: not seen in the public mempool before inclusion (%, coverage-corrected)"
    )
    ax.set_title("Bots' transactions are almost all private; victims' almost all public")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(results / "figures" / "rq2_private_share.png", dpi=150)
    plt.close(fig)

    (results / "rq2_summary.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    headline = ("coverage_baseline", "events", "events_by_value", "victim_lead_time_s")
    print(json.dumps({k: out[k] for k in headline}, indent=1, default=float))


if __name__ == "__main__":
    main()
