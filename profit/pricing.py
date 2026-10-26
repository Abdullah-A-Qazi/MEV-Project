"""Values token amounts in ETH (wei) at the state just before a block.

Price source, in order:
  1. The token's WETH pair among the V2 swaps already decoded for that block. The reserves
     before the pair's first swap in the block are the pre-block state, so this is free.
  2. Otherwise, getReserves() on the token's Uniswap V2 (then SushiSwap) WETH pair at
     block - 1, one cached eth_call. The pair address is computed with CREATE2, so no
     factory lookup is needed.

Pre-block prices are used on purpose: the MEV transactions themselves move the price inside
the block, so a price taken after them would be biased by the very trades being measured.
A pair with less than `min_weth_reserve` is ignored as too thin to give a reliable price.
"""

from collections.abc import Iterable

from collector.credits import BudgetExhausted
from collector.pools import create2_pair_address
from collector.rpc import OfflineMiss, RpcClient, RpcError
from common.constants import V2_FACTORIES, WETH, function_selector
from detectors.swaps import Swap
from profit.uniswap_v2 import Reserves

GET_RESERVES = function_selector("getReserves()")
DEFAULT_MIN_WETH_RESERVE = 10 * 10**18


class EthPricer:
    def __init__(
        self,
        swaps: Iterable[Swap] = (),
        rpc: RpcClient | None = None,
        min_weth_reserve: int = DEFAULT_MIN_WETH_RESERVE,
        market=None,
    ):
        """`market` (profit.market.Market) adds Uniswap V3 WETH pools as a last price source."""
        self.rpc = rpc
        self.market = market
        self.min_weth_reserve = min_weth_reserve
        self.lookup_failures = 0  # chain lookups that failed for network reasons

        # Earliest swap per (block, pool): its pre-swap reserves are the pre-block state.
        earliest: dict[tuple[int, str], Swap] = {}
        for swap in swaps:
            key = (swap.block_number, swap.pool)
            if key not in earliest or swap.position < earliest[key].position:
                earliest[key] = swap

        # Per (block, token): V3 WETH pools that traded in that block.
        self._v3_pools: dict[tuple[int, str], list[str]] = {}
        for swap in earliest.values():
            if swap.is_v3 and WETH in (swap.token0, swap.token1):
                token = swap.token1 if swap.token0 == WETH else swap.token0
                self._v3_pools.setdefault((swap.block_number, token), []).append(swap.pool)

        # Per (block, token): (token_reserve, weth_reserve) of the deepest WETH pool.
        self._from_swaps: dict[tuple[int, str], tuple[int, int]] = {}
        for swap in earliest.values():
            if WETH not in (swap.token0, swap.token1) or swap.reserves_before is None:
                continue
            before = swap.reserves_before
            if swap.token0 == WETH:
                token, reserves = swap.token1, (before.reserve1, before.reserve0)
            else:
                token, reserves = swap.token0, (before.reserve0, before.reserve1)
            key = (swap.block_number, token)
            if key not in self._from_swaps or reserves[1] > self._from_swaps[key][1]:
                self._from_swaps[key] = reserves

    def _from_chain(self, token: str, block: int) -> tuple[int, int] | None:
        if self.rpc is None:
            return None
        token0, token1 = sorted([token, WETH])
        for spec in V2_FACTORIES.values():
            pair = create2_pair_address(spec["factory"], spec["init_code_hash"], token0, token1)
            try:
                result = self.rpc.call(
                    "eth_call", [{"to": pair, "data": GET_RESERVES}, hex(block - 1)]
                )
            except RpcError:
                continue
            except (ConnectionError, BudgetExhausted, OfflineMiss):
                # Network trouble, or offline and never fetched: leave the token unpriced
                # rather than abort the run. Successful lookups are cached, so an offline
                # rebuild reproduces every price an online run found.
                self.lookup_failures += 1
                return None
            if not result or len(result) < 2 + 64 * 2:
                continue  # no contract at that address: the pair does not exist
            r0, r1 = int(result[2:66], 16), int(result[66:130], 16)
            reserves = Reserves(r0, r1)
            pair_reserves = (
                (reserves.reserve0, reserves.reserve1)
                if token0 == token
                else (reserves.reserve1, reserves.reserve0)
            )
            if pair_reserves[1] >= self.min_weth_reserve:
                return pair_reserves
        return None

    def _from_v3(self, token: str, block: int) -> tuple[int, int] | None:
        """(token, WETH) virtual reserves of the deepest V3 WETH pool, before the block."""
        if self.market is None:
            return None
        best = None
        for pool in self._v3_pools.get((block, token), []):
            state = self.market.pre_block(block, pool)
            if state is None:
                continue
            x, y = self.market.depth(state, zero_for_one=True)  # (token0, token1) virtual
            reserves = (x, y) if token < WETH else (y, x)  # token0 is the smaller address
            if reserves[1] >= self.min_weth_reserve and (best is None or reserves[1] > best[1]):
                best = reserves
        return best

    def wei_value(self, token: str, amount: int, block: int) -> int | None:
        """Value of `amount` raw units of `token` in wei, at pre-block mid price."""
        token = token.lower()
        if token == WETH or amount == 0:
            return amount
        reserves = self._from_swaps.get((block, token))
        if reserves is None or reserves[1] < self.min_weth_reserve:
            reserves = self._from_chain(token, block)
        if reserves is None:
            reserves = self._from_v3(token, block)
        if reserves is None or reserves[0] == 0:
            return None
        token_reserve, weth_reserve = reserves
        # Floor division toward zero for negative amounts too, so signs stay symmetric.
        value = abs(amount) * weth_reserve // token_reserve
        return value if amount > 0 else -value
