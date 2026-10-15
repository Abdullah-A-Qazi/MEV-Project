"""Loads config.yaml and .env into typed objects."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from collector.credits import CreditPolicy
from collector.rpc import Provider

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.yaml"

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / self.raw.get("data_dir", "data")

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache" / "rpc_cache.sqlite"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "cache" / "credit_ledger.sqlite"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def sample_path(self) -> Path:
        return self.root / "sample_blocks.csv"

    @property
    def sample(self) -> dict[str, Any]:
        return self.raw["sample"]

    @property
    def detectors(self) -> dict[str, Any]:
        return self.raw.get("detectors", {})

    def providers(self, order: list[str] | None = None) -> list[Provider]:
        """Enabled providers in the requested order (default: `provider_order`)."""
        names = order or self.raw.get("provider_order", [])
        result = []
        for name in names:
            spec = self.raw["providers"][name]
            url = expand_env(spec["url"])
            if url is None:
                continue
            policy = CreditPolicy(
                limit=spec.get("limit"),
                period=spec.get("period", "day"),
                safety_fraction=spec.get("safety_fraction", 0.95),
                costs=dict(spec.get("costs") or {}),
                default_cost=spec.get("default_cost", 80),
            )
            result.append(Provider(name, url, policy, spec.get("credits_per_second")))
        return result


def expand_env(template: str) -> str | None:
    """Substitute ${VAR} references; None if any referenced variable is unset or empty."""
    missing = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal missing
        value = os.environ.get(match.group(1), "")
        if not value:
            missing = True
        return value

    expanded = _ENV_REF.sub(substitute, template)
    return None if missing else expanded


def load_config(path: Path | str = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    load_dotenv(path.parent / ".env")
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, root=path.parent)
