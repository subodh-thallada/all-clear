"""Call slots: spec test 32 (two workers race for the last slot) and how
slots are counted.

Slots in use are counted from live voice attempts inside the transaction
that inserts the intent, and that transaction also increments the capacity
document, so the two racers conflict on it: Atlas aborts one, and its retry
counts again and finds the slot gone.
"""
from __future__ import annotations

import threading

import pytest

from levela.db.client import run_txn
from levela.db.seed import load_scenario, start_recall
from levela.orchestrator import claim
from levela.orchestrator.attempts import AttemptRefused, build_attempt, complete, open_attempt

RID = "RCL-2026-017"
SIM_NOW = 10.0


@pytest.fixture
def recall(client, db):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    claim.set_max_live(db, 2)
    return RID


def _intent(db, party_id, channel="voice"):
    return build_attempt(db, recall_id=RID, party_id=party_id, channel=channel, to_number="+14165550100")


def _version(db):
    return db.capacity.find_one({"_id": claim.CAPACITY_ID})["version"]


def test_two_workers_race_for_the_last_slot_and_the_loser_retries_and_finds_none(client, db, recall, monkeypatch):
    open_attempt(db, recall_id=RID, party_id="harbour-market", channel="voice")       # one of two slots taken
    db.parties.update_one({"recall_id": RID, "party_id": "harbour-market"}, {"$set": {"next_action_at": None}})
    parties = {}
    for wid in ("w1", "w2"):
        parties[wid] = claim.claim_next(db, worker_id=wid, sim_now=SIM_NOW, recall_id=RID)
    assert {s["party_id"] for s in parties.values()} == {"lakeside-foods", "queen-st-grocer"}
    before = _version(db)

    # Both transactions count before either writes: a true race for the last slot.
    counted = threading.Barrier(2, timeout=30)
    first_count = set()
    real_count = claim.live_voice_calls

    def count_then_race(db_, *, session=None):
        n = real_count(db_, session=session)
        me = threading.get_ident()
        if me not in first_count:
            first_count.add(me)
            counted.wait()
        return n

    monkeypatch.setattr(claim, "live_voice_calls", count_then_race)
    results, runs, dialled, errors = {}, {}, [], []

    def worker(wid):
        party, counter = parties[wid], {}
        try:
            results[wid] = claim.take_slot_and_intent(db, _intent(db, party["party_id"]),
                                                      fence=claim.fence(party), counter=counter)
            runs[wid] = counter["runs"]
            if results[wid] is not None:
                dialled.append(party["party_id"])
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(wid,)) for wid in parties]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert errors == []
    winners = [w for w, r in results.items() if r is not None]
    assert len(winners) == 1 and len(dialled) == 1, "one dials"
    [loser] = set(parties) - set(winners)
    assert results[loser] is None and runs[loser] >= 2, "the loser's transaction retried and found no slot"
    assert db.attempts.count_documents({"channel": "voice", "state": "intent"}) == 2
    assert db.attempts.count_documents({"party_id": parties[loser]["party_id"]}) == 0
    assert _version(db) == before + 1


def test_slots_are_counted_from_live_voice_attempts(client, db, recall):
    claim.set_max_live(db, 1)
    assert claim.slots(db) == {"max_live": 1, "live": 0, "free": 1}
    open_attempt(db, recall_id=RID, party_id="lakeside-foods", channel="sim")          # simulated: no slot
    held = claim.take_slot_and_intent(db, _intent(db, "harbour-market"))
    assert held is not None and claim.slots(db)["free"] == 0
    assert claim.take_slot_and_intent(db, _intent(db, "queen-st-grocer")) is None
    with pytest.raises(AttemptRefused) as refused:
        claim.set_max_live(db, 5)
        claim.take_slot_and_intent(db, _intent(db, "harbour-market"))
    assert "E11000" in refused.value.message and refused.value.index == "one_live_attempt_per_party"

    claim.set_max_live(db, 1)
    complete(db, held["_id"], "completed")            # the call ended: its slot is free again
    inside = run_txn(client, lambda s: claim.take_slot_and_intent(db, _intent(db, "queen-st-grocer"),
                                                                 session=s))
    assert inside is not None and claim.slots(db) == {"max_live": 1, "live": 1, "free": 0}
    assert db.parties.find_one({"recall_id": RID, "party_id": "queen-st-grocer"})["state"] == "contacting"
