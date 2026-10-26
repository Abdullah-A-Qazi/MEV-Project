"""One row per ordinary user swap, with everything RQ3 compares (python -m mitigation.dataset).

Population: every Uniswap V2 / SushiSwap swap in the sample that is not a leg of a detected
arbitrage or sandwich, i.e. role "other" (not attacked) or "sandwich_victim" (attacked).

Columns:
  sandwiched        the swap is a detected sandwich victim
  public            its transaction was seen in the public mempool before its block
  decoded           a single-hop router limit was found (mitigation/slippage.py)
  kind, tolerance   exact_input/exact_output; slippage tolerance at the pre-block state
  size_ratio        amount in / pool's input-side reserve before the block
  input_is_weth     the user paid in WETH/ETH (needed for the safe-threshold analysis)
  loss_eth          victim loss valued in ETH at pre-block prices (victims only)

Matching a limit to a swap: among the transaction's swaps on the limit's token pair, the
LAST one. Tax tokens sell their collected tax in the same pool inside the user's
transaction, just before the user's swap; with this rule only 3 of about 61,000 decoded
swaps break their own limit, which the router makes impossible for a correct match.
"""

import pandas as pd

from collector.cache import RpcCache
from collector.credits import CreditLedger
from collector.rpc import RpcClient
from common.config import load_config
from common.constants import WETH
from common.nulls import is_missing
from detectors.load import build_market, load_swaps
from detectors.swaps import Swap
from mitigation.slippage import decode_limits, slippage_tolerance, slippage_tolerance_v3
from profit.pricing import EthPricer


def _limit_matches(lim, swap: Swap, fees: dict[str, int]) -> bool:
    if (swap.token_in, swap.token_out) != lim.path or not swap.is_simple:
        return False
    if lim.protocol == "v3":
        return swap.is_v3 and lim.fees == (fees.get(swap.pool),)
    return not swap.is_v3


def user_swap_rows(
    swaps: list[Swap],
    roles: dict[tuple[str, int], str],
    txs: pd.DataFrame,
    public: dict[str, bool],
    losses: dict[tuple[str, int], tuple[str, int]],
    pricer: EthPricer,
    market,
) -> list[dict]:
    by_tx: dict[str, list[Swap]] = {}
    for swap in swaps:
        if roles.get((swap.tx_hash, swap.log_index), "other") in ("other", "sandwich_victim"):
            by_tx.setdefault(swap.tx_hash, []).append(swap)

    rows = []
    for tx_hash, tx_swaps in by_tx.items():
        tx = txs.loc[tx_hash]
        to = None if is_missing(tx["to"]) else tx["to"]
        limits = [lim for lim in decode_limits(to, tx["input"], int(tx["value"]))
                  if len(lim.path) == 2]  # fmt: skip
        matched: dict[tuple[str, int], object] = {}
        for lim in limits:
            same_pair = [s for s in tx_swaps if _limit_matches(lim, s, market.fees)]
            if same_pair:
                last = max(same_pair, key=lambda s: s.log_index)
                matched[(last.tx_hash, last.log_index)] = lim
        for swap in tx_swaps:
            if not swap.is_simple:
                continue
            key = (swap.tx_hash, swap.log_index)
            state = market.pre_block(swap.block_number, swap.pool)
            if swap.is_v3 and state is not None:
                # Single-range check: the pool's real swaps up to this one kept the start's L.
                path = [s for s in market.v3._by_pool.get((swap.block_number, swap.pool), [])
                        if s.position <= swap.position]  # fmt: skip
                if not market.trusted_v3_path(state, path):
                    state = None
            r_in, r_out = market.depth(state, swap.zero_for_one) if state is not None else (0, 0)
            lim = matched.get(key)
            tolerance = None
            if lim is not None and state is not None:
                if swap.is_v3:
                    tolerance = slippage_tolerance_v3(
                        lim, swap.amount_in, swap.amount_out, state, swap.zero_for_one,
                        market.fees[swap.pool],
                    )  # fmt: skip
                else:
                    tolerance = slippage_tolerance(
                        lim, swap.amount_in, swap.amount_out, r_in, r_out
                    )
            loss = losses.get(key)
            rows.append({
                "tx_hash": swap.tx_hash,
                "log_index": swap.log_index,
                "block_number": swap.block_number,
                "pool": swap.pool,
                "protocol": "v3" if swap.is_v3 else "v2",
                "sandwiched": roles.get(key) == "sandwich_victim",
                "public": public.get(swap.tx_hash, False),
                "decoded": lim is not None,
                "router": None if lim is None else lim.router,
                "kind": None if lim is None else lim.kind,
                "tolerance": tolerance,
                "size_ratio": swap.amount_in / r_in if r_in else None,
                "amount_in": str(swap.amount_in),
                "reserve_in": str(r_in),
                "reserve_out": str(r_out),
                "input_is_weth": swap.token_in == WETH,
                "amount_in_eth": pricer.wei_value(swap.token_in, swap.amount_in, swap.block_number),
                "loss_eth": None if loss is None
                else pricer.wei_value(loss[0], loss[1], swap.block_number),
            })  # fmt: skip
    return rows


def main() -> None:
    config = load_config()
    results = config.data_dir / "results"
    rpc = RpcClient([], RpcCache(config.cache_path), CreditLedger(config.ledger_path), offline=True)
    swaps, logs, txs, pools = load_swaps(config, rpc)
    # No RPC: V3 pre-block states are worked back from each pool's first swap in the block.
    market = build_market(swaps, logs, pools, None)
    role_table = pd.read_parquet(
        results / "swaps.parquet", columns=["tx_hash", "log_index", "role"]
    )
    roles = {(h, int(i)): r for h, i, r in role_table.itertuples(index=False)}
    vis = pd.read_parquet(results / "visibility.parquet", columns=["tx_hash", "seen_before"])
    public = dict(zip(vis["tx_hash"], vis["seen_before"].astype(bool), strict=True))
    sandwiches = pd.read_parquet(results / "sandwiches.parquet").dropna(subset=["victim_loss"])
    losses = {
        (h, int(i)): (tok, int(loss))
        for h, i, tok, loss in sandwiches[
            ["victim_tx", "victim_log_index", "victim_token_out", "victim_loss"]
        ].itertuples(index=False)
    }
    txs = txs.set_index("tx_hash")[["to", "input", "value"]]
    pricer = EthPricer(swaps, market=market)  # pre-block prices from the sample, no network
    frame = pd.DataFrame(user_swap_rows(swaps, roles, txs, public, losses, pricer, market))
    for col in ("amount_in_eth", "loss_eth"):
        frame[col] = frame[col].astype("float64") / 1e18
    frame.to_parquet(results / "user_swaps.parquet", index=False)
    print(frame.groupby(["decoded", "public"])["sandwiched"].agg(["size", "mean"]).to_string())


if __name__ == "__main__":
    main()
