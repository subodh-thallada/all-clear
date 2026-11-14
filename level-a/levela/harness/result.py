"""Results (V2 §6): one JSON object per run, read from the facts and checked
against them.

    {outcome, evidence: [ids], next: {action, at} | None, note}

`outcome` is one of OUTCOMES. A result counts only if the facts in Atlas
support it: every evidence id exists and belongs to this run (an attempt or
an operation carrying the run's id, or a claim on the run's attempt), and

    claim_accepted       an accepted claim on this run's attempt
    claim_rejected       a rejected claim on this run's attempt
    no_answer            this run's attempt ended without counts
    callback_requested   a callback hour on the run's attempt, or recorded by the run
    form_sent            a send_form_link operation of this run, sent or verified
    answer_recorded      an answer on the party carrying this run's attempt
    escalated            an escalate_rep operation of this run, or the party
                         handed to a person by this run
    hold_verified        a verified place_hold / confirm_hold operation of this run
    accounts_read        a verified read_accounts / read_inventory operation of this run
    failed               always (it claims nothing)

Anything else is `unsupported_result`. `derive()` builds the result from the
same facts, so a correct run's own result always passes; `check()` is what
stops a decider (or a saboteur) that claims success it did not have.

A run that ends in a failure class gets one `kind: "failure"` episode, so the
party's next brief says what went wrong last time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from ..clock import fmt


OUTCOMES = ("claim_accepted", "claim_rejected", "no_answer", "callback_requested", "form_sent", "answer_recorded",
            "escalated", "failed", "hold_verified", "accounts_read")
FAILURE_CLASSES = ("no_answer", "claim_rejected", "contract_failed", "budget_exceeded", "unsupported_result",
                   "gate_refused")
FINAL_ATTEMPT = ("completed", "no_answer", "failed", "abandoned")
HOLD_OPS = ("place_hold", "confirm_hold")
READ_OPS = ("read_accounts", "read_inventory")
UNSETTLED_OPS = ("intent", "sent", "needs_reverify", "failed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make(outcome: str, evidence=(), *, next_action: str | None = None, next_at: float | None = None,
         note: str = "") -> dict:
    return {"outcome": outcome, "evidence": [str(e) for e in evidence if e],
            "next": {"action": next_action, "at": next_at} if (next_action or next_at is not None) else None,
            "note": note}


def problems(result) -> list[str]:
    """Schema check: free text is not a result."""
    if not isinstance(result, dict):
        return ["the result is not an object"]
    out = []
    if result.get("outcome") not in OUTCOMES:
        out.append(f"outcome {result.get('outcome')!r} is not one of {', '.join(OUTCOMES)}")
    evidence = result.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        out.append("evidence must be a list of ids")
    nxt = result.get("next")
    if nxt is not None and (not isinstance(nxt, dict) or set(nxt) - {"action", "at"}):
        out.append("next must be {action, at} or null")
    if not isinstance(result.get("note", ""), str):
        out.append("note must be text")
    extra = set(result) - {"outcome", "evidence", "next", "note"}
    if extra:
        out.append(f"unexpected fields: {', '.join(sorted(extra))}")
    return out


@dataclass
class Facts:
    """What Atlas holds about one run: its attempts, their claims, its
    operations and the party as it is now. Read once, used by derive and check."""
    run_id: str
    attempts: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    ops: list = field(default_factory=list)
    party: dict = field(default_factory=dict)
    run_doc: dict = field(default_factory=dict)

    @property
    def attempt_ids(self) -> set:
        return {a["_id"] for a in self.attempts}

    def accepted(self) -> list:
        return [c for c in self.claims if c.get("status") == "accepted"]

    def rejected(self) -> list:
        return [c for c in self.claims if c.get("status") == "rejected"]

    def answers(self) -> list:
        ids = self.attempt_ids
        return [qid for qid, a in (self.party.get("answers") or {}).items()
                if isinstance(a, dict) and a.get("attempt_id") in ids]

    def ops_of(self, kinds, states=None) -> list:
        return [o for o in self.ops if o["kind"] in kinds and (states is None or o["state"] in states)]

    def nothing_done(self) -> bool:
        """The run left no trace in the world: no attempt, no operation, no
        callback, no escalation (its decider decided nothing was owed)."""
        return (not self.attempts and not self.ops and self.run_doc.get("callback_at") is None
                and self.party.get("escalated_run") != self.run_id)


def snapshot(db, run) -> Facts:
    """Four reads: the run, its attempts and their claims, its operations, its party."""
    attempts = list(db.attempts.find({"run_id": run.id}).sort("seq", 1))
    claims = list(db.claims.find({"attempt_id": {"$in": [a["_id"] for a in attempts]}})) if attempts else []
    ops = list(db.operations.find({"run_id": run.id}))
    party = (db.parties.find_one({"recall_id": run.recall_id, "party_id": run.party_id}) or {}) if run.party_id else {}
    run_doc = db.task_runs.find_one({"_id": run.id}) or {}
    return Facts(run_id=run.id, attempts=attempts, claims=claims, ops=ops, party=party, run_doc=run_doc)


def derive(facts: Facts) -> dict:
    """The run's result, read from the facts (never from what a decider said)."""
    if facts.attempts:
        return _from_call(facts, facts.attempts[-1])
    form = facts.ops_of(("send_form_link",), ("sent", "verified"))
    if form:
        return make("form_sent", [form[-1]["_id"]], note="form link delivered")
    rep = facts.ops_of(("escalate_rep",))
    if rep:
        return make("escalated", [rep[-1]["_id"]], note="escalated to the distributor's rep")
    if facts.party.get("escalated_run") == facts.run_id:
        return make("escalated", note="handed to a person")
    reads = facts.ops_of(READ_OPS, ("verified",))
    if reads:
        return make("accounts_read", [o["_id"] for o in reads], note="order system read back")
    holds = facts.ops_of(HOLD_OPS, ("verified",))
    if holds:
        return make("hold_verified", [o["_id"] for o in holds], note="hold shown by a fresh order system read")
    if facts.run_doc.get("callback_at") is not None:
        return make("callback_requested", note=f"callback recorded for {fmt(facts.run_doc['callback_at'])}")
    unsettled = facts.ops_of(HOLD_OPS + READ_OPS, UNSETTLED_OPS)
    if unsettled:
        states = ", ".join(f"{o['kind']} {o['state']}" for o in unsettled)
        return make("failed", [o["_id"] for o in unsettled], note=f"order system did not confirm: {states}")
    return make("failed", note="no effect recorded for this run")


def _from_call(facts: Facts, attempt: dict) -> dict:
    aid = attempt["_id"]
    accepted, rejected = facts.accepted(), facts.rejected()
    if accepted:
        return make("claim_accepted", [aid, str(accepted[-1]["_id"])], note="counts accepted by Atlas")
    answers = facts.answers()
    if answers:
        return make("answer_recorded", [aid], note=f"answered {', '.join(sorted(answers))}")
    if attempt.get("callback_at") is not None:
        return make("callback_requested", [aid], note=f"asked to be called back at {fmt(attempt['callback_at'])}")
    if rejected:
        return make("claim_rejected", [aid, str(rejected[-1]["_id"])],
                    note=f"rejected: {rejected[-1].get('code') or rejected[-1].get('reject_reason')}")
    if attempt["state"] in FINAL_ATTEMPT:
        return make("no_answer", [aid], note=f"call ended {attempt['state']} without counts")
    return make("failed", [aid], note=f"call still {attempt['state']} when the run ended")


def check(facts: Facts, result) -> tuple[bool, str]:
    """(supported, reason). The reason is the first fact that is missing."""
    issues = problems(result)
    if issues:
        return False, "; ".join(issues)
    for eid in result.get("evidence", []):
        if not _belongs(facts, eid):
            return False, f"evidence {eid} does not exist or is not this run's"
    outcome = result["outcome"]
    ok = _SUPPORT[outcome](facts)
    return (True, "supported") if ok else (False, f"{outcome} is not supported by the facts of run {facts.run_id}")


def _belongs(facts: Facts, eid: str) -> bool:
    if eid in facts.attempt_ids or any(o["_id"] == eid for o in facts.ops):
        return True
    return any(str(c["_id"]) == eid or (ObjectId.is_valid(eid) and c["_id"] == ObjectId(eid)) for c in facts.claims)


_SUPPORT = {
    "claim_accepted": lambda f: bool(f.accepted()),
    "claim_rejected": lambda f: bool(f.rejected()),
    "no_answer": lambda f: any(a["state"] in FINAL_ATTEMPT for a in f.attempts),
    "callback_requested": lambda f: (any(a.get("callback_at") is not None for a in f.attempts)
                                     or f.run_doc.get("callback_at") is not None),
    "form_sent": lambda f: bool(f.ops_of(("send_form_link",), ("sent", "verified"))),
    "answer_recorded": lambda f: bool(f.answers()),
    "escalated": lambda f: bool(f.ops_of(("escalate_rep",))) or f.party.get("escalated_run") == f.run_id,
    "hold_verified": lambda f: bool(f.ops_of(HOLD_OPS, ("verified",))),
    "accounts_read": lambda f: bool(f.ops_of(READ_OPS, ("verified",))),
    "failed": lambda f: True,
}


def failure_class(result: dict, run_kind: str) -> str | None:
    """The failure class a supported result implies (None for success). A
    order system that did not confirm broke its contract; a party that could not be
    reached any other way (an undelivered form link) was not answered."""
    outcome = result.get("outcome")
    if outcome == "failed":
        return "contract_failed" if run_kind == "order_system_sync" else "no_answer"
    return {"no_answer": "no_answer", "claim_rejected": "claim_rejected"}.get(outcome)


def callback_at(facts: Facts) -> float | None:
    """The callback hour the run's facts carry (the party's own request wins)."""
    for attempt in reversed(facts.attempts):
        if attempt.get("callback_at") is not None:
            return float(attempt["callback_at"])
    at = facts.run_doc.get("callback_at")
    return float(at) if at is not None else None


def save_failure_episode(db, run, failure_cls: str, note: str, *, sim_now: float) -> dict | None:
    """One `kind: "failure"` episode per failed run (idempotent: `_id` is
    derived from the run). No embedding: failure episodes are read by party
    for the brief, never searched by similarity."""
    if failure_cls not in FAILURE_CLASSES:
        raise ValueError(f"unknown failure class {failure_cls!r}")
    doc = {"_id": f"ep_{run.id}", "kind": "failure", "failure_class": failure_cls, "recall_id": run.recall_id,
           "party_id": run.party_id, "run_id": run.id, "worker": run.worker, "run_kind": run.kind,
           "summary": f"{note} ({fmt(sim_now)})", "tags": [failure_cls], "entities": {}, "valid": True,
           "sim": round(float(sim_now), 4), "created_at": utcnow()}
    try:
        db.episodes.insert_one(doc)
    except DuplicateKeyError:
        return None
    return doc
