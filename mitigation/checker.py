"""Sandwich risk checker: how exposed is a planned Uniswap V2 trade? (stretch goal)

    uv run python -m mitigation.checker --pool 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc \\
        --sell 0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2 --amount 2.5 --slippage 1.0

Reads the pool's current reserves and base fee (three uncached calls, about 240 Infura
credits), then reports, for the trade as planned:
  * the most a sandwich attacker could take at the chosen slippage (mitigation/safe_threshold),
  * the safe slippage: the highest tolerance at which no sandwich covers the attacker's gas,
  * the advice that follows (tighten slippage, or send privately).

It only reads chain state. It never signs or sends anything.
"""

import argparse
import json
from pathlib import Path

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.rpc import RpcClient
from common.config import load_config
from common.constants import function_selector
from mitigation.safe_threshold import max_sandwich_profit, safe_tolerance
from profit.uniswap_v2 import get_amount_out

GET_RESERVES = function_selector("getReserves()")
TOKEN0 = function_selector("token0()")
DECIMALS = function_selector("decimals()")
# Median gas of a detected sandwich (front-run + back-run) in the 2024 sample, as of the
# 26 Oct 2026 results. Used only when data/results/rq3_summary.json is not available.
DEFAULT_ATTACK_GAS = 380_587


def default_attack_gas(results_dir: Path) -> int:
    """The attack gas median from the latest RQ3 results, so the checker cannot go stale."""
    try:
        summary = json.loads((results_dir / "rq3_summary.json").read_text())
        return int(summary["safe_threshold"]["attack_gas_median"])
    except (OSError, KeyError, TypeError, ValueError):
        return DEFAULT_ATTACK_GAS


def assess(amount_in: int, r_in: int, r_out: int, slippage: float, gas_cost: int) -> dict:
    """Pure maths behind the checker (raw units of the sold token; costs in that token)."""
    expected = get_amount_out(amount_in, r_in, r_out)
    worst = max_sandwich_profit(amount_in, r_in, r_out, slippage)
    safe = safe_tolerance(amount_in, r_in, r_out, gas_cost)
    return {
        "expected_out": expected,
        "max_attacker_profit": worst,
        "worth_attacking": worst > gas_cost,
        "safe_slippage": safe,
        "size_vs_pool": amount_in / r_in,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m mitigation.checker", description=__doc__)
    parser.add_argument("--pool", required=True, help="Uniswap V2 / SushiSwap pair address")
    parser.add_argument("--sell", required=True, help="address of the token you sell")
    parser.add_argument("--amount", type=float, required=True, help="amount sold, in tokens")
    parser.add_argument("--slippage", type=float, required=True, help="tolerance in percent")
    parser.add_argument(
        "--gas",
        type=int,
        default=None,
        help="attack gas used (default: the sample's median attack gas)",
    )
    parser.add_argument("--provider", nargs="+", default=["infura"])
    args = parser.parse_args()

    config = load_config()
    if args.gas is None:
        args.gas = default_attack_gas(config.data_dir / "results")
    rpc = RpcClient(config.providers(args.provider), RpcCache(config.cache_path),
                    CreditLedger(config.ledger_path))  # fmt: skip
    pool, sell = args.pool.lower(), args.sell.lower()
    reserves = rpc.call("eth_call", [{"to": pool, "data": GET_RESERVES}, "latest"])
    token0 = "0x" + rpc.call("eth_call", [{"to": pool, "data": TOKEN0}, "latest"])[-40:]
    decimals = int(rpc.call("eth_call", [{"to": sell, "data": DECIMALS}, "latest"]), 16)
    r0, r1 = int(reserves[2:66], 16), int(reserves[66:130], 16)
    r_in, r_out = (r0, r1) if sell == token0 else (r1, r0)
    base_fee = int(rpc.call("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)

    amount_in = int(args.amount * 10**decimals)
    # Gas is paid in ETH; this assumes the sold token is WETH (otherwise convert first).
    result = assess(amount_in, r_in, r_out, args.slippage / 100, base_fee * args.gas)
    unit = 10**decimals
    print(f"Trade size: {result['size_vs_pool']:.4%} of the pool's {sell[:8]} reserve")
    print(f"Most a sandwich could take at {args.slippage}% slippage: "
          f"{result['max_attacker_profit'] / unit:.6f} (attack gas about "
          f"{base_fee * args.gas / 1e18:.6f} ETH)")  # fmt: skip
    if result["safe_slippage"] >= 0.5:
        print("Safe slippage for this trade now: any setting up to 50% (the search limit): "
              "the trade is too small for this pool to be worth sandwiching")  # fmt: skip
    else:
        print(f"Safe slippage for this trade now: {result['safe_slippage']:.3%}")
    if result["worth_attacking"]:
        print("Advice: this trade is worth sandwiching. Lower your slippage to the safe level "
              "or send it privately (e.g. Flashbots Protect / MEV Blocker).")  # fmt: skip
    else:
        print("Advice: at this slippage no sandwich covers its gas; the trade is low risk.")


if __name__ == "__main__":
    main()
