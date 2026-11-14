"""From complaints to a recall (spec "Store records" > "From complaints to a recall").

The rule is one aggregation over `complaints`:

  match   classification.defect is set, within 30 simulated days
  group   by defect and batch_code: the complaints and the distinct stores
  match   3 or more complaints and 3 or more stores
  lookup  shipments on that batch: who was shipped it

A group that passes creates one `proposed` signal (the partial unique index
allows one open signal per batch); a later complaint is added to it.

Approval is a person's act, never a model's: approve() sets approved_by and
creates the recall (source "signal", source_signal) in ONE transaction, and
it is the only way to open a recall from a signal. No harness tool list has
an approve tool (test 35 asserts it).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from .. import feed
from ..db.client import run_txn
from ..db.schema_store import OPEN_SIGNAL_STATES
from ..ledger.names import normalise

log = logging.getLogger(__name__)

WINDOW_DAYS = 30
MIN_COMPLAINTS = 3
MIN_STORES = 3
RULE = f"{MIN_COMPLAINTS} complaints from {MIN_STORES} stores within {WINDOW_DAYS} days"
FEED_SYSTEM = "store records"


class SignalError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rule_pipeline(now_day: float, *, batch_code: str | None = None) -> list[dict]:
    match = {"classification.defect": {"$type": "string"}, "day": {"$gte": now_day - WINDOW_DAYS, "$lte": now_day}}
    if batch_code:
        match["batch_code"] = batch_code
    return [
        {"$match": match},
        {"$sort": {"day": 1, "_id": 1}},
        {"$group": {"_id": {"defect": "$classification.defect", "batch_code": "$batch_code"},
                    "complaints": {"$push": "$_id"}, "stores": {"$addToSet": "$store_key"},
                    "names": {"$push": {"k": "$store_key", "n": "$bought_at"}},
                    "first_day": {"$min": "$day"}, "last_day": {"$max": "$day"}}},
        {"$match": {"$expr": {"$and": [{"$gte": [{"$size": "$complaints"}, MIN_COMPLAINTS]},
                                       {"$gte": [{"$size": "$stores"}, MIN_STORES]}]}}},
        {"$lookup": {"from": "shipments", "let": {"b": "$_id.batch_code"}, "as": "shipped", "pipeline": [
            {"$match": {"$expr": {"$eq": ["$batch_code", "$$b"]}}},
            {"$project": {"_id": 0, "name": 1, "party_id": 1}}]}},
    ]


def _now_day(db, batch_code: str | None) -> float:
    q = {"classification.defect": {"$type": "string"}}
    if batch_code:
        q["batch_code"] = batch_code
    row = db.complaints.find_one(q, {"day": 1}, sort=[("day", -1)])
    return float((row or {}).get("day") or 0)


def _store_names(names: list[dict]) -> list[str]:
    seen, out = set(), []
    for row in names:
        key = row.get("k") or normalise(row.get("n") or "")
        if key and key not in seen:
            seen.add(key)
            out.append(row.get("n") or key)
    return out


def _shipped_names(rows: list[dict]) -> list[str]:
    out = []
    for row in rows:
        name = row.get("name") or row.get("party_id")
        if name and name not in out:
            out.append(name)
    return out


def _next_id(db) -> str:
    n = db.signals.estimated_document_count() + 1
    while db.signals.find_one({"_id": f"sig-{n:02d}"}, {"_id": 1}) is not None:
        n += 1
    return f"sig-{n:02d}"


def evaluate(db, *, now_day: float | None = None, batch_code: str | None = None,
             now: datetime | None = None) -> list[dict]:
    """Run the rule. Creates a proposed signal for each new group that passes
    and adds later complaints to the batch's open signal. Returns the signals
    created or changed."""
    now = now or utcnow()
    day = _now_day(db, batch_code) if now_day is None else float(now_day)
    changed = []
    for group in db.complaints.aggregate(rule_pipeline(day, batch_code=batch_code)):
        defect, batch = group["_id"]["defect"], group["_id"]["batch_code"]
        stores, shipped = _store_names(group["names"]), _shipped_names(group["shipped"])
        sig = _upsert(db, defect, batch, group["complaints"], stores, shipped, day, now)
        if sig is not None:
            changed.append(sig)
    return changed


def _upsert(db, defect, batch, complaint_ids, stores, shipped, day, now) -> dict | None:
    for _ in range(3):
        existing = db.signals.find_one({"batch_code": batch, "status": {"$in": OPEN_SIGNAL_STATES}})
        if existing is not None:
            if existing["defect"] != defect:
                log.info("batch %s already has an open %s signal; %s complaints wait", batch,
                         existing["defect"], defect)
                return None
            new = [c for c in complaint_ids if c not in existing["complaints"]]
            new_stores = [s for s in stores if normalise(s) not in {normalise(x) for x in existing["stores"]}]
            update = {"$addToSet": {"complaints": {"$each": new}, "stores": {"$each": new_stores}},
                      "$set": {"updated_at": now, "last_day": day}}
            if shipped and not existing.get("shipped_to"):
                update["$set"]["shipped_to"] = shipped
            if not new and not new_stores:
                return None
            db.signals.update_one({"_id": existing["_id"]}, update)
            db.complaints.update_many({"_id": {"$in": new}}, {"$set": {"signal_id": existing["_id"]}})
            after = db.signals.find_one({"_id": existing["_id"]})
            feed.emit(db, "signal_updated", f"Signal {after['_id']}: complaint {', '.join(new)} added "
                      f"({len(after['complaints'])} complaints from {len(after['stores'])} stores)",
                      recall_id=after.get("recall_id"), system=FEED_SYSTEM, detail={"signal_id": after["_id"]})
            return after
        doc = {"_id": _next_id(db), "defect": defect, "batch_code": batch, "rule": RULE,
               "complaints": list(complaint_ids), "stores": stores, "shipped_to": shipped, "status": "proposed",
               "approved_by": None, "recall_id": None, "proposed_at": now, "first_day": day, "last_day": day}
        try:
            db.signals.insert_one(doc)
        except DuplicateKeyError:
            continue        # another writer opened it first (or took the id): add to it
        db.complaints.update_many({"_id": {"$in": doc["complaints"]}}, {"$set": {"signal_id": doc["_id"]}})
        feed.emit(db, "signal_proposed", f"Signal {doc['_id']} proposed: {defect.replace('_', ' ')} on batch "
                  f"{batch}, {len(doc['complaints'])} complaints from {len(stores)} stores. A person must approve",
                  level="warn", system=FEED_SYSTEM, detail={"signal_id": doc["_id"], "stores": stores,
                                                            "shipped_to": shipped})
        return doc
    return None


def recall_from_signal(client, db, signal_id: str, scenario: dict, *, session, recall_id: str | None = None,
                       **start_kwargs) -> dict:
    """Open the recall a signal points to, inside the caller's transaction.
    Refused unless the signal is approved: a recall with a signal source
    cannot be created from a proposed one."""
    from ..db.seed import start_recall
    sig = db.signals.find_one({"_id": signal_id}, session=session)
    if sig is None:
        raise SignalError("unknown_signal", f"no signal {signal_id}")
    if sig["status"] != "approved" or not sig.get("approved_by"):
        raise SignalError("not_approved", f"signal {signal_id} is {sig['status']}: a person must approve it first")
    return start_recall(client, db, scenario, recall_id=recall_id, source="signal", source_signal=signal_id,
                        session=session, **start_kwargs)


def approve(client, db, signal_id: str, *, by: str, scenario: dict, recall_id: str | None = None,
            now: datetime | None = None, **start_kwargs) -> dict:
    """A person approves a proposed signal: approved_by is set and the recall
    opens, in one transaction. Returns {signal_id, recall_id, approved_by, recall}."""
    now = now or utcnow()
    if not isinstance(by, str) or not by.strip():
        raise SignalError("no_approver", "approval names the person who approves (by)")
    rid = recall_id or scenario["recall"]["_id"]
    holder: dict = {}

    def txn(session):
        sig = db.signals.find_one({"_id": signal_id}, session=session)
        if sig is None:
            raise SignalError("unknown_signal", f"no signal {signal_id}")
        if sig["status"] != "proposed":
            raise SignalError("not_proposed", f"signal {signal_id} is {sig['status']}, not proposed")
        if db.recalls.find_one({"_id": rid}, {"_id": 1}, session=session) is not None:
            raise SignalError("recall_exists", f"recall {rid} already exists")
        db.signals.update_one({"_id": signal_id, "status": "proposed"}, {"$set": {
            "status": "approved", "approved_by": by.strip(), "approved_at": now, "recall_id": rid}},
            session=session)
        holder["recall"] = recall_from_signal(client, db, signal_id, scenario, session=session, recall_id=rid,
                                              emit=False, **start_kwargs)
        db.recalls.update_one({"_id": rid}, {"$set": {
            "defect": sig["defect"], "approved_by": by.strip(), "barcode_verified": False,
            "signal": {"_id": signal_id, "complaints": sig["complaints"], "stores": sig["stores"]}}},
            session=session)
        shipped = [s.get("name") or s["party_id"] for s in db.parties.find(
            {"recall_id": rid, "tier": 1}, {"name": 1, "party_id": 1}, session=session)]
        if shipped and not sig.get("shipped_to"):
            db.signals.update_one({"_id": signal_id}, {"$set": {"shipped_to": shipped}}, session=session)
        holder["sig"] = sig

    from ..db.seed import ScenarioError
    try:
        run_txn(client, txn)
    except ScenarioError as exc:
        raise SignalError("bad_scenario", str(exc)) from None
    sig = holder["sig"]
    feed.emit(db, "recall_started", f"{by} approved signal {signal_id}: recall {rid} opened for batch "
              f"{sig['batch_code']} ({sig['defect'].replace('_', ' ')}, {len(sig['complaints'])} complaints from "
              f"{len(sig['stores'])} stores)", recall_id=rid, level="good", system=FEED_SYSTEM,
              detail={"signal_id": signal_id, "approved_by": by, "source": "signal"})
    _after_open(db, rid, client)
    return {"signal_id": signal_id, "recall_id": rid, "approved_by": by, "recall": holder["recall"]}


def _after_open(db, recall_id: str, client) -> None:
    """The plant party (idempotent; recall creation also does it) and the
    connectors of the stores on the shipment list."""
    from .link import link_recall
    from .plant import ensure_plant_party
    recall = db.recalls.find_one({"_id": recall_id})
    try:
        ensure_plant_party(db, recall)
    except Exception as exc:  # noqa: BLE001 - the recall is open; the plant call is retried by the next open
        log.warning("plant party for %s not created: %s", recall_id, exc)
    try:
        link_recall(db, recall_id, client=client)
    except Exception as exc:  # noqa: BLE001
        log.warning("store connectors for %s not linked: %s", recall_id, exc)


def dismiss(db, signal_id: str, *, by: str, now: datetime | None = None) -> dict:
    res = db.signals.update_one({"_id": signal_id, "status": "proposed"},
                                {"$set": {"status": "dismissed", "dismissed_by": by, "dismissed_at": now or utcnow()}})
    if not res.modified_count:
        raise SignalError("not_proposed", f"signal {signal_id} is not proposed")
    return db.signals.find_one({"_id": signal_id})
