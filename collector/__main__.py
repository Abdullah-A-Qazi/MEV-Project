"""Command line for the collector.

    python -m collector sample              resolve config.yaml's sampling rule -> sample_blocks.csv
    python -m collector collect             fetch sampled blocks into the cache (spends credits)
    python -m collector collect --wait      ... and when out of budget, sleep until the reset
    python -m collector build               cache -> Parquet tables (offline, free)
    python -m collector credits             show credit usage per provider and method

`--blocks 17000000 17000001` overrides the sample, and `--provider` the provider order.
"""

import argparse
import json
import logging
import time
from datetime import UTC, datetime

from collector.cache import RpcCache
from collector.credits import CreditLedger, next_reset
from collector.pipeline import build_tables, collect
from collector.rpc import RpcClient
from collector.sampling import build_sample, read_sample, write_sample
from common.config import load_config
from common.provenance import write_provenance

log = logging.getLogger("collector")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m collector")
    parser.add_argument("command", choices=["sample", "collect", "build", "credits"])
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--provider", nargs="+", help="provider names, in order of preference")
    parser.add_argument("--blocks", nargs="+", type=int, help="explicit block numbers")
    parser.add_argument("--wait", action="store_true", help="sleep through budget resets")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config) if args.config else load_config()
    cache, ledger = RpcCache(config.cache_path), CreditLedger(config.ledger_path)

    if args.command == "credits":
        for row in ledger.report():
            print("{:<10} {:<11} {:<28} calls={:<8} credits={}".format(*row))
        return

    def blocks() -> list[int]:
        if args.blocks:
            return args.blocks
        if not config.sample_path.exists():
            raise SystemExit(f"{config.sample_path.name} not found: run `make sample` first")
        return read_sample(config.sample_path)

    if args.command == "build":
        rpc = RpcClient([], cache, ledger, offline=True)
        counts = build_tables(rpc, blocks(), config.processed_dir)
        write_provenance(
            config.processed_dir / "PROVENANCE.json",
            dataset="raw chain tables (blocks, transactions, receipts, logs)",
            blocks=blocks(),
            cache=cache,
            extra={"row_counts": counts},
        )
        print(json.dumps(counts, indent=1))
        return

    providers = config.providers(args.provider)
    rpc = RpcClient(providers, cache, ledger)

    if args.command == "sample":
        rows = build_sample(rpc, config.sample)
        write_sample(config.sample_path, rows)
        print(
            f"wrote {len(rows)} blocks over {len({d for d, _ in rows})} days "
            f"to {config.sample_path.name}"
        )
        return

    # collect
    while True:
        result = collect(rpc, blocks())
        print(json.dumps(result))
        if not (result["stopped_early"] and args.wait):
            break
        resume = min(next_reset(p.policy.period, datetime.now(UTC)) for p in providers)
        log.info("budget exhausted; sleeping until %s", resume.isoformat())
        time.sleep(max(60.0, (resume - datetime.now(UTC)).total_seconds() + 60))


if __name__ == "__main__":
    main()
