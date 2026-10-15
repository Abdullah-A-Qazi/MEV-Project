"""Constants, pool verification, block flattening and swap decoding."""

import json
from pathlib import Path

from collector.pipeline import normalise_block
from collector.pools import PoolInfo, create2_pair_address, identify_dex
from common.constants import (
    ERC20_TRANSFER_TOPIC,
    UNISWAP_V2_SWAP_TOPIC,
    UNISWAP_V2_SYNC_TOPIC,
    UNISWAP_V3_SWAP_TOPIC,
    V2_FACTORIES,
    V2_ROUTER_SWAP_FUNCTIONS,
)
from detectors.swaps import decode_v2_swaps

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "uniswap_v2_golden.json").read_text())


def test_event_topics_match_well_known_values():
    assert UNISWAP_V2_SWAP_TOPIC == (
        "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
    )
    assert UNISWAP_V2_SYNC_TOPIC == (
        "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
    )
    assert UNISWAP_V3_SWAP_TOPIC == (
        "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
    )
    assert ERC20_TRANSFER_TOPIC == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


def test_router_selectors():
    assert V2_ROUTER_SWAP_FUNCTIONS["0x38ed1739"][0].startswith("swapExactTokensForTokens(")
    assert V2_ROUTER_SWAP_FUNCTIONS["0x7ff36ab5"][0].startswith("swapExactETHForTokens(")
    assert V2_ROUTER_SWAP_FUNCTIONS["0x8803dbee"][1] == "exact_output"


def test_create2_reproduces_real_pair_addresses():
    """Each fixture pool's address is recomputed from its factory and tokens."""
    for address, meta in GOLDEN["pools"].items():
        spec = V2_FACTORIES[meta["dex"]]
        assert (
            create2_pair_address(
                spec["factory"], spec["init_code_hash"], meta["token0"], meta["token1"]
            )
            == address
        )
        assert identify_dex(address, meta["token0"], meta["token1"]) == meta["dex"]


def test_look_alike_pool_is_unknown():
    meta = GOLDEN["pools"]["0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"]
    assert identify_dex("0x" + "12" * 20, meta["token0"], meta["token1"]) == "unknown"


def _word(n: int) -> str:
    return f"{n:064x}"


def _topic_addr(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def test_decode_v2_swap_pairs_with_preceding_sync():
    pool, tok0, tok1 = "0x" + "11" * 20, "0x" + "aa" * 20, "0x" + "bb" * 20
    tx = "0x" + "cd" * 32
    logs = [
        {
            "block_number": 7,
            "tx_index": 0,
            "tx_hash": tx,
            "log_index": 3,
            "address": pool,
            "topic0": UNISWAP_V2_SYNC_TOPIC,
            "topic1": None,
            "topic2": None,
            "topic3": None,
            "data": "0x" + _word(1_100) + _word(910),
        },
        {
            "block_number": 7,
            "tx_index": 0,
            "tx_hash": tx,
            "log_index": 4,
            "address": pool,
            "topic0": UNISWAP_V2_SWAP_TOPIC,
            "topic1": _topic_addr("0x" + "22" * 20),
            "topic2": _topic_addr("0x" + "33" * 20),
            "topic3": None,
            "data": "0x" + _word(100) + _word(0) + _word(0) + _word(90),
        },
        {
            "block_number": 7,
            "tx_index": 1,
            "tx_hash": "0x" + "ef" * 32,
            "log_index": 5,
            "address": "0x" + "99" * 20,
            "topic0": UNISWAP_V2_SWAP_TOPIC,
            "topic1": _topic_addr("0x" + "22" * 20),
            "topic2": _topic_addr("0x" + "33" * 20),
            "topic3": None,
            "data": "0x" + _word(1) + _word(0) + _word(0) + _word(1),
        },
    ]
    pools = {
        pool: PoolInfo(pool, tok0, tok1, "uniswap_v2"),
        "0x" + "99" * 20: PoolInfo("0x" + "99" * 20, tok0, tok1, "unknown"),
    }
    txs = {tx: {"from": "0x" + "44" * 20, "to": "0x" + "55" * 20}}
    [swap] = decode_v2_swaps(logs, txs, pools)  # the unverified pool is dropped
    assert swap.zero_for_one and swap.is_simple
    assert (swap.token_in, swap.token_out) == (tok0, tok1)
    assert (swap.amount_in, swap.amount_out) == (100, 90)
    assert swap.sender == "0x" + "22" * 20 and swap.recipient == "0x" + "33" * 20
    assert swap.reserves_after.reserve0 == 1_100
    assert (swap.reserves_before.reserve0, swap.reserves_before.reserve1) == (1_000, 1_000)
    assert swap.tx_from == "0x" + "44" * 20


def test_normalise_block_flattens_everything():
    block = {
        "number": "0x10",
        "hash": "0xAB",
        "timestamp": "0x5",
        "miner": "0xFEE",
        "baseFeePerGas": "0x3b9aca00",
        "gasUsed": "0x5208",
        "gasLimit": "0x1c9c380",
        "transactions": [
            {
                "hash": "0xT1",
                "transactionIndex": "0x0",
                "from": "0xA",
                "to": "0xB",
                "value": "0xde0b6b3a7640000",
                "nonce": "0x1",
                "type": "0x2",
                "gas": "0x5208",
                "gasPrice": "0x1",
                "maxFeePerGas": "0x2",
                "maxPriorityFeePerGas": "0x1",
                "input": "0x",
            }
        ],
    }
    receipts = [
        {
            "transactionHash": "0xT1",
            "transactionIndex": "0x0",
            "status": "0x1",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x1",
            "contractAddress": None,
            "logs": [
                {
                    "transactionHash": "0xT1",
                    "transactionIndex": "0x0",
                    "logIndex": "0x0",
                    "address": "0xC",
                    "topics": ["0xT0"],
                    "data": "0x",
                }
            ],
        }
    ]
    rows = normalise_block(block, receipts)
    assert rows["blocks"][0]["block_number"] == 16
    assert rows["blocks"][0]["fee_recipient"] == "0xfee"
    assert rows["transactions"][0]["value"] == str(10**18)  # uint256 kept exact as text
    assert rows["receipts"][0]["gas_used"] == 21_000
    assert rows["logs"][0]["topic0"] == "0xt0" and rows["logs"][0]["topic1"] is None
