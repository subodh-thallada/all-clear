"""Spec test 16: the long run.

scenarios/recall_long.json (batch B-4471, 2,400 tins), every party
simulated, at 0.05 s per simulated hour, with the API (hosting the simulator,
which also plays Summit Foods' node, and the invariant scheduler) and a pool
of eight worker threads in this process and the two mock order systems as
processes. The workers run until ten simulated days have passed and nothing
is due before day 10 or in flight. Pass: 40 parties (5 at the start), only
the slice's tins open (15 disputed between Queen St Grocer and Corner Mart,
25 still on the shelf at Corner Mart), Summit Foods closed with nothing
unresolved, the mass balance equal to the recall's units at every sample (a
snapshot read about every 5 simulated hours) and at every hourly invariant
check, and the whole run inside three minutes on CI's local MongoDB.

The helpers here are shared with tests 22 and 31 (test_random_kills.py loads
this file by path).
"""
from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from levela import clock as simclock
from levela.config import LIVE_ATTEMPT_STATES, ROOT, SCENARIO_DIR
from levela.db.client import run_txn
from levela.db.seed import load_scenario, start_recall
from levela.ledger.reconciler import mass_balance, metrics
from levela.orchestrator import timers

V2_MODULES = ("levela.harness", "levela.systems.order_client", "levela.systems.operations", "levela.mock.order_system",
              "levela.ledger.invariants", "levela.api.scheduler")
def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Free ports per test session (not the 8101/8102 defaults), so two sessions
# can run the long run side by side.
ORDER_URLS = {name: f"http://127.0.0.1:{_free_port()}" for name in ("north", "east")}
SPEED = 0.05
DAYS = 10
DEADLINE_S = 180


def missing_v2() -> list[str]:
    """What tests 16, 22 and 31 need from the other wave-2 modules."""
    missing = []
    for name in V2_MODULES:
        try:
            found = importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            found = False
        if not found:
            missing.append(name)
    if not (SCENARIO_DIR / "recall_long.json").exists():
        missing.append("scenarios/recall_long.json")
    return missing


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{url} never answered"
        time.sleep(0.2)


def worker_env(settings, test_uri: str, api_url: str) -> dict:
    """The environment a worker process (or an order system) needs to join this test's system."""
    return {**os.environ, "ATLAS_URI": test_uri, "ATLAS_DB": settings.db_name, "API_BASE_URL": api_url,
            "TOOL_SECRET": settings.tool_secret, "ELEVEN_WEBHOOK_SECRET": settings.webhook_secret,
            "ADMIN_TOKEN": settings.admin_token, "VOICE_PROVIDER": "sim", "EMBED_PROVIDER": "hash",
            "MEMORY_ENABLED": "false", "INDOUBT_GRACE_S": "1",
            "HEARTBEAT_S": "0.5", "STALE_CALL_S": "20", "SIM_TIME_SCALE": "0", "WORKER_NAME": "worker-long",
            "ORDER_NORTH_URL": ORDER_URLS["north"], "ORDER_EAST_URL": ORDER_URLS["east"],
            "PYTHONUNBUFFERED": "1"}


@contextmanager
def mock_order_systems(env: dict, monkeypatch, seconds_per_sim_hour: float, scenario: str = "recall_long"):
    """`python -m levela.mock.order_system` for north and east on their default
    ports, on the run's clock speed."""
    for name, url in ORDER_URLS.items():
        monkeypatch.setenv(f"ORDER_{name.upper()}_URL", url)
    procs = [subprocess.Popen([sys.executable, "-m", "levela.mock.order_system", "--name", name, "--port",
                               url.rsplit(":", 1)[1], "--scenario", scenario,
                               "--seconds-per-sim-hour", str(seconds_per_sim_hour)], cwd=str(ROOT), env=env)
             for name, url in ORDER_URLS.items()]
    try:
        for url in ORDER_URLS.values():
            wait_http(url + "/openapi.json")
        yield ORDER_URLS
    finally:
        for p in procs:
            p.terminate()
            p.wait(15)


@contextmanager
def api_server(settings, client):
    """create_app() served over HTTP from a thread (worker processes reach it by URL)."""
    import uvicorn

    from levela.api.main import create_app

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    # The scheduler reads invariant 2 from the order systems this test starts: an
    # unread order system log makes the check unknown, not held.
    s = replace(settings, api_base_url=url, order_north_url=ORDER_URLS["north"],
                order_east_url=ORDER_URLS["east"])
    server = uvicorn.Server(uvicorn.Config(create_app(s, client=client), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_http(url + "/healthz")
        yield SimpleNamespace(url=url, settings=s)
    finally:
        server.should_exit = True
        thread.join(15)


class BalanceSampler:
    """The mass balance read at one point in time (a snapshot transaction),
    sampled through the run."""

    def __init__(self, client, db, recall_id, every_s: float = 0.25):
        self.client, self.db, self.recall_id, self.every_s = client, db, recall_id, every_s
        self.samples: list[tuple[float, int]] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            hour = simclock.sim_now(self.db, self.recall_id, self.client)
            value = run_txn(self.client, lambda s: mass_balance(self.db, self.recall_id, session=s))
            self.samples.append((hour, value))
            self.stop.wait(self.every_s)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(10)


def run_settled(client, db, recall_id) -> bool:
    """Ten simulated days have passed and nothing is in flight: no live call,
    no lease, no timer before day 10."""
    if simclock.sim_now(db, recall_id, client) < 24 * DAYS:
        return False
    nxt = timers.next_due_at(db, recall_id)
    return ((nxt is None or nxt > 24 * DAYS)
            and db.attempts.count_documents({"recall_id": recall_id,
                                             "state": {"$in": list(LIVE_ATTEMPT_STATES)}}) == 0
            and db.parties.count_documents({"recall_id": recall_id,
                                           "$expr": {"$gt": ["$lease_until", "$$NOW"]}}) == 0)


def wait_settled(client, db, recall_id, t0: float, deadline_s: float) -> None:
    while not run_settled(client, db, recall_id):
        assert time.monotonic() - t0 < deadline_s, (
            f"the run did not settle in {deadline_s:.0f} s: {metrics(db, recall_id)}")
        time.sleep(0.5)


def last_check(db, recall_id) -> dict | None:
    """The run's last invariant check, read while the mock order systems still run:
    once they stop, the API's scheduler (every 0.05 s on the test clock)
    records invariant 2 as unknown because their hold logs cannot be read."""
    return (db.runs.find_one({"_id": f"run:{recall_id}"}) or {}).get("last_invariant_check")


def assert_ends_like_test_16(db, recall_id, samples, check: dict | None = None) -> None:
    """The scenario's expected end (40 parties, 5 at the start, 40 tins open:
    the slice's 15 disputed and 25 on Corner Mart's shelf), the balance at
    every sample and every check."""
    final = load_scenario("recall_long")["expected"]["final"]
    total = db.recalls.find_one({"_id": recall_id})["units_total"]
    n = db.parties.count_documents({"recall_id": recall_id})
    assert n == final["parties"], f"{n} parties, not {final['parties']}: {open_detail(db, recall_id)}"
    assert db.parties.count_documents({"recall_id": recall_id, "found_via": "shipment"}) == final["at_start"]
    m = metrics(db, recall_id)
    assert (m["open_units"], m["disputed_units"], m["on_shelf"], m["not_located"]) == (
        final["open_units"], final["disputed_units"], final["on_shelf"], final["not_located"]), \
        f"only the slice's tins stay open (15 disputed, 25 on the shelf at Corner Mart): {m}; " \
        f"still open: {open_detail(db, recall_id)}"
    corner = db.parties.find_one({"recall_id": recall_id, "party_id": "corner-mart"})
    assert corner["on_shelf"] == final["on_shelf"]
    summit = db.parties.find_one({"recall_id": recall_id, "party_id": "summit-foods"})
    assert summit["unresolved"] == 0 and summit["on_shelf"] == 0, "Summit's reports moved its 120 tins out of unresolved"
    assert (summit.get("sub") or {}).get("state") == "closed", "Summit reported closed on day 9"
    broken = [(round(h, 1), v) for h, v in samples if v != total]
    assert samples and not broken, f"mass balance broke at {broken[:5]}"
    run = db.runs.find_one({"_id": f"run:{recall_id}"})
    assert run["invariant_failed"] is False and run["duplicates"] == 0
    assert (check or run["last_invariant_check"])["passed"] == [True, True, True, True]


def open_detail(db, recall_id) -> dict:
    """What a failure needs to be diagnosed from a CI log: every transfer that
    is not corroborated with the recipient's position on the ladder, every
    party not reconciled, and the feed's last lines."""
    parties = {s["party_id"]: s for s in db.parties.find({"recall_id": recall_id})}

    def view(sid):
        r = parties.get(sid, {})
        return {"party": sid, "state": r.get("state"), "channel": r.get("channel"), "ladder": r.get("ladder_step"),
                "escalation": r.get("escalation"), "next_action_at": r.get("next_action_at"),
                "last_outcome": r.get("last_outcome"), "slice": r.get("slice"),
                "attempts": db.attempts.count_documents({"recall_id": recall_id, "party_id": sid})}
    edges = [{"edge": f"{e['from_party']}>{e['to_party']}", "state": e["state"], "sender": e["sender_qty"],
              "recipient": e.get("recipient_qty"), **view(e["to_party"])}
             for e in db.edges.find({"recall_id": recall_id, "state": {"$ne": "corroborated"}})]
    open_parties = [view(sid) for sid, st in parties.items() if st.get("state") != "reconciled"]
    feed = [f"{f.get('sim_label')} {f.get('system')} {f['text']}"
            for f in db.feed.find({"recall_id": recall_id}).sort("at", -1).limit(25)]
    return {"edges": edges, "open_parties": open_parties, "feed": feed}


def start_long_run(client, db, seconds_per_sim_hour: float) -> str:
    """recall_long, all simulated; the slice's follow-ups stay manual, so its
    dispute of 15 and Corner Mart's 25 on the shelf are all that is left open.

    The scheduled order system switch is held (null = by hand, V2 §3). Until the API
    was pointed at this test's order systems (api_server) for invariant 2, its
    scheduler could not reach them and the switch never fired; holding it
    keeps what these runs exercise unchanged."""
    rid = start_recall(client, db, load_scenario("recall_long"), all_sim=True, mode="sim", followups="manual",
                       memory=False, seconds_per_sim_hour=seconds_per_sim_hour, env={})["recall_id"]
    db.runs.update_one({"_id": f"run:{rid}"},
                       {"$set": {"events.order_system_change_at": {"north": None, "east": None}}})
    return rid


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_16_the_long_run(client, db, settings, test_uri, monkeypatch):
    missing = missing_v2()
    if missing:
        pytest.skip(f"needs the other wave-2 modules: {', '.join(missing)}")
    from levela import harness
    from levela.orchestrator.planner import Planner
    from levela.orchestrator.failpoints import Failpoints
    from levela.orchestrator.worker import WorkerPool, build_decider, build_order_systems
    from levela.providers import SimProbe, SimVoice

    with api_server(settings, client) as api, \
            mock_order_systems(worker_env(api.settings, test_uri, api.url), monkeypatch, SPEED):
        s = api.settings
        # Every party is simulated: calls go to the API's SimNetwork, as from a worker process.
        sim_voice, probe = SimVoice(api.url, s.tool_secret), SimProbe(db)
        rid = start_long_run(client, db, SPEED)
        pool = WorkerPool(s, db, client, providers={"sim": sim_voice, "voice": sim_voice},
                          probes={"sim": probe, "voice": probe}, order_systems=build_order_systems(),
                          failpoints=Failpoints(db, env={}, worker=s.worker_name), decider=build_decider(s),
                          harness=harness, workers=8, recall_id=rid)
        pool.planner = Planner(s, db, client, planner_id=f"{s.worker_name}/planner", context=pool.context, recall_id=rid)
        stop = threading.Event()
        thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
        t0 = time.monotonic()
        with BalanceSampler(client, db, rid) as sampler:
            thread.start()
            try:
                wait_settled(client, db, rid, t0, DEADLINE_S)
            finally:
                stop.set()
                thread.join(30)
        elapsed = time.monotonic() - t0
        check = last_check(db, rid)
    assert elapsed < DEADLINE_S, f"the long run took {elapsed:.0f} s"
    assert_ends_like_test_16(db, rid, sampler.samples, check)
