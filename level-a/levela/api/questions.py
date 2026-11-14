"""Questions as data (spec "Questions as data", V2 §10, test 32).

A new question for every party is one document and one update:

  questions  {_id: "<recall_id>:q-02", recall_id, question_id: "q-02", text, answer_type,
              applies_to: {tier: [1, 2, 3]}, asked_at (sim h), created_at, by}
  parties     open_questions: ["q-02"]        answers: {"q-02": {value, at (sim h), attempt_id}}

Question ids count per recall ("q-01", "q-02", ...); the document id carries
the recall so two recalls can each have a q-01.

This module owns the schema. Everything that reads a question goes through
it, so the ids on parties.open_questions always resolve: the admin endpoint
and the planner's add_question tool insert through insert_question(), the
harness brief and the call's dynamic variables read through question_docs()
and call_variables(), the simulator looks up the questions a call carried
through question_docs(), and /tools/submit_answer finds the question with
get_question(). A party is asked at most PER_CALL questions on one call (the
brief shows the same ones); a party with more is due again for the rest.

Adding a question gives it to every matching party that has neither the
question open nor an answer to it, and makes those parties due now (their
next_action_at becomes the simulated now, or stays earlier if it already was).
Parties that answered are never touched. The same fan-out runs again once per
simulated hour (api.scheduler), so a party found after the question was
added is asked it too. Distributors (channel "order_system") have no phone and no
question endpoint, and parties handed to a person are not dialled, so neither
is given questions.

A party whose counts are already on the books is asked the question only:
call_variables() says ask_counts "no" and lists the open questions. Those are
the dynamic variables a call to the party carries, and submit_answer (the
voice agent's tool) records each answer through record_answer().
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .. import feed

ANSWER_TYPES = ("yes_no_count", "yes_no", "count", "text")
TIERS = (1, 2, 3)
MAX_TEXT = 300
ID_ATTEMPTS = 8
PER_CALL = 3            # questions asked on one call: the call stays short and the brief stays fixed-size

# How the agent is told to collect each kind of answer.
HOW_TO_ANSWER = {
    "yes_no_count": "yes or no, and how many tins",
    "yes_no": "yes or no",
    "count": "a number of tins",
    "text": "a few words",
}
_YES = {"yes", "y", "yeah", "yep", "true"}
_NO = {"no", "n", "nope", "false", "none"}
_YES_NO_COUNT = re.compile(r"^\s*(yes|yeah|yep|y|no|nope|n)\b\W*(\d[\d,]*)?", re.IGNORECASE)


class QuestionError(ValueError):
    """A question or an answer the API refuses. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def doc_id(recall_id: str, question_id: str) -> str:
    return f"{recall_id}:{question_id}"


def tiers_of(applies_to: dict | None) -> list[int]:
    """applies_to {"tier": [...]} -> sorted tiers; empty means every tier."""
    applies_to = applies_to or {}
    unknown = set(applies_to) - {"tier"}
    if unknown:
        raise QuestionError("bad_applies_to", f"applies_to accepts tier, not {sorted(unknown)}")
    raw = applies_to.get("tier") or []
    if not isinstance(raw, list) or any(isinstance(v, bool) or v not in TIERS for v in raw):
        raise QuestionError("bad_applies_to", f"applies_to.tier must list tiers from {list(TIERS)}")
    return sorted(set(raw))


# ---- adding a question --------------------------------------------------------

def add_question(db, *, recall_id: str, text: str, answer_type: str, applies_to: dict | None,
                 sim_now: float, by: str = "admin", now: datetime | None = None) -> dict:
    """Insert the next question of the recall and fan it out. Returns the
    question document plus `due`, the number of parties it made due."""
    doc = insert_question(db, recall_id=recall_id, text=text, answer_type=answer_type, applies_to=applies_to,
                          sim_now=sim_now, by=by, now=now)
    due = fan_out(db, doc, sim_now)
    feed.emit(db, "question_added", f"question {doc['question_id']} added: {due} parties due", recall_id=recall_id,
              system="planner", sim=sim_now, source="admin" if by == "admin" else "worker",
              detail={"question_id": doc["question_id"], "text": doc["text"], "answer_type": doc["answer_type"],
                      "tiers": doc["applies_to"]["tier"], "due": due, "by": by})
    return {**doc, "due": due}


def insert_question(db, *, recall_id: str, text: str, answer_type: str, applies_to: dict | None,
                    sim_now: float, by: str, now: datetime | None = None, session=None) -> dict:
    """Check a new question and insert it as the recall's next one (not yet
    fanned out). The one writer of question documents: the admin endpoint
    and the planner's add_question tool (inside the gate's transaction, via
    `session`) both come here, so every reader finds the same shape."""
    text = normalize_text(text)
    if not text or len(text) > MAX_TEXT:
        raise QuestionError("bad_text", f"a question needs 1 to {MAX_TEXT} characters of text")
    if answer_type not in ANSWER_TYPES:
        raise QuestionError("bad_answer_type", f"answer_type must be one of {list(ANSWER_TYPES)}")
    tiers = tiers_of(applies_to)
    return _insert_next(db, {
        "recall_id": recall_id, "text": text, "answer_type": answer_type, "applies_to": {"tier": tiers},
        "asked_at": round(sim_now, 4), "created_at": now or datetime.now(timezone.utc), "by": by,
    }, session=session)


def normalize_text(text) -> str:
    """A question as it is stored and read aloud: one line, single spaces."""
    return " ".join(str(text or "").split())


def find_same(db, recall_id: str, text: str, session=None) -> dict | None:
    """The recall's question with this text, case and spacing aside. The planner
    asking the same question twice gets the first one back, not a second
    number. A recall holds a handful of questions, so they are compared here."""
    key = normalize_text(text).lower()
    for question in db.questions.find({"recall_id": recall_id}, session=session).sort("question_id", 1):
        if normalize_text(question.get("text")).lower() == key:
            return question
    return None


def _insert_next(db, fields: dict, session=None) -> dict:
    """Number the question after the recall's existing ones. Two questions
    added at once may pick the same number; the loser takes the next one.
    Inside a caller's transaction a duplicate key has already aborted it, so
    the error is raised for the caller to start its transaction again (the
    gate does, once)."""
    for _ in range(1 if session is not None else ID_ATTEMPTS):
        n = db.questions.count_documents({"recall_id": fields["recall_id"]}, session=session)
        qid = f"q-{n + 1:02d}"
        doc = {"_id": doc_id(fields["recall_id"], qid), "question_id": qid, **fields}
        try:
            db.questions.insert_one(doc, session=session)
            return doc
        except DuplicateKeyError:
            if session is not None:
                raise
            continue
    raise QuestionError("busy", "could not number the question: too many added at once, try again")


def _askable(question: dict) -> dict:
    """Parties of the recall the question applies to that have neither it open nor an answer."""
    qid = question["question_id"]
    filt = {"recall_id": question["recall_id"], "channel": {"$ne": "order_system"}, "escalation": {"$ne": "human"},
            "open_questions": {"$ne": qid}, f"answers.{qid}": {"$exists": False}}
    tiers = (question.get("applies_to") or {}).get("tier") or []
    if tiers:
        filt["tier"] = {"$in": tiers}
    return filt


def fan_out(db, question: dict, sim_now: float, session=None) -> int:
    """Give one question to every party it applies to that has not got it yet,
    due now. Idempotent: a party that has it open or answered is not matched."""
    qid = question["question_id"]
    res = db.parties.update_many(_askable(question), [{"$set": {
        "open_questions": {"$concatArrays": [{"$ifNull": ["$open_questions", []]}, [{"$literal": qid}]]},
        "next_action_at": {"$min": [{"$ifNull": ["$next_action_at", sim_now]}, sim_now]},
    }}], session=session)
    return res.modified_count


def fan_out_all(db, recall_id: str, sim_now: float) -> dict[str, int]:
    """Re-run the fan-out of every question of a recall (parties found since
    a question was added get it). Returns {question_id: parties newly given it}."""
    given = {}
    for question in db.questions.find({"recall_id": recall_id}).sort("question_id", 1):
        n = fan_out(db, question, sim_now)
        if n:
            given[question["question_id"]] = n
            feed.emit(db, "question_added", f"question {question['question_id']} given to {n} "
                      f"{'party' if n == 1 else 'parties'} found since it was added", recall_id=recall_id,
                      system="planner", sim=sim_now, detail={"question_id": question["question_id"], "due": n})
    return given


# ---- reading ------------------------------------------------------------------

def get_question(db, recall_id: str, question_id: str) -> dict | None:
    return db.questions.find_one({"_id": doc_id(recall_id, question_id)})


def question_docs(db, recall_id: str, question_ids, session=None) -> list[dict]:
    """The recall's questions with these ids ("q-01", as parties.open_questions
    and a call's question_ids hold them), in the order given. An id with no
    question document is left out rather than asked with no text."""
    ids = [qid for qid in dict.fromkeys(question_ids or ()) if qid]
    if not ids:
        return []
    found = {q["question_id"]: q for q in db.questions.find(
        {"_id": {"$in": [doc_id(recall_id, qid) for qid in ids]}},
        {"question_id": 1, "text": 1, "answer_type": 1}, session=session)}
    return [found[qid] for qid in ids if qid in found]


def asked_on_next_call(db, party: dict, session=None) -> list[dict]:
    """The open questions the party's next call asks: the first PER_CALL that
    exist, in the order they were added."""
    return question_docs(db, party["recall_id"], party.get("open_questions") or [], session=session)[:PER_CALL]


def list_questions(db, recall_id: str) -> list[dict]:
    """The recall's questions with how many parties have each open and answered."""
    rows = list(db.questions.find({"recall_id": recall_id}).sort("question_id", 1))
    if not rows:
        return rows
    parties = list(db.parties.find({"recall_id": recall_id}, {"open_questions": 1, "answers": 1}))
    for q in rows:
        qid = q["question_id"]
        q["open"] = sum(qid in (s.get("open_questions") or []) for s in parties)
        q["answered"] = sum(qid in (s.get("answers") or {}) for s in parties)
    return rows


def counts_on_books(party: dict) -> bool:
    """Every tin at the party is explained by its own accepted claim."""
    return party.get("state") in ("claimed", "reconciled") and party.get("unresolved") == 0


def ask_counts(party: dict) -> bool:
    """A party whose counts are on the books is asked its open questions only."""
    return not counts_on_books(party)


def call_variables(db, party: dict, *, take_counts: bool | None = None, session=None) -> dict:
    """The question part of a call's dynamic variables (all strings, as
    ElevenLabs requires), in the form agent/prompt.md reads them:

        questions     "q-02: Did you also receive batch L-4480? (answer: yes or no, and how many tins)."
        question_ids  "q-02"
        ask_counts    "yes" | "no"

    The questions are the ones asked_on_next_call() picks. `take_counts` is
    the caller's decision when it has one (the harness: a question-only run
    takes no counts); otherwise the party's own books decide."""
    asked = asked_on_next_call(db, party, session=session)
    counts = ask_counts(party) if take_counts is None else bool(take_counts)
    return {
        "questions": " ".join(f"{q['question_id']}: {q['text']} "
                              f"(answer: {HOW_TO_ANSWER.get(q.get('answer_type'), HOW_TO_ANSWER['text'])})."
                              for q in asked),
        "question_ids": ",".join(q["question_id"] for q in asked),
        "ask_counts": "yes" if counts else "no",
    }


# ---- answers --------------------------------------------------------------------

def _yes_no(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        word = value.strip().lower().rstrip(".!")
        if word in _YES:
            return "yes"
        if word in _NO:
            return "no"
    raise QuestionError("bad_answer", "the answer must be yes or no")


def _count(value) -> int:
    if isinstance(value, bool):
        raise QuestionError("bad_answer", "the count must be a whole number of tins")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str) and value.strip().replace(",", "").isdigit():
        return int(value.strip().replace(",", ""))
    raise QuestionError("bad_answer", "the count must be a whole number of tins")


def _yes_no_count(value) -> dict:
    """{"answer": "yes"|"no", "count": n|None} from a dict, "yes, 12 tins", "no", or a bare count."""
    if isinstance(value, dict):
        answer = _yes_no(value.get("answer"))
        count = None if value.get("count") in (None, "") else _count(value.get("count"))
    elif isinstance(value, str) and (m := _YES_NO_COUNT.match(value)):
        answer = _yes_no(m.group(1))
        count = _count(m.group(2)) if m.group(2) else None
    else:
        count = _count(value)
        answer = "yes" if count else "no"
    if answer == "no" and count:
        raise QuestionError("bad_answer", "a no cannot come with a count; read the answer back and ask again")
    return {"answer": answer, "count": 0 if answer == "no" else count}


def normalize(answer_type: str, value):
    """The answer in its stored form, or QuestionError the agent can act on."""
    if value is None or value == "":
        raise QuestionError("bad_answer", "no answer was given")
    if answer_type == "yes_no_count":
        return _yes_no_count(value)
    if answer_type == "yes_no":
        return _yes_no(value)
    if answer_type == "count":
        return _count(value)
    text = str(value).strip()
    if not text or len(text) > MAX_TEXT:
        raise QuestionError("bad_answer", f"the answer must be 1 to {MAX_TEXT} characters")
    return text


def _panels(n: int, unit: str) -> str:
    plural = f"{unit}es" if unit.endswith(("s", "x", "ch", "sh")) else f"{unit}s"
    return f"{n} {unit if n == 1 else plural}"


def answer_text(answer_type: str, value, unit: str = "tin") -> str:
    """'yes, 12 tins' - the answer as the screen shows it."""
    if answer_type == "yes_no_count":
        if value["answer"] == "no":
            return "no"
        return "yes" if value["count"] is None else f"yes, {_panels(value['count'], unit)}"
    if answer_type == "count":
        return _panels(value, unit)
    if answer_type == "text":
        return f'"{value}"'
    return str(value)


def record_answer(db, *, party: dict, attempt_id: str, question: dict, value, sim_now: float) -> dict:
    """Store one answer: answers.<qid> = {value, at, attempt_id} and the id
    leaves open_questions, in one document update. The same answer again is a
    duplicate (a redelivered tool call); a different one corrects it.

    Returns {value, duplicate, corrected, questions_left}."""
    qid = question["question_id"]
    stored = normalize(question["answer_type"], value)
    prior = (party.get("answers") or {}).get(qid)
    if prior is None and qid not in (party.get("open_questions") or []):
        raise QuestionError("not_asked", f"{qid} is not one of this party's questions")
    if prior is not None and prior.get("value") == stored:
        return {"value": stored, "duplicate": True, "corrected": False,
                "questions_left": len(party.get("open_questions") or [])}
    after = db.parties.find_one_and_update(
        {"_id": party["_id"]},
        {"$set": {f"answers.{qid}": {"value": stored, "at": round(sim_now, 4), "attempt_id": attempt_id}},
         "$pull": {"open_questions": qid}},
        projection={"open_questions": 1}, return_document=ReturnDocument.AFTER)
    return {"value": stored, "duplicate": False, "corrected": prior is not None,
            "questions_left": len((after or {}).get("open_questions") or [])}
