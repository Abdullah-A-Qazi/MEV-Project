"""First results summary from data/results/ (rebuilt by `make results`, no network).

Writes data/results/summary.json and prints it. Figures for each RQ will be added here as
the corresponding components land; this is the skeleton they plug into.
"""

import json

import pandas as pd

from analysis.stats import summarise
from common.config import load_config


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    seed = int(config.sample["seed"])
    swaps = pd.read_parquet(results / "swaps.parquet")
    sandwiches = pd.read_parquet(results / "sandwiches.parquet")
    arbitrages = pd.read_parquet(results / "arbitrages.parquet")

    victims = sandwiches.dropna(subset=["victim_loss_fraction"])
    loss = summarise(victims["victim_loss_fraction"] * 100, seed=seed)
    summary = {
        "blocks": int(swaps["block_number"].nunique()) if len(swaps) else 0,
        "v2_swaps": len(swaps),
        "arbitrages": len(arbitrages),
        "sandwiches": int(sandwiches[["front_tx", "back_tx", "pool"]].drop_duplicates().shape[0])
        if len(sandwiches)
        else 0,
        "sandwich_victims": len(sandwiches),
        "victim_loss_pct_of_output": {
            "n": loss.n,
            "median": loss.median,
            "median_95ci": loss.median_ci,
            "mean": loss.mean,
            "mean_95ci": loss.mean_ci,
        },
    }
    (results / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
