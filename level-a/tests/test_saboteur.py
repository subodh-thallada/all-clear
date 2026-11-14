"""Spec test 29 and saboteur mode (V2 §6).

29 loop  a subagent that never returns is stopped at its budget: the lease is
         released, a budget_exceeded episode is saved and the party is due again.
         Two ways never to return: calling tools forever (stopped by the tool
         budget, which Atlas counts) and never answering at all (stopped by the
         simulated-time budget).

The saboteur modes run through run_once(), the worker's entry, against the
same gate the real workers use.
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import levela.harness
from levela import clock, runs
from levela.api.main import create_app
from levela.harness import execute, saboteur
from levela.ledger.transfers import FREE_LEASE
from levela.orchestrator.worker import WorkerPool
from test_harness import (RID, W2, W5, InstantVoice, engine, feed_texts, lease, make_ctx, start,  # noqa: F401
                          world)

ADMIN = {"X-Admin-Token": "test-admin-token"}


def assert_due_again(db, ctx, party_id):
    party = db.parties.find_one({"_id": f"{RID}:{party_id}"})
    assert party["lease_until"] == FREE_LEASE
    assert party["next_action_at"] is not None and party["next_action_at"] <= ctx.sim_now()
    return party


# ---------------------------------------------------------------- test 29

def test_29_a_subagent_that_loops_is_stopped_at_its_tool_budget(db, world):
    party, fence = lease(db, "queen-st-alarm", W5)
    out = execute(world.ctx, party, worker_id=W5, fence=fence, decider=saboteur.SaboteurDecider("loop"),
                  saboteur="loop")

    assert out["status"] == "stopped" and out["failure_class"] == "budget_exceeded" and out["released"]
    run = db.task_runs.find_one({"_id": out["run_id"]})
    assert run["status"] == "stopped" and run["used"]["tool_calls"] == 6
    assert feed_texts(db, "gate_stopped") == [
        "STOPPED w5 (saboteur): budget_exceeded after 6 tool calls. Party queen-st-alarm is due again"]
    assert_due_again(db, world.ctx, "queen-st-alarm")
    episode = db.episodes.find_one({"_id": f"ep_{out['run_id']}"})
    assert episode["kind"] == "failure" and episode["failure_class"] == "budget_exceeded"
    assert episode["party_id"] == "queen-st-alarm"
    assert db.attempts.count_documents({}) == 0 and world.voice.count() == 0


class NeverReturns:
    """A decider stuck forever (until the test lets it go)."""
    name, is_model, continues_after_effect = "never-returns", False, False

    def __init__(self):
        self.release = threading.Event()

    def decide(self, brief):
        self.release.wait()
        raise RuntimeError("released by the test")


def test_29_a_decider_that_never_returns_is_stopped_at_its_time_budget(db, settings, client):
    """2 simulated hours at 0.5 s each: given up after one real second."""
    start(client, db, seconds_per_sim_hour=0.5)
    ctx = make_ctx(settings, client, db, InstantVoice(client, db))
    decider = NeverReturns()
    party, fence = lease(db, "harbour-fire", W5)
    try:
        out = execute(ctx, party, worker_id=W5, fence=fence, decider=decider)
    finally:
        decider.release.set()
    assert out["status"] == "stopped" and out["failure_class"] == "budget_exceeded" and out["released"]
    assert feed_texts(db, "gate_stopped") == [
        "STOPPED w5: budget_exceeded after 2 simulated hours. Party harbour-fire is due again"]
    assert_due_again(db, ctx, "harbour-fire")
    assert db.episodes.count_documents({"kind": "failure", "failure_class": "budget_exceeded"}) == 1
    decision = db.traces.find_one({"run_id": out["run_id"], "kind": "decision"})
    assert decision["output"] is None and decision["brief"]["text"].startswith("RULES")


# ---------------------------------------------------------- saboteur mode

def test_wrong_tool_is_refused_on_allowlist_and_the_party_goes_back_to_the_workers(db, world):
    out = saboteur.run_once(world.ctx, "wrong_tool", worker_id=W5)
    run = out["run"]
    assert run["status"] == "refused" and run["rule"] == "allowlist" and run["released"]
    assert any(t.startswith(f"REFUSED w5 (saboteur): close_recall on {out['party_id']}. Rule: allowlist.")
               for t in feed_texts(db, "gate_refused"))
    party = db.parties.find_one({"_id": f"{RID}:{out['party_id']}"})
    assert party["lease_until"] == FREE_LEASE and party["next_action_at"] is not None
    assert db.recalls.find_one({"_id": RID})["status"] == "open"


def test_double_send_places_one_call_and_the_second_returns_the_stored_outcome(db, world):
    out = saboteur.run_once(world.ctx, "double_send", worker_id=W5)
    run = out["run"]
    assert run["status"] == "done" and run["result"]["outcome"] == "claim_accepted"
    calls = [t for t in db.traces.find({"run_id": run["run_id"], "kind": "tool"}).sort("seq", 1)]
    assert [(t["tool"], t["decision"]["duplicate"]) for t in calls] == [("place_call", False), ("place_call", True)]
    assert db.attempts.count_documents({"party_id": out["party_id"]}) == 1
    assert world.voice.count(out["party_id"]) == 1


def test_false_success_is_refused_twice_by_atlas_and_by_the_facts(db, world):
    out = saboteur.run_once(world.ctx, "false_success", worker_id=W5)
    assert out["forged"] == {"rejected": True, "code": 121}
    assert feed_texts(db, "atlas_rejected") == [
        "REJECTED w5 (saboteur): operations.state = verified without readback_value"]
    assert db.feed.find_one({"kind": "atlas_rejected"})["detail"]["code"] == 121
    assert db.operations.count_documents({}) == 0
    assert out["run"]["failure_class"] == "unsupported_result"
    assert_due_again(db, world.ctx, out["party_id"])


def test_loop_mode_is_stopped_and_the_party_is_due_again(db, world):
    out = saboteur.run_once(world.ctx, "loop", worker_id=W5)
    assert out["run"]["status"] == "stopped" and out["run"]["failure_class"] == "budget_exceeded"
    assert_due_again(db, world.ctx, out["party_id"])


def test_stale_acts_with_an_old_epoch_and_its_release_writes_nothing(db, world):
    lease(db, "lakeside-systems", W2, epoch=4)
    before = db.parties.find_one({"_id": f"{RID}:lakeside-systems"})
    out = saboteur.run_once(world.ctx, "stale")

    assert out["decision"]["rule"] == "lease" and out["release_wrote"] is False
    assert feed_texts(db, "gate_refused") == [
        "REFUSED w2 (saboteur): place_call on lakeside-systems. Rule: lease. Holder is w2, epoch 4"]
    after = db.parties.find_one({"_id": f"{RID}:lakeside-systems"})
    assert {k: after[k] for k in ("lease_owner", "lease_epoch", "lease_until", "next_action_at")} == \
           {k: before[k] for k in ("lease_owner", "lease_epoch", "lease_until", "next_action_at")}
    assert db.attempts.count_documents({}) == 0


def test_modes_without_a_target_say_so_instead_of_inventing_one(db, world):
    db.parties.update_many({}, {"$set": {"next_action_at": None}})
    assert saboteur.run_once(world.ctx, "loop", worker_id=W5) == {"mode": "loop", "skipped": "nothing is due"}
    assert saboteur.run_once(world.ctx, "stale")["skipped"] == "no party has been leased yet"
    with pytest.raises(ValueError):
        saboteur.run_once(world.ctx, "arson")


# ------------------------------------------------- saboteur mode in the live run

def _pool(settings, db, client, voice=None, **kw):
    providers = {"sim": voice, "voice": voice} if voice is not None else {}
    return WorkerPool(settings, db, client, providers=providers, probes={}, harness=levela.harness, **kw)


def _switch(client, settings, **body):
    """POST /admin/saboteur, the wall's button."""
    api = TestClient(create_app(settings, client=client, sim_network=object(),
                                http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))))
    r = api.post("/admin/saboteur", headers=ADMIN, json={"recall_id": RID, **body})
    assert r.status_code == 200, r.text


def test_the_pool_runs_the_saboteur_while_the_switch_is_on_and_every_refusal_is_on_the_feed(
        db, world, settings, client):
    """spec-saboteur-not-wired: POST /admin/saboteur only set a flag and
    nothing ran the saboteur. Now each pass while the switch is on is one
    misdeed, in mode order, as w5 (the demo pool has four threads), and the
    gate's refusal is on the feed."""
    lease(db, "lakeside-systems", W2, epoch=4)
    pool = _pool(settings, db, client, world.voice, workers=4)
    assert pool.sabotage_once() == [], "the switch is off"

    _switch(client, settings, on=True, seconds=30)
    first = pool.sabotage_once()
    assert [(o["recall_id"], o["mode"], o["party_id"]) for o in first] == [(RID, "wrong_party", "lakeside-systems")]
    assert feed_texts(db, "gate_refused") == [
        "REFUSED w5 (saboteur): place_call on lakeside-systems. Rule: lease. Holder is w2, epoch 4"]
    [second] = pool.sabotage_once()
    assert second["mode"] == "wrong_tool" and second["run"]["rule"] == "allowlist"
    assert feed_texts(db, "gate_refused")[1].startswith(
        f"REFUSED w5 (saboteur): close_recall on {second['party_id']}. Rule: allowlist.")
    assert db.task_runs.count_documents({"worker": "worker-test/w5", "saboteur": {"$ne": None}}) == 2

    _switch(client, settings, on=False)
    assert pool.sabotage_once() == []
    _switch(client, settings, on=True, seconds=30)
    db.runs.update_one({"_id": runs.run_id(RID)},
                       {"$set": {"saboteur.until": clock.server_now(client) - timedelta(seconds=1)}})
    assert pool.sabotage_once() == [], "until is behind the database's clock"
    assert len(feed_texts(db, "gate_refused")) == 2


class RecordingSaboteur:
    """harness.saboteur's interface; run_once raises in mode `fails`."""
    MODES = saboteur.MODES

    def __init__(self, fails: str | None = None):
        self.calls, self.fails = [], fails

    def run_once(self, ctx, mode, *, worker_id, recall_id):
        self.calls.append((time.monotonic(), mode, worker_id, recall_id, ctx.recall["_id"]))
        if mode == self.fails:
            raise RuntimeError(f"{mode} tripped")
        return {"mode": mode, "skipped": "recorded"}


def test_the_saboteur_thread_cycles_its_modes_about_once_a_second_until_the_switch_lapses(
        db, world, settings, client):
    db.parties.update_many({}, {"$set": {"next_action_at": None}})       # the real threads stay idle
    recorder = RecordingSaboteur(fails="wrong_tool")
    pool = _pool(settings, db, client, saboteur=recorder, workers=5)
    assert pool.saboteur_id == "worker-test/w6", "never the id of one of the five real threads"
    pool.startup()                       # before the switch: the window is the saboteur's, not the boot's
    _switch(client, settings, on=True, seconds=4)
    stop = threading.Event()
    thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        quiet_since, seen = time.monotonic(), 0
        while not seen or time.monotonic() - quiet_since < 2.5:     # it ran, then lapsed: quiet for 2.5 s
            assert time.monotonic() < deadline, (f"no start, or no stop: {len(recorder.calls)} calls; feed: "
                                                 f"{[r['text'] for r in db.feed.find({'level': 'bad'})]}")
            if len(recorder.calls) != seen:
                quiet_since, seen = time.monotonic(), len(recorder.calls)
            time.sleep(0.1)
    finally:
        stop.set()
        thread.join(30)
    assert not thread.is_alive()

    modes = [mode for _, mode, _, _, _ in recorder.calls]
    assert 2 <= len(modes) <= 6 and modes == list(saboteur.MODES[:len(modes)]), modes
    assert {(w, r, c) for _, _, w, r, c in recorder.calls} == {("worker-test/w6", RID, RID)}
    gaps = [b[0] - a[0] for a, b in zip(recorder.calls, recorder.calls[1:])]
    assert all(g >= 0.9 for g in gaps), gaps
    [error] = list(db.feed.find({"kind": "error"}))
    assert error["text"] == "worker-test/w6: saboteur wrong_tool failed (RuntimeError: wrong_tool tripped); retrying"
