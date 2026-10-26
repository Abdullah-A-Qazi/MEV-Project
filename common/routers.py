"""Which contracts are shared by many unrelated users, and so never an attacker's identity.

A sandwich's attacker is recognised by the EOA that signed its transactions or the contract
they called. That only works for a bot's own contract. A contract that thousands of
unrelated people call (Uniswap's routers, aggregators, Telegram trading-bot routers such as
Banana Gun or Maestro) says nothing about who is trading, so two strangers who happen to
use it must not be paired as "one attacker".

Rather than rely on a hand-made list alone, a contract counts as shared if at least
`detectors.shared_contract_min_senders` distinct EOAs call it in the sample. In the 2024
sample bots are called by at most 15 EOAs and shared routers by 873 or more, so the default
of 100 sits far from both. The list used is written to data/processed/shared_contracts.csv.
"""

from pathlib import Path

import duckdb
import pandas as pd

from common.constants import PUBLIC_ROUTERS

DEFAULT_MIN_SENDERS = 100


def shared_contracts(processed_dir: Path, min_senders: int = DEFAULT_MIN_SENDERS) -> pd.DataFrame:
    """Contracts called by at least `min_senders` distinct EOAs, with their sender counts."""
    txs = str(processed_dir / "transactions" / "*.parquet")
    return duckdb.sql(
        f'select "to" as contract, count(distinct "from") as senders from \'{txs}\' '
        f'where "to" is not null group by 1 having count(distinct "from") >= {int(min_senders)} '
        "order by senders desc"
    ).df()


def known_routers(config) -> frozenset[str]:
    """PUBLIC_ROUTERS plus every shared contract in the sample (and save the list)."""
    min_senders = int(config.detectors.get("shared_contract_min_senders", DEFAULT_MIN_SENDERS))
    shared = shared_contracts(config.processed_dir, min_senders)
    shared.to_csv(config.processed_dir / "shared_contracts.csv", index=False)
    return frozenset(PUBLIC_ROUTERS) | frozenset(shared["contract"])
