"""Evidence before any redial.

An attempt is in doubt when the worker never saved the provider's answer
(dial_confirmed is not true): it died between asking for the call and saving
the reply, or the reply was lost. The call may exist, so redialling on a
hunch could ring a party twice. resolve_in_doubt() decides from evidence,
strongest first:

  1. tool_events holds an event for this attempt -> the call happened: adopt it
  2. the post-call webhook already closed it     -> adopt (evidence post_call)
  3. younger than indoubt_grace_s                -> defer: the provider may still
                                                    be creating the call
  4. the probe (Twilio's Calls list, or the simulator's call log) has a call to
     this number that started at or after intent_at - probe_skew_s and is not
     already on another attempt                  -> adopt the newest
  5. nothing                                     -> abandon. This is the only path
                                                    that dials again.

A probe that raises gives no information: the attempt is deferred, never
abandoned. Only attempts whose owner is gone are touched, so two workers can
run at once. The owner is gone when its boot is dead (resolvable(), by
heartbeat), or, with `fenced=True`, when the caller holds the party's lease at
a newer epoch (V2 section 5: recovery at claim time). The grace period still
protects a call the provider may be creating.

check_stale() closes a dialed or connected attempt whose post-call webhook
never came, to match what the probe says about the call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .. import feed
from ..config import FINAL_ATTEMPT_STATES
from ..providers.base import FINAL_STATUS_TO_ATTEMPT, LIVE_STATUSES
from . import attempts

log = logging.getLogger("levela.recovery")

_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def in_doubt_filter(recall_ids) -> dict:
    """The Mongo filter for attempts.in_doubt() within these recalls."""
    return {"recall_id": {"$in": list(recall_ids)}, "channel": {"$ne": "form"},
            "dial_confirmed": {"$ne": True}, "state": {"$ne": "abandoned"}}


def resolvable(db, attempt: dict, settings, now: datetime, me: dict) -> bool:
    """May `me` ({worker, boot}) settle this in-doubt attempt? Yes when its
    owner boot is dead: this worker's previous boot (dead by definition), or
    another worker whose heartbeat is older than worker_dead_after_s, who
    stopped, or whose boot changed. This boot's own attempt only once it is
    flagged in_doubt (the provider's reply was lost); the grace period in
    resolve_in_doubt() still protects a call the provider is creating."""
    owner = attempt.get("owner") or {}
    if owner.get("worker") == me["worker"]:
        if owner.get("boot") != me["boot"]:
            return True
        return bool(attempt.get("in_doubt"))
    return _boot_dead(db, owner, settings, now)


def _boot_dead(db, owner: dict, settings, now: datetime) -> bool:
    if not owner.get("worker"):
        return True
    state = db.worker_state.find_one({"_id": f"worker:{owner['worker']}"},
                                     {"boot_id": 1, "heartbeat_at": 1, "stopped_at": 1})
    if state is None or state.get("boot_id") != owner.get("boot") or state.get("stopped_at"):
        return True
    beat = _aware(state.get("heartbeat_at"))
    return beat is None or (now - beat).total_seconds() > settings.worker_dead_after_s


def resolve_in_doubt(db, attempt: dict, probe, settings, now: datetime, me: dict, *, fenced: bool = False) -> dict:
    """Settle one in-doubt attempt from evidence (order in the module docstring).
    `fenced=True` means the caller leases the attempt's party at a newer
    epoch than the attempt's, so no heartbeat is consulted.
    Returns {"action": "adopted"|"abandoned"|"deferred", "evidence", ...}."""
    current = attempts.get(db, attempt["_id"])
    if current is None or not attempts.in_doubt(current):
        return _deferred("not in doubt")
    if not fenced and not resolvable(db, current, settings, now, me):
        return _deferred("owner is alive")
    events = list(db.tool_events.find({"attempt_id": current["_id"]}).sort("received_at", 1))
    if events:
        return _adopt_from_events(db, current, events, now, me)
    if current["state"] in FINAL_ATTEMPT_STATES:
        return _adopt(db, current, "post_call", now=now, me=me)
    intent_at = _aware(current["intent_at"])
    if (now - intent_at).total_seconds() < settings.indoubt_grace_s:
        return _deferred("younger than the grace period")
    if probe is None:
        return _deferred(f"no call probe for channel {current.get('channel')}")
    if not current.get("to_number"):
        return _deferred("no number to probe")
    since = intent_at - timedelta(seconds=settings.probe_skew_s)
    try:
        calls = probe.find_calls(to_number=current["to_number"], since=since)
    except Exception as exc:  # noqa: BLE001 - a failed probe is no information, never a reason to redial
        log.warning("probe %s failed for %s: %s", getattr(probe, "name", "?"), current["_id"], exc)
        return _deferred(f"probe failed: {exc}")
    call = match_call(db, current, calls, since)
    if call is not None:
        return _adopt(db, current, probe_evidence(probe), now=now, me=me,
                      state=FINAL_STATUS_TO_ATTEMPT.get(call.status, "dialed"),
                      call_sid=call.call_sid, conversation_id=call.conversation_id)
    if not attempts.abandon(db, current["_id"], "no evidence of a call", now=now, by=me):
        return _deferred("the attempt moved on while recovering")
    feed.emit(db, "attempt_abandoned",
              f"Attempt {current['_id']} for {_party_name(db, current)} abandoned: no evidence of a call. "
              "Dialling again is safe.",
              recall_id=current["recall_id"], party_id=current["party_id"], attempt_id=current["_id"],
              level="warn", source="worker", detail={"owner": current.get("owner"), "by": me})
    return {"action": "abandoned", "evidence": None}


def match_call(db, attempt: dict, calls, since: datetime):
    """The newest probed call that can be this attempt's: started at or after
    `since` (or not started yet and still live), and not already on another
    attempt. Providers may filter by date only, so the timestamp is compared here."""
    candidates = []
    for call in calls:
        start = _aware(call.start_time)
        if start is None and call.status not in LIVE_STATUSES:
            continue
        if start is not None and start < since:
            continue
        candidates.append(call)
    if not candidates:
        return None
    taken = {a["call_sid"] for a in db.attempts.find(
        {"call_sid": {"$in": [c.call_sid for c in candidates]}, "_id": {"$ne": attempt["_id"]}}, {"call_sid": 1})}
    free = [c for c in candidates if c.call_sid not in taken]
    if not free:
        return None
    return max(free, key=lambda c: _aware(c.start_time) or _LATEST)


def probe_evidence(probe) -> str:
    """How the wall names probe evidence: "twilio", or "<name> probe" (sim probe)."""
    name = getattr(probe, "name", "call")
    return "twilio" if name == "twilio" else f"{name} probe"


def check_stale(db, attempt: dict, probe, settings, now: datetime, me: dict) -> dict:
    """A dialed or connected attempt older than stale_call_s with no post-call
    webhook: ask the probe for the call's status and close the attempt to
    match. Returns {"action": "closed"|"live"|"unknown"|"fresh"|"skipped"|"deferred", ...}."""
    current = attempts.get(db, attempt["_id"])
    if current is None or current["state"] not in ("dialed", "connected"):
        return {"action": "skipped"}
    started = _aware(current.get("dialed_at") or current.get("connected_at") or current["intent_at"])
    age = (now - started).total_seconds()
    if age < settings.stale_call_s:
        return {"action": "fresh"}
    if probe is None:
        return _deferred(f"no call probe for channel {current.get('channel')}")
    sid = current.get("call_sid")
    try:
        call = probe.call_status(sid) if sid else _find_own_call(db, current, probe, settings)
    except Exception as exc:  # noqa: BLE001 - no information: ask again next pass
        log.warning("probe %s failed for stale %s: %s", getattr(probe, "name", "?"), current["_id"], exc)
        return _deferred(f"probe failed: {exc}")
    if call is None:
        return {"action": "unknown"}
    state = FINAL_STATUS_TO_ATTEMPT.get(call.status)
    if state is None:
        return {"action": "live", "status": call.status}
    evidence = probe_evidence(probe)
    fields = {} if sid else {"call_sid": call.call_sid}
    if not attempts.complete(db, current["_id"], state, reason=f"stale: {evidence} reports {call.status}",
                             now=now, **fields):
        return {"action": "skipped"}
    feed.emit(db, "attempt_stale",
              f"Attempt {current['_id']} for {_party_name(db, current)} had no post-call after {int(age)} s; "
              f"{evidence} reports the call {call.status}. Closed as {state}.",
              recall_id=current["recall_id"], party_id=current["party_id"], attempt_id=current["_id"],
              level="warn", source="worker",
              detail={"evidence": evidence, "status": call.status, "call_sid": call.call_sid, "by": me})
    return {"action": "closed", "state": state, "status": call.status, "evidence": evidence}


def _find_own_call(db, attempt: dict, probe, settings):
    """A live attempt without a call SID: look its call up the way recovery would."""
    if not attempt.get("to_number"):
        return None
    since = _aware(attempt["intent_at"]) - timedelta(seconds=settings.probe_skew_s)
    return match_call(db, attempt, probe.find_calls(to_number=attempt["to_number"], since=since), since)


def _adopt_from_events(db, attempt: dict, events: list, now: datetime, me: dict) -> dict:
    conversation_id = next((e["conversation_id"] for e in events if e.get("conversation_id")), None)
    call_sid = next((e["call_sid"] for e in events if e.get("call_sid")), None)
    state = None
    if attempt["state"] not in FINAL_ATTEMPT_STATES:
        posts = [e for e in events if e.get("kind") == "post_call"]
        state = _post_call_state(posts[-1]) if posts else "connected"
    return _adopt(db, attempt, "tool_events", now=now, me=me, state=state, call_sid=call_sid,
                  conversation_id=conversation_id)


def _post_call_state(event: dict) -> str:
    """The attempt state a logged post-call payload implies (the API may not
    have applied it yet). An unrecognised payload leaves the call connected,
    and check_stale() settles it."""
    payload = event.get("payload") or {}
    kind = payload.get("type")
    if kind == "post_call_transcription":
        return "completed"
    if kind == "call_initiation_failure":
        return "no_answer" if (payload.get("data") or {}).get("failure_reason") == "no-answer" else "failed"
    return "connected"


def _adopt(db, attempt: dict, evidence: str, *, now: datetime, me: dict, state=None, call_sid=None,
           conversation_id=None) -> dict:
    if not attempts.adopt(db, attempt["_id"], evidence=evidence, conversation_id=conversation_id,
                          call_sid=call_sid, state=state, now=now, by=me):
        return _deferred("resolved elsewhere")
    after = attempts.get(db, attempt["_id"]) or attempt
    sid = after.get("call_sid")
    owner = attempt.get("owner") or {}
    own_boot = owner.get("worker") == me["worker"] and owner.get("boot") == me["boot"]
    how = "was placed but the provider's answer was lost" if own_boot else "happened while the worker was down"
    feed.emit(db, "attempt_adopted",
              f"Attempt {attempt['_id']} for {_party_name(db, attempt)} adopted from {evidence}: "
              f"{f'call {sid}' if sid else 'the call'} {how}. No redial.",
              recall_id=attempt["recall_id"], party_id=attempt["party_id"], attempt_id=attempt["_id"],
              level="good", source="worker",
              detail={"evidence": evidence, "call_sid": sid, "conversation_id": after.get("conversation_id"),
                      "state": after.get("state"), "owner": owner, "by": me})
    return {"action": "adopted", "evidence": evidence, "call_sid": sid,
            "conversation_id": after.get("conversation_id"), "state": after.get("state")}


def _deferred(reason: str) -> dict:
    return {"action": "deferred", "evidence": None, "reason": reason}


def _party_name(db, attempt: dict) -> str:
    party = db.parties.find_one({"recall_id": attempt["recall_id"], "party_id": attempt["party_id"]}, {"name": 1})
    return party["name"] if party else attempt["party_id"]
