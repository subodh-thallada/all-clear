"""Leases on parties: spec test 23 (claim race, database clock) and test 24
(stale worker), plus the due filter, extend and release.

Lease expiry is always arranged with a pipeline update relative to `$$NOW`,
so no assertion here depends on the laptop's clock.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

import pytest
from pymongo import MongoClient, monitoring

from levela.db.seed import load_scenario, start_recall
from levela.ledger.transfers import FREE_LEASE, stub_party
from levela.orchestrator import claim, timers
from levela.orchestrator.attempts import build_attempt, utcnow

RID = "RCL-2026-017"
SIM_NOW = 10.0          # recall_1's parties are due from 9.0 (sim_start)


@pytest.fixture
def recall(client, db):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    return RID


def _only_due(db, party_id):
    """Push every other party's timer far into the future."""
    db.parties.update_many({"recall_id": RID, "party_id": {"$ne": party_id}}, {"$set": {"next_action_at": 1e6}})


def _set_lease_from_now(db, party_id, ms):
    """lease_until = $$NOW + ms, computed by the server."""
    db.parties.update_one({"recall_id": RID, "party_id": party_id},
                         [{"$set": {"lease_until": {"$add": ["$$NOW", ms]}}}])


def _party(db, party_id):
    return db.parties.find_one({"recall_id": RID, "party_id": party_id})


# -- test 23: the claim race ---------------------------------------------------------

def test_eight_workers_race_one_due_party_and_exactly_one_wins(db, recall):
    _only_due(db, "lakeside-systems")
    barrier = threading.Barrier(8, timeout=30)
    won, errors = {}, []

    def race(i):
        try:
            barrier.wait()
            won[i] = claim.claim_next(db, worker_id=f"w{i}", sim_now=SIM_NOW, recall_id=RID)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    winners = {i: s for i, s in won.items() if s is not None}
    assert len(winners) == 1, f"exactly one claim succeeds, got {sorted(winners)}"
    [(i, party)] = winners.items()
    assert (party["party_id"], party["lease_owner"], party["lease_epoch"]) == ("lakeside-systems", f"w{i}", 1)
    assert _party(db, "lakeside-systems")["lease_epoch"] == 1, "the epoch rose by exactly 1"


class _Commands(monitoring.CommandListener):
    def __init__(self):
        self.events = []

    def started(self, event):
        self.events.append(event)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


def _datetimes(value):
    if isinstance(value, datetime):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _datetimes(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _datetimes(v)


def test_a_lease_expires_by_the_database_clock(db, recall, test_uri):
    _only_due(db, "lakeside-systems")
    listener = _Commands()
    spy = MongoClient(test_uri, tz_aware=True, event_listeners=[listener])
    try:
        first = claim.claim_next(spy[db.name], worker_id="w1", sim_now=SIM_NOW, recall_id=RID, lease_s=30)
    finally:
        spy.close()
    [cmd] = [e.command for e in listener.events if e.command_name == "findAndModify"]
    sent = json.dumps({"query": cmd["query"], "update": cmd["update"]}, default=str)
    assert sent.count("$$NOW") == 2, "the server's clock in the filter and in the update"
    assert not list(_datetimes(cmd["query"])) and not list(_datetimes(cmd["update"])), \
        "no worker clock is sent with the claim"

    held = {"recall_id": RID, "party_id": "lakeside-systems",
            "$expr": {"$and": [{"$gt": ["$lease_until", {"$add": ["$$NOW", 25_000]}]},
                               {"$lte": ["$lease_until", {"$add": ["$$NOW", 30_000]}]}]}}
    assert db.parties.count_documents(held) == 1, "lease_until = $$NOW + 30 s on the server"
    assert claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW, recall_id=RID) is None

    _set_lease_from_now(db, "lakeside-systems", 60_000)
    assert claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW, recall_id=RID) is None, "still in the future"
    _set_lease_from_now(db, "lakeside-systems", -1)
    second = claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW, recall_id=RID)
    assert (second["lease_owner"], second["lease_epoch"]) == ("w2", first["lease_epoch"] + 1)


def test_the_due_query_and_its_order(db, recall):
    """Due and with work left; never held, handed to a human, done, early or
    leased; most urgent first."""
    now = utcnow()
    extra = {
        "held": {"hold_for_demo": True},
        "human": {"escalation": "human"},
        "done": {"state": "reconciled"},
        "asked": {"state": "reconciled", "open_questions": ["q-01"], "priority": 5},
        "early": {"next_action_at": SIM_NOW + 1},
        "no-timer": {"next_action_at": None},
        "leased": {"lease_owner": "w9", "lease_epoch": 3},
    }
    db.parties.insert_many([{**stub_party(RID, sid, sid, None, "claim:queen-st-alarm", now,
                                         next_action_at=SIM_NOW - 5), **fields} for sid, fields in extra.items()])
    _set_lease_from_now(db, "leased", 60_000)
    db.parties.update_one({"recall_id": RID, "party_id": "harbour-fire"}, {"$set": {"next_action_at": 9.5}})
    assert timers.due_count(db, RID, SIM_NOW) == 5, "3 direct + asked + leased"
    assert timers.next_due_at(db, RID) == SIM_NOW - 5

    order = []
    while (party := claim.claim_next(db, worker_id="w1", sim_now=SIM_NOW, recall_id=RID)) is not None:
        order.append(party["party_id"])
    assert order == ["asked", "lakeside-systems", "queen-st-alarm", "harbour-fire"]
    assert claim.claim_next(db, worker_id="w1", sim_now=SIM_NOW + 2, recall_id=RID)["party_id"] == "early"


def test_extend_moves_only_the_holders_lease(db, recall):
    _only_due(db, "lakeside-systems")
    party = claim.claim_next(db, worker_id="w1", sim_now=SIM_NOW, recall_id=RID, lease_s=1)
    f = claim.fence(party)
    assert f == {"_id": f"{RID}:lakeside-systems", "lease_owner": "w1", "lease_epoch": 1}
    assert claim.extend(db, f["_id"], "w1", 1, lease_s=120)
    far = {"_id": f["_id"], "$expr": {"$gt": ["$lease_until", {"$add": ["$$NOW", 100_000]}]}}
    assert db.parties.count_documents(far) == 1
    assert not claim.extend(db, f["_id"], "w1", 2, lease_s=120), "wrong epoch"
    assert not claim.extend(db, f["_id"], "w2", 1, lease_s=120), "wrong owner"
    with pytest.raises(ValueError, match="party document's _id"):
        claim.extend(db, "lakeside-systems", "w1", 1)


# -- test 24: the stale worker -------------------------------------------------------

def test_a_worker_paused_past_its_lease_writes_nothing_and_the_second_result_stands(client, db, recall):
    _only_due(db, "queen-st-alarm")
    db.task_runs.insert_many([{"_id": run, "party_id": "queen-st-alarm", "status": "running"}
                              for run in ("tr-a", "tr-b")])
    a = claim.claim_next(db, worker_id="w1", sim_now=SIM_NOW, recall_id=RID)
    fence_a = claim.fence(a)

    _set_lease_from_now(db, "queen-st-alarm", -1)          # w1 is paused past its lease
    b = claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW, recall_id=RID)
    fence_b = claim.fence(b)
    assert (b["lease_owner"], b["lease_epoch"]) == ("w2", fence_a["lease_epoch"] + 1)

    # w1 resumes: every fenced write matches nothing.
    assert not claim.extend(db, fence_a["_id"], "w1", fence_a["lease_epoch"])
    attempt = build_attempt(db, recall_id=RID, party_id="queen-st-alarm", channel="voice")
    with pytest.raises(claim.LeaseLost):
        claim.take_slot_and_intent(db, attempt, fence=fence_a)
    assert not timers.release(client, db, a, fence_a, next_action_at=99.0, ladder_step=3,
                              last_outcome={"outcome": "no_answer", "at": SIM_NOW},
                              run_update={"_id": "tr-a", "status": "done"})
    assert db.attempts.count_documents({}) == 0
    assert db.task_runs.find_one({"_id": "tr-a"})["status"] == "running"
    held = _party(db, "queen-st-alarm")
    assert (held["lease_owner"], held["next_action_at"], held["ladder_step"]) == ("w2", 9.0, 1)

    # w2's result stands, and w1 still cannot overwrite it.
    assert timers.release(client, db, b, fence_b, next_action_at=13.0, ladder_step=2, last_outcome="no_answer",
                          extra={"memory_hour": 14},
                          run_update={"_id": "tr-b", "$set": {"status": "done"}, "$inc": {"used.tool_calls": 1}})
    assert not timers.release(client, db, a, fence_a, next_action_at=99.0, ladder_step=3, last_outcome="no_answer")
    after = _party(db, "queen-st-alarm")
    assert (after["next_action_at"], after["ladder_step"], after["memory_hour"]) == (13.0, 2, 14)
    assert after["last_outcome"]["outcome"] == "no_answer" and after["last_outcome"]["at"] >= 9.0
    assert (after["lease_owner"], after["lease_until"]) == (None, FREE_LEASE)
    run_b = db.task_runs.find_one({"_id": "tr-b"})
    assert (run_b["status"], run_b["used"]["tool_calls"]) == ("done", 1)


def test_a_released_party_is_claimed_again_only_when_its_timer_is_due(client, db, recall):
    _only_due(db, "harbour-fire")
    party = claim.claim_next(db, worker_id="w1", sim_now=SIM_NOW, recall_id=RID)
    f = claim.fence(party)
    assert timers.release(client, db, party, f, next_action_at=SIM_NOW + 4, ladder_step=2,
                          last_outcome={"outcome": "no_answer", "at": SIM_NOW})
    assert not claim.extend(db, f["_id"], "w1", f["lease_epoch"]), "a released run's fence is dead"
    assert not timers.release(client, db, party, f, next_action_at=None, ladder_step=1, last_outcome=None)
    assert claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW + 3.9, recall_id=RID) is None
    again = claim.claim_next(db, worker_id="w2", sim_now=SIM_NOW + 4, recall_id=RID)
    assert (again["party_id"], again["lease_epoch"], again["ladder_step"]) == ("harbour-fire", 2, 2)
    for bad, why in (({"status": "done"}, "_id"), ({"_id": "tr-x", "$set": {}, "status": "done"}, "mixes")):
        with pytest.raises(ValueError, match=why):
            timers.release(client, db, again, claim.fence(again), next_action_at=None, ladder_step=2,
                           last_outcome=None, run_update=bad)
