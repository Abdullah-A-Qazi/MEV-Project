"""RQ1's simulated estimate: replay a sandwich on a local fork of mainnet with Anvil.

For a seeded sample of bundles that hold exactly one sandwich (front-run + back-run, profit
in WETH), we fork the chain at the block before, then replay only the bot's plan: the
front-run, the victims and the back-run, in their original order, sent by their original
senders (Anvil impersonation), at the original timestamp, base fee and fee recipient. Other
transactions of the block are left out, exactly like the analytical estimate.

Simulated revenue = change in the bot's ETH + WETH holdings + the gas its EOAs paid on the
fork. Comparing it with the analytical estimate shows what the AMM formula misses (token
taxes, the bot's own logic); comparing it with realised profit repeats RQ1's gap with a
second estimator.

Everything runs locally: Anvil reads mainnet state through collector/proxy.py, so every
read goes through the cache and the credit counter. Nothing is ever sent to mainnet.

    python -m profit.simulate --sample 100     -> data/results/rq1_simulation.parquet
"""

import argparse
import json
import logging
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.pipeline import read_table
from collector.proxy import RpcProxy
from collector.rpc import RpcClient
from common.config import load_config
from common.constants import PUBLIC_ROUTERS, WETH, function_selector
from common.nulls import is_missing

log = logging.getLogger("simulate")
BALANCE_OF = function_selector("balanceOf(address)")


def find_anvil() -> str | None:
    return shutil.which("anvil") or (
        str(p) if (p := Path.home() / ".foundry" / "bin" / "anvil").exists() else None
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fork:
    """A running Anvil fork at `block`, reading state through `fork_url`."""

    def __init__(self, anvil: str, fork_url: str, block: int):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            [anvil, "--fork-url", fork_url, "--fork-block-number", str(block),
             "--port", str(self.port), "--order", "fifo", "--no-mining", "--silent"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        self._id = 0
        for _ in range(300):
            try:
                self.call("eth_blockNumber")
                return
            except requests.RequestException:
                time.sleep(0.2)
        self.close()
        raise TimeoutError("anvil did not start")

    def call(self, method: str, params: list | None = None):
        self._id += 1
        reply = requests.post(
            self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method,
                            "params": params or []}, timeout=600,
        ).json()  # fmt: skip
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def holdings(self, addresses: list[str]) -> int:
        """ETH + WETH held by `addresses`, in wei."""
        total = 0
        for a in addresses:
            total += int(self.call("eth_getBalance", [a, "latest"]), 16)
            data = BALANCE_OF + a[2:].rjust(64, "0")
            total += int(self.call("eth_call", [{"to": WETH, "data": data}, "latest"]), 16)
        return total

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def pick_sample(realised: pd.DataFrame, sandwiches: pd.DataFrame, n: int, seed: int) -> list:
    """Seeded sample of bundles with exactly one sandwich, profit in WETH, fully explained."""
    eligible = realised[
        (realised["n_sandwiches"] == 1) & (realised["n_arbitrages"] == 0)
        & (realised["undetected_swaps"] == 0) & realised["analytical_estimate"].notna()
    ]  # fmt: skip
    events = sandwiches.drop_duplicates("event_id")
    weth_events = events[events["token_x"] == WETH]
    by_pair = {(r.front_tx, r.back_tx): r.event_id for r in weth_events.itertuples()}
    candidates = []
    for row in eligible.itertuples():
        hashes = list(row.tx_hashes)
        for (front, back), event_id in by_pair.items():
            if front in hashes and back in hashes:
                candidates.append((row.bundle_id, event_id))
                break
    candidates.sort()
    return random.Random(seed).sample(candidates, min(n, len(candidates)))


def simulate_event(fork: Fork, txs: list[dict], block: dict, actors: list[str]) -> dict:
    """Replay `txs` (dicts with from/to/value/input/gas_limit/gas_price) as one block."""
    fork.call("anvil_setCoinbase", [block["fee_recipient"]])
    fork.call("anvil_setNextBlockBaseFeePerGas", [hex(int(block["base_fee_per_gas"]))])
    fork.call("evm_setNextBlockTimestamp", [int(block["timestamp"])])
    before = fork.holdings(actors)
    hashes = []
    for tx in txs:
        fork.call("anvil_impersonateAccount", [tx["from"]])
        hashes.append(fork.call("eth_sendTransaction", [{
            "from": tx["from"], "to": tx["to"], "value": hex(int(tx["value"])),
            "data": tx["input"], "gas": hex(int(tx["gas_limit"])),
            "gasPrice": hex(int(tx["gas_price"])),
        }]))  # fmt: skip
    fork.call("evm_mine")
    receipts = [fork.call("eth_getTransactionReceipt", [h]) for h in hashes]
    after = fork.holdings(actors)
    gas_paid = sum(
        int(r["gasUsed"], 16) * int(r["effectiveGasPrice"], 16)
        for r, tx in zip(receipts, txs, strict=True)
        if tx["from"] in actors
    )
    statuses = [int(r["status"], 16) for r in receipts]
    return {"statuses": statuses, "sim_revenue": after - before + gas_paid}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m profit.simulate")
    parser.add_argument("--sample", type=int, default=None, help="bundles to simulate")
    parser.add_argument("--provider", nargs="+", default=["infura"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    anvil = find_anvil()
    if anvil is None:
        raise SystemExit("anvil not found: install Foundry first (see README)")

    config = load_config()
    results = config.data_dir / "results"
    n = args.sample or int(config.raw.get("profit", {}).get("simulation_sample_size", 100))
    realised = pd.read_parquet(results / "realised_profit.parquet")
    sandwiches = pd.read_parquet(results / "sandwiches.parquet")
    sample = pick_sample(realised, sandwiches, n, int(config.sample["seed"]))

    txs = read_table(config.processed_dir, "transactions").set_index("tx_hash")
    receipts = read_table(config.processed_dir, "receipts").set_index("tx_hash")
    blocks = read_table(config.processed_dir, "blocks").set_index("block_number")
    rpc = RpcClient(config.providers(args.provider), RpcCache(config.cache_path),
                    CreditLedger(config.ledger_path))  # fmt: skip
    realised = realised.set_index("bundle_id")
    events = sandwiches.groupby("event_id")

    rows = []
    for i, (bundle_id, event_id) in enumerate(sample, 1):
        group = events.get_group(event_id).sort_values("victim_log_index")
        first = group.iloc[0]
        block_number = int(first["block_number"])
        order = [first["front_tx"], *group["victim_tx"].drop_duplicates(), first["back_tx"]]
        order = sorted(set(order), key=lambda h: int(txs.loc[h, "tx_index"]))
        replay = [{
            "from": txs.loc[h, "from"], "to": txs.loc[h, "to"], "value": txs.loc[h, "value"],
            "input": txs.loc[h, "input"], "gas_limit": txs.loc[h, "gas_limit"],
            "gas_price": receipts.loc[h, "effective_gas_price"],
        } for h in order]  # fmt: skip
        actors = sorted({
            a for h in (first["front_tx"], first["back_tx"])
            for a in (txs.loc[h, "from"], txs.loc[h, "to"])
            if not is_missing(a) and a not in PUBLIC_ROUTERS
        })  # fmt: skip
        row = {"bundle_id": bundle_id, "event_id": event_id, "block_number": block_number}
        try:
            with RpcProxy(rpc, fork_block=block_number - 1) as fork_url:
                fork = Fork(anvil, fork_url, block_number - 1)
                try:
                    row |= simulate_event(fork, replay, blocks.loc[block_number].to_dict(), actors)
                finally:
                    fork.close()
            row["ok"] = all(s == 1 for s in row["statuses"])
        except Exception as exc:
            row |= {"ok": False, "error": str(exc)[:300]}
        rows.append(row)
        log.info("%d/%d %s ok=%s", i, len(sample), event_id[:24], row["ok"])

    frame = pd.DataFrame(rows)
    frame["sim_revenue"] = (
        frame.get("sim_revenue")
        .astype("object")
        .map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(int(v)))
    )
    frame["statuses"] = frame.get("statuses").map(json.dumps)
    frame.to_parquet(results / "rq1_simulation.parquet", index=False)
    print(json.dumps({"simulated": len(frame), "ok": int(frame["ok"].sum())}))


if __name__ == "__main__":
    main()
