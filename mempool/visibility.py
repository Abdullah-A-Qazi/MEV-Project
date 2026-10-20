"""Was each transaction in our sample visible in the public mempool before its block? (RQ2)

A transaction counts as publicly visible if Mempool Dumpster's observers saw it strictly
before its block's timestamp. Seen only afterwards, or never, means it most likely reached
the builder privately. Two caveats are handled in analysis/rq2.py:

* Observers miss some public transactions. Plain ETH transfers between wallets almost always
  go through the public mempool, so the share of them Dumpster missed estimates its miss rate
  (the coverage baseline) and is used to correct the private share.
* Builders pay the proposer with a plain transfer that never enters the mempool; transfers
  from or to the block's fee recipient are therefore kept out of the baseline.

    python -m mempool.visibility   -> data/results/visibility.parquet
"""

import numpy as np
import pandas as pd

from collector.pipeline import read_table
from common.config import load_config

MEV_ROLES = ("sandwich_front", "sandwich_back", "arbitrage")
# When one transaction plays several roles, the first match wins.
CATEGORY_ORDER = (*MEV_ROLES, "sandwich_victim", "user_swap", "eth_transfer", "other")


def categorise(txs: pd.DataFrame, swap_roles: pd.DataFrame, fee_recipients: pd.Series) -> pd.Series:
    """One category per transaction (see CATEGORY_ORDER).

    txs: tx_hash, block_number, from, to, value, input.
    swap_roles: tx_hash, role (from data/results/swaps.parquet).
    fee_recipients: block_number -> fee recipient address.
    """
    roles = swap_roles.groupby("tx_hash")["role"].agg(set)
    tx_roles = txs["tx_hash"].map(roles)

    def has(role: str) -> pd.Series:
        return tx_roles.map(lambda r: isinstance(r, set) and role in r)

    recipient = txs["block_number"].map(fee_recipients)
    plain_transfer = (
        (txs["input"] == "0x")
        & (txs["value"] != "0")
        & (txs["from"] != recipient)
        & (txs["to"] != recipient)
    )
    conditions = [has(r) for r in MEV_ROLES] + [
        has("sandwich_victim"),
        has("other"),
        plain_transfer,
    ]
    return pd.Series(
        np.select(conditions, list(CATEGORY_ORDER[:-1]), default="other"), index=txs.index
    )


def build(config) -> pd.DataFrame:
    processed, results = config.processed_dir, config.data_dir / "results"
    blocks = read_table(processed, "blocks")[["block_number", "timestamp", "fee_recipient"]]
    txs = read_table(processed, "transactions")[
        ["tx_hash", "block_number", "tx_index", "from", "to", "value", "input"]
    ]
    swap_roles = pd.read_parquet(results / "swaps.parquet", columns=["tx_hash", "role"])
    seen = pd.read_parquet(config.data_dir / "mempool" / "seen.parquet")

    out = txs.merge(blocks, on="block_number").merge(seen, on="tx_hash", how="left")
    out["category"] = categorise(out, swap_roles, blocks.set_index("block_number")["fee_recipient"])
    out["block_ts_ms"] = out["timestamp"] * 1000
    out["seen"] = out["first_seen_ms"].notna()
    out["seen_before"] = out["first_seen_ms"] < out["block_ts_ms"]
    out["lead_ms"] = out["block_ts_ms"] - out["first_seen_ms"]
    return out[
        ["tx_hash", "block_number", "tx_index", "category", "seen", "seen_before",
         "first_seen_ms", "block_ts_ms", "lead_ms", "sources"]
    ]  # fmt: skip


def main() -> None:
    config = load_config()
    table = build(config)
    path = config.data_dir / "results" / "visibility.parquet"
    table.to_parquet(path, index=False)
    print(table.groupby("category")["seen_before"].agg(["size", "mean"]).round(4).to_string())


if __name__ == "__main__":
    main()
