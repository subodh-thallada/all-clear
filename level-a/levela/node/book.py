"""Booking numbers onto one party, as the node does it on either side.

Two callers: the parent books a report from a child node onto the one party
that stands for that node (levela.node.report), and the child books its own
staff's counts and its stores' stock moves (levela.node.child). Both go the
same way: one transaction writes the party's buckets and evidence, inserts
the claim and its evidence event (tool_events, invariant 3), runs the
caller's extra writes and recomputes the recall's open_units. Atlas decides:
the parties validator (levela.db.schema, rule 2) refuses counts the evidence
does not allow. explain() names the rule and the two values that disagree, in
the words WS1's claim path uses (still_on_shelf, held_needs_evidence,
sold_needs_till, sub_closed, over_count / under_count for conservation).

If the database accepted a write that explain() refuses (a validator not yet
installed, an older schema), the node still refuses it and records
enforced_by "node", so the rule holds either way and the claim says who
enforced it.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError, WriteError

from ..db.client import run_txn

BUCKETS = ("sold", "on_shelf", "held", "returned", "forwarded", "unresolved")
EVIDENCE = ("held_by_system", "sold_by_till", "system_on_shelf")
TILL_REPORT = "till_report"
VALIDATION_FAILED = 121


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Refused(Exception):
    def __init__(self, rule: str, values, atlas: dict | None = None):
        super().__init__(rule)
        self.rule, self.values, self.atlas = rule, values, atlas


def explain(doc: dict) -> tuple[str, list] | None:
    """(rule, [a, b]) for the first rule the party document breaks, else None.
    Mirrors the parties validator, in the order WS1's claim path names them."""
    q = {b: doc.get(b, 0) for b in BUCKETS}
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in q.values()):
        bad = next(v for v in q.values() if not isinstance(v, int) or isinstance(v, bool) or v < 0)
        return "negative", [bad, 0]
    total, shipped = sum(q.values()), doc.get("shipped_in", 0)
    if total != shipped:
        return ("over_count" if total > shipped else "under_count"), [total, shipped]
    ev = {e: doc.get(e) or 0 for e in EVIDENCE}
    if q["on_shelf"] < ev["system_on_shelf"]:
        return "still_on_shelf", [q["on_shelf"], ev["system_on_shelf"]]
    by_phone = (isinstance(doc.get("batch_seen"), str) and doc.get("batch_seen") == doc.get("batch_code")
                and doc.get("block_test") is True)
    if q["held"] > ev["held_by_system"] and not by_phone:
        return "held_needs_evidence", [q["held"], ev["held_by_system"]]
    if q["sold"] > ev["sold_by_till"] and doc.get("sold_source") != TILL_REPORT:
        return "sold_needs_till", [q["sold"], ev["sold_by_till"]]
    if (doc.get("sub") or {}).get("state") == "closed" and (q["on_shelf"] or q["unresolved"]):
        return "sub_closed", [q["on_shelf"], q["unresolved"]]
    return None


def atlas_error(exc: WriteError) -> dict:
    d = exc.details or {}
    return {"code": exc.code, "codeName": d.get("codeName", "DocumentValidationFailure"),
            "errmsg": d.get("errmsg", str(exc))[:2000]}


_EDGE_OPEN = {"$switch": {"branches": [
    {"case": {"$eq": ["$state", "awaiting_recipient"]}, "then": {"$subtract": ["$sender_qty", "$corroborated_qty"]}},
    {"case": {"$eq": ["$state", "disputed"]}, "then": "$disputed_qty"},
], "default": 0}}


def open_units(db, recall_id: str, session=None) -> int:
    """Σ parties (unresolved + on_shelf) + Σ edges (open or disputed qty)."""
    p = next(db.parties.aggregate([{"$match": {"recall_id": recall_id}}, {"$group": {
        "_id": None, "n": {"$sum": {"$add": ["$unresolved", "$on_shelf"]}}}}], session=session), None)
    e = next(db.edges.aggregate([{"$match": {"recall_id": recall_id}}, {"$group": {
        "_id": None, "n": {"$sum": _EDGE_OPEN}}}], session=session), None)
    return (p or {}).get("n", 0) + (e or {}).get("n", 0)


def refresh_open_units(db, recall_id: str, session=None) -> int:
    n = open_units(db, recall_id, session)
    db.recalls.update_one({"_id": recall_id}, {"$set": {"open_units": n}}, session=session)
    return n


def new_claim_id() -> str:
    return f"clm_{secrets.token_hex(7)}"


def book(client, db, *, recall_id: str, party_id: str, sets: dict, claim: dict, evidence: dict | None = None,
         extra=None, now: datetime | None = None) -> dict:
    """Write `sets` onto the party with its claim in one transaction.

    claim: the claim document without status (needs dedupe_key, source,
    attempt_id). evidence: the tool_events row. extra(session): more writes
    inside the same transaction. Returns {"accepted", "claim_id", "rule",
    "values", "duplicate"}; a dedupe_key already booked returns that claim.
    """
    now = now or utcnow()
    prior = db.claims.find_one({"dedupe_key": claim["dedupe_key"]})
    if prior is not None:
        return _reply(prior, duplicate=True)
    claim = {"_id": claim.get("_id") or new_claim_id(), "recall_id": recall_id, "party_id": party_id,
             "created_at": now, **claim}

    def txn(session):
        party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id}, session=session)
        if party is None:
            raise Refused("unknown_party", [party_id, None])
        after = {**party, **sets}
        rule = explain(after)
        try:
            db.parties.update_one({"_id": party["_id"]}, {"$set": sets}, session=session)
        except WriteError as exc:
            if exc.code != VALIDATION_FAILED:
                raise
            raise Refused(*(rule or ("refused", None)), atlas=atlas_error(exc)) from None
        if rule is not None:
            raise Refused(*rule)          # the database let it through: the node refuses it
        db.claims.insert_one({**claim, "status": "accepted", "accepted": True}, session=session)
        if evidence is not None:
            db.tool_events.insert_one({"_id": f"te-{claim['_id']}", "attempt_id": claim.get("attempt_id"),
                                       "recall_id": recall_id, "party_id": party_id, "received_at": now,
                                       **evidence}, session=session)
        if extra is not None:
            extra(session)
        return refresh_open_units(db, recall_id, session)

    try:
        units = run_txn(client, txn)
    except Refused as exc:
        doc = {**claim, "status": "rejected", "accepted": False, "rule": exc.rule, "values": exc.values,
               "enforced_by": "atlas" if exc.atlas else "node", "atlas_error": exc.atlas}
        try:
            db.claims.insert_one(doc)
        except DuplicateKeyError:
            return _reply(db.claims.find_one({"dedupe_key": claim["dedupe_key"]}), duplicate=True)
        return _reply(doc)
    except DuplicateKeyError:
        found = db.claims.find_one({"dedupe_key": claim["dedupe_key"]})
        if found is None:
            raise
        return _reply(found, duplicate=True)
    out = _reply({**claim, "status": "accepted", "accepted": True})
    out["open_units"] = units
    return out


def _reply(claim: dict, duplicate: bool = False) -> dict:
    return {"accepted": bool(claim.get("accepted")), "claim_id": claim["_id"], "rule": claim.get("rule"),
            "values": claim.get("values"), "duplicate": duplicate}
