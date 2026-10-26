"""Builds tests/fixtures/uniswap_v3_golden.json: real V3 swaps whose starting state is known.

For two consecutive swaps s1, s2 in the same V3 pool and block, with no Mint or Burn on that
pool between them, s2 started exactly from the state s1 left (sqrtPriceX96, liquidity). If
s2's liquidity after equals s1's, s2 stayed inside one range, so profit/uniswap_v3.py must
reproduce s2's price after and its amounts exactly, as an exact-input or an exact-output
swap (we do not know which the trader used; one of them must match).

Built from the cached sample (no network): run after `python -m detectors`, which records
each V3 pool's fee tier.

    uv run python -m validation.build_v3_golden --cases 60
"""

import argparse
import json
import random
from datetime import UTC, datetime

import pandas as pd

from collector.pipeline import read_table
from common.config import REPO_ROOT, load_config
from common.constants import UNISWAP_V3_SWAP_TOPIC, event_topic

V3_MINT = event_topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
V3_BURN = event_topic("Burn(address,int24,int24,uint128,uint256,uint256)")
OUTPUT = REPO_ROOT / "tests" / "fixtures" / "uniswap_v3_golden.json"


def _signed(word: int) -> int:
    return word - (1 << 256) if word >= 1 << 255 else word


def _decode(data: str) -> dict:
    w = [int(data[2 + i : 2 + i + 64], 16) for i in range(0, len(data) - 2, 64)]
    return {"amount0": _signed(w[0]), "amount1": _signed(w[1]), "sqrt_price_x96": w[2],
            "liquidity": w[3], "tick": _signed(w[4])}  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=int, default=60)
    args = parser.parse_args()
    config = load_config()
    logs = read_table(config.processed_dir, "logs")
    logs = logs[logs["topic0"].isin([UNISWAP_V3_SWAP_TOPIC, V3_MINT, V3_BURN])]
    pools = pd.read_parquet(config.processed_dir / "pools.parquet")
    fees = {r.address: int(r.fee) for r in pools.itertuples()
            if r.dex == "uniswap_v3" and r.fee == r.fee}  # fmt: skip

    candidates = []
    for (block, pool), group in logs.groupby(["block_number", "address"]):
        if pool not in fees:
            continue
        rows = group.sort_values("log_index").to_dict("records")
        for prev, cur in zip(rows, rows[1:], strict=False):
            if prev["topic0"] != UNISWAP_V3_SWAP_TOPIC or cur["topic0"] != UNISWAP_V3_SWAP_TOPIC:
                continue  # a Mint/Burn between them changes the state
            a, b = _decode(prev["data"]), _decode(cur["data"])
            if a["liquidity"] != b["liquidity"] or b["amount0"] == 0 or b["amount1"] == 0:
                continue
            candidates.append({
                "block_number": int(block), "pool": pool, "fee": fees[pool],
                "tx_hash": cur["tx_hash"], "log_index": int(cur["log_index"]),
                "sqrt_price_before": str(a["sqrt_price_x96"]), "liquidity": str(a["liquidity"]),
                "amount0": str(b["amount0"]), "amount1": str(b["amount1"]),
                "sqrt_price_after": str(b["sqrt_price_x96"]),
            })  # fmt: skip
    cases = random.Random(int(config.sample["seed"])).sample(
        candidates, min(args.cases, len(candidates))
    )
    fixture = {
        "provenance": {
            "source": "Ethereum mainnet V3 Swap/Mint/Burn logs from the cached sample "
            "(sample_blocks.csv); pool fee tiers read with eth_call fee()",
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "candidates": len(candidates),
            "builder": "validation/build_v3_golden.py",
        },
        "cases": sorted(cases, key=lambda c: (c["block_number"], c["log_index"])),
    }
    OUTPUT.write_text(json.dumps(fixture, indent=1) + "\n")
    print(f"wrote {len(cases)} of {len(candidates)} candidate cases to {OUTPUT.name}")


if __name__ == "__main__":
    main()
