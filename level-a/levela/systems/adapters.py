"""Adapters: the contract the agent holds each order system to (V2 §7).

    {_id: "north:v2", order system, vendor, version, status (active|retired|candidate|rejected),
     replaces, source ("builtin"|"model"|"rules"|"episode ep-…"|"scripted"),
     paths{place_hold, confirm_hold, list_holds, list_accounts}  ("POST /v2/holds", or null),
     steps{place_hold: [...]}, mapping{canonical: order system dot path},
     status_in_force[...], status_pending[...], defaults{request field: value},
     expected_fields{accounts: [...], holds: [...]}, canary{...}, last_failure{...},
     model_calls, usage, rationale, history[...], created_at, activated_at, retired_at}

Adapters are per order system, not per recall: the order system changed for everyone.
The builtin v1 adapter is written the first time an order system is used. A repair
writes a candidate (the next version), the canary checks it, and activate()
swaps it in with the old one retired in one transaction; the partial unique
index one_active_adapter_per_order_system makes a second active version impossible
even if two repairs raced past every other check.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.client import run_txn
from .contract import CANONICAL, expected_fields

BUILTIN_V1 = {
    "version": 1,
    "paths": {"place_hold": "POST /v1/holds", "confirm_hold": None,
              "list_holds": "GET /v1/holds", "list_accounts": "GET /v1/accounts"},
    "mapping": {field: field for field in CANONICAL},
    "status_in_force": ["on_hold"],
    "status_pending": [],
    "defaults": {},
}


class AdapterConflict(Exception):
    """Another repair activated or rewrote this adapter first."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def adapter_id(order_system: str, version: int) -> str:
    return f"{order_system}:v{version}"


def steps_for(paths: dict) -> dict:
    """The calls a hold takes: one POST, or a POST and its confirm."""
    return {"place_hold": [p for p in (paths["place_hold"], paths.get("confirm_hold")) if p]}


def _body(order_system: str, version: int, spec: dict, *, status: str, source: str, vendor: str | None,
          replaces: str | None, now: datetime) -> dict:
    mapping = dict(spec["mapping"])
    return {
        "_id": adapter_id(order_system, version), "order_system": order_system, "vendor": vendor, "version": version,
        "status": status, "replaces": replaces, "source": source,
        "paths": dict(spec["paths"]), "steps": steps_for(spec["paths"]), "mapping": mapping,
        "status_in_force": list(spec["status_in_force"]), "status_pending": list(spec["status_pending"]),
        "defaults": dict(spec.get("defaults") or {}), "expected_fields": expected_fields(mapping),
        "canary": None, "last_failure": None, "created_at": now,
    }


def builtin(order_system: str, *, now: datetime | None = None) -> dict:
    return _body(order_system, 1, BUILTIN_V1, status="active", source="builtin", vendor=None, replaces=None,
                 now=now or utcnow())


def active(db, order_system: str) -> dict | None:
    """The order system's active adapter, seeding the builtin v1 the first time.

    None only when the order system has adapters but none is active, which the
    one-transaction swap never leaves behind (it means someone edited them)."""
    doc = db.adapters.find_one({"order_system": order_system, "status": "active"})
    if doc is not None:
        return doc
    if db.adapters.find_one({"order_system": order_system}, {"_id": 1}) is None:
        try:
            db.adapters.insert_one(builtin(order_system))
        except DuplicateKeyError:
            pass                      # another worker seeded it first
    return db.adapters.find_one({"order_system": order_system, "status": "active"})


def build_candidate(order_system: str, proposal: dict, old: dict, *, source: str, vendor: str | None,
                    model_calls: int = 0, usage: dict | None = None, rationale: str = "",
                    failure: dict | None = None, now: datetime | None = None) -> dict:
    """The next version of `old`, from a validated proposal."""
    doc = _body(order_system, old["version"] + 1, proposal, status="candidate", source=source, vendor=vendor,
                replaces=old["_id"], now=now or utcnow())
    doc.update(model_calls=model_calls, usage=usage, rationale=rationale, failure=failure)
    return doc


def save_candidate(db, doc: dict) -> dict:
    """Store a candidate. An earlier candidate or rejected attempt at the same
    version is overwritten, and what it proposed is kept in `history`."""
    prior = db.adapters.find_one({"_id": doc["_id"]})
    if prior is None:
        try:
            db.adapters.insert_one({**doc, "history": []})
        except DuplicateKeyError:
            raise AdapterConflict(f"{doc['_id']} was written by another repair") from None
        return {**doc, "history": []}
    if prior["status"] not in ("candidate", "rejected"):
        raise AdapterConflict(f"{doc['_id']} is already {prior['status']}")
    earlier = {k: prior.get(k) for k in ("source", "mapping", "paths", "canary", "status", "created_at")}
    history = [*(prior.get("history") or []), earlier]
    new = {**doc, "history": history}
    res = db.adapters.replace_one({"_id": doc["_id"], "status": prior["status"]}, new)
    if res.matched_count != 1:
        raise AdapterConflict(f"{doc['_id']} changed while it was being rewritten")
    return new


def reject(db, adapter_id_: str, canary: dict, *, now: datetime | None = None) -> None:
    db.adapters.update_one({"_id": adapter_id_, "status": "candidate"},
                           {"$set": {"status": "rejected", "canary": canary, "rejected_at": now or utcnow()}})


def record_failure(db, adapter_id_: str, failure: dict) -> None:
    """The contract failure that broke this adapter: what repair works from."""
    db.adapters.update_one({"_id": adapter_id_}, {"$set": {"last_failure": failure}})


def activate(client, db, candidate: dict, old: dict, *, canary: dict, now: datetime | None = None) -> dict:
    """One transaction: old active -> retired, candidate -> active (with its
    canary), and the order system's distributor parties unpaused. Raises
    AdapterConflict when either document is no longer in the state the
    caller saw (a concurrent repair won)."""
    now = now or utcnow()

    def txn(session):
        retired = db.adapters.update_one({"_id": old["_id"], "status": "active"},
                                         {"$set": {"status": "retired", "retired_at": now}}, session=session)
        if retired.matched_count != 1:
            raise AdapterConflict(f"{old['_id']} is no longer active")
        doc = db.adapters.find_one_and_update(
            {"_id": candidate["_id"], "status": "candidate"},
            {"$set": {"status": "active", "activated_at": now, "canary": canary}},
            return_document=ReturnDocument.AFTER, session=session)
        if doc is None:
            raise AdapterConflict(f"{candidate['_id']} is no longer a candidate")
        db.parties.update_many({"order_system": candidate["order_system"], "channel": "order_system"},
                              {"$set": {"order_system_paused": False, "order_system_failure": None}}, session=session)
        return doc

    return run_txn(client, txn)
