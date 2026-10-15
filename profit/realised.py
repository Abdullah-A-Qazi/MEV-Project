"""Realised profit of a searcher: what it actually kept after gas and builder payments.

The searcher is a set of addresses S: the EOAs that signed the MEV transactions plus the
bot contract they called (never a public router). Over the MEV transactions we sum
everything that crosses the boundary of S:

* ERC-20 Transfer events in the receipts (tokens into S minus tokens out of S),
* ETH transfers: the top-level `value` of each transaction, and, when an execution trace is
  available, the internal ETH transfers too,
* gas paid by S's EOAs: gas_used x effective_gas_price, split into the burnt base fee and
  the priority fee that goes to the block's fee recipient.

ETH and WETH are treated as one asset, so wrapping and unwrapping are ignored: WETH emits no
Transfer for deposit/withdraw (only Deposit/Withdrawal events), and the matching ETH moves
between S and the WETH contract are skipped as well. Neither changes what S holds.

Without a trace, ETH that the bot contract sends internally (typically a "coinbase transfer"
tipping the builder) is invisible, so untraced realised profit is an UPPER bound. The traced
sample measures how large that gap is (RQ1). tests/test_realised_golden.py checks this
accounting against real balance changes of a searcher over whole blocks.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from common.constants import ERC20_TRANSFER_TOPIC, WETH
from common.nulls import is_missing

# Call types whose `value` is a real transfer. DELEGATECALL frames repeat the parent's value
# and STATICCALL cannot carry any, so counting them would double-count.
_VALUE_CALL_TYPES = frozenset({"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"})


@dataclass(frozen=True)
class TxData:
    """What the accounting needs to know about one transaction."""

    tx_hash: str
    tx_from: str
    tx_to: str | None
    value: int
    gas_used: int
    effective_gas_price: int
    logs: tuple[Mapping[str, Any], ...] = ()  # log rows of this tx (collector.pipeline format)
    status: int = 1  # 0 = reverted: pays gas, but moves no value and emits no logs


@dataclass(frozen=True)
class RealisedProfit:
    eth_flow: int  # net ETH + WETH into S, in wei (builder tips already subtracted)
    token_flows: dict[str, int]  # net raw amounts of every other token into S
    gas_fee: int  # total gas paid by S's EOAs, in wei
    base_fee_burnt: int
    priority_fee: int  # part of gas_fee paid to the fee recipient
    coinbase_transfers: int  # ETH sent by S directly to the fee recipient, in wei
    traced: bool  # True if internal ETH transfers were included for every tx
    token_value_eth: int | None = None  # token_flows valued in wei; None if any is unpriced
    unpriced_tokens: tuple[str, ...] = field(default_factory=tuple)

    @property
    def builder_payment(self) -> int:
        return self.priority_fee + self.coinbase_transfers

    @property
    def revenue(self) -> int | None:
        """Value produced by the trades, before gas and builder tips (in wei)."""
        if self.token_value_eth is None:
            return None
        return self.eth_flow + self.coinbase_transfers + self.token_value_eth

    @property
    def realised_eth_only(self) -> int:
        """ETH + WETH kept after gas, ignoring other-token residuals (always defined)."""
        return self.eth_flow - self.gas_fee

    @property
    def realised(self) -> int | None:
        """What S kept: revenue - gas - coinbase transfers (in wei)."""
        if self.token_value_eth is None:
            return None
        return self.eth_flow + self.token_value_eth - self.gas_fee


def _words(data: str) -> list[int]:
    payload = data[2:] if data.startswith("0x") else data
    return [int(payload[i : i + 64], 16) for i in range(0, len(payload), 64)]


def _topic_address(topic: str | None) -> str | None:
    return None if is_missing(topic) else "0x" + topic[-40:].lower()


def token_transfers(logs: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, str, int]]:
    """(token, from, to, amount) for every ERC-20 Transfer log.

    ERC-721 Transfers share the topic but put the token id in topic3 and have no data;
    they are skipped.
    """
    transfers = []
    for row in logs:
        if row["topic0"] != ERC20_TRANSFER_TOPIC or not is_missing(row.get("topic3")):
            continue
        words = _words(row["data"])
        if len(words) != 1:
            continue
        transfers.append(
            (
                row["address"].lower(),
                _topic_address(row["topic1"]),
                _topic_address(row["topic2"]),
                words[0],
            )
        )
    return transfers


def trace_value_transfers(frame: Mapping[str, Any]) -> list[tuple[str, str, int]]:
    """(from, to, wei) for every ETH transfer in a geth callTracer frame tree.

    Reverted frames (with an "error") and everything below them are skipped: their value
    transfers were undone.
    """
    if frame.get("error"):
        return []
    transfers = []
    value = int(frame.get("value") or "0x0", 16)
    if value and frame.get("type", "CALL").upper() in _VALUE_CALL_TYPES and frame.get("to"):
        transfers.append((frame["from"].lower(), frame["to"].lower(), value))
    for child in frame.get("calls") or []:
        transfers.extend(trace_value_transfers(child))
    return transfers


def account(
    txs: Iterable[TxData],
    actors: Iterable[str],
    fee_recipient: str,
    base_fee: int,
    traces: Mapping[str, Mapping[str, Any]] | None = None,
) -> RealisedProfit:
    """Net flows of the address set `actors` over `txs`, all in one block.

    `traces` maps tx_hash -> callTracer frame. A tx with a trace uses the trace for ETH
    transfers (its root frame already includes the top-level value); one without falls back
    to the top-level value only.
    """
    s = {a.lower() for a in actors}
    fee_recipient = fee_recipient.lower()
    traces = traces or {}
    eth_flow = gas_fee = base_burnt = coinbase = 0
    tokens: dict[str, int] = defaultdict(int)
    traced = True

    def crossing(src: str | None, dst: str | None) -> int:
        """+1 into S, -1 out of S, 0 if it does not cross the boundary."""
        return (dst in s) - (src in s)

    for tx in txs:
        for token, src, dst, amount in token_transfers(tx.logs):
            sign = crossing(src, dst)
            if not sign:
                continue
            if token == WETH:
                eth_flow += sign * amount
            else:
                tokens[token] += sign * amount

        if tx.tx_hash in traces:
            eth_moves = trace_value_transfers(traces[tx.tx_hash])
        else:
            traced = False
            moved = tx.value and tx.tx_to and tx.status == 1
            eth_moves = [(tx.tx_from, tx.tx_to, tx.value)] if moved else []
        for src, dst, wei in eth_moves:
            if WETH in (src, dst):
                continue  # wrapping/unwrapping: ETH and WETH are one asset here
            eth_flow += crossing(src, dst) * wei
            if src in s and dst == fee_recipient and dst not in s:
                coinbase += wei

        if tx.tx_from.lower() in s:
            gas_fee += tx.gas_used * tx.effective_gas_price
            base_burnt += tx.gas_used * base_fee

    return RealisedProfit(
        eth_flow=eth_flow,
        token_flows={t: v for t, v in sorted(tokens.items()) if v},
        gas_fee=gas_fee,
        base_fee_burnt=base_burnt,
        priority_fee=gas_fee - base_burnt,
        coinbase_transfers=coinbase,
        traced=traced,
    )
