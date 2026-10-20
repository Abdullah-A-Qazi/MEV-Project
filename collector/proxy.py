"""A local JSON-RPC endpoint that forwards every request through RpcClient.

Anvil, when forking mainnet, reads chain state (balances, code, storage) from an RPC URL.
Pointed straight at Infura, those reads would bypass our cache and credit counter, so a
second run would pay again and the daily limit could be overrun. Pointed at this proxy, each
read is cached forever and counted like every other request.

    with RpcProxy(rpc, fork_block=19_000_000) as url:
        ... start anvil with --fork-url {url} ...

`fork_block` tells the cache which block the requests are about, for methods whose params
do not name one. The proxy only listens on 127.0.0.1.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from collector.rpc import RpcClient, RpcError


def handle_request(rpc: RpcClient, request: dict[str, Any], fork_block: int | None) -> dict:
    """Answer one JSON-RPC request object through `rpc`."""
    base = {"jsonrpc": "2.0", "id": request.get("id")}
    try:
        result = rpc.call(request["method"], request.get("params") or [], block=fork_block)
    except RpcError as exc:
        return {**base, "error": exc.error}
    except Exception as exc:  # budget exhausted, network down: report, do not crash Anvil
        return {**base, "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}
    return {**base, "result": result}


class RpcProxy:
    def __init__(self, rpc: RpcClient, fork_block: int | None = None, port: int = 0):
        self.rpc = rpc
        self.fork_block = fork_block
        self._lock = threading.Lock()  # RpcClient and its SQLite handles are not thread-safe
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server naming)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with proxy._lock:
                    if isinstance(body, list):
                        reply: Any = [handle_request(proxy.rpc, r, proxy.fork_block) for r in body]
                    else:
                        reply = handle_request(proxy.rpc, body, proxy.fork_block)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:  # keep stdout quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.url

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
