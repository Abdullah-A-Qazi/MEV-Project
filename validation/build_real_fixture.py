"""Freezes the V2 logs of a few real blocks into tests/fixtures/ for detector regression tests.

The fixture holds everything the detectors need (Swap/Sync logs, tx sender/target, pool
metadata), so the tests run offline. The expected detections are the detector's own output
at the time the fixture was built, with attackers that are well-known sandwich bots. That
makes it a REGRESSION fixture: it catches unintended changes. Before treating any case as
ground truth, hand-check it on Etherscan and record the check in `hand_checked`.

Run after `python -m collector build` and `python -m detectors` for those blocks:
    uv run python -m validation.build_real_fixture --blocks 17200000 17200001 17200002
"""

import argparse
import json
from datetime import UTC, datetime

import pandas as pd

from collector.pipeline import read_table
from common.config import REPO_ROOT, load_config
from common.constants import UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", nargs="+", type=int, required=True)
    args = parser.parse_args()
    config = load_config()
    blocks = set(args.blocks)

    logs = read_table(config.processed_dir, "logs")
    logs = logs[
        logs["block_number"].isin(blocks)
        & logs["topic0"].isin([UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC])
    ]
    txs = read_table(config.processed_dir, "transactions")
    txs = txs[txs["tx_hash"].isin(set(logs["tx_hash"]))]
    pools = pd.read_parquet(config.processed_dir / "pools.parquet")
    pools = pools[pools["address"].isin(set(logs["address"]))]
    sandwiches = pd.read_parquet(config.data_dir / "results" / "sandwiches.parquet")
    sandwiches = sandwiches[sandwiches["block_number"].isin(blocks)]

    expected = (
        sandwiches.groupby(["block_number", "pool", "front_tx", "back_tx", "attacker"])["victim_tx"]
        .apply(sorted)
        .reset_index()
        .to_dict("records")
    )
    fixture = {
        "provenance": {
            "source": "Ethereum mainnet, cached RPC responses (see data/processed/PROVENANCE.json)",
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "blocks": sorted(blocks),
            "builder": "validation/build_real_fixture.py",
            "status": "regression (detector output), not hand-labelled",
        },
        "hand_checked": [],
        "logs": logs.to_dict("records"),
        "txs": {r["tx_hash"]: {"from": r["from"], "to": r["to"]} for r in txs.to_dict("records")},
        "pools": pools.to_dict("records"),
        "expected_sandwiches": [{**e, "block_number": int(e["block_number"])} for e in expected],
    }
    first, last = min(blocks), max(blocks)
    out = REPO_ROOT / "tests" / "fixtures" / f"real_blocks_{first}_{last}.json"
    out.write_text(json.dumps(fixture, indent=1, default=int) + "\n")
    print(f"wrote {len(fixture['logs'])} logs, {len(expected)} sandwiches to {out.name}")


if __name__ == "__main__":
    main()
