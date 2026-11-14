"""The screen contract (spec test 12, the screen): what web/src/data/live.js
reads from GET /wall/state, and the admin calls its buttons make.

The four-party slice (recall 1) is started through POST /admin/recalls and
booked through the voice tool, as the simulator books it: Harbour Market's
lie is refused, the truth accepted, Queen St Grocer names Corner Mart, Corner
Mart's 25 make a 15-tin dispute. Two worker processes are registered the way
levela.orchestrator.worker registers a boot; each leases a party through the
real dispatch (claim_next). One of them dies at a failpoint (the kill beat),
so the screen must show it dead with its lease still draining.

The field list is the table in docs/BUILD_PLAN.md, "Screen contract (lead
follow-up)". When live.js is present, its feed-kind tables and its admin
paths are read from the file, so a change on the screen's side that the
backend does not meet fails here.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from levela.api.main import create_app
from levela.orchestrator import claim
from levela.orchestrator.attempts import mark_connected, mark_dialed, open_attempt
from levela.orchestrator.failpoints import Failpoints, WorkerCrash, raise_crash
from tests.support import HELD_EVIDENCE, SOLD_EVIDENCE

TOOL = {"X-Tool-Secret": "test-tool-secret"}
# What live.js sends with every admin request (headers(true)).
ADMIN = {"X-Admin-Token": "test-admin-token", "Accept": "application/json", "Content-Type": "application/json"}
RID = "RCL-2026-017"
LIVE_JS = Path(__file__).resolve().parents[1] / "web" / "src" / "data" / "live.js"
LEVELA = Path(__file__).resolve().parents[1] / "levela"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

NUM = (int, float)
OPT = type(None)
# ---- the inventory: field -> accepted JSON types (docs/BUILD_PLAN.md table) ----
TOP = {"now": str, "recall": dict, "parties": list, "edges": list, "feed": list, "workers": list, "systems": list,
       "metrics": dict, "run": dict, "failpoints": dict, "calls_in_flight": dict, "refusals": list, "headline": str}
RECALL = {"_id": str, "batch_code": str, "product": str, "units_total": int, "status": str}
PARTY = {"party_id": str, "name": str, "type": str, "tier": int, "found_via": (str, OPT), "discovered_by": (str, OPT),
         "channel": str, "simulated": bool, "connected": bool, "shipped_in": int, "sold": int, "on_shelf": int,
         "held": int, "returned": int, "forwarded": int, "unresolved": int, "state": str, "ladder_step": (int, OPT),
         "next_action_at": (*NUM, OPT), "hold_for_demo": (bool, OPT), "priority": (int, OPT),
         "escalation": (str, OPT), "lease": dict, "in_flight": (str, OPT), "live_attempt": (dict, OPT)}
LEASE = {"owner": (str, OPT), "token": int, "remaining_s": (*NUM, OPT), "expired": bool}
LIVE_ATTEMPT = {"_id": str, "state": str, "since_s": (*NUM, OPT)}
WORKER = {"id": str, "party_id": (str, OPT), "lease_epoch": (int, OPT), "kind": (str, OPT), "alive": bool,
          "saboteur": bool}
FEED = {"id": str, "at": str, "kind": str, "text": str, "system": (str, OPT), "source": (str, OPT), "level": str,
        "party_id": (str, OPT), "sim_label": (str, OPT), "detail": (dict, str, list, OPT)}
RUN = {"sim_now": NUM, "sim_label": str, "day": int, "days_total": int, "real_elapsed_s": (*NUM, OPT),
       "seconds_per_sim_hour": NUM, "paused": bool, "steps": int, "kills": int, "adoptions": int, "duplicates": int,
       "tokens_per_decision": (int, OPT), "invariants": dict, "parties": dict, "saboteur": dict, "checked": list}
METRICS = {"units_total": int, "open_units": int, "on_shelf": int, "not_located": int, "disputed_units": int,
           "mass_balance": int, "checks_completed": list, "tins_located": list, "held_or_returned": list,
           "status": str}
PHONE = {"system": str, "attempts": int, "accepted_claims": int}
ORDERS = {"system": str, "adapter_version": (int, OPT), "adapter_source": (str, OPT), "status": str, "paused": bool}
# Kinds the screen routes by that no backend code writes; the screen shows
# nothing special for them until live.js maps the backend's kind (BUILD_PLAN,
# "Screen contract"): attempt_refused is written as gate_refused with
# detail.rule "intent"; kill is a `supervisor` line "worker killed (n)";
# op_refused is `atlas_rejected` (collection operations).
SCREEN_ONLY_KINDS = {"attempt_refused", "kill", "op_refused"}


def has(row: dict, spec: dict, where: str) -> None:
    for key, types in spec.items():
        assert key in row, f"{where}: missing {key!r} (live.js reads it)"
        assert isinstance(row[key], types if isinstance(types, tuple) else (types,)), \
            f"{where}.{key} is {row[key]!r}, expected {types}"


def mock_order_systems(log: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def order_log():
    return []


@pytest.fixture
def app(settings, client, order_log):
    # A worker is alive while its heartbeat is younger than this; the test's own pauses must not kill it.
    return create_app(replace(settings, worker_dead_after_s=60.0), client=client, sim_network=object(),
                      http=mock_order_systems(order_log))


@pytest.fixture
def api(app):
    return TestClient(app)


def start(api, mode="sim", **options) -> dict:
    r = api.post("/admin/recalls", headers=ADMIN, json={"scenario": "recall_1", "all_sim": True, "mode": mode,
                                                        "followups": "manual", "memory": False, **options})
    assert r.status_code == 200, r.text
    return r.json()


def dial(db, party_id, *, connect=False):
    party = db.parties.find_one({"recall_id": RID, "party_id": party_id})
    att = open_attempt(db, recall_id=RID, party_id=party_id, channel="sim", purpose="initial",
                       to_number=party.get("phone"), context={"ask_delivery_note": "no"})
    mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CAsim{att['_id']}")
    if connect:
        mark_connected(db, att["_id"])
    return db.attempts.find_one({"_id": att["_id"]})


def say(api, att, **counts) -> dict:
    body = {"attempt_id": att["_id"], "party_id": att["party_id"], "system__conversation_id": att["conversation_id"],
            "system__call_sid": att["call_sid"], **counts}
    r = api.post("/tools/submit_count", json=body, headers=TOOL)
    assert r.status_code == 200, r.text
    return r.json()


def book_slice(api, db) -> None:
    harbour = dial(db, "harbour-market")
    assert say(api, harbour, on_shelf=60)["accepted"] is False                       # the lie
    assert say(api, harbour, sold=10, sold_source="till_report", returned=30)["accepted"]
    assert say(api, dial(db, "queen-st-grocer"), sold=50, **SOLD_EVIDENCE, held=30, **HELD_EVIDENCE,
               forwarded_qty=40, forwarded_to_name="Corner Mart")["accepted"]
    assert say(api, dial(db, "lakeside-foods"), sold=20, **SOLD_EVIDENCE, held=60, **HELD_EVIDENCE)["accepted"]
    assert say(api, dial(db, "corner-mart"), received_qty=25, on_shelf=25)["accepted"]


def register_worker(db, name: str, threads: int, *, started_s_ago: float = 0.0) -> list[str]:
    """A worker boot as levela.orchestrator.worker.startup() writes it."""
    now = datetime.now(timezone.utc)
    ids = [f"{name}/w{i}" for i in range(1, threads + 1)]
    db.worker_state.update_one({"_id": f"worker:{name}"}, {"$set": {
        "boot_id": f"boot-{name}", "pid": 4242 if name.endswith("dead") else 4141, "host": "test",
        "started_at": now - timedelta(seconds=started_s_ago), "heartbeat_at": now, "workers": ids}}, upsert=True)
    return ids


def state(api, app) -> dict:
    app.state.wall_cache.clear()
    r = api.get("/wall/state", headers={"Accept": "application/json", "X-Admin-Token": "test-admin-token"})
    assert r.status_code == 200, r.text
    return r.json()


def live_js_tables() -> dict[str, set[str]] | None:
    """The feed kinds live.js routes by (KIND keys, REFUSED, SAY, ROUTES keys)."""
    if not LIVE_JS.is_file():
        return None
    src = LIVE_JS.read_text(encoding="utf-8")
    out = {}
    for name in ("KIND", "ROUTES"):
        m = re.search(rf"const {name} = \{{(.*?)\n\}};", src, re.S)
        if m:
            out[name] = set(re.findall(r"^\s*([a-z_]+):", m.group(1), re.M))
    for name in ("REFUSED", "SAY"):
        m = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", src, re.S)
        if m:
            out[name] = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    return out or None


def backend_kinds() -> set[str]:
    """Every string literal in the code that writes feed lines (levela/, and
    the supervisor script): a superset of the kinds feed.emit is given.
    levela/feed.py and levela/api/wall.py are left out: they only list kinds."""
    readers = {LEVELA / "feed.py", LEVELA / "api" / "wall.py"}
    files = [p for p in LEVELA.rglob("*.py") if p not in readers]
    files.append(SCRIPTS / "supervisor.py")
    text = "\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file())
    return set(re.findall(r"\"([a-z]+(?:_[a-z]+)*)\"", text))


# ---- tests ------------------------------------------------------------------------

def test_spec_12_wall_state_carries_every_field_the_screen_reads(api, app, db, assert_balance):
    start(api)
    book_slice(api, db)
    assert_balance(RID)

    # Two worker processes; each thread of the first is listed, idle or busy.
    alive_ids = register_worker(db, "worker-test", 4)
    dead_ids = register_worker(db, "worker-dead", 1, started_s_ago=5)
    busy = claim.claim_next(db, worker_id=alive_ids[0], sim_now=1000.0, recall_id=RID, lease_s=8)
    ghost = claim.claim_next(db, worker_id=dead_ids[0], sim_now=1000.0, recall_id=RID, lease_s=8)
    assert busy and ghost, "two due parties to lease"
    call = dial(db, busy["party_id"], connect=True)
    # The kill beat: the dead process's thread dies at the failpoint mid-call, its lease still running.
    assert api.post("/admin/failpoint", headers=ADMIN,
                    json={"name": "after_dial_request", "party_id": ghost["party_id"]}).status_code == 200
    with pytest.raises(WorkerCrash):
        Failpoints(db, env={}, worker="worker-dead", crash=raise_crash).hit(
            "after_dial_request", {"_id": "att-ghost", "recall_id": RID, "party_id": ghost["party_id"],
                                   "channel": "sim"})
    assert api.post("/admin/close", headers=ADMIN, json={"recall_id": RID}).json()["closed"] is False

    s = state(api, app)
    if os.environ.get("LEVELA_DUMP_WALL"):      # for checking live.js's normalize() against this body by hand
        Path(os.environ["LEVELA_DUMP_WALL"]).write_text(json.dumps(s), encoding="utf-8")
    has(s, TOP, "state")
    has(s["recall"], RECALL, "recall")
    assert s["recall"]["_id"] == RID and "milk" in s["recall"]["product"]
    has(s["metrics"], METRICS, "metrics")
    assert (s["metrics"]["open_units"], s["metrics"]["disputed_units"], s["metrics"]["on_shelf"]) == (40, 15, 25)
    assert all(len(s["metrics"][k]) == 2 for k in ("checks_completed", "tins_located", "held_or_returned"))
    has(s["run"], RUN, "run")
    assert isinstance(s["run"]["parties"]["at_start"], int) and isinstance(s["run"]["saboteur"]["on"], bool)
    assert {"at", "passed"} <= set(s["run"]["invariants"])
    assert set(s["calls_in_flight"]) >= {"real", "simulated"}

    parties = {p["party_id"]: p for p in s["parties"]}
    assert {"harbour-market", "queen-st-grocer", "lakeside-foods", "corner-mart"} <= set(parties)
    for pid, p in parties.items():
        has(p, PARTY, f"parties[{pid}]")
        has(p["lease"], LEASE, f"parties[{pid}].lease")
    assert (parties["queen-st-grocer"]["sold"], parties["queen-st-grocer"]["held"],
            parties["queen-st-grocer"]["forwarded"]) == (50, 30, 40)
    assert parties["corner-mart"]["found_via"] or parties["corner-mart"]["discovered_by"]
    # The busy party: its lease is live and names its owner; its call is live.
    held = parties[busy["party_id"]]
    assert held["lease"]["owner"] == alive_ids[0] and held["lease"]["remaining_s"] > 0
    assert held["lease"]["token"] == busy["lease_epoch"] and held["lease"]["expired"] is False
    has(held["live_attempt"], LIVE_ATTEMPT, "live_attempt")
    assert held["live_attempt"]["_id"] == call["_id"] and held["live_attempt"]["state"] == "connected"
    assert parties["harbour-market"]["lease"] == {"owner": None, "token": 0, "until": None, "remaining_s": None,
                                                   "expired": True} or parties["harbour-market"]["lease"]["owner"]

    workers = {w["id"]: w for w in s["workers"]}
    assert set(alive_ids) | set(dead_ids) <= set(workers), "every thread of a shown process has a row"
    for wid, w in workers.items():
        has(w, WORKER, f"workers[{wid}]")
    assert workers[alive_ids[0]]["alive"] is True and workers[alive_ids[0]]["party_id"] == busy["party_id"]
    assert workers[alive_ids[1]]["alive"] is True and workers[alive_ids[1]]["party_id"] is None     # idle
    # Dead at once (the failpoint fired in its process), its lease still draining: the screen's ghost.
    assert workers[dead_ids[0]]["alive"] is False and workers[dead_ids[0]]["party_id"] == ghost["party_id"]
    assert parties[ghost["party_id"]]["lease"]["owner"] == dead_ids[0]
    assert parties[ghost["party_id"]]["lease"]["remaining_s"] > 0

    assert len(s["feed"]) >= 8
    for line in s["feed"]:
        has(line, FEED, f"feed[{line.get('kind')}]")
    assert len({line["id"] for line in s["feed"]}) == len(s["feed"]), "feed ids are unique"
    assert [line["at"] for line in s["feed"]] == sorted((line["at"] for line in s["feed"]), reverse=True), \
        "newest first (live.js reverses it)"
    kinds = {line["kind"] for line in s["feed"]}
    assert {"claim_rejected", "claim_accepted", "party_discovered", "edge_disputed", "failpoint_fired",
            "close_refused"} <= kinds
    rejected = next(line for line in s["feed"] if line["kind"] == "claim_rejected")
    assert rejected["level"] == "bad" and re.search(r"reported\D{0,12}60", rejected["text"])
    assert re.search(r"shipped(?:_in)?\D{0,12}40", rejected["text"]), "live.js reads the two numbers from the text"

    assert {r["kind"] for r in s["refusals"]} >= {"claim_rejected", "close_refused"}
    assert all({"at", "kind", "party_id", "primitive", "collection"} <= set(r) for r in s["refusals"])
    systems = {x["system"]: x for x in s["systems"]}
    has(systems["phone"], PHONE, "systems[phone]")
    assert s["failpoints"] == {}, "the failpoint fired and disarmed itself"


def test_order_systems_carry_the_status_the_screen_draws(api, app, db):
    start(api)
    db.parties.update_one({"recall_id": RID, "party_id": "lakeside-foods"}, {"$set": {"order_system": "north"}})
    s = state(api, app)
    north = next(x for x in s["systems"] if re.match(r"^north", x["system"], re.I))
    has(north, ORDERS, "systems[North orders]")
    assert north["status"] == "ok"
    db.parties.update_one({"recall_id": RID, "party_id": "lakeside-foods"}, {"$set": {"order_system_paused": True}})
    assert next(x for x in state(api, app)["systems"] if x["system"] == "North orders")["status"] == "contract_failed"
    db.worker_state.insert_one({"_id": "repair:north", "owner": "worker-test/ab12",
                                "until": datetime.now(timezone.utc) + timedelta(seconds=60)})
    assert next(x for x in state(api, app)["systems"] if x["system"] == "North orders")["status"] == "repairing"


def test_every_admin_call_the_screen_makes_answers_2xx_with_the_admin_key(api, app, db, order_log):
    """The buttons in live.js `actions`, in the order the demo presses them."""
    start(api, mode="demo", hold_slice=True)
    s = state(api, app)
    held = {p["party_id"] for p in s["parties"] if p["hold_for_demo"]}
    assert {"harbour-market", "lakeside-foods"} <= held, "demo mode holds the slice until the judge's call"
    calls = [
        ("/admin/supervisor", {"recall_id": RID, "paused": True, "reason": "the judge's call: the kill beat uses the "
                                                                           "failpoint"}),
        ("/admin/release", {"recall_id": RID, "party_id": "harbour-market"}),
        ("/admin/release", {"recall_id": RID, "party_id": "lakeside-foods"}),
        ("/admin/failpoint", {"name": "after_dial_request"}),
        ("/admin/failpoint", {"name": None}),
        ("/admin/failpoint", {"name": "after_dial_request", "party_id": "corner-mart"}),
        ("/admin/release", {"recall_id": RID, "party_id": "corner-mart"}),
        ("/admin/release", {"recall_id": RID, "party_id": "queen-st-grocer"}),
        ("/admin/order-system", {"order_system": "north", "action": "switch_v2"}),
        ("/admin/saboteur", {"recall_id": RID, "on": True, "seconds": 20}),
        ("/admin/close", {"recall_id": RID}),
        ("/admin/clock", {"recall_id": RID, "action": "pause"}),
        ("/admin/clock", {"recall_id": RID, "action": "resume"}),
    ]
    for path, body in calls:
        r = api.post(path, headers=ADMIN, content=json.dumps(body))
        assert 200 <= r.status_code < 300, f"{path} {body}: {r.status_code} {r.text}"
        assert api.post(path, headers={k: v for k, v in ADMIN.items() if k != "X-Admin-Token"},
                        content=json.dumps(body)).status_code == 401, f"{path} needs the key"
    assert ("POST", "/_admin/version") in order_log
    s = state(api, app)
    parties = {p["party_id"]: p for p in s["parties"]}
    assert not parties["harbour-market"]["hold_for_demo"] and not parties["lakeside-foods"]["hold_for_demo"]
    assert s["run"]["saboteur"]["on"] is True and s["failpoints"].get("after_dial_request", {}).get("party_id") \
        == "corner-mart"
    assert s["run"]["paused"] is False, "paused, then resumed"
    if LIVE_JS.is_file():
        used = set(re.findall(r"'(/admin/[a-z-]+)'", LIVE_JS.read_text(encoding="utf-8")))
        known = {path for path, _ in calls} | {"/admin/recalls"}      # Start over: test_start_over_opens_a_fresh_demo_run
        assert used <= known, f"live.js posts to {sorted(used - known)}"


def test_start_over_opens_a_fresh_demo_run(api, app, db, monkeypatch):
    """Start over in live.js posts this body: the long scenario in demo mode under a new recall id,
    which the screen then shows (the newest run-time recall), with Harbour held for Call Harbour."""
    monkeypatch.setenv("PHONE_HARBOUR", "+14165550101")
    monkeypatch.setenv("PHONE_CORNER", "+14165550102")
    body = {"scenario": "recall_long", "mode": "demo", "recall_id": "RCL-2026-1114-163000"}
    no_key = {k: v for k, v in ADMIN.items() if k != "X-Admin-Token"}
    assert api.post("/admin/recalls", headers=no_key, content=json.dumps(body)).status_code == 401
    r = api.post("/admin/recalls", headers=ADMIN, content=json.dumps(body))
    assert r.status_code == 200, r.text
    assert r.json()["recall_id"] == body["recall_id"]
    s = state(api, app)
    assert s["recall"]["_id"] == body["recall_id"]
    assert {p["party_id"]: p["hold_for_demo"] for p in s["parties"]}["harbour-market"] is True
    assert api.post("/admin/recalls", headers=ADMIN, content=json.dumps(body)).status_code == 409, \
        "the same id twice is refused, so the screen makes a new id from the time"


def test_the_feed_kinds_the_screen_routes_by_are_written_by_the_backend():
    tables = live_js_tables()
    if tables is None:
        pytest.skip("web/src/data/live.js not present or its tables moved")
    screen = set().union(*(v for k, v in tables.items() if k != "ROUTES"))
    # ROUTES keys are the screen's own names (after KIND); map them back.
    kind = {}
    m = re.search(r"const KIND = \{(.*?)\n\};", LIVE_JS.read_text(encoding="utf-8"), re.S)
    if m:
        kind = dict(re.findall(r"^\s*([a-z_]+):\s*'([a-z_]+)'", m.group(1), re.M))
    screen_names = {v: k for k, v in kind.items()}
    screen |= {screen_names.get(r, r) for r in tables.get("ROUTES", set())}
    labels = LIVE_JS.parents[1] / "ui" / "labels.js"
    if labels.is_file():     # the banner's sentences, by the screen's kind names
        screen |= {screen_names.get(k, k) for k in re.findall(r"case '([a-z_]+)'", labels.read_text(encoding="utf-8"))}
    missing = screen - backend_kinds() - SCREEN_ONLY_KINDS
    assert not missing, f"live.js routes feed kinds no backend code writes: {sorted(missing)}"
