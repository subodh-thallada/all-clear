"""Evidence the API writes: tool events, and operations it may mark verified.

Every claim, answer and callback that reaches the API is logged in
`tool_events` before it is acted on: recovery adopts an in-doubt attempt from
such a row instead of redialling, and invariant 3 (V2 §9) requires one for
every accepted claim (matched by attempt_id, which for a report is its
evidence id).

The API is the only writer of `verified` on operations it can prove from its
own records (spec "Verification"): a form link is verified when a submission
arrives with the party's token, and a report's evidence operation when the
claim it carried was accepted. The read-back recorded is the accepted claim's
id, so the operations validator's "verified needs a read-back" rule holds.
"""
from __future__ import annotations

from datetime import datetime

from .. import feed

FORM_LINK = "send_form_link"
OUTSTANDING = ["intent", "sent", "needs_reverify"]


def log_event(db, *, kind: str, attempt_id, party_id, recall_id, source: str, payload: dict, now: datetime,
              conversation_id=None, call_sid=None) -> None:
    db.tool_events.insert_one({
        "kind": kind, "attempt_id": attempt_id, "party_id": party_id, "recall_id": recall_id,
        "conversation_id": conversation_id, "call_sid": call_sid, "source": source, "payload": payload,
        "received_at": now,
    })


def _verified(claim_id, sim_now: float) -> dict:
    return {"state": "verified", "readback_at": round(sim_now, 4), "readback_value": claim_id,
            "verified_by": "api"}


def verify_operation(db, op_id: str, claim_id, sim_now: float) -> bool:
    """Mark one operation verified by an accepted claim. False when the id is
    not an operation or it was already verified."""
    res = db.operations.update_one({"_id": op_id, "state": {"$ne": "verified"}}, {"$set": _verified(claim_id, sim_now)})
    return res.modified_count == 1


def verify_form_links(db, party: dict, claim_id, sim_now: float) -> int:
    """A submission with the party's token is the read-back of every form link
    still outstanding for that party. Announced on the wall when one moved."""
    res = db.operations.update_many(
        {"recall_id": party["recall_id"], "party_id": party["party_id"], "kind": FORM_LINK,
         "state": {"$in": OUTSTANDING}},
        {"$set": _verified(claim_id, sim_now)})
    if res.modified_count:
        feed.emit(db, "form_link_verified", f"{party['name']}: form link verified, a submission arrived with "
                  "the party's link", level="good", recall_id=party["recall_id"], party_id=party["party_id"],
                  system="form", sim=sim_now, detail={"claim_id": str(claim_id), "operations": res.modified_count})
    return res.modified_count
