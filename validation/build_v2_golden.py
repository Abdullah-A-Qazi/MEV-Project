"""Builds tests/fixtures/uniswap_v2_golden.json: real swaps whose true output is known.

For each chosen pool we fetch its Swap and Sync logs over a recent block range and keep
swaps made through Uniswap V2 Router02 (or the SushiSwap router) with a single-hop path.
For these, the router computed the trade with getAmountOut / getAmountIn on the pool's
reserves at that moment, so our maths must reproduce the on-chain amounts to the wei:

* exact-input calls (swapExact...): amountOut == get_amount_out(amountIn, reserves)
* exact-output calls (swap...ForExact...): amountIn == get_amount_in(amountOut, reserves)

The pre-swap reserves are taken from the pool's previous Sync event, which is independent of
the swap being tested, so the fixture also checks the reserve reconstruction identity.

Run: uv run python -m validation.build_v2_golden   (uses the keyless public RPC)
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from eth_abi import decode

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.pools import fetch_pool_info
from collector.rpc import RpcClient
from common.config import REPO_ROOT, load_config
from common.constants import (
    SUSHISWAP_ROUTER,
    UNISWAP_V2_ROUTER_02,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V2_SYNC_TOPIC,
    V2_ROUTER_SWAP_FUNCTIONS,
)

POOLS = {
    "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc": "Uniswap V2 USDC/WETH",
    "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852": "Uniswap V2 WETH/USDT",
    "0xa478c2975ab1ea89e8196811f51a7b7ade33eb11": "Uniswap V2 DAI/WETH",
    "0x397ff1542f962076d0bfe58ea045ffa2d347aca0": "SushiSwap USDC/WETH",
}
ROUTERS = {UNISWAP_V2_ROUTER_02, SUSHISWAP_ROUTER}
OUTPUT = REPO_ROOT / "tests" / "fixtures" / "uniswap_v2_golden.json"


def _words(data: str) -> list[int]:
    return [int(data[2 + i : 2 + i + 64], 16) for i in range(0, len(data) - 2, 64)]


def _path_length(tx_input: str) -> int | None:
    """Number of tokens in the router call's path argument (2 = single hop)."""
    selector = tx_input[:10]
    if selector not in V2_ROUTER_SWAP_FUNCTIONS:
        return None
    signature, _ = V2_ROUTER_SWAP_FUNCTIONS[selector]
    types = signature[signature.index("(") + 1 : -1].split(",")
    args = decode(types, bytes.fromhex(tx_input[10:]))
    path = next(a for a, t in zip(args, types, strict=True) if t == "address[]")
    return len(path)


def build_window(
    rpc: RpcClient, to_block: int, blocks_back: int, per_pool: int, chunk: int
) -> tuple[list[dict], dict]:
    """Golden cases from the `blocks_back` blocks ending at `to_block`."""
    from_block = to_block - blocks_back
    cases = []
    pools_meta = {}
    for pool, label in POOLS.items():
        info = fetch_pool_info(rpc, pool, to_block)
        pools_meta[pool] = {
            "label": label,
            "token0": info.token0,
            "token1": info.token1,
            "dex": info.dex,
        }
        logs = []
        for start in range(from_block, to_block + 1, chunk):
            logs += rpc.call(
                "eth_getLogs",
                [
                    {
                        "address": pool,
                        "topics": [[UNISWAP_V2_SWAP_TOPIC, UNISWAP_V2_SYNC_TOPIC]],
                        "fromBlock": hex(start),
                        "toBlock": hex(min(start + chunk - 1, to_block)),
                    }
                ],
            )
        logs.sort(key=lambda r: (int(r["blockNumber"], 16), int(r["logIndex"], 16)))

        counts = {"exact_input": 0, "exact_output": 0}
        prev_sync = None  # reserves after the previous reserve-changing call (pre-state)
        this_sync = None  # Sync emitted by the current call, just before its Swap
        for entry in logs:
            if entry["topics"][0] == UNISWAP_V2_SYNC_TOPIC:
                prev_sync, this_sync = this_sync, _words(entry["data"])
                continue
            if prev_sync is None or this_sync is None:
                continue
            if all(c >= per_pool for c in counts.values()):
                break
            tx = rpc.call("eth_getTransactionByHash", [entry["transactionHash"]])
            if tx["to"] is None or tx["to"].lower() not in ROUTERS:
                continue
            selector = tx["input"][:10].lower()
            if _path_length(tx["input"]) != 2:
                continue
            kind = V2_ROUTER_SWAP_FUNCTIONS[selector][1]
            if counts[kind] >= per_pool:
                continue
            a0_in, a1_in, a0_out, a1_out = _words(entry["data"])
            cases.append(
                {
                    "pool": pool,
                    "dex": info.dex,
                    "block_number": int(entry["blockNumber"], 16),
                    "tx_hash": entry["transactionHash"].lower(),
                    "log_index": int(entry["logIndex"], 16),
                    "router_function": V2_ROUTER_SWAP_FUNCTIONS[selector][0].split("(")[0],
                    "kind": kind,
                    "reserve0_before": str(prev_sync[0]),
                    "reserve1_before": str(prev_sync[1]),
                    "reserve0_after": str(this_sync[0]),
                    "reserve1_after": str(this_sync[1]),
                    "amount0_in": str(a0_in),
                    "amount1_in": str(a1_in),
                    "amount0_out": str(a0_out),
                    "amount1_out": str(a1_out),
                }
            )
            counts[kind] += 1
        print(f"blocks {from_block}-{to_block} {label}: {counts}")
    return cases, pools_meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--to-blocks",
        type=int,
        nargs="+",
        # Mid-2021 (Router02 at its busiest) and mid-2023 (after the Universal Router launch).
        default=[13_000_000, 18_000_000],
        help="end block of each window",
    )
    parser.add_argument("--blocks-back", type=int, default=600)
    parser.add_argument("--chunk", type=int, default=100, help="blocks per eth_getLogs call")
    parser.add_argument("--per-pool", type=int, default=8, help="cases per pool and kind")
    parser.add_argument("--provider", default="drpc")
    args = parser.parse_args()

    config = load_config()
    rpc = RpcClient(
        config.providers([args.provider]),
        RpcCache(config.cache_path),
        CreditLedger(config.ledger_path),
        max_retries=8,  # keyless endpoints throttle hard; be patient
    )
    cases, pools_meta, ranges = [], {}, []
    for to_block in args.to_blocks:
        window_cases, window_pools = build_window(
            rpc, to_block, args.blocks_back, args.per_pool, args.chunk
        )
        cases += window_cases
        pools_meta.update(window_pools)
        ranges.append([to_block - args.blocks_back, to_block])
    fixture = {
        "provenance": {
            "source": "Ethereum mainnet via " + ", ".join(p.name for p in rpc.providers),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "block_ranges": ranges,
            "builder": "validation/build_v2_golden.py",
        },
        "pools": pools_meta,
        "cases": cases,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT).write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"wrote {len(fixture['cases'])} cases to {OUTPUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
