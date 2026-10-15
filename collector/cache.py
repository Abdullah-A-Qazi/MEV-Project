"""Local cache of RPC responses, so that no piece of chain data is ever fetched twice.

Responses are stored in SQLite, keyed by a hash of (method, params). Each row also keeps
the provider that served it and the UTC time it was fetched, which is the provenance
record for every raw datum the project uses.

Only immutable data belongs here: callers must not cache anything that refers to
"latest"/"pending" or to blocks that could still be reorganised (see RpcClient).
"""

import hashlib
import json
import sqlite3
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rpc_cache (
    key        TEXT PRIMARY KEY,
    method     TEXT NOT NULL,
    params     TEXT NOT NULL,
    result     BLOB NOT NULL,
    provider   TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rpc_cache_method ON rpc_cache(method);
"""


def canonical_params(params: list[Any]) -> str:
    """Stable JSON encoding of params: sorted keys, no whitespace, lowercase hex strings.

    Lowercasing makes "0xAbC" and "0xabc" (the same address or block) share one entry.
    """

    def normalise(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("0x"):
            return value.lower()
        if isinstance(value, dict):
            return {k: normalise(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalise(v) for v in value]
        return value

    return json.dumps(normalise(params), sort_keys=True, separators=(",", ":"))


def cache_key(method: str, params: list[Any]) -> str:
    return hashlib.sha256(f"{method}:{canonical_params(params)}".encode()).hexdigest()


class RpcCache:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)

    def get(self, method: str, params: list[Any]) -> Any | None:
        row = self._conn.execute(
            "SELECT result FROM rpc_cache WHERE key = ?", (cache_key(method, params),)
        ).fetchone()
        if row is None:
            return None
        return json.loads(zlib.decompress(row[0]))

    def contains(self, method: str, params: list[Any]) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM rpc_cache WHERE key = ?", (cache_key(method, params),)
        ).fetchone()
        return row is not None

    def put(self, method: str, params: list[Any], result: Any, provider: str) -> None:
        if result is None:
            raise ValueError("refusing to cache a null result (it may mean 'not available yet')")
        blob = zlib.compress(json.dumps(result, separators=(",", ":")).encode(), level=6)
        self._conn.execute(
            "INSERT OR REPLACE INTO rpc_cache VALUES (?, ?, ?, ?, ?, ?)",
            (
                cache_key(method, params),
                method,
                canonical_params(params),
                blob,
                provider,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self._conn.commit()

    def provenance_summary(self) -> list[dict[str, Any]]:
        """Per (method, provider): number of entries and first/last fetch time."""
        rows = self._conn.execute(
            "SELECT method, provider, COUNT(*), MIN(fetched_at), MAX(fetched_at) "
            "FROM rpc_cache GROUP BY method, provider ORDER BY method, provider"
        ).fetchall()
        return [
            {
                "method": m,
                "provider": p,
                "entries": n,
                "first_fetched": first,
                "last_fetched": last,
            }
            for m, p, n, first, last in rows
        ]

    def close(self) -> None:
        self._conn.close()
