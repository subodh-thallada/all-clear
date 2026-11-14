"""The run document behind the wall's counters (V2 §3).

One per recall, `_id = "run:<recall_id>"`: the clock, steps, kills,
duplicates, model calls, tokens per decision, the last invariant check and
the supervisor / saboteur switches. Counters only ever move by `$inc`, so
any number of workers can bump them at once.

`state` is `running` or `paused` (spec "Stop it mid-run", test 40).
POST /admin/pause sets `paused`: the claim (orchestrator.claim.claim_next)
requires `running`, so no worker takes new work, while calls already in
flight finish and are recorded by the API as usual. POST /admin/resume sets
it back. A run document without `state` (written before this field) counts
as running.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument

from .clock import DEFAULT_SECONDS_PER_SIM_HOUR, DEFAULT_SIM_START, Clock, forget


RUNNING, PAUSED = "running", "paused"
RUN_STATES = (RUNNING, PAUSED)


def run_id(recall_id: str) -> str:
    return f"run:{recall_id}"


def create_run(db, recall_id: str, *, real_start: datetime | None = None,
               seconds_per_sim_hour: float = DEFAULT_SECONDS_PER_SIM_HOUR,
               sim_start: float = DEFAULT_SIM_START, parties_at_start: int = 0,
               schedule: dict | None = None, session=None) -> dict:
    now = real_start or datetime.now(timezone.utc)
    clock = Clock(real_start=now, sim_start=sim_start, seconds_per_sim_hour=seconds_per_sim_hour)
    schedule = schedule or {}
    doc = {
        "_id": run_id(recall_id), "recall_id": recall_id, "clock": clock.to_doc(),
        "steps": 0, "kills": 0, "duplicates": 0, "model_calls": 0,
        "tokens": {"last": 0, "max": 0, "total": 0, "n": 0},
        "last_invariant_check": None, "invariant_failed": False,
        "supervisor": {"paused": False}, "saboteur": {"on": False, "until": None},
        "parties_at_start": parties_at_start, "schedule": schedule,
        "events": {"order_system_change_at": dict(schedule.get("order_system_change_at") or {})},
        "planner_wake": True, "started_at": now, "state": RUNNING,
    }
    db.runs.insert_one(doc, session=session)
    return doc


def get_run(db, recall_id: str) -> dict | None:
    return db.runs.find_one({"_id": run_id(recall_id)})


def inc(db, recall_id: str, field: str, n: int = 1, session=None) -> None:
    db.runs.update_one({"_id": run_id(recall_id)}, {"$inc": {field: n}}, session=session)


def record_tokens(db, recall_id: str, tokens: int) -> None:
    db.runs.update_one({"_id": run_id(recall_id)}, [
        {"$set": {"tokens.last": tokens,
                  "tokens.max": {"$max": [{"$ifNull": ["$tokens.max", 0]}, tokens]},
                  "tokens.total": {"$add": [{"$ifNull": ["$tokens.total", 0]}, tokens]},
                  "tokens.n": {"$add": [{"$ifNull": ["$tokens.n", 0]}, 1]}}},
    ])


def set_clock(db, recall_id: str, clock: Clock) -> dict | None:
    out = db.runs.find_one_and_update({"_id": run_id(recall_id)}, {"$set": {"clock": clock.to_doc()}},
                                      return_document=ReturnDocument.AFTER)
    forget(db)
    return out


def wake_planner(db, recall_id: str, reason: str) -> None:
    db.runs.update_one({"_id": run_id(recall_id)}, {"$set": {"planner_wake": True, "planner_wake_reason": reason}})


# ------------------------------------------------------------ pause / resume

def paused_recall_ids(db, recall_ids=None, *, session=None) -> list[str]:
    """The recalls whose run is paused (optionally among `recall_ids`)."""
    flt = {"state": PAUSED}
    if recall_ids is not None:
        flt["recall_id"] = {"$in": list(recall_ids)}
    return [r["recall_id"] for r in db.runs.find(flt, {"recall_id": 1}, session=session)]


def is_running(db, recall_id: str, *, session=None) -> bool:
    """False only when the recall's run says `paused` (no run document: running)."""
    doc = db.runs.find_one({"_id": run_id(recall_id)}, {"state": 1}, session=session)
    return (doc or {}).get("state", RUNNING) != PAUSED


def set_state(db, recall_id: str, state: str, *, by: str | None = None, reason: str | None = None,
              now: datetime | None = None) -> dict | None:
    """Pause or resume one recall's run. Returns the run after the change, or
    None when the recall has no run document. Nothing else is touched: every
    party, lease, attempt and operation stays where it is in Atlas, so a
    resume picks up exactly where the pause left off."""
    if state not in RUN_STATES:
        raise ValueError(f"run state must be one of {RUN_STATES}, not {state!r}")
    now = now or datetime.now(timezone.utc)
    stamp = "paused_at" if state == PAUSED else "resumed_at"
    return db.runs.find_one_and_update(
        {"_id": run_id(recall_id)},
        {"$set": {"state": state, stamp: now, "state_by": by, "state_reason": reason},
         "$push": {"state_log": {"$each": [{"state": state, "at": now, "by": by, "reason": reason}],
                                 "$slice": -50}}},
        return_document=ReturnDocument.AFTER)
