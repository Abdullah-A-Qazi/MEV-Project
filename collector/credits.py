"""Credit counter: tracks RPC spending per provider so collection stops before a free-tier cap.

Infura's free tier allows a fixed number of credits per UTC day and Alchemy's a fixed number
of compute units per calendar month. Each provider has a cost table (credits per method).
Before every network request the client asks the ledger whether the cost still fits under
`limit * safety_fraction` for the current period; if not, that provider is skipped.

The ledger is our own estimate. It is deliberately conservative (it records a request
before sending it), and week 1 of the project checks it against the providers' dashboards.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_usage (
    period   TEXT NOT NULL,
    provider TEXT NOT NULL,
    method   TEXT NOT NULL,
    calls    INTEGER NOT NULL,
    credits  INTEGER NOT NULL,
    PRIMARY KEY (period, provider, method)
);
"""


class BudgetExhausted(RuntimeError):
    """No configured provider has credits left in its current period."""


@dataclass(frozen=True)
class CreditPolicy:
    """Free-tier allowance of one provider."""

    limit: int | None  # credits per period; None = unmetered (e.g. a public endpoint)
    period: str = "day"  # "day" (resets 00:00 UTC) or "month" (resets on the 1st, UTC)
    safety_fraction: float = 0.95  # stop at this share of the limit
    costs: dict[str, int] = field(default_factory=dict)
    default_cost: int = 80

    def cost(self, method: str) -> int:
        return self.costs.get(method, self.default_cost)

    @property
    def budget(self) -> int | None:
        return None if self.limit is None else int(self.limit * self.safety_fraction)


def period_key(period: str, now: datetime) -> str:
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "month":
        return now.strftime("%Y-%m")
    raise ValueError(f"unknown credit period {period!r}")


def next_reset(period: str, now: datetime) -> datetime:
    """UTC time at which the current period ends."""
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return start_of_day + timedelta(days=1)
    if period == "month":
        first = start_of_day.replace(day=1)
        return (first + timedelta(days=32)).replace(day=1)
    raise ValueError(f"unknown credit period {period!r}")


class CreditLedger:
    def __init__(self, path: Path | str, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)
        self._clock = clock or (lambda: datetime.now(UTC))

    def used(self, provider: str, policy: CreditPolicy) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM credit_usage WHERE period = ? AND provider = ?",
            (period_key(policy.period, self._clock()), provider),
        ).fetchone()
        return int(row[0])

    def remaining(self, provider: str, policy: CreditPolicy) -> int | None:
        if policy.budget is None:
            return None
        return max(0, policy.budget - self.used(provider, policy))

    def can_spend(self, provider: str, policy: CreditPolicy, method: str) -> bool:
        remaining = self.remaining(provider, policy)
        return remaining is None or policy.cost(method) <= remaining

    def record(self, provider: str, policy: CreditPolicy, method: str, credits: int) -> None:
        self._conn.execute(
            "INSERT INTO credit_usage VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(period, provider, method) DO UPDATE SET "
            "calls = calls + 1, credits = credits + excluded.credits",
            (period_key(policy.period, self._clock()), provider, method, credits),
        )
        self._conn.commit()

    def refund(self, provider: str, policy: CreditPolicy, method: str, credits: int) -> None:
        """Undo a recorded charge for a request the provider does not bill (429, 5xx)."""
        self._conn.execute(
            "UPDATE credit_usage SET credits = MAX(0, credits - ?) "
            "WHERE period = ? AND provider = ? AND method = ?",
            (credits, period_key(policy.period, self._clock()), provider, method),
        )
        self._conn.commit()

    def report(self) -> list[tuple[str, str, str, int, int]]:
        """All rows as (period, provider, method, calls, credits), newest period first."""
        return self._conn.execute(
            "SELECT period, provider, method, calls, credits FROM credit_usage "
            "ORDER BY period DESC, provider, credits DESC"
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
