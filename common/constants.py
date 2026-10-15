"""On-chain constants: event topics, function selectors and well-known addresses.

Topics and selectors are derived from their canonical signatures with keccak256 so that
each one can be checked against its signature (see tests/test_constants.py) instead of
being trusted as a magic hex string.
"""

from eth_utils import keccak, to_checksum_address


def event_topic(signature: str) -> str:
    """keccak256 of an event signature, as a 0x-prefixed lowercase hex string."""
    return "0x" + keccak(text=signature).hex()


def function_selector(signature: str) -> str:
    """First four bytes of keccak256 of a function signature, as 0x-prefixed hex."""
    return "0x" + keccak(text=signature)[:4].hex()


# --- Event topics -------------------------------------------------------------------------

UNISWAP_V2_SWAP_TOPIC = event_topic("Swap(address,uint256,uint256,uint256,uint256,address)")
UNISWAP_V2_SYNC_TOPIC = event_topic("Sync(uint112,uint112)")
UNISWAP_V3_SWAP_TOPIC = event_topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
ERC20_TRANSFER_TOPIC = event_topic("Transfer(address,address,uint256)")
WETH_DEPOSIT_TOPIC = event_topic("Deposit(address,uint256)")
WETH_WITHDRAWAL_TOPIC = event_topic("Withdrawal(address,uint256)")

# --- Tokens -------------------------------------------------------------------------------

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# --- Uniswap V2 and V2-compatible forks ----------------------------------------------------
# Each factory deploys pairs with CREATE2, so a pair address can be recomputed from
# (factory, token0, token1, init code hash). This lets us confirm that a contract emitting
# V2-style Swap events really is a pair from that factory, and not a look-alike contract.

V2_FACTORIES: dict[str, dict[str, str]] = {
    "uniswap_v2": {
        "factory": "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f",
        "init_code_hash": "0x96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f",
    },
    # Read from the factory's pairCodeHash() on 2026-10-15.
    "sushiswap": {
        "factory": "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac",
        "init_code_hash": "0xe18a34eb0e04b04f7a0ac29a6e80748dca96319b42c54d679cb821dca90c6303",
    },
}

# --- Routers --------------------------------------------------------------------------------
# Public routers are shared by many unrelated users. Two swaps whose transactions both call
# the same router are NOT evidence of the same actor, so the sandwich detector must never
# use a router address as an attacker identity.

UNISWAP_V2_ROUTER_02 = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
SUSHISWAP_ROUTER = "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f"
UNISWAP_UNIVERSAL_ROUTER_V1 = "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b"
UNISWAP_UNIVERSAL_ROUTER_V1_2 = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"
UNISWAP_UNIVERSAL_ROUTER_V2 = "0x66a9893cc07d91d95644aedd05d03f95e1dba8af"
UNISWAP_V3_ROUTER = "0xe592427a0aece92de3edee1f18e0157c05861564"
UNISWAP_V3_ROUTER_02 = "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45"
ONEINCH_V5_ROUTER = "0x1111111254eeb25477b68fb85ed929f73a960582"
ONEINCH_V6_ROUTER = "0x111111125421ca6dc452d289314280a0f8842a65"
ZEROX_EXCHANGE_PROXY = "0xdef1c0ded9bec7f1a1670819833240f027b25eff"
PARASWAP_V5 = "0xdef171fe48cf0115b1d80b88dc8eab59176fee57"
COWSWAP_SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"

PUBLIC_ROUTERS: frozenset[str] = frozenset(
    {
        UNISWAP_V2_ROUTER_02,
        SUSHISWAP_ROUTER,
        UNISWAP_UNIVERSAL_ROUTER_V1,
        UNISWAP_UNIVERSAL_ROUTER_V1_2,
        UNISWAP_UNIVERSAL_ROUTER_V2,
        UNISWAP_V3_ROUTER,
        UNISWAP_V3_ROUTER_02,
        ONEINCH_V5_ROUTER,
        ONEINCH_V6_ROUTER,
        ZEROX_EXCHANGE_PROXY,
        PARASWAP_V5,
        COWSWAP_SETTLEMENT,
    }
)

# --- Uniswap V2 Router02 swap functions ------------------------------------------------------
# "exact_input": the user fixes amountIn and a minimum output (amountOutMin).
# "exact_output": the user fixes amountOut and a maximum input (amountInMax).
# The slippage decoder (RQ3) and the golden tests both rely on this split.

V2_ROUTER_SWAP_FUNCTIONS: dict[str, tuple[str, str]] = {
    function_selector(sig): (sig, kind)
    for sig, kind in [
        ("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)", "exact_input"),
        ("swapExactETHForTokens(uint256,address[],address,uint256)", "exact_input"),
        ("swapExactTokensForETH(uint256,uint256,address[],address,uint256)", "exact_input"),
        ("swapTokensForExactTokens(uint256,uint256,address[],address,uint256)", "exact_output"),
        ("swapETHForExactTokens(uint256,address[],address,uint256)", "exact_output"),
        ("swapTokensForExactETH(uint256,uint256,address[],address,uint256)", "exact_output"),
        (
            "swapExactTokensForTokensSupportingFeeOnTransferTokens"
            "(uint256,uint256,address[],address,uint256)",
            "exact_input",
        ),
        (
            "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)",
            "exact_input",
        ),
        (
            "swapExactTokensForETHSupportingFeeOnTransferTokens"
            "(uint256,uint256,address[],address,uint256)",
            "exact_input",
        ),
    ]
}


def checksum(address: str) -> str:
    """EIP-55 checksummed form, for display only. Internally we keep addresses lowercase."""
    return to_checksum_address(address)
