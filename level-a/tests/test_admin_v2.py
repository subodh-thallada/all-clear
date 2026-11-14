"""The long run's admin controls: recall options (speed, held slice, held
order system change), release of held parties, the clock, the order system buttons
(proxied to a mock order system behind httpx.MockTransport; a switch owes the
distributor a contract probe), the supervisor and the saboteur switches;
reset (the order systems too) and the planner's wake-ups on a refused close (API and
command line) and on a party created by a claim."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from levela import clock, runs
from levela.api.main import create_app
from levela.clock import Clock
from levela.db import seed
from levela.db.seed import load_scenario, start_recall
from levela.ledger import submit_claim

ADMIN = {"X-Admin-Token": "test-admin-token"}
PORTS = {8101: "north", 8102: "east"}
LONG_SCHEDULE = {"sim_start": 9.0, "hold_slice_in_demo": True, "order_system_change_at": {"north": 81.5, "east": 82.0}}


@pytest.fixture
def order_system_log():
    return []


@pytest.fixture
def down():
    return set()


def order_system_http(order_system_log: list, down: set) -> httpx.Client:
    """Both mock order systems behind one MockTransport: every request is logged; a
    order system named in `down` refuses the connection."""
    def handler(request: httpx.Request) -> httpx.Response:
        name = PORTS[request.url.port]
        order_system_log.append((name, request.method, request.url.path, request.headers.get("X-Admin-Token"),
                           json.loads(request.content) if request.content else None))
        if name in down:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def app(settings, client, order_system_log, down):
    return create_app(settings, client=client, sim_network=object(), http=order_system_http(order_system_log, down))


@pytest.fixture
def api(app):
    return TestClient(app)


@pytest.fixture
def rid(client, db):
    return start_recall(client, db, load_scenario("recall_1"), all_sim=True)["recall_id"]


def pin(db, recall_id, hour):
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, recall_id, Clock(real_start=t0, sim_start=hour, paused_at=t0))
    clock.forget()


def party(db, rid, party_id):
    return db.parties.find_one({"recall_id": rid, "party_id": party_id})


def texts(db, kind):
    return [line["text"] for line in db.feed.find({"kind": kind}).sort("at", 1)]


@pytest.fixture
def long_scenario(monkeypatch):
    """recall_long as far as these endpoints care: recall_1's parties with a
    long-run schedule (the generated scenario belongs to the simulator)."""
    real_load, real_list = seed.load_scenario, seed.list_scenarios

    def load(name):
        if name != "recall_long":
            return real_load(name)
        scenario = copy.deepcopy(real_load("recall_1"))
        scenario.update(scenario_id="recall_long", schedule=LONG_SCHEDULE)
        scenario["recall"]["_id"] = "RCL-LONG"
        return scenario

    monkeypatch.setattr(seed, "load_scenario", load)
    monkeypatch.setattr(seed, "list_scenarios", lambda: sorted({*real_list(), "recall_long"}))


def test_long_run_options_then_release_by_party_and_by_slice(api, db, long_scenario):
    r = api.post("/admin/recalls", headers=ADMIN, json={"scenario": "recall_long", "all_sim": True, "mode": "demo",
                                                        "seconds_per_sim_hour": 15})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["recall_id"], out["mode"]) == ("RCL-LONG", "demo")
    assert out["run"]["seconds_per_sim_hour"] == 15 and out["run"]["sim_start_label"] == "d0 09:00"
    assert out["run"]["order_system_change_at"] == {"north": None, "east": None}           # by hand in demo mode
    run = runs.get_run(db, "RCL-LONG")
    assert run["schedule"]["order_system_change_at"] == {"north": 81.5, "east": 82.0}
    assert {s["party_id"]: s["hold_for_demo"] for s in db.parties.find({"recall_id": "RCL-LONG"})} == {
        "harbour-market": True, "queen-st-grocer": True, "lakeside-foods": True}

    pin(db, "RCL-LONG", 9.5)
    r = api.post("/admin/release", headers=ADMIN, json={"recall_id": "RCL-LONG", "party_id": "harbour-market"})
    assert r.status_code == 200
    assert {k: r.json()[k] for k in ("parties", "unheld", "next_action_at")} == {
        "parties": ["harbour-market"], "unheld": ["harbour-market"], "next_action_at": 9.5}
    harbour = party(db, "RCL-LONG", "harbour-market")
    assert (harbour["hold_for_demo"], harbour["next_action_at"]) == (False, 9.5)
    assert db.worker_state.find_one({"_id": "sim_release:RCL-LONG:harbour-market"})["released_at"]
    pin(db, "RCL-LONG", 10.0)
    again = api.post("/admin/release", headers=ADMIN, json={"recall_id": "RCL-LONG", "party_id": "harbour-market"})
    assert again.json()["unheld"] == [] and party(db, "RCL-LONG", "harbour-market")["next_action_at"] == 9.5

    r = api.post("/admin/release", headers=ADMIN, json={"recall_id": "RCL-LONG", "party_id": "slice"})
    assert (r.json()["parties"], r.json()["unheld"]) == (
        ["corner-mart", "harbour-market", "lakeside-foods", "queen-st-grocer"], ["lakeside-foods", "queen-st-grocer"])
    assert db.parties.count_documents({"recall_id": "RCL-LONG", "hold_for_demo": True}) == 0
    [corner] = db.recalls.find_one({"_id": "RCL-LONG"})["directory"]
    assert corner["hold_for_demo"] is False                                   # found later, created unheld
    assert db.worker_state.count_documents({"_id": {"$regex": "^sim_release:RCL-LONG:"}}) == 4
    assert texts(db, "sim_released") == [
        "Released Harbour Market to the timers: due now at d0 09:30",
        "Released the held reply from Harbour Market",
        "Released the live slice (4 parties): 2 due now at d0 10:00"]
    assert api.post("/admin/release", headers=ADMIN,
                    json={"recall_id": "RCL-LONG", "party_id": "nowhere"}).status_code == 404

    sim = api.post("/admin/recalls", headers=ADMIN, json={"scenario": "recall_long", "all_sim": True,
                                                          "recall_id": "RCL-LONG-SIM"}).json()
    assert sim["mode"] == "sim" and sim["run"]["order_system_change_at"] == {"north": 81.5, "east": 82.0}
    assert db.parties.count_documents({"recall_id": "RCL-LONG-SIM", "hold_for_demo": True}) == 0
    assert api.post("/admin/recalls", headers=ADMIN, json={"scenario": "recall_long", "recall_id": "X",
                                                           "seconds_per_sim_hour": 0}).status_code == 422


def test_clock_pause_speed_resume_and_start(api, db, client, rid, order_system_log):
    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"order_system": "north"}})
    assert api.post("/admin/clock", headers=ADMIN, json={"recall_id": rid}).status_code == 400
    assert api.post("/admin/clock", headers=ADMIN, json={"recall_id": "RCL-NOPE", "action": "pause"}).status_code == 404

    paused = api.post("/admin/clock", headers=ADMIN, json={"recall_id": rid, "action": "pause"}).json()
    assert paused["paused"] is True and paused["order_systems"] == {}
    frozen = paused["sim_now"]
    assert 9.0 <= frozen < 9.5
    clock.forget()                         # this process cached the running clock when the recall started
    assert clock.sim_now(db, rid, client) == pytest.approx(frozen)

    faster = api.post("/admin/clock", headers=ADMIN, json={"recall_id": rid, "seconds_per_sim_hour": 15}).json()
    assert (faster["paused"], faster["clock"]["seconds_per_sim_hour"]) == (True, 15)
    assert faster["sim_now"] == pytest.approx(frozen)                         # re-anchored: no jump
    assert faster["order_systems"]["north"]["ok"] is True
    assert order_system_log == [("north", "POST", "/_admin/clock", "test-admin-token", {"seconds_per_sim_hour": 15})]

    resumed = api.post("/admin/clock", headers=ADMIN, json={"recall_id": rid, "action": "resume"}).json()
    assert resumed["paused"] is False and frozen <= resumed["sim_now"] < frozen + 0.5
    started = api.post("/admin/clock", headers=ADMIN, json={"recall_id": rid, "action": "start"}).json()
    assert started["paused"] is False and started["clock"]["paused_total_s"] == 0
    assert started["clock"]["sim_start"] == pytest.approx(started["sim_now"], abs=0.01)
    assert started["sim_now"] >= resumed["sim_now"]

    lines = texts(db, "clock_changed")
    assert lines[0] == f"Clock: paused at {clock.fmt(frozen)}"
    assert lines[1] == "Clock: 15 s per simulated hour"
    assert lines[2].startswith("Clock: running from d0 ") and len(lines) == 4
    assert db.feed.find_one({"kind": "clock_changed"})["system"] == "supervisor"


def test_order_system_buttons_are_proxied_to_the_mock_order_system(api, db, client, rid, order_system_log, down):
    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"order_system": "north"}})
    assert api.post("/admin/order-system", json={"order_system": "north", "action": "switch_v2"}).status_code == 401

    r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "switch_v2"})
    assert r.status_code == 200 and r.json()["recalls"] == [rid]
    assert order_system_log[-1] == ("north", "POST", "/_admin/version", "test-admin-token", {"version": 2})
    changed = runs.get_run(db, rid)["events"]["order_system_changed"]["north"]
    assert changed["by"] == "admin"                                           # the scheduler will not switch it again
    [line] = db.feed.find({"kind": "order_system_switched"})
    assert (line["text"], line["system"], line["recall_id"]) == (
        "North orders switched to v2 by the operator", "North orders", rid)

    r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "fault",
                                                       "fault": "accepted_then_dropped", "count": 2})
    assert r.status_code == 200
    assert order_system_log[-1] == ("north", "POST", "/_admin/fault", "test-admin-token",
                              {"fault": "accepted_then_dropped", "count": 2})
    assert texts(db, "order_system_fault") == ["North orders: fault accepted_then_dropped armed (2)"]

    calls = len(order_system_log)
    for body in ({"order_system": "north", "action": "fault", "fault": "melt"}, {"order_system": "north", "action": "fault"},
                 {"order_system": "west", "action": "switch_v2"}):
        assert api.post("/admin/order-system", headers=ADMIN, json=body).status_code == 400, body
    assert api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "reboot"}).status_code == 422
    assert len(order_system_log) == calls

    down.add("east")
    r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "east", "action": "switch_v2"})
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
    [failed] = db.feed.find({"kind": "order_system_admin_failed"})
    assert failed["level"] == "bad" and failed["system"] == "East orders"


def test_supervisor_and_saboteur_switches(api, app, db, client, rid):
    r = api.post("/admin/supervisor", headers=ADMIN, json={"recall_id": rid, "paused": True, "reason": "judge's call"})
    assert r.json() == {"recall_id": rid, "supervisor": {"paused": True, "kills_paused_reason": "judge's call"}}
    assert runs.get_run(db, rid)["supervisor"] == {"paused": True, "kills_paused_reason": "judge's call"}
    api.post("/admin/supervisor", headers=ADMIN, json={"recall_id": rid, "paused": False})
    assert runs.get_run(db, rid)["supervisor"]["paused"] is False
    assert texts(db, "supervisor") == ["Random kills paused: judge's call", "Random kills resumed"]

    before = clock.server_now(client)
    r = api.post("/admin/saboteur", headers=ADMIN, json={"recall_id": rid, "on": True, "seconds": 20})
    assert r.status_code == 200
    saboteur = runs.get_run(db, rid)["saboteur"]
    assert saboteur["on"] is True
    assert before + timedelta(seconds=15) < saboteur["until"] < before + timedelta(seconds=40)
    assert api.get("/wall/state").json()["run"]["saboteur"] == {"on": True}
    [line] = db.feed.find({"kind": "saboteur"})
    assert line["system"] == "gate" and line["text"].startswith("Saboteur on for 20 s")

    api.post("/admin/saboteur", headers=ADMIN, json={"recall_id": rid, "on": False})
    assert runs.get_run(db, rid)["saboteur"]["on"] is False and runs.get_run(db, rid)["saboteur"]["until"] is None
    app.state.wall_cache.clear()
    assert api.get("/wall/state").json()["run"]["saboteur"] == {"on": False}

    for path, body in (("/admin/supervisor", {"recall_id": "RCL-NOPE", "paused": True}),
                       ("/admin/saboteur", {"recall_id": "RCL-NOPE", "on": True})):
        assert api.post(path, headers=ADMIN, json=body).status_code == 404
    assert api.post("/admin/saboteur", headers=ADMIN, json={"recall_id": rid, "on": True,
                                                            "seconds": 0}).status_code == 422


# -- a switch owes a contract probe ------------------------------------------------------------

def test_a_switch_wakes_the_order_systems_distributor_with_a_contract_probe_owed(api, db, rid):
    """Detection needs an order system call; with its accounts reconciled (or read
    today) the distributor would make none, so the switch makes it due now."""
    for sid, order_system in (("queen-st-grocer", "north"), ("lakeside-foods", "east")):
        db.parties.update_one({"recall_id": rid, "party_id": sid},
                             {"$set": {"order_system": order_system, "channel": "order_system", "next_action_at": None}})
    pin(db, rid, 50.0)
    r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "switch_v2"})
    assert r.status_code == 200 and r.json()["probes_owed"] == {rid: 1}
    queen = party(db, rid, "queen-st-grocer")
    assert (queen["next_action_at"], queen["order_system_probe_due"], queen["order_system_probes"]) == (50.0, True, 1)
    assert db.feed.find_one({"kind": "order_system_switched"})["detail"]["probes_owed"] == 1
    assert "order_system_probes" not in party(db, rid, "lakeside-foods"), "only the switched order system's distributor"

    # An earlier timer stands; each switch owes one more probe; arming a fault owes none.
    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"next_action_at": 40.0}})
    api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "switch_v2"})
    queen = party(db, rid, "queen-st-grocer")
    assert (queen["next_action_at"], queen["order_system_probes"]) == (40.0, 2)
    r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "fault", "fault": "stale_read"})
    assert r.json()["probes_owed"] == {} and party(db, rid, "queen-st-grocer")["order_system_probes"] == 2


# -- reset resets the order systems ------------------------------------------------------------------

def test_reset_also_resets_every_mock_order_system_and_names_one_it_could_not(api, db, rid, order_system_log, down):
    down.add("east")
    r = api.post("/admin/reset", headers=ADMIN)
    assert r.status_code == 200
    out = r.json()
    assert out["removed"]["recalls"] == 1
    assert out["order_systems"]["north"] == {"ok": True, "status": 200, "body": {"ok": True}, "error": None}
    assert out["order_systems"]["east"]["ok"] is False and "unreachable" in out["order_systems"]["east"]["error"]
    assert [(p, m, path, token) for p, m, path, token, _ in order_system_log] == [
        ("east", "POST", "/_admin/reset", "test-admin-token"), ("north", "POST", "/_admin/reset", "test-admin-token")]
    [line] = db.feed.find({"kind": "order_system_admin_failed"})
    assert (line["text"], line["level"], line["system"]) == (
        "East orders: reset failed, order system unreachable: ConnectError. Restart it before the next run",
        "bad", "East orders")


# -- the planner wakes on a refused close and on a party created by a claim -----------------------

def test_a_refused_close_wakes_the_planner(api, db, rid):
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"planner_wake": False}})
    r = api.post("/admin/close", headers=ADMIN, json={"recall_id": rid})
    assert r.json()["closed"] is False
    run = runs.get_run(db, rid)
    assert (run["planner_wake"], run["planner_wake_reason"]) == (True, f"close refused: {r.json()['open_units']} open units")


def test_a_claim_that_creates_a_party_wakes_the_planner_in_its_own_transaction(client, db, rid):
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"planner_wake": False}})
    plain = submit_claim(client, db, recall_id=rid, party_id="harbour-market", attempt_id="form-hm", source="form",
                         raw={"held": 40})
    assert plain["accepted"] and runs.get_run(db, rid)["planner_wake"] is False, "no party created, no wake"
    forwarded = submit_claim(client, db, recall_id=rid, party_id="queen-st-grocer", attempt_id="form-qsg",
                             source="form", raw={"held": 80, "forwarded_qty": 40,
                                                 "forwarded_to_name": "Corner Mart"})
    assert forwarded["accepted"] and forwarded["discovered"] == "corner-mart"
    run = runs.get_run(db, rid)
    assert (run["planner_wake"], run["planner_wake_reason"]) == (True, "parties_created")


# -- the command line's reset and close ---------------------------------------------------------

def test_the_cli_close_wakes_the_planner_and_the_cli_reset_resets_the_order_systems(
        settings, client, db, rid, monkeypatch, capsys, order_system_log, down):
    import levela.__main__ as cli
    from levela.api import order_admin

    monkeypatch.setattr(cli, "_ctx", lambda: (settings, client, db))
    monkeypatch.setattr(order_admin, "LazyHttp", lambda: order_system_http(order_system_log, down))
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"planner_wake": False}})
    cli.main(["close", rid])
    refused = json.loads(capsys.readouterr().out)
    assert refused["closed"] is False
    run = runs.get_run(db, rid)
    assert (run["planner_wake"], run["planner_wake_reason"]) == (True, f"close refused: {refused['open_units']} open units")

    down.add("east")
    cli.main(["reset"])
    printed = capsys.readouterr()
    out = json.loads(printed.out)
    assert out["removed"]["recalls"] == 1 and db.recalls.count_documents({"seed_batch": {"$exists": False}}) == 0
    assert (out["order_systems"]["north"]["ok"], out["order_systems"]["east"]["ok"]) == (True, False)
    assert [(p, path) for p, _, path, _, _ in order_system_log] == [("east", "/_admin/reset"), ("north", "/_admin/reset")]
    assert "East orders was not reset (order system unreachable: ConnectError): restart it before the next run" in printed.err
