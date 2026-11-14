"""HTTP client for one distributor order system, driven by an adapter (V2 §7).

Every call names the adapter it speaks: the adapter gives the endpoint
("POST /v2/holds"), the field names of the request, and the dot path of
every canonical field in the answer. The client checks the answer against
the adapter's contract (levela.systems.contract) and raises ContractError on
a 410, a 422, a missing field or a count that is not an integer, so a
changed order system is caught before a wrong number reaches the ledger.

Two other outcomes are kept apart because recovery depends on them:

  OrderSystemTimeout  no answer in time, or the connection broke after the request
                 went out: the effect is unknown and only a read-back decides
  OrderSystemError    the order system answered with an error (or could not be reached
                 at all): `sent` says whether the request may have taken effect

The client is synchronous (the worker is threaded) and thread-safe. Each
client owns one asyncio loop in a daemon thread and one pooled
httpx.AsyncClient on it, built on first use: building a transport loads the
TLS trust party, which costs seconds on a loaded laptop and must not happen
per call. Any httpx async transport works, so tests pass
httpx.ASGITransport(app) and talk to the order system in-process; the timeout is
enforced by the loop even then.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading

import httpx

from .contract import ContractError, require_field, set_path, to_canonical

DEFAULT_PORTS = {"north": 8101, "east": 8102}
DEFAULT_TIMEOUT_S = 10.0
ADMIN_ACTIONS = ("version", "fault", "clock", "reset")
_PARAM = re.compile(r"\{[^}/]+\}")


class OrderSystemTimeout(Exception):
    """No answer in time: the request may or may not have taken effect."""

    def __init__(self, endpoint: str, timeout: float):
        super().__init__(f"{endpoint}: no answer within {timeout:g} s; the effect is unknown")
        self.endpoint = endpoint


class OrderSystemError(Exception):
    """The order system answered with an error, or could not be reached (sent=False)."""

    def __init__(self, endpoint: str, status: int | None, body: str, *, sent: bool = True):
        what = f"answered {status}" if status is not None else "unreachable"
        super().__init__(f"{endpoint} {what}: {body[:300]}")
        self.endpoint, self.status, self.body, self.sent = endpoint, status, body, sent

    @property
    def effect_unknown(self) -> bool:
        """A 5xx may have committed before failing; a 4xx or no connection did not."""
        return self.sent and (self.status is None or self.status >= 500)


def order_url(name: str, env=None) -> str:
    """ORDER_<NAME>_URL, else 127.0.0.1 on the order system's default port."""
    env = os.environ if env is None else env
    url = env.get(f"ORDER_{name.upper()}_URL")
    if url:
        return url.rstrip("/")
    if name not in DEFAULT_PORTS:
        raise KeyError(f"no URL for order system {name!r}: set ORDER_{name.upper()}_URL")
    return f"http://127.0.0.1:{DEFAULT_PORTS[name]}"


def build_order_systems(env=None, *, names=None, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """{name: OrderClient} for the harness context (ctx.order systems)."""
    env = os.environ if env is None else env
    found = {k[len("ORDER_"):-len("_URL")].lower() for k in env if k.startswith("ORDER_") and k.endswith("_URL")}
    names = sorted(names or (set(DEFAULT_PORTS) | found))
    return {n: OrderClient(order_url(n, env), timeout=timeout, name=n) for n in names}


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    """The client's loop thread: run until close() stops it, then release it."""
    try:
        loop.run_forever()
    finally:
        loop.close()


def _summary(body: str) -> str:
    """The first sentence of a JSON error's detail, for the one-line failure text."""
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return body[:120]
    if isinstance(detail, list) and detail:
        first = detail[0]
        loc = ".".join(str(p) for p in first.get("loc", []))
        return f"{loc}: {first.get('msg', '')}".strip(": ")
    return str(detail).split(". ")[0].rstrip(".") if detail else body[:120]


class OrderClient:
    def __init__(self, base_url: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, name: str | None = None):
        if base_url is None and transport is None:
            raise ValueError("OrderClient needs a base_url or a transport")
        self.base_url = (base_url or "http://order-system").rstrip("/")
        self.transport, self.timeout, self.name = transport, float(timeout), name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: httpx.AsyncClient | None = None      # touched only on the loop's thread

    # ------------------------------------------------------------- transport
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=_serve, args=(loop,), name=f"order system-{self.name or 'client'}",
                                 daemon=True).start()
                self._loop = loop
            return self._loop

    def close(self) -> None:
        """Close the connection pool and stop the loop thread. A later call
        starts a fresh one, so closing is always safe."""
        with self._lock:
            loop, self._loop = self._loop, None
        if loop is None:
            return

        async def shutdown():
            if self._http is not None:
                http, self._http = self._http, None
                await http.aclose()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(10)
        finally:
            loop.call_soon_threadsafe(loop.stop)

    async def _asend(self, method: str, path: str, kwargs: dict) -> httpx.Response:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=self.timeout)
        return await asyncio.wait_for(self._http.request(method, path, **kwargs), self.timeout)

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        endpoint = f"{method} {path}"
        future = asyncio.run_coroutine_threadsafe(self._asend(method, path, kwargs), self._ensure_loop())
        try:
            # The loop enforces the timeout; the margin covers building the
            # client on first use, never a request that outlives its timeout.
            return future.result(self.timeout + 60.0)
        except (TimeoutError, httpx.TimeoutException):
            future.cancel()
            raise OrderSystemTimeout(endpoint, self.timeout) from None
        except httpx.ConnectError as exc:
            raise OrderSystemError(endpoint, None, str(exc), sent=False) from None
        except httpx.TransportError:
            # The connection broke after the request was written: unknown effect.
            raise OrderSystemTimeout(endpoint, self.timeout) from None

    def _call(self, adapter: dict, role: str, *, path_value: str | None = None, key: str | None = None,
              **kwargs) -> tuple[str, object, str]:
        """(endpoint, parsed JSON, raw text) for one adapter role, or the right
        exception. `path_value` fills the endpoint's one path parameter
        ("/v2/holds/{hold_id}/confirm"), whatever the order system named it."""
        endpoint = adapter["paths"].get(role)
        if not endpoint:
            raise ValueError(f"adapter {adapter.get('_id')} has no {role} endpoint")
        method, _, path = endpoint.partition(" ")
        if path_value is not None:
            path = _PARAM.sub(str(path_value), path)
        headers = {"Idempotency-Key": key} if key else None
        resp = self._send(method, path, headers=headers, **kwargs)
        text = resp.text
        if resp.status_code in (410, 422):
            phrase = "Gone" if resp.status_code == 410 else "Unprocessable"
            raise ContractError(f"{method} {path} answered {resp.status_code} {phrase} ({_summary(text)})",
                                raw_body=text, status=resp.status_code, endpoint=endpoint)
        if resp.status_code >= 400:
            raise OrderSystemError(f"{method} {path}", resp.status_code, text)
        try:
            return endpoint, resp.json(), text
        except ValueError:
            raise ContractError(f"{method} {path} did not answer JSON", raw_body=text,
                                status=resp.status_code, endpoint=endpoint) from None

    # ------------------------------------------------------- adapter calls
    def place_hold(self, batch: str, scope: str, key: str, *, adapter: dict) -> tuple[str, dict]:
        """(hold_id, raw answer). The request body is built from the adapter's
        mapping and its fixed defaults (v2 needs reason_code)."""
        mapping = adapter["mapping"]
        body: dict = {}
        set_path(body, mapping["batch"], batch)
        set_path(body, mapping["account_scope"], scope)
        for field, value in (adapter.get("defaults") or {}).items():
            set_path(body, field, value)
        endpoint, data, text = self._call(adapter, "place_hold", key=key, json=body)
        hold_id = require_field(data, mapping["hold_id"], endpoint=endpoint, raw_body=text)
        return str(hold_id), data

    def confirm_hold(self, hold_id: str, key: str, *, adapter: dict) -> dict:
        """The adapter's confirm step (v2). Returns the raw answer."""
        endpoint, data, text = self._call(adapter, "confirm_hold", path_value=hold_id, key=key)
        require_field(data, adapter["mapping"]["status"], endpoint=endpoint, raw_body=text)
        return data

    def list_holds(self, batch: str, *, adapter: dict) -> list[dict]:
        """Holds on the batch, as canonical rows {hold_id, batch, account_scope, status}."""
        endpoint, data, text = self._call(adapter, "list_holds", params={adapter["mapping"]["batch"]: batch})
        return to_canonical(data, adapter["mapping"], "holds", endpoint=endpoint, raw_body=text)

    def list_accounts(self, batch: str, *, adapter: dict) -> list[dict]:
        """Accounts that received the batch, as canonical rows
        {account, name, phone, qty_shipped, qty_in_stock}."""
        endpoint, data, text = self._call(adapter, "list_accounts", params={adapter["mapping"]["batch"]: batch})
        return to_canonical(data, adapter["mapping"], "accounts", endpoint=endpoint, raw_body=text)

    # ---------------------------------------------------- order system plumbing
    def openapi(self) -> dict:
        resp = self._send("GET", "/openapi.json")
        if resp.status_code != 200:
            raise OrderSystemError("GET /openapi.json", resp.status_code, resp.text)
        return resp.json()

    def holds_log(self) -> list[dict]:
        """The order system's own record of every hold, by idempotency key."""
        resp = self._send("GET", "/_log/holds")
        if resp.status_code != 200:
            raise OrderSystemError("GET /_log/holds", resp.status_code, resp.text)
        return resp.json()["holds"]

    def admin(self, action: str, *, token: str, body: dict | None = None) -> dict:
        """POST /_admin/<action> (version, fault, clock, reset); "state" reads."""
        headers = {"X-Admin-Token": token}
        if action == "state":
            resp = self._send("GET", "/_admin/state", headers=headers)
        elif action in ADMIN_ACTIONS:
            resp = self._send("POST", f"/_admin/{action}", headers=headers, json=body or {})
        else:
            raise ValueError(f"unknown order system admin action {action!r}")
        if resp.status_code != 200:
            raise OrderSystemError(f"/_admin/{action}", resp.status_code, resp.text)
        return resp.json()
