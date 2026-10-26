"""RQ3: can users reduce their exposure to sandwich attacks, or are they hopeless?

Input: data/results/user_swaps.parquet (python -m mitigation.dataset). Five analyses, as
planned in the methodology note:

Uniswap V2 and V3 swaps are both included (rates are also given per protocol); the
safe-threshold analysis uses V2 victims only, as its maths is constant-product.

1. Private submission: sandwich rate for public vs private swaps, plus the private rate
   after removing public swaps that Mempool Dumpster simply missed (coverage baseline).
2. Slippage tolerance: sandwich rate and loss by tolerance bucket (public, decoded swaps).
3. Trade size relative to the pool: sandwich rate by size bucket.
4. Logistic regression of P(sandwiched) on tolerance bucket and log trade size (public,
   decoded swaps), to separate the two effects.
5. Safe threshold and avoidable loss: for each WETH-paying victim, the tolerance below which
   the best sandwich would not have covered the attacker's gas; the share of victim losses
   that a tolerance at or below that threshold, or private submission, would have avoided.

Writes data/results/rq3_summary.json and figures/rq3_*.png.
"""

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import statsmodels.formula.api as smf  # noqa: E402

from analysis.stats import proportion_ci  # noqa: E402
from collector.pipeline import read_table  # noqa: E402
from common.config import load_config  # noqa: E402
from mitigation.safe_threshold import safe_tolerance  # noqa: E402

SLIP_EDGES = [-np.inf, 0.001, 0.005, 0.01, 0.03, 0.05, 0.10, np.inf]
SLIP_LABELS = ["<0.1%", "0.1-0.5%", "0.5-1%", "1-3%", "3-5%", "5-10%", ">=10%"]
SIZE_EDGES = [0, 1e-4, 1e-3, 1e-2, 5e-2, np.inf]
SIZE_LABELS = ["<0.01%", "0.01-0.1%", "0.1-1%", "1-5%", ">=5%"]


def rate(flags: pd.Series) -> dict:
    n, k = int(flags.size), int(flags.sum())
    return {"n": n, "sandwiched": k, "rate": k / n if n else float("nan"),
            "rate_ci": proportion_ci(k, n)}  # fmt: skip


def private_rate_corrected(df: pd.DataFrame, miss_rate: float) -> float:
    """Sandwich rate among truly private swaps, after removing the public swaps that the
    mempool observers missed (they are counted as "private" but behave like public ones)."""
    pub, priv = df[df["public"]], df[~df["public"]]
    missed_public = miss_rate / (1 - miss_rate) * len(pub)
    true_private = len(priv) - missed_public
    if true_private <= 0:
        return float("nan")
    expected_from_missed = missed_public * pub["sandwiched"].mean()
    return max(0.0, (priv["sandwiched"].sum() - expected_from_missed) / true_private)


def attack_gas(config) -> float:
    """Median gas used by a sandwich (front-run + back-run)."""
    sandwiches = pd.read_parquet(config.data_dir / "results" / "sandwiches.parquet")
    events = sandwiches.drop_duplicates("event_id")
    gas = read_table(config.processed_dir, "receipts").set_index("tx_hash")["gas_used"]
    return float((events["front_tx"].map(gas) + events["back_tx"].map(gas)).median())


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    df = pd.read_parquet(results / "user_swaps.parquet")
    rq2 = json.loads((results / "rq2_summary.json").read_text())
    miss = rq2["coverage_baseline"]["miss_rate"]
    out: dict = {"population": {"user_swaps": len(df), "decoded_share": float(df["decoded"].mean()),
                                "victims": int(df["sandwiched"].sum())}}  # fmt: skip

    # 1. Private submission
    out["private_submission"] = {
        "all": {"public": rate(df[df["public"]]["sandwiched"]),
                "private": rate(df[~df["public"]]["sandwiched"]),
                "private_rate_corrected": private_rate_corrected(df, miss)},
        "decoded": {"public": rate(df[df["public"] & df["decoded"]]["sandwiched"]),
                    "private": rate(df[~df["public"] & df["decoded"]]["sandwiched"]),
                    "private_rate_corrected": private_rate_corrected(df[df["decoded"]], miss)},
    }  # fmt: skip

    out["by_protocol"] = {
        proto: {"public": rate(g[g["public"]]["sandwiched"]),
                "private": rate(g[~g["public"]]["sandwiched"]),
                "decoded_share": float(g["decoded"].mean())}
        for proto, g in df.groupby("protocol")
    }  # fmt: skip

    # 2-3. Tolerance and size buckets, public decoded swaps
    pub = df[df["public"] & df["decoded"]].dropna(subset=["tolerance", "size_ratio"]).copy()
    pub["slip"] = pd.cut(pub["tolerance"], SLIP_EDGES, labels=SLIP_LABELS, right=False)
    pub["size"] = pd.cut(pub["size_ratio"], SIZE_EDGES, labels=SIZE_LABELS, right=False)
    pub["loss_pct"] = pub["loss_eth"] / pub["amount_in_eth"] * 100
    out["by_tolerance"] = {
        str(b): {**rate(g["sandwiched"]), "median_loss_pct_of_trade":
                 float(g.loc[g["sandwiched"], "loss_pct"].median())}
        for b, g in pub.groupby("slip", observed=False)
    }  # fmt: skip
    out["by_size"] = {str(b): rate(g["sandwiched"]) for b, g in pub.groupby("size", observed=False)}
    heat = pub.pivot_table(index="size", columns="slip", values="sandwiched", aggfunc="mean",
                           observed=False)  # fmt: skip

    # 4. Logistic regression
    reg = pub[pub["size_ratio"] > 0].copy()
    reg["log_size"] = np.log10(reg["size_ratio"])
    reg["y"] = reg["sandwiched"].astype(int)
    model = smf.logit("y ~ C(slip, Treatment('<0.1%')) + log_size", data=reg).fit(disp=False)
    ci = model.conf_int()
    out["logit"] = {
        "n": int(model.nobs), "pseudo_r2": float(model.prsquared),
        "odds_ratios": {k: {"or": float(np.exp(v)), "ci": (float(np.exp(ci.loc[k, 0])),
                                                           float(np.exp(ci.loc[k, 1])))}
                        for k, v in model.params.items() if k != "Intercept"},
    }  # fmt: skip

    # 5. Safe threshold and avoidable loss
    gas = attack_gas(config)
    base_fee = read_table(config.processed_dir, "blocks").set_index("block_number")[
        "base_fee_per_gas"
    ]
    victims = df[df["sandwiched"] & df["loss_eth"].notna() & (df["loss_eth"] > 0)].copy()
    weth_victims = victims[
        victims["decoded"]
        & (victims["kind"] == "exact_input")
        & victims["input_is_weth"]
        # Constant-product maths: V2 pools only (a hypothetical V3 attack may cross ranges).
        & (victims["protocol"] == "v2")
    ].copy()
    weth_victims["safe"] = [
        safe_tolerance(int(r.amount_in), int(r.reserve_in), int(r.reserve_out),
                       int(int(base_fee[r.block_number]) * gas))
        for r in weth_victims.itertuples()
    ]  # fmt: skip
    above = weth_victims["tolerance"] > weth_victims["safe"]
    total_loss = victims["loss_eth"].sum()
    out["safe_threshold"] = {
        "attack_gas_median": gas,
        "victims_evaluated": len(weth_victims),
        "safe_tolerance_median": float(weth_victims["safe"].median()),
        "safe_tolerance_iqr": (float(weth_victims["safe"].quantile(0.25)),
                               float(weth_victims["safe"].quantile(0.75))),
        "victim_tolerance_median": float(weth_victims["tolerance"].median()),
        "share_victims_above_safe": float(above.mean()),
        "loss_avoidable_by_tolerance_share": float(
            weth_victims.loc[above, "loss_eth"].sum() / weth_victims["loss_eth"].sum()),
    }  # fmt: skip
    out["avoidable_loss"] = {
        "total_victim_loss_eth": float(total_loss),
        "by_private_submission_share": float(
            victims.loc[victims["public"], "loss_eth"].sum() / total_loss),
        "by_tolerance_share_of_evaluated": out["safe_threshold"][
            "loss_avoidable_by_tolerance_share"
        ],
        "by_either_share_of_evaluated": float(
            weth_victims.loc[above | weth_victims["public"], "loss_eth"].sum()
            / weth_victims["loss_eth"].sum()),
    }  # fmt: skip

    figures = results / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    tol = out["by_tolerance"]
    fig, ax = plt.subplots(figsize=(8, 4))
    rates = [tol[b]["rate"] * 100 for b in SLIP_LABELS]
    errs = [[(tol[b]["rate"] - tol[b]["rate_ci"][0]) * 100 for b in SLIP_LABELS],
            [(tol[b]["rate_ci"][1] - tol[b]["rate"]) * 100 for b in SLIP_LABELS]]  # fmt: skip
    ax.bar(SLIP_LABELS, rates, yerr=errs, color="#888780", capsize=3)
    for i, b in enumerate(SLIP_LABELS):
        ax.text(i, rates[i] + errs[1][i] + 0.3, f"{rates[i]:.1f}%\nn={tol[b]['n']}",
                ha="center", fontsize=8)  # fmt: skip
    ax.set_ylim(0, max(r + e for r, e in zip(rates, errs[1], strict=True)) * 1.25)
    ax.set_xlabel("Slippage tolerance the user set")
    ax.set_ylabel("Sandwiched (%)")
    ax.set_title("Below 0.1% slippage sandwiches almost vanish; above it, slippage matters little")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(figures / "rq3_rate_by_tolerance.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4))
    im = ax.imshow(heat.values * 100, cmap="Blues", aspect="auto")
    ax.set_xticks(range(len(heat.columns)), heat.columns)
    ax.set_yticks(range(len(heat.index)), heat.index)
    for i in range(heat.shape[0]):
        for j in range(heat.shape[1]):
            v = heat.values[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v * 100:.0f}", ha="center", va="center", fontsize=8)
    ax.set_xlabel("Slippage tolerance")
    ax.set_ylabel("Trade size / pool reserve")
    ax.set_title("Above 0.1% slippage, trade size drives the sandwich rate (%)")
    fig.colorbar(im, ax=ax, label="%")
    fig.tight_layout()
    fig.savefig(figures / "rq3_heatmap.png", dpi=150)
    plt.close(fig)

    (results / "rq3_summary.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    print(json.dumps({k: out[k] for k in ("population", "private_submission", "by_protocol",
                                          "safe_threshold",
                                          "avoidable_loss")}, indent=1, default=float))  # fmt: skip


if __name__ == "__main__":
    main()
