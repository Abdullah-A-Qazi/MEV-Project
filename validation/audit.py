"""Manual audit of the detectors: seeded samples to hand-label, and the scores they give.

    python -m validation.audit sample   write the label sheets into validation/labels/
    python -m validation.audit score    precision, miss rate and recall from the filled sheets

Sheets (CSV, open in a spreadsheet; links go to Etherscan):
  sandwich_detections.csv     100 detected sandwiches     -> is it really a sandwich?
  user_swap_nondetections.csv 100 user swaps not flagged   -> was it actually sandwiched?
  arbitrage_detections.csv     50 detected arbitrages     -> is it really an arbitrage?

Fill the `label` column with y (the detector is right) or n (it is wrong), and `notes` with
what you saw. Existing labels are never overwritten by `sample`. The sheets are committed:
they are the evidence behind the precision and recall in the report.

Recall estimate: missed = miss rate among non-detected user swaps x all non-detected user
swaps; recall = detected true sandwiches / (detected true sandwiches + missed).
"""

import argparse
import json
import random
from pathlib import Path

import pandas as pd

from analysis.stats import proportion_ci
from common.config import REPO_ROOT, load_config

LABELS_DIR = REPO_ROOT / "validation" / "labels"
ETHERSCAN = "https://etherscan.io/tx/"
SHEETS = {
    "sandwich_detections": 100,
    "user_swap_nondetections": 100,
    "arbitrage_detections": 50,
}


def _sample(frame: pd.DataFrame, n: int, seed: int, key: str) -> pd.DataFrame:
    keys = sorted(frame[key].unique())
    chosen = set(random.Random(seed).sample(keys, min(n, len(keys))))
    return frame[frame[key].isin(chosen)].drop_duplicates(key).sort_values(key)


def make_sheets(results: Path, seed: int) -> dict[str, pd.DataFrame]:
    sandwiches = pd.read_parquet(results / "sandwiches.parquet")
    arbitrages = pd.read_parquet(results / "arbitrages.parquet")
    users = pd.read_parquet(results / "user_swaps.parquet")

    sw = _sample(sandwiches, SHEETS["sandwich_detections"], seed, "event_id")
    victims = sandwiches.groupby("event_id")["victim_tx"].agg(
        lambda v: " ".join(ETHERSCAN + h for h in sorted(set(v)))
    )
    sw_sheet = pd.DataFrame({
        "event_id": sw["event_id"], "block": sw["block_number"], "pool": sw["pool"],
        "attacker": sw["attacker"], "front_run": ETHERSCAN + sw["front_tx"],
        "victims": sw["event_id"].map(victims), "back_run": ETHERSCAN + sw["back_tx"],
        "label": "", "notes": "",
    })  # fmt: skip

    clean = users[~users["sandwiched"]].assign(
        key=lambda d: d["tx_hash"] + ":" + d["log_index"].astype(str)
    )
    nd = _sample(clean, SHEETS["user_swap_nondetections"], seed, "key")
    nd_sheet = pd.DataFrame({
        "swap_id": nd["key"], "block": nd["block_number"], "pool": nd["pool"],
        "transaction": ETHERSCAN + nd["tx_hash"], "public": nd["public"],
        "label": "", "notes": "",
    })  # fmt: skip

    ar = _sample(arbitrages, SHEETS["arbitrage_detections"], seed, "event_id")
    ar_sheet = pd.DataFrame({
        "event_id": ar["event_id"], "block": ar["block_number"],
        "transaction": ETHERSCAN + ar["tx_hash"], "bot": ar["tx_to"], "n_swaps": ar["n_swaps"],
        "label": "", "notes": "",
    })  # fmt: skip
    return {"sandwich_detections": sw_sheet, "user_swap_nondetections": nd_sheet,
            "arbitrage_detections": ar_sheet}  # fmt: skip


def score(labels_dir: Path, results: Path, column: str = "label") -> dict:
    """Precision, miss rate and recall from one label column: "label" (the person's
    audit) or "ai_label" (validation/ai_audit.py). Only y/n rows count."""

    def labelled(name: str) -> pd.DataFrame:
        path = labels_dir / f"{name}.csv"
        sheet = pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else None
        if sheet is None or column not in sheet:
            return pd.DataFrame(columns=["label"])
        sheet = sheet.assign(label=sheet[column])
        return sheet[sheet["label"].str.lower().isin(["y", "n"])]

    out: dict = {}
    sw, nd, ar = (labelled(n) for n in SHEETS)
    for name, sheet, what in (("sandwich_precision", sw, "y"), ("arbitrage_precision", ar, "y")):
        k, n = int((sheet["label"].str.lower() == what).sum()), len(sheet)
        out[name] = {"labelled": n, "correct": k, "value": k / n if n else None,
                     "ci": proportion_ci(k, n) if n else None}  # fmt: skip
    # In the non-detection sheet, "n" means the detector was wrong: a missed sandwich.
    missed, n = int((nd["label"].str.lower() == "n").sum()), len(nd)
    out["miss_rate_among_nondetections"] = {
        "labelled": n, "missed": missed, "value": missed / n if n else None,
        "ci": proportion_ci(missed, n) if n else None,
    }  # fmt: skip
    if n and out["sandwich_precision"]["value"] is not None:
        users = pd.read_parquet(results / "user_swaps.parquet", columns=["sandwiched"])
        detected = int(users["sandwiched"].sum()) * out["sandwich_precision"]["value"]
        not_detected = int((~users["sandwiched"]).sum())
        missed_est = missed / n * not_detected
        out["victim_recall_estimate"] = detected / (detected + missed_est) if detected else None
        # The miss rate's interval drives recall's: few checked non-detections leave it wide.
        low_miss, high_miss = proportion_ci(missed, n)
        out["victim_recall_ci"] = (
            (
                detected / (detected + high_miss * not_detected),
                detected / (detected + low_miss * not_detected),
            )
            if detected
            else None
        )
    scan = results / "ai_miss_scan.parquet"
    if column == "ai_label" and scan.exists():
        # The AI auditor's non-detection rule run on every unflagged user swap
        # (validation/ai_audit.py --scan-all). Treating every flagged swap as a real miss
        # gives a recall floor for the bracketing pattern the rule looks for.
        users = pd.read_parquet(results / "user_swaps.parquet", columns=["sandwiched"])
        detected = int(users["sandwiched"].sum())
        possible = len(pd.read_parquet(scan))
        out["ai_full_scan"] = {
            "unflagged_user_swaps": int((~users["sandwiched"]).sum()),
            "possible_misses": possible,
            "recall_if_all_were_misses": detected / (detected + possible) if detected else None,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m validation.audit")
    parser.add_argument("command", choices=["sample", "score"])
    parser.add_argument("--column", default="label", help='"label" (yours) or "ai_label"')
    args = parser.parse_args()
    config = load_config()
    results = config.data_dir / "results"
    if args.command == "sample":
        LABELS_DIR.mkdir(parents=True, exist_ok=True)
        for name, sheet in make_sheets(results, int(config.sample["seed"])).items():
            path = LABELS_DIR / f"{name}.csv"
            if path.exists():
                print(f"kept existing {path.name} (delete it to resample)")
                continue
            sheet.to_csv(path, index=False)
            print(f"wrote {len(sheet)} rows to {path.relative_to(REPO_ROOT)}")
    else:
        out = {"scored_column": args.column, **score(LABELS_DIR, results, args.column)}
        print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
