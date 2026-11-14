"""recovery: evidence before any redial, in DESIGN.md section 5's order, and
only for attempts whose owner is gone. Every test uses an explicit `now`.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from levela.db.seed import load_scenario, start_recall
from levela.orchestrator import attempts as A
from levela.orchestrator import recovery
from levela.orchestrator.attempts import utcnow
from levela.providers.base import ProbedCall

RID, SID, PHONE = "RCL-2026-017", "queen-st-alarm", "+14165550142"
ME = {"worker": "worker-test", "boot": "boot-now"}
DEAD = {"worker": "worker-gone", "boot": "boot-gone"}


class ScriptedProbe:
    """A call log that answers like Twilio's: every call to the number, newest
    first, unfiltered by time (the caller must compare timestamps)."""

    def __init__(self, calls=(), *, name="sim", fail=None):
        self.name, self.calls, self.fail, self.asked = name, list(calls), fail, []

    def find_calls(self, *, to_number, since):
        self.asked.append(("find", to_number, since))
        if self.fail is not None:
            raise self.fail
        return [c for c in self.calls if c.to == to_number]

    def call_status(self, call_sid):
        self.asked.append(("status", call_sid))
        if self.fail is not None:
            raise self.fail
        return next((c for c in self.calls if c.call_sid == call_sid), None)


def _call(sid, start, status="in-progress", to=PHONE, conversation_id=None):
    return ProbedCall(call_sid=sid, status=status, start_time=start, to=to, conversation_id=conversation_id)


@pytest.fixture
def recall(client, db):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    return RID


def _open(db, t0, owner=DEAD, party_id=SID, **kw):
    return A.open_attempt(db, recall_id=RID, party_id=party_id, channel="sim", owner=owner, to_number=PHONE,
                          now=t0, **kw)


def _tool_event(db, attempt, kind="tool_call", payload=None, **ids):
    db.tool_events.insert_one({"attempt_id": attempt["_id"], "party_id": attempt["party_id"], "recall_id": RID,
                               "kind": kind, "payload": payload or {}, "received_at": utcnow(), **ids})


def _resolve(db, settings, attempt, probe, now, me=ME):
    return recovery.resolve_in_doubt(db, attempt, probe, settings, now, me)


def _feed(db, kind):
    return list(db.feed.find({"kind": kind}))


# -- evidence, strongest first ------------------------------------------------------

def test_tool_events_adopt_the_call_without_asking_the_probe(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    _tool_event(db, att, conversation_id="conv_7", call_sid="CA7")
    probe = ScriptedProbe()
    res = _resolve(db, settings, att, probe, t0 + timedelta(seconds=30))
    assert res == {"action": "adopted", "evidence": "tool_events", "call_sid": "CA7", "conversation_id": "conv_7",
                   "state": "connected"}
    assert probe.asked == []
    row = A.get(db, att["_id"])
    assert row["dial_confirmed"] is True and row["adopted"]["evidence"] == "tool_events"
    assert row["adopted"]["by"] == ME
    [line] = _feed(db, "attempt_adopted")
    assert line["text"] == (f"Attempt {att['_id']} for Queen St Alarm adopted from tool_events: call CA7 "
                            "happened while the worker was down. No redial.")
    assert (line["level"], line["attempt_id"], line["source"]) == ("good", att["_id"], "worker")


def test_tool_events_beat_the_grace_period(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    _tool_event(db, att)
    res = _resolve(db, replace(settings, indoubt_grace_s=60), att, ScriptedProbe(), t0)
    assert res["action"] == "adopted" and res["call_sid"] is None
    [line] = _feed(db, "attempt_adopted")
    assert line["text"] == (f"Attempt {att['_id']} for Queen St Alarm adopted from tool_events: the call "
                            "happened while the worker was down. No redial.")


@pytest.mark.parametrize("payload, state", [
    ({"type": "post_call_transcription"}, "completed"),
    ({"type": "call_initiation_failure", "data": {"failure_reason": "no-answer"}}, "no_answer"),
    ({"type": "call_initiation_failure", "data": {"failure_reason": "busy"}}, "failed"),
])
def test_a_logged_post_call_sets_the_final_state(db, settings, recall, payload, state):
    t0 = utcnow()
    att = _open(db, t0)
    _tool_event(db, att, kind="post_call", payload=payload, conversation_id="conv_8")
    res = _resolve(db, settings, att, ScriptedProbe(), t0)
    assert res["action"] == "adopted" and res["state"] == state
    assert A.get(db, att["_id"])["outcome_reason"] == "adopted from tool_events"


def test_a_call_the_webhook_already_closed_is_adopted_from_post_call(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    A.complete(db, att["_id"], "completed", now=t0 + timedelta(seconds=40))
    probe = ScriptedProbe()
    res = _resolve(db, settings, att, probe, t0 + timedelta(seconds=60))
    assert res["action"] == "adopted" and res["evidence"] == "post_call" and res["state"] == "completed"
    assert probe.asked == []
    row = A.get(db, att["_id"])
    assert row["dial_confirmed"] is True and row["adopted"]["evidence"] == "post_call"
    assert "adopted from post_call" in _feed(db, "attempt_adopted")[0]["text"]


def test_a_young_attempt_is_deferred_before_the_probe(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    probe = ScriptedProbe()
    res = _resolve(db, replace(settings, indoubt_grace_s=15), att, probe, t0 + timedelta(seconds=14))
    assert res["action"] == "deferred" and res["reason"] == "younger than the grace period"
    assert probe.asked == []
    assert A.get(db, att["_id"])["state"] == "intent"
    res = _resolve(db, replace(settings, indoubt_grace_s=15), att, probe, t0 + timedelta(seconds=16))
    assert res["action"] == "abandoned"


@pytest.mark.parametrize("name, evidence", [("sim", "sim probe"), ("twilio", "twilio")])
def test_the_probe_finds_the_call_and_it_is_adopted(db, settings, recall, name, evidence):
    t0 = utcnow()
    att = _open(db, t0)
    probe = ScriptedProbe([_call("CA5", t0 + timedelta(seconds=1), conversation_id="conv_5")], name=name)
    res = _resolve(db, settings, att, probe, t0 + timedelta(seconds=20))
    assert (res["action"], res["evidence"], res["call_sid"], res["state"]) == ("adopted", evidence, "CA5", "dialed")
    stored = A.get(db, att["_id"])["intent_at"]        # Mongo keeps milliseconds: compare what is stored
    assert probe.asked == [("find", PHONE, stored - timedelta(seconds=settings.probe_skew_s))]
    row = A.get(db, att["_id"])
    assert (row["state"], row["call_sid"], row["conversation_id"]) == ("dialed", "CA5", "conv_5")
    assert _feed(db, "attempt_adopted")[0]["text"] == (
        f"Attempt {att['_id']} for Queen St Alarm adopted from {evidence}: call CA5 happened while the worker "
        "was down. No redial.")


@pytest.mark.parametrize("status, state", [("completed", "completed"), ("no-answer", "no_answer"),
                                           ("busy", "failed"), ("ringing", "dialed")])
def test_an_adopted_call_takes_its_state_from_the_probe(db, settings, recall, status, state):
    t0 = utcnow()
    att = _open(db, t0)
    res = _resolve(db, settings, att, ScriptedProbe([_call("CA5", t0, status=status)]), t0 + timedelta(seconds=5))
    assert res["state"] == state


def test_start_times_are_compared_with_the_allowed_skew(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    inside = _call("CA_in", t0 - timedelta(seconds=settings.probe_skew_s * 0.5))
    res = _resolve(db, settings, att, ScriptedProbe([inside]), t0 + timedelta(seconds=5))
    assert res["call_sid"] == "CA_in"

    att2 = _open(db, t0, party_id="lakeside-systems", attempt_id="att_lakeside_1")
    outside = _call("CA_old", t0 - timedelta(seconds=settings.probe_skew_s * 3))
    res = _resolve(db, settings, att2, ScriptedProbe([outside]), t0 + timedelta(seconds=5))
    assert res["action"] == "abandoned"


def test_a_call_not_started_yet_counts_only_while_live(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    res = _resolve(db, settings, att, ScriptedProbe([_call("CA_q", None, status="queued")]), t0)
    assert res["action"] == "adopted" and res["call_sid"] == "CA_q"

    att2 = _open(db, t0, party_id="lakeside-systems")
    res = _resolve(db, settings, att2, ScriptedProbe([_call("CA_x", None, status="canceled")]), t0)
    assert res["action"] == "abandoned"


def test_a_call_already_on_another_attempt_is_never_adopted(db, settings, recall):
    t0 = utcnow()
    earlier = _open(db, t0 - timedelta(seconds=1), owner=ME)
    A.mark_dialed(db, earlier["_id"], call_sid="CA_first")
    A.complete(db, earlier["_id"], "no_answer")
    att = _open(db, t0)
    res = _resolve(db, settings, att, ScriptedProbe([_call("CA_first", t0)]), t0 + timedelta(seconds=5))
    assert res["action"] == "abandoned"
    assert A.get(db, att["_id"])["call_sid"] is None


def test_the_newest_free_call_is_adopted(db, settings, recall):
    t0 = utcnow()
    other = _open(db, t0, party_id="lakeside-systems")
    A.mark_dialed(db, other["_id"], call_sid="CA_taken")
    att = _open(db, t0)
    calls = [_call("CA_taken", t0 + timedelta(seconds=3)), _call("CA_new", t0 + timedelta(seconds=2)),
             _call("CA_older", t0 + timedelta(seconds=1))]
    res = _resolve(db, settings, att, ScriptedProbe(calls), t0 + timedelta(seconds=5))
    assert res["call_sid"] == "CA_new"


def test_no_evidence_abandons_so_the_decider_may_dial_again(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    res = _resolve(db, settings, att, ScriptedProbe([_call("CA_else", t0, to="+14165550199")]), t0)
    assert res == {"action": "abandoned", "evidence": None}
    row = A.get(db, att["_id"])
    assert (row["state"], row["outcome_reason"], row["abandoned_by"]) == ("abandoned", "no evidence of a call", ME)
    [line] = _feed(db, "attempt_abandoned")
    assert line["text"] == (f"Attempt {att['_id']} for Queen St Alarm abandoned: no evidence of a call. "
                            "Dialling again is safe.")
    assert line["level"] == "warn"


def test_a_probe_error_is_no_information(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    res = _resolve(db, settings, att, ScriptedProbe(fail=TimeoutError("twilio unreachable")), t0)
    assert res["action"] == "deferred" and res["reason"] == "probe failed: twilio unreachable"
    assert A.get(db, att["_id"])["state"] == "intent"
    assert _feed(db, "attempt_abandoned") == []


def test_no_probe_for_the_channel_defers(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    assert _resolve(db, settings, att, None, t0)["action"] == "deferred"
    assert A.get(db, att["_id"])["state"] == "intent"


def test_settled_attempts_are_not_in_doubt(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0)
    A.abandon(db, att["_id"], "no evidence")
    probe = ScriptedProbe([_call("CA1", t0)])
    assert _resolve(db, settings, att, probe, t0) == {"action": "deferred", "evidence": None,
                                                     "reason": "not in doubt"}
    assert "adopted" not in A.get(db, att["_id"])
    confirmed = _open(db, t0)
    A.mark_dialed(db, confirmed["_id"], call_sid="CA2")
    assert _resolve(db, settings, confirmed, probe, t0)["reason"] == "not in doubt"


def test_in_doubt_filter_matches_in_doubt(db, settings, recall):
    t0 = utcnow()
    ids = {name: _open(db, t0, party_id=sid)["_id"] for name, sid in
           (("intent", "harbour-fire"), ("closed_unconfirmed", "lakeside-systems"), ("confirmed", SID))}
    A.complete(db, ids["closed_unconfirmed"], "completed")
    A.mark_dialed(db, ids["confirmed"], call_sid="CA1")
    found = {a["_id"] for a in db.attempts.find(recovery.in_doubt_filter([RID]))}
    assert found == {ids["intent"], ids["closed_unconfirmed"]}
    assert found == {a["_id"] for a in db.attempts.find() if A.in_doubt(a)}


# -- ownership -------------------------------------------------------------------------

def _register(db, worker, boot, beat, **extra):
    db.worker_state.update_one({"_id": f"worker:{worker}"},
                               {"$set": {"boot_id": boot, "heartbeat_at": beat, **extra}}, upsert=True)


def test_another_live_workers_attempt_is_untouched_until_its_heartbeat_goes_stale(db, settings, recall):
    t0 = utcnow()
    _register(db, "worker-b", "b1", t0)
    att = _open(db, t0, owner={"worker": "worker-b", "boot": "b1"})
    probe = ScriptedProbe()
    res = _resolve(db, settings, att, probe, t0 + timedelta(seconds=settings.worker_dead_after_s / 2))
    assert res["action"] == "deferred" and res["reason"] == "owner is alive"
    assert probe.asked == [] and A.get(db, att["_id"])["state"] == "intent"
    res = _resolve(db, settings, att, probe, t0 + timedelta(seconds=settings.worker_dead_after_s + 1))
    assert res["action"] == "abandoned"


@pytest.mark.parametrize("owner, state, flagged, expected", [
    ({"worker": "worker-test", "boot": "boot-before"}, None, False, True),     # my previous boot
    ({"worker": "worker-test", "boot": "boot-now"}, None, False, False),       # mine, mid-dispatch
    ({"worker": "worker-test", "boot": "boot-now"}, None, True, True),         # mine, reply lost
    ({"worker": "worker-b", "boot": "b1"}, {"boot_id": "b1"}, False, False),   # alive
    ({"worker": "worker-b", "boot": "b1"}, {"boot_id": "b2"}, False, True),    # restarted since
    ({"worker": "worker-b", "boot": "b1"}, {"boot_id": "b1", "stopped": True}, False, True),
    ({"worker": "worker-b", "boot": "b1"}, None, False, True),                 # never registered
    (None, None, False, True),
])
def test_resolvable_follows_the_owner_boot(db, settings, recall, owner, state, flagged, expected):
    now = utcnow()
    if state is not None:
        extra = {"stopped_at": now} if state.get("stopped") else {}
        _register(db, "worker-b", state["boot_id"], now, **extra)
    attempt = {"owner": owner, "in_doubt": flagged}
    assert recovery.resolvable(db, attempt, settings, now, ME) is expected


# -- stale calls -------------------------------------------------------------------------

def _dialed(db, t0, sid="CA9"):
    att = _open(db, t0, owner=ME)
    A.mark_dialed(db, att["_id"], call_sid=sid, now=t0)
    return att


def test_a_stale_call_is_closed_to_match_the_probe(db, settings, recall):
    t0 = utcnow()
    att = _dialed(db, t0)
    probe = ScriptedProbe([_call("CA9", t0, status="completed")])
    now = t0 + timedelta(seconds=settings.stale_call_s + 1)
    res = recovery.check_stale(db, att, probe, settings, now, ME)
    assert res == {"action": "closed", "state": "completed", "status": "completed", "evidence": "sim probe"}
    row = A.get(db, att["_id"])
    assert row["state"] == "completed" and row["outcome_reason"] == "stale: sim probe reports completed"
    [line] = _feed(db, "attempt_stale")
    assert line["text"] == (f"Attempt {att['_id']} for Queen St Alarm had no post-call after "
                            f"{int(settings.stale_call_s + 1)} s; sim probe reports the call completed. "
                            "Closed as completed.")


def test_a_fresh_call_is_not_probed(db, settings, recall):
    t0 = utcnow()
    att = _dialed(db, t0)
    probe = ScriptedProbe([_call("CA9", t0, status="completed")])
    assert recovery.check_stale(db, att, probe, settings, t0 + timedelta(seconds=1), ME)["action"] == "fresh"
    assert probe.asked == []


def test_a_long_call_still_in_progress_stays_open(db, settings, recall):
    t0 = utcnow()
    att = _dialed(db, t0)
    A.mark_connected(db, att["_id"])
    now = t0 + timedelta(seconds=settings.stale_call_s + 1)
    res = recovery.check_stale(db, att, ScriptedProbe([_call("CA9", t0)]), settings, now, ME)
    assert res == {"action": "live", "status": "in-progress"}
    assert A.get(db, att["_id"])["state"] == "connected"


def test_stale_probe_error_or_unknown_call_leaves_the_attempt(db, settings, recall):
    t0 = utcnow()
    att = _dialed(db, t0)
    now = t0 + timedelta(seconds=settings.stale_call_s + 1)
    assert recovery.check_stale(db, att, ScriptedProbe(fail=OSError("down")), settings, now, ME)["action"] == \
        "deferred"
    assert recovery.check_stale(db, att, ScriptedProbe(), settings, now, ME)["action"] == "unknown"
    assert A.get(db, att["_id"])["state"] == "dialed"


def test_a_stale_call_without_a_sid_is_found_by_number(db, settings, recall):
    t0 = utcnow()
    att = _open(db, t0, owner=ME)
    A.mark_dialed(db, att["_id"], conversation_id="conv_only", now=t0)
    probe = ScriptedProbe([_call("CA_found", t0, status="no-answer")])
    now = t0 + timedelta(seconds=settings.stale_call_s + 1)
    res = recovery.check_stale(db, att, probe, settings, now, ME)
    assert res["state"] == "no_answer"
    assert A.get(db, att["_id"])["call_sid"] == "CA_found"
