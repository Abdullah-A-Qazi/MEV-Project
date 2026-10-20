"""Decodes a user's slippage limit from the router call that made a swap (RQ3).

A user protects a trade by setting a limit: for an exact-input swap, the least output they
will accept (amountOutMin); for an exact-output swap, the most input they will pay
(amountInMax). The router reverts the trade if the limit is broken, so the limit is the
most a sandwich can take from them.

Supported routers (single-hop trades only, so the limit applies to exactly one pool):
* Uniswap V2 Router02 and the SushiSwap router: the nine `swap...` functions.
* Uniswap Universal Router: V2_SWAP_EXACT_IN (0x08) and V2_SWAP_EXACT_OUT (0x09) commands
  inside `execute(bytes commands, bytes[] inputs[, uint256 deadline])`.

Multi-hop trades, aggregators and custom routers are not decoded; analysis/rq3.py reports
the share of user swaps decoded.
"""

from dataclasses import dataclass

from eth_abi import decode

from common.constants import (
    SUSHISWAP_ROUTER,
    UNISWAP_UNIVERSAL_ROUTER_V1,
    UNISWAP_UNIVERSAL_ROUTER_V1_2,
    UNISWAP_UNIVERSAL_ROUTER_V2,
    UNISWAP_V2_ROUTER_02,
    V2_ROUTER_SWAP_FUNCTIONS,
    function_selector,
)
from profit.uniswap_v2 import InsufficientLiquidity, Reserves, get_amount_in, get_amount_out

V2_ROUTERS = frozenset({UNISWAP_V2_ROUTER_02, SUSHISWAP_ROUTER})
UNIVERSAL_ROUTERS = frozenset(
    {UNISWAP_UNIVERSAL_ROUTER_V1, UNISWAP_UNIVERSAL_ROUTER_V1_2, UNISWAP_UNIVERSAL_ROUTER_V2}
)
UR_EXECUTE = {
    function_selector("execute(bytes,bytes[],uint256)"): ["bytes", "bytes[]", "uint256"],
    function_selector("execute(bytes,bytes[])"): ["bytes", "bytes[]"],
}
UR_V2_SWAP_EXACT_IN = 0x08
UR_V2_SWAP_EXACT_OUT = 0x09
UR_COMMAND_MASK = 0x3F  # the low bits name the command; 0x80 = "allow revert"
UR_CONTRACT_BALANCE = 1 << 255  # sentinel: "use whatever the router holds"


@dataclass(frozen=True)
class SlippageLimit:
    router: str  # "v2_router" or "universal_router"
    kind: str  # "exact_input" or "exact_output"
    path: tuple[str, ...]
    limit: int  # amountOutMin (exact_input) or amountInMax (exact_output), raw units


def _named_args(signature: str, data: bytes) -> dict[str, object]:
    types = signature[signature.index("(") + 1 : -1].split(",")
    values = decode(types, data)
    # Router02's argument order, by function family.
    if signature.startswith("swapExactETH"):
        names = ["amountOutMin", "path", "to", "deadline"]
    elif signature.startswith("swapETHForExact"):
        names = ["amountOut", "path", "to", "deadline"]
    elif signature.startswith("swapExact"):
        names = ["amountIn", "amountOutMin", "path", "to", "deadline"]
    else:  # swapTokensForExact...
        names = ["amountOut", "amountInMax", "path", "to", "deadline"]
    return dict(zip(names, values, strict=True))


def decode_v2_router(tx_input: str, value: int) -> list[SlippageLimit]:
    selector = tx_input[:10].lower()
    if selector not in V2_ROUTER_SWAP_FUNCTIONS:
        return []
    signature, kind = V2_ROUTER_SWAP_FUNCTIONS[selector]
    args = _named_args(signature, bytes.fromhex(tx_input[10:]))
    path = tuple(a.lower() for a in args["path"])
    if kind == "exact_input":
        limit = int(args["amountOutMin"])
    else:  # swapETHForExactTokens pays at most msg.value
        limit = int(args.get("amountInMax", value))
    return [SlippageLimit("v2_router", kind, path, limit)]


def decode_universal_router(tx_input: str) -> list[SlippageLimit]:
    selector = tx_input[:10].lower()
    if selector not in UR_EXECUTE:
        return []
    decoded = decode(UR_EXECUTE[selector], bytes.fromhex(tx_input[10:]))
    commands, inputs = decoded[0], decoded[1]
    limits = []
    for command, data in zip(commands, inputs, strict=False):
        kind_code = command & UR_COMMAND_MASK
        if kind_code not in (UR_V2_SWAP_EXACT_IN, UR_V2_SWAP_EXACT_OUT):
            continue
        _recipient, _amount, limit, path, _payer = decode(
            ["address", "uint256", "uint256", "address[]", "bool"], data
        )
        kind = "exact_input" if kind_code == UR_V2_SWAP_EXACT_IN else "exact_output"
        limits.append(
            SlippageLimit("universal_router", kind, tuple(p.lower() for p in path), int(limit))
        )
    return limits


def decode_limits(tx_to: str | None, tx_input: str, value: int) -> list[SlippageLimit]:
    """All V2 swap limits in a transaction's call (empty if the router is not supported)."""
    if not tx_to or len(tx_input) < 10:
        return []
    try:
        if tx_to in V2_ROUTERS:
            return decode_v2_router(tx_input, value)
        if tx_to in UNIVERSAL_ROUTERS:
            return decode_universal_router(tx_input)
    except Exception:  # malformed calldata: treat as undecodable rather than crash
        return []
    return []


def slippage_tolerance(
    limit: SlippageLimit, amount_in: int, amount_out: int, reserves_in: int, reserves_out: int
) -> float | None:
    """The user's tolerance as a fraction of the expected trade, on the given reserves.

    exact_input:  1 - amountOutMin / expected output for the actual input
    exact_output: amountInMax / expected input for the actual output - 1
    None if the expected amount cannot be computed. Negative values mean the limit was tighter
    than the price on that state (the state the user saw differed).
    """
    try:
        if limit.kind == "exact_input":
            expected = get_amount_out(amount_in, reserves_in, reserves_out)
            return 1 - limit.limit / expected if expected else None
        expected = get_amount_in(amount_out, reserves_in, reserves_out)
        return limit.limit / expected - 1 if expected else None
    except (InsufficientLiquidity, ValueError):
        return None


def reserves_in_out(reserves: Reserves, zero_for_one: bool) -> tuple[int, int]:
    if zero_for_one:
        return reserves.reserve0, reserves.reserve1
    return reserves.reserve1, reserves.reserve0
