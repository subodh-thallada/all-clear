"""The worker pool and the planner on the v2 engine, with the harness stubbed.

StubHarness stands in for levela.harness (V2 section 6) and makes exactly the
calls into the engine the real one makes: it decides from facts (a party
whose call was confirmed is not called again), places a call the gate's way
with a failpoint after each step, and ends every run with timers.release
under the fence it was given. FakeVoice is the phone network: it outlives any
pool, so "one call per party" is asserted on what really rang.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from levela import clock as simclock
from levela import runs
from levela.clock import EPOCH
from levela.config import CALL_FAILPOINTS
from levela.db.seed import load_scenario, start_recall
from levela.ledger.transfers import FREE_LEASE, stub_party
from levela.orchestrator import planner as planner_mod
from levela.orchestrator import timers
from levela.orchestrator import worker as worker_mod
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.orchestrator.planner import Planner
from levela.orchestrator.failpoints import Failpoints, WorkerCrash, arm, raise_crash
from levela.orchestrator.worker import WorkerPool
from levela.providers import FakeVoice

R1, R2 = "RCL-2026-017", "RCL-2026-018"
W1 = "worker-test/w1"


class StubHarness:
    """harness.execute(ctx, party, *, worker_id, fence, decider), reduced to
    what the engine relies on."""

    def __init__(self, voice=None, *, dial=True, delay_s=0.0, fail=None):
        self.voice, self.dial, self.delay_s, self.fail = voice, dial, delay_s, dict(fail or {})
        self.runs: list[tuple[str, str, int]] = []
        self.finished: list[str] = []
        self.lock = threading.Lock()

    @staticmethod
    def HarnessContext(**fields):  # noqa: N802 - mirrors the harness's class name
        return SimpleNamespace(**fields)

    def execute(self, ctx, party, *, worker_id, fence, decider):
        with self.lock:
            self.runs.append((worker_id, party["party_id"], fence["lease_epoch"]))
        if party["party_id"] in self.fail:
            raise self.fail[party["party_id"]]
        if self.dial and not ctx.db.attempts.find_one({"party_id": party["party_id"], "dial_confirmed": True}):
            self._place(ctx, party, worker_id)
        time.sleep(self.delay_s)
        timers.release(ctx.client, ctx.db, party, fence, next_action_at=None, ladder_step=party["ladder_step"],
                       last_outcome={"outcome": "claim_accepted", "at": ctx.clock.sim_now()})
        with self.lock:
            self.finished.append(party["party_id"])
        return {"outcome": "claim_accepted"}

    def _place(self, ctx, party, worker_id):
        att = open_attempt(ctx.db, recall_id=party["recall_id"], party_id=party["party_id"],
                           channel=party["channel"], owner={"worker": worker_id}, to_number=party["phone"],
                           context={"party_id": party["party_id"]})
        ctx.failpoints.hit("after_intent", att)
        placed = self.voice.place_call(to_number=party["phone"], dynamic_variables={"party_id": party["party_id"]},
                                       attempt_id=att["_id"])
        ctx.failpoints.hit("after_dial_request", att)
        mark_dialed(ctx.db, att["_id"], conversation_id=placed.conversation_id, call_sid=placed.call_sid)
        ctx.failpoints.hit("after_mark_dialed", att)


class StubMemory:
    def __init__(self, actions=None):
        self.actions, self.checked, self.episodes = actions or {}, [], []

    def check_party(self, recall, party):
        self.checked.append(party["party_id"])
        return {"mode": "local", "hits": [{"episode_id": "ep_old", "party_id": party["party_id"], "tags": []}],
                "actions": self.actions.get(party["party_id"], [])}

    def write_episode(self, attempt, now=None):
        self.episodes.append(attempt["_id"])
        return {"_id": f"ep_{attempt['_id']}"}


def open_stub_run(db, **fields):
    """harness.budget.open_run, reduced: party the run, return an object with .id and .doc."""
    doc = {"_id": f"tr-{time.monotonic_ns()}", "status": "running", **fields}
    db.task_runs.insert_one(doc)
    return SimpleNamespace(id=doc["_id"], doc=doc, recall_id=doc["recall_id"])


class StubGate:
    """gate.call for the planner: records each call and applies it."""

    def __init__(self, db):
        self.db, self.calls = db, []

    def call(self, ctx, run, tool, args, *, sim_now=None):
        self.calls.append((tool, dict(args), dict(run.doc)))
        rid = run.recall_id
        if tool == "set_priority":
            self.db.parties.update_one({"recall_id": rid, "party_id": args["party_id"]},
                                      {"$set": {"priority": args["priority"]}})
        elif tool == "escalate":
            self.db.parties.update_one({"recall_id": rid, "party_id": args["party_id"]}, {"$set": {"ladder_step": 5}})
        return SimpleNamespace(allowed=True, rule=None)


@pytest.fixture
def start(client, db):
    def go(scenario="recall_1", **options):
        return start_recall(client, db, load_scenario(scenario), all_sim=True, env={}, emit=False,
                            **options)["recall_id"]
    return go


@pytest.fixture
def voice():
    return FakeVoice()


@pytest.fixture
def make(client, db, settings, voice):
    def build(harness=None, *, workers=1, settings_=None, **kw):
        s = settings_ or replace(settings, lease_s=30)
        return WorkerPool(s, db, client, providers={"sim": voice, "voice": voice},
                          probes={"sim": voice.probe, "voice": voice.probe},
                          failpoints=Failpoints(db, crash=raise_crash, env={}, worker=s.worker_name),
                          harness=harness or StubHarness(voice), workers=workers, **kw)
    return build


def fast(db, rid, seconds_per_sim_hour=0.05):
    clock = simclock.get_clock(db, rid, max_age_s=0)
    runs.set_clock(db, rid, clock.with_speed(seconds_per_sim_hour, simclock.server_now(db.client)))


def sim_now(db, rid):
    return simclock.get_clock(db, rid, max_age_s=0).sim_now(simclock.server_now(db.client))


def party(db, rid, sid):
    return db.parties.find_one({"recall_id": rid, "party_id": sid})


def only_due(db, rid, *sids):
    db.parties.update_many({"recall_id": rid, "party_id": {"$nin": list(sids)}}, {"$set": {"next_action_at": None}})


def drain(pool, worker_id=W1, *, until, timeout=15.0):
    """work_once until `until()` holds (sleeping like an idle thread)."""
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "the pool did not get there in time"
        if pool.work_once(worker_id) is None:
            time.sleep(0.05)


def feed(db, kind):
    return list(db.feed.find({"kind": kind}).sort([("at", 1), ("_id", 1)]))


# -- claim, execute, release ----------------------------------------------------------

def test_each_due_party_runs_once_under_a_fresh_lease(db, start, make):
    rid = start()
    harness = StubHarness(dial=False)
    pool = make(harness)
    pool.startup()
    assert [pool.work_once(W1) for _ in range(3)] == ["executed"] * 3
    assert pool.work_once(W1) is None
    assert sorted(sid for _, sid, _ in harness.runs) == ["harbour-market", "lakeside-foods", "queen-st-grocer"]
    assert {(w, epoch) for w, _, epoch in harness.runs} == {(W1, 1)}
    for sid in ("harbour-market", "lakeside-foods", "queen-st-grocer"):
        row = party(db, rid, sid)
        assert (row["next_action_at"], row["lease_until"], row["lease_owner"]) == (None, FREE_LEASE, None)
        assert row["last_outcome"]["outcome"] == "claim_accepted"


def test_startup_reports_what_is_due_and_in_doubt(db, start, make):
    rid = start()
    runs.inc(db, rid, "kills", 3)
    open_attempt(db, recall_id=rid, party_id="harbour-market", channel="sim", to_number="+14165550141")
    db.operations.insert_one({"_id": "op-1", "recall_id": rid, "kind": "place_hold", "party_id": "dist-north",
                              "idempotency_key": f"{rid}:dist-north:place_hold", "state": "sent"})
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"}, {"$set": {"next_action_at": 1e6}})
    pool = make()
    assert pool.startup() == {rid: {"kills": 3, "due": 2, "in_doubt": 2, "read": 4, "freed": 0}}
    [line] = feed(db, "worker_resumed")
    assert line["text"] == "restart 3: 2 due, 2 in doubt, 4 documents read, 0 steps replayed"
    assert (line["system"], line["recall_id"], line["detail"]["steps_replayed"]) == ("worker", rid, 0)
    state = db.worker_state.find_one({"_id": "worker:worker-test"})
    assert state["boot_id"] == pool.boot_id and state["workers"] == [W1]


# -- spec test 7 on the v2 engine: a kill at each failpoint ----------------------------

@pytest.mark.parametrize("failpoint", CALL_FAILPOINTS)
def test_a_failpoint_kills_the_pool_and_the_next_boot_never_calls_twice(db, start, make, voice, failpoint):
    rid = start()
    fast(db, rid)
    only_due(db, rid, "harbour-market")
    arm(db, failpoint, party_id="harbour-market")
    first = make()
    with pytest.raises(WorkerCrash):
        first.run()
    assert voice.count("harbour-market") == (0 if failpoint == "after_intent" else 1)
    assert feed(db, "failpoint_fired")

    harness = StubHarness(voice)
    second = make(harness)
    assert second.startup()[rid]["freed"] == 1, "the dead boot's lease is freed at once"
    if failpoint == "after_mark_dialed":
        assert second.work_once(W1) == "recheck", "the saved call is still in flight: look again, never redial"
        [att] = db.attempts.find({"party_id": "harbour-market"})
        complete(db, att["_id"], "completed")               # the post-call webhook arrives
    drain(second, until=lambda: harness.finished == ["harbour-market"])
    assert voice.count("harbour-market") == 1
    states = [a["state"] for a in db.attempts.find({"party_id": "harbour-market"}).sort("seq", 1)]
    expected = {"after_intent": ["abandoned", "dialed"], "after_dial_request": ["completed"],
                "after_mark_dialed": ["completed"]}[failpoint]
    assert states == expected
    if failpoint == "after_dial_request":
        assert "No redial." in feed(db, "attempt_adopted")[0]["text"]


def test_an_in_doubt_attempt_inside_its_grace_period_is_left_to_the_provider(db, start, make, settings):
    rid = start()
    only_due(db, rid, "lakeside-foods")
    open_attempt(db, recall_id=rid, party_id="lakeside-foods", channel="sim", to_number="+14165550143")
    harness = StubHarness(dial=False)
    pool = make(harness, settings_=replace(settings, indoubt_grace_s=30, lease_s=30))
    before = sim_now(db, rid)
    assert pool.work_once(W1) == "recheck"
    row = party(db, rid, "lakeside-foods")
    assert harness.runs == [] and row["lease_until"] == FREE_LEASE
    assert before + 0.9 <= row["next_action_at"] <= before + 1.1, "30 real seconds at 30 s per sim hour"
    assert db.attempts.find_one({"party_id": "lakeside-foods"})["state"] == "intent"


def test_a_run_that_raises_is_reported_and_its_party_tried_again_later(db, start, make):
    rid = start()
    only_due(db, rid, "lakeside-foods")
    pool = make(StubHarness(dial=False, fail={"lakeside-foods": RuntimeError("decider exploded")}))
    before = sim_now(db, rid)
    assert pool.work_once(W1) == "error"
    row = party(db, rid, "lakeside-foods")
    assert row["last_outcome"]["outcome"] == "failed" and row["next_action_at"] >= before + 1.0
    [line] = feed(db, "error")
    assert "decider exploded" in line["text"] and line["level"] == "bad" and line["system"] == "worker"


def _op(rid, party_id, kind, *, order_system, state="sent", key=None, created_at=None):
    """An operations document shaped like harness.tools.operation_doc."""
    now = created_at or datetime.now(timezone.utc)
    return {"_id": f"op-{kind}-{party_id}", "recall_id": rid, "run_id": "tr-earlier", "kind": kind, "order_system": order_system,
            "party_id": party_id, "idempotency_key": key or f"{rid}:{party_id}:{kind}", "adapter_version": None,
            "state": state, "lease_epoch": 1, "sent_at": 33.0, "readback_at": None, "readback_value": None,
            "request": {}, "response": None, "error": None, "created_at": now}


def test_a_sent_form_link_or_rep_escalation_is_not_in_doubt_at_claim_or_restart(db, start, make):
    """systems-1 / crash-1: a form link (order system None) and a rep escalation
    (its distributor's order system) rest in `sent` until someone answers them.
    Claim-time recovery used to hand them to the order system code, which raised
    outside any try: the party was never released and failed forever."""
    rid = start()
    only_due(db, rid, "lakeside-foods")
    db.operations.insert_many([
        _op(rid, "lakeside-foods", "send_form_link", order_system=None, key=f"{rid}:lakeside-foods:form:3"),
        _op(rid, "lakeside-foods", "escalate_rep", order_system="north")])
    harness = StubHarness(dial=False)
    pool = make(harness)
    assert pool.startup()[rid]["in_doubt"] == 0, "the resume line counts only order system calls"
    assert pool.work_once(W1) == "executed"
    assert harness.runs == [(W1, "lakeside-foods", 1)]
    assert {op["state"] for op in db.operations.find({})} == {"sent"}, "neither is touched"
    assert db.adapters.count_documents({}) == 0, "no adapter is seeded for order system None (or any order system)"
    assert feed(db, "error") == []


def test_a_recovery_that_raises_is_reported_and_releases_the_party_with_a_retry_time(db, start, make):
    """crash-1: an order system call whose order system has no distributor in this recall
    makes recovery raise. The party is released with a retry time and the
    error is on the wall, instead of the lease being held until it lapses."""
    rid = start()
    only_due(db, rid, "lakeside-foods")
    db.operations.insert_one(_op(rid, "lakeside-foods", "read_inventory", order_system="north", state="intent"))
    harness = StubHarness(dial=False)
    pool = make(harness)
    before = sim_now(db, rid)
    assert pool.work_once(W1) == "error"
    row = party(db, rid, "lakeside-foods")
    assert (row["lease_until"], row["lease_owner"]) == (FREE_LEASE, None)
    assert row["next_action_at"] >= before + worker_mod.ERROR_RETRY_H and row["ladder_step"] == 1
    assert harness.runs == [], "nothing new starts on a party whose recovery failed"
    [line] = feed(db, "error")
    assert line["text"].startswith(f"{W1}: recovery on Lakeside Foods failed (LookupError: recall {rid} has no "
                                   "distributor on North orders); trying again at")
    assert db.adapters.count_documents({}) == 0, "the missing distributor is found before any adapter is seeded"


# -- a call that outlived its run (crash-2) ----------------------------------------------

def _late_call(db, rid, sid, *, run_status="running", state="no_answer", callback_at=None):
    """A contact run that dialled `sid` and ended (a kill, a timeout, a lost
    lease) before its call did; the call has ended since."""
    run_id = f"tr-late-{sid}"
    db.task_runs.insert_one({"_id": run_id, "status": run_status, "recall_id": rid, "party_id": sid,
                             "kind": "contact_party", "worker": "worker-test/w2", "lease_epoch": 1})
    att = open_attempt(db, recall_id=rid, party_id=sid, channel="sim", to_number="+14165550143",
                       extra={"run_id": run_id, "owner": {"worker": "worker-test/w2", "epoch": 1}})
    mark_dialed(db, att["_id"], call_sid="CAlate")
    if callback_at is not None:
        db.attempts.update_one({"_id": att["_id"]}, {"$set": {"callback_at": callback_at}})
    complete(db, att["_id"], state)
    return run_id, att


def test_a_call_that_outlived_its_run_moves_the_ladder_once_and_is_not_redialled(db, start, make, voice):
    rid = start()
    only_due(db, rid, "lakeside-foods")
    before = sim_now(db, rid)
    run_id, att = _late_call(db, rid, "lakeside-foods")
    harness = StubHarness(voice)
    pool = make(harness)
    assert pool.work_once(W1) == "ladder"
    assert harness.runs == [] and voice.count("lakeside-foods") == 0, "no new contact in the claim that applied it"
    row = party(db, rid, "lakeside-foods")
    assert row["ladder_step"] == 2, "no answer on step 1: step 2"
    assert before + 3.9 <= row["next_action_at"] <= sim_now(db, rid) + 4.0, "4 simulated hours after the call"
    assert (row["last_outcome"]["outcome"], row["last_outcome"]["late"], row["lease_until"]) == (
        "no_answer", True, FREE_LEASE)
    applied = db.task_runs.find_one({"_id": run_id})["ladder_applied"]
    assert (applied["attempt_id"], applied["outcome"], applied["ladder_step"], applied["by"]) == (
        att["_id"], "no_answer", 2, W1)
    [line] = feed(db, "run_ended")
    assert line["text"].startswith("Lakeside Foods: attempt 1 ended after its run did (no answer). Ladder step 2")

    # When the timer comes due, the party is worked at step 2: the call is applied once only.
    db.parties.update_one({"_id": row["_id"]}, {"$set": {"next_action_at": sim_now(db, rid)}})
    assert pool.work_once(W1) == "executed"
    assert [sid for _, sid, _ in harness.runs] == ["lakeside-foods"]
    assert party(db, rid, "lakeside-foods")["ladder_step"] == 2


def test_a_callback_asked_for_on_a_call_that_outlived_its_run_keeps_its_hour(db, start, make, voice):
    rid = start()
    only_due(db, rid, "lakeside-foods")
    hour = sim_now(db, rid) + 20.0
    run_id, _ = _late_call(db, rid, "lakeside-foods", run_status="stopped", state="completed", callback_at=hour)
    harness = StubHarness(voice)
    assert make(harness).work_once(W1) == "ladder"
    row = party(db, rid, "lakeside-foods")
    assert (row["ladder_step"], row["next_action_at"]) == (1, hour), "same step, the party's own hour"
    assert row["last_outcome"]["outcome"] == "callback_requested" and harness.runs == []


def test_a_late_call_whose_timer_has_passed_is_due_at_once_on_the_next_step(db, start, make, voice):
    rid = start()
    clock = simclock.get_clock(db, rid, max_age_s=0)
    began = simclock.server_now(db.client) - timedelta(seconds=clock.real_seconds(5.0))
    runs.set_clock(db, rid, replace(clock, real_start=began))          # the run is five simulated hours old
    only_due(db, rid, "lakeside-foods")
    _, att = _late_call(db, rid, "lakeside-foods", run_status="refused")
    ended = began + timedelta(seconds=clock.real_seconds(0.5))         # the call ended half an hour in
    db.attempts.update_one({"_id": att["_id"]}, {"$set": {"ended_at": ended}})
    harness = StubHarness(voice)
    pool = make(harness)
    assert pool.work_once(W1) == "ladder"
    assert party(db, rid, "lakeside-foods")["ladder_step"] == 2
    assert pool.work_once(W1) == "executed", "due at once, and the late call is not applied twice"
    assert [sid for _, sid, _ in harness.runs] == ["lakeside-foods"]
    assert party(db, rid, "lakeside-foods")["ladder_step"] == 2


def test_a_party_of_a_recall_that_closed_meanwhile_gets_no_timer(db, start, make):
    rid = start()
    pool = make(StubHarness(dial=False))
    assert pool._open_recalls() == [rid]
    db.recalls.update_one({"_id": rid}, {"$set": {"status": "closed", "open_units": 0}})
    assert pool.work_once(W1) == "closed"
    assert db.parties.count_documents({"recall_id": rid, "next_action_at": None}) == 1


# -- memory before first contact (recall 2's ordering) -----------------------------------

def test_a_memory_suspect_is_contacted_before_the_party_that_named_it(db, start, make, monkeypatch):
    rid = start("recall_2", memory=True)
    db.parties.update_one({"recall_id": rid, "party_id": "queen-st-grocer"}, {"$set": {"priority": 90}})
    memory = StubMemory({
        "queen-st-grocer": [{"action": "suspect_party", "name": "corner market", "episode_id": "ep_att_qsg",
                             "from_party": "queen-st-grocer"}],
        "harbour-market": [{"action": "ask_delivery_note", "episode_id": "ep_att_hm"}],
    })
    monkeypatch.setattr(worker_mod, "SUSPECT_HEAD_START_S", 0.5)
    harness = StubHarness(dial=False)
    pool = make(harness, memory=memory)
    assert pool.work_once(W1) == "suspects"
    cm, qsg = party(db, rid, "corner-mart"), party(db, rid, "queen-st-grocer")
    assert (cm["name"], cm["found_via"], cm["discovered_by"], cm["priority"]) == ("Corner Mart", "memory", "memory", 91)
    assert cm["memory_source"] == {"episode_id": "ep_att_qsg", "from_party": "queen-st-grocer"}
    assert qsg["memory"]["actions"][0]["action"] == "suspect_party" and qsg["ladder_step"] == 1
    assert qsg["next_action_at"] > cm["next_action_at"], "the suspect goes first"
    assert db.runs.find_one({"_id": runs.run_id(rid)})["planner_wake"] is True

    drain(pool, until=lambda: len(harness.finished) == 4)
    order = harness.finished
    assert order.index("corner-mart") < order.index("queen-st-grocer")
    assert sorted(memory.checked) == ["corner-mart", "harbour-market", "lakeside-foods", "queen-st-grocer"]
    assert party(db, rid, "harbour-market")["memory"]["actions"] == [
        {"action": "ask_delivery_note", "episode_id": "ep_att_hm"}]
    [suspect] = feed(db, "memory_suspect")
    assert suspect["text"] == ("Memory: Queen St Grocer forwarded to Corner Mart last time - contacting "
                               "Corner Mart now") and suspect["system"] == "memory"
    assert any("delivery note" in line["text"] for line in feed(db, "memory_note"))


def test_memory_off_means_no_retrieval(db, start, make):
    start("recall_2", memory=False)
    memory = StubMemory()
    pool = make(StubHarness(dial=False), memory=memory)
    assert [pool.work_once(W1) for _ in range(4)] == ["executed"] * 3 + [None]
    assert memory.checked == []


# -- the threads -------------------------------------------------------------------------

def test_the_pool_works_in_parallel_and_stops_cleanly(db, start, make):
    start()
    harness = StubHarness(dial=False, delay_s=0.6)
    ran = threading.Event()
    pool = make(harness, workers=3, planner=SimpleNamespace(run=lambda stop: ran.set()))
    stop = threading.Event()
    thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while len(harness.finished) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len({w for w, _, _ in harness.runs}) == 3, "three threads, three parties"
    stop.set()
    thread.join(20)
    assert not thread.is_alive() and ran.is_set()
    assert feed(db, "worker_stopped")[0]["text"] == "Worker worker-test stopped (stop requested)"
    assert db.worker_state.find_one({"_id": "worker:worker-test"})["stopped_at"] is not None


def test_a_newer_boot_with_the_same_name_supersedes_the_old_one(db, make):
    old, new = make(), make()
    old.startup()
    new.startup()
    assert old.maintain_once() == {"superseded": True} and old.superseded
    assert "was replaced by a newer boot" in feed(db, "worker_heartbeat_lost")[0]["text"]
    assert new.maintain_once() == {"orphans": 0, "stale": [], "episodes": 0}


def test_maintenance_closes_stale_calls_and_writes_each_episode_once(db, start, make, voice, settings):
    rid = start()
    memory = StubMemory()
    pool = make(memory=memory, settings_=replace(settings, stale_call_s=0.0))
    pool.startup()
    live = open_attempt(db, recall_id=rid, party_id="lakeside-foods", channel="sim", to_number="+14165550143")
    placed = voice.place_call(to_number="+14165550143", dynamic_variables={}, attempt_id=live["_id"])
    mark_dialed(db, live["_id"], call_sid=placed.call_sid)
    ended = open_attempt(db, recall_id=rid, party_id="harbour-market", channel="sim", to_number="+14165550141")
    mark_dialed(db, ended["_id"], call_sid="CAother")
    complete(db, ended["_id"], "no_answer")
    out = pool.maintain_once()
    assert [(s["attempt_id"], s["state"]) for s in out["stale"]] == [(live["_id"], "completed")]
    assert out["episodes"] == 2 and sorted(memory.episodes) == sorted([live["_id"], ended["_id"]])
    assert pool.maintain_once()["episodes"] == 0


# -- the planner ---------------------------------------------------------------------------

def _planner(db, client, settings, gate, planner_id="worker-test/planner", repairs=None):
    return Planner(settings, db, client, planner_id=planner_id, context=lambda recall, clock: SimpleNamespace(recall=recall),
                gate=gate, open_run=open_stub_run,
                repair=lambda ctx, order_system: repairs.append(order_system) if repairs is not None else None)


def test_the_planner_ranks_by_unresolved_disputes_and_questions_through_the_gate(client, db, settings, start):
    rid = start()
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"}, {"$set": {"open_questions": ["q-01"]}})
    db.edges.insert_one({"_id": f"{rid}:queen-st-grocer>corner-mart", "recall_id": rid, "from_party": "queen-st-grocer",
                         "to_party": "corner-mart", "sender_qty": 40, "recipient_qty": 25, "corroborated_qty": 25,
                         "disputed_qty": 15, "state": "disputed"})
    gate = StubGate(db)
    planner = _planner(db, client, settings, gate)
    [summary] = planner.tick()
    assert {(a["party_id"], a["priority"]) for t, a, _ in gate.calls if t == "set_priority"} == {
        ("harbour-market", 40), ("queen-st-grocer", 150), ("lakeside-foods", 130)}
    run = gate.calls[0][2]
    assert (run["kind"], run["party_id"], run["worker"], run["lease_epoch"], run["budget"]) == (
        "planner", None, "worker-test/planner", 1, {"tool_calls": 3})
    assert run["lease"] == {"coll": "worker_state", "_id": "planner"}, "the planner lease fences the run"
    assert summary["allowed"] == 3 and db.task_runs.find_one({"_id": run["_id"]})["status"] == "done"
    assert feed(db, "planner")[0]["text"].startswith("planner: 3 priorities set. Oldest due d0 09:00, 2 of 2 call slots")
    after = db.runs.find_one({"_id": runs.run_id(rid)})
    assert after["planner_wake"] is False and after["planner_last_at"] >= 9.0
    assert planner.tick() == [], "not woken, and 6 simulated hours have not passed"
    runs.wake_planner(db, rid, "party created")
    [again] = planner.tick()
    assert again["decisions"] == [] and db.task_runs.count_documents({"kind": "planner"}) == 1


def test_one_planner_at_a_time(client, db, settings):
    a = _planner(db, client, settings, None, "a/planner")
    b = _planner(db, client, settings, None, "b/planner")
    assert a.acquire() == 1 and b.acquire() is None and a.acquire() == 1
    db.worker_state.update_one({"_id": planner_mod.PLANNER_DOC}, [{"$set": {"lease_until": {"$add": ["$$NOW", -1]}}}])
    assert b.acquire() == 2 and a.acquire() is None


def test_the_planner_escalates_stuck_parties_and_asks_for_order_system_repairs(client, db, settings, start):
    rid = start()
    now = sim_now(db, rid)
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"},
                         {"$set": {"ladder_step": 4, "next_action_at": now - planner_mod.STUCK_AFTER_H - 1}})
    db.parties.update_one({"recall_id": rid, "party_id": "harbour-market"},
                         {"$set": {"ladder_step": 4, "next_action_at": now - 1}})
    db.parties.insert_many([{**stub_party(rid, f"dist-{p}", f"Distributor {p}", None, "shipment", None),
                            "channel": "order_system", "order_system": p, "order_system_paused": True,
                            "order_system_failure": "field qty_in_stock missing"}
                           for p in ("north", "east")])
    db.adapters.insert_one({"_id": "east:v2", "order_system": "east", "version": 2, "status": "candidate"})
    db.parties.update_one({"recall_id": rid, "party_id": "harbour-market"},          # being worked on right now
                         [{"$set": {"lease_owner": "w1", "lease_until": {"$add": ["$$NOW", 60_000]}}}])
    gate, repairs = StubGate(db), []
    summary = _planner(db, client, settings, gate, repairs=repairs).pass_once(rid)
    assert [a["party_id"] for t, a, _ in gate.calls if t == "escalate"] == ["lakeside-foods"]
    assert sorted(a["party_id"] for t, a, _ in gate.calls if t == "set_priority") == [
        "lakeside-foods", "queen-st-grocer"], "a leased party's priority waits for the next pass"
    assert summary["repairs"] == ["north"] and repairs == ["north"]
    assert party(db, rid, "lakeside-foods")["ladder_step"] == 5


def _distributors(db, rid, paused: dict):
    db.parties.insert_many([{**stub_party(rid, f"dist-{p}", f"Distributor {p}", None, "shipment", None),
                            "channel": "order_system", "order_system": p, "order_system_paused": is_paused}
                           for p, is_paused in paused.items()])


def test_a_candidate_left_by_a_dead_repair_does_not_block_the_order_systems_repair(client, db, settings, start):
    """systems-4 (b): a kill between save_candidate and reject/activate left
    a candidate forever, and the planner skipped that paused order system for good."""
    rid = start()
    _distributors(db, rid, {"north": True, "east": True})
    now = datetime.now(timezone.utc)
    db.adapters.insert_many([
        {"_id": "north:v2", "order_system": "north", "version": 2, "status": "candidate",
         "created_at": now - timedelta(seconds=planner_mod.CANDIDATE_TTL_S + 60)},
        {"_id": "east:v2", "order_system": "east", "version": 2, "status": "candidate", "created_at": now}])
    repairs = []
    summary = _planner(db, client, settings, StubGate(db), repairs=repairs).pass_once(rid)
    assert summary["repairs"] == ["north"] and repairs == ["north"], "east's repair is still in progress"


def test_the_planner_resumes_a_repair_interrupted_after_activation(client, db, settings, start):
    """systems-4 (a): a repair killed after activate() leaves the order system
    unpaused under a canary-passed adapter with operations still flagged.
    The planner asks for that order system again; an unverified adapter's flagged ops
    (east) wait for a real repair instead."""
    from levela.systems.repair import LOCK_TTL_S

    assert planner_mod.CANDIDATE_TTL_S == LOCK_TTL_S
    rid = start()
    _distributors(db, rid, {"north": False, "east": False})
    db.adapters.insert_many([
        {"_id": "north:v2", "order_system": "north", "version": 2, "status": "active", "canary": {"passed": True}},
        {"_id": "east:v1", "order_system": "east", "version": 1, "status": "active", "canary": None}])
    db.operations.insert_many([
        _op(rid, "acct-1", "read_inventory", order_system="north", state="needs_reverify"),
        _op(rid, "acct-2", "read_inventory", order_system="east", state="needs_reverify"),
        _op(rid, "acct-3", "escalate_rep", order_system="north", state="sent")])
    snap = planner_mod.snapshot(db, rid, sim_now(db, rid))
    assert snap["reverify_order_systems"] == ["north"]
    repairs = []
    summary = _planner(db, client, settings, StubGate(db), repairs=repairs).pass_once(rid)
    assert summary["repairs"] == ["north"] and repairs == ["north"]
    assert "repair requested for North orders" in feed(db, "planner")[0]["text"]


def test_startup_frees_the_dead_boots_repair_locks_and_wakes_the_planner_for_flagged_ops(db, start, make):
    rid = start()
    held = (datetime.now(timezone.utc) + timedelta(seconds=300)).replace(microsecond=0)
    db.worker_state.insert_many([{"_id": "repair:north", "owner": "worker-test/0a1b2c", "until": held},
                                 {"_id": "repair:east", "owner": "worker-other/3d4e5f", "until": held}])
    db.operations.insert_one(_op(rid, "acct-1", "read_inventory", order_system="north", state="needs_reverify"))
    db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"planner_wake": False}})
    make().startup()
    assert db.worker_state.find_one({"_id": "repair:north"})["until"] == EPOCH, "this name's dead boot held it"
    assert db.worker_state.find_one({"_id": "repair:east"})["until"] == held, "another worker's lock stands"
    assert db.runs.find_one({"_id": runs.run_id(rid)})["planner_wake"] is True


def test_restart_adopts_an_in_doubt_call_whose_party_reconciled_while_the_worker_was_down(db, start, make, voice):
    """The kill beat on the long run: the worker dies after dialling Corner
    Mart, the claim lands and the party reconciles while it is down, so the
    party is never due again. The restart still adopts the attempt from the
    tool event (no redial) instead of leaving it in doubt."""
    rid = start()
    att = open_attempt(db, recall_id=rid, party_id="lakeside-foods", channel="sim", to_number="+14165550143",
                       extra={"owner": {"worker": "worker-test/w1", "epoch": 1}})
    db.attempts.update_one({"_id": att["_id"]}, {"$set": {"intent_at": simclock.server_now(db.client)
                                                          - timedelta(seconds=60)}})
    db.tool_events.insert_one({"kind": "tool_call", "attempt_id": att["_id"], "party_id": "lakeside-foods",
                               "recall_id": rid, "conversation_id": "conv_sim_1", "call_sid": "CAsim1",
                               "payload": {}, "received_at": simclock.server_now(db.client)})
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"},
                         {"$set": {"state": "reconciled", "next_action_at": None, "lease_epoch": 1}})
    pool = make()
    pool.startup()
    adopted = db.attempts.find_one({"_id": att["_id"]})
    assert adopted["dial_confirmed"] is True and adopted["adopted"]["evidence"] == "tool_events"
    assert adopted["call_sid"] == "CAsim1" and voice.count() == 0
    assert any(f["attempt_id"] == att["_id"] for f in feed(db, "attempt_adopted"))


def test_maintenance_leaves_a_live_runs_in_doubt_attempt_to_that_run(db, start, make, voice):
    rid = start()
    att = open_attempt(db, recall_id=rid, party_id="lakeside-foods", channel="sim", to_number="+14165550143",
                       extra={"owner": {"worker": "worker-test/w1", "epoch": 4}})
    db.parties.update_one({"recall_id": rid, "party_id": "lakeside-foods"}, [
        {"$set": {"lease_epoch": 4, "lease_owner": "worker-test/w1",
                  "lease_until": {"$add": ["$$NOW", 60000]}}}])
    pool = make()
    pool.boot_id = "b-test"
    assert pool.settle_orphans([rid]) == 0
    assert db.attempts.find_one({"_id": att["_id"]})["dial_confirmed"] is False
