"""JSON-RPC client with a local cache, a credit counter and provider fallback.

Every request goes through three checks, in order:
  1. Cache: if the response is already stored locally, return it (costs nothing).
  2. Budget: pick the first provider (in configured order) whose credit allowance still
     covers the call. If none does, raise BudgetExhausted so the collector can stop and
     resume after the reset.
  3. Network: send the request, retry on rate limits and server errors, and fall through
     to the next provider if one keeps failing.

We use plain JSON-RPC over `requests` rather than web3.py's provider stack because it
makes the cost of each request explicit and the raw responses easy to cache. web3.py's
libraries (eth_abi, eth_utils) are still used for decoding.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from collector.cache import RpcCache
from collector.credits import BudgetExhausted, CreditLedger, CreditPolicy

log = logging.getLogger(__name__)

# Methods whose result never changes once the referenced block is final.
CACHEABLE_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getBlockReceipts",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getLogs",
        "eth_call",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getBalance",
        "debug_traceTransaction",
        "trace_block",
        "trace_transaction",
    }
)

BLOCK_TAGS = frozenset({"latest", "pending", "safe", "finalized", "earliest"})

# A block this many blocks behind the head is treated as final (finality on Ethereum is
# two epochs = 64 slots). Responses about younger blocks are returned but not cached.
FINALITY_DEPTH = 64


class RpcError(RuntimeError):
    """The provider answered with a JSON-RPC error object (e.g. bad params, reverted call)."""

    def __init__(self, method: str, error: dict[str, Any]):
        super().__init__(f"{method}: {error.get('code')} {error.get('message')}")
        self.error = error


class OfflineMiss(LookupError):
    """Offline mode was requested but the response is not in the cache."""


@dataclass
class Provider:
    name: str
    url: str
    policy: CreditPolicy
    credits_per_second: float | None = None  # throughput cap; None = no client-side limit


class _RateLimiter:
    """Token bucket over credits, so we stay under a provider's credits-per-second cap."""

    def __init__(self, credits_per_second: float | None):
        self.rate = credits_per_second
        self.tokens = credits_per_second or 0.0
        self.last = time.monotonic()

    def wait(self, cost: int) -> None:
        if not self.rate:
            return
        while True:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= cost or self.tokens >= self.rate:
                self.tokens -= cost
                return
            time.sleep((min(cost, self.rate) - self.tokens) / self.rate)


def _hex_to_int(value: Any) -> int | None:
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    if isinstance(value, int):
        return value
    return None


def referenced_block(method: str, params: list[Any], result: Any) -> int | None:
    """Highest block number a request/response depends on, or None if unknown."""
    if method in ("eth_getBlockByNumber", "eth_getBlockReceipts", "trace_block"):
        return _hex_to_int(params[0])
    if method in ("eth_call", "eth_getCode", "eth_getStorageAt", "eth_getBalance"):
        return _hex_to_int(params[-1])
    if method == "eth_getLogs":
        return _hex_to_int(params[0].get("toBlock"))
    if isinstance(result, dict):
        return _hex_to_int(result.get("blockNumber"))
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return _hex_to_int(result[0].get("blockNumber"))
    return None


def uses_block_tag(params: Any) -> bool:
    if isinstance(params, str):
        return params in BLOCK_TAGS
    if isinstance(params, dict):
        return any(uses_block_tag(v) for v in params.values())
    if isinstance(params, list):
        return any(uses_block_tag(v) for v in params)
    return False


class RpcClient:
    def __init__(
        self,
        providers: list[Provider],
        cache: RpcCache,
        ledger: CreditLedger,
        *,
        offline: bool = False,
        timeout: float = 30.0,
        max_retries: int = 4,
        session: requests.Session | None = None,
    ):
        if not providers and not offline:
            raise ValueError("no RPC providers configured (check your .env)")
        self.providers = providers
        self.cache = cache
        self.ledger = ledger
        self.offline = offline
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._limiters = {p.name: _RateLimiter(p.credits_per_second) for p in providers}
        self._head: int | None = None
        self._request_id = 0
        self.network_calls = 0
        self.cache_hits = 0

    # -- public API --------------------------------------------------------------------

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        params = params or []
        cacheable = method in CACHEABLE_METHODS and not uses_block_tag(params)
        if cacheable:
            cached = self.cache.get(method, params)
            if cached is not None:
                self.cache_hits += 1
                return cached
        if self.offline:
            raise OfflineMiss(f"{method} {params} not in cache (offline mode)")

        result, provider = self._send(method, params)
        if cacheable and result is not None and self._is_final(method, params, result):
            self.cache.put(method, params, result, provider)
        return result

    def head(self) -> int:
        """Current chain head (uncached, fetched at most once per client)."""
        if self._head is None:
            self._head = int(self._send("eth_blockNumber", [])[0], 16)
        return self._head

    # -- internals -----------------------------------------------------------------------

    def _is_final(self, method: str, params: list[Any], result: Any) -> bool:
        if method == "eth_chainId":
            return True
        block = referenced_block(method, params, result)
        if block is None:
            return False
        return block <= self.head() - FINALITY_DEPTH

    def _send(self, method: str, params: list[Any]) -> tuple[Any, str]:
        budget_blocked = []
        last_error: Exception | None = None
        for provider in self.providers:
            if not self.ledger.can_spend(provider.name, provider.policy, method):
                budget_blocked.append(provider.name)
                continue
            try:
                return self._send_to(provider, method, params), provider.name
            except RpcError:
                raise
            except (requests.RequestException, _Retryable) as exc:
                log.warning("provider %s failed for %s: %s", provider.name, method, exc)
                last_error = exc
        if len(budget_blocked) == len(self.providers):
            raise BudgetExhausted(
                f"credit budget used up for all providers ({', '.join(budget_blocked)})"
            )
        raise ConnectionError(f"all providers failed for {method}") from last_error

    def _send_to(self, provider: Provider, method: str, params: list[Any]) -> Any:
        cost = provider.policy.cost(method)
        for attempt in range(self.max_retries + 1):
            self._limiters[provider.name].wait(cost)
            self._request_id += 1
            payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
            # Record before sending: if the process dies mid-request we over-count, never
            # under-count.
            self.ledger.record(provider.name, provider.policy, method, cost)
            self.network_calls += 1
            try:
                response = self.session.post(provider.url, json=payload, timeout=self.timeout)
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt)
                continue

            if response.status_code in (408, 429) or response.status_code >= 500:
                # Timeouts, rate limits and server errors are transient; Infura does not
                # bill 429/5xx responses.
                self.ledger.refund(provider.name, provider.policy, method, cost)
                if attempt == self.max_retries:
                    raise _Retryable(f"HTTP {response.status_code} from {provider.name}")
                self._backoff(attempt)
                continue
            if response.status_code == 403:
                # A JSON-RPC error body means the request itself was refused (e.g. archive
                # data on a non-archive endpoint): retrying will not help.
                error = _json_error(response)
                if error is not None:
                    raise RpcError(method, error)
                # Otherwise: public endpoints answer 403 when throttling; for a keyed provider
                # it means a bad key, so after the retries we fall through to the next one.
                if attempt == self.max_retries:
                    raise _Retryable(f"HTTP 403 from {provider.name}")
                self._backoff(attempt)
                continue
            if response.status_code == 402:
                # Provider says the daily allowance is gone even though our ledger did not.
                self.ledger.record(
                    provider.name, provider.policy, method, provider.policy.limit or 0
                )
                raise _Retryable(f"{provider.name} reports its credit limit is exhausted")
            response.raise_for_status()

            body = response.json()
            if "error" in body:
                if _is_rate_limit_error(body["error"]) and attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                raise RpcError(method, body["error"])
            return body.get("result")
        raise _Retryable("retries exhausted")  # pragma: no cover - loop always returns/raises

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(30.0, 0.5 * 2**attempt))


class _Retryable(RuntimeError):
    """Transient provider failure: try again later or fall through to the next provider."""


def _json_error(response: requests.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, dict) else None


def _is_rate_limit_error(error: dict[str, Any]) -> bool:
    message = str(error.get("message", "")).lower()
    return error.get("code") in (-32005, 429) or "rate limit" in message or "too many" in message
