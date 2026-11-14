"""SimNetwork end to end against a scratch database.

RecordingPoster stands in for the API: it books every tool body through
levela.ledger.submit_claim exactly as POST /tools/submit_count does and keeps
every webhook, so these tests exercise the scripts, the pacing, the faults
and the payloads without depending on the API module.

The long-run tests (recall_long, profile parties) use LongRunPoster, which
also closes attempts on the post-call webhook and books form submissions
through the API's own handlers, and records the three endpoints the API adds
in V2 (submit_answer, request_callback, report). Their run clocks are fast:
a simulated hour is 0.05 s unless a test needs pacing it can measure.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from levela import signing
from levela.api.form import form_attempt, raw_from_form
from levela.api.hooks import handle_post_call
from levela.api.main import create_app
from levela.api.tools import book_claim
from levela.clock import Clock, get_clock, next_morning, server_now
from levela.db.client import run_txn
from levela.db.seed import load_scenario, start_recall
from levela.ledger import stub_party, submit_claim
from levela.ledger.transfers import open_edges, slugify
from levela.orchestrator.attempts import complete, open_attempt
from levela.providers import ClientPoster, FakeVoice, ProviderError, ProviderTimeout, SimNetwork, SimProbe, SimVoice
from levela.providers import profiles
from levela.runs import set_clock

RAW_KEYS = ("on_hand", "sold", "returned", "destroyed", "forwarded_qty", "forwarded_to_name", "received_qty",
            "delivery_note")
R1, R2 = "RCL-2026-017", "RCL-2026-018"


class RecordingPoster:
    """The API's two endpoints, reduced to what the simulator relies on."""

    def __init__(self, client, db):
        self.client, self.db = client, db
        self.tools: list[dict] = []
        self.hooks: list[dict] = []
        self._lock = threading.Lock()

    def post_tool(self, body: dict) -> dict:
        entered = time.monotonic()
        attempt = self.db.attempts.find_one({"_id": body["attempt_id"]})
        result = submit_claim(self.client, self.db, recall_id=attempt["recall_id"], party_id=body["party_id"],
                              attempt_id=body["attempt_id"], source=body["source"],
                              raw={k: body[k] for k in RAW_KEYS if body.get(k) is not None},
                              require_delivery_note=attempt["context"].get("ask_delivery_note") == "yes")
        with self._lock:
            self.tools.append({"at": entered, "body": dict(body), "result": result})
        return result

    def post_hook(self, raw: bytes, headers: dict) -> int:
        with self._lock:
            self.hooks.append({"at": time.monotonic(), "raw": raw, "headers": dict(headers),
                               "payload": json.loads(raw)})
        return 200

    def order(self) -> list[str]:
        return [t["body"]["party_id"] for t in self.tools]


@pytest.fixture
def poster(client, db):
    return RecordingPoster(client, db)


@pytest.fixture
def network(db, poster, settings):
    net = SimNetwork(db, poster, settings)
    yield net
    net.stop()
    assert net.wait_idle(5), "a player outlived the test"


def start(client, db, scenario="recall_1", **options):
    return start_recall(client, db, load_scenario(scenario), all_sim=True, env={}, **options)


def dial(provider, db, recall_id, party_id, *, purpose="initial", ask_note="no", faults=None, to=None):
    """What the worker does: open the attempt, then ask the provider (a
    SimNetwork, or a FakeVoice around one) to dial."""
    party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id})
    variables = {"party_id": party_id, "party_name": party["name"], "purpose": purpose,
                 "company": "Tamarack Distribution", "product": "Granola", "batch_spoken": "M O, four four one seven",
                 "ask_delivery_note": ask_note, "followup_note": ""}
    attempt = open_attempt(db, recall_id=recall_id, party_id=party_id, channel="sim", purpose=purpose,
                           to_number=to or party["phone"], context=variables)
    extra = {"faults": faults} if faults is not None else {}
    placed = provider.place_call(to_number=to or party["phone"], dynamic_variables=variables,
                                 attempt_id=attempt["_id"], **extra)
    return attempt, placed


def eventually(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def feed_lines(db, kind):
    return list(db.feed.find({"kind": kind}).sort("at", 1))


def add_corner_properties_stub(db, recall_id):
    """What memory does in run 2: Corner Properties exists before anyone names it."""
    recall = db.recalls.find_one({"_id": recall_id})
    entry = next(e for e in recall["directory"] if e["party_id"] == "corner-properties")
    db.parties.insert_one(stub_party(recall_id, "corner-properties", entry["name"], entry, "memory",
                                    datetime.now(timezone.utc)))


# ---------------------------------------------------------------- scripts and payloads

def test_harbour_fire_plays_its_script_in_order(client, db, network, poster, assert_balance):
    start(client, db)
    t0 = time.monotonic()
    attempt, placed = dial(network, db, R1, "harbour-fire")
    assert network.wait_idle(10)
    assert time.monotonic() - t0 < 5, "sim_time_scale 0 plays without waiting"

    first, second = poster.tools
    assert first["result"]["accepted"] is False and first["result"]["code"] == "over_count"
    assert (first["result"]["reported_total"], first["result"]["shipped_in"]) == (60, 40)
    assert second["result"]["accepted"] is True
    assert first["body"] == {"in_stock": 60, "attempt_id": attempt["_id"], "party_id": "harbour-fire",
                             "system__conversation_id": placed.conversation_id,
                             "system__call_sid": placed.call_sid, "source": "sim"}
    assert {k: second["body"][k] for k in ("on_hand", "sold", "returned")} == {"in_stock": 35, "returned": 5}
    assert db.recalls.find_one({"_id": R1})["open_units"] == 200
    assert_balance(R1)

    row = db.sim_calls.find_one({"_id": placed.call_sid})
    assert re.fullmatch(r"CA[0-9a-f]{32}", row["_id"]) and row["conversation_id"].startswith("conv_sim_")
    assert (row["status"], row["to"], row["attempt_id"], row["recall_id"], row["party_id"], row["purpose"],
            row["script_key"], row["sim"]) == ("completed", "+14165550141", attempt["_id"], R1, "harbour-fire",
                                               "initial", "initial", True)
    assert row["end_time"] >= row["start_time"]
    assert [r["say"] for r in row["replies"]] == ["We've got sixty on hand.",
                                                  "Sorry, I was reading the wrong line. Twenty-five on hand, ten "
                                                  "sold, five already sent back."]
    assert row["replies"][0]["claim"] == {"in_stock": 60}
    assert row["replies"][0]["reply"]["code"] == "over_count" and row["replies"][1]["reply"]["accepted"]

    lines = feed_lines(db, "sim_reply")
    assert [line["text"] for line in lines] == [
        'Harbour Fire (simulated): "We\'ve got sixty on hand."',
        'Harbour Fire (simulated): "Sorry, I was reading the wrong line. Twenty-five on hand, ten sold, '
        'five already sent back."']
    assert all(line["source"] == "sim" and line["attempt_id"] == attempt["_id"] for line in lines)


def test_post_call_payload_is_shaped_and_signed(client, db, network, poster, settings):
    start(client, db)
    attempt, placed = dial(network, db, R1, "harbour-fire")
    assert network.wait_idle(10)

    (hook,) = poster.hooks
    assert hook["at"] > poster.tools[-1]["at"], "the post-call follows the last reply"
    assert hook["headers"]["Content-Type"] == "application/json"
    signature = hook["headers"][signing.HEADER]
    assert signing.verify(hook["raw"], signature, settings.webhook_secret)
    assert not signing.verify(hook["raw"], signature, "some-other-secret")
    assert not signing.verify(hook["raw"] + b" ", signature, settings.webhook_secret)

    payload = hook["payload"]
    assert payload["type"] == "post_call_transcription" and isinstance(payload["event_timestamp"], int)
    data = payload["data"]
    assert (data["agent_id"], data["conversation_id"], data["status"]) == ("sim", placed.conversation_id, "done")
    meta = data["metadata"]
    assert meta["phone_call"] == {"type": "twilio", "direction": "outbound", "external_number": "+14165550141",
                                  "call_sid": placed.call_sid}
    assert isinstance(meta["start_time_unix_secs"], int) and meta["call_duration_secs"] >= 0
    variables = data["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables["attempt_id"] == attempt["_id"] and variables["party_id"] == "harbour-fire"
    assert variables["system__call_sid"] == placed.call_sid
    assert all(isinstance(v, str) for v in variables.values())
    roles = [t["role"] for t in data["transcript"]]
    assert roles == ["agent", "user", "agent", "user", "agent", "agent"]
    users = [t["message"] for t in data["transcript"] if t["role"] == "user"]
    assert users == [r["say"] for r in db.sim_calls.find_one({"_id": placed.call_sid})["replies"]]
    assert "Tamarack Distribution" in data["transcript"][0]["message"]
    assert data["transcript"][2]["tool_calls"][0]["tool_name"] == "submit_count"


def test_delays_follow_the_time_scale(client, db, poster, settings):
    net = SimNetwork(db, poster, replace(settings, sim_time_scale=0.05))
    try:
        start(client, db)
        t0 = time.monotonic()
        dial(net, db, R1, "harbour-fire")                  # replies at delay_s 2.0 and 2.0
        assert net.wait_idle(10)
    finally:
        net.stop()
    first, second = poster.tools
    assert first["at"] - t0 >= 0.1 - 0.02
    assert second["at"] - first["at"] >= 0.1 - 0.02


def test_delivery_note_only_when_asked(client, db, network, poster, assert_balance):
    start(client, db, "recall_2")
    add_corner_properties_stub(db, R2)
    dial(network, db, R2, "harbour-fire", ask_note="yes")
    dial(network, db, R2, "lakeside-systems", ask_note="no")
    dial(network, db, R2, "corner-properties", ask_note="yes")      # its answer is null: nothing to say
    dial(network, db, R2, "queen-st-alarm", ask_note="no")
    assert network.wait_idle(10)

    bodies = {t["body"]["party_id"]: t["body"] for t in poster.tools}
    assert bodies["harbour-fire"]["delivery_note"] == "DN-51877"
    assert "delivery_note" not in bodies["lakeside-systems"]
    assert "delivery_note" not in bodies["corner-properties"]
    assert "delivery_note" not in bodies["queen-st-alarm"]
    assert all(t["result"]["accepted"] for t in poster.tools), [t["result"] for t in poster.tools]
    assert db.recalls.find_one({"_id": R2})["open_units"] == 0, "recall 2 explains every panel"
    assert_balance(R2)


# ---------------------------------------------------------------- pacing

def test_demo_hold_waits_for_release(client, db, network, poster):
    out = start(client, db, mode="demo")
    assert out["mode"] == "demo" and db.recalls.find_one({"_id": R1})["mode"] == "demo"
    dial(network, db, R1, "queen-st-alarm")

    assert eventually(lambda: feed_lines(db, "sim_hold"))
    assert not network.wait_idle(0.5)
    assert poster.tools == [] and poster.hooks == []
    (hold,) = feed_lines(db, "sim_hold")
    assert hold["text"] == "Queen St Alarm's reply is held for the demo - release it when ready"
    assert hold["source"] == "sim" and hold["party_id"] == "queen-st-alarm"

    db.worker_state.update_one({"_id": f"sim_release:{R1}:queen-st-alarm"},
                               {"$set": {"released_at": datetime.now(timezone.utc)}}, upsert=True)
    assert network.wait_idle(10)
    (tool,) = poster.tools
    assert tool["result"]["accepted"] and tool["result"]["discovered"] == "corner-properties"
    assert len(feed_lines(db, "sim_hold")) == 1


def test_demo_hold_is_ignored_in_sim_mode(client, db, network, poster):
    start(client, db)
    dial(network, db, R1, "queen-st-alarm")
    assert network.wait_idle(10)
    assert len(poster.tools) == 1 and feed_lines(db, "sim_hold") == []


def test_must_follow_holds_corner_properties_until_queen_st_has_claimed(client, db, network, poster, assert_balance):
    start(client, db, "recall_2")
    add_corner_properties_stub(db, R2)
    dial(network, db, R2, "corner-properties")
    time.sleep(0.6)
    assert poster.tools == [], "Corner Properties must not answer before the sender reports the transfer"

    dial(network, db, R2, "queen-st-alarm")
    assert network.wait_idle(10)
    assert poster.order() == ["queen-st-alarm", "corner-properties"]
    assert all(t["result"]["accepted"] for t in poster.tools)
    edge = db.edges.find_one({"recall_id": R2})
    assert (edge["sender_qty"], edge["recipient_qty"], edge["state"]) == (30, 30, "corroborated")
    assert_balance(R2)


def test_a_party_still_waiting_hangs_up_after_max_call_s(client, db, poster, settings):
    net = SimNetwork(db, poster, settings, max_call_s=0.3)
    try:
        start(client, db, mode="demo")
        _, placed = dial(net, db, R1, "queen-st-alarm")
        assert net.wait_idle(5)
    finally:
        net.stop()
    assert poster.tools == []
    (hook,) = poster.hooks
    assert hook["payload"]["type"] == "post_call_transcription"
    assert db.sim_calls.find_one({"_id": placed.call_sid})["status"] == "completed"
    (warning,) = [line for line in feed_lines(db, "info") if line["source"] == "sim"]
    assert warning["level"] == "warn" and "hung up" in warning["text"] and "demo release" in warning["text"]


def test_stop_ends_waiting_players_and_refuses_new_calls(client, db, network, poster):
    start(client, db, mode="demo")
    _, placed = dial(network, db, R1, "queen-st-alarm")
    assert eventually(lambda: feed_lines(db, "sim_hold"))
    network.stop()
    assert network.wait_idle(5)
    assert poster.tools == [] and poster.hooks == []
    assert db.sim_calls.find_one({"_id": placed.call_sid})["status"] == "in-progress"
    with pytest.raises(ProviderError, match="stopped"):
        dial(network, db, R1, "lakeside-systems")


def test_missing_script_still_posts_a_post_call(client, db, network, poster):
    start(client, db)
    _, placed = dial(network, db, R1, "lakeside-systems", purpose="dispute_followup")
    assert network.wait_idle(10)
    assert poster.tools == []
    (hook,) = poster.hooks
    assert [t["role"] for t in hook["payload"]["data"]["transcript"]] == ["agent", "agent"]
    row = db.sim_calls.find_one({"_id": placed.call_sid})
    assert (row["script_key"], row["status"], row["replies"]) == ("dispute_followup", "completed", [])


# ---------------------------------------------------------------- faults

def test_duplicate_webhook_posts_twice_and_books_once(client, db, network, poster, assert_balance):
    start(client, db)
    attempt, placed = dial(network, db, R1, "lakeside-systems", faults={"duplicate_webhook": True})
    assert network.wait_idle(10)
    first, second = poster.tools
    assert first["body"] == second["body"]
    assert first["result"]["accepted"] and not first["result"].get("duplicate")
    assert second["result"]["accepted"] and second["result"]["duplicate"] is True
    assert db.claims.count_documents({"attempt_id": attempt["_id"]}) == 1
    assert db.recalls.find_one({"_id": R1})["open_units"] == 160
    assert_balance(R1)
    (entry,) = db.sim_calls.find_one({"_id": placed.call_sid})["replies"]
    assert entry["duplicate_reply"]["duplicate"] is True


def test_no_answer_posts_a_call_initiation_failure(client, db, network, poster, settings):
    start(client, db)
    _, placed = dial(network, db, R1, "harbour-fire", faults={"no_answer": True})
    assert network.wait_idle(10)
    assert poster.tools == [] and feed_lines(db, "sim_reply") == []
    (hook,) = poster.hooks
    assert signing.verify(hook["raw"], hook["headers"][signing.HEADER], settings.webhook_secret)
    payload = hook["payload"]
    assert payload["type"] == "call_initiation_failure"
    assert payload["data"] == {"agent_id": "sim", "conversation_id": placed.conversation_id,
                               "failure_reason": "no-answer",
                               "metadata": {"type": "twilio", "body": {"CallSid": placed.call_sid,
                                                                       "CallStatus": "no-answer",
                                                                       "To": "+14165550141"}}}
    row = db.sim_calls.find_one({"_id": placed.call_sid})
    assert row["status"] == "no-answer" and row["end_time"] is not None


def test_drop_post_call_leaves_the_call_unfinished(client, db, network, poster):
    start(client, db)
    _, placed = dial(network, db, R1, "lakeside-systems", faults={"drop_post_call": True})
    assert network.wait_idle(10)
    assert len(poster.tools) == 1 and poster.hooks == []
    row = db.sim_calls.find_one({"_id": placed.call_sid})
    assert row["status"] == "in-progress" and row["end_time"] is None


def test_late_post_call_holds_the_webhook_back(client, db, network, poster):
    start(client, db)
    _, placed = dial(network, db, R1, "lakeside-systems", faults={"late_post_call_s": 1.5})
    assert eventually(lambda: poster.tools)
    assert poster.hooks == []
    assert network.wait_idle(10)
    (hook,) = poster.hooks
    assert hook["at"] - poster.tools[0]["at"] >= 1.5 - 0.02
    duration = hook["payload"]["data"]["metadata"]["call_duration_secs"]
    # The call itself is one claim round trip (well under a second, even to
    # Atlas); counting the 1.5 s hold would round it up to 2.
    assert duration <= 1, "a late webhook does not lengthen the call"


def test_place_call_refuses_bad_input(client, db, network):
    start(client, db)
    with pytest.raises(ProviderError, match="no attempt"):
        network.place_call(to_number="+14165550141", dynamic_variables={}, attempt_id="att_missing")
    with pytest.raises(ValueError, match="unknown simulator fault"):
        dial(network, db, R1, "lakeside-systems", faults={"explode": True})
    with pytest.raises(ValueError, match="late_post_call_s"):
        network.place_call(to_number="+1", dynamic_variables={}, attempt_id="x", faults={"late_post_call_s": True})


# ---------------------------------------------------------------- probe and FakeVoice

def test_sim_probe_finds_the_call(client, db, network):
    start(client, db)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    _, first = dial(network, db, R1, "lakeside-systems", to="+14165550199")
    time.sleep(0.01)                                        # start_time has millisecond precision
    _, second = dial(network, db, R1, "harbour-fire", to="+14165550199")
    assert network.wait_idle(10)

    probe = SimProbe(db)
    found = probe.find_calls(to_number="+14165550199", since=before)
    assert [c.call_sid for c in found] == [second.call_sid, first.call_sid]
    assert found[0].status == "completed" and found[0].conversation_id == second.conversation_id
    assert found[0].to == "+14165550199" and found[0].start_time.tzinfo is not None
    assert probe.find_calls(to_number="+14165550199", since=datetime.now(timezone.utc) + timedelta(seconds=5)) == []
    assert probe.find_calls(to_number="+14165550141", since=before) == []
    assert probe.call_status(first.call_sid).status == "completed"
    assert probe.call_status("CA" + "f" * 32) is None


def test_fake_voice_with_a_network(client, db, network, poster):
    start(client, db)
    voice = FakeVoice(network, faults={"lakeside-systems": "drop_response", "harbour-fire": "fail_before",
                                       "queen-st-alarm": "duplicate_webhook"})
    assert isinstance(voice.probe, SimProbe)
    with pytest.raises(ProviderTimeout):
        dial(voice, db, R1, "lakeside-systems")
    with pytest.raises(ProviderTimeout):
        dial(voice, db, R1, "harbour-fire")
    dial(voice, db, R1, "queen-st-alarm")
    assert network.wait_idle(10)

    assert voice.count() == 2 and voice.count("harbour-fire") == 0
    assert db.sim_calls.count_documents({"party_id": "harbour-fire"}) == 0, "fail_before never reached the vendor"
    lost = voice.placed_calls("lakeside-systems")[0]
    assert db.sim_calls.find_one({"_id": lost["call_sid"]})["status"] == "completed", \
        "the call whose answer was lost still happened"
    assert sorted(poster.order()) == ["lakeside-systems", "queen-st-alarm", "queen-st-alarm"]
    assert voice.probe.call_status(lost["call_sid"]).raw["attempt_id"] == lost["attempt_id"]


# ================================================================ the long run (V2 section 8)

RL = "RCL-2026-020"
LONG = load_scenario("recall_long")
TOOL_HEADERS = {"X-Tool-Secret": "test-tool-secret"}


class LongRunPoster(RecordingPoster):
    """RecordingPoster plus what the long run needs from the API: the
    post-call webhook closes the attempt (the API's handler), a form
    submission is booked exactly as POST /form/{token} books it, and the
    three V2 endpoints are recorded with the database time they arrived."""

    def __init__(self, client, db):
        super().__init__(client, db)
        self.answers: list[dict] = []
        self.callbacks: list[dict] = []
        self.reports: list[dict] = []
        self.forms: list[dict] = []

    def post_hook(self, raw: bytes, headers: dict) -> int:
        code = super().post_hook(raw, headers)
        handle_post_call(self.db, json.loads(raw))
        return code

    def post_form(self, token: str, fields: dict) -> int:
        party = self.db.parties.find_one({"form_token": token})
        raw = raw_from_form({k: str(v) for k, v in fields.items()}, party)
        attempt = form_attempt(self.db, party)
        result, _ = book_claim(self.client, self.db, attempt, raw, source="form")
        if result["accepted"] and attempt["channel"] == "form":
            complete(self.db, attempt["_id"], "completed", reason="form claim accepted")
        self._record(self.forms, {"token": token, "fields": dict(fields), "result": result})
        return 200

    def post_answer(self, body: dict) -> dict:
        return self._record(self.answers, {"body": dict(body)})

    def post_callback(self, body: dict) -> dict:
        return self._record(self.callbacks, {"body": dict(body)})

    def post_report(self, body: dict) -> dict:
        return self._record(self.reports, {"body": dict(body)})

    def _record(self, log: list, entry: dict) -> dict:
        with self._lock:
            log.append({**entry, "when": server_now(self.client)})
        return {"accepted": True}


@pytest.fixture
def long_poster(client, db):
    return LongRunPoster(client, db)


@pytest.fixture
def long_net(db, long_poster, settings):
    net = SimNetwork(db, long_poster, settings, event_wait_s=0.05)
    yield net
    net.stop()
    assert net.wait_idle(5), "a player outlived the test"


def start_long(client, db, *, speed=0.05, sim_start=None):
    """recall_long, every party simulated, on a fast clock (optionally
    re-anchored so the run starts at `sim_start`)."""
    out = start_recall(client, db, LONG, all_sim=True, env={}, seconds_per_sim_hour=speed)
    if sim_start is not None:
        set_clock(db, RL, Clock(real_start=server_now(client), sim_start=sim_start, seconds_per_sim_hour=speed))
    return out


def add_account(client, db, party_id):
    """What the order system read does for one account (V2 section 7): a level-2
    party, an edge from its distributor, and the distributor's books showing
    the forward - all in one transaction, so the mass balance holds."""
    entry = LONG["sim_truth"][party_id]
    acct = next(a for p in LONG["order_systems"].values() for a in p["accounts"] if slugify(a["name"]) == party_id)
    dist, qty, now = entry["upstream"], acct["qty_shipped"], datetime.now(timezone.utc)

    def txn(session):
        db.parties.update_one({"recall_id": RL, "party_id": dist}, [{"$set": {
            "forwarded": {"$add": ["$forwarded", qty]}, "unresolved": {"$subtract": ["$unresolved", qty]}}}],
            session=session)
        db.parties.insert_one(stub_party(RL, party_id, acct["name"], {"channel": "sim", "phone": acct["phone"],
                                                                    "level": 2}, "order_system", now,
                                        next_action_at=9.0), session=session)
        open_edges(db, recall_id=RL, from_party=dist, sender_qty={party_id: qty}, now=now, session=session)
    run_txn(client, txn)
    return acct


def party_doc(db, party_id):
    return db.parties.find_one({"recall_id": RL, "party_id": party_id})


def booked(db, party_id):
    s = party_doc(db, party_id)
    return {b: s[b] for b in ("on_hand", "sold", "returned", "destroyed")}


def truth_buckets(party_id):
    t = LONG["sim_truth"][party_id]["truth"]
    return {b: t[b] for b in ("on_hand", "sold", "returned", "destroyed")}


def sim_hour(client, db, when=None):
    return get_clock(db, RL, max_age_s=0).sim_now(when or server_now(client))


def calls_to(db, party_id):
    return list(db.sim_calls.find({"recall_id": RL, "party_id": party_id}).sort("start_time", 1))


def test_an_over_reporter_is_refused_then_books_the_truth_in_one_paced_call(client, db, long_net, long_poster,
                                                                           assert_balance):
    start_long(client, db, speed=6.0)                 # a simulated minute is 0.1 s: the pacing is measurable
    add_account(client, db, "ridgeway-alarm-co")
    by = LONG["sim_truth"]["ridgeway-alarm-co"]["params"]["over_report_by"]
    attempt, placed = dial(long_net, db, RL, "ridgeway-alarm-co")
    assert long_net.wait_idle(10)

    first, second = long_poster.tools
    received = LONG["sim_truth"]["ridgeway-alarm-co"]["truth"]["received"]
    assert first["result"]["code"] == "over_count"
    assert (first["result"]["reported_total"], first["result"]["shipped_in"]) == (received + by, received)
    assert second["result"]["accepted"] and second["body"]["received_qty"] == received
    assert second["at"] - first["at"] >= 0.1 - 0.02, "one simulated minute per turn"
    assert booked(db, "ridgeway-alarm-co") == truth_buckets("ridgeway-alarm-co")
    assert party_doc(db, "ridgeway-alarm-co")["state"] == "reconciled"
    assert_balance(RL)

    (row,) = calls_to(db, "ridgeway-alarm-co")
    assert (row["profile"], row["reply_kind"], row["script_key"], row["attempt_no"], row["status"]) == \
        ("over_reporter", "answer", None, 1, "completed")
    assert [r["claim"]["on_hand"] for r in row["replies"]] == [first["body"]["on_hand"], second["body"]["on_hand"]]
    (hook,) = long_poster.hooks
    assert hook["payload"]["type"] == "post_call_transcription"
    assert db.attempts.find_one({"_id": attempt["_id"]})["state"] == "completed"
    lines = feed_lines(db, "sim_reply")
    assert [line["text"].split(": ", 1)[0] for line in lines] == ["Ridgeway Alarm Co (simulated)"] * 2
    assert "wrong line" in lines[1]["text"] and all(line.get("sim_label") for line in lines)


def test_a_no_answer_party_rings_out_then_is_busy_then_answers_the_third_call(client, db, long_net, long_poster,
                                                                             assert_balance):
    start_long(client, db)
    add_account(client, db, "elmvale-fire-systems")
    states = []
    for _ in range(3):
        attempt, _ = dial(long_net, db, RL, "elmvale-fire-systems")
        assert long_net.wait_idle(10)
        states.append(db.attempts.find_one({"_id": attempt["_id"]})["state"])

    assert states == ["no_answer", "failed", "completed"]
    failures = [h["payload"] for h in long_poster.hooks[:2]]
    assert [p["type"] for p in failures] == ["call_initiation_failure"] * 2
    assert [p["data"]["failure_reason"] for p in failures] == ["no-answer", "busy"]
    assert long_poster.hooks[2]["payload"]["type"] == "post_call_transcription"
    rows = calls_to(db, "elmvale-fire-systems")
    assert [(r["status"], r["reply_kind"], r["attempt_no"]) for r in rows] == [
        ("no-answer", "no_answer", 1), ("busy", "busy", 2), ("completed", "answer", 3)]
    (tool,) = long_poster.tools
    assert tool["result"]["accepted"] and booked(db, "elmvale-fire-systems") == truth_buckets("elmvale-fire-systems")
    (line,) = feed_lines(db, "sim_reply")
    assert "Sorry we missed your calls." in line["text"]
    assert_balance(RL)


def test_a_callback_party_asks_for_tomorrow_morning_then_answers(client, db, long_net, long_poster):
    start_long(client, db)
    add_account(client, db, "cedarbrae-life-safety")
    hour = LONG["sim_truth"]["cedarbrae-life-safety"]["params"]["callback_hour"]
    first, _ = dial(long_net, db, RL, "cedarbrae-life-safety")
    assert long_net.wait_idle(10)

    (cb,) = long_poster.callbacks
    (row,) = calls_to(db, "cedarbrae-life-safety")
    assert cb["body"] == {"attempt_id": first["_id"], "party_id": "cedarbrae-life-safety",
                          "callback_at": next_morning(row["sim_at"], hour)}
    assert row["reply_kind"] == "callback" and row["replies"][0]["callback_at"] == cb["body"]["callback_at"]
    assert long_poster.tools == [] and db.attempts.find_one({"_id": first["_id"]})["state"] == "completed"

    dial(long_net, db, RL, "cedarbrae-life-safety", purpose="retry")
    assert long_net.wait_idle(10)
    (tool,) = long_poster.tools
    assert tool["result"]["accepted"] and len(long_poster.callbacks) == 1, "it asks for a callback only once"


def test_a_slice_party_answers_questions_after_its_script_and_on_a_question_call(client, db, long_net,
                                                                                long_poster):
    start_long(client, db)
    db.questions.insert_one({"_id": f"{RL}:q-02", "recall_id": RL, "question_id": "q-02",
                             "text": "Did you also receive batch B-4480?", "answer_type": "yes_no_count",
                             "applies_to": {"level": [1, 2, 3]}, "asked_at": 9.0})
    db.parties.update_one({"recall_id": RL, "party_id": "queen-st-alarm"}, {"$set": {"open_questions": ["q-02"]}})
    count = LONG["sim_truth"]["queen-st-alarm"]["truth"]["other_batches"]["B-4480"]

    scripted, _ = dial(long_net, db, RL, "queen-st-alarm")
    assert long_net.wait_idle(10)
    (tool,) = long_poster.tools
    assert tool["result"]["accepted"] and tool["result"]["discovered"] == "corner-properties", "the script played"
    asked, _ = dial(long_net, db, RL, "queen-st-alarm", purpose="ask_question")
    assert long_net.wait_idle(10)

    assert len(long_poster.tools) == 1, "a question call asks no counts"
    value = {"answer": "yes", "count": count}
    assert [a["body"] for a in long_poster.answers] == [
        {"attempt_id": scripted["_id"], "party_id": "queen-st-alarm", "question_id": "q-02", "value": value},
        {"attempt_id": asked["_id"], "party_id": "queen-st-alarm", "question_id": "q-02", "value": value}]
    first, second = calls_to(db, "queen-st-alarm")
    assert (first["script_key"], first["profile"]) == ("initial", None)
    assert (second["script_key"], second["profile"], second["reply_kind"]) == (None, "forwarder", "answer")
    transcript = long_poster.hooks[-1]["payload"]["data"]["transcript"]
    assert any(c["tool_name"] == "submit_answer" for t in transcript for c in t["tool_calls"])

    # Harbour Fire is a real phone in the demo and has no ground truth; simulated, it still answers.
    db.parties.update_one({"recall_id": RL, "party_id": "harbour-fire"}, {"$set": {"open_questions": ["q-02"]}})
    harbour, _ = dial(long_net, db, RL, "harbour-fire", purpose="ask_question")
    assert long_net.wait_idle(10)
    assert long_poster.answers[-1]["body"] == {"attempt_id": harbour["_id"], "party_id": "harbour-fire",
                                               "question_id": "q-02", "value": {"answer": "no", "count": 0}}
    assert len(long_poster.tools) == 1


def test_a_form_link_is_answered_once_in_sim_time_and_survives_an_api_restart(client, db, settings, long_poster,
                                                                             assert_balance):
    start_long(client, db, speed=0.5)                 # two simulated hours are a second of real time
    add_account(client, db, "morrow-avenue-alarms")
    add_account(client, db, "elmvale-fire-systems")
    token = party_doc(db, "morrow-avenue-alarms")["form_token"]
    link = {"recall_id": RL, "party_id": "morrow-avenue-alarms", "token": token}
    after = LONG["sim_truth"]["morrow-avenue-alarms"]["params"]["form_after_hours"]

    first = SimNetwork(db, long_poster, settings, event_wait_s=0.05)
    try:
        api = TestClient(create_app(settings, client=client, sim_network=first))
        before = sim_hour(client, db)
        r = api.post("/sim/form_link", json=link, headers=TOOL_HEADERS)
        answered = sim_hour(client, db)
        assert r.status_code == 200, r.text
        out = r.json()
        assert before + after <= out["submit_at"] <= answered + after and out["duplicate"] is False
        assert api.post("/sim/form_link", json=link).status_code == 401
        assert api.post("/sim/form_link", json={**link, "token": "not-it"}, headers=TOOL_HEADERS).status_code == 400
        assert api.post("/sim/form_link", json={**link, "party_id": "nobody"},
                        headers=TOOL_HEADERS).status_code == 404
    finally:
        first.stop()                                  # the API process goes down before the party submits
    assert long_poster.forms == []

    second = SimNetwork(db, long_poster, settings, event_wait_s=0.05)
    try:
        again = second.form_link_delivered(**link)    # the harness re-sends after its own restart
        assert again == {**out, "duplicate": True}
        ignored = second.form_link_delivered(recall_id=RL, party_id="elmvale-fire-systems",
                                             token=party_doc(db, "elmvale-fire-systems")["form_token"])
        assert ignored["submit_at"] is None
        assert eventually(lambda: long_poster.forms, timeout=10)
        time.sleep(0.3)
    finally:
        second.stop()

    (form,) = long_poster.forms
    assert form["token"] == token and form["result"]["accepted"]
    assert sim_hour(client, db, form["when"]) >= out["submit_at"] - 1e-6, "not before its simulated time"
    assert form["fields"] == profiles.claim_body(LONG["sim_truth"]["morrow-avenue-alarms"]["truth"])
    assert booked(db, "morrow-avenue-alarms") == truth_buckets("morrow-avenue-alarms")
    assert db.claims.find_one({"party_id": "morrow-avenue-alarms", "status": "accepted"})["source"] == "form"
    events = {e["party_id"]: e for e in db.worker_state.find({"kind": "sim_event"})}
    assert events["morrow-avenue-alarms"]["state"] == "done" and events["morrow-avenue-alarms"]["result"]["booked"]
    assert events["elmvale-fire-systems"]["state"] == "ignored"
    assert len([line for line in feed_lines(db, "sim_reply") if "form link" in line["text"]]) == 2
    assert_balance(RL)


def test_a_reviser_corrects_its_count_through_the_form_48_sim_hours_after_acceptance(client, db, long_net,
                                                                                     long_poster, assert_balance):
    start_long(client, db, speed=0.02)                # 48 simulated hours pass in about a second
    add_account(client, db, "glenholme-fire-services")
    revise = LONG["sim_truth"]["glenholme-fire-services"]["params"]["revise"]
    dial(long_net, db, RL, "glenholme-fire-services")
    assert long_net.wait_idle(10)
    call_ended = sim_hour(client, db)
    (tool,) = long_poster.tools
    assert tool["result"]["accepted"] and booked(db, "glenholme-fire-services") == truth_buckets("glenholme-fire-services")
    event = db.worker_state.find_one({"_id": f"sim_event:{RL}:glenholme-fire-services:revise"})
    call_began = calls_to(db, "glenholme-fire-services")[0]["sim_at"]
    assert call_began <= event["due_at"] - revise["after_hours"] <= call_ended, "48 h after the acceptance"

    assert eventually(lambda: long_poster.forms, timeout=10)
    time.sleep(0.2)
    (form,) = long_poster.forms
    assert sim_hour(client, db, form["when"]) >= event["due_at"] - 1e-6
    expected = truth_buckets("glenholme-fire-services")
    expected["on_hand"] -= revise["delta"]
    expected["returned"] += revise["delta"]
    assert form["result"]["accepted"] and booked(db, "glenholme-fire-services") == expected
    assert [c["source"] for c in db.claims.find({"party_id": "glenholme-fire-services", "status": "accepted"})
            .sort("created_at", 1)] == ["sim", "form"]
    assert db.worker_state.find_one({"_id": event["_id"]})["state"] == "done"
    assert_balance(RL)


def test_the_rep_reports_for_the_unreachable_party_once_after_escalation(client, db, long_net, long_poster):
    start_long(client, db, sim_start=200.5)           # half a simulated hour before day 8, 09:00
    add_account(client, db, "tallpine-fire-safety")
    key = f"sim_event:{RL}:tallpine-fire-safety:rep"
    long_net.start()
    assert eventually(lambda: db.worker_state.find_one({"_id": key}))
    assert eventually(lambda: sim_hour(client, db) > 202.5)
    time.sleep(0.1)
    assert long_poster.reports == [], "nobody escalated the party, so the rep has nothing to answer"
    assert db.worker_state.find_one({"_id": key})["due_at"] > 201.0

    db.operations.insert_one({"_id": "op-rep-tallpine", "recall_id": RL, "kind": "escalate_rep",
                              "party_id": "tallpine-fire-safety", "state": "sent",
                              "idempotency_key": f"{RL}:tallpine-fire-safety:escalate_rep"})
    assert eventually(lambda: long_poster.reports, timeout=5)
    time.sleep(0.2)
    (report,) = long_poster.reports
    truth = LONG["sim_truth"]["tallpine-fire-safety"]["truth"]
    assert report["body"] == {"recall_id": RL, "party_id": "tallpine-fire-safety", "source": "rep",
                              "evidence_id": "op-rep-tallpine", **profiles.rep_report(truth)}
    event = db.worker_state.find_one({"_id": key})
    assert event["state"] == "done" and event["result"]["op_id"] == "op-rep-tallpine"
    (line,) = [x for x in feed_lines(db, "sim_reply") if "rep reports" in x["text"]]
    assert line["text"].startswith("Distributor North's rep reports for Tallpine Fire & Safety")
    assert line["system"] == "North orders"


# ---------------------------------------------------------------- the posters and the worker's side

def test_posters_reach_the_v2_endpoints_and_the_form():
    app, got = FastAPI(), {}

    async def endpoint(request: Request):
        got[request.url.path] = (request.headers.get("x-tool-secret"), await request.json())
        return {"accepted": True}

    for path in ("/tools/submit_answer", "/tools/request_callback", "/tools/report"):
        app.add_api_route(path, endpoint, methods=["POST"])

    @app.post("/form/{token}")
    async def form(token: str, request: Request):
        got["form"] = (token, request.headers.get("content-type"), (await request.body()).decode())
        return {"ok": True}

    poster = ClientPoster(TestClient(app), "tool-secret")
    assert poster.post_answer({"question_id": "q-02"}) == {"accepted": True}
    assert poster.post_callback({"callback_at": 33.0}) == {"accepted": True}
    assert poster.post_report({"source": "rep"}) == {"accepted": True}
    assert poster.post_form("tok-en", {"received_qty": 31, "in_stock": 8}) == 200
    assert got["/tools/submit_answer"] == ("tool-secret", {"question_id": "q-02"})
    assert got["/tools/request_callback"] == ("tool-secret", {"callback_at": 33.0})
    assert got["/tools/report"] == ("tool-secret", {"source": "rep"})
    assert got["form"] == ("tok-en", "application/x-www-form-urlencoded", "received_qty=31&in_stock=8")


def test_sim_voice_delivers_a_form_link():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, secret=request.headers.get("x-tool-secret"),
                    body=json.loads(request.content))
        return httpx.Response(200, json={"accepted": True, "submit_at": 35.0, "duplicate": False})

    voice = SimVoice("http://api.test", "tool-secret", http=httpx.Client(transport=httpx.MockTransport(handler)))
    out = voice.send_form_link(recall_id=RL, party_id="morrow-avenue-alarms", token="abc")
    assert out["submit_at"] == 35.0
    assert seen == {"path": "/sim/form_link", "secret": "tool-secret",
                    "body": {"recall_id": RL, "party_id": "morrow-avenue-alarms", "token": "abc"}}
