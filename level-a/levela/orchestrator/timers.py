"""Durable timers (V2 section 5; spec "Clock and timers").

Every open party carries its timer as `next_action_at`, in simulated hours
(null: no timer, the party waits for an event). Nothing ticks: a party is due
when its timer is at or before the run's simulated now, and the worker's
loop is one query for what is due. A kill loses nothing, because the timer is
a field in Atlas, and a restart reads only what is due.

release() ends a task run. In one transaction it writes the run's result, the
party's next timer, ladder step and last outcome, and frees the lease
(`lease_until = 1970-01-01`, `lease_owner = null`). The party write carries
the fence, so a worker whose lease was taken over writes nothing at all and
learns it from the False return. Clearing the owner also kills the fence:
after a release, the same run can neither extend nor release again.
"""
from __future__ import annotations

from pymongo import ASCENDING

from .. import clock as simclock
from ..db.client import run_txn
from ..ledger.transfers import FREE_LEASE


class _FenceMoved(Exception):
    """Raised inside the release transaction to abort it."""


def work_filter(recall_id: str | None = None) -> dict:
    """Parties with work left: not reconciled, with an open question, or a
    distributor owing a contract probe after its order system switched (a switch
    is noticed only by an order system call, and a reconciled distributor would
    make none); not handed to a human, not held for the demo."""
    flt = {"$or": [{"state": {"$ne": "reconciled"}}, {"open_questions.0": {"$exists": True}},
                   {"order_system_probe_due": True}],
           "escalation": {"$ne": "human"}, "hold_for_demo": {"$ne": True}}
    if recall_id is not None:
        flt["recall_id"] = recall_id
    return flt


def due_filter(sim_now: float, recall_id: str | None = None) -> dict:
    """Parties with work left whose timer has passed (uses the next_action_at indexes)."""
    return {**work_filter(recall_id), "next_action_at": {"$lte": float(sim_now)}}


def release(client, db, party: dict, fence: dict, *, next_action_at: float | None, ladder_step: int,
            last_outcome, extra: dict | None = None, run_update: dict | None = None) -> bool:
    """Write the run's result and the party's next timer, and free the lease.

    `last_outcome` is `{outcome, at}` (a bare outcome string is stamped with
    the run's simulated now). `extra` adds party fields to the same write
    (the lease fields always win). `run_update` is the task run's `_id` plus
    either the fields to set (`{"_id", "status": "done", ...}`) or an update
    document (`{"_id", "$set": {...}, "$inc": {...}}`), applied to that task
    run in the same transaction. Returns False, having written nothing, when
    the fence no longer matches.
    """
    outcome = _outcome(db, party, last_outcome)
    sets = {**(extra or {}), "next_action_at": _hours(next_action_at), "ladder_step": int(ladder_step),
            "last_outcome": outcome, "lease_until": FREE_LEASE, "lease_owner": None}
    run_id, run_change = _run_update(run_update)

    def txn(session):
        if db.parties.update_one(dict(fence), {"$set": sets}, session=session).matched_count != 1:
            raise _FenceMoved()
        if run_id is not None:
            db.task_runs.update_one({"_id": run_id}, run_change, session=session)
        return True

    try:
        return run_txn(client, txn)
    except _FenceMoved:
        return False


def due_count(db, recall_id: str | None, sim_now: float) -> int:
    """How many parties are due now (leased or not)."""
    return db.parties.count_documents(due_filter(sim_now, recall_id))


def next_due_at(db, recall_id: str | None) -> float | None:
    """The earliest timer among parties with work left, or None if no party has one."""
    flt = {**work_filter(recall_id), "next_action_at": {"$type": "number"}}
    row = db.parties.find_one(flt, {"next_action_at": 1}, sort=[("next_action_at", ASCENDING)])
    return float(row["next_action_at"]) if row else None


def seconds_until(clock: simclock.Clock, sim_hour: float | None, real_now, *, cap_s: float) -> float:
    """Real seconds to sleep until `sim_hour`, never more than cap_s (a timer
    set while sleeping, or a clock change, is noticed within cap_s)."""
    if sim_hour is None:
        return cap_s
    return max(0.0, min(cap_s, clock.seconds_until(sim_hour, real_now)))


def _hours(value: float | None) -> float | None:
    return None if value is None else float(value)


def _outcome(db, party: dict, last_outcome) -> dict | None:
    if last_outcome is None or isinstance(last_outcome, dict):
        return last_outcome
    return {"outcome": str(last_outcome), "at": simclock.sim_now(db, party["recall_id"], db.client)}


def _run_update(run_update: dict | None) -> tuple[str | None, dict]:
    """(task run id, update document) from release()'s run_update."""
    if not run_update:
        return None, {}
    fields = dict(run_update)
    run_id = fields.pop("_id", None)
    if run_id is None:
        raise ValueError("run_update needs the task run's _id")
    operators = [k for k in fields if k.startswith("$")]
    if operators and len(operators) != len(fields):
        raise ValueError("run_update mixes update operators with plain fields")
    return run_id, (fields if operators else {"$set": fields})
