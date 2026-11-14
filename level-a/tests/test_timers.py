"""Durable timers: spec test 21.

The worker is killed with five timers pending and restarted after they are
due. Each fires once, and the restart reads only due and in-doubt
documents: a command listener on the restarted worker's own client counts
every document the server returned to it, per collection.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

from pymongo import MongoClient, monitoring

from levela import clock as simclock
from levela import runs
from levela.db.seed import load_scenario, start_recall
from levela.ledger.transfers import stub_party
from levela.orchestrator import timers
from levela.orchestrator.attempts import utcnow
from levela.orchestrator.failpoints import Failpoints, raise_crash
from levela.orchestrator.worker import WorkerPool
from levela.providers import FakeVoice

RID = "RCL-2026-017"
PENDING = ["harbour-fire", "lakeside-systems", "queen-st-alarm", "party-a", "party-b"]
HISTORY = ["Bloor Alarm", "Annex Fire", "Danforth Market", "Leslieville Pantry"]
LEDGER = ("parties", "attempts", "operations", "claims", "edges", "tool_events", "task_runs", "traces", "episodes")


class FiringHarness:
    """harness.execute reduced to a timer firing: record it, release with no next timer."""

    HarnessContext = staticmethod(lambda **fields: SimpleNamespace(**fields))

    def __init__(self):
        self.fired: list[str] = []

    def execute(self, ctx, party, *, worker_id, fence, decider):
        self.fired.append(party["party_id"])
        timers.release(ctx.client, ctx.db, party, fence, next_action_at=None, ladder_step=party["ladder_step"],
                       last_outcome="claim_accepted")


class DocumentsRead(monitoring.CommandListener):
    """Documents in every reply's cursor batch, by collection."""

    def __init__(self):
        self.by_collection: Counter = Counter()
        self._collection: dict[int, str] = {}

    def started(self, event):
        coll = event.command.get(event.command_name)
        if event.command_name in ("find", "aggregate", "getMore") and isinstance(coll, str):
            self._collection[event.request_id] = event.command.get("collection", coll)

    def succeeded(self, event):
        coll = self._collection.pop(event.request_id, None)
        cursor = (event.reply or {}).get("cursor") or {}
        batch = cursor.get("firstBatch", cursor.get("nextBatch")) or []
        if coll is not None:
            self.by_collection[coll] += len(batch)

    def failed(self, event):
        self._collection.pop(event.request_id, None)


def _pool(settings, db, client, harness):
    voice = FakeVoice()
    return WorkerPool(settings, db, client, providers={"sim": voice, "voice": voice},
                      probes={"sim": voice.probe, "voice": voice.probe},
                      failpoints=Failpoints(db, crash=raise_crash, env={}, worker=settings.worker_name),
                      harness=harness, workers=1)


def test_timers_survive_a_kill_each_fires_once_and_the_restart_reads_only_what_is_due(
        client, db, settings, test_uri):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    clock = simclock.get_clock(db, RID, max_age_s=0)
    runs.set_clock(db, RID, clock.with_speed(0.05, simclock.server_now(client)))
    clock = simclock.get_clock(db, RID, max_age_s=0)
    now = clock.sim_now(simclock.server_now(client))
    due_at = {sid: now + 40 + i for i, sid in enumerate(PENDING)}         # 2.0 to 2.2 real seconds ahead
    stubs = [stub_party(RID, sid, sid, None, "claim:queen-st-alarm", utcnow(), next_action_at=due_at[sid])
             for sid in PENDING[3:]]
    # A long history the restart must not read: settled parties and far timers.
    stubs += [{**stub_party(RID, f"old-{i}", name, None, "claim:queen-st-alarm", utcnow()),
               "state": "reconciled", "next_action_at": None} for i, name in enumerate(HISTORY)]
    stubs += [stub_party(RID, f"later-{i}", f"Later {i}", None, "claim:queen-st-alarm", utcnow(),
                         next_action_at=now + 5000) for i in range(3)]
    db.parties.insert_many(stubs)
    for sid in PENDING[:3]:
        db.parties.update_one({"recall_id": RID, "party_id": sid}, {"$set": {"next_action_at": due_at[sid]}})
    s = replace(settings, lease_s=30)

    first = _pool(s, db, client, FiringHarness())
    first.startup()
    assert first.work_once("worker-test/w1") is None, "five timers pending, none due yet"
    del first                                         # killed: no shutdown, nothing written

    wait = clock.seconds_until(max(due_at.values()), simclock.server_now(client)) + 0.1
    time.sleep(wait)
    listener = DocumentsRead()
    spy = MongoClient(test_uri, tz_aware=True, event_listeners=[listener])
    try:
        harness = FiringHarness()
        second = _pool(s, spy[db.name], spy, harness)
        summary = second.startup()
        assert summary == {RID: {"kills": 0, "due": 5, "in_doubt": 0, "read": 5, "freed": 0}}
        read = {c: n for c, n in listener.by_collection.items() if c in LEDGER and n}
        assert read == {"parties": 5}, f"the restart read only the due parties, not {read}"
        line = db.feed.find_one({"kind": "worker_resumed", "detail.boot": second.boot_id})
        assert line["text"] == "restart 0: 5 due, 0 in doubt, 5 documents read, 0 steps replayed"

        while second.work_once("worker-test/w1") is not None:
            pass
    finally:
        spy.close()
    assert sorted(harness.fired) == sorted(PENDING), "each timer fired once"
    assert timers.due_count(db, RID, clock.sim_now(simclock.server_now(client))) == 0
    assert db.parties.count_documents({"recall_id": RID, "lease_epoch": 1}) == 5
