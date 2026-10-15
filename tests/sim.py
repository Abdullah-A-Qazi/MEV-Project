"""A tiny Uniswap V2 pool simulator for building realistic synthetic swaps in tests.

Every Swap it produces has amounts and post-swap reserves that a real pair would emit, so
detector tests exercise the same code paths as real data.
"""

from itertools import count

from detectors.swaps import Swap
from profit.uniswap_v2 import Reserves, apply_swap

WETH = "0x" + "e" * 40
USDC = "0x" + "c" * 40
DAI = "0x" + "d" * 40
ROUTER = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"  # Uniswap V2 Router02 (public)


class Pool:
    def __init__(self, address: str, token0: str, token1: str, reserve0: int, reserve1: int):
        self.address = address
        self.token0, self.token1 = token0, token1
        self.reserves = Reserves(reserve0, reserve1)
        self._log_index = count()

    def swap(
        self,
        tx_index: int,
        amount_in: int,
        token_in: str,
        tx_from: str,
        tx_to: str | None = ROUTER,
        tx_hash: str | None = None,
        block: int = 100,
        log_index: int | None = None,
    ) -> Swap:
        zero_for_one = token_in == self.token0
        self.reserves, out = apply_swap(self.reserves, amount_in, zero_for_one)
        return Swap(
            block_number=block,
            tx_index=tx_index,
            tx_hash=tx_hash or f"0x{tx_index:064x}",
            log_index=next(self._log_index) if log_index is None else log_index,
            pool=self.address,
            dex="uniswap_v2",
            token0=self.token0,
            token1=self.token1,
            amount0_in=amount_in if zero_for_one else 0,
            amount1_in=0 if zero_for_one else amount_in,
            amount0_out=0 if zero_for_one else out,
            amount1_out=out if zero_for_one else 0,
            sender=tx_to or tx_from,
            recipient=tx_from,
            tx_from=tx_from,
            tx_to=tx_to,
            reserves_after=self.reserves,
        )


def addr(n: int) -> str:
    return f"0x{n:040x}"
