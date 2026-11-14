"""The API's side of the mock distributor order systems (V2 §7): their admin
endpoints and their own hold log, and what a switch to v2 owes the run.

Each order system is a separate process, so any of them may be down. Every call
here returns a OrderReply instead of raising: the operator's button shows
the reason, and the hourly invariant check records invariant 2 as unknown
rather than failed (an order system we cannot read has not been shown to hold a
duplicate).

Admin calls carry the API's admin token in X-Admin-Token; the mock order system is
started with the same ADMIN_TOKEN.

A switch is detected only when the agent next calls the order system (V2 §7
"Detect": a check in code on a real response). owe_probe() makes that call
happen within seconds, whoever switched (the operator's button or the
scheduler): the recall's distributors on that order system become due now and owe
one contract probe (see levela.harness.brief.order_system_facts).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import httpx

log = logging.getLogger("levela.api.order_admin")

TIMEOUT_S = 3.0
FAULTS = ("accepted_then_dropped", "timeout_after_commit", "stale_read")
V2 = 2


class LazyHttp:
    """The httpx client for the order systems, built on first use. Building one
    loads the CA bundle, which takes seconds on a cold disk, and an API
    process (or a test) that never reaches an order system should not pay for it.
    Has the one method the calls below use, httpx.Client.request, so a test
    can pass a real httpx.Client with a MockTransport instead."""

    def __init__(self):
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def _get(self) -> httpx.Client:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = httpx.Client(timeout=TIMEOUT_S)
        return self._client

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self._get().request(method, url, **kwargs)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


@dataclass(frozen=True)
class OrderReply:
    ok: bool
    status: int | None = None
    body: object = None
    error: str | None = None

    def view(self) -> dict:
        return {"ok": self.ok, "status": self.status, "body": self.body, "error": self.error}


def _body(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text


def _call(http: httpx.Client | LazyHttp, method: str, url: str, *, token: str | None = None, json=None) -> OrderReply:
    headers = {"X-Admin-Token": token} if token else {}
    try:
        response = http.request(method, url, json=json, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("order system %s %s failed: %s", method, url, exc)
        return OrderReply(ok=False, error=f"order system unreachable: {exc.__class__.__name__}")
    body = _body(response)
    if response.is_success:
        return OrderReply(ok=True, status=response.status_code, body=body)
    return OrderReply(ok=False, status=response.status_code, body=body,
                       error=f"order system answered {response.status_code}")


def switch_version(http: httpx.Client | LazyHttp, base_url: str, token: str, version: int = V2) -> OrderReply:
    return _call(http, "POST", f"{base_url}/_admin/version", token=token, json={"version": version})


def arm_fault(http: httpx.Client | LazyHttp, base_url: str, token: str, fault: str, count: int = 1) -> OrderReply:
    return _call(http, "POST", f"{base_url}/_admin/fault", token=token, json={"fault": fault, "count": count})


def set_speed(http: httpx.Client | LazyHttp, base_url: str, token: str, seconds_per_sim_hour: float) -> OrderReply:
    """The order system's stale-read fault lasts 10 simulated minutes, so it needs the run's speed."""
    return _call(http, "POST", f"{base_url}/_admin/clock", token=token,
                 json={"seconds_per_sim_hour": seconds_per_sim_hour})


def holds_log(http: httpx.Client | LazyHttp, base_url: str) -> OrderReply:
    """The order system's own record of every hold by idempotency key (invariant 2)."""
    return _call(http, "GET", f"{base_url}/_log/holds")


def reset(http: httpx.Client | LazyHttp, base_url: str, token: str) -> OrderReply:
    """Back to v1 with no holds and no armed faults. An order system keeps its state
    in memory for as long as its process lives, so a database reset alone
    would leave the next run meeting an order system already on v2 at day 0."""
    return _call(http, "POST", f"{base_url}/_admin/reset", token=token)


def reset_all(http: httpx.Client | LazyHttp, order_urls: dict[str, str], token: str) -> dict[str, dict]:
    """reset() on every configured order system: {name: reply.view()}, so an order system
    that could not be reset (down, wrong token) is named, not hidden."""
    return {name: reset(http, url, token).view() for name, url in sorted(order_urls.items())}


def unreset(replies: dict[str, dict]) -> list[str]:
    """The order systems reset_all() could not reset."""
    return sorted(name for name, view in replies.items() if not view.get("ok"))


def owe_probe(db, recall_id: str, order_system: str, sim_now: float) -> int:
    """The recall's distributors on `order system` are due now (an earlier timer
    stands) and owe one more contract probe: order_system_probes counts the
    switches, order_system_probe_due says the latest one is not yet probed. A
    reconciled distributor is due again because of the flag
    (orchestrator.timers.work_filter). Returns how many distributors owe it."""
    return db.parties.update_many(
        {"recall_id": recall_id, "order_system": order_system, "channel": "order_system"},
        [{"$set": {"next_action_at": {"$min": [{"$ifNull": ["$next_action_at", sim_now]}, sim_now]},
                   "order_system_probe_due": True,
                   "order_system_probes": {"$add": [{"$ifNull": ["$order_system_probes", 0]}, 1]}}}]).modified_count
