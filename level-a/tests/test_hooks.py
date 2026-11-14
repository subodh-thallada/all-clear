"""POST /hooks/post_call: HMAC verification, closing attempts from the
post-call events, and matching call_initiation_failure (which carries no
dynamic variables) by conversation, CallSid and dialled number."""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from levela.api import hooks
from levela.api.main import create_app
from levela.config import DEV_SECRETS, Settings
from levela.db.seed import load_scenario, start_recall
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.signing import HEADER, sign, verify

SECRET = "test-webhook-secret"


@pytest.fixture
def api(settings, client):
    return TestClient(create_app(settings, client=client, sim_network=object()))


@pytest.fixture
def rid(client, db):
    return start_recall(client, db, load_scenario("recall_1"), all_sim=True)["recall_id"]


def phone(db, recall_id, party_id):
    return db.parties.find_one({"recall_id": recall_id, "party_id": party_id})["phone"]


def opened(db, recall_id, party_id):
    """An attempt in intent: the worker died before saving the provider's answer."""
    return open_attempt(db, recall_id=recall_id, party_id=party_id, channel="sim",
                        to_number=phone(db, recall_id, party_id))


def dialed(db, recall_id, party_id):
    att = opened(db, recall_id, party_id)
    mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CA{att['_id']}")
    return db.attempts.find_one({"_id": att["_id"]})


def post(api, event, *, secret=SECRET, t=None, raw=None):
    body = json.dumps(event).encode() if raw is None else raw
    return api.post("/hooks/post_call", content=body,
                    headers={HEADER: sign(body, secret, t), "Content-Type": "application/json"})


def transcription(att, *, conversation_id=None, call_sid=None, status="done", dynamic=True):
    data = {
        "agent_id": "agent_test", "conversation_id": conversation_id or att["conversation_id"], "status": status,
        "transcript": [
            {"role": "agent", "message": "Hello, this is the recall desk for batch M O four four one seven.",
             "time_in_call_secs": 0},
            {"role": "user", "message": "Twenty-five on hand, ten sold, five sent back.", "time_in_call_secs": 9},
            {"role": "agent", "message": None, "tool_calls": [{"tool_name": "submit_count"}],
             "time_in_call_secs": 14},
            {"role": "agent", "message": "Recorded. Thank you.", "time_in_call_secs": 16},
        ],
        "metadata": {"start_time_unix_secs": int(time.time()) - 52, "call_duration_secs": 52,
                     "termination_reason": "end_call tool was called.",
                     "phone_call": {"type": "twilio", "direction": "outbound",
                                    "call_sid": call_sid or att["call_sid"]}},
        "analysis": {"call_successful": "success", "transcript_summary": "Counts confirmed: 25/10/5."},
    }
    if dynamic:
        data["conversation_initiation_client_data"] = {"dynamic_variables": {
            "attempt_id": att["_id"], "party_id": att["party_id"], "system__conversation_id": data["conversation_id"]}}
    return {"type": "post_call_transcription", "event_timestamp": int(time.time()), "data": data}


def initiation_failure(reason, *, conversation_id="conv_unknown", call_sid="CAunknown", to="+19995550100"):
    return {"type": "call_initiation_failure", "event_timestamp": int(time.time()), "data": {
        "agent_id": "agent_test", "conversation_id": conversation_id, "failure_reason": reason,
        "metadata": {"type": "twilio", "body": {"CallSid": call_sid, "CallStatus": reason, "To": to,
                                                "From": "+14165550199", "Direction": "outbound-api"}}}}


def feed_texts(db, kind="post_call"):
    return [line["text"] for line in db.feed.find({"kind": kind}).sort("at", 1)]


def test_transcription_completes_the_attempt_and_keeps_the_transcript(api, db, rid):
    att = dialed(db, rid, "harbour-fire")
    r = post(api, transcription(att))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "matched": True, "attempt_id": att["_id"], "state": "completed", "changed": True}

    done = db.attempts.find_one({"_id": att["_id"]})
    assert done["state"] == "completed" and done["ended_at"]
    assert done["transcript"] == [
        {"role": "agent", "message": "Hello, this is the recall desk for batch M O four four one seven."},
        {"role": "user", "message": "Twenty-five on hand, ten sold, five sent back."},
        {"role": "agent", "message": "Recorded. Thank you."},
    ]
    assert (done["call_duration_secs"], done["termination_reason"], done["summary"]) == (
        52, "end_call tool was called.", "Counts confirmed: 25/10/5.")
    [event] = db.tool_events.find({"kind": "post_call"})
    assert (event["attempt_id"], event["recall_id"], event["party_id"]) == (att["_id"], rid, "harbour-fire")
    assert (event["conversation_id"], event["call_sid"], event["source"]) == (att["conversation_id"],
                                                                               att["call_sid"], "sim")
    assert event["payload"]["type"] == "post_call_transcription"
    assert feed_texts(db) == ["Call with Harbour Fire ended after 52 s"]

    again = post(api, transcription(att))          # a redelivery changes nothing
    assert again.status_code == 200 and again.json()["changed"] is False
    assert feed_texts(db) == ["Call with Harbour Fire ended after 52 s"]
    assert db.tool_events.count_documents({"kind": "post_call"}) == 2


def test_transcription_closes_an_in_doubt_attempt_and_fills_its_ids(api, db, rid):
    att = opened(db, rid, "lakeside-systems")
    event = transcription(att, conversation_id="conv_while_down", call_sid="CAwhiledown")
    assert post(api, event).json()["state"] == "completed"
    done = db.attempts.find_one({"_id": att["_id"]})
    assert (done["conversation_id"], done["call_sid"]) == ("conv_while_down", "CAwhiledown")
    assert done["dial_confirmed"] is False          # recovery, not the webhook, decides adoption


def test_transcription_without_dynamic_variables_matches_by_conversation(api, db, rid):
    att = dialed(db, rid, "queen-st-alarm")
    r = post(api, transcription(att, dynamic=False))
    assert r.json()["attempt_id"] == att["_id"]
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "completed"


def test_failed_conversation_fails_the_attempt(api, db, rid):
    att = dialed(db, rid, "harbour-fire")
    assert post(api, transcription(att, status="failed")).json()["state"] == "failed"
    assert feed_texts(db) == ["Call with Harbour Fire failed (end_call tool was called.)"]


def test_bad_signature_is_refused(api, db, rid):
    att = dialed(db, rid, "harbour-fire")
    event = transcription(att)
    assert post(api, event, secret="wrong").status_code == 401
    assert post(api, event, t=int(time.time()) - 31 * 60).status_code == 401
    body = json.dumps(event).encode()
    tampered = api.post("/hooks/post_call", content=body.replace(b"52", b"53"),
                        headers={HEADER: sign(body, SECRET)})
    assert tampered.status_code == 401
    assert api.post("/hooks/post_call", content=body).status_code == 401
    assert db.tool_events.count_documents({}) == 0
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "dialed"


def test_initiation_failure_matched_by_conversation_id(api, db, rid):
    att = dialed(db, rid, "harbour-fire")
    r = post(api, initiation_failure("no-answer", conversation_id=att["conversation_id"]))
    assert r.json()["state"] == "no_answer"
    done = db.attempts.find_one({"_id": att["_id"]})
    assert (done["state"], done["outcome_reason"]) == ("no_answer", "no-answer")
    assert feed_texts(db) == ["Call to Harbour Fire was not answered"]


def test_initiation_failure_matched_by_call_sid(api, db, rid):
    att = dialed(db, rid, "lakeside-systems")
    r = post(api, initiation_failure("unknown", call_sid=att["call_sid"]))
    assert (r.json()["attempt_id"], r.json()["state"]) == (att["_id"], "failed")


def test_initiation_failure_matched_by_the_dialled_number(api, db, rid):
    number = phone(db, rid, "queen-st-alarm")
    earlier = opened(db, rid, "queen-st-alarm")
    complete(db, earlier["_id"], "no_answer")
    live = opened(db, rid, "queen-st-alarm")               # in doubt: no ids saved at all
    dialed(db, rid, "harbour-fire")                       # live, but to another number
    r = post(api, initiation_failure("busy", to=number))
    assert (r.json()["attempt_id"], r.json()["state"]) == (live["_id"], "failed")
    assert db.attempts.find_one({"_id": earlier["_id"]})["state"] == "no_answer"
    assert feed_texts(db) == ["Call to Queen St Alarm failed: busy"]
    saved = db.attempts.find_one({"_id": live["_id"]})
    assert (saved["conversation_id"], saved["call_sid"]) == ("conv_unknown", "CAunknown")


def test_audio_is_logged_without_the_audio_and_ignored(api, db, rid):
    att = dialed(db, rid, "harbour-fire")
    event = {"type": "post_call_audio", "data": {"agent_id": "agent_test", "conversation_id": att["conversation_id"],
                                                  "full_audio": "UklGRiQAAABXQVZF" * 64}}
    r = post(api, event)
    assert r.status_code == 200 and r.json() == {"ok": True, "ignored": "post_call_audio"}
    [row] = db.tool_events.find({"kind": "post_call"})
    assert "full_audio" not in row["payload"]["data"] and row["attempt_id"] == att["_id"]
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "dialed"


def test_unknown_attempt_is_acknowledged_and_flagged(api, db, rid):
    ghost = {"_id": "att_00000000000000", "party_id": "harbour-fire", "conversation_id": "conv_ghost",
             "call_sid": "CAghost"}
    r = post(api, transcription(ghost))
    assert r.status_code == 200 and r.json() == {"ok": True, "matched": False}
    [line] = db.feed.find({"kind": "post_call"})
    assert line["level"] == "warn" and "conv_ghost" in line["text"]
    assert db.tool_events.find_one({"kind": "post_call"})["attempt_id"] is None


def test_signed_body_that_is_not_json_is_still_acknowledged(api):
    r = post(api, None, raw=b"not json")
    assert r.status_code == 200 and r.json()["ok"] is False


@pytest.mark.parametrize("header", [
    "t={now},v0=\xe9",                        # latin-1 text: hmac.compare_digest raised TypeError
    "t=\xb2,v0=ab",                           # a superscript two passes isdigit(), then int() raised
    "t=" + "9" * 5000 + ",v0=ab",             # int() refuses more than 4300 digits
    "t={now},v0=zz", "t=,v0=ab", "v0=ab", "garbage",
])
def test_a_malformed_signature_header_is_a_401_not_a_crash(api, db, header):
    """Starlette decodes header bytes as latin-1, so anyone can send any of
    these; each used to raise inside verify (a 500 and a traceback)."""
    raw = header.replace("{now}", str(int(time.time()))).encode("latin-1")
    r = api.post("/hooks/post_call", content=b"{}", headers={HEADER: raw})
    assert r.status_code == 401
    assert not verify(b"{}", raw.decode("latin-1"), SECRET)
    assert db.tool_events.count_documents({}) == 0


def test_an_unsigned_request_is_refused_before_its_body_is_read(api, db):
    sent = []

    def body():
        for _ in range(4):
            sent.append(1)
            yield b"x" * 1024
    r = api.post("/hooks/post_call", content=body())
    assert r.status_code == 401 and sent == []


def test_a_body_over_the_cap_is_refused_even_with_a_signature(api, db, monkeypatch):
    monkeypatch.setattr(hooks, "MAX_BODY", 1024)
    body = json.dumps({"type": "post_call_audio", "data": {"full_audio": "A" * 4096}}).encode()
    r = api.post("/hooks/post_call", content=body, headers={HEADER: sign(body, SECRET)})
    assert r.status_code == 413 and db.tool_events.count_documents({}) == 0


def test_an_unset_webhook_secret_refuses_the_public_dev_value(client, db, rid, settings):
    """Unset used to mean "dev-webhook-secret", a value in the repository, so
    anyone could sign a webhook that closes a live call's attempt."""
    host = TestClient(create_app(replace(settings, webhook_secret=Settings.from_env(env={}).webhook_secret),
                                 client=client, sim_network=object()))
    att = dialed(db, rid, "harbour-fire")
    r = post(host, transcription(att, status="failed"), secret=DEV_SECRETS["webhook_secret"])
    assert r.status_code == 401
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "dialed"
