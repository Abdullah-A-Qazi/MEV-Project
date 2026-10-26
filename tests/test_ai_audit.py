"""The rule-by-rule auditor on hand-built token transfers."""

import pandas as pd

from common.constants import UNISWAP_V2_ROUTER_02, WETH
from validation.ai_audit import Chain, audit_arbitrage, audit_nondetection, audit_sandwich

POOL, POOL2, TOKEN = "0x" + "11" * 20, "0x" + "12" * 20, "0x" + "77" * 20
BOT, BOT_EOA, USER, USER2 = "0x" + "b0" * 20, "0x" + "e0" * 20, "0x" + "a1" * 20, "0x" + "a2" * 20
E18 = 10**18


def h(n: int) -> str:
    return "0x" + f"{n:064x}"


def chain(txs, transfers) -> Chain:
    tx = pd.DataFrame(txs, columns=["tx_hash", "block_number", "tx_index", "from", "to"])
    tr = pd.DataFrame(
        transfers, columns=["tx_hash", "log_index", "address", "src", "dst", "amount"]
    )
    tr["block_number"] = 100
    return Chain(tx, tr)


def sandwich_chain(back_sells: int):
    txs = [(h(1), 100, 1, BOT_EOA, BOT), (h(2), 100, 2, USER, UNISWAP_V2_ROUTER_02),
           (h(3), 100, 3, BOT_EOA, BOT)]  # fmt: skip
    transfers = [
        (h(1), 0, WETH, BOT, POOL, 5 * E18), (h(1), 1, TOKEN, POOL, BOT, 1000),  # front buys
        (h(2), 2, WETH, USER, POOL, 2 * E18), (h(2), 3, TOKEN, POOL, USER, 300),  # victim buys
        (h(3), 4, TOKEN, BOT, POOL, back_sells), (h(3), 5, WETH, POOL, BOT, 5 * E18 + 10**16),
    ]  # fmt: skip
    return chain(txs, transfers)


ROW = {"front_run": h(1), "victims": h(2), "back_run": h(3), "pool": POOL}


def test_real_sandwich_passes_every_rule():
    label, evidence = audit_sandwich(ROW, sandwich_chain(back_sells=1000))
    assert label == "y", evidence
    assert "gross" in evidence


def test_back_run_selling_far_less_fails_rule_5():
    label, evidence = audit_sandwich(ROW, sandwich_chain(back_sells=100))
    assert label == "n" and "5✗" in evidence


def test_pool_absent_from_a_transaction_fails_rule_3():
    row = {**ROW, "pool": POOL2}
    label, evidence = audit_sandwich(row, sandwich_chain(back_sells=1000))
    assert label == "n" and "3✗" in evidence


def test_nondetection_flags_a_bracketing_actor():
    c = sandwich_chain(back_sells=1000)
    label, evidence = audit_nondetection({"swap_id": h(2) + ":2", "pool": POOL}, c)
    assert label == "n" and "missed sandwich" in evidence


def test_nondetection_clean_when_nobody_brackets():
    txs = [(h(2), 100, 2, USER, UNISWAP_V2_ROUTER_02), (h(4), 100, 4, USER2, UNISWAP_V2_ROUTER_02)]
    transfers = [
        (h(2), 0, WETH, USER, POOL, E18),
        (h(2), 1, TOKEN, POOL, USER, 100),
        (h(4), 2, TOKEN, USER2, POOL, 50),
        (h(4), 3, WETH, POOL, USER2, E18 // 2),
    ]
    label, _ = audit_nondetection({"swap_id": h(2) + ":1", "pool": POOL}, chain(txs, transfers))
    assert label == "y"


def test_profitable_loop_is_arbitrage_and_router_user_is_not():
    transfers = [(h(5), 0, WETH, BOT, POOL, E18), (h(5), 1, TOKEN, POOL, POOL2, 500),
                 (h(5), 2, WETH, POOL2, BOT, E18 + 10**16)]  # fmt: skip
    bot_tx = chain([(h(5), 100, 5, BOT_EOA, BOT)], transfers)
    label, evidence = audit_arbitrage({"transaction": h(5)}, bot_tx)
    assert label == "y", evidence
    user_tx = chain([(h(5), 100, 5, USER, UNISWAP_V2_ROUTER_02)],
                    [(t[0], t[1], t[2], USER if t[3] == BOT else t[3],
                      USER if t[4] == BOT else t[4], t[5]) for t in transfers])  # fmt: skip
    label, evidence = audit_arbitrage({"transaction": h(5)}, user_tx)
    assert label == "n" and "a✗" in evidence


def test_loop_with_unchained_amounts_is_left_to_a_human():
    # The bot buys TOKEN with 1 WETH but routes only a fifth of it on: the loop exists,
    # the amounts do not chain, so the auditor must not decide.
    other = "0x" + "99" * 20
    transfers = [
        (h(6), 0, WETH, BOT, POOL, E18),
        (h(6), 1, TOKEN, POOL, BOT, 1000),
        (h(6), 2, TOKEN, BOT, POOL2, 200),
        (h(6), 3, other, POOL2, BOT, 50),
        (h(6), 4, other, BOT, "0x" + "13" * 20, 50),
        (h(6), 5, WETH, "0x" + "13" * 20, BOT, E18 // 4),
    ]
    c = chain([(h(6), 100, 6, BOT_EOA, BOT)], transfers)
    label, evidence = audit_arbitrage({"transaction": h(6)}, c)
    assert label == "u" and "judge by hand" in evidence
