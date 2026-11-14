"""Questions as data through the API: POST/GET /admin/questions, the
submit_answer tool, the hourly fan-out, and spec test 33 at the API level
(a question added mid-run: only parties without an answer become due,
reconciled parties are asked the question only, no count is asked again).

What a worker would see is checked where it lives: parties.open_questions and
next_action_at (the claim query), and questions.call_variables (the dynamic
variables of the call). Spec test 33 is also run end to end (slow): a worker
pool with the real harness calls the simulated parties and they answer through
/tools/submit_answer. The mass balance is asserted after every ledger write."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from levela import clock, harness, runs
from levela.api import questions
from levela.api.main import create_app
from levela.api.questions import QuestionError, call_variables
from levela.api.scheduler import Scheduler
from levela.clock import Clock
from levela.config import LIVE_ATTEMPT_STATES
from levela.db.seed import load_scenario, start_recall
from levela.ledger.reconciler import metrics
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.orchestrator.failpoints import Failpoints
from levela.orchestrator.worker import WorkerPool, build_decider
from levela.providers import ClientPoster, FakeVoice, SimNetwork

R1 = "RCL-2026-017"
TOOL = {"X-Tool-Secret": "test-tool-secret"}
ADMIN = {"X-Admin-Token": "test-admin-token"}
COUNTS = ("shipped_in", "sold", "on_shelf", "held", "returned", "forwarded", "unresolved", "state")
L4480 = "Did you also receive batch B-4480?"


class AllHold:
    """Stands in for levela.ledger.invariants in the scheduler's hourly pass."""

    def check(self, db, recall_id, order_systems):
        return {"at": None, "passed": [True] * 4, "details": [None] * 4}

    def record(self, db, recall_id, result):
        pass


@pytest.fixture
def app(settings, client):
    return create_app(settings, client=client, sim_network=object())


@pytest.fixture
def api(app):
    return TestClient(app)


@pytest.fixture
def rid(client, db):
    """recall_1 with its clock frozen at d2 02:00, so simulated times are exact."""
    recall_id = start_recall(client, db, load_scenario("recall_1"), all_sim=True)["recall_id"]
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, recall_id, Clock(real_start=t0, sim_start=50.0, paused_at=t0))
    clock.forget()
    return recall_id


def party(db, rid, party_id):
    return db.parties.find_one({"recall_id": rid, "party_id": party_id})


def dial(db, rid, party_id, purpose="initial"):
    att = open_attempt(db, recall_id=rid, party_id=party_id, channel="sim", purpose=purpose,
                       context={"ask_delivery_note": "no"})
    mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CAsim{att['_id']}")
    return att


def say(api, att, **claim):
    r = api.post("/tools/submit_count", headers=TOOL, json={"attempt_id": att["_id"], "party_id": att["party_id"],
                                                            **claim})
    assert r.status_code == 200, r.text
    return r.json()


def answer(api, att, question_id, value):
    r = api.post("/tools/submit_answer", headers=TOOL, json={"attempt_id": att["_id"], "party_id": att["party_id"],
                                                             "question_id": question_id, "value": value})
    assert r.status_code == 200, r.text
    return r.json()


def ask(api, rid, text=L4480, answer_type="yes_no_count", **applies_to):
    return api.post("/admin/questions", headers=ADMIN, json={"recall_id": rid, "text": text,
                                                             "answer_type": answer_type, "applies_to": applies_to})


def texts(db, kind):
    return [line["text"] for line in db.feed.find({"kind": kind}).sort("at", 1)]


def test_spec_33_question_added_mid_run(api, app, client, db, rid, assert_balance):
    # Mid-run: Harbour Market is reconciled with no timer left; the other two wait on timers.
    harbour = dial(db, rid, "harbour-market")
    assert say(api, harbour, sold=35, sold_source="till_report", returned=5)["accepted"]
    assert_balance(rid)
    complete(db, harbour["_id"], "completed")
    db.parties.update_one({"_id": f"{rid}:harbour-market"}, {"$set": {"next_action_at": None}})
    db.parties.update_many({"recall_id": rid, "party_id": {"$in": ["lakeside-foods", "queen-st-grocer"]}},
                          {"$set": {"next_action_at": 60.0, "ladder_step": 2}})
    booked = party(db, rid, "harbour-market")
    assert booked["state"] == "reconciled"

    r = ask(api, rid, tier=[1, 2, 3])
    assert r.status_code == 200, r.text
    q = r.json()
    assert (q["_id"], q["question_id"], q["due"], q["asked_at"], q["applies_to"]) == (
        f"{rid}:q-01", "q-01", 3, 50.0, {"tier": [1, 2, 3]})
    for sid in ("harbour-market", "lakeside-foods", "queen-st-grocer"):
        s = party(db, rid, sid)
        assert (s["open_questions"], s["next_action_at"]) == (["q-01"], 50.0), sid     # all due now
    [line] = db.feed.find({"kind": "question_added"})
    assert (line["text"], line["system"], line["sim_label"]) == ("question q-01 added: 3 parties due", "planner", "d2 02:00")
    assert db.runs.find_one({"_id": runs.run_id(rid)})["planner_wake_reason"] == "question q-01 added"

    # The reconciled party is asked the question only: its counts stay as booked.
    h = party(db, rid, "harbour-market")
    assert {k: h[k] for k in COUNTS} == {k: booked[k] for k in COUNTS}
    assert call_variables(db, h) == {
        "questions": f"q-01: {L4480} (answer: yes or no, and how many tins).", "question_ids": "q-01",
        "ask_counts": "no"}
    assert call_variables(db, party(db, rid, "lakeside-foods"))["ask_counts"] == "yes"

    # Lakeside answers on its call. An answer books nothing in the ledger.
    lake = dial(db, rid, "lakeside-foods")
    assert answer(api, lake, "q-01", "yes, 12 tins") == {"accepted": True, "message": "Recorded. No more questions.",
                                                          "questions_left": 0}
    s = party(db, rid, "lakeside-foods")
    assert s["answers"] == {"q-01": {"value": {"answer": "yes", "count": 12}, "at": 50.0, "attempt_id": lake["_id"]}}
    assert s["open_questions"] == [] and s["state"] == "reached"
    assert texts(db, "answer_recorded") == ["Lakeside Foods answered q-01: yes, 12 tins"]
    assert db.tool_events.find_one({"attempt_id": lake["_id"]})["kind"] == "answer"
    assert db.claims.count_documents({"party_id": "lakeside-foods"}) == 0
    again = answer(api, lake, "q-01", "Yes, 12")                       # a redelivered tool call
    assert (again["accepted"], again["duplicate"]) == (True, True)
    assert len(texts(db, "answer_recorded")) == 1

    # Harbour on a question-only call: answered, still reconciled, no new claim.
    assert answer(api, dial(db, rid, "harbour-market", purpose="ask_question"), "q-01", "no")["accepted"]
    h = party(db, rid, "harbour-market")
    assert h["answers"]["q-01"]["value"] == {"answer": "no", "count": 0}
    assert {k: h[k] for k in COUNTS} == {k: booked[k] for k in COUNTS}
    assert db.claims.count_documents({"party_id": "harbour-market"}) == 1
    assert_balance(rid)

    # Corner Mart is found later. The hourly pass gives it q-01 and makes it due; the parties
    # that answered, and Queen St (which has it open), are not touched.
    queen = dial(db, rid, "queen-st-grocer")
    assert say(api, queen, returned=80, forwarded_qty=40, forwarded_to_name="Corner Mart")["accepted"]
    assert_balance(rid)
    db.parties.update_many({"recall_id": rid}, {"$set": {"next_action_at": 70.0}})
    [done] = Scheduler(app.state.settings, client, db, http=None, invariants=AllHold()).tick(force=True)
    assert done["questions"] == {"q-01": 1}
    corner = party(db, rid, "corner-mart")
    assert (corner["tier"], corner["open_questions"], corner["next_action_at"]) == (3, ["q-01"], 50.0)
    assert call_variables(db, corner)["ask_counts"] == "yes"
    for sid in ("harbour-market", "lakeside-foods", "queen-st-grocer"):
        assert party(db, rid, sid)["next_action_at"] == 70.0, sid
    assert party(db, rid, "queen-st-grocer")["open_questions"] == ["q-01"]

    # A question for tier 3 only reaches only Corner Mart.
    assert (ask(api, rid, "Do you still have any on the shelf?", "yes_no", tier=[3]).json()["due"]) == 1
    assert call_variables(db, party(db, rid, "corner-mart"))["question_ids"] == "q-01,q-02"
    listed = api.get("/admin/questions", params={"recall_id": rid}, headers=ADMIN).json()["questions"]
    assert [(x["question_id"], x["open"], x["answered"]) for x in listed] == [("q-01", 2, 2), ("q-02", 1, 0)]


def _quiet(db, recall_id) -> bool:
    """No call in flight and no party leased."""
    return (db.attempts.count_documents({"recall_id": recall_id, "state": {"$in": list(LIVE_ATTEMPT_STATES)}}) == 0
            and db.parties.count_documents({"recall_id": recall_id, "$expr": {"$gt": ["$lease_until", "$$NOW"]}}) == 0)


def _wait(check, timeout: float, what) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, what() if callable(what) else what
        time.sleep(0.25)


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_spec_33_end_to_end_a_question_added_mid_run_is_asked_and_answered(client, db, settings, assert_balance):
    """Spec test 33 through every real piece in one process: the API (hosting
    the simulator), a pool of four worker threads with the real harness, and
    the simulated parties of recall 1. Mid-run (every party's counts booked,
    the dispute waiting for the operator) POST /admin/questions adds a question
    (after the plant's three).
    Each party is then called once with purpose ask_question: the call
    carries the question as agent/prompt.md reads it and ask_counts no, the
    party answers through POST /tools/submit_answer, the answer is stored and
    open_questions empties, no count is asked or booked, and nothing calls
    the party again."""
    api = TestClient(create_app(settings, client=client))
    net = SimNetwork(db, ClientPoster(api, settings.tool_secret), settings)
    api.app.state.sim = net
    voice = FakeVoice(net)
    pool = WorkerPool(settings, db, client, providers={"sim": voice, "voice": voice},
                      probes={"sim": voice.probe, "voice": voice.probe},
                      failpoints=Failpoints(db, env={}, worker=settings.worker_name), decider=build_decider(settings),
                      harness=harness, workers=4, recall_id=R1)
    stop = threading.Event()
    thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    started = api.post("/admin/recalls", headers=ADMIN, json={"scenario": "recall_1", "all_sim": True, "mode": "sim",
                                                              "followups": "manual", "memory": False})
    assert started.status_code == 200, started.text
    parties = ("harbour-market", "queen-st-grocer", "lakeside-foods", "corner-mart")
    thread.start()
    try:
        # The four stores are checked; the plant (tier 0) answers its own questions and holds no tins.
        _wait(lambda: all(party(db, R1, sid)["state"] in ("claimed", "reconciled") for sid in parties)
              and _quiet(db, R1), 90, lambda: f"recall 1 did not settle: {metrics(db, R1)}")
        booked = {sid: {k: party(db, R1, sid)[k] for k in COUNTS} for sid in parties}
        assert booked["harbour-market"]["state"] == booked["lakeside-foods"]["state"] == "reconciled"
        claims_before = {sid: db.claims.count_documents({"recall_id": R1, "party_id": sid}) for sid in parties}
        calls_before = {sid: voice.count(sid) for sid in parties}

        before = db.questions.count_documents({"recall_id": R1})      # the plant's three questions come first
        r = ask(api, R1, tier=[1, 2, 3])
        assert r.status_code == 200, r.text
        qid = r.json()["question_id"]
        assert (qid, r.json()["due"]) == (f"q-{before + 1:02d}", 4)
        _wait(lambda: all(not party(db, R1, sid)["open_questions"] for sid in parties) and _quiet(db, R1), 90,
              lambda: {sid: party(db, R1, sid).get("open_questions") for sid in parties})
        time.sleep(1.0)                  # a party wrongly due again would be claimed by now
        assert _quiet(db, R1)
    finally:
        stop.set()
        thread.join(30)
        net.stop()
        net.wait_idle(10)

    asked = f"{qid}: {L4480} (answer: yes or no, and how many tins)."
    for sid in parties:
        s = party(db, R1, sid)
        [call] = list(db.attempts.find({"recall_id": R1, "party_id": sid, "purpose": "ask_question"}))
        assert (call["state"], call["context"]["ask_counts"]) == ("completed", "no"), sid
        assert (call["context"]["question_ids"], call["context"]["questions"]) == (qid, asked), sid
        assert s["answers"][qid]["attempt_id"] == call["_id"], sid
        assert s["answers"][qid]["value"] == {"answer": "no", "count": 0}, sid      # recall 1 knows no other batch
        assert s["open_questions"] == [] and s["next_action_at"] is None, sid
        assert voice.count(sid) == calls_before[sid] + 1, f"{sid} is called once for the question"
        assert {k: s[k] for k in COUNTS} == booked[sid], f"{sid}'s counts are not asked again"
        assert db.claims.count_documents({"recall_id": R1, "party_id": sid}) == claims_before[sid], sid
        [row] = list(db.sim_calls.find({"attempt_id": call["_id"]}))
        assert row["replies"] and all("answer" in reply and "claim" not in reply for reply in row["replies"]), sid
    assert sorted(t for t in texts(db, "answer_recorded") if f" {qid}:" in t) == sorted(
        f"{party(db, R1, sid)['name']} answered {qid}: no" for sid in parties)
    assert_balance(R1)


def test_answers_and_questions_the_api_refuses(api, db, rid):
    assert api.post("/admin/questions", json={"recall_id": rid, "text": L4480}).status_code == 401
    assert ask(api, "RCL-NOPE").status_code == 404
    for bad in ({"answer_type": "essay"}, {"text": ""}):
        body = {"recall_id": rid, "text": L4480, **bad}
        r = api.post("/admin/questions", headers=ADMIN, json=body)
        assert r.status_code == 400, (bad, r.text)
    assert ask(api, rid, tier=[4]).status_code == 400
    assert api.post("/admin/questions", headers=ADMIN, json={"recall_id": rid, "text": L4480,
                                                             "applies_to": {"region": ["east"]}}).status_code == 400
    assert ask(api, rid, L4480, "yes_no", tier=[1]).json()["due"] == 3
    assert ask(api, rid, "Any left?", "count", tier=[3]).json()["due"] == 0           # no tier-3 party yet

    att = dial(db, rid, "lakeside-foods")
    r = answer(api, att, "q-01", "maybe")
    assert (r["accepted"], r["stop"], r["questions_left"]) == (False, False, 1) and "yes or no" in r["message"]
    assert "q-01" not in (party(db, rid, "lakeside-foods").get("answers") or {})
    for qid in ("q-09", None):
        r = answer(api, att, qid, "yes")
        assert r["accepted"] is False and "There is no question" in r["message"]
    r = answer(api, att, "q-02", 4)
    assert r["accepted"] is False and "not one of this party's questions" in r["message"]

    assert answer(api, att, "q-01", "yes")["accepted"]
    assert answer(api, att, "q-01", "no")["accepted"]                   # corrected on the same call
    assert party(db, rid, "lakeside-foods")["answers"]["q-01"]["value"] == "no"
    assert texts(db, "answer_recorded") == ["Lakeside Foods answered q-01: yes", "Lakeside Foods corrected q-01: no"]

    unmatched = api.post("/tools/submit_answer", headers=TOOL, json={"attempt_id": att["_id"],
                                                                     "party_id": "harbour-market",
                                                                     "question_id": "q-01", "value": "yes"}).json()
    assert (unmatched["accepted"], unmatched["stop"]) == (False, True)
    assert api.post("/tools/submit_answer", json={"attempt_id": att["_id"]}).status_code == 401
    assert api.post("/tools/submit_answer", headers=TOOL, content=b"[]").status_code == 400

    db.recalls.update_one({"_id": rid}, {"$set": {"status": "closed", "open_units": 0}})
    assert ask(api, rid).status_code == 409


@pytest.mark.parametrize("answer_type, value, stored", [
    ("yes_no_count", "yes, 12 tins", {"answer": "yes", "count": 12}),
    ("yes_no_count", "Yes 1,200", {"answer": "yes", "count": 1200}),
    ("yes_no_count", "no", {"answer": "no", "count": 0}),
    ("yes_no_count", "yes", {"answer": "yes", "count": None}),
    ("yes_no_count", {"answer": "yes", "count": "8"}, {"answer": "yes", "count": 8}),
    ("yes_no_count", 5, {"answer": "yes", "count": 5}),
    ("yes_no_count", 0, {"answer": "no", "count": 0}),
    ("yes_no", True, "yes"),
    ("yes_no", " Nope. ", "no"),
    ("count", "14", 14),
    ("count", 3.0, 3),
    ("text", "  back room shelf ", "back room shelf"),
])
def test_answers_are_stored_in_one_form(answer_type, value, stored):
    assert questions.normalize(answer_type, value) == stored


@pytest.mark.parametrize("answer_type, value", [
    ("yes_no_count", "no, 4"), ("yes_no_count", "maybe"), ("yes_no", "perhaps"), ("count", -1), ("count", 2.5),
    ("count", True), ("count", "a few"), ("text", "   "), ("yes_no", None), ("count", ""),
])
def test_unusable_answers_are_refused(answer_type, value):
    with pytest.raises(QuestionError):
        questions.normalize(answer_type, value)


def test_answer_text_reads_like_the_wall():
    assert questions.answer_text("yes_no_count", {"answer": "yes", "count": 12}) == "yes, 12 tins"
    assert questions.answer_text("yes_no_count", {"answer": "yes", "count": 1}) == "yes, 1 tin"
    assert questions.answer_text("yes_no_count", {"answer": "no", "count": 0}) == "no"
    assert questions.answer_text("count", 3, "case") == "3 cases"
    assert questions.answer_text("text", "back room") == '"back room"'


PLANT_QUESTIONS = (("Which other batches ran with the same fault?", "text"),
                   ("How many tins of this batch did you ship to us?", "count"),
                   ("What must stores do with the tins: return them or destroy them?", "text"))


def test_spec_39_the_plants_three_answers_are_stored_against_its_attempt(api, db, rid):
    """Spec "Calling the plant": the three plant questions are data (tier 0
    only), the plant is asked them and no counts, and each answer is stored
    with the attempt that took it."""
    from tests.support import party_doc
    db.parties.insert_one(party_doc(rid, "maple-farm-plant", 0, type_="plant", tier=0, channel="sim",
                                    name="Maple Farm plant, Guelph", batch_code="B-4471"))
    ids = []
    for text, answer_type in PLANT_QUESTIONS:
        r = ask(api, rid, text, answer_type, tier=[0])
        assert r.status_code == 200, r.text
        assert r.json()["due"] == 1                                   # the plant, never a store
        ids.append(r.json()["question_id"])
    for sid in ("harbour-market", "queen-st-grocer", "lakeside-foods"):
        assert not party(db, rid, sid).get("open_questions"), sid
    plant = party(db, rid, "maple-farm-plant")
    variables = call_variables(db, plant)
    assert variables["ask_counts"] == "no" and variables["question_ids"] == ",".join(ids)
    assert call_variables(db, plant, take_counts=True)["ask_counts"] == "no"

    att = dial(db, rid, "maple-farm-plant")
    replies = [answer(api, att, ids[0], "B-4472"), answer(api, att, ids[1], "2400"), answer(api, att, ids[2], "return")]
    assert [r["accepted"] for r in replies] == [True, True, True]
    assert replies[-1]["message"] == "Recorded. No more questions."
    stored = party(db, rid, "maple-farm-plant")["answers"]
    assert {q: (a["value"], a["attempt_id"]) for q, a in stored.items()} == {
        ids[0]: ("B-4472", att["_id"]), ids[1]: (2400, att["_id"]), ids[2]: ("return", att["_id"])}
    assert texts(db, "answer_recorded")[1] == f"Maple Farm plant, Guelph answered {ids[1]}: 2400 tins"
    assert db.claims.count_documents({"party_id": "maple-farm-plant"}) == 0
