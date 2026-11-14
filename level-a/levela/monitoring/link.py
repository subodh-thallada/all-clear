"""Connectors: a store's till and stock record, registered by store name and
linked to a party when a claim or an account list names that store
(spec "Proof that a tin left the shelf" > "Linking a connector to a party").

  store_systems  {_id, store_label, batch_code, product, party_id, recall_id,
                  connects: ["till", "stock"], blocked, block_requested_at,
                  block_readback_at, last_event_at, last_event, stock: {received,
                  sold, on_shelf, held}, simulated, linked_at}

A label is matched to a party with levela.ledger.names.match_party (Atlas
Search fuzzy where the cluster has it, else the normalised exact name), so
the claim path and the connector agree on who "Corner Mart" is. $search is
refused inside a transaction, so matching happens before any transaction.

On link the connector posts one `baseline` event from the store's own stock
record. Blocks: until a till has recognised a scanned tin as the recalled
product (recall.barcode_verified), only one store, the first, is asked to
block; after it, every linked store is (store_events._verify_barcode).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from .. import feed
from ..ledger.names import match_party, normalise
from .store_events import FEED_SYSTEM, STOCK_KEYS, StoreEventError, post_event

log = logging.getLogger(__name__)

CONNECTS = ("till", "stock")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def register_store(db, store_system_id: str, store_label: str, *, batch_code: str = "B-4471",
                   product: str | None = None, connects=CONNECTS, stock: dict | None = None,
                   simulated: bool = True, now: datetime | None = None) -> dict:
    """Create the connector, or leave an existing one as it is. Returns it."""
    label = " ".join(str(store_label or "").split())
    if not label:
        raise StoreEventError("bad_store", "store_label is required")
    doc = {"_id": store_system_id, "store_label": label, "batch_code": batch_code, "product": product,
           "party_id": None, "recall_id": None, "connects": [c for c in connects if c in CONNECTS],
           "blocked": False, "block_requested_at": None, "block_readback_at": None, "last_event_at": None,
           "last_event": None, "stock": {k: int((stock or {}).get(k, 0)) for k in STOCK_KEYS},
           "simulated": bool(simulated), "created_at": now or utcnow()}
    try:
        db.store_systems.update_one({"_id": store_system_id}, {"$setOnInsert": doc}, upsert=True)
    except DuplicateKeyError:
        raise StoreEventError("duplicate_label", f"another connector already has the label {label!r}") from None
    return db.store_systems.find_one({"_id": store_system_id})


def find_by_label(db, name: str) -> dict | None:
    """The connector whose label is this store name (normalised)."""
    key = normalise(name)
    if not key:
        return None
    for row in db.store_systems.find({}, {"store_label": 1, "party_id": 1, "recall_id": 1}):
        if normalise(row.get("store_label", "")) == key:
            return row
    return None


def _party_for_label(db, recall: dict, label: str) -> dict | None:
    pid, _ = match_party(db, recall["_id"], label, recall.get("directory"))
    return db.parties.find_one({"recall_id": recall["_id"], "party_id": pid})


def link_store(db, store_system_id: str, recall_id: str, party_id: str, *, client=None,
               now: datetime | None = None, baseline: bool = True) -> dict:
    """Link one connector to one party and post its baseline. Idempotent:
    an already linked connector returns as it is."""
    now = now or utcnow()
    store = db.store_systems.find_one({"_id": store_system_id})
    if store is None:
        raise StoreEventError("unknown_store", f"no store system {store_system_id}")
    if store.get("recall_id") == recall_id and store.get("party_id") == party_id:
        return {"linked": False, "already": True, "store_system_id": store_system_id, "party_id": party_id}
    recall = db.recalls.find_one({"_id": recall_id}) or {}
    fields = {"recall_id": recall_id, "party_id": party_id, "linked_at": now}
    if recall.get("barcode_verified") and not store.get("blocked"):
        # The barcode is proven: a store connected after that is blocked at once.
        fields.update(blocked=True, block_requested_at=now,
                      **({"block_readback_at": now} if store.get("simulated", True) else {}))
    db.store_systems.update_one({"_id": store_system_id}, {"$set": fields})
    db.parties.update_one({"recall_id": recall_id, "party_id": party_id},
                          {"$set": {"store_system_id": store_system_id, "connected": True}})
    feed.emit(db, "store_linked", f"{store['store_label']}'s till and stock record linked to the recall",
              recall_id=recall_id, party_id=party_id, system=FEED_SYSTEM,
              detail={"store_system_id": store_system_id})
    out = {"linked": True, "store_system_id": store_system_id, "recall_id": recall_id, "party_id": party_id}
    if baseline:
        stock = store.get("stock") or {}
        out["baseline"] = post_event(db, store_system_id, {
            "event_id": f"baseline:{recall_id}", "kind": "baseline", "batch_code": store.get("batch_code"),
            **{k: int(stock.get(k, 0)) for k in STOCK_KEYS}}, client=client, now=now)
    return out


def link_recall(db, recall_id: str, *, client=None, names: list[str] | None = None) -> list[dict]:
    """Link every unlinked connector whose label matches a party of the recall
    (or only those matching `names`: the stores a claim or an account list
    just named). Returns the links made."""
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None or recall.get("status") != "open":
        return []
    wanted = {normalise(n) for n in names or []}
    made = []
    for store in list(db.store_systems.find({"party_id": None})):
        if wanted and normalise(store["store_label"]) not in wanted:
            continue
        if store.get("batch_code") and recall.get("batch_code") and store["batch_code"] != recall["batch_code"] \
                and store["batch_code"] not in (recall.get("affected_batches") or []):
            continue
        party = _party_for_label(db, recall, store["store_label"])
        if party is None or party.get("type") not in (None, "store"):
            continue
        made.append(link_store(db, store["_id"], recall_id, party["party_id"], client=client))
    return made


def link_named(db, recall_id: str, names: list[str], *, client=None) -> list[dict]:
    """What the claim path (or an order system's account list) calls when it
    names stores: link their connectors, if any."""
    return link_recall(db, recall_id, client=client, names=[n for n in names if n])


def try_link(db, store_system_id: str, *, client=None) -> dict | None:
    """An unlinked connector looks for a party in the open recalls of its batch
    (newest first). Used before applying an event."""
    store = db.store_systems.find_one({"_id": store_system_id})
    if store is None or store.get("party_id"):
        return None
    q = {"status": "open"}
    if store.get("batch_code"):
        q["$or"] = [{"batch_code": store["batch_code"]}, {"affected_batches": store["batch_code"]}]
    for recall in db.recalls.find(q).sort("opened_at", -1):
        party = _party_for_label(db, recall, store["store_label"])
        if party is not None and party.get("type") in (None, "store"):
            return link_store(db, store_system_id, recall["_id"], party["party_id"], client=client)
    return None


# ---- blocks --------------------------------------------------------------------------

def block_allowed(db, recall: dict, store_system_id: str) -> tuple[bool, str]:
    """A block may be requested at this store now? Before the barcode is
    proven only the first store (the canary) may be asked."""
    if recall.get("barcode_verified"):
        return True, "barcode verified"
    first = db.store_systems.find_one({"recall_id": recall["_id"], "block_requested_at": {"$ne": None},
                                       "_id": {"$ne": store_system_id}}, {"_id": 1})
    if first is None:
        return True, "first store: proves the barcode"
    return False, (f"barcode not verified yet: waiting for {first['_id']}'s till to recognise a tin "
                   f"before any other store is blocked")


def request_block(db, store_system_id: str, *, now: datetime | None = None) -> dict:
    """Ask a linked store's till to block the product. A simulated till applies
    and reads the block back at once."""
    now = now or utcnow()
    store = db.store_systems.find_one({"_id": store_system_id})
    if store is None:
        raise StoreEventError("unknown_store", f"no store system {store_system_id}")
    recall = db.recalls.find_one({"_id": store.get("recall_id")}) if store.get("recall_id") else None
    if recall is None:
        return {"requested": False, "reason": "the store is not linked to a recall"}
    ok, why = block_allowed(db, recall, store_system_id)
    if not ok:
        return {"requested": False, "reason": why}
    if store.get("blocked"):
        return {"requested": False, "already": True, "reason": "already blocked"}
    fields = {"blocked": True, "block_requested_at": now}
    if store.get("simulated", True):
        fields["block_readback_at"] = now
    db.store_systems.update_one({"_id": store_system_id}, {"$set": fields})
    feed.emit(db, "store_block", f"{store['store_label']}: block requested at the till ({why})",
              recall_id=recall["_id"], party_id=store.get("party_id"), system=FEED_SYSTEM,
              detail={"store_system_id": store_system_id})
    return {"requested": True, "reason": why}


def set_store(db, store_system_id: str, *, blocked: bool | None = None, stock: dict | None = None,
              store_label: str | None = None, batch_code: str | None = None,
              now: datetime | None = None) -> dict:
    """POST /admin/stores/{id}: set a store's block or stock numbers for a test
    (creates the connector when a label is given and it does not exist)."""
    now = now or utcnow()
    if db.store_systems.find_one({"_id": store_system_id}, {"_id": 1}) is None:
        if not store_label:
            raise StoreEventError("unknown_store", f"no store system {store_system_id}: give a store_label")
        register_store(db, store_system_id, store_label, batch_code=batch_code or "B-4471", stock=stock, now=now)
    fields: dict = {}
    if blocked is not None:
        fields.update(blocked=bool(blocked), block_requested_at=now if blocked else None,
                      block_readback_at=now if blocked else None)
    if stock:
        for k in STOCK_KEYS:
            if k in stock:
                n = stock[k]
                if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                    raise StoreEventError("bad_stock", f"stock.{k} must be a whole number >= 0")
                fields[f"stock.{k}"] = n
    if fields:
        db.store_systems.update_one({"_id": store_system_id}, {"$set": fields})
    return db.store_systems.find_one({"_id": store_system_id})


def tick(db, recall_id: str, *, client=None) -> list[dict]:
    """The scheduler's hourly pass: link connectors whose store a party of the
    recall now names (a claim or an account list created it). Never raises."""
    try:
        if db.store_systems.find_one({"party_id": None}, {"_id": 1}) is None:
            return []
        return link_recall(db, recall_id, client=client)
    except Exception as exc:  # noqa: BLE001 - the scheduler's other work must go on
        log.warning("store connector link pass failed for %s: %s", recall_id, exc)
        return []
