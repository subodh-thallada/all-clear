"""open_units, the global mass balance, the three metrics, the headline and
the close gate.

open_units is what blocks "all clear": every party's unresolved and
installed (installed and not yet confirmed fixed) panels, plus every edge's
open or disputed quantity.

The three numbers are reported side by side and never merged:

  checks completed   parties in claimed or reconciled / all parties
  panels located     in stock + installed + fixed + returned / units_total
  fixed or returned  fixed + returned / units_total

The headline is the database's verdict in words: "ALL CLEAR" once the
recall is closed, else "NOT ALL CLEAR: n panels open (a disputed, b awaiting
fix, c not located)", naming only the parts that are not zero.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pymongo.errors import WriteError

from ..db.client import run_txn

VALIDATION_FAILED = 121


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_EDGE_OPEN = {"$switch": {"branches": [
    {"case": {"$eq": ["$state", "awaiting_recipient"]}, "then": {"$subtract": ["$sender_qty", "$corroborated_qty"]}},
    {"case": {"$eq": ["$state", "disputed"]}, "then": "$disputed_qty"},
], "default": 0}}


def _totals(recall_id, party_sum, edge_sum) -> list:
    """One aggregation on the recall: a party total and an edge total beside
    units_total. $lookup sub-pipelines rather than $unionWith, because
    $unionWith is refused inside a transaction and both callers need one."""
    def total(coll, expr):
        return {"$lookup": {"from": coll, "as": coll, "pipeline": [
            {"$match": {"recall_id": recall_id}}, {"$group": {"_id": None, "n": {"$sum": expr}}}]}}
    return [
        {"$match": {"_id": recall_id}},
        total("parties", party_sum), total("edges", edge_sum),
        {"$project": {"_id": 0, "units_total": 1, "parties": {"$sum": "$parties.n"}, "edges": {"$sum": "$edges.n"}}},
    ]


_HELD = ("in_stock", "installed", "fixed", "returned", "unresolved")


def balance_pipeline(recall_id) -> list:
    """units_total beside the two halves of the mass balance:
    parties (in_stock + installed + fixed + returned + unresolved) and edges
    (sender_qty - corroborated_qty)."""
    return _totals(recall_id, {"$add": [f"${k}" for k in _HELD]},
                   {"$subtract": ["$sender_qty", "$corroborated_qty"]})


def mass_balance(db, recall_id, session=None) -> int:
    """units_total must equal this after every write (0 for an unknown recall):
    sum over parties of (in_stock + installed + fixed + returned + unresolved)
    plus sum over edges of (sender_qty - corroborated_qty)."""
    row = next(db.recalls.aggregate(balance_pipeline(recall_id), session=session), None)
    return row["parties"] + row["edges"] if row else 0


def open_units(db, recall_id, session=None) -> int:
    """Every party's unresolved and installed panels plus every edge's open or
    disputed quantity (0 for an unknown recall). One round trip: it runs in
    every claim."""
    row = next(db.recalls.aggregate(_totals(recall_id, {"$add": ["$unresolved", "$installed"]}, _EDGE_OPEN),
                                    session=session), None)
    return row["parties"] + row["edges"] if row else 0


def metrics(db, recall_id, parties=None, edges=None) -> dict:
    """The three numbers, what is open and why, and the mass balance. Pass the
    party and edge rows if you already have them (the screen does) to save
    queries.

      open_units = not_located + awaiting_fix + disputed_units
      not_located   unresolved + transfers still awaiting their recipient
      awaiting_fix  installed and not yet confirmed fixed
    """
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None:
        return {}
    rows = parties if parties is not None else list(db.parties.find({"recall_id": recall_id}))
    erows = edges if edges is not None else list(db.edges.find({"recall_id": recall_id}))
    total = recall["units_total"]
    awaiting = sum(e["sender_qty"] - e["corroborated_qty"] for e in erows if e["state"] == "awaiting_recipient")
    disputed = sum(e["disputed_qty"] for e in erows if e["state"] == "disputed")
    unresolved = sum(r["unresolved"] for r in rows)
    installed = sum(r["installed"] for r in rows)
    balance = sum(sum(r[k] for k in _HELD) for r in rows) + sum(e["sender_qty"] - e["corroborated_qty"] for e in erows)
    return {
        "units_total": total,
        "checks_completed": [sum(r["state"] in ("claimed", "reconciled") for r in rows), len(rows)],
        "panels_located": [sum(r["in_stock"] + r["installed"] + r["fixed"] + r["returned"] for r in rows), total],
        "fixed_or_returned": [sum(r["fixed"] + r["returned"] for r in rows), total],
        "open_units": unresolved + installed + awaiting + disputed,
        "not_located": unresolved + awaiting,
        "awaiting_fix": installed,
        "disputed_units": disputed,
        "mass_balance": balance,
        "status": recall["status"],
    }


def headline(m: dict) -> str:
    """The words the database allows, from metrics(): "ALL CLEAR" for a closed
    recall, else "NOT ALL CLEAR: 39 panels open (15 disputed, 24 awaiting
    fix)" with only the parts that are not zero."""
    if m.get("status") == "closed":
        return "ALL CLEAR"
    parts = [f"{n} {label}" for n, label in ((m["disputed_units"], "disputed"), (m["awaiting_fix"], "awaiting fix"),
                                              (m["not_located"], "not located")) if n]
    n = m["open_units"]
    text = f"NOT ALL CLEAR: {n} panel{'' if n == 1 else 's'} open"
    return f"{text} ({', '.join(parts)})" if parts else text


def atlas_error(exc: WriteError) -> dict:
    """The raw validation error as Atlas returned it, for the wall feed."""
    d = exc.details or {}
    return {"code": exc.code, "codeName": d.get("codeName", "DocumentValidationFailure"),
            "errmsg": d.get("errmsg", str(exc)), "errInfo": d.get("errInfo")}


def close_line(res: dict) -> tuple[str, str, str]:
    """(feed kind, text, level) for a try_close() result: the close beat's
    line on the screen."""
    if res["closed"]:
        return "recall_closed", "ALL CLEAR: every panel of the batch is found and fixed or returned", "good"
    if res.get("atlas_error"):
        return "close_refused", f"Close refused by Atlas: {res['headline']}", "bad"
    return "close_refused", f"Close refused: {res['reason']}", "bad"


def try_close(db, recall_id, now=None) -> dict:
    """Recompute open_units, write it, then try to close. Atlas refuses while
    open_units is above 0 (the close-gate validator on recalls). The result
    carries the headline: "ALL CLEAR", or what is still open.

    The claim path maintains open_units inside every claim transaction, and
    it also reads the recall's status, so a claim racing this close either
    commits first (and the close sees its open_units) or sees `closed` and is
    refused.

    The recompute is itself a transaction that writes the recall document:
    computed outside one, a claim committing between the read and the write
    had its open_units overwritten by the stale value, and a stale 0 let the
    gate close a recall with panels still open. Now the two writes conflict
    and with_transaction recomputes from the claim's committed state.

    The same snapshot also reads the mass balance, and a recall whose balance
    is not units_total is not closed even with open_units at 0: panels have
    gone missing from the ledger (a write around the claim path, or a booking
    bug such as a receipt dropping another sender's corroborated panels), and
    open_units cannot see panels that are on no party and no edge. Every claim
    keeps the balance, so this refuses only a ledger that is already wrong.
    """
    now = now or utcnow()
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None:
        return {"closed": False, "open_units": None, "reason": f"unknown recall {recall_id}"}
    if recall["status"] == "closed":
        return {"closed": True, "open_units": recall["open_units"], "already": True, "headline": "ALL CLEAR"}

    def recompute(session):
        n = open_units(db, recall_id, session)
        balance = mass_balance(db, recall_id, session)
        db.recalls.update_one({"_id": recall_id}, {"$set": {"open_units": n}}, session=session)
        return n, balance

    units, balance = run_txn(db.client, recompute)
    total = recall["units_total"]
    if balance != total:
        db.recalls.update_one({"_id": recall_id}, {"$set": {"last_close_refused_at": now}})
        return {"closed": False, "open_units": units, "mass_balance": balance, "units_total": total,
                "headline": headline(metrics(db, recall_id)),
                "reason": f"mass balance {balance} != units_total {total}: "
                          f"{abs(total - balance)} panels are not accounted for"}
    try:
        db.recalls.update_one({"_id": recall_id, "status": "open"},
                              {"$set": {"status": "closed", "closed_at": now}})
    except WriteError as exc:
        if exc.code != VALIDATION_FAILED:
            raise
        db.recalls.update_one({"_id": recall_id}, {"$set": {"last_close_refused_at": now}})
        words = headline(metrics(db, recall_id))
        return {"closed": False, "open_units": units, "headline": words, "reason": f"Atlas refused: {words}",
                "atlas_error": atlas_error(exc)}
    return {"closed": True, "open_units": 0, "headline": "ALL CLEAR"}
