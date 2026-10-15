"""Unit tests for realised-profit accounting and ETH pricing on hand-built cases."""

from common.constants import ERC20_TRANSFER_TOPIC, WETH
from profit.pricing import EthPricer
from profit.realised import TxData, account, token_transfers, trace_value_transfers
from tests.sim import USDC, Pool, addr

BOT, EOA, POOL, BUILDER, OTHER = addr(0xB07), addr(0xE0A), addr(0x9001), addr(0xB11D), addr(0x1)
GWEI, E18 = 10**9, 10**18


def transfer(token, src, dst, amount, topic3=None):
    pad = lambda a: "0x" + "0" * 24 + a[2:]  # noqa: E731
    return {
        "address": token,
        "topic0": ERC20_TRANSFER_TOPIC,
        "topic1": pad(src),
        "topic2": pad(dst),
        "topic3": topic3,
        "data": "0x" + f"{amount:064x}",
    }


def tx(logs=(), value=0, to=BOT, sender=EOA, gas_used=100_000, price=30 * GWEI, status=1):
    return TxData("0x" + "aa" * 32, sender, to, value, gas_used, price, tuple(logs), status)


def test_token_transfers_skip_nfts_and_handle_nan_topics():
    rows = [
        transfer(USDC, POOL, BOT, 5),
        transfer(USDC, POOL, BOT, 5, topic3=float("nan")),  # pandas "missing" -> ERC-20
        {**transfer(USDC, POOL, BOT, 0, topic3="0x" + "0" * 63 + "7"), "data": "0x"},  # ERC-721
    ]
    assert len(token_transfers(rows)) == 2


def test_arbitrage_profit_after_gas():
    # Bot spends 10 WETH, receives 10.5 WETH; pays 100k gas at 30 gwei (base fee 20 gwei).
    result = account(
        [tx([transfer(WETH, BOT, POOL, 10 * E18), transfer(WETH, POOL, BOT, 105 * E18 // 10)])],
        actors=[BOT, EOA],
        fee_recipient=BUILDER,
        base_fee=20 * GWEI,
    )
    assert result.eth_flow == E18 // 2
    assert result.gas_fee == 100_000 * 30 * GWEI
    assert result.base_fee_burnt == 100_000 * 20 * GWEI
    assert result.priority_fee == 100_000 * 10 * GWEI
    assert result.token_flows == {}
    assert not result.traced


def test_transfers_inside_the_searcher_do_not_count():
    result = account([tx([transfer(USDC, EOA, BOT, 7)], value=E18)], [BOT, EOA], BUILDER, 0)
    assert result.eth_flow == 0 and result.token_flows == {}


def test_traced_coinbase_transfer_is_found_and_reverted_frames_ignored():
    trace = {
        "type": "CALL", "from": EOA, "to": BOT, "value": "0x0",
        "calls": [
            {"type": "CALL", "from": BOT, "to": BUILDER, "value": hex(E18 // 10)},
            {"type": "DELEGATECALL", "from": BOT, "to": OTHER, "value": hex(E18)},  # no transfer
            {"type": "CALL", "from": BOT, "to": OTHER, "value": hex(E18), "error": "reverted",
             "calls": [{"type": "CALL", "from": OTHER, "to": BOT, "value": hex(E18)}]},
        ],
    }  # fmt: skip
    assert trace_value_transfers(trace) == [(BOT, BUILDER, E18 // 10)]
    t = tx()
    result = account([t], [BOT, EOA], BUILDER, 0, traces={t.tx_hash: trace})
    assert result.traced
    assert result.coinbase_transfers == E18 // 10
    assert result.eth_flow == -(E18 // 10)
    assert result.builder_payment == result.priority_fee + E18 // 10


def test_wrapping_eth_is_not_profit():
    # Bot unwraps 1 WETH: no Transfer log, ETH arrives from the WETH contract.
    trace = {"type": "CALL", "from": EOA, "to": BOT, "value": "0x0",
             "calls": [{"type": "CALL", "from": WETH, "to": BOT, "value": hex(E18)}]}  # fmt: skip
    t = tx()
    result = account([t], [BOT, EOA], BUILDER, 0, traces={t.tx_hash: trace})
    assert result.eth_flow == 0


def test_reverted_tx_pays_gas_but_moves_no_value():
    result = account([tx(value=E18, to=OTHER, status=0)], [BOT, EOA], BUILDER, 0)
    assert result.eth_flow == 0 and result.gas_fee > 0


def test_gas_only_charged_when_searcher_signs():
    result = account([tx(sender=OTHER, to=BOT)], [BOT, EOA], BUILDER, 0)
    assert result.gas_fee == 0


def test_pricer_uses_pre_block_reserves_of_deepest_weth_pool():
    token = USDC
    shallow = Pool(addr(0xA), token, WETH, 3_000 * E18, 1 * E18)  # 3000 per ETH, too thin
    deep = Pool(addr(0xB), token, WETH, 2_000_000 * E18, 1_000 * E18)  # 2000 per ETH
    s1 = deep.swap(1, 100 * E18, WETH, OTHER)  # moves the price inside the block
    s2 = deep.swap(2, 1_000 * E18, token, OTHER)
    s3 = shallow.swap(3, 10 * E18, token, OTHER)
    pricer = EthPricer([s2, s1, s3])
    # Price from the deep pool's state before its first swap: 2000 token per ETH.
    assert pricer.wei_value(token, 2_000 * E18, block=100) == E18
    assert pricer.wei_value(token, -2_000 * E18, block=100) == -E18
    assert pricer.wei_value(WETH, 5, block=100) == 5
    assert pricer.wei_value(addr(0xDEAD), 5, block=100) is None  # unknown, no RPC
    assert pricer.wei_value(token, 5, block=101) is None  # other block
