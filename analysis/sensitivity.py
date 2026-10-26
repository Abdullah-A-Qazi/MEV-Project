"""Robustness of the headline answers (python -m analysis.sensitivity, offline).

1. Sandwich amount tolerance: the back-run must sell within +/-tol of what the front-run
   bought. We rerun detection at 10%, 20% (the setting used) and 50%.
2. Split sample: the same answers for January-March and April-June 2024 separately, as a
   stand-in for a second sample window.

For each variant: number of sandwiches, RQ2 private share of sandwiches (neither leg seen
before inclusion), and RQ3 sandwich rate of public vs private user swaps.
Writes data/results/sensitivity.json.
"""

import json

import pandas as pd

from analysis.stats import proportion_ci
from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.pipeline import read_table
from collector.rpc import RpcClient
from common.config import load_config
from common.routers import known_routers
from detectors.load import load_swaps
from detectors.sandwich import detect_sandwiches

TOLERANCES = (0.1, 0.2, 0.5)
HALVES = {"Jan-Mar 2024": ("2024-01", "2024-03"), "Apr-Jun 2024": ("2024-04", "2024-06")}


def answers(sandwiches, user_keys: set, seen_before: dict, user_public: dict) -> dict:
    """RQ2 and RQ3 headline numbers for one set of detected sandwiches."""
    private = [
        not seen_before.get(s.front.tx_hash, False) and not seen_before.get(s.back.tx_hash, False)
        for s in sandwiches
    ]
    victims = {(v.tx_hash, v.log_index) for s in sandwiches for v in s.victims}
    out = {"sandwiches": len(sandwiches),
           "private_share": sum(private) / len(private) if private else None,
           "private_share_ci": proportion_ci(sum(private), len(private))}  # fmt: skip
    for label, want in (("public", True), ("private", False)):
        keys = [k for k in user_keys if user_public[k] is want]
        hit = sum(k in victims for k in keys)
        out[f"rate_{label}"] = hit / len(keys) if keys else None
        out[f"rate_{label}_ci"] = proportion_ci(hit, len(keys))
    return out


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    rpc = RpcClient([], RpcCache(config.cache_path), CreditLedger(config.ledger_path), offline=True)
    swaps, _, _, _ = load_swaps(config, rpc)
    vis = pd.read_parquet(results / "visibility.parquet", columns=["tx_hash", "seen_before"])
    seen_before = dict(zip(vis["tx_hash"], vis["seen_before"].astype(bool), strict=True))
    users = pd.read_parquet(results / "user_swaps.parquet",
                            columns=["tx_hash", "log_index", "block_number", "public"])  # fmt: skip
    user_public = {(h, int(i)): bool(p) for h, i, _, p in users.itertuples(index=False)}
    user_block = {(h, int(i)): int(b) for h, i, b, _ in users.itertuples(index=False)}
    blocks = read_table(config.processed_dir, "blocks")
    month = dict(zip(blocks["block_number"],
                     pd.to_datetime(blocks["timestamp"], unit="s").dt.strftime("%Y-%m"),
                     strict=True))  # fmt: skip
    # A user swap that was a bot's leg under one tolerance is still a user swap under the
    # population used for RQ3 (role other or victim at the 20% setting).
    user_keys = set(user_public)

    routers = known_routers(config)
    out: dict = {"tolerance": {}, "halves": {}}
    for tol in TOLERANCES:
        out["tolerance"][str(tol)] = answers(
            detect_sandwiches(swaps, tol, routers), user_keys, seen_before, user_public
        )
    tolerance = float(config.detectors.get("sandwich", {}).get("amount_tolerance", 0.2))
    base = detect_sandwiches(swaps, tolerance, routers)
    for label, (first, last) in HALVES.items():
        part = [s for s in base if first <= month[s.block_number] <= last]
        keys = {k for k in user_keys if first <= month[user_block[k]] <= last}
        out["halves"][label] = answers(part, keys, seen_before, user_public)
    (results / "sensitivity.json").write_text(json.dumps(out, indent=1, default=float) + "\n")
    print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
