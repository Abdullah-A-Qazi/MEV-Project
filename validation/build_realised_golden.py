"""Builds tests/fixtures/realised_golden_<name>.json: searcher accounting checked against balances.

For each (block, searcher) pair it records every transaction in the block that touches the
searcher, their execution traces, and the searcher's ETH and WETH balances before and after
the block. The test then checks that profit.realised.account reproduces the true change in
the searcher's ETH + WETH holdings exactly:

    eth_flow - gas_fee  ==  sum over S of (ETH balance change + WETH balance change)

Balances come from chain state (eth_getBalance, WETH.balanceOf at blocks b-1 and b), which is
independent of the Transfer logs and traces the accounting uses.

Searchers are the sandwich attackers and arbitrage bots found by `python -m detectors` in the
chosen blocks.
Run (after collector build + detectors for those blocks; traces need an archive provider):
    uv run python -m validation.build_realised_golden --name 2023 --blocks 17200000 ...
"""

import argparse
import json
from datetime import UTC, datetime

import pandas as pd

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.pipeline import read_table
from collector.rpc import RpcClient
from common.config import REPO_ROOT, load_config
from common.constants import ERC20_TRANSFER_TOPIC, PUBLIC_ROUTERS, WETH, function_selector
from common.nulls import nulls_to_none

BALANCE_OF = function_selector("balanceOf(address)")


def _prune(frame: dict) -> dict:
    """Keep only what the accounting reads from a callTracer frame."""
    kept = {k: frame[k] for k in ("type", "from", "to", "value", "error") if k in frame}
    if frame.get("calls"):
        kept["calls"] = [_prune(c) for c in frame["calls"]]
    return kept


def _token_balance(rpc: RpcClient, token: str, owner: str, block: int) -> int:
    data = BALANCE_OF + owner[2:].rjust(64, "0")
    return int(rpc.call("eth_call", [{"to": token, "data": data}, hex(block)])[:66], 16)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", nargs="+", type=int, required=True)
    parser.add_argument("--provider", default="drpc")
    parser.add_argument("--name", required=True, help="fixture name suffix, e.g. 2024")
    args = parser.parse_args()
    config = load_config()
    rpc = RpcClient(
        config.providers([args.provider]),
        RpcCache(config.cache_path),
        CreditLedger(config.ledger_path),
        max_retries=8,
    )
    blocks = set(args.blocks)
    processed = config.processed_dir
    block_rows = read_table(processed, "blocks").set_index("block_number")
    txs = read_table(processed, "transactions")
    receipts = read_table(processed, "receipts").set_index("tx_hash")
    logs = read_table(processed, "logs")
    sandwiches = pd.read_parquet(config.data_dir / "results" / "sandwiches.parquet")
    arbitrages = pd.read_parquet(config.data_dir / "results" / "arbitrages.parquet")

    cases = []
    for block in sorted(blocks):
        attackers = set(sandwiches.loc[sandwiches["block_number"] == block, "attacker"])
        for arb in arbitrages[arbitrages["block_number"] == block].to_dict("records"):
            bot = arb["tx_to"] if arb["tx_to"] not in PUBLIC_ROUTERS else arb["tx_from"]
            attackers.add(bot)
        block_txs = txs[txs["block_number"] == block]
        block_logs = logs[logs["block_number"] == block]
        for contract in sorted(attackers):
            eoas = set(block_txs.loc[block_txs["to"] == contract, "from"])
            actors = sorted(eoas | {contract})
            # Every tx that touches S: sent by S, calling S, or moving tokens to/from S.
            padded = {"0x" + "0" * 24 + a[2:] for a in actors}
            transfer_logs = block_logs[block_logs["topic0"] == ERC20_TRANSFER_TOPIC]
            touching = set(
                transfer_logs.loc[
                    transfer_logs["topic1"].isin(padded) | transfer_logs["topic2"].isin(padded),
                    "tx_hash",
                ]
            )
            touching |= set(
                block_txs.loc[
                    block_txs["from"].isin(actors) | block_txs["to"].isin(actors), "tx_hash"
                ]
            )
            case_txs = []
            for tx in (
                block_txs[block_txs["tx_hash"].isin(touching)]
                .sort_values("tx_index")
                .pipe(nulls_to_none)
                .to_dict("records")
            ):
                receipt = receipts.loc[tx["tx_hash"]]
                trace = rpc.call(
                    "debug_traceTransaction", [tx["tx_hash"], {"tracer": "callTracer"}]
                )
                tx_logs = block_logs[
                    (block_logs["tx_hash"] == tx["tx_hash"])
                    & (block_logs["topic0"] == ERC20_TRANSFER_TOPIC)
                ]
                case_txs.append(
                    {
                        "tx_hash": tx["tx_hash"],
                        "tx_from": tx["from"],
                        "tx_to": tx["to"],
                        "value": tx["value"],
                        "status": int(receipt["status"]),
                        "gas_used": int(receipt["gas_used"]),
                        "effective_gas_price": receipt["effective_gas_price"],
                        "logs": tx_logs.pipe(nulls_to_none).to_dict("records"),
                        "trace": _prune(trace),
                    }
                )
            balances = {
                a: {
                    "eth_before": str(int(rpc.call("eth_getBalance", [a, hex(block - 1)]), 16)),
                    "eth_after": str(int(rpc.call("eth_getBalance", [a, hex(block)]), 16)),
                    "weth_before": str(_token_balance(rpc, WETH, a, block - 1)),
                    "weth_after": str(_token_balance(rpc, WETH, a, block)),
                }
                for a in actors
            }
            # Every other token S sent or received: balances before/after, summed over S.
            tokens = set()
            for tx in case_txs:
                for log in tx["logs"]:
                    if log["address"] != WETH and (
                        log["topic1"] in padded or log["topic2"] in padded
                    ):
                        tokens.add(log["address"])
            token_changes = {}
            for token in sorted(tokens):
                try:
                    token_changes[token] = str(
                        sum(
                            _token_balance(rpc, token, a, block)
                            - _token_balance(rpc, token, a, block - 1)
                            for a in actors
                        )
                    )
                except Exception as exc:  # e.g. a token without a standard balanceOf
                    print(f"  skipping token {token}: {exc}")
            cases.append(
                {
                    "block_number": block,
                    "fee_recipient": block_rows.loc[block, "fee_recipient"],
                    "base_fee_per_gas": block_rows.loc[block, "base_fee_per_gas"],
                    "searcher_contract": contract,
                    "actors": actors,
                    "txs": case_txs,
                    "balances": balances,
                    "token_balance_changes": token_changes,
                }
            )
            print(f"block {block} searcher {contract[:10]}: {len(case_txs)} txs")

    fixture = {
        "provenance": {
            "source": "Ethereum mainnet: tables from the RPC cache; traces and balances via "
            + args.provider,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "blocks": sorted(blocks),
            "builder": "validation/build_realised_golden.py",
        },
        "cases": cases,
    }
    output = REPO_ROOT / "tests" / "fixtures" / f"realised_golden_{args.name}.json"
    output.write_text(json.dumps(fixture, indent=1, default=int) + "\n")
    print(f"wrote {len(cases)} cases to {output.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
