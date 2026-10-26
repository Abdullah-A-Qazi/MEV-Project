"""Pool metadata: which tokens a pool holds, and which DEX deployed it (Uniswap V2 / V3).

Anyone can deploy a contract that emits a Uniswap-V2-shaped Swap event, so a matching
topic alone does not prove the event came from a real Uniswap pair. We read token0() and
token1() from the contract (two eth_calls, cached), then recompute the CREATE2 address
each known factory would give that token pair. Only if it equals the contract's address
is the pool labelled with that DEX; otherwise it is "unknown" and excluded by default.

V3 pools are verified the same way, with the fee tier (fee(), a third call) in the salt.
"""

from dataclasses import dataclass

from eth_utils import keccak

from collector.rpc import RpcClient, RpcError
from common.constants import V2_FACTORIES, V3_FACTORIES, function_selector

TOKEN0 = function_selector("token0()")
TOKEN1 = function_selector("token1()")
FEE = function_selector("fee()")


@dataclass(frozen=True)
class PoolInfo:
    address: str
    token0: str | None
    token1: str | None
    dex: str  # "uniswap_v2", "sushiswap", "uniswap_v3" or "unknown"
    fee: int | None = None  # V3 fee tier in hundredths of a basis point (500 = 0.05%)


def create2_pair_address(factory: str, init_code_hash: str, token0: str, token1: str) -> str:
    """Address of the pair `factory` deploys for (token0, token1), with token0 < token1."""
    salt = keccak(bytes.fromhex(token0[2:]) + bytes.fromhex(token1[2:]))
    digest = keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + bytes.fromhex(init_code_hash[2:]))
    return "0x" + digest[12:].hex()


def identify_dex(address: str, token0: str, token1: str) -> str:
    for dex, spec in V2_FACTORIES.items():
        expected = create2_pair_address(spec["factory"], spec["init_code_hash"], token0, token1)
        if expected == address.lower():
            return dex
    return "unknown"


def create2_v3_pool_address(
    factory: str, init_code_hash: str, token0: str, token1: str, fee: int
) -> str:
    """Address of the V3 pool `factory` deploys for (token0, token1, fee)."""
    salt = keccak(
        bytes.fromhex(token0[2:].rjust(64, "0"))
        + bytes.fromhex(token1[2:].rjust(64, "0"))
        + fee.to_bytes(32, "big")
    )
    digest = keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + bytes.fromhex(init_code_hash[2:]))
    return "0x" + digest[12:].hex()


def identify_v3_dex(address: str, token0: str, token1: str, fee: int) -> str:
    for dex, spec in V3_FACTORIES.items():
        expected = create2_v3_pool_address(
            spec["factory"], spec["init_code_hash"], token0, token1, fee
        )
        if expected == address.lower():
            return dex
    return "unknown"


def _read_address(rpc: RpcClient, contract: str, selector: str, block: int) -> str | None:
    try:
        result = rpc.call("eth_call", [{"to": contract, "data": selector}, hex(block)])
    except RpcError:
        return None  # not a pair (call reverted or function missing)
    if not result or len(result) < 66:
        return None
    return "0x" + result[-40:].lower()


def fetch_pool_info(rpc: RpcClient, address: str, block: int) -> PoolInfo:
    """Read and verify a pool's metadata at `block` (any block at which the pool exists)."""
    address = address.lower()
    token0 = _read_address(rpc, address, TOKEN0, block)
    token1 = _read_address(rpc, address, TOKEN1, block)
    if token0 is None or token1 is None:
        return PoolInfo(address, token0, token1, "unknown")
    return PoolInfo(address, token0, token1, identify_dex(address, token0, token1))


def fetch_v3_pool_info(rpc: RpcClient, address: str, block: int) -> PoolInfo:
    """Read and verify a V3 pool's tokens and fee tier at `block`."""
    address = address.lower()
    token0 = _read_address(rpc, address, TOKEN0, block)
    token1 = _read_address(rpc, address, TOKEN1, block)
    try:
        raw = rpc.call("eth_call", [{"to": address, "data": FEE}, hex(block)])
        fee = int(raw, 16) if raw and len(raw) >= 66 else None
    except RpcError:
        fee = None
    if token0 is None or token1 is None or fee is None:
        return PoolInfo(address, token0, token1, "unknown", fee)
    return PoolInfo(address, token0, token1, identify_v3_dex(address, token0, token1, fee), fee)
