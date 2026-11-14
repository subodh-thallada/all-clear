"""Leases on parties, and slots for real calls (V2 section 5; spec "Claiming
work" and "Slots for real calls").

A worker claims the next due party with one findOneAndUpdate. Five rules
make it safe:

1. The database clock decides. `$$NOW` is evaluated by the server, in the
   filter (through `$expr`) and in the pipeline update. No worker's clock is
   ever compared with another's, so a laptop whose clock drifts cannot steal
   a lease or keep one.
2. The epoch is a fencing token. Every later write for the party carries
   fence(party) = {_id, lease_owner, lease_epoch} in its filter. A worker
   whose lease expired and was taken over writes nothing, because the epoch
   has moved on.
3. Heartbeat: extend() moves lease_until forward, under the fence, while a
   call is in progress.
4. Recovery runs at claim time (the worker resolves what an earlier epoch
   left in doubt before anything new).
5. Release and result are one transaction (timers.release).

A real call also needs a slot. Slots in use are counted from live voice
attempts, never kept in a counter, so a killed worker cannot leak one. The
count and the attempt's intent are written in one transaction that also
increments `capacity.version`: two workers racing for the last slot both
write that document, Atlas aborts one with a write conflict, and its retry
counts again and finds none.
"""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, ReturnDocument

from ..config import LIVE_ATTEMPT_STATES
from ..db.client import run_txn
from .attempts import insert_intent
from .timers import due_filter

DEFAULT_LEASE_S = 60.0
CAPACITY_ID = "voice"
DEFAULT_MAX_LIVE = 2        # two real phones at once (Twilio without an approved profile)

CLAIM_SORT = [("priority", DESCENDING), ("next_action_at", ASCENDING), ("_id", ASCENDING)]


class LeaseLost(Exception):
    """The fence no longer matches the party: another worker holds it now."""


def _ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000))


def claim_next(db, *, worker_id: str, sim_now: float, recall_id: str | None = None,
               lease_s: float = DEFAULT_LEASE_S) -> dict | None:
    """Lease the most urgent due party, or return None when nothing is due.

    The spec's findOneAndUpdate, exactly: due and free (`lease_until < $$NOW`,
    judged by the server), sorted by priority then by how long it has been
    due (`_id` only breaks exact ties), and a pipeline update that stamps the
    owner, a lease measured from `$$NOW` and the next epoch. Returns the
    party after the update, so `fence(party)` is this worker's token.
    """
    flt = {**due_filter(sim_now, recall_id), "$expr": {"$lt": ["$lease_until", "$$NOW"]}}
    update = [{"$set": {
        "lease_owner": worker_id,
        "lease_until": {"$add": ["$$NOW", _ms(lease_s)]},
        "lease_epoch": {"$add": [{"$ifNull": ["$lease_epoch", 0]}, 1]},
    }}]
    return db.parties.find_one_and_update(flt, update, sort=CLAIM_SORT, return_document=ReturnDocument.AFTER)


def fence(party: dict) -> dict:
    """The fencing token of a claimed party, usable as-is as a filter."""
    return {"_id": party["_id"], "lease_owner": party["lease_owner"], "lease_epoch": party["lease_epoch"]}


def extend(db, party_id: str, worker_id: str, epoch: int, lease_s: float = DEFAULT_LEASE_S) -> bool:
    """Push this worker's lease to `$$NOW + lease_s`. False when the lease was
    taken over (or released): the caller must stop acting on the party.

    `party_id` is the party document's `_id` ("<recall>:<party>", as in
    fence(party)["_id"]): a bare party slug is ambiguous across recalls, and
    an extend that silently matched nothing would let the lease lapse.
    """
    if ":" not in str(party_id):
        raise ValueError(f"extend() takes the party document's _id (fence(party)['_id']), not {party_id!r}")
    res = db.parties.update_one({"_id": party_id, "lease_owner": worker_id, "lease_epoch": epoch},
                               [{"$set": {"lease_until": {"$add": ["$$NOW", _ms(lease_s)]}}}])
    return res.matched_count == 1


def holds(db, fence_: dict, *, session=None) -> bool:
    """True while the fence still matches the party."""
    return db.parties.find_one(fence_, {"_id": 1}, session=session) is not None


# ------------------------------------------------------------------ call slots

def live_voice_calls(db, *, session=None) -> int:
    """Real calls in progress, counted from the attempts themselves (every
    recall: the phones are shared)."""
    return db.attempts.count_documents({"channel": "voice", "state": {"$in": list(LIVE_ATTEMPT_STATES)}},
                                       session=session)


def max_live(db, *, session=None) -> int:
    """capacity.max_live, set from the accounts on the day (default 2)."""
    doc = db.capacity.find_one({"_id": CAPACITY_ID}, {"max_live": 1}, session=session) or {}
    return int(doc.get("max_live", DEFAULT_MAX_LIVE))


def slots(db) -> dict:
    """{max_live, live, free} for the planner and the wall."""
    cap, live = max_live(db), live_voice_calls(db)
    return {"max_live": cap, "live": live, "free": max(0, cap - live)}


def set_max_live(db, n: int) -> None:
    """Set the number of real calls allowed at once."""
    if n < 0:
        raise ValueError("max_live cannot be negative")
    db.capacity.update_one({"_id": CAPACITY_ID}, {"$set": {"max_live": int(n)}, "$inc": {"version": 1}},
                           upsert=True)


def take_slot_and_intent(db, attempt: dict, *, session=None, fence: dict | None = None,
                         counter: dict | None = None) -> dict | None:
    """Take a call slot and insert the attempt's intent, in one transaction.

    `attempt` is an intent document (attempts.build_attempt). Returns it once
    inserted, or None when every slot is taken (nothing is written). With
    `fence`, the transaction also checks the caller still holds the party
    and raises LeaseLost if not. Raises attempts.AttemptRefused when the
    party already has a live attempt.

    Pass `session` to run inside the caller's own transaction (the gate's
    lease, budget and intent checks are one transaction); otherwise this
    runs its own, and `counter["runs"]` reports how many times it ran: 2 or
    more means a write conflict on the capacity document made it count again.
    """
    def body(s):
        if fence is not None and not holds(db, fence, session=s):
            raise LeaseLost(f"lease on {fence['_id']} moved past epoch {fence['lease_epoch']}")
        if live_voice_calls(db, session=s) >= max_live(db, session=s):
            return None
        insert_intent(db, attempt, session=s)
        # Both racers write this document, so Atlas lets only one commit.
        db.capacity.update_one({"_id": CAPACITY_ID},
                               {"$inc": {"version": 1}, "$setOnInsert": {"max_live": DEFAULT_MAX_LIVE}},
                               upsert=True, session=s)
        return attempt

    if session is not None:
        return body(session)
    return run_txn(db.client, body, counter=counter)
