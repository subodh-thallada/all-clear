"""Contact attempts and their state machine.

    intent -> dialed -> connected -> completed | no_answer | failed
    intent -> abandoned          (recovery found no evidence of a call)

open_attempt() builds and inserts the intent; build_attempt() and
insert_intent() are its two halves, so the call-slot transaction
(claim.take_slot_and_intent) can insert the same document inside its own
transaction.

Every transition is a conditional update on the current state, so the API
(post-call webhook), the worker and recovery can race without regressing an
attempt. The partial unique index on (recall_id, party_id) while the state is
intent, dialed or connected refuses a second live attempt for one party; that
refusal surfaces as AttemptRefused carrying Atlas's duplicate-key message.

`dial_confirmed` is the worker's own record that it saved the provider's
answer (step 3 of "Placing a call"). An attempt without it is in doubt: the
call may or may not exist, and only recovery may decide, from evidence.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from ..config import FINAL_ATTEMPT_STATES, LIVE_ATTEMPT_STATES

LIVE = list(LIVE_ATTEMPT_STATES)
FINAL = list(FINAL_ATTEMPT_STATES)

# to_state -> states it may be entered from
TRANSITIONS = {
    "dialed": ["intent"],
    "connected": ["intent", "dialed"],
    "completed": LIVE,
    "no_answer": LIVE,
    "failed": LIVE,
    "abandoned": ["intent"],
}


class AttemptRefused(Exception):
    """Atlas refused a second live attempt (or a seq race) for one party."""

    def __init__(self, message: str, *, index: str | None = None, existing: dict | None = None):
        super().__init__(message)
        self.message, self.index, self.existing = message, index, existing


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_attempt_id() -> str:
    return f"att_{secrets.token_hex(7)}"


def build_attempt(db, *, recall_id, party_id, channel, purpose="initial", owner=None, to_number=None,
                  context=None, now=None, attempt_id=None, extra=None, seq=None, session=None) -> dict:
    """The intent document open_attempt() inserts, without inserting it.

    `seq` pins the sequence number a decider decided on. If any attempt took
    that seq after the decider read the party (another worker, the form),
    uniq_recall_party_seq refuses the insert, so a stale plan never dials.
    Without it the next free seq is used."""
    now = now or utcnow()
    if seq is None:
        last = db.attempts.find_one({"recall_id": recall_id, "party_id": party_id}, sort=[("seq", DESCENDING)],
                                    session=session)
        seq = (last["seq"] + 1) if last else 1
    return {
        "_id": attempt_id or new_attempt_id(),
        "recall_id": recall_id, "party_id": party_id, "seq": seq,
        "channel": channel, "state": "intent", "purpose": purpose, "intent_at": now,
        "conversation_id": None, "call_sid": None, "to_number": to_number,
        "owner": owner, "dial_confirmed": False, "context": context or {},
        **(extra or {}),
    }


def insert_intent(db, doc: dict, *, session=None) -> dict:
    """Insert an intent document and move a pending party to contacting.
    Raises AttemptRefused with Atlas's duplicate-key message verbatim."""
    try:
        db.attempts.insert_one(doc, session=session)
    except DuplicateKeyError as exc:
        raise refused(db, exc, doc["recall_id"], doc["party_id"]) from exc
    db.parties.update_one({"recall_id": doc["recall_id"], "party_id": doc["party_id"], "state": "pending"},
                         {"$set": {"state": "contacting"}}, session=session)
    return doc


def refused(db, exc: DuplicateKeyError, recall_id: str, party_id: str) -> AttemptRefused:
    """The AttemptRefused for a duplicate-key error on attempts: which index
    said no, and the live attempt that holds the party (read outside any
    transaction, so it sees what is committed)."""
    msg = (exc.details or {}).get("errmsg", str(exc))
    index = "one_live_attempt_per_party" if "one_live_attempt_per_party" in msg else (
        "uniq_recall_party_seq" if "uniq_recall_party_seq" in msg else None)
    existing = db.attempts.find_one({"recall_id": recall_id, "party_id": party_id, "state": {"$in": LIVE}})
    return AttemptRefused(msg, index=index, existing=existing)


def open_attempt(db, *, recall_id, party_id, channel, purpose="initial", owner=None, to_number=None,
                 context=None, now=None, attempt_id=None, extra=None, seq=None) -> dict:
    """Insert an attempt in state intent (step 1 of placing a call). See
    build_attempt() for `seq`."""
    doc = build_attempt(db, recall_id=recall_id, party_id=party_id, channel=channel, purpose=purpose, owner=owner,
                        to_number=to_number, context=context, now=now, attempt_id=attempt_id, extra=extra, seq=seq)
    return insert_intent(db, doc)


def transition(db, attempt_id, to_state, *, from_states=None, now=None, **fields) -> bool:
    """Move an attempt to to_state if it is currently in one of from_states.
    Returns True if this call made the change."""
    now = now or utcnow()
    allowed = from_states or TRANSITIONS[to_state]
    stamp = {"dialed": "dialed_at", "connected": "connected_at"}.get(to_state, "ended_at")
    res = db.attempts.update_one(
        {"_id": attempt_id, "state": {"$in": list(allowed)}},
        {"$set": {"state": to_state, stamp: now, **fields}})
    return res.modified_count == 1


def _fill_ids(db, attempt_id, conversation_id, call_sid, extra=None):
    """Save identifiers that are still missing, whatever the state."""
    sets = dict(extra or {})
    if conversation_id:
        db.attempts.update_one({"_id": attempt_id, "conversation_id": None},
                               {"$set": {"conversation_id": conversation_id}})
    if call_sid:
        db.attempts.update_one({"_id": attempt_id, "call_sid": None}, {"$set": {"call_sid": call_sid}})
    if sets:
        db.attempts.update_one({"_id": attempt_id}, {"$set": sets})


def mark_dialed(db, attempt_id, *, conversation_id=None, call_sid=None, now=None) -> bool:
    """Step 3: the worker saves the provider's answer. If the API or recovery
    already moved the attempt on, only fill in what is missing."""
    now = now or utcnow()
    ids = {k: v for k, v in (("conversation_id", conversation_id), ("call_sid", call_sid)) if v}
    if transition(db, attempt_id, "dialed", now=now, dial_confirmed=True, **ids):
        return True
    _fill_ids(db, attempt_id, conversation_id, call_sid, {"dial_confirmed": True, "dialed_at": now})
    return False


def mark_connected(db, attempt_id, *, conversation_id=None, call_sid=None, now=None) -> bool:
    """First tool event of a confirmed call (called by the API). An attempt
    still in intent is left alone: it is in doubt and belongs to recovery."""
    ok = transition(db, attempt_id, "connected", from_states=["dialed"], now=now)
    _fill_ids(db, attempt_id, conversation_id, call_sid)
    return ok


def complete(db, attempt_id, state, *, reason=None, now=None, **fields) -> bool:
    """Close a live attempt: completed, no_answer or failed."""
    if state not in ("completed", "no_answer", "failed"):
        raise ValueError(f"not a closing state: {state}")
    extra = {"outcome_reason": reason} if reason else {}
    return transition(db, attempt_id, state, now=now, **extra, **fields)


def abandon(db, attempt_id, reason, *, now=None, by=None) -> bool:
    """Recovery found no evidence of a call. This is the only path that dials again."""
    return transition(db, attempt_id, "abandoned", now=now, outcome_reason=reason, abandoned_by=by)


def adopt(db, attempt_id, *, evidence: str, conversation_id=None, call_sid=None, state=None,
          now=None, by=None) -> bool:
    """Recovery found evidence that the call happened. Save its identifiers,
    record the adoption, and never redial. Returns True if this call adopted it."""
    now = now or utcnow()
    res = db.attempts.update_one(
        {"_id": attempt_id, "dial_confirmed": {"$ne": True}, "state": {"$ne": "abandoned"}},
        {"$set": {"dial_confirmed": True, "adopted": {"evidence": evidence, "at": now, "by": by}}})
    if res.modified_count != 1:
        return False
    _fill_ids(db, attempt_id, conversation_id, call_sid)
    if state:
        if state in ("dialed", "connected"):
            transition(db, attempt_id, state, from_states=["intent"] if state == "dialed" else ["intent", "dialed"],
                       now=now)
        elif state in ("completed", "no_answer", "failed"):
            complete(db, attempt_id, state, reason=f"adopted from {evidence}", now=now)
    return True


def get(db, attempt_id):
    return db.attempts.find_one({"_id": attempt_id})


def live_attempt(db, recall_id, party_id):
    return db.attempts.find_one({"recall_id": recall_id, "party_id": party_id, "state": {"$in": LIVE}})


def in_doubt(attempt: dict) -> bool:
    """The worker never saved the provider's answer for this call."""
    return (attempt.get("channel") != "form" and not attempt.get("dial_confirmed")
            and attempt.get("state") != "abandoned")
