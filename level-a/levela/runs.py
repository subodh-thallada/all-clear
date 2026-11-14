"""The run document behind the wall's counters (V2 §3).

One per recall, `_id = "run:<recall_id>"`: the clock, steps, kills,
duplicates, model calls, tokens per decision, the last invariant check and
the supervisor / saboteur switches. Counters only ever move by `$inc`, so
any number of workers can bump them at once.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument

from .clock import DEFAULT_SECONDS_PER_SIM_HOUR, DEFAULT_SIM_START, Clock, forget


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
        "planner_wake": True, "started_at": now,
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
