"""API: the voice tool, the callback and report tools, admin, the wall's
state JSON and the simulator's telephony endpoints, driven through FastAPI's
TestClient against a scratch database. The mass balance is asserted after
every ledger write."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from levela import __version__, clock, runs
from levela.api import wall
from levela.api.deps import MAX_TOOL_BODY, jsonable, read_capped
from levela.api.main import create_app
from levela.clock import Clock
from levela.config import DEV_SECRETS, Settings
from levela.db.seed import load_scenario, start_recall
from levela.ledger import metrics
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.orchestrator.failpoints import armed
from levela.providers.base import PlacedCall
from tests.support import HELD_EVIDENCE, SOLD_EVIDENCE, party_doc, recall_doc

TOOL = {"X-Tool-Secret": "test-tool-secret"}
ADMIN = {"X-Admin-Token": "test-admin-token"}
STATE_KEYS = {"now", "recall", "recalls", "metrics", "parties", "edges", "feed", "worker", "calls", "failpoints",
              "badges", "memory_ablation", "run", "systems", "workers", "calls_in_flight"}
PARTY_KEYS = {"party_id", "name", "type", "tier", "channel", "state", "shipped_in", "sold", "on_shelf", "held",
              "returned", "forwarded", "unresolved", "batch_code", "batch_seen", "sold_source", "block_test",
              "held_by_system", "sold_by_till", "system_on_shelf", "discovered_by", "link_id", "sub", "escalation",
              "live_attempt", "attempts", "memory", "found_via", "ladder_step", "next_action_at", "next_label",
              "hold_for_demo", "slice", "order_system", "priority", "open_questions", "answered", "simulated", "tag",
              "lease", "in_flight", "connected"}
FEED_KEYS = {"at", "kind", "text", "level", "source", "party_id", "detail", "system", "sim_label"}
RUN_KEYS = {"sim_now", "sim_label", "day", "days_total", "real_elapsed_s", "seconds_per_sim_hour", "paused",
            "parties", "checked", "steps", "kills", "duplicates", "model_calls", "tokens_per_decision",
            "invariants", "supervisor", "saboteur"}


class StubNetwork:
    """Stands in for providers.sim.SimNetwork: records calls, places nothing."""

    def __init__(self):
        self.calls = []

    def place_call(self, *, to_number, dynamic_variables, attempt_id, faults=None):
        self.calls.append({"to_number": to_number, "dynamic_variables": dynamic_variables,
                           "attempt_id": attempt_id, "faults": faults})
        return PlacedCall(conversation_id=f"conv_sim_{attempt_id}", call_sid=f"CAsim{attempt_id}")


@pytest.fixture
def stub():
    return StubNetwork()


@pytest.fixture
def app(settings, client, stub):
    return create_app(settings, client=client, sim_network=stub)


@pytest.fixture
def api(app):
    return TestClient(app)


@pytest.fixture
def rid(client, db):
    return start_recall(client, db, load_scenario("recall_1"), all_sim=True)["recall_id"]


def dial(db, recall_id, party_id, *, purpose="initial", **context):
    """An attempt the worker has opened and dialled (state dialed)."""
    party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id})
    att = open_attempt(db, recall_id=recall_id, party_id=party_id, channel="sim", purpose=purpose,
                       to_number=party["phone"], context={"ask_delivery_note": "no", **context})
    mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CA{att['_id']}")
    return db.attempts.find_one({"_id": att["_id"]})


def tool_body(att, **claim):
    return {"attempt_id": att["_id"], "party_id": att["party_id"], "system__conversation_id": att["conversation_id"],
            "system__call_sid": att["call_sid"], **claim}


def say(api, att, **claim):
    """One submit_count tool call, as ElevenLabs would send it."""
    r = api.post("/tools/submit_count", json=tool_body(att, **claim), headers=TOOL)
    assert r.status_code == 200, r.text
    return r.json()


def lines(db, kind=None):
    """Feed lines, oldest first."""
    return list(db.feed.find({"kind": kind} if kind else {}, {"_id": 0}).sort("at", 1))


def texts(db, kind=None):
    return [line["text"] for line in lines(db, kind)]


def pin_clock(db, recall_id, hour):
    """Freeze the run's simulated clock at `hour`, so simulated times are exact.
    Every process caches clocks briefly; drop them all so the app sees it now."""
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, recall_id, Clock(real_start=t0, sim_start=hour, seconds_per_sim_hour=30.0, paused_at=t0))
    clock.forget()


# ---- the voice tool -------------------------------------------------------------

def test_third_rejection_stops_the_call_and_hands_the_party_to_a_person(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market")
    r1 = say(api, att, on_shelf=60)
    assert (r1["challenges_left"], r1["stop"]) == (1, False)
    r2 = say(api, att, on_shelf=70)
    assert (r2["challenges_left"], r2["stop"]) == (0, False)
    assert "last time" in r2["message"]
    assert_balance(rid)

    r3 = say(api, att, on_shelf=80)
    assert (r3["accepted"], r3["challenges_left"], r3["stop"]) == (False, 0, True)
    assert "Do not ask again" in r3["message"] and "person from the recall team" in r3["message"]
    assert "end the call" in r3["message"]
    assert_balance(rid)
    assert db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})["escalation"] == "human"
    assert len(lines(db, "needs_human")) == 1

    again = say(api, att, on_shelf=80)          # the same delivery again: no second hand-off line
    assert again["duplicate"] is True and again["stop"] is True
    assert len(lines(db, "needs_human")) == 1

    # The cap stops the questions, not the ledger: a valid answer is still booked.
    r4 = say(api, att, on_shelf=35, returned=5)
    assert r4["accepted"] is True
    assert_balance(rid)


def test_a_valid_third_answer_is_accepted_without_escalation(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market")
    say(api, att, on_shelf=60)
    say(api, att, on_shelf=70)
    r = say(api, att, on_shelf=35, returned=5)
    assert (r["accepted"], r["stop"], r["challenges_left"]) == (True, False, 0)
    assert_balance(rid)
    assert "escalation" not in db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})
    assert lines(db, "needs_human") == []


def test_bad_or_missing_tool_secret_is_refused(api, db, rid):
    att = dial(db, rid, "harbour-market")
    assert api.post("/tools/submit_count", json=tool_body(att, on_shelf=25)).status_code == 401
    r = api.post("/tools/submit_count", json=tool_body(att, on_shelf=25), headers={"X-Tool-Secret": "nope"})
    assert r.status_code == 401
    assert db.tool_events.count_documents({}) == 0 and db.claims.count_documents({}) == 0


def test_unset_secrets_fail_closed_and_the_public_dev_values_need_an_opt_in(client, db, rid):
    """With TOOL_SECRET and ADMIN_TOKEN unset the public dev values used to be
    filled in silently, so anyone who read the repository could book claims
    or reset the run on a host that forgot a secret."""
    unset = Settings.from_env(env={"TOOL_SECRET": "", "ADMIN_TOKEN": "", "ELEVEN_WEBHOOK_SECRET": ""})
    assert (unset.tool_secret, unset.webhook_secret, unset.admin_token) == ("", "", "")
    assert Settings.from_env(env={}).admin_token == ""
    dev = Settings.from_env(env={"LEVELA_DEV": "1"})
    assert (dev.tool_secret, dev.webhook_secret, dev.admin_token) == (
        DEV_SECRETS["tool_secret"], DEV_SECRETS["webhook_secret"], DEV_SECRETS["admin_token"])
    assert Settings.from_env(env={"LEVELA_DEV": "1", "ADMIN_TOKEN": "real-token"}).admin_token == "real-token"
    assert Settings.from_env(env={"LEVELA_DEV": "0"}).tool_secret == ""

    host = TestClient(create_app(replace(unset, atlas_uri="unused", db_name=db.name, scheduler_enabled=False),
                                 client=client, sim_network=StubNetwork()))
    att = dial(db, rid, "harbour-market")
    r = host.post("/tools/submit_count", json=tool_body(att, on_shelf=25),
                  headers={"X-Tool-Secret": DEV_SECRETS["tool_secret"]})
    assert r.status_code == 401
    assert host.post("/admin/reset", headers={"X-Admin-Token": DEV_SECRETS["admin_token"]}).status_code == 401
    assert host.get("/admin/status", params={"key": DEV_SECRETS["admin_token"]}).status_code == 401
    assert db.claims.count_documents({}) == 0 and db.recalls.count_documents({"_id": rid}) == 1


def test_tool_bodies_are_read_after_the_secret_and_only_up_to_their_cap(api, db, rid):
    att = dial(db, rid, "harbour-market")
    padded = {**tool_body(att, on_shelf=25), "padding": "x" * MAX_TOOL_BODY}
    assert api.post("/tools/submit_count", json=padded, headers=TOOL).status_code == 413
    assert api.post("/tools/submit_count", json=padded).status_code == 401
    big_report = {"recall_id": rid, "party_id": "harbour-market", "source": "rep", "evidence_id": "op-x",
                  "delivery_note": "x" * MAX_TOOL_BODY}
    assert api.post("/tools/report", json=big_report, headers=TOOL).status_code == 413
    assert api.post("/tools/report", json=big_report).status_code == 401
    assert db.tool_events.count_documents({}) == 0 and db.claims.count_documents({}) == 0


def _request(chunks: list[bytes], headers: dict | None = None):
    """A Starlette request over a fake ASGI receive that counts its calls."""
    calls = []

    async def receive():
        calls.append(1)
        body = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}
    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"",
             "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
    return Request(scope, receive), calls


def test_a_body_is_read_only_up_to_its_cap():
    """An oversized body is abandoned once past the cap (a declared one before
    any of it is read), never buffered whole the way request.body() does."""
    req, calls = _request([b"x" * 1000] * 64)
    assert asyncio.run(read_capped(req, 4096)) is None and len(calls) == 5
    req, calls = _request([b"x" * 10], {"content-length": "999999"})
    assert asyncio.run(read_capped(req, 4096)) is None and calls == []
    req, calls = _request([b"x"], {"content-length": "9" * 5000})
    assert asyncio.run(read_capped(req, 4096)) is None and calls == []
    req, _ = _request([b"ab", b"cd"], {"content-length": "4"})
    assert asyncio.run(read_capped(req, 4096)) == b"abcd"


def test_a_count_too_large_for_the_database_is_a_rejection_not_a_500(api, db, rid, assert_balance):
    """99999999999999999999 is valid JSON; it used to make the BSON encoder
    raise OverflowError while logging the tool event, a 500 the agent cannot read."""
    att = dial(db, rid, "lakeside-foods")
    body = json.dumps(tool_body(att, returned=60)).replace("}", ', "on_shelf": 99999999999999999999}')
    r = api.post("/tools/submit_count", headers={**TOOL, "Content-Type": "application/json"}, content=body)
    assert r.status_code == 200, r.text
    reply = r.json()
    assert (reply["accepted"], reply["stop"]) == (False, False) and "is not a count" in reply["reason"]
    [event] = db.tool_events.find({"attempt_id": att["_id"]})
    assert event["payload"]["on_shelf"] == "99999999999999999999"
    assert db.claims.find_one({"attempt_id": att["_id"]})["code"] == "bad_number"
    assert_balance(rid)


def test_a_direct_party_that_received_a_transfer_keeps_a_zero_receipt(api, db, rid, assert_balance):
    queen = dial(db, rid, "queen-st-grocer")
    assert say(api, queen, on_shelf=110, forwarded_qty=10, forwarded_to_name="Lakeside Foods")["accepted"]
    lake = dial(db, rid, "lakeside-foods")
    r = say(api, lake, received_qty=0, on_shelf=20, returned=60)    # "we never got any from them"
    assert r["accepted"] is True
    edge = db.edges.find_one({"recall_id": rid, "to_party": "lakeside-foods"})
    assert (edge["recipient_qty"], edge["state"], edge["disputed_qty"]) == (0, "disputed", 10)
    assert db.claims.find_one({"attempt_id": lake["_id"]})["raw"]["received_qty"] == 0
    assert_balance(rid)


def test_malformed_body_is_a_400(api):
    r = api.post("/tools/submit_count", content=b"not json", headers=TOOL)
    assert r.status_code == 400
    assert api.post("/tools/submit_count", json=[1, 2], headers=TOOL).status_code == 400


def test_unknown_attempt_or_wrong_party_is_logged_and_stops(api, db, rid):
    r = api.post("/tools/submit_count", headers=TOOL,
                 json={"attempt_id": "att_00000000000000", "party_id": "harbour-market", "on_shelf": 25})
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["stop"]) == (False, True)
    assert db.tool_events.find_one({"attempt_id": "att_00000000000000"})["recall_id"] is None

    att = dial(db, rid, "harbour-market")
    r = api.post("/tools/submit_count", headers=TOOL, json={**tool_body(att, on_shelf=25), "party_id": "lakeside-foods"})
    assert (r.json()["accepted"], r.json()["stop"]) == (False, True)
    assert db.claims.count_documents({}) == 0
    assert db.tool_events.count_documents({}) == 2
    errors = lines(db, "error")
    assert len(errors) == 2 and all(line["level"] == "bad" for line in errors)


def test_receipt_without_transfer_stops_but_an_idle_zero_receipt_is_ignored(api, db, rid, assert_balance):
    harbour = dial(db, rid, "harbour-market")
    r = say(api, harbour, on_shelf=35, returned=5, received_qty=0)
    assert r["accepted"] is True
    assert_balance(rid)
    assert "received_qty" not in db.claims.find_one({"attempt_id": harbour["_id"]})["raw"]

    lakeside = dial(db, rid, "lakeside-foods")
    r = say(api, lakeside, received_qty=5, on_shelf=80)
    assert (r["accepted"], r["stop"]) == (False, True)
    assert_balance(rid)
    assert texts(db, "claim_rejected") == [
        "Lakeside Foods - rejected: no sender has reported a transfer to this party yet"]


def test_delivery_note_is_required_when_the_attempt_asks_for_it(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market", ask_delivery_note="yes")
    r = say(api, att, on_shelf=35, returned=5)
    assert (r["accepted"], r["stop"], r["challenges_left"]) == (False, False, 2)
    assert "delivery note" in r["message"]
    r = say(api, att, on_shelf=35, returned=5, delivery_note="DN-48213")
    assert r["accepted"] is True
    assert_balance(rid)


# ---- admin ----------------------------------------------------------------------

def test_admin_needs_the_token(api):
    assert api.post("/admin/reset").status_code == 401
    assert api.post("/admin/reset", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert api.get("/admin/status", params={"key": "wrong"}).status_code == 401
    assert api.get("/admin/status", params={"key": "test-admin-token"}).status_code == 200
    assert api.get("/admin/status", headers=ADMIN).status_code == 200


def test_admin_starts_a_recall_and_replies_with_form_urls(api, db, settings, monkeypatch):
    for name in ("PHONE_HARBOUR", "PHONE_CORNER"):
        monkeypatch.delenv(name, raising=False)
    r = api.post("/admin/recalls", json={"scenario": "recall_1", "all_sim": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["recall_id"] == "RCL-2026-017" and out["mode"] == "sim"
    tokens = {s["party_id"]: s["form_token"] for s in db.parties.find({"recall_id": "RCL-2026-017"})}
    assert {s["party_id"]: s["form_url"] for s in out["parties"]} == {
        sid: f"{settings.webhook_base}/form/{tok}" for sid, tok in tokens.items()}
    assert lines(db, "recall_started")

    assert api.post("/admin/recalls", json={"scenario": "recall_1", "all_sim": True}, headers=ADMIN).status_code == 409
    assert api.post("/admin/recalls", json={"scenario": "nope"}, headers=ADMIN).status_code == 400
    assert api.post("/admin/recalls", json={"scenario": "../recall_1"}, headers=ADMIN).status_code == 400
    r = api.post("/admin/recalls", json={"scenario": "recall_1", "recall_id": "RCL-VOICE"}, headers=ADMIN)
    assert r.status_code == 400 and "is a voice party" in r.json()["detail"]
    r = api.post("/admin/recalls", json={"scenario": "recall_1", "all_sim": True, "mode": "bogus",
                                         "recall_id": "RCL-B"}, headers=ADMIN)
    assert r.status_code == 400


def test_release_followups_and_failpoints(api, db, rid):
    r = api.post("/admin/release", json={"recall_id": rid, "party_id": "queen-st-grocer"}, headers=ADMIN)
    assert r.status_code == 200
    assert db.worker_state.find_one({"_id": f"sim_release:{rid}:queen-st-grocer"})["released_at"]
    assert texts(db, "sim_released") == ["Released the held reply from Queen St Grocer"]
    assert api.post("/admin/release", json={"recall_id": "RCL-NOPE", "party_id": "x"},
                    headers=ADMIN).status_code == 404

    assert api.post("/admin/followups", json={"recall_id": rid}, headers=ADMIN).status_code == 200
    assert db.recalls.find_one({"_id": rid})["followups_released"] is True
    assert len(lines(db, "followups_released")) == 1

    r = api.post("/admin/failpoint", json={"name": "after_dial_request", "party_id": "corner-mart",
                                           "channel": "voice"}, headers=ADMIN)
    assert r.status_code == 200
    spec = armed(db)["after_dial_request"]
    assert (spec["party_id"], spec["channel"], spec["by"]) == ("corner-mart", "voice", "admin")
    assert r.json()["armed"]["after_dial_request"]["party_id"] == "corner-mart"
    [line] = lines(db, "failpoint_armed")
    assert line["text"] == "Failpoint after_dial_request armed (corner-mart, voice)" and line["level"] == "warn"
    assert api.post("/admin/failpoint", json={"name": "bogus"}, headers=ADMIN).status_code == 400
    assert api.post("/admin/failpoint", json={"name": "after_intent", "channel": "fax"},
                    headers=ADMIN).status_code == 400

    r = api.post("/admin/failpoint", json={"name": None}, headers=ADMIN)
    assert r.json() == {"armed": {}} and armed(db) == {}
    assert texts(db, "failpoint_armed")[-1] == "All failpoints disarmed"


def test_status_and_reset(api, db, rid):
    dial(db, rid, "harbour-market")
    status = api.get("/admin/status", headers=ADMIN).json()
    assert [r["_id"] for r in status["recalls"]] == [rid]
    assert "directory" not in status["recalls"][0]          # resolved phone numbers stay private
    assert [a["party_id"] for a in status["live_attempts"]] == ["harbour-market"]
    assert status["sim_calls"] == {"total": 0, "by_status": {}}

    r = api.post("/admin/reset", headers=ADMIN)
    assert r.status_code == 200 and r.json()["removed"]["recalls"] == 1
    assert db.recalls.count_documents({}) == 0 and db.attempts.count_documents({}) == 0


# ---- the wall -------------------------------------------------------------------

def test_wall_shows_the_newest_runtime_recall_by_default(api, app, client, db):
    empty = api.get("/wall/state").json()
    assert (empty["recall"], empty["metrics"], empty["parties"]) == (None, None, [])

    t0 = datetime.now(timezone.utc)
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, now=t0)
    start_recall(client, db, load_scenario("recall_2"), all_sim=True, now=t0 + timedelta(seconds=1))
    db.recalls.insert_one({**recall_doc("RCL-2025-001", units_total=10), "batch_code": "OLD-1", "open_units": 0,
                           "status": "closed", "seed_batch": "history", "opened_at": t0 + timedelta(days=1)})
    app.state.wall_cache.clear()

    state = api.get("/wall/state").json()
    assert state["recall"]["_id"] == "RCL-2026-018"
    assert [r["_id"] for r in state["recalls"]] == ["RCL-2026-018", "RCL-2026-017"]
    assert api.get("/wall/state", params={"recall_id": "RCL-2026-017"}).json()["recall"]["_id"] == "RCL-2026-017"
    unknown = api.get("/wall/state", params={"recall_id": "RCL-NOPE"}).json()
    assert (unknown["recall"], unknown["parties"]) == (None, [])


def test_wall_state_adds_real_calls_only_for_the_admin_key(api, app, client, db, rid):
    """The words of a real call and the number dialled reach a screen holding
    the admin key; the public body, cached and shared, never carries them."""
    party = db.parties.find_one({"recall_id": rid})["party_id"]
    voice = open_attempt(db, recall_id=rid, party_id=party, channel="voice", to_number="+16479361710")
    complete(db, voice["_id"], "completed")
    db.attempts.update_one({"_id": voice["_id"]}, {"$set": {
        "transcript": [{"role": "agent", "message": "Hello, this is the recall line."},
                       {"role": "user", "message": "We sold twenty to Corner Mart."}],
        "call_duration_secs": 37}})
    open_attempt(db, recall_id=rid, party_id=party, channel="sim", to_number="+14165550142")
    app.state.wall_cache.clear()

    assert "real_calls" not in api.get("/wall/state").json()
    assert "real_calls" not in api.get("/wall/state", headers={"X-Admin-Token": "wrong"}).json()
    calls = api.get("/wall/state", headers={"X-Admin-Token": "test-admin-token"}).json()["real_calls"]
    assert [(c["_id"], c["party_id"], c["phone_tail"], c["call_duration_secs"]) for c in calls] == [
        (voice["_id"], party, "1710", 37)]
    assert calls[0]["transcript"][1] == {"role": "user", "message": "We sold twenty to Corner Mart."}
    assert "to_number" not in calls[0]
    assert "real_calls" not in api.get("/wall/state").json()    # the cached public body is unchanged


def test_wall_page_and_screen_assets(monkeypatch, tmp_path, settings, client):
    monkeypatch.setattr(wall, "SCREEN_DIR", tmp_path / "missing")
    bare = TestClient(create_app(settings, client=client, sim_network=StubNetwork()))
    r = bare.get("/wall")
    assert r.status_code == 404 and "index.html" in r.text
    assert bare.get("/screen/assets/index.js").status_code == 404

    (tmp_path / "index.html").write_text("<!doctype html><title>All Clear</title>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index.css").write_text("body{margin:0}", encoding="utf-8")
    monkeypatch.setattr(wall, "SCREEN_DIR", tmp_path)
    built = TestClient(create_app(settings, client=client, sim_network=StubNetwork()))
    r = built.get("/wall")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "All Clear" in r.text
    assert built.get("/screen/assets/index.css").text == "body{margin:0}"
    r = built.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/wall"


def test_wall_state_cache_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(wall.time, "monotonic", lambda: clock[0])
    cache, builds = wall.StateCache(0.5), []

    def build():
        builds.append(clock[0])
        return {"n": len(builds)}

    assert cache.get("a", build) == {"n": 1}
    clock[0] += 0.4
    assert cache.get("a", build) == {"n": 1}
    assert cache.get("b", build) == {"n": 2}
    clock[0] += 0.2
    assert cache.get("a", build) == {"n": 3}


def test_healthz(api, settings):
    assert api.get("/healthz").json() == {"ok": True, "db": settings.db_name, "version": __version__}


# ---- simulator telephony --------------------------------------------------------

def test_sim_calls_place_and_list(api, db, stub):
    body = {"to_number": "+14165550142", "attempt_id": "att_1", "dynamic_variables": {"party_id": "queen-st-grocer"}}
    assert api.post("/sim/calls", json=body).status_code == 401
    r = api.post("/sim/calls", json=body, headers=TOOL)
    assert r.json() == {"success": True, "message": "Simulated call placed", "conversation_id": "conv_sim_att_1",
                        "callSid": "CAsimatt_1"}
    assert stub.calls == [{"to_number": "+14165550142", "dynamic_variables": {"party_id": "queen-st-grocer"},
                           "attempt_id": "att_1", "faults": None}]
    api.post("/sim/calls", json={**body, "faults": {"no_answer": True}}, headers=TOOL)
    assert stub.calls[-1]["faults"] == {"no_answer": True}
    assert api.post("/sim/calls", json={"attempt_id": "att_1"}, headers=TOOL).status_code == 422

    t0 = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    db.sim_calls.insert_many([
        {"_id": "CAsim1", "to": "+14165550142", "status": "completed", "start_time": t0},
        {"_id": "CAsim2", "to": "+14165550142", "status": "in-progress", "start_time": t0 + timedelta(minutes=5)},
        {"_id": "CAsim3", "to": "+14165550143", "status": "completed", "start_time": t0 + timedelta(minutes=9)},
    ])
    assert api.get("/sim/calls").status_code == 401
    listed = api.get("/sim/calls", params={"to": "+14165550142"}, headers=TOOL).json()
    assert [c["sid"] for c in listed["calls"]] == ["CAsim2", "CAsim1"] and listed["next_page_uri"] is None
    since = api.get("/sim/calls", params={"to": "+14165550142", "since": "2026-11-14T10:01:00Z"}, headers=TOOL)
    assert [c["sid"] for c in since.json()["calls"]] == ["CAsim2"]
    unencoded = api.get("/sim/calls?to=+14165550142", headers=TOOL).json()     # "+" arrives as a space
    assert [c["sid"] for c in unencoded["calls"]] == ["CAsim2", "CAsim1"]
    assert api.get("/sim/calls", params={"since": "yesterday"}, headers=TOOL).status_code == 400

    one = api.get("/sim/calls/CAsim3", headers=TOOL).json()
    assert (one["sid"], one["status"], one["start_time"]) == ("CAsim3", "completed", "2026-11-14T10:09:00+00:00")
    assert api.get("/sim/calls/CAnope", headers=TOOL).status_code == 404


def test_sim_network_is_built_lazily_on_first_use(monkeypatch, settings, client):
    built = []

    class FakePoster:
        def __init__(self, base_url, tool_secret):
            self.args = (base_url, tool_secret)

    class FakeNetwork(StubNetwork):
        def __init__(self, db, poster, settings_):
            super().__init__()
            built.append((db.name, poster.args, settings_))

    fake = types.ModuleType("levela.providers.sim")
    fake.SimNetwork, fake.HttpPoster = FakeNetwork, FakePoster
    monkeypatch.setitem(sys.modules, "levela.providers.sim", fake)

    api = TestClient(create_app(settings, client=client))
    assert built == []
    body = {"to_number": "+14165550142", "attempt_id": "att_1"}
    assert api.post("/sim/calls", json=body, headers=TOOL).json()["success"] is True
    assert api.post("/sim/calls", json=body, headers=TOOL).json()["success"] is True
    assert built == [(settings.db_name, (settings.api_base_url, settings.tool_secret), settings)]


# ---- v2: the wall's run block, systems, workers and calls in flight ---------------

# ---- v2: request_callback ---------------------------------------------------------

def callback(api, att, **when):
    r = api.post("/tools/request_callback", headers=TOOL, json={"attempt_id": att["_id"],
                                                                "party_id": att["party_id"], **when})
    assert r.status_code == 200, r.text
    return r.json()


def test_request_callback_sets_the_attempts_callback_hour(api, db, rid):
    pin_clock(db, rid, 57.25)                                   # d2 09:15
    att = dial(db, rid, "lakeside-foods")

    r = callback(api, att, hour_of_day=9, day_offset=1)
    assert r == {"accepted": True, "callback_at": 81.0, "callback_label": "d3 09:00",
                 "message": "Callback booked for tomorrow at 09:00. Confirm the time, thank them and end the call."}
    row = db.attempts.find_one({"_id": att["_id"]})
    assert (row["callback_at"], row["state"]) == (81.0, "connected")
    assert db.tool_events.find_one({"attempt_id": att["_id"]})["kind"] == "callback"
    [line] = lines(db, "callback_requested")
    assert line["text"] == "Lakeside Foods asked to be called back tomorrow at 09:00 (d3 09:00)"
    assert (line["system"], line["sim_label"]) == ("phone", "d2 09:15")

    assert callback(api, att, hour_of_day=8)["callback_at"] == 80.0          # the next 08:00
    assert callback(api, att, callback_at="60.5")["callback_label"] == "d2 12:30"
    assert db.attempts.find_one({"_id": att["_id"]})["callback_at"] == 60.5
    for when, why in (({"callback_at": 50}, "has already passed"), ({"hour_of_day": 25}, "hour_of_day"),
                      ({"hour_of_day": 9, "day_offset": 1.5}, "day_offset"), ({}, "give callback_at"),
                      ({"callback_at": 57.25 + 24 * 15}, "within 14 days"), ({"callback_at": True}, "number")):
        r = callback(api, att, **when)
        assert (r["accepted"], r["stop"]) == (False, False) and why in r["message"], (when, r)
    assert db.attempts.find_one({"_id": att["_id"]})["callback_at"] == 60.5

    unknown = api.post("/tools/request_callback", headers=TOOL,
                       json={"attempt_id": "att_nope", "party_id": "lakeside-foods", "hour_of_day": 9}).json()
    assert (unknown["accepted"], unknown["stop"]) == (False, True)
    assert api.post("/tools/request_callback", json={"attempt_id": att["_id"]}).status_code == 401


# ---- v2: report -------------------------------------------------------------------

def report(api, **body):
    r = api.post("/tools/report", headers=TOOL, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_oversized_body_is_refused_before_any_route_runs(api):
    """/admin/* parses its JSON body before checking the token, so the size cap
    is a middleware: an anonymous 2 MB body never reaches a route."""
    huge = b"{" + b" " * (2 * 1024 * 1024) + b"}"
    resp = api.post("/admin/reset", content=huge, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert api.post("/admin/reset", json={}).status_code in (401, 403)


# ---- the food recall: the voice tool, the wall, the close gate ---------------------

def test_spec_8_harbour_market_says_60_for_its_40_and_atlas_refuses(api, db, rid, assert_balance):
    """The first answer is a lie (60 tins for a 40-tin store). The reply names
    the two values that disagree with a one-line reason; the raw Atlas error
    reaches the feed; the corrected count is accepted and a redelivery posts once."""
    att = dial(db, rid, "harbour-market")
    r = say(api, att, sold=0, on_shelf=60, held=0, returned=0, forwarded_qty=0)
    assert r["accepted"] is False and r["stop"] is False and r["challenges_left"] == 1
    assert r["values"] == {"reported": 60, "shipped_in": 40} and r["pair"] == [60, 40]
    assert r["two_values"] == [{"field": "reported", "value": 60}, {"field": "shipped_in", "value": 40}]
    assert r["reason"] == "reported 60, shipped_in 40" and "\n" not in r["reason"]
    assert "60" in r["message"] and "40" in r["message"] and "Challenge 1 of 2" in r["message"]
    assert_balance(rid)
    claim = db.claims.find_one({"attempt_id": att["_id"]})
    assert (claim["status"], claim["code"], claim["source"]) == ("rejected", "over_count", "voice")
    [rejected] = lines(db, "claim_rejected")
    assert rejected["text"] == ("Harbour Market - REJECTED by Atlas: Document failed validation on parties. "
                                "reported 60, shipped_in 40")
    assert rejected["detail"] == claim["atlas_error"] and rejected["detail"]["code"] == 121
    assert rejected["system"] == "ledger" and rejected["level"] == "bad"

    ok = say(api, att, sold=10, sold_source="till_report", on_shelf=0, held=0, returned=30, forwarded_qty=0)
    assert ok["accepted"] is True and "two_values" not in ok
    assert texts(db, "claim_accepted") == ["Harbour Market - accepted: 10 sold, 30 returned, sold counted from the till"]
    again = say(api, att, sold=10, sold_source="till_report", on_shelf=0, held=0, returned=30, forwarded_qty=0)
    assert again["duplicate"] is True
    assert db.claims.count_documents({"attempt_id": att["_id"], "status": "accepted"}) == 1
    assert_balance(rid)


def test_spec_12_the_slice_on_the_wall_in_the_food_domain(api, app, db, rid, assert_balance):
    """Four-party slice: the wall's headline, the three metrics, SIMULATED
    beside simulated parties, the systems lines and the screen text."""
    assert say(api, dial(db, rid, "harbour-market"), sold=10, sold_source="till_report", returned=30)["accepted"]
    queen = say(api, dial(db, rid, "queen-st-grocer"), sold=50, **SOLD_EVIDENCE, held=30, **HELD_EVIDENCE,
                forwarded_qty=40, forwarded_to_name="Corner Mart")
    assert queen["accepted"], queen
    assert say(api, dial(db, rid, "lakeside-foods"), sold=20, **SOLD_EVIDENCE, held=60, **HELD_EVIDENCE)["accepted"]
    corner = say(api, dial(db, rid, "corner-mart"), received_qty=25, on_shelf=25)
    assert corner["accepted"], corner
    assert_balance(rid)
    accepted = texts(db, "claim_accepted")
    assert ("Queen St Grocer - accepted: 50 sold, 30 held, 40 passed to Corner Mart, batch B-4471, "
            "sold counted from the till, till refused the sale: yes") in accepted
    assert "New party discovered: Corner Mart (named by Queen St Grocer)" in texts(db, "party_discovered")
    assert any("15 tins disputed" in t for t in texts(db, "edge_disputed"))

    app.state.wall_cache.clear()
    state = api.get("/wall/state").json()
    m = state["metrics"]
    assert state["headline"] == "NOT ALL CLEAR: 40 tins open (25 still on a shelf, 15 disputed)"
    assert (m["tins_located"], m["held_or_returned"], m["open_units"]) == ([225, 240], [120, 240], 40)
    assert (m["on_shelf"], m["disputed_units"], m["not_located"]) == (25, 15, 0)
    assert m["mass_balance"] == 240
    by_id = {p["party_id"]: p for p in state["parties"]}
    assert set(by_id["corner-mart"]) == PARTY_KEYS
    assert all(p["simulated"] and p["tag"] == "SIMULATED" for p in state["parties"])    # all_sim run
    assert (by_id["queen-st-grocer"]["sold"], by_id["queen-st-grocer"]["held"]) == (50, 30)
    assert any(line["tag"] == "SIMULATED" for line in state["feed"] if line["party_id"])
    systems = {s["system"]: s for s in state["systems"]}
    assert systems["phone"]["line"] == "4 attempts, 4 accepted claims"
    assert systems["stores"]["line"] == "0 store events, 0 complaints, 0 signals approved"
    assert state["monitoring"]["line"].startswith("Stores connected 0      tins on a shelf there 0")
    screen = state["screen"]
    assert screen[0] == "All Clear   Recall of batch B-4471   240 tins"
    assert screen[1] == state["headline"]
    assert "Located 225 of 240 (94%)      Held or returned 120 of 240 (50%)" in screen
    assert "Systems" in screen and "Feed, newest first" in screen
    text = api.get("/wall/text").text
    assert state["headline"] in text and "SIMULATED" in text

    r = api.post("/admin/close", headers=ADMIN, json={"recall_id": rid})
    assert r.status_code == 200 and r.json()["closed"] is False
    assert any("40 tins open" in t for t in texts(db, "close_refused"))


def test_the_one_strip_and_the_agents_line_read_store_records_and_agent_parties(api, app, db, rid):
    """Connected stores, tins on a shelf there, moved to held, the last store
    event; and the agents line from a party with channel agent and its sub."""
    db.store_systems.insert_one({"_id": "ss-corner", "store_label": "Corner Mart", "party_id": "harbour-market",
                                 "connects": ["till", "stock"], "blocked": True})
    at = datetime.now(timezone.utc)
    db.store_events.insert_one({"_id": "ev-1", "store_system_id": "ss-corner", "event_id": "t-88213",
                                "kind": "quarantine", "batch_code": "B-4471", "qty": 1, "at": at})
    db.parties.insert_one(party_doc(rid, "summit-foods", 120, channel="agent", name="Summit Foods",
                                    link_id="ln-summit", sub={"seq": 14, "parties_total": 7, "parties_open": 3,
                                                              "state": "open",
                                                              "buyers": {"identified": 19, "told": 15,
                                                                         "unreachable": 4, "anonymous_sales": 12}}))
    app.state.wall_cache.clear()
    state = api.get("/wall/state").json()
    strip = state["monitoring"]
    assert (strip["connected"], strip["on_shelf"], strip["moved_to_held"]) == (1, 0, 0)
    assert strip["last_event"]["text"] == "Harbour Market: quarantine 1, event t-88213"
    assert strip["line"] == ("Stores connected 1      tins on a shelf there 0      moved to held 0      "
                             "last store event: Harbour Market: quarantine 1, event t-88213")
    systems = {s["system"]: s for s in state["systems"]}
    assert systems["stores"]["line"] == "1 store event, 0 complaints, 0 signals approved"
    assert systems["agents"]["line"] == ("Summit Foods: 0 reports accepted. 120 tins, 0 on a shelf. "
                                         "Buyers told 15 of 19. Names not shared")
    summit = next(p for p in state["parties"] if p["party_id"] == "summit-foods")
    assert summit["simulated"] is False and summit["sub"]["buyers"]["told"] == 15


def test_wall_tolerates_missing_store_collections(api, app, db, rid):
    for name in ("store_systems", "store_events", "complaints", "signals"):
        db[name].drop()
    app.state.wall_cache.clear()
    state = api.get("/wall/state").json()
    assert state["monitoring"]["connected"] == 0 and state["headline"].startswith("NOT ALL CLEAR: 240 tins open")


def test_describe_counts_speaks_tins_and_checks():
    from levela.api.tools import describe_counts
    raw = {"sold": 50, "held": 30, "forwards": [{"to_name": "Corner Mart", "qty": 25}, {"to_name": "Bloor Foods",
                                                                                        "qty": 15}],
           "batch_seen": "B-4471", "sold_source": "till", "block_test": "yes"}
    assert describe_counts(raw) == ("50 sold, 30 held, 25 passed to Corner Mart, 15 passed to Bloor Foods, "
                                    "batch B-4471, sold counted from the till, till refused the sale: yes")
    assert describe_counts({}) == "no tins sold, on the shelf, held or returned"


def test_report_from_a_distributors_rep_books_through_the_claim_path(api, db, rid, assert_balance):
    body = {"recall_id": rid, "party_id": "lakeside-foods", "source": "rep", "evidence_id": "op-rep-1",
            "sold": 20, "sold_source": "till_report", "held": 60, "batch_seen": "B-4471", "block_test": True}
    r = api.post("/tools/report", headers=TOOL, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    assert "Lakeside Foods: the distributor's rep reported the counts" in texts(db, "report_received")
    over = api.post("/tools/report", headers=TOOL, json={**body, "evidence_id": "op-rep-2", "held": 70}).json()
    assert over["accepted"] is False and over["pair"] == [90, 80]
    assert_balance(rid)
