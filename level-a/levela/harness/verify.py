"""Verification (V2 §6): the model that acted never decides whether the
action worked. Each effect has one fact that proves it, read here:

    phone call     the post-call webhook completed the attempt AND Atlas
                   accepted a claim carrying the attempt id (both written by the API)
    order system hold    a fresh order system read showed it: the operation is `verified`,
                   which the operations validator allows only with
                   readback_at and readback_value (written by the read-back
                   code in levela.systems, through mark_verified)
    form link      a submission arrived with the party's token: the operation
                   is verified by the API, or a form claim was accepted after
                   the link was sent
    answer         submit_answer stored it under answers.<qid> with the attempt
    adapter        its canary reproduced the ledger's corroborated count

`mark_verified` is the one sanctioned way to set `verified`: it carries the
read-back, so Atlas accepts it; a write without one is refused with
validation error 121 (test 28). The gate never calls it.
"""
from __future__ import annotations

UNVERIFIED_OPS = ("intent", "sent", "needs_reverify")


def call_verified(db, attempt_id: str) -> bool:
    attempt = db.attempts.find_one({"_id": attempt_id}, {"state": 1})
    if not attempt or attempt.get("state") != "completed":
        return False
    return db.claims.count_documents({"attempt_id": attempt_id, "status": "accepted"}, limit=1) == 1


def operation_verified(db, op_id: str) -> bool:
    op = db.operations.find_one({"_id": op_id}, {"state": 1, "readback_at": 1, "readback_value": 1})
    return bool(op and op.get("state") == "verified" and op.get("readback_at") is not None
                and op.get("readback_value") is not None)


def form_link_verified(db, op_id: str) -> bool:
    op = db.operations.find_one({"_id": op_id}, {"state": 1, "recall_id": 1, "party_id": 1, "sent_wall_at": 1})
    if not op:
        return False
    if op.get("state") == "verified":
        return True
    since = op.get("sent_wall_at")
    if since is None:
        return False
    return db.claims.count_documents({"recall_id": op["recall_id"], "party_id": op["party_id"], "source": "form",
                                      "status": "accepted", "created_at": {"$gte": since}}, limit=1) == 1


def answer_verified(db, recall_id: str, party_id: str, question_id: str, attempt_id: str | None = None) -> bool:
    party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id}, {f"answers.{question_id}": 1})
    answer = ((party or {}).get("answers") or {}).get(question_id)
    if not isinstance(answer, dict):
        return False
    return attempt_id is None or answer.get("attempt_id") == attempt_id


def adapter_verified(db, adapter_id: str) -> bool:
    adapter = db.adapters.find_one({"_id": adapter_id}, {"canary": 1})
    canary = (adapter or {}).get("canary") or {}
    return bool(canary.get("passed")) and canary.get("ledger_value") == canary.get("order_system_value")


def mark_verified(db, op_id: str, *, readback_value, readback_at, session=None) -> bool:
    """Move an unverified operation to `verified` with the read-back that
    proves it. False when the operation was already verified or failed."""
    if readback_value is None or readback_at is None:
        raise ValueError("verified needs the read-back: readback_value and readback_at")
    res = db.operations.update_one(
        {"_id": op_id, "state": {"$in": list(UNVERIFIED_OPS)}},
        {"$set": {"state": "verified", "readback_value": readback_value, "readback_at": readback_at}},
        session=session)
    return res.modified_count == 1
