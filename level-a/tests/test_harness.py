"""harness.execute end to end: brief -> decider -> gate -> effect -> facts ->
result -> release, for every kind of run, plus the brief's fixed size, the
Claude decider over a mocked transport, traces and replay, and the planner.

Shared helpers for test_gate.py and test_saboteur.py live here too:

  engine        orchestrator.claim / orchestrator.timers. The engine engineer
                owns them; until they are merged these tests install
                stand-ins written to the DESIGN_V2 §5 contract, and once they
                are, the real modules are used unchanged.
  InstantVoice  a VoiceProvider that plays the party in-process through the
                API's own handlers (levela.api.tools.handle_tool_call and
                levela.api.hooks.handle_post_call), so a claim is booked and
                the attempt closed exactly as in production, before
                place_call returns.
"""
from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import threading
import time
import types
from datetime import datetime, timezone

import httpx
import pytest
from pymongo import ReturnDocument

import levela.orchestrator as orchestrator_pkg
from levela import runs
from levela.api.hooks import handle_post_call
from levela.api.tools import handle_tool_call
from levela.db.client import run_txn
from levela.db.seed import load_scenario, start_recall
from levela.harness import HarnessContext, execute, run_planner, trace
from levela.harness import brief as brief_
from levela.harness.models import AnthropicClient
from levela.harness.decide import ClaudeDecider, Decision, RuleDecider
from levela.ledger.transfers import FREE_LEASE
from levela.memory.govern import governed
from levela.providers.base import PlacedCall

RID = "RCL-2026-017"
W2 = "worker-test/w2"
W5 = "worker-test/w5"


# ------------------------------------------------------------------ engine

class _Lost(Exception):
    pass


def _stub_timers() -> types.ModuleType:
    mod = types.ModuleType("levela.orchestrator.timers")

    def release(client, db, party, fence, *, next_action_at, ladder_step, last_outcome, extra=None, run_update=None):
        def txn(session):
            res = db.parties.update_one(
                {"_id": fence["_id"], "lease_owner": fence["lease_owner"], "lease_epoch": fence["lease_epoch"]},
                {"$set": {"next_action_at": next_action_at, "ladder_step": ladder_step, "last_outcome": last_outcome,
                          "lease_until": FREE_LEASE, **(extra or {})}}, session=session)
            if res.matched_count == 0:
                raise _Lost()
            if run_update:
                db.task_runs.update_one({"_id": run_update["_id"]},
                                        {k: v for k, v in run_update.items() if k != "_id"}, session=session)
        try:
            run_txn(client, txn)
        except _Lost:
            return False
        return True

    mod.release = release
    return mod


def _stub_claim() -> types.ModuleType:
    mod = types.ModuleType("levela.orchestrator.claim")

    def fence(party):
        return {"_id": party["_id"], "lease_owner": party["lease_owner"], "lease_epoch": party["lease_epoch"]}

    def claim_next(db, *, worker_id, sim_now, recall_id=None, lease_s=60):
        query = {"$or": [{"state": {"$ne": "reconciled"}}, {"open_questions.0": {"$exists": True}}],
                 "escalation": {"$ne": "human"}, "next_action_at": {"$ne": None, "$lte": sim_now},
                 "hold_for_demo": {"$ne": True}, "$expr": {"$lt": ["$lease_until", "$$NOW"]}}
        if recall_id:
            query["recall_id"] = recall_id
        return db.parties.find_one_and_update(query, _lease_pipeline(worker_id, lease_s),
                                             sort=[("priority", -1), ("next_action_at", 1)],
                                             return_document=ReturnDocument.AFTER)

    def extend(db, party_id, worker_id, epoch, lease_s):
        res = db.parties.update_one({"_id": party_id, "lease_owner": worker_id, "lease_epoch": epoch},
                                   [{"$set": {"lease_until": {"$add": ["$$NOW", int(lease_s * 1000)]}}}])
        return res.matched_count == 1

    def take_slot_and_intent(db, attempt, *, session):
        db.attempts.insert_one(attempt, session=session)
        return True

    mod.fence, mod.claim_next, mod.extend, mod.take_slot_and_intent = fence, claim_next, extend, take_slot_and_intent
    return mod


def _lease_pipeline(worker_id, lease_s, epoch=None):
    bump = epoch if epoch is not None else {"$add": [{"$ifNull": ["$lease_epoch", 0]}, 1]}
    return [{"$set": {"lease_owner": worker_id, "lease_until": {"$add": ["$$NOW", int(lease_s * 1000)]},
                      "lease_epoch": bump}}]


@pytest.fixture(autouse=True)
def engine(monkeypatch):
    """The engine's claim and timers: the real modules when merged, else the stand-ins."""
    for name, make in (("timers", _stub_timers), ("claim", _stub_claim)):
        full = f"levela.orchestrator.{name}"
        if importlib.util.find_spec(full) is None:
            mod = make()
            monkeypatch.setitem(sys.modules, full, mod)
            monkeypatch.setattr(orchestrator_pkg, name, mod, raising=False)
    return types.SimpleNamespace(claim=importlib.import_module("levela.orchestrator.claim"),
                                 timers=importlib.import_module("levela.orchestrator.timers"))


# ------------------------------------------------------------------- world

class InstantVoice:
    """Plays each party in-process through the API's handlers. `script` maps
    party_id -> a list of behaviours, used in order (the last one repeats):
    a dict of counts (submit_count, then a completed post-call), "no_answer",
    ("callback", sim_hour) (the API's request_callback sets
    attempt.callback_at), or "hang" (the call stays live)."""

    name = "instant"

    def __init__(self, client, db, script=None):
        self.client, self.db, self.script = client, db, dict(script or {})
        self.calls: list[dict] = []

    def count(self, party_id=None):
        return sum(1 for c in self.calls if party_id is None or c["party_id"] == party_id)

    def place_call(self, *, to_number, dynamic_variables, attempt_id):
        sid = dynamic_variables["party_id"]
        plan = self.script.get(sid) or [{"sold": 0, "returned": 0}]
        behaviour = plan[min(self.count(sid), len(plan) - 1)]
        conv, call_sid = f"conv_inst_{secrets.token_hex(6)}", f"CAinst{secrets.token_hex(8)}"
        self.calls.append({"party_id": sid, "attempt_id": attempt_id, "variables": dict(dynamic_variables)})
        dyn = {"conversation_initiation_client_data": {"dynamic_variables": {"attempt_id": attempt_id}}}
        if behaviour == "no_answer":
            handle_post_call(self.db, {"type": "call_initiation_failure",
                                       "data": {"conversation_id": conv, "failure_reason": "no-answer", **dyn}})
        elif isinstance(behaviour, tuple) and behaviour[0] == "callback":
            self.db.attempts.update_one({"_id": attempt_id}, {"$set": {"callback_at": float(behaviour[1])}})
            self._completed(conv, call_sid, dyn)
        elif isinstance(behaviour, tuple) and behaviour[0] == "answer":
            self._answer(dynamic_variables, attempt_id, behaviour[1])
            self._completed(conv, call_sid, dyn)
        elif isinstance(behaviour, dict):
            handle_tool_call(self.client, self.db, {"attempt_id": attempt_id, "party_id": sid, "source": "sim",
                                                     "system__conversation_id": conv, "system__call_sid": call_sid,
                                                     **behaviour})
            self._completed(conv, call_sid, dyn)
        return PlacedCall(conversation_id=conv, call_sid=call_sid)

    def _answer(self, variables, attempt_id, value):
        """What POST /tools/submit_answer parties: answers.<qid> with the attempt, the id pulled."""
        for qid in filter(None, variables.get("question_ids", "").split(",")):
            self.db.parties.update_one({"recall_id": RID, "party_id": variables["party_id"]},
                                      {"$set": {f"answers.{qid}": {"value": value, "attempt_id": attempt_id}},
                                       "$pull": {"open_questions": qid}})

    def _completed(self, conv, call_sid, dyn):
        handle_post_call(self.db, {"type": "post_call_transcription", "data": {
            "conversation_id": conv, "status": "done", "transcript": [{"role": "user", "message": "Counts given."}],
            "metadata": {"call_duration_secs": 30, "phone_call": {"call_sid": call_sid}}, **dyn}})


def start(client, db, *, seconds_per_sim_hour=30.0, **options) -> str:
    return start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False,
                        seconds_per_sim_hour=seconds_per_sim_hour, **options)["recall_id"]


def make_ctx(settings, client, db, voice=None, **fields) -> HarnessContext:
    providers = {"sim": voice, "voice": voice} if voice is not None else {}
    return HarnessContext(settings=settings, client=client, db=db, providers=providers,
                          recall=db.recalls.find_one({"_id": RID}), **fields)


def lease(db, party_id, worker=W2, *, epoch=None, seconds=60, rid=RID):
    """Lease one party the way claim_next does ($$NOW, epoch + 1, or the
    epoch given). Returns (party, fence)."""
    party = db.parties.find_one_and_update({"_id": f"{rid}:{party_id}"}, _lease_pipeline(worker, seconds, epoch),
                                          return_document=ReturnDocument.AFTER)
    return party, {"_id": party["_id"], "lease_owner": worker, "lease_epoch": party["lease_epoch"]}


def set_party(db, party_id, rid=RID, **fields):
    db.parties.update_one({"_id": f"{rid}:{party_id}"}, {"$set": fields})


def feed_texts(db, kind):
    return [row["text"] for row in db.feed.find({"kind": kind}).sort("at", 1)]


@pytest.fixture
def world(client, db, settings):
    """A recall_1 run on a slow clock (sim time barely moves during a test)
    and a voice that answers with counts unless told otherwise."""
    start(client, db)
    voice = InstantVoice(client, db)
    return types.SimpleNamespace(voice=voice, ctx=make_ctx(settings, client, db, voice))


def run_doc(db, run_id):
    return db.task_runs.find_one({"_id": run_id})


# ---------------------------------------------------------------- contacts

def test_a_contact_run_books_the_claim_from_the_facts_and_ends_the_ladder(db, world):
    world.voice.script["lakeside-foods"] = [{"sold": 0, "returned": 80}]
    party, fence = lease(db, "lakeside-foods")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())

    assert out["status"] == "done" and out["result"]["outcome"] == "claim_accepted"
    attempt = db.attempts.find_one({"run_id": out["run_id"]})
    assert attempt["idempotency_key"] == f"{out['run_id']}:place_call" and attempt["state"] == "completed"
    assert attempt["_id"] in out["result"]["evidence"]
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["state"] == "reconciled" and after["next_action_at"] is None
    assert after["lease_until"] == FREE_LEASE and after["last_outcome"]["outcome"] == "claim_accepted"
    run = run_doc(db, out["run_id"])
    assert run["status"] == "done" and run["used"]["tool_calls"] == 1 and run["failure_class"] is None
    assert [t["kind"] for t in trace.load(db, out["run_id"])] == ["decision", "tool", "result"]
    counters = runs.get_run(db, RID)
    assert counters["steps"] == 1 and counters["tokens"]["last"] > 0 and counters["model_calls"] == 0


def test_no_answer_moves_the_ladder_and_the_next_brief_remembers_it(db, world):
    world.voice.script["queen-st-grocer"] = ["no_answer"]
    party, fence = lease(db, "queen-st-grocer")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())

    assert out["result"]["outcome"] == "no_answer" and out["failure_class"] == "no_answer"
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["ladder_step"] == 2
    assert after["next_action_at"] == pytest.approx(out["result"]["next"]["at"])
    assert after["next_action_at"] == pytest.approx(run_doc(db, out["run_id"])["ended_sim"] + 4.0, abs=1e-3)
    episode = db.episodes.find_one({"_id": f"ep_{out['run_id']}"})
    assert episode["kind"] == "failure" and episode["failure_class"] == "no_answer"
    assert any("Queen St Grocer: no answer, attempt 1. Next action" in t for t in feed_texts(db, "run_ended"))
    facts = brief_.gather(world.ctx, after, world.ctx.sim_now())
    assert facts["memory"][0]["source"] == "failure"
    assert "[failure] no_answer" in brief_.render(facts, tools=["place_call"]).text


def test_ladder_step_3_sends_the_form_link_through_the_simulator(db, settings, client):
    start(client, db)
    seen = []

    def order_system(request: httpx.Request):
        seen.append((request.url.path, json.loads(request.content), request.headers.get("X-Tool-Secret")))
        return httpx.Response(200, json={"delivered": True})

    ctx = make_ctx(settings, client, db, InstantVoice(client, db), http=httpx.Client(transport=httpx.MockTransport(order_system)))
    set_party(db, "harbour-market", ladder_step=3)
    party, fence = lease(db, "harbour-market")
    out = execute(ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())

    assert out["result"]["outcome"] == "form_sent"
    op = db.operations.find_one({"run_id": out["run_id"]})
    assert op["kind"] == "send_form_link" and op["state"] == "sent"
    assert op["idempotency_key"] == f"{RID}:harbour-market:form:3"
    assert seen == [("/sim/form_link", {"recall_id": RID, "party_id": "harbour-market", "token": party["form_token"],
                                        "op_id": op["_id"], "form_url": op["request"]["form_url"],
                                        "ladder_step": 3}, settings.tool_secret)]
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["ladder_step"] == 4 and after["next_action_at"] == pytest.approx(op["sent_at"] + 24.0, abs=0.05)


def test_ladder_step_5_hands_a_party_to_a_person_and_an_account_to_its_rep(db, world):
    set_party(db, "harbour-market", ladder_step=5)
    party, fence = lease(db, "harbour-market")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert out["result"]["outcome"] == "escalated" and out["next_action_at"] is None
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["escalation"] == "human" and after["escalated_run"] == out["run_id"]
    assert any("Harbour Market needs a person" in t for t in feed_texts(db, "needs_human"))

    set_party(db, "lakeside-foods", ladder_step=5, distributor="queen-st-grocer", account="N-014")
    party, fence = lease(db, "lakeside-foods")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    op = db.operations.find_one({"run_id": out["run_id"]})
    assert out["result"]["outcome"] == "escalated" and op["kind"] == "escalate_rep" and op["state"] == "sent"
    assert op["idempotency_key"] == f"{RID}:lakeside-foods:escalate_rep"
    assert db.parties.find_one({"_id": party["_id"]})["escalation"] == "rep"
    assert world.voice.count() == 0


def test_a_callback_request_becomes_the_timer_and_keeps_the_step(db, world):
    world.voice.script["queen-st-grocer"] = [("callback", 33.0)]
    party, fence = lease(db, "queen-st-grocer")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert out["result"]["outcome"] == "callback_requested"
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["next_action_at"] == 33.0 and after["ladder_step"] == 1


def test_a_place_call_failpoint_kills_the_run_after_the_dial_and_leaves_the_attempt_in_doubt(db, settings, client):
    from levela.orchestrator.failpoints import Failpoints, WorkerCrash, arm, raise_crash

    start(client, db)
    voice = InstantVoice(client, db, {"queen-st-grocer": ["hang"]})
    ctx = make_ctx(settings, client, db, voice, failpoints=Failpoints(db, crash=raise_crash, env={}))
    arm(db, "after_dial_request", party_id="queen-st-grocer")
    party, fence = lease(db, "queen-st-grocer")
    with pytest.raises(WorkerCrash):
        execute(ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    attempt = db.attempts.find_one({"party_id": "queen-st-grocer"})
    assert voice.count() == 1 and attempt["state"] == "intent" and attempt["dial_confirmed"] is False


L4480 = "Did you also receive batch B-4480?"
ASKED_L4480 = f"q-01: {L4480} (answer: yes or no, and how many tins)."


def insert_question(db, text=L4480, answer_type="yes_no_count", sim_now=9.0):
    """A question exactly as POST /admin/questions parties it (levela.api.questions)."""
    from levela.api import questions

    return questions.insert_question(db, recall_id=RID, text=text, answer_type=answer_type, applies_to={},
                                     sim_now=sim_now, by="admin")


def test_a_reconciled_party_is_asked_only_its_open_question(db, world):
    """Counts accepted with a question still open: due again now, and the next
    call asks the question only (purpose ask_question, ask_counts no), with
    the question's text in the form agent/prompt.md reads, then the party has
    no timer. The question is stored the way the API parties it."""
    q = insert_question(db)
    assert (q["_id"], q["question_id"]) == (f"{RID}:q-01", "q-01")
    set_party(db, "lakeside-foods", open_questions=["q-01"])
    world.voice.script["lakeside-foods"] = [{"sold": 0, "returned": 80},
                                            ("answer", "no")]
    party, fence = lease(db, "lakeside-foods")
    first = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert first["result"]["outcome"] == "claim_accepted"
    assert first["next_action_at"] is not None and first["next_action_at"] <= world.ctx.sim_now()
    counted = world.voice.calls[-1]["variables"]
    assert (counted["purpose"], counted["ask_counts"], counted["questions"]) == ("initial", "yes", ASKED_L4480)

    party, fence = lease(db, "lakeside-foods")
    assert brief_.open_questions(db, party) == [{"id": "q-01", "text": L4480, "answer_type": "yes_no_count"}]
    second = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert second["kind"] == "ask_question" and second["result"]["outcome"] == "answer_recorded"
    variables = world.voice.calls[-1]["variables"]
    assert (variables["purpose"], variables["ask_counts"]) == ("ask_question", "no")
    assert (variables["question_ids"], variables["questions"]) == ("q-01", ASKED_L4480)
    attempt = db.attempts.find_one({"run_id": second["run_id"]})
    assert attempt["purpose"] == "ask_question" and attempt["context"]["questions"] == ASKED_L4480
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["answers"]["q-01"]["value"] == "no" and after["open_questions"] == []
    assert after["next_action_at"] is None and db.claims.count_documents({"party_id": "lakeside-foods"}) == 1


def test_a_call_asks_at_most_three_questions_and_the_brief_lists_the_same(db, world):
    """Four open questions: the brief and the call carry the first three (the
    call stays short); an id with no question document is never asked."""
    for i in range(4):
        insert_question(db, text=f"Did you also receive batch L-448{i}?", answer_type="yes_no")
    set_party(db, "lakeside-foods", open_questions=["q-01", "q-09", "q-02", "q-03", "q-04"])
    party = db.parties.find_one({"_id": f"{RID}:lakeside-foods"})
    listed = brief_.open_questions(db, party)
    assert [q["id"] for q in listed] == ["q-01", "q-02", "q-03"], "q-09 has no document; q-04 waits"
    world.voice.script["lakeside-foods"] = [{"sold": 0, "returned": 80}]
    party, fence = lease(db, "lakeside-foods")
    execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    variables = world.voice.calls[-1]["variables"]
    assert variables["question_ids"] == "q-01,q-02,q-03"
    assert variables["questions"] == " ".join(f"q-0{i + 1}: Did you also receive batch L-448{i}? (answer: yes or no)."
                                              for i in range(3))


def test_the_planner_adds_a_question_the_way_the_api_does(db, world):
    """The planner's add_question writes the API's schema ("<recall>:q-NN",
    question_id "q-NN") inside the gate's transaction and fans it out like
    POST /admin/questions (never to a distributor), so the brief, the call
    and /tools/submit_answer all find it. The same text again is the same
    question."""
    from levela.api.questions import get_question
    from levela.harness import budget, gate

    make_distributor(db)
    db.worker_state.insert_one({"_id": "planner", "lease_owner": "worker-test/planner", "lease_epoch": 1,
                                "lease_until": datetime(2999, 1, 1, tzinfo=timezone.utc)})
    run = budget.open_run(db, worker="worker-test/planner", recall_id=RID, party_id=None,
                          lease={"coll": "worker_state", "_id": "planner"}, lease_epoch=1, kind="planner", sim_now=9.0,
                          decider="rules")
    added = gate.call(world.ctx, run, "add_question", {"text": f"  {L4480} ", "answer_type": "yes_no_count",
                                                       "applies_to": {"tier": [1]}})
    assert added.allowed and not added.duplicate, added.reason
    [doc] = db.questions.find({"recall_id": RID})
    assert (doc["_id"], doc["question_id"], doc["text"], doc["applies_to"]) == (
        f"{RID}:q-01", "q-01", L4480, {"tier": [1]})
    assert get_question(db, RID, "q-01")["_id"] == doc["_id"]
    given = {s["party_id"]: s.get("open_questions") for s in db.parties.find({"recall_id": RID})}
    assert given == {"harbour-market": ["q-01"], "queen-st-grocer": ["q-01"], "lakeside-foods": ["q-01"],
                     "dist-north": []}
    lakeside = db.parties.find_one({"_id": f"{RID}:lakeside-foods"})
    assert brief_.open_questions(db, lakeside) == [{"id": "q-01", "text": L4480, "answer_type": "yes_no_count"}]
    assert feed_texts(db, "question_added") == [f"Planner added question q-01: {L4480} (3 parties now due)"]

    again = gate.call(world.ctx, run, "add_question", {"text": L4480.lower(), "answer_type": "yes_no_count"})
    assert again.allowed and again.duplicate and db.questions.count_documents({}) == 1
    refused = gate.call(world.ctx, run, "add_question", {"text": "Any left?", "answer_type": "essay"})
    assert not refused.allowed and refused.rule == "intent" and "answer_type" in refused.reason


def test_a_claimed_party_waiting_on_its_counterpart_is_released_without_a_run(db, world):
    set_party(db, "lakeside-foods", state="claimed", ladder_step=2)
    party, fence = lease(db, "lakeside-foods")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert out["status"] == "deferred" and out["run_id"] is None and out["next_action_at"] is None
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["lease_until"] == FREE_LEASE and after["next_action_at"] is None
    assert db.task_runs.count_documents({}) == 0


@pytest.mark.slow
def test_a_disputed_transfer_gets_one_followup_per_side(db, world):
    """Queen St says 40 went to Corner Mart, Corner Mart says 25 arrived: each
    side is told the other's number once, and the dispute stays open."""
    world.voice.script.update({
        "queen-st-grocer": [{"sold": 50, "sold_source": "till_report", "held": 30, "batch_seen": "B-4471", "block_test": True, "forwarded_qty": 40,
                             "forwarded_to_name": "Corner Mart"}],
        "corner-mart": [{"received_qty": 25, "held": 25, "batch_seen": "B-4471", "block_test": True}]})
    for sid in ("queen-st-grocer", "corner-mart"):
        party, fence = lease(db, sid)
        assert execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())["result"]["outcome"] == \
            "claim_accepted"
    edge = db.edges.find_one({"recall_id": RID})
    assert edge["state"] == "disputed" and edge["disputed_qty"] == 15
    # Corner Mart's own claim opened the dispute: it is due again for its follow-up.
    corner = db.parties.find_one({"_id": f"{RID}:corner-mart"})
    assert corner["next_action_at"] is not None and corner["next_action_at"] <= world.ctx.sim_now()
    outs = []
    for sid in ("queen-st-grocer", "corner-mart"):
        party, fence = lease(db, sid)
        outs.append(execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider()))
    assert [o["kind"] for o in outs] == ["followup", "followup"]
    followups = list(db.attempts.find({"purpose": "dispute_followup"}).sort("party_id", 1))
    assert [(a["party_id"], a["told"]) for a in followups] == [("corner-mart", 40), ("queen-st-grocer", 25)]
    party, fence = lease(db, "queen-st-grocer")
    again = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert again["status"] == "deferred" and db.attempts.count_documents({"purpose": "dispute_followup"}) == 2


# ------------------------------------------------------------------- order system

@pytest.fixture
def fake_operations(monkeypatch):
    """Stands in for levela.systems.operations (the systems engineer's) with
    the contract the order system tools call: execute(ctx, op) sends the operation,
    reads it back and records it. This one verifies everything at once and
    read_accounts adds one account to the distributor."""
    from levela.harness.verify import mark_verified

    sent = []

    def perform(ctx, op):
        sent.append((op["kind"], op["idempotency_key"]))
        ctx.db.operations.update_one({"_id": op["_id"]}, {"$set": {"state": "sent", "sent_at": 9.0}})
        if op["kind"] == "read_accounts":
            ctx.db.parties.update_one({"_id": f"{RID}:lakeside-foods"},
                                     {"$set": {"distributor": op["party_id"], "account": "N-001"}})
        mark_verified(ctx.db, op["_id"], readback_value={"ok": op["kind"]}, readback_at=9.1)

    from levela.systems import operations as real_operations

    def execute(ctx, op, *, fresh=False):
        perform(ctx, op)
        return {"state": ctx.db.operations.find_one({"_id": op["_id"]})["state"]}

    # The real intent_for builds the operation; only carrying it out is faked.
    monkeypatch.setattr(real_operations, "execute", execute)
    return sent


def make_distributor(db, sid="dist-north", order_system="north", rid=RID):
    db.parties.insert_one({**db.parties.find_one({"_id": f"{rid}:harbour-market"}, {"_id": 0}),
                          "_id": f"{rid}:{sid}", "party_id": sid, "name": "Distributor North", "channel": "order_system",
                          "phone": None, "order_system": order_system, "level": 1, "lease_epoch": 0, "lease_owner": None})


def test_order_system_sync_runs_its_operations_in_order_once_a_day(db, world, fake_operations):
    make_distributor(db)
    party, fence = lease(db, "dist-north")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())

    assert out["kind"] == "order_system_sync" and out["result"]["outcome"] == "accounts_read"
    day = int(world.ctx.sim_now() // 24)
    assert fake_operations == [("place_hold", f"{RID}:dist-north:place_hold"),
                               ("read_accounts", f"{RID}:dist-north:read_accounts"),
                               ("read_inventory", f"{RID}:dist-north:read_inventory:d{day}")]
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["next_action_at"] == pytest.approx(24.0 * (day + 1) + 9.0)  # tomorrow's inventory read
    assert runs.get_run(db, RID)["steps"] == 1   # reads are not steps; the hold is

    party, fence = lease(db, "dist-north")
    again = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    assert again["status"] == "deferred" and len(fake_operations) == 3


# -------------------------------------------------------------------- brief

def test_the_brief_does_not_grow_with_the_run(db, world):
    """Same party shape at step 1 and at step 300: same text, same tokens,
    whatever the rest of the run has piled up."""
    party = db.parties.find_one({"_id": f"{RID}:queen-st-grocer"})
    first = brief_.render(brief_.gather(world.ctx, party, 9.0), tools=["place_call", "send_form_link"])

    db.runs.update_one({"_id": f"run:{RID}"}, {"$set": {"steps": 300}})
    other = [{"_id": f"att_other{i:04d}", "recall_id": RID, "party_id": "lakeside-foods", "seq": i + 1,
              "channel": "sim", "state": "no_answer", "purpose": "retry"} for i in range(300)]
    db.attempts.insert_many(other)
    db.traces.insert_many([{"_id": f"tr-old:{i:03d}", "run_id": "tr-old", "seq": i, "kind": "tool"} for i in range(300)])
    db.episodes.insert_many([{"_id": f"ep_x{i}", **governed("failure", party_id="lakeside-foods"), "recall_id": RID,
                              "party_id": "lakeside-foods", "failure_class": "no_answer", "summary": "x",
                              "created_at": datetime.now(timezone.utc)} for i in range(50)])
    later = brief_.render(brief_.gather(world.ctx, party, 9.0), tools=["place_call", "send_form_link"])
    assert later.text == first.text and later.tokens == first.tokens

    # Its own history is capped too: 50 failures still make 3 memory lines.
    db.episodes.update_many({}, {"$set": {"party_id": "queen-st-grocer", "scope": "queen-st-grocer"}})
    capped = brief_.gather(world.ctx, party, 9.0)
    assert len(capped["memory"]) == brief_.MAX_MEMORY
    for section in ("RULES", "PARTY", "EDGES", "MEMORY", "QUESTIONS", "ASK"):
        assert any(line.startswith(section) for line in first.text.splitlines())


# ------------------------------------------------------------------ deciders

def anthropic_reply(*, tool=None, args=None, text="", input_tokens=1234, stop_reason=None):
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "toolu_1", "name": tool, "input": args or {}})
    return {"id": "msg_1", "type": "message", "model": "claude-haiku-5-5", "content": content,
            "stop_reason": stop_reason or ("tool_use" if tool else "end_turn"),
            "usage": {"input_tokens": input_tokens, "output_tokens": 40}}


def test_the_claude_decider_offers_the_allowlist_and_records_the_real_input_tokens(db, world):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=anthropic_reply(tool="place_call", args={"note": "step 1"}))

    decider = ClaudeDecider(AnthropicClient("test-key", http=httpx.Client(transport=httpx.MockTransport(handler))),
                            "claude-haiku-5-5")
    world.voice.script["lakeside-foods"] = [{"sold": 20, "sold_source": "till_report", "held": 60, "batch_seen": "B-4471", "block_test": True}]
    party, fence = lease(db, "lakeside-foods")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=decider)

    assert out["result"]["outcome"] == "claim_accepted" and len(requests) == 1
    sent = json.loads(requests[0].content)
    assert requests[0].headers["anthropic-version"] == "2026-10-21" and requests[0].headers["x-api-key"] == "test-key"
    assert sent["model"] == "claude-haiku-5-5" and sent["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in sent["tools"]] == ["place_call", "send_form_link", "schedule_callback", "escalate"]
    assert not {"temperature", "top_p", "top_k"} & set(sent)
    assert sent["messages"][0]["content"].startswith("RULES")
    counters = runs.get_run(db, RID)
    assert counters["model_calls"] == 1 and counters["tokens"]["last"] == 1234
    assert run_doc(db, out["run_id"])["used"]["model_calls"] == 1


def test_a_model_that_finds_nothing_owed_leaves_the_step_and_looks_again_shortly(db, world):
    reply = anthropic_reply(text="Nothing is owed now.", input_tokens=900)
    client = AnthropicClient("k", http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=reply))))
    party, fence = lease(db, "harbour-market")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=ClaudeDecider(client, "claude-haiku-5-5"))
    assert out["status"] == "done" and out["failure_class"] is None and out["ladder_step"] == 1
    assert out["next_action_at"] == pytest.approx(run_doc(db, out["run_id"])["ended_sim"] + 0.25, abs=1e-3)
    assert db.episodes.count_documents({}) == 0 and world.voice.count() == 0


def test_the_claude_decider_falls_back_to_the_rules_when_the_model_is_unavailable(db, world):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})

    client = AnthropicClient("k", http=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    party = db.parties.find_one({"_id": f"{RID}:harbour-market"})
    facts = brief_.gather(world.ctx, party, 9.0)
    decision = ClaudeDecider(client, "claude-haiku-5-5").decide(brief_.render(facts, tools=["place_call"]))
    assert decision.tool == "place_call" and "model unavailable" in decision.note and len(calls) == 1


# -------------------------------------------------------------- trace/replay

def test_a_recorded_run_replays_to_the_same_decisions_with_tools_stubbed(db, world):
    party, fence = lease(db, "harbour-market")
    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    db.attempts.delete_many({})          # replay must not need the effects, only the traces

    report = trace.replay(db, out["run_id"], RuleDecider())
    assert report["total"] == 1 and report["matched"] == 1
    assert report["result"]["output"]["result"]["outcome"] == "claim_accepted"

    class Contrary:
        def decide(self, brief):
            return Decision("send_form_link")

    assert trace.replay(db, out["run_id"], Contrary())["matched"] == 0

    from scripts.replay_run import render
    assert "1 of 1 decisions match" in render(report)


# --------------------------------------------------------------------- planner

def test_the_planner_ranks_through_the_gate_and_writes_its_report(db, world):
    db.worker_state.insert_one({"_id": "planner", "lease_owner": "worker-test/planner", "lease_epoch": 3,
                                "lease_until": datetime(2999, 1, 1, tzinfo=timezone.utc)})
    insert_question(db, text="Any other batches?", answer_type="yes_no")
    set_party(db, "lakeside-foods", open_questions=["q-01"])
    out = run_planner(world.ctx, worker_id="worker-test/planner",
                   fence={"_id": "planner", "lease_owner": "worker-test/planner", "lease_epoch": 3}, decider=RuleDecider())

    assert out["status"] == "done"
    priorities = {s["party_id"]: s["priority"] for s in db.parties.find({"recall_id": RID})}
    assert priorities == {"harbour-market": 40, "queen-st-grocer": 120, "lakeside-foods": 130}
    assert [a["tool"] for a in out["result"]["actions"]] == ["set_priority"] * 3 + ["draft_report"]
    assert "parties still open" in runs.get_run(db, RID)["report"]["text"]


def test_the_planner_may_not_call_a_contact_tool(db, world):
    from levela.harness import budget, gate

    db.worker_state.insert_one({"_id": "planner", "lease_owner": "worker-test/planner", "lease_epoch": 1,
                                "lease_until": datetime(2999, 1, 1, tzinfo=timezone.utc)})
    run = budget.open_run(db, worker="worker-test/planner", recall_id=RID, party_id=None,
                          lease={"coll": "worker_state", "_id": "planner"}, lease_epoch=1, kind="planner", sim_now=9.0,
                          decider="rules")
    decision = gate.call(world.ctx, run, "place_call", {})
    assert not decision.allowed and decision.rule == "allowlist"


# ------------------------------------------------------------- lost leases

def test_a_run_whose_lease_is_taken_writes_nothing_to_the_party(db, world):
    """Another worker claims the party while this run is deciding: the gate
    refuses on lease, and the release (with the old fence) writes nothing."""
    party, fence = lease(db, "harbour-market")

    class Overtaken:
        name, is_model, continues_after_effect = "overtaken", False, False

        def decide(self, brief):
            lease(db, "harbour-market", worker="worker-test/w7")
            return Decision("place_call")

    out = execute(world.ctx, party, worker_id=W2, fence=fence, decider=Overtaken())
    assert out["status"] == "refused" and out["rule"] == "lease" and not out["released"]
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["lease_owner"] == "worker-test/w7" and after["lease_epoch"] == fence["lease_epoch"] + 1
    assert after["next_action_at"] == party["next_action_at"] and world.voice.count() == 0
    assert run_doc(db, out["run_id"])["failure_class"] == "gate_refused"


def test_waiting_on_a_live_call_keeps_the_lease_and_gives_up_at_the_floor(db, settings, client):
    """2 simulated hours at 0.5 s each is 1 real second, so the 2.5 s floor
    governs. The 2 s lease outlives the wait only because it is extended
    with the fence; the release at the end then succeeds."""
    start(client, db, seconds_per_sim_hour=0.5)
    voice = InstantVoice(client, db, {"queen-st-grocer": ["hang"]})
    ctx = make_ctx(settings, client, db, voice)
    party, fence = lease(db, "queen-st-grocer", seconds=2)
    started = time.monotonic()
    out = execute(ctx, party, worker_id=W2, fence=fence, decider=RuleDecider(), wait_floor_s=2.5, extend_every_s=0.5)
    assert time.monotonic() - started >= 2.5
    assert out["status"] == "stopped" and out["failure_class"] == "budget_exceeded" and out["released"]
    assert db.attempts.find_one({"party_id": "queen-st-grocer"})["state"] == "dialed"
    assert any("budget_exceeded after 2 simulated hours" in t for t in feed_texts(db, "gate_stopped"))


def test_a_decider_exception_is_raised_to_the_worker(db, world):
    class Broken:
        name, is_model = "broken", False

        def decide(self, brief):
            raise RuntimeError("decider bug")

    party, fence = lease(db, "harbour-market")
    with pytest.raises(RuntimeError, match="decider bug"):
        execute(world.ctx, party, worker_id=W2, fence=fence, decider=Broken())


def test_decisions_run_off_the_calling_thread(db, world):
    seen = []

    class Recorder(RuleDecider):
        def decide(self, brief):
            seen.append(threading.current_thread().name)
            return super().decide(brief)

    party, fence = lease(db, "harbour-market")
    execute(world.ctx, party, worker_id=W2, fence=fence, decider=Recorder())
    assert seen == ["decider"]
