"""Decodes a user's slippage limit from the router call that made a swap (RQ3).

A user protects a trade by setting a limit: for an exact-input swap, the least output they
will accept (amountOutMin); for an exact-output swap, the most input they will pay
(amountInMax). The router reverts the trade if the limit is broken, so the limit is the
most a sandwich can take from them.

Supported routers (single-hop trades only, so the limit applies to exactly one pool):
* Uniswap V2 Router02 and the SushiSwap router: the nine `swap...` functions.
* Uniswap Universal Router: V2_SWAP_EXACT_IN/OUT (0x08/0x09) and V3_SWAP_EXACT_IN/OUT
  (0x00/0x01) commands inside `execute(bytes commands, bytes[] inputs[, uint256 deadline])`.
* Uniswap V3 SwapRouter and SwapRouter02: exactInput(Single) / exactOutput(Single), also
  inside `multicall`, and SwapRouter02's two V2 functions.

V3 paths are packed bytes: token (20) | fee (3) | token (20) ...; exact-output paths run
from the output token back to the input token.

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
    UNISWAP_V3_ROUTER,
    UNISWAP_V3_ROUTER_02,
    V2_ROUTER_SWAP_FUNCTIONS,
    function_selector,
)
from profit.uniswap_v2 import InsufficientLiquidity, Reserves, get_amount_in, get_amount_out
from profit.uniswap_v3 import V3State, swap_exact_input, swap_exact_output

V2_ROUTERS = frozenset({UNISWAP_V2_ROUTER_02, SUSHISWAP_ROUTER})
UNIVERSAL_ROUTERS = frozenset(
    {UNISWAP_UNIVERSAL_ROUTER_V1, UNISWAP_UNIVERSAL_ROUTER_V1_2, UNISWAP_UNIVERSAL_ROUTER_V2}
)
UR_EXECUTE = {
    function_selector("execute(bytes,bytes[],uint256)"): ["bytes", "bytes[]", "uint256"],
    function_selector("execute(bytes,bytes[])"): ["bytes", "bytes[]"],
}
UR_V3_SWAP_EXACT_IN = 0x00
UR_V3_SWAP_EXACT_OUT = 0x01
UR_V2_SWAP_EXACT_IN = 0x08
UR_V2_SWAP_EXACT_OUT = 0x09
V3_ROUTERS = frozenset({UNISWAP_V3_ROUTER, UNISWAP_V3_ROUTER_02})

# SwapRouter (v1) structs carry a deadline; SwapRouter02 structs do not.
_SINGLE = "(address,address,uint24,address,uint256,uint256,uint256,uint160)"
_SINGLE_02 = "(address,address,uint24,address,uint256,uint256,uint160)"
V3_ROUTER_FUNCTIONS: dict[str, tuple[str, list[str]]] = {
    function_selector(f"exactInputSingle({_SINGLE})"): ("in_single_v1", [_SINGLE]),
    function_selector(f"exactOutputSingle({_SINGLE})"): ("out_single_v1", [_SINGLE]),
    function_selector("exactInput((bytes,address,uint256,uint256,uint256))"):
        ("in_path_v1", ["(bytes,address,uint256,uint256,uint256)"]),
    function_selector("exactOutput((bytes,address,uint256,uint256,uint256))"):
        ("out_path_v1", ["(bytes,address,uint256,uint256,uint256)"]),
    function_selector(f"exactInputSingle({_SINGLE_02})"): ("in_single_02", [_SINGLE_02]),
    function_selector(f"exactOutputSingle({_SINGLE_02})"): ("out_single_02", [_SINGLE_02]),
    function_selector("exactInput((bytes,address,uint256,uint256))"):
        ("in_path_02", ["(bytes,address,uint256,uint256)"]),
    function_selector("exactOutput((bytes,address,uint256,uint256))"):
        ("out_path_02", ["(bytes,address,uint256,uint256)"]),
    function_selector("swapExactTokensForTokens(uint256,uint256,address[],address)"):
        ("v2_in_02", ["uint256", "uint256", "address[]", "address"]),
    function_selector("swapTokensForExactTokens(uint256,uint256,address[],address)"):
        ("v2_out_02", ["uint256", "uint256", "address[]", "address"]),
}  # fmt: skip
MULTICALL = {
    function_selector("multicall(bytes[])"): ["bytes[]"],
    function_selector("multicall(uint256,bytes[])"): ["uint256", "bytes[]"],
    function_selector("multicall(bytes32,bytes[])"): ["bytes32", "bytes[]"],
}
UR_COMMAND_MASK = 0x3F  # the low bits name the command; 0x80 = "allow revert"
UR_CONTRACT_BALANCE = 1 << 255  # sentinel: "use whatever the router holds"


@dataclass(frozen=True)
class SlippageLimit:
    router: str  # "v2_router", "universal_router" or "v3_router"
    kind: str  # "exact_input" or "exact_output"
    path: tuple[str, ...]  # input token first, output token last
    limit: int  # amountOutMin (exact_input) or amountInMax (exact_output), raw units
    protocol: str = "v2"  # which pool type the trade goes through: "v2" or "v3"
    fees: tuple[int, ...] = ()  # V3 fee tier per hop


def decode_v3_path(path: bytes, exact_output: bool) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """(tokens input-first, fees) from a packed V3 path."""
    tokens, fees, i = ["0x" + path[0:20].hex()], [], 20
    while i + 23 <= len(path):
        fees.append(int.from_bytes(path[i : i + 3], "big"))
        tokens.append("0x" + path[i + 3 : i + 23].hex())
        i += 23
    if exact_output:  # exact-output paths are stored output-first
        tokens.reverse()
        fees.reverse()
    return tuple(tokens), tuple(fees)


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
        if kind_code in (UR_V2_SWAP_EXACT_IN, UR_V2_SWAP_EXACT_OUT):
            _recipient, _amount, limit, path, _payer = decode(
                ["address", "uint256", "uint256", "address[]", "bool"], data
            )
            kind = "exact_input" if kind_code == UR_V2_SWAP_EXACT_IN else "exact_output"
            limits.append(
                SlippageLimit("universal_router", kind, tuple(p.lower() for p in path), int(limit))
            )
        elif kind_code in (UR_V3_SWAP_EXACT_IN, UR_V3_SWAP_EXACT_OUT):
            _recipient, _amount, limit, path, _payer = decode(
                ["address", "uint256", "uint256", "bytes", "bool"], data
            )
            exact_out = kind_code == UR_V3_SWAP_EXACT_OUT
            tokens, fees = decode_v3_path(path, exact_out)
            kind = "exact_output" if exact_out else "exact_input"
            limits.append(SlippageLimit("universal_router", kind, tokens, int(limit), "v3", fees))
    return limits


def decode_v3_router(tx_input: str) -> list[SlippageLimit]:
    """Limits from a SwapRouter / SwapRouter02 call, unwrapping multicall."""
    selector, data = tx_input[:10].lower(), bytes.fromhex(tx_input[10:])
    if selector in MULTICALL:
        calls = decode(MULTICALL[selector], data)[-1]
        return [lim for call in calls for lim in decode_v3_router("0x" + call.hex())]
    if selector not in V3_ROUTER_FUNCTIONS:
        return []
    name, types = V3_ROUTER_FUNCTIONS[selector]
    args = decode(types, data)
    if name.startswith("v2_"):
        _amount, limit, path, _to = args
        kind = "exact_input" if name == "v2_in_02" else "exact_output"
        return [SlippageLimit("v3_router", kind, tuple(p.lower() for p in path), int(limit))]
    params = args[0]
    exact_out = name.startswith("out_")
    kind = "exact_output" if exact_out else "exact_input"
    if "single" in name:
        token_in, token_out, fee = params[0].lower(), params[1].lower(), int(params[2])
        # v1: (in, out, fee, recipient, deadline, amount, limit, sqrtLimit); 02: no deadline
        limit = params[6] if name.endswith("v1") else params[5]
        return [SlippageLimit("v3_router", kind, (token_in, token_out), int(limit), "v3", (fee,))]
    tokens, fees = decode_v3_path(params[0], exact_out)
    limit = params[4] if name.endswith("v1") else params[3]
    return [SlippageLimit("v3_router", kind, tokens, int(limit), "v3", fees)]


def decode_limits(tx_to: str | None, tx_input: str, value: int) -> list[SlippageLimit]:
    """All swap limits in a transaction's call (empty if the router is not supported)."""
    if not tx_to or len(tx_input) < 10:
        return []
    try:
        if tx_to in V2_ROUTERS:
            return decode_v2_router(tx_input, value)
        if tx_to in UNIVERSAL_ROUTERS:
            return decode_universal_router(tx_input)
        if tx_to in V3_ROUTERS:
            return decode_v3_router(tx_input)
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


def slippage_tolerance_v3(
    limit: SlippageLimit,
    amount_in: int,
    amount_out: int,
    state: V3State,
    zero_for_one: bool,
    fee: int,
) -> float | None:
    """As slippage_tolerance, priced with V3 single-range maths on `state`."""
    try:
        if limit.kind == "exact_input":
            _, expected = swap_exact_input(state, amount_in, zero_for_one, fee)
            return 1 - limit.limit / expected if expected else None
        _, expected = swap_exact_output(state, amount_out, zero_for_one, fee)
        return limit.limit / expected - 1 if expected else None
    except ValueError:
        return None


def reserves_in_out(reserves: Reserves, zero_for_one: bool) -> tuple[int, int]:
    if zero_for_one:
        return reserves.reserve0, reserves.reserve1
    return reserves.reserve1, reserves.reserve0
