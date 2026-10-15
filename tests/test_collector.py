"""Cache, credit counter and RPC client, against a fake HTTP session (no network)."""

from datetime import UTC, date, datetime

import pytest

from collector.cache import RpcCache, cache_key
from collector.credits import BudgetExhausted, CreditLedger, CreditPolicy, next_reset
from collector.rpc import OfflineMiss, Provider, RpcClient, RpcError, uses_block_tag
from collector.sampling import draw_blocks, first_block_at_or_after, sampled_days

HEAD = 20_000_000


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Answers JSON-RPC by method; `script` can force status codes per call."""

    def __init__(self, handler=None, script=None):
        self.handler = handler or (lambda method, params: {"method": method, "params": params})
        self.script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    def post(self, url, json, timeout):
        self.calls.append((url, json["method"]))
        if self.script:
            status = self.script.pop(0)
            if status != 200:
                return FakeResponse(status, {})
        if json["method"] == "eth_blockNumber":
            return FakeResponse(200, {"result": hex(HEAD)})
        result = self.handler(json["method"], json["params"])
        if isinstance(result, dict) and "__error__" in result:
            return FakeResponse(200, {"error": result["__error__"]})
        return FakeResponse(200, {"result": result})


def make_client(tmp_path, session, limits=(None,), offline=False, costs=None):
    providers = [
        Provider(
            f"p{i}",
            f"https://p{i}.example",
            CreditPolicy(limit=limit, safety_fraction=1.0, costs=costs or {}, default_cost=10),
        )
        for i, limit in enumerate(limits)
    ]
    client = RpcClient(
        providers,
        RpcCache(tmp_path / "c.sqlite"),
        CreditLedger(tmp_path / "l.sqlite"),
        offline=offline,
        session=session,
        max_retries=2,
    )
    client._backoff = lambda attempt: None  # no sleeping in tests
    return client


# --- cache ----------------------------------------------------------------------------------


def test_cache_key_ignores_hex_case():
    assert cache_key("eth_call", [{"to": "0xABC"}, "0x10"]) == cache_key(
        "eth_call", [{"to": "0xabc"}, "0x10"]
    )


def test_cache_round_trip_and_refuses_null(tmp_path):
    cache = RpcCache(tmp_path / "c.sqlite")
    cache.put("eth_getBlockByNumber", ["0x1", True], {"number": "0x1"}, "infura")
    assert cache.get("eth_getBlockByNumber", ["0x1", True]) == {"number": "0x1"}
    assert cache.get("eth_getBlockByNumber", ["0x1", False]) is None
    with pytest.raises(ValueError):
        cache.put("eth_getBlockByNumber", ["0x2", True], None, "infura")
    [summary] = cache.provenance_summary()
    assert summary["provider"] == "infura" and summary["entries"] == 1


# --- credits --------------------------------------------------------------------------------


def test_ledger_resets_each_day(tmp_path):
    now = [datetime(2026, 10, 15, 23, 59, tzinfo=UTC)]
    ledger = CreditLedger(tmp_path / "l.sqlite", clock=lambda: now[0])
    policy = CreditPolicy(limit=100, safety_fraction=0.9, default_cost=40)
    assert ledger.can_spend("infura", policy, "x")
    ledger.record("infura", policy, "x", 40)
    ledger.record("infura", policy, "x", 40)
    assert ledger.remaining("infura", policy) == 10  # budget 90 - 80
    assert not ledger.can_spend("infura", policy, "x")
    now[0] = datetime(2026, 10, 16, 0, 1, tzinfo=UTC)
    assert ledger.remaining("infura", policy) == 90


def test_next_reset():
    t = datetime(2026, 12, 31, 15, 0, tzinfo=UTC)
    assert next_reset("day", t) == datetime(2027, 1, 1, tzinfo=UTC)
    assert next_reset("month", t) == datetime(2027, 1, 1, tzinfo=UTC)


# --- rpc client ----------------------------------------------------------------------------


def test_second_identical_call_is_served_from_cache(tmp_path):
    session = FakeSession()
    rpc = make_client(tmp_path, session)
    first = rpc.call("eth_getBlockReceipts", [hex(HEAD - 1000)])
    second = rpc.call("eth_getBlockReceipts", [hex(HEAD - 1000)])
    assert first == second
    methods = [m for _, m in session.calls]
    assert methods.count("eth_getBlockReceipts") == 1
    assert rpc.cache_hits == 1


def test_recent_blocks_and_tags_are_not_cached(tmp_path):
    session = FakeSession()
    rpc = make_client(tmp_path, session)
    rpc.call("eth_getBlockReceipts", [hex(HEAD - 5)])  # not final yet
    rpc.call("eth_getBlockReceipts", [hex(HEAD - 5)])
    rpc.call("eth_getBlockByNumber", ["latest", False])
    rpc.call("eth_getBlockByNumber", ["latest", False])
    methods = [m for _, m in session.calls]
    assert methods.count("eth_getBlockReceipts") == 2
    assert methods.count("eth_getBlockByNumber") == 2


def test_uses_block_tag():
    assert uses_block_tag([{"to": "0x1"}, "latest"])
    assert uses_block_tag([{"fromBlock": "0x1", "toBlock": "latest"}])
    assert not uses_block_tag([{"to": "0x1"}, "0x10"])


def test_falls_back_to_second_provider_when_first_budget_is_spent(tmp_path):
    session = FakeSession()
    rpc = make_client(tmp_path, session, limits=(25, None))
    rpc.call("eth_getBlockByNumber", [hex(1), False])  # p0: head lookup 10 + call 10 = 20
    rpc.call("eth_getBlockByNumber", [hex(2), False])  # p0 has 5 left < 10 -> p1
    urls = [u for u, m in session.calls if m == "eth_getBlockByNumber"]
    assert urls == ["https://p0.example", "https://p1.example"]


def test_raises_budget_exhausted_when_all_providers_are_spent(tmp_path):
    rpc = make_client(tmp_path, FakeSession(), limits=(20,))
    rpc.call("eth_getBlockByNumber", [hex(1), False])  # 10 (head) + 10
    with pytest.raises(BudgetExhausted):
        rpc.call("eth_getBlockByNumber", [hex(2), False])


def test_rate_limited_requests_are_retried_and_refunded(tmp_path):
    session = FakeSession(script=[429, 200])
    rpc = make_client(tmp_path, session, limits=(1000,))
    rpc.call("eth_chainId", [])
    policy = rpc.providers[0].policy
    assert rpc.ledger.used("p0", policy) == 10  # the 429 attempt was refunded


def test_json_rpc_errors_are_raised_not_retried(tmp_path):
    session = FakeSession(handler=lambda m, p: {"__error__": {"code": 3, "message": "revert"}})
    rpc = make_client(tmp_path, session)
    with pytest.raises(RpcError):
        rpc.call("eth_call", [{"to": "0x1", "data": "0x"}, hex(1)])
    assert len(session.calls) == 1


def test_offline_mode_never_touches_the_network(tmp_path):
    session = FakeSession()
    rpc = make_client(tmp_path, session, offline=True)
    with pytest.raises(OfflineMiss):
        rpc.call("eth_getBlockByNumber", [hex(1), True])
    assert session.calls == []


# --- sampling -------------------------------------------------------------------------------


def test_sampled_days_are_weekly_and_inclusive():
    days = list(sampled_days(date(2024, 1, 3), date(2024, 1, 17), 7))
    assert days == [date(2024, 1, 3), date(2024, 1, 10), date(2024, 1, 17)]


def test_draw_blocks_is_reproducible_and_independent_per_day():
    a = draw_blocks(100, 7_299, 300, seed=1, day=date(2024, 1, 3))
    b = draw_blocks(100, 7_299, 300, seed=1, day=date(2024, 1, 3))
    c = draw_blocks(100, 7_299, 300, seed=1, day=date(2024, 1, 10))
    assert a == b and a != c
    assert len(set(a)) == 300 and a == sorted(a) and a[0] >= 100 and a[-1] <= 7_299
    assert draw_blocks(1, 5, None, seed=1, day=date(2024, 1, 3)) == [1, 2, 3, 4, 5]


def test_first_block_at_or_after_binary_search(tmp_path):
    # Block n has timestamp 1000 + 12 n.
    def handler(method, params):
        return {"number": params[0], "timestamp": hex(1000 + 12 * int(params[0], 16))}

    rpc = make_client(tmp_path, FakeSession(handler=handler))
    assert first_block_at_or_after(rpc, 1000 + 12 * 500, 0, 10_000) == 500
    assert first_block_at_or_after(rpc, 1000 + 12 * 500 + 1, 0, 10_000) == 501
