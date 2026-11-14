"""Audit and run control (spec "Borrowed from Agents You Can Audit", tests 40 and 41).

    POST /admin/pause               runs.state = paused: no worker claims new work
    POST /admin/resume              runs.state = running again
    GET  /admin/audit/{party_id}    one party's history, in order

Pause and resume write only the run document (levela.runs.set_state). The
claim requires `running` (levela.orchestrator.claim.claim_next), so after a
pause no worker takes new work; a call already in flight finishes and is
recorded by the post-call webhook as usual. Nothing is lost, because all
state is in Atlas.

The audit is one aggregation, started on `traces` and joined with
`$unionWith` over `task_runs`, `attempts`, `operations` and `claims` (plus the
feed's Atlas refusals, which leave no other document: a write Atlas refused
was never stored). Each row says who acted (worker, task run, lease epoch),
under which approval (the recall's `approved_by`, its source and source
signal), what the gate decided (allowed or the rule that refused) and what
Atlas accepted or refused (claim status and reason, operation and attempt
states). A refusal of the saboteur is a gate row with `saboteur` set.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import feed, runs
from .deps import jsonable, require_admin

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# Rows that share a timestamp are ordered as the work happens: the run opens,
# the decider decides, the gate admits the tool, the attempt or operation is
# written, the claim lands, the run's result is traced.
ORDER = {"task_run": 0, "decision": 1, "gate": 2, "attempt": 3, "operation": 4, "claim": 5, "atlas": 6,
         "result": 7}


class RunStateIn(BaseModel):
    recall_id: str | None = None
    reason: str | None = None
    by: str | None = None


# ------------------------------------------------------------------ pause

def _target_recalls(db, recall_id: str | None) -> list[str]:
    if recall_id is not None:
        if db.runs.find_one({"_id": runs.run_id(recall_id)}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail=f"no run for recall {recall_id}")
        return [recall_id]
    open_ids = [r["_id"] for r in db.recalls.find({"status": "open"}, {"_id": 1})]
    return [r["recall_id"] for r in db.runs.find({"recall_id": {"$in": open_ids}}, {"recall_id": 1})]


def _in_flight(db, recall_id: str) -> dict:
    """What a pause lets finish: live attempts and order-system operations not yet settled."""
    return {"attempts": db.attempts.count_documents({"recall_id": recall_id,
                                                     "state": {"$in": ["intent", "dialed", "connected"]}}),
            "operations": db.operations.count_documents({"recall_id": recall_id, "state": {"$in": ["intent", "sent"]}}),
            "leased": db.parties.count_documents({"recall_id": recall_id, "lease_owner": {"$ne": None},
                                                  "$expr": {"$gt": ["$lease_until", "$$NOW"]}})}


def set_run_state(db, state: str, body: RunStateIn) -> dict:
    out = []
    for rid in _target_recalls(db, body.recall_id):
        doc = runs.set_state(db, rid, state, by=body.by or "operator", reason=body.reason)
        flight = _in_flight(db, rid)
        if state == runs.PAUSED:
            text = (f"PAUSED by {body.by or 'the operator'}: no worker takes new work. In flight: "
                    f"{flight['attempts']} call{'s' if flight['attempts'] != 1 else ''}, "
                    f"{flight['operations']} order-system operation{'s' if flight['operations'] != 1 else ''}; "
                    "they finish and are recorded")
        else:
            text = f"RESUMED by {body.by or 'the operator'}: workers claim due work again"
        feed.emit(db, f"run_{state}", text + (f" ({body.reason})" if body.reason else ""), recall_id=rid,
                  level="warn" if state == runs.PAUSED else "good", source="api", system="run",
                  detail={"state": state, "in_flight": flight})
        out.append({"recall_id": rid, "state": (doc or {}).get("state"), "in_flight": flight})
    return {"ok": True, "state": state, "runs": out}


@router.post("/pause")
def pause(request: Request, body: RunStateIn | None = None) -> dict:
    """Spec test 40: no worker claims new work; calls in flight finish."""
    return jsonable(set_run_state(request.app.state.db, runs.PAUSED, body or RunStateIn()))


@router.post("/resume")
def resume(request: Request, body: RunStateIn | None = None) -> dict:
    return jsonable(set_run_state(request.app.state.db, runs.RUNNING, body or RunStateIn()))


# ------------------------------------------------------------------ audit

def _match(party_id: str, recall_id: str | None) -> dict:
    return {"party_id": party_id, **({"recall_id": recall_id} if recall_id else {})}


def audit_pipeline(party_id: str, recall_id: str | None = None) -> list[dict]:
    """The one aggregation behind GET /admin/audit/{party_id}, run on `traces`."""
    m = _match(party_id, recall_id)
    traces = [
        {"$match": m},
        {"$project": {
            "_id": 0, "at": "$at", "sim": "$sim", "recall_id": 1, "party_id": 1, "worker": 1, "run_id": 1,
            "seq": 1, "source": {"$literal": "traces"},
            "what": {"$switch": {"branches": [{"case": {"$eq": ["$kind", "tool"]}, "then": "gate"},
                                              {"case": {"$eq": ["$kind", "result"]}, "then": "result"}],
                                 "default": "decision"}},
            "tool": 1, "args": 1,
            "gate": {"$cond": [{"$eq": ["$kind", "tool"]},
                               {"allowed": "$decision.allowed", "rule": "$decision.rule",
                                "reason": "$decision.reason", "duplicate": "$decision.duplicate",
                                "evidence_id": "$decision.evidence_id"}, None]},
            "output": {"$cond": [{"$eq": ["$kind", "tool"]}, None, "$output"]},
        }},
    ]
    task_runs = [
        {"$match": m},
        {"$project": {
            "_id": 0, "at": "$started_at", "sim": "$started_sim", "recall_id": 1, "party_id": 1, "worker": 1,
            "run_id": "$_id", "source": {"$literal": "task_runs"}, "what": {"$literal": "task_run"},
            "run": {"kind": "$kind", "status": "$status", "tools": "$tools", "lease_epoch": "$lease_epoch",
                    "failure_class": "$failure_class", "outcome": "$result.outcome", "used": "$used",
                    "ended_at": "$ended_at", "ended_sim": "$ended_sim"},
        }},
    ]
    attempts = [
        {"$match": m},
        {"$project": {
            "_id": 0, "at": "$intent_at", "sim": "$intent_sim", "recall_id": 1, "party_id": 1,
            "worker": "$owner.worker", "run_id": 1, "source": {"$literal": "attempts"}, "what": {"$literal": "attempt"},
            "attempt": {"attempt_id": "$_id", "seq": "$seq", "channel": "$channel", "purpose": "$purpose",
                        "state": "$state", "call_sid": "$call_sid", "conversation_id": "$conversation_id",
                        "idempotency_key": "$idempotency_key", "lease_epoch": "$owner.epoch",
                        "ended_at": "$ended_at", "outcome_reason": "$outcome_reason"},
        }},
    ]
    operations = [
        {"$match": m},
        {"$project": {
            "_id": 0, "at": "$created_at", "sim": "$intent_at", "recall_id": 1, "party_id": 1, "worker": "$worker",
            "run_id": 1, "source": {"$literal": "operations"}, "what": {"$literal": "operation"},
            "operation": {"op_id": "$_id", "kind": "$kind", "order_system": "$order_system", "state": "$state",
                          "idempotency_key": "$idempotency_key", "adapter_version": "$adapter_version",
                          "sent_at": "$sent_at", "readback_at": "$readback_at",
                          "readback_value": "$readback_value", "lease_epoch": "$lease_epoch"},
        }},
    ]
    claims = [
        {"$match": m},
        {"$project": {
            "_id": 0, "at": "$created_at", "recall_id": 1, "party_id": 1, "source": {"$literal": "claims"},
            "what": {"$literal": "claim"}, "attempt_id": 1,
            "atlas": {"claim_id": "$_id", "status": "$status", "reason": "$reject_reason", "code": "$code",
                      "values": "$values", "from": "$source", "atlas_error": "$atlas_error"},
        }},
    ]
    refused = [
        {"$match": {**m, "kind": "atlas_rejected"}},
        {"$project": {
            "_id": 0, "at": "$at", "sim": "$sim", "recall_id": 1, "party_id": 1, "source": {"$literal": "feed"},
            "what": {"$literal": "atlas"}, "atlas": {"status": "rejected", "reason": "$text", "detail": "$detail"},
        }},
    ]
    return [
        *traces,
        {"$unionWith": {"coll": "task_runs", "pipeline": task_runs}},
        {"$unionWith": {"coll": "attempts", "pipeline": attempts}},
        {"$unionWith": {"coll": "operations", "pipeline": operations}},
        {"$unionWith": {"coll": "claims", "pipeline": claims}},
        {"$unionWith": {"coll": "feed", "pipeline": refused}},
        # Who acted: the task run behind every row that names one.
        {"$lookup": {"from": "task_runs", "localField": "run_id", "foreignField": "_id", "as": "_run",
                     "pipeline": [{"$project": {"kind": 1, "lease_epoch": 1, "saboteur": 1, "worker": 1}}]}},
        # Under which approval: the recall, and the signal a person approved.
        {"$lookup": {"from": "recalls", "localField": "recall_id", "foreignField": "_id", "as": "_recall",
                     "pipeline": [{"$project": {"approved_by": 1, "approved_at": 1, "source": 1,
                                                "source_signal": 1}}]}},
        {"$set": {"_run": {"$first": "$_run"}, "_recall": {"$first": "$_recall"},
                  "_order": {"$switch": {"branches": [{"case": {"$eq": ["$what", k]}, "then": v}
                                                      for k, v in ORDER.items()], "default": 9}}}},
        {"$set": {
            "worker": {"$ifNull": ["$worker", "$_run.worker"]},
            "task_run": {"$cond": [{"$ifNull": ["$run_id", False]},
                                   {"run_id": "$run_id", "kind": "$_run.kind", "lease_epoch": "$_run.lease_epoch"},
                                   None]},
            "saboteur": {"$ifNull": ["$_run.saboteur", None]},
            "approval": {"approved_by": "$_recall.approved_by", "approved_at": "$_recall.approved_at",
                         "source": "$_recall.source", "source_signal": "$_recall.source_signal"},
        }},
        {"$sort": {"at": 1, "_order": 1, "seq": 1}},
        {"$unset": ["_run", "_recall", "_order"]},
    ]


def audit(db, party_id: str, recall_id: str | None = None) -> list[dict]:
    """The party's history in order (spec test 41)."""
    return list(db.traces.aggregate(audit_pipeline(party_id, recall_id)))


def _summary(rows: list[dict]) -> dict:
    gate_rows = [r for r in rows if r.get("what") == "gate"]
    return {"rows": len(rows),
            "gate_refusals": sum(1 for r in gate_rows if not (r.get("gate") or {}).get("allowed")),
            "saboteur_refusals": sum(1 for r in gate_rows if r.get("saboteur") and not (r.get("gate") or {}).get("allowed")),
            "claims_accepted": sum(1 for r in rows if r.get("what") == "claim"
                                   and (r.get("atlas") or {}).get("status") == "accepted"),
            "claims_rejected": sum(1 for r in rows if r.get("what") == "claim"
                                   and (r.get("atlas") or {}).get("status") == "rejected"),
            "workers": sorted({r["worker"] for r in rows if r.get("worker")})}


@router.get("/audit/{party_id}")
def audit_party(party_id: str, request: Request, recall_id: str | None = None) -> dict:
    db = request.app.state.db
    rows = audit(db, party_id, recall_id)
    if not rows and db.parties.find_one(_match(party_id, recall_id), {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail=f"no party {party_id}")
    return jsonable({"party_id": party_id, "recall_id": recall_id, "summary": _summary(rows), "history": rows})
