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

TOOL = {"X-Tool-Secret": "test-tool-secret"}
ADMIN = {"X-Admin-Token": "test-admin-token"}
STATE_KEYS = {"now", "recall", "recalls", "metrics", "parties", "edges", "feed", "worker", "calls", "failpoints",
              "badges", "memory_ablation", "run", "systems", "workers", "calls_in_flight"}
PARTY_KEYS = {"party_id", "name", "tier", "channel", "state", "shipped_in", "on_hand", "sold", "returned",
              "destroyed", "forwarded", "unresolved", "discovered_by", "escalation", "live_attempt",
              "attempts", "memory", "level", "found_via", "ladder_step", "next_action_at", "next_label",
              "hold_for_demo", "slice", "order_system", "priority", "open_questions", "answered"}
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

def test_voice_path_rejects_60_then_accepts_and_ignores_the_duplicate(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market")

    r = say(api, att, held=60)
    assert {k: v for k, v in r.items() if k != "message"} == {
        "accepted": False, "reason": "60 reported, 40 shipped", "reported_total": 60, "shipped_in": 40,
        "challenges_left": 1, "stop": False}
    assert "60" in r["message"] and "40" in r["message"] and "Challenge 1 of 2" in r["message"]
    assert_balance(rid)

    claim = db.claims.find_one({"attempt_id": att["_id"]})
    assert (claim["status"], claim["code"], claim["source"]) == ("rejected", "over_count", "voice")
    assert claim["atlas_error"]["code"] == 121
    assert claim["atlas_error"]["codeName"] == "DocumentValidationFailure"
    assert claim["atlas_error"]["errInfo"]["failingDocumentId"] == f"{rid}:harbour-market"
    [rejected] = lines(db, "claim_rejected")
    assert rejected["text"] == "Harbour Market - Rejected by Atlas: 60 reported, 40 shipped"
    assert rejected["level"] == "bad"
    assert rejected["detail"] == claim["atlas_error"]

    [event] = db.tool_events.find({"attempt_id": att["_id"]})
    assert event["kind"] == "tool_call" and event["recall_id"] == rid and event["source"] == "voice"
    assert event["conversation_id"] == att["conversation_id"] and event["call_sid"] == att["call_sid"]
    assert event["payload"] == tool_body(att, held=60)
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "connected"
    assert db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})["state"] == "reached"

    # Counts arrive from an LLM as strings and integral floats; the ledger coerces them.
    r = say(api, att, held=35.0, returned=5)
    assert r == {"accepted": True, "message": "Recorded. Thank them and move on.", "challenges_left": 1,
                 "stop": False}
    assert_balance(rid)
    assert texts(db, "claim_accepted") == ["Harbour Market - accepted: 25 on hand, 10 sold, 5 returned"]
    party = db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})
    assert (party["on_hand"], party["sold"], party["returned"], party["unresolved"]) == (25, 10, 5, 0)

    r = say(api, att, held=35, returned=5)
    assert r["accepted"] is True and r["duplicate"] is True
    assert_balance(rid)
    assert texts(db, "claim_duplicate") == ["Duplicate delivery ignored for Harbour Market (same dedupe key)"]
    assert db.claims.count_documents({"attempt_id": att["_id"], "status": "accepted"}) == 1


def test_forwarding_discovers_corner_properties_and_the_receipt_disputes_15(api, db, rid, assert_balance):
    queen = dial(db, rid, "queen-st-grocer")
    r = say(api, queen, held=80, forwarded_qty=40, forwarded_to_name="Corner Mart", source="sim")
    assert r["accepted"] is True
    assert_balance(rid)
    assert db.claims.find_one({"attempt_id": queen["_id"]})["source"] == "sim"
    assert db.tool_events.find_one({"attempt_id": queen["_id"]})["source"] == "sim"

    corner = db.parties.find_one({"recall_id": rid, "party_id": "corner-mart"})
    assert (corner["tier"], corner["discovered_by"], corner["shipped_in"]) == ("downstream", "claim:queen-st-grocer", 0)
    newest = [line["kind"] for line in db.feed.find({}, {"kind": 1}).sort("at", -1).limit(3)]
    assert newest == ["edge_opened", "party_discovered", "claim_accepted"]
    assert texts(db, "claim_accepted") == [
        "Queen St Grocer - accepted: 50 on hand, 30 sold, 40 forwarded to Corner Mart"]
    [found] = lines(db, "party_discovered")
    assert found["text"] == "New party discovered: Corner Mart (named by Queen St Grocer)"
    assert found["party_id"] == "corner-mart"
    assert texts(db, "edge_opened") == [
        "Transfer opened: Queen St Grocer -> Corner Mart, 40 tins awaiting Corner Mart's count"]

    mart = dial(db, rid, "corner-mart")
    r = say(api, mart, received_qty=25, held=25)
    assert r["accepted"] is True
    assert_balance(rid)
    assert texts(db, "claim_accepted")[-1] == "Corner Mart - accepted: received 25, 25 on hand"
    [disputed] = lines(db, "edge_disputed")
    assert disputed["text"] == "Transfer disputed: Queen St Grocer says 40, Corner Mart says 25 - 15 tins disputed"
    assert disputed["level"] == "bad"
    edge = db.edges.find_one({"recall_id": rid})
    assert (edge["state"], edge["disputed_qty"], edge["corroborated_qty"]) == ("disputed", 15, 25)


def test_third_rejection_stops_the_call_and_hands_the_party_to_a_person(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market")
    r1 = say(api, att, held=60)
    assert (r1["challenges_left"], r1["stop"]) == (1, False)
    r2 = say(api, att, held=70)
    assert (r2["challenges_left"], r2["stop"]) == (0, False)
    assert "last time" in r2["message"]
    assert_balance(rid)

    r3 = say(api, att, held=80)
    assert (r3["accepted"], r3["challenges_left"], r3["stop"]) == (False, 0, True)
    assert "Do not ask again" in r3["message"] and "person from the recall team" in r3["message"]
    assert "end the call" in r3["message"]
    assert_balance(rid)
    assert db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})["escalation"] == "human"
    assert len(lines(db, "needs_human")) == 1

    again = say(api, att, held=80)          # the same delivery again: no second hand-off line
    assert again["duplicate"] is True and again["stop"] is True
    assert len(lines(db, "needs_human")) == 1

    # The cap stops the questions, not the ledger: a valid answer is still booked.
    r4 = say(api, att, held=35, returned=5)
    assert r4["accepted"] is True
    assert_balance(rid)


def test_a_valid_third_answer_is_accepted_without_escalation(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market")
    say(api, att, held=60)
    say(api, att, held=70)
    r = say(api, att, held=35, returned=5)
    assert (r["accepted"], r["stop"], r["challenges_left"]) == (True, False, 0)
    assert_balance(rid)
    assert "escalation" not in db.parties.find_one({"recall_id": rid, "party_id": "harbour-market"})
    assert lines(db, "needs_human") == []


def test_bad_or_missing_tool_secret_is_refused(api, db, rid):
    att = dial(db, rid, "harbour-market")
    assert api.post("/tools/submit_count", json=tool_body(att, held=25)).status_code == 401
    r = api.post("/tools/submit_count", json=tool_body(att, held=25), headers={"X-Tool-Secret": "nope"})
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
    r = host.post("/tools/submit_count", json=tool_body(att, held=25),
                  headers={"X-Tool-Secret": DEV_SECRETS["tool_secret"]})
    assert r.status_code == 401
    assert host.post("/admin/reset", headers={"X-Admin-Token": DEV_SECRETS["admin_token"]}).status_code == 401
    assert host.get("/admin/status", params={"key": DEV_SECRETS["admin_token"]}).status_code == 401
    assert db.claims.count_documents({}) == 0 and db.recalls.count_documents({"_id": rid}) == 1


def test_tool_bodies_are_read_after_the_secret_and_only_up_to_their_cap(api, db, rid):
    att = dial(db, rid, "harbour-market")
    padded = {**tool_body(att, held=25), "padding": "x" * MAX_TOOL_BODY}
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
    body = json.dumps(tool_body(att, returned=60)).replace("}", ', "on_hand": 99999999999999999999}')
    r = api.post("/tools/submit_count", headers={**TOOL, "Content-Type": "application/json"}, content=body)
    assert r.status_code == 200, r.text
    reply = r.json()
    assert (reply["accepted"], reply["stop"]) == (False, False) and "is not a count" in reply["reason"]
    [event] = db.tool_events.find({"attempt_id": att["_id"]})
    assert event["payload"]["on_hand"] == "99999999999999999999"
    assert db.claims.find_one({"attempt_id": att["_id"]})["code"] == "bad_number"
    assert_balance(rid)


def test_leaving_out_a_counted_transfer_stops_the_call_without_using_a_challenge(api, db, rid, assert_balance):
    queen = dial(db, rid, "queen-st-grocer")
    assert say(api, queen, held=80, forwarded_qty=40, forwarded_to_name="Corner Mart")["accepted"]
    corner = dial(db, rid, "corner-mart")
    assert say(api, corner, received_qty=25, held=25)["accepted"]
    assert_balance(rid)

    complete(db, queen["_id"], "completed")
    followup = dial(db, rid, "queen-st-grocer", purpose="dispute_followup")
    r = say(api, followup, held=120)                     # "none went to Corner Mart"
    assert (r["accepted"], r["stop"], r["challenges_left"]) == (False, True, 2)
    assert "end the call" in r["message"] and "Challenge" not in r["message"]
    r = say(api, followup, held=90, forwarded_qty=30, forwarded_to_name="Bloor Alarm")
    assert (r["accepted"], r["stop"]) == (False, True)
    assert db.claims.find_one({"attempt_id": followup["_id"], "code": "forward_changed"}) is not None
    assert "escalation" not in db.parties.find_one({"recall_id": rid, "party_id": "queen-st-grocer"})
    assert_balance(rid)


def test_a_direct_party_that_received_a_transfer_keeps_a_zero_receipt(api, db, rid, assert_balance):
    queen = dial(db, rid, "queen-st-grocer")
    assert say(api, queen, held=110, forwarded_qty=10, forwarded_to_name="Lakeside Foods")["accepted"]
    lake = dial(db, rid, "lakeside-foods")
    r = say(api, lake, received_qty=0, held=20, returned=60)    # "we never got any from them"
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
                 json={"attempt_id": "att_00000000000000", "party_id": "harbour-market", "held": 25})
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["stop"]) == (False, True)
    assert db.tool_events.find_one({"attempt_id": "att_00000000000000"})["recall_id"] is None

    att = dial(db, rid, "harbour-market")
    r = api.post("/tools/submit_count", headers=TOOL, json={**tool_body(att, held=25), "party_id": "lakeside-foods"})
    assert (r.json()["accepted"], r.json()["stop"]) == (False, True)
    assert db.claims.count_documents({}) == 0
    assert db.tool_events.count_documents({}) == 2
    errors = lines(db, "error")
    assert len(errors) == 2 and all(line["level"] == "bad" for line in errors)


def test_receipt_without_transfer_stops_but_an_idle_zero_receipt_is_ignored(api, db, rid, assert_balance):
    harbour = dial(db, rid, "harbour-market")
    r = say(api, harbour, held=35, returned=5, received_qty=0)
    assert r["accepted"] is True
    assert_balance(rid)
    assert "received_qty" not in db.claims.find_one({"attempt_id": harbour["_id"]})["raw"]

    lakeside = dial(db, rid, "lakeside-foods")
    r = say(api, lakeside, received_qty=5, held=80)
    assert (r["accepted"], r["stop"]) == (False, True)
    assert_balance(rid)
    assert texts(db, "claim_rejected") == [
        "Lakeside Foods - rejected: no sender has reported a transfer to this party yet"]


def test_delivery_note_is_required_when_the_attempt_asks_for_it(api, db, rid, assert_balance):
    att = dial(db, rid, "harbour-market", ask_delivery_note="yes")
    r = say(api, att, held=35, returned=5)
    assert (r["accepted"], r["stop"], r["challenges_left"]) == (False, False, 2)
    assert "delivery note" in r["message"]
    r = say(api, att, held=35, returned=5, delivery_note="DN-48213")
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


def test_close_is_refused_with_15_open_then_closes_after_the_corrected_claim(api, db, rid, assert_balance):
    assert say(api, dial(db, rid, "harbour-market"), on_hand=25, sold=10, returned=5)["accepted"]
    assert_balance(rid)
    lakeside = dial(db, rid, "lakeside-foods")
    assert say(api, lakeside, held=20, returned=60)["accepted"]
    assert_balance(rid)
    queen = dial(db, rid, "queen-st-grocer")
    assert say(api, queen, held=80, forwarded_qty=40, forwarded_to_name="Corner Mart")["accepted"]
    assert_balance(rid)
    assert say(api, dial(db, rid, "corner-mart"), received_qty=25, on_hand=25)["accepted"]
    assert_balance(rid)

    r = api.post("/admin/close", json={"recall_id": rid}, headers=ADMIN)
    assert r.status_code == 200
    refused = r.json()
    assert (refused["closed"], refused["open_units"]) == (False, 15)
    assert refused["atlas_error"]["code"] == 121
    [line] = lines(db, "close_refused")
    assert line["text"] == "Close refused by Atlas: 15 open units" and line["level"] == "bad"
    assert line["detail"] == refused["atlas_error"]
    assert db.recalls.find_one({"_id": rid})["status"] == "open"

    complete(db, queen["_id"], "completed")
    followup = dial(db, rid, "queen-st-grocer", purpose="dispute_followup")
    r = say(api, followup, held=95, forwarded_qty=25, forwarded_to_name="Corner Mart")
    assert r["accepted"] is True
    assert_balance(rid)
    assert texts(db, "edge_corroborated") == [
        "Transfer corroborated: Queen St Grocer and Corner Mart agree on 25 tins"]

    r = api.post("/admin/close", json={"recall_id": rid}, headers=ADMIN)
    assert r.json() == {"closed": True, "open_units": 0}
    [closed] = lines(db, "recall_closed")
    assert closed["level"] == "good" and closed["text"] == "Recall closed: every tin is explained"
    assert db.recalls.find_one({"_id": rid})["status"] == "closed"
    assert api.post("/admin/close", json={"recall_id": rid}, headers=ADMIN).json()["already"] is True
    assert len(lines(db, "recall_closed")) == 1

    late = say(api, lakeside, held=19, returned=61)
    assert (late["accepted"], late["stop"]) == (False, True)
    assert db.claims.find_one({"attempt_id": lakeside["_id"], "status": "rejected"})["code"] == "recall_closed"
    assert api.post("/admin/close", json={"recall_id": "RCL-NOPE"}, headers=ADMIN).status_code == 404


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

def test_wall_state_has_the_design_shape_and_the_reconciler_metrics(api, app, db, rid, settings, assert_balance):
    # A long TTL: on Atlas a claim takes longer than the real half second, and
    # this test is about what a cached entry hides, not about its lifetime
    # (test_wall_state_cache_expires covers that with a fake clock).
    app.state.wall_cache = wall.StateCache(60.0)
    harbour = dial(db, rid, "harbour-market")
    say(api, harbour, held=60)
    queen = dial(db, rid, "queen-st-grocer")
    say(api, queen, held=80, forwarded_qty=40, forwarded_to_name="Corner Mart")
    assert_balance(rid)
    db.sim_calls.insert_many([
        {"_id": queen["call_sid"], "recall_id": rid, "party_id": "queen-st-grocer", "status": "completed"},
        {"_id": "CAsimlakeside", "recall_id": rid, "party_id": "lakeside-foods", "status": "in-progress"},
    ])
    now = datetime.now(timezone.utc)     # test settings call a worker dead after 1 s of silence
    db.worker_state.insert_many([
        {"_id": "worker:w-old", "boot_id": "b0", "heartbeat_at": now - timedelta(minutes=5)},
        {"_id": "worker:w1", "boot_id": "b1", "heartbeat_at": now, "resumed": {"missed": 3}, "resume_token": {"x": 1}},
        {"_id": "badge:crash_schedules", "text": "Crash schedules: 12/12 passed", "at": now},
        {"_id": "ablation", "with": 2, "without": 5},
    ])

    state = api.get("/wall/state").json()
    assert set(state) == STATE_KEYS
    assert state["recall"]["_id"] == rid
    assert set(state["recall"]) == {"_id", "batch_code", "product", "brand", "hazard", "units_total", "status", "mode",
                                    "options", "opened_at", "closed_at"}
    assert state["recalls"] == [{"_id": rid, "status": "open", "batch_code": "B-4471"}]
    assert state["metrics"] == jsonable(metrics(db, rid))
    assert state["metrics"]["mass_balance"] == 240 and state["metrics"]["open_units"] == 160

    parties = {s["party_id"]: s for s in state["parties"]}
    assert set(parties) == {"harbour-market", "queen-st-grocer", "lakeside-foods", "corner-mart"}
    assert all(set(s) == PARTY_KEYS for s in state["parties"])
    assert parties["harbour-market"]["live_attempt"] == {"_id": harbour["_id"], "state": "connected",
                                                        "purpose": "initial", "seq": 1}
    assert parties["harbour-market"]["attempts"] == 1 and parties["harbour-market"]["state"] == "reached"
    assert parties["corner-mart"]["discovered_by"] == "claim:queen-st-grocer"
    assert parties["corner-mart"]["live_attempt"] is None and parties["corner-mart"]["attempts"] == 0
    # The wall is public: no party's form token (the form's only credential) appears anywhere in it.
    tokens = [s["form_token"] for s in db.parties.find({"recall_id": rid}, {"form_token": 1})]
    published = json.dumps(state)
    assert len(tokens) == 4 and not any(tok in published for tok in tokens)
    assert "/form/" not in published
    assert state["edges"] == [{"from_party": "queen-st-grocer", "to_party": "corner-mart", "sender_qty": 40,
                               "recipient_qty": None, "corroborated_qty": 0, "disputed_qty": 0,
                               "state": "awaiting_recipient"}]

    assert state["feed"][0]["kind"] == "edge_opened" and len(state["feed"]) <= 60
    assert all(set(line) == FEED_KEYS for line in state["feed"])
    assert state["feed"][0]["system"] == "ledger" and state["feed"][0]["sim_label"].startswith("d0 ")
    assert state["worker"] == {"name": "w1", "alive": True, "heartbeat_age_s": state["worker"]["heartbeat_age_s"],
                               "boot_id": "b1", "resumed": {"missed": 3}}
    assert state["calls"] == {"total": 3, "by_party": {"harbour-market": 1, "queen-st-grocer": 1,
                                                       "lakeside-foods": 1}}
    assert state["failpoints"] == {}
    assert state["badges"] == [{"key": "crash_schedules", "text": "Crash schedules: 12/12 passed"}]
    assert state["memory_ablation"] == {"with": 2, "without": 5}

    # Cached: a new claim is not visible until the entry expires (or is cleared).
    say(api, harbour, held=35, returned=5)
    assert_balance(rid)
    cached = api.get("/wall/state").json()
    assert cached["now"] == state["now"] and cached["metrics"]["open_units"] == 160
    app.state.wall_cache.clear()
    fresh = api.get("/wall/state").json()
    assert fresh["now"] != state["now"] and fresh["metrics"]["open_units"] == 120


def test_wall_shows_the_newest_runtime_recall_by_default(api, app, client, db):
    empty = api.get("/wall/state").json()
    assert (empty["recall"], empty["metrics"], empty["parties"]) == (None, None, [])

    t0 = datetime.now(timezone.utc)
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, now=t0)
    start_recall(client, db, load_scenario("recall_2"), all_sim=True, now=t0 + timedelta(seconds=1))
    db.recalls.insert_one({"_id": "RCL-2025-001", "batch_code": "OLD-1", "units_total": 10, "open_units": 0,
                           "status": "closed", "seed_batch": "history", "opened_at": t0 + timedelta(days=1)})
    app.state.wall_cache.clear()

    state = api.get("/wall/state").json()
    assert state["recall"]["_id"] == "RCL-2026-018"
    assert [r["_id"] for r in state["recalls"]] == ["RCL-2026-018", "RCL-2026-017"]
    assert api.get("/wall/state", params={"recall_id": "RCL-2026-017"}).json()["recall"]["_id"] == "RCL-2026-017"
    unknown = api.get("/wall/state", params={"recall_id": "RCL-NOPE"}).json()
    assert (unknown["recall"], unknown["parties"]) == (None, [])


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

def test_wall_state_v2_run_systems_workers_and_calls_in_flight(settings, client, db, rid, stub, assert_balance):
    live = TestClient(create_app(replace(settings, voice_provider="elevenlabs"), client=client, sim_network=stub))
    pin_clock(db, rid, 58.0)                                    # d2 10:00
    queen = open_attempt(db, recall_id=rid, party_id="queen-st-grocer", channel="voice", to_number="+14165550142")
    mark_dialed(db, queen["_id"], conversation_id="conv_q", call_sid="CAreal1")
    queen = db.attempts.find_one({"_id": queen["_id"]})
    assert say(live, queen, held=80, forwarded_qty=40, forwarded_to_name="Corner Mart")["accepted"]
    assert_balance(rid)
    dial(db, rid, "lakeside-foods")                             # a simulated call, dialled
    db_now = clock.server_now(client)
    db.parties.update_one({"recall_id": rid, "party_id": "harbour-market"},
                         {"$set": {"lease_owner": "worker-test/w1", "lease_until": db_now + timedelta(seconds=60),
                                   "lease_epoch": 3}})
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"},           # an expired lease is not live
                         {"$set": {"lease_owner": "worker-test/w2", "lease_until": db_now - timedelta(seconds=5)}})
    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"order_system": "north"}})
    db.task_runs.insert_one({"_id": "tr-1", "worker": "worker-test/w1", "recall_id": rid,
                             "party_id": "harbour-market", "kind": "contact_party", "status": "running",
                             "started_at": db_now - timedelta(seconds=4)})
    db.adapters.insert_one({"_id": "north:v2", "order_system": "north", "version": 2, "status": "active", "source": "model"})
    db.operations.insert_many([
        {"_id": f"op-{i}", "recall_id": rid, "order_system": "north", "kind": "place_hold", "idempotency_key": f"k{i}",
         "state": state, "readback_at": 30.0, "readback_value": {"hold_id": f"H{i}"}}
        for i, state in enumerate(["verified", "verified", "needs_reverify"])] + [
        {"_id": "op-form", "recall_id": rid, "kind": "send_form_link", "party_id": "corner-mart",
         "idempotency_key": "form1", "state": "sent"}])
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {
        "steps": 41, "kills": 2, "tokens": {"last": 1850, "max": 1900, "total": 3700, "n": 2},
        "last_invariant_check": {"at": 57.0, "passed": [True, True, True, True], "details": []},
        "supervisor.paused": True, "saboteur": {"on": True, "until": db_now + timedelta(seconds=30)}}})

    state = live.get("/wall/state").json()
    assert set(state) == STATE_KEYS and set(state["run"]) == RUN_KEYS
    run = state["run"]
    assert (run["sim_now"], run["sim_label"], run["day"], run["days_total"], run["paused"]) == (
        58.0, "d2 10:00", 2, 10, True)
    assert run["parties"] == {"total": 4, "at_start": 3, "found": 1}
    assert run["checked"] == [1, 4] and (run["steps"], run["kills"], run["duplicates"]) == (41, 2, 0)
    assert run["tokens_per_decision"] == 1850 and run["seconds_per_sim_hour"] == 30.0
    assert run["invariants"] == {"at": 57.0, "at_label": "d2 09:00", "passed": [True] * 4, "ok": True}
    assert run["supervisor"] == {"paused": True} and run["saboteur"] == {"on": True}
    assert state["systems"] == [
        {"system": "phone", "attempts": 2, "accepted_claims": 1},
        {"system": "form", "attempts": 0, "accepted_claims": 0, "links_sent": 1, "links_verified": 0},
        {"system": "North orders", "adapter_version": 2, "adapter_source": "model", "ops_verified": 2,
         "ops_flagged": 1, "ops_total": 3, "accepted_claims": 0, "paused": False}]
    assert state["workers"] == [{"id": "worker-test/w1", "party_id": "harbour-market", "name": "Harbour Market",
                                 "kind": "contact_party", "since_s": state["workers"][0]["since_s"], "lease_epoch": 3}]
    assert 3 <= state["workers"][0]["since_s"] < 60
    assert state["calls_in_flight"] == {"real": 1, "simulated": 1}
    parties = {s["party_id"]: s for s in state["parties"]}
    assert (parties["corner-mart"]["level"], parties["corner-mart"]["found_via"]) == (3, "claim")
    assert (parties["corner-mart"]["next_action_at"], parties["corner-mart"]["next_label"]) == (58.0, "d2 10:00")
    accepted = next(line for line in state["feed"] if line["kind"] == "claim_accepted")
    assert (accepted["system"], accepted["sim_label"]) == ("ledger", "d2 10:00")

    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {
        "invariant_failed": True, "last_invariant_check.passed": [True, False, True, True],
        "saboteur.until": db_now - timedelta(seconds=1)}})
    live.app.state.wall_cache.clear()
    later = live.get("/wall/state").json()["run"]
    assert later["invariants"]["ok"] is False and later["saboteur"] == {"on": False}


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


def test_report_books_through_the_claim_path_and_verifies_its_evidence(api, db, rid, assert_balance):
    pin_clock(db, rid, 201.0)                                   # d8 09:00, the rep's day
    db.operations.insert_many([
        {"_id": "op-rep", "recall_id": rid, "kind": "escalate_rep", "party_id": "lakeside-foods",
         "idempotency_key": f"{rid}:lakeside-foods:escalate_rep", "state": "sent"},
        {"_id": "op-link", "recall_id": rid, "kind": "send_form_link", "party_id": "harbour-market",
         "idempotency_key": f"{rid}:harbour-market:form:3", "state": "sent"},
        {"_id": "op-link-old", "recall_id": rid, "kind": "send_form_link", "party_id": "harbour-market",
         "idempotency_key": f"{rid}:harbour-market:form:2", "state": "needs_reverify"}])

    over = report(api, recall_id=rid, party_id="lakeside-foods", source="rep", evidence_id="op-rep", held=90)
    assert (over["accepted"], over["code"], over["reported_total"], over["shipped_in"]) == (False, "over_count", 90, 80)
    assert over["verified"] == [] and db.operations.find_one({"_id": "op-rep"})["state"] == "sent"
    assert_balance(rid)

    ok = report(api, recall_id=rid, party_id="lakeside-foods", source="rep", evidence_id="op-rep",
                held=20, returned=60)
    assert ok["accepted"] is True and ok["verified"] == ["op-rep"]
    assert_balance(rid)
    claim = db.claims.find_one({"party_id": "lakeside-foods", "status": "accepted"})
    assert (claim["attempt_id"], claim["source"], str(claim["_id"])) == ("op-rep", "rep", ok["claim_id"])
    op = db.operations.find_one({"_id": "op-rep"})
    assert (op["state"], op["readback_at"], op["readback_value"]) == ("verified", 201.0, claim["_id"])
    events = list(db.tool_events.find({"attempt_id": "op-rep"}))
    assert {e["kind"] for e in events} == {"rep"} and len(events) == 2
    assert texts(db, "report_received") == ["Lakeside Foods: the distributor's rep reported the counts"] * 2
    assert lines(db, "report_received")[0]["system"] == "phone"
    assert texts(db, "claim_accepted") == ["Lakeside Foods - accepted: 20 on hand, 60 destroyed"]

    again = report(api, recall_id=rid, party_id="lakeside-foods", source="rep", evidence_id="op-rep",
                   held=20, returned=60)
    assert (again["accepted"], again["duplicate"], again["verified"]) == (True, True, [])

    form = report(api, recall_id=rid, party_id="harbour-market", source="form", evidence_id="op-link",
                  held="35", returned=5)
    assert form["accepted"] is True and form["verified"] == ["op-link"]
    assert_balance(rid)
    links = {o["_id"]: o for o in db.operations.find({"kind": "send_form_link"})}
    assert {o["state"] for o in links.values()} == {"verified"}
    assert links["op-link-old"]["readback_value"] == links["op-link"]["readback_value"]
    assert texts(db, "form_link_verified") == [
        "Harbour Market: form link verified, a submission arrived with the party's link"]

    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"order_system": "north"}})
    read = report(api, recall_id=rid, party_id="queen-st-grocer", source="order_system", evidence_id="op-accounts",
                  held=80, forwarded_qty=40, forwarded_to_name="Corner Mart")
    assert read["accepted"] is True and read["verified"] == []             # op-accounts is not an operation
    assert_balance(rid)
    assert lines(db, "report_received")[-1]["system"] == "North orders"
    assert db.tool_events.find_one({"attempt_id": "op-accounts"})["kind"] == "order_system"

    assert api.post("/tools/report", headers=TOOL, json={"recall_id": rid, "party_id": "nope", "source": "rep",
                                                         "evidence_id": "x"}).status_code == 404
    assert api.post("/tools/report", headers=TOOL, json={"recall_id": rid, "party_id": "harbour-market",
                                                         "source": "fax", "evidence_id": "x"}).status_code == 422
    assert api.post("/tools/report", json={"recall_id": rid}).status_code == 401


def test_describe_counts_names_every_destination_of_a_multi_forward():
    from levela.api.tools import describe_counts
    raw = {"on_hand": 5, "forwards": [{"to_name": "Corner Mart", "qty": 12}, {"to_party_id": "annex", "qty": 8}]}
    assert describe_counts(raw) == "5 on hand, 12 forwarded to Corner Mart, 8 forwarded to annex"


def test_an_oversized_body_is_refused_before_any_route_runs(api):
    """/admin/* parses its JSON body before checking the token, so the size cap
    is a middleware: an anonymous 2 MB body never reaches a route."""
    huge = b"{" + b" " * (2 * 1024 * 1024) + b"}"
    resp = api.post("/admin/reset", content=huge, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert api.post("/admin/reset", json={}).status_code in (401, 403)
