"""Flashbots Mempool Dumpster: download the daily transaction lists and index them.

Mempool Dumpster (https://mempool-dumpster.flashbots.net, CC-0) publishes, per UTC day, every
transaction its observers saw in the public mempool, with the time it was first seen. We
need, for each sampled day D, the files for D and D-1: a transaction included shortly after
midnight may have been first seen the previous day.

    python -m mempool.dumpster download   fetch the zipped CSVs into data/mempool/raw/
    python -m mempool.dumpster index      data/mempool/seen.parquet: first sighting of each
                                          transaction in our sampled blocks

Downloads resume: a file whose size already matches the server's is skipped.
"""

import argparse
import csv
import io
import json
import logging
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from collector.pipeline import read_table
from common.config import Config, load_config

log = logging.getLogger(__name__)

BASE_URL = "https://mempool-dumpster.flashbots.net/ethereum/mainnet"


def needed_days(sample_days: list[date]) -> list[date]:
    """Each sampled day and the day before it, sorted, without duplicates."""
    return sorted({d for day in sample_days for d in (day, day - timedelta(days=1))})


def file_url(day: date) -> str:
    return f"{BASE_URL}/{day:%Y-%m}/{day:%Y-%m-%d}.csv.zip"


def download(day: date, raw_dir: Path, session: requests.Session) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{day:%Y-%m-%d}.csv.zip"
    url = file_url(day)
    expected = int(session.head(url, timeout=60).headers.get("content-length", -1))
    if path.exists() and path.stat().st_size == expected:
        return path
    tmp = path.with_suffix(".part")
    with session.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    if tmp.stat().st_size != expected:
        raise OSError(f"{url}: got {tmp.stat().st_size} bytes, expected {expected}")
    tmp.rename(path)
    return path


def read_day(path: Path, keep: set[str]) -> pd.DataFrame:
    """(tx_hash, first_seen_ms, sources) from one zipped daily CSV, only for hashes in `keep`.

    A day holds about a million sightings; filtering while streaming keeps memory small.
    """
    rows = []
    with zipfile.ZipFile(path) as zf:
        [name] = [n for n in zf.namelist() if n.endswith(".csv")]
        with zf.open(name) as raw:
            for r in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8")):
                tx_hash = r["hash"].lower()
                if tx_hash in keep:
                    rows.append((tx_hash, int(r["timestamp_ms"]), r.get("sources", "")))
    return pd.DataFrame(rows, columns=["tx_hash", "first_seen_ms", "sources"])


def build_index(raw_dir: Path, out: Path, keep: set[str]) -> dict[str, int]:
    """One row per kept hash seen in any downloaded day, with its earliest sighting."""
    files = sorted(raw_dir.glob("*.csv.zip"))
    frames = []
    for i, path in enumerate(files, 1):
        frames.append(read_day(path, keep))
        log.info("%d/%d %s: %d matching sightings", i, len(files), path.name, len(frames[-1]))
    seen = pd.concat(frames, ignore_index=True)
    seen = seen.sort_values("first_seen_ms").drop_duplicates("tx_hash", keep="first")
    out.parent.mkdir(parents=True, exist_ok=True)
    seen.to_parquet(out, index=False)
    return {"files": len(files), "hashes_kept": len(keep), "hashes_seen": len(seen)}


def sample_days(config: Config) -> list[date]:
    with open(config.sample_path) as f:
        return sorted({date.fromisoformat(r["utc_date"]) for r in csv.DictReader(f)})


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mempool.dumpster")
    parser.add_argument("command", choices=["download", "index"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    raw_dir = config.data_dir / "mempool" / "raw"
    if args.command == "download":
        session = requests.Session()
        days = needed_days(sample_days(config))
        for i, day in enumerate(days, 1):
            download(day, raw_dir, session)
            log.info("%d/%d %s", i, len(days), day)
        (raw_dir / "PROVENANCE.json").write_text(json.dumps({
            "source": BASE_URL, "licence": "CC-0", "files": [file_url(d) for d in days],
            "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }, indent=1) + "\n")  # fmt: skip
    else:
        keep = set(read_table(config.processed_dir, "transactions")["tx_hash"])
        counts = build_index(raw_dir, config.data_dir / "mempool" / "seen.parquet", keep)
        print(json.dumps(counts))


if __name__ == "__main__":
    main()
