"""The API's scheduler: the hourly invariant check (with fake order system hold
logs and a stand-in for levela.ledger.invariants), the scheduled order system
change, and the thread's lifespan. tick() is driven directly; the clock is
frozen so every simulated time is exact."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from levela import clock, runs
from levela.api.main import create_app
from levela.api.scheduler import Scheduler
from levela.clock import Clock
from levela.db.seed import load_scenario, start_recall

PORTS = {8101: "north", 8102: "east"}
HOLDS = {"holds": [{"idempotency_key": "RCL-2026-017:dist-north:place_hold", "hold_id": "H-7781"}]}


class FakeInvariants:
    """Records what the scheduler hands levela.ledger.invariants and answers `result`."""

    def __init__(self, passed):
        self.result = {"at": None, "passed": list(passed), "details": [None] * 4}
        self.checked, self.recorded = [], []

    def check(self, db, recall_id, order_systems):
        self.checked.append((recall_id, order_systems))
        return self.result

    def record(self, db, recall_id, result):
        self.recorded.append((recall_id, result))


def order_client(log: list, down: set) -> httpx.Client:
    """Both mock order systems behind one MockTransport: every request is logged; a
    order system named in `down` refuses the connection."""
    def handler(request: httpx.Request) -> httpx.Response:
        name = PORTS[request.url.port]
        body = json.loads(request.content) if request.content else None
        log.append((name, request.method, request.url.path, request.headers.get("X-Admin-Token"), body))
        if name in down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/_log/holds":
            return httpx.Response(200, json=HOLDS)
        return httpx.Response(200, json={"ok": True, **(body or {})})
    return httpx.Client(transport=httpx.MockTransport(handler))


def pin(db, recall_id, hour):
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, recall_id, Clock(real_start=t0, sim_start=hour, paused_at=t0))
    clock.forget()


@pytest.fixture
def rid(client, db):
    """recall_1 with Queen St on North orders and Lakeside on East orders, at d3 09:15."""
    recall_id = start_recall(client, db, load_scenario("recall_1"), all_sim=True)["recall_id"]
    db.parties.update_one({"recall_id": recall_id, "party_id": "queen-st-grocer"}, {"$set": {"order_system": "north"}})
    db.parties.update_one({"recall_id": recall_id, "party_id": "lakeside-foods"}, {"$set": {"order_system": "east"}})
    pin(db, recall_id, 81.25)
    return recall_id


def lines(db, kind):
    return list(db.feed.find({"kind": kind}).sort("at", 1))


def test_invariants_run_once_per_simulated_hour_and_a_failure_stops_the_run(settings, client, db, rid):
    log, down = [], {"east"}
    fake = FakeInvariants([True, None, True, True])
    sched = Scheduler(settings, client, db, http=order_client(log, down), invariants=fake)

    [done] = sched.tick()
    assert (done["recall_id"], done["sim"], done["checked"]) == (rid, 81.25, True)
    assert fake.checked == [(rid, {"east": None, "north": HOLDS})]          # east is down: unknown, not failed
    assert fake.recorded == [(rid, fake.result)]
    assert [(p, m, path, token) for p, m, path, token, _ in log] == [("east", "GET", "/_log/holds", None),
                                                                     ("north", "GET", "/_log/holds", None)]
    [line] = lines(db, "invariants")
    assert (line["text"], line["level"], line["system"], line["sim_label"]) == (
        "3 of 4 hold, 1 unknown: East orders unreachable", "warn", "invariants", "d3 09:15")
    assert db.runs.find_one({"_id": runs.run_id(rid)})["invariants_hour"] == 81

    assert sched.tick()[0]["checked"] is False                              # same simulated hour
    assert len(fake.checked) == 1

    pin(db, rid, 82.0)
    down.clear()
    fake.result = {"at": 82.0, "passed": [True] * 4, "details": [None] * 4}
    assert sched.tick()[0]["checked"] is True
    assert (lines(db, "invariants")[-1]["text"], lines(db, "invariants")[-1]["level"]) == ("4 of 4 hold", "good")
    run = db.runs.find_one({"_id": runs.run_id(rid)})
    assert run["invariant_failed"] is False

    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"planner_wake": False}})
    fake.result = {"at": 82.0, "passed": [True, True, False, True],
                   "details": [None, None, "claim 65f0 has no tool event", None]}
    sched.tick(force=True)
    failed = lines(db, "invariants")[-1]
    assert failed["level"] == "bad"
    assert failed["text"] == ("FAILED 3 every accepted claim has a tool event (claim 65f0 has no tool event). "
                              "3 of 4 hold")
    run = db.runs.find_one({"_id": runs.run_id(rid)})
    assert (run["invariant_failed"], run["planner_wake"], run["planner_wake_reason"]) == (True, True, "invariant failed: 3")

    def broken(db_, recall_id, order_systems):
        raise RuntimeError("database unreachable")
    fake.check = broken
    assert sched.tick(force=True)[0]["result"] is None                      # the thread carries on
    cannot = lines(db, "invariants")[-1]
    assert (cannot["text"], cannot["level"]) == (
        "Invariant check could not run: RuntimeError. Next check in a simulated hour", "warn")


def test_scheduled_order_system_change_fires_once_and_retries_a_order_system_that_is_down(settings, client, db, rid):
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"events.order_system_change_at": {"north": 81.0, "east": 81.5}}})
    log, down = [], {"east"}
    sched = Scheduler(settings, client, db, http=order_client(log, down), invariants=FakeInvariants([True] * 4))

    assert sched.tick()[0]["fired"] == ["north"]                            # east is not due until 81.5
    admin = [(p, path, token, body) for p, _, path, token, body in log if path.startswith("/_admin")]
    assert admin == [("north", "/_admin/version", "test-admin-token", {"version": 2})]
    changed = db.runs.find_one({"_id": runs.run_id(rid)})["events"]["order_system_changed"]
    assert (changed["north"]["by"], changed["north"]["sim"]) == ("scheduler", 81.25) and "east" not in changed
    [switched] = lines(db, "order_system_switched")
    assert (switched["text"], switched["system"]) == ("North orders switched to v2 (scheduled for d3 09:00)",
                                                      "North orders")

    pin(db, rid, 81.75)
    assert sched.tick()[0]["fired"] == []                                   # east is due but down
    [failed] = lines(db, "order_system_admin_failed")
    assert failed["level"] == "bad" and failed["text"].startswith("East orders: scheduled switch to v2 failed")
    assert "east" not in db.runs.find_one({"_id": runs.run_id(rid)})["events"]["order_system_changed"]
    calls = len(log)
    assert sched.tick()[0]["fired"] == [] and len(log) == calls             # retried an hour later, not at once

    pin(db, rid, 82.8)
    down.clear()
    assert sched.tick()[0]["fired"] == ["east"]
    assert sched.tick()[0]["fired"] == []                                   # each order system switches once
    assert len(lines(db, "order_system_switched")) == 2

    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"events.order_system_change_at.north": None},
                                                   "$unset": {"events.order_system_changed.north": ""}})
    assert sched.tick()[0]["fired"] == []                                   # null means "by hand"


def test_a_scheduled_switch_makes_the_distributor_due_now_with_a_contract_probe_owed(settings, client, db, rid):
    """The change is detected by the agent's next order system call: without the
    probe that call waits for the next morning's read, or never comes once
    every account is reconciled."""
    for sid in ("queen-st-grocer", "lakeside-foods"):
        db.parties.update_one({"recall_id": rid, "party_id": sid}, {"$set": {"channel": "order_system", "next_action_at": None}})
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"events.order_system_change_at": {"north": 81.0, "east": 81.5}}})
    sched = Scheduler(settings, client, db, http=order_client([], set()), invariants=FakeInvariants([True] * 4))

    assert sched.tick()[0]["fired"] == ["north"]
    queen = db.parties.find_one({"recall_id": rid, "party_id": "queen-st-grocer"})
    assert (queen["next_action_at"], queen["order_system_probe_due"], queen["order_system_probes"]) == (81.25, True, 1)
    assert lines(db, "order_system_switched")[0]["detail"]["probes_owed"] == 1
    lakeside = db.parties.find_one({"recall_id": rid, "party_id": "lakeside-foods"})
    assert lakeside["next_action_at"] is None and "order_system_probes" not in lakeside, "east is not switched yet"


def test_the_lifespan_runs_the_thread_only_when_the_scheduler_is_enabled(settings, client, db):
    http = order_client([], set())
    app = create_app(settings, client=client, sim_network=object(), http=http)
    assert app.state.scheduler.running is False                             # building an app starts nothing
    with TestClient(app):
        assert app.state.scheduler.running is True
    assert app.state.scheduler.running is False

    off = create_app(replace(settings, scheduler_enabled=False), client=client, sim_network=object(), http=http)
    with TestClient(off):
        assert off.state.scheduler.running is False
