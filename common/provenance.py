"""Provenance records: every derived dataset says where its data came from and when."""

import json
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collector.cache import RpcCache
from common.config import REPO_ROOT


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_provenance(
    path: Path,
    dataset: str,
    blocks: Iterable[int],
    cache: RpcCache | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    blocks = sorted(set(blocks))
    record = {
        "dataset": dataset,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_commit": git_commit(),
        "block_count": len(blocks),
        "block_range": [blocks[0], blocks[-1]] if blocks else None,
        "sources": cache.provenance_summary() if cache else [],
        **(extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1) + "\n")
