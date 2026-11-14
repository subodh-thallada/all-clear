"""The run document (V2 §3): its shape, the counters, tokens per decision,
and the clock every process reads from it."""
from __future__ import annotations

from datetime import datetime, timezone

from levela.clock import Clock, forget, get_clock
from levela.runs import create_run, get_run, inc, record_tokens, run_id, set_clock, wake_planner

RID = "RCL-RUN"
T0 = datetime(2026, 11, 14, 21, 55, tzinfo=timezone.utc)
SCHEDULE = {"sim_start": 0.0, "hold_slice_in_demo": True, "order_system_change_at": {"north": 81.5, "east": 82.0}}


def test_create_run_shape(db):
    doc = create_run(db, RID, real_start=T0, seconds_per_sim_hour=0.05, sim_start=0.0, parties_at_start=5,
                     schedule=SCHEDULE)
    stored = get_run(db, RID)
    assert stored == doc and stored["_id"] == run_id(RID) == "run:RCL-RUN"
    assert {k: stored[k] for k in ("steps", "kills", "duplicates", "model_calls", "invariant_failed",
                                   "last_invariant_check", "parties_at_start", "planner_wake")} == {
        "steps": 0, "kills": 0, "duplicates": 0, "model_calls": 0, "invariant_failed": False,
        "last_invariant_check": None, "parties_at_start": 5, "planner_wake": True}
    assert stored["tokens"] == {"last": 0, "max": 0, "total": 0, "n": 0}
    assert stored["supervisor"] == {"paused": False} and stored["saboteur"] == {"on": False, "until": None}
    assert stored["events"] == {"order_system_change_at": {"north": 81.5, "east": 82.0}}
    assert stored["schedule"] == SCHEDULE
    assert Clock.from_doc(stored["clock"]) == Clock(real_start=T0, sim_start=0.0, seconds_per_sim_hour=0.05)
    assert get_run(db, "RCL-none") is None


def test_counters_and_tokens(db):
    create_run(db, RID, real_start=T0)
    inc(db, RID, "steps")
    inc(db, RID, "steps", 4)
    inc(db, RID, "kills")
    for tokens in (1850, 1700, 1900):
        record_tokens(db, RID, tokens)
    wake_planner(db, RID, "party_created")
    run = get_run(db, RID)
    assert (run["steps"], run["kills"]) == (5, 1)
    assert run["tokens"] == {"last": 1900, "max": 1900, "total": 5450, "n": 3}
    assert (run["planner_wake"], run["planner_wake_reason"]) == (True, "party_created")


def test_set_clock_is_seen_at_once_and_reads_are_cached(client, db):
    create_run(db, RID, real_start=T0, seconds_per_sim_hour=30.0)
    assert get_clock(db, RID, max_age_s=0).seconds_per_sim_hour == 30.0
    faster = Clock.from_doc(get_run(db, RID)["clock"]).with_speed(15.0, T0)
    assert set_clock(db, RID, faster)["clock"]["seconds_per_sim_hour"] == 15.0
    assert get_clock(db, RID, max_age_s=60).seconds_per_sim_hour == 15.0     # set_clock forgot the cache

    # A write behind the cache's back is not seen until the entry is forgotten,
    # and forgetting works through any handle on the same database.
    db.runs.update_one({"_id": run_id(RID)}, {"$set": {"clock.seconds_per_sim_hour": 0.05}})
    assert get_clock(db, RID, max_age_s=60).seconds_per_sim_hour == 15.0
    forget(client[db.name])
    assert get_clock(db, RID, max_age_s=60).seconds_per_sim_hour == 0.05
    assert get_clock(db, "RCL-none", max_age_s=0).sim_start == 9.0           # no run: the default clock
