"""Store events: a connected store's till and stock record report for the product
(spec "Store records" > "Proof that a tin left the shelf").

post_event() is the one writer. In ONE transaction it stores the event (the
unique (store_system_id, event_id) index makes a repeated post count once),
updates the store's own stock record on its `store_systems` document and,
when the connector is linked to a party of an open recall, moves the tins on
that party's ledger:

  baseline         sets sold_by_till / system_on_shelf (and held_by_system) from the
                   store's own records; unresolved tins those records explain are located
  sale             one tin on_shelf -> sold, +1 sold_by_till. After the block was read
                   back it is a breach: the party drops below claimed, the feed line is red
  blocked_sale     moves nothing. The first one proves the barcode (barcode_verified)
                   and the blocks go out to the recall's other connected stores
  quarantine       one tin on_shelf -> held, +1 held_by_system
  customer_return  one tin sold -> held, +1 held_by_system (never back to on_shelf)
  delivery         tins onto the shelf, +system_on_shelf

No model reads a store event. The party validator (levela.db.schema) has the
last word: `on_shelf >= system_on_shelf`, `held <= held_by_system` unless the
phone checks passed, `sold <= sold_by_till` unless counted from the till.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError, OperationFailure, WriteError

from .. import feed
from ..db.client import run_txn
from ..db.schema_store import EVENT_KINDS

log = logging.getLogger(__name__)

VALIDATION_FAILED = 121
BUCKETS = ("sold", "on_shelf", "held", "returned", "forwarded", "unresolved")
EVIDENCE = ("held_by_system", "sold_by_till", "system_on_shelf")
STOCK_KEYS = ("received", "sold", "on_shelf", "held")
FEED_SYSTEM = "store records"
CLAIMED_STATES = ("claimed", "reconciled")
REOPENED_STATE = "reached"


class StoreEventError(ValueError):
    """An event the API refuses. `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def event_doc_id(store_system_id: str, event_id: str) -> str:
    return f"{store_system_id}:{event_id}"


def _int(value, default: int = 0, *, name: str = "qty") -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise StoreEventError("bad_event", f"{name} must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise StoreEventError("bad_event", f"{name} must be a whole number") from None
    if n < 0 or n != float(value):
        raise StoreEventError("bad_event", f"{name} must be a whole number >= 0")
    return n


def _when(value, now: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            raise StoreEventError("bad_event", "at must be an ISO 8601 time") from None
    return now


def normalise_event(event: dict, store: dict, now: datetime) -> dict:
    """The event as stored: checked kind and quantities. Raises StoreEventError."""
    event = dict(event or {})
    kind = event.get("kind")
    if kind not in EVENT_KINDS:
        raise StoreEventError("bad_kind", f"kind must be one of {', '.join(EVENT_KINDS)}")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise StoreEventError("bad_event", "event_id is required (a repeated event_id counts once)")
    out = {
        "event_id": event_id.strip(), "kind": kind,
        "batch_code": str(event.get("batch_code") or store.get("batch_code") or ""),
        "qty": _int(event.get("qty"), 1), "at": _when(event.get("at"), now),
        "best_before": event.get("best_before"), "barcode": event.get("barcode"),
        "text": (str(event["text"])[:200] if event.get("text") else None),
    }
    if not out["batch_code"]:
        raise StoreEventError("bad_event", "batch_code is required")
    if kind == "baseline":
        stock = {k: _int(event.get(k), 0, name=k) for k in STOCK_KEYS}
        if not stock["received"]:
            stock["received"] = stock["sold"] + stock["on_shelf"] + stock["held"]
        out["stock"] = stock
        out["qty"] = stock["received"]
    return out


# ---- the store's own stock record --------------------------------------------------

def _stock_after(stock: dict, ev: dict) -> dict:
    """The store's stock record after the event. Counts never go below zero."""
    s = {k: int((stock or {}).get(k, 0)) for k in STOCK_KEYS}
    n, kind = ev["qty"], ev["kind"]
    if kind == "baseline":
        return dict(ev["stock"])
    if kind == "sale":
        s["on_shelf"], s["sold"] = max(0, s["on_shelf"] - n), s["sold"] + n
    elif kind == "quarantine":
        moved = min(n, s["on_shelf"])
        s["on_shelf"], s["held"] = s["on_shelf"] - moved, s["held"] + moved
    elif kind == "customer_return":
        s["sold"], s["held"] = max(0, s["sold"] - n), s["held"] + n
    elif kind == "delivery":
        s["received"], s["on_shelf"] = s["received"] + n, s["on_shelf"] + n
    return s


# ---- the party's ledger ------------------------------------------------------------

def _take(v: dict, sources: tuple, n: int) -> list[tuple[str, int]]:
    """Take up to n tins from the buckets in order; returns [(bucket, qty)]."""
    taken = []
    for src in sources:
        if n <= 0:
            break
        q = min(n, v[src])
        if q:
            v[src] -= q
            n -= q
            taken.append((src, q))
    return taken


def ledger_change(party: dict, ev: dict, *, breach: bool = False) -> tuple[dict, list[dict]]:
    """Pure: the party's buckets and evidence after one event, and the moves
    [{from, to, qty}]. Conservation holds by construction (tins only move)."""
    v = {k: int(party.get(k) or 0) for k in BUCKETS + EVIDENCE}
    moves: list[dict] = []
    n, kind = ev["qty"], ev["kind"]

    def move(sources, to, qty):
        for src, q in _take(v, sources, qty):
            v[to] += q
            moves.append({"from": src, "to": to, "qty": q})

    if kind == "baseline":
        s = ev["stock"]
        v["sold_by_till"] = max(v["sold_by_till"], s["sold"])
        v["held_by_system"] = max(v["held_by_system"], s["held"])
        # Tins the store's records explain are located: first the shelf (the
        # validator needs on_shelf >= system_on_shelf), then sold and held.
        move(("unresolved",), "on_shelf", max(0, s["on_shelf"] - v["on_shelf"]))
        move(("unresolved",), "sold", max(0, s["sold"] - v["sold"]))
        move(("unresolved",), "held", max(0, s["held"] - v["held"]))
        v["system_on_shelf"] = min(s["on_shelf"], v["on_shelf"])
    elif kind == "quarantine":
        move(("on_shelf", "unresolved"), "held", n)
        v["held_by_system"] += n
        v["system_on_shelf"] = max(0, v["system_on_shelf"] - n)
    elif kind == "sale":
        # After a block a tin the ledger called held may be the one sold.
        move(("on_shelf", "unresolved", "held") if breach else ("on_shelf", "unresolved"), "sold", n)
        v["sold_by_till"] += n
        v["system_on_shelf"] = max(0, v["system_on_shelf"] - n)
    elif kind == "customer_return":
        move(("sold", "unresolved"), "held", n)
        v["held_by_system"] += n
    elif kind == "delivery":
        before = v["on_shelf"]
        move(("unresolved",), "on_shelf", n)
        v["system_on_shelf"] += v["on_shelf"] - before
    # blocked_sale: nothing moves.
    v["system_on_shelf"] = min(v["system_on_shelf"], v["on_shelf"])
    return v, moves


def _batch_matches(recall: dict, batch_code: str) -> bool:
    return batch_code == recall.get("batch_code") or batch_code in (recall.get("affected_batches") or [])


def _recount_open_units(db, recall_id: str, session) -> int | None:
    try:
        from ..ledger.reconciler import open_units
        n = open_units(db, recall_id, session)
    except Exception as exc:  # noqa: BLE001 - WS1 owns the formula; the event still lands
        log.warning("open_units recount skipped: %s", exc)
        return None
    db.recalls.update_one({"_id": recall_id}, {"$set": {"open_units": n}}, session=session)
    return n


def _sim_now(db, recall_id: str) -> float | None:
    try:
        from ..clock import sim_now
        return float(sim_now(db, recall_id))
    except Exception:  # noqa: BLE001 - a recall without a run has no clock
        return None


# ---- the barcode and the blocks ------------------------------------------------------

def _verify_barcode(db, recall: dict, store: dict, ev: dict, now: datetime, session) -> list[str] | None:
    """The first till that recognises a scanned tin as the recalled product
    proves the barcode; then the blocks go out to the recall's other
    connected stores. Returns the store system ids newly blocked, or None
    when this scan proved nothing new."""
    if recall.get("barcode_verified"):
        return None
    scanned = ev.get("barcode")
    if scanned and recall.get("barcode") and _digits(scanned) != _digits(recall["barcode"]):
        return None
    db.recalls.update_one({"_id": recall["_id"]}, {"$set": {
        "barcode_verified": True,
        "barcode_verified_by": {"store_system_id": store["_id"], "event_id": ev["event_id"], "at": now}}},
        session=session)
    others = list(db.store_systems.find({"recall_id": recall["_id"], "_id": {"$ne": store["_id"]},
                                          "blocked": False}, {"_id": 1, "simulated": 1}, session=session))
    for other in others:
        # A simulated till applies its block at once, so it is read back at once.
        fields = {"blocked": True, "block_requested_at": now}
        if other.get("simulated", True):
            fields["block_readback_at"] = now
        db.store_systems.update_one({"_id": other["_id"]}, {"$set": fields}, session=session)
    return [o["_id"] for o in others]


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


# ---- post_event ------------------------------------------------------------------------

def post_event(db, store_system_id: str, event: dict, *, client=None, now: datetime | None = None) -> dict:
    """Store one event and apply it, in one transaction (see the module docstring).

    Returns {accepted, duplicate, event_id, kind, store_system_id, recall_id,
    party_id, moves, breach, barcode_verified, blocks_sent, stock, ledger}.
    Raises StoreEventError for an unknown store or a malformed event. A party
    validator refusal returns accepted False with the Atlas error, and
    nothing is written."""
    now = now or utcnow()
    client = client or db.client
    store = db.store_systems.find_one({"_id": store_system_id})
    if store is None:
        raise StoreEventError("unknown_store", f"no store system {store_system_id}")
    ev = normalise_event(event, store, now)
    doc_id = event_doc_id(store_system_id, ev["event_id"])
    out: dict = {}

    def txn(session):
        out.clear()
        if db.store_events.find_one({"_id": doc_id}, {"_id": 1}, session=session) is not None:
            raise _Duplicate()
        s = db.store_systems.find_one({"_id": store_system_id}, session=session)
        recall = None
        if s.get("recall_id"):
            recall = db.recalls.find_one({"_id": s["recall_id"]}, session=session)
        applies = (recall is not None and recall.get("status") == "open" and s.get("party_id")
                   and _batch_matches(recall, ev["batch_code"]))
        ours = ev["batch_code"] == (s.get("batch_code") or ev["batch_code"]) or applies
        stock = _stock_after(s.get("stock") or {}, ev) if ours else dict(s.get("stock") or {})
        breach = bool(ev["kind"] == "sale" and s.get("blocked") and s.get("block_readback_at"))
        record = {"_id": doc_id, "store_system_id": store_system_id, **ev,
                  "recall_id": s.get("recall_id"), "party_id": s.get("party_id"),
                  "breach": breach, "received_at": now, "ledger": None}
        if not ours:
            record["ignored"] = "another batch"
        result = {"moves": [], "breach": breach, "barcode_verified": False, "newly_verified": False, "blocks_sent": [],
                  "open_units": None, "reopened": False}
        if applies:
            party = db.parties.find_one({"recall_id": s["recall_id"], "party_id": s["party_id"]}, session=session)
            if party is not None:
                after, moves = ledger_change(party, ev, breach=breach)
                fields = {**after, "store_system_id": store_system_id, "connected": True,
                          "last_store_event": {"event_id": ev["event_id"], "kind": ev["kind"], "at": ev["at"]}}
                if breach:
                    fields["breach"] = {"event_id": ev["event_id"], "at": ev["at"]}
                    if party.get("state") in CLAIMED_STATES:
                        fields["state"] = REOPENED_STATE
                        result["reopened"] = True
                    sim = _sim_now(db, s["recall_id"])
                    if sim is not None:
                        fields["next_action_at"] = round(sim, 4)
                db.parties.update_one({"_id": party["_id"]}, {"$set": fields}, session=session)
                result["moves"] = moves
                record["ledger"] = {"moves": moves, **{k: after[k] for k in BUCKETS + EVIDENCE}}
                if ev["kind"] == "blocked_sale":
                    sent = _verify_barcode(db, recall, s, ev, now, session)
                    result["newly_verified"] = sent is not None
                    result["blocks_sent"] = sent or []
                    result["barcode_verified"] = bool(recall.get("barcode_verified")) or sent is not None
                if moves or ev["kind"] == "baseline":
                    result["open_units"] = _recount_open_units(db, s["recall_id"], session)
        db.store_events.insert_one(record, session=session)
        db.store_systems.update_one({"_id": store_system_id}, {"$set": {
            "stock": stock, "last_event_at": ev["at"],
            "last_event": {"kind": ev["kind"], "event_id": ev["event_id"], "at": ev["at"], "text": ev.get("text"),
                           "breach": breach}}}, session=session)
        out.update(result, stock=stock, recall_id=s.get("recall_id"), party_id=s.get("party_id"),
                   ledger=record["ledger"], applied=bool(applies))

    base = {"event_id": ev["event_id"], "kind": ev["kind"], "store_system_id": store_system_id}
    try:
        run_txn(client, txn)
    except (_Duplicate, DuplicateKeyError):
        prior = db.store_events.find_one({"_id": doc_id}) or {}
        return {**base, "accepted": True, "duplicate": True, "moves": [], "breach": False,
                "recall_id": prior.get("recall_id"), "party_id": prior.get("party_id")}
    except WriteError as exc:
        if exc.code != VALIDATION_FAILED:
            raise
        detail = {"code": exc.code, "errmsg": str(exc.details.get("errmsg") if exc.details else exc)}
        feed.emit(db, "store_event", f"{store['store_label']}: {ev['kind']} {ev['event_id']} refused by Atlas",
                  recall_id=store.get("recall_id"), party_id=store.get("party_id"), level="error",
                  system=FEED_SYSTEM, detail=detail)
        return {**base, "accepted": False, "duplicate": False, "reason": "refused by the party validator",
                "atlas_error": detail}
    except OperationFailure:
        raise
    _announce(db, store, ev, out)
    return {**base, "accepted": True, "duplicate": False, **out}


class _Duplicate(Exception):
    pass


def _tins(n: int) -> str:
    return f"{n} tin{'' if n == 1 else 's'}"


def _announce(db, store: dict, ev: dict, out: dict) -> None:
    label, kind = store["store_label"], ev["kind"]
    moved = sum(m["qty"] for m in out.get("moves") or [])
    level = "info"
    if kind == "baseline":
        s = out["stock"]
        text = (f"{label} connected: stock record {s['on_shelf']} on the shelf, {s['sold']} sold, "
                f"{s['held']} held")
    elif kind == "quarantine":
        text = f"{label} stock record: {_tins(ev['qty'])} moved to held (event {ev['event_id']})"
    elif kind == "sale" and out.get("breach"):
        level = "error"
        text = (f"BREACH: {label}'s till sold {_tins(ev['qty'])} after the block was read back"
                + ("; the store is reopened" if out.get("reopened") else ""))
    elif kind == "sale":
        text = f"{label} till sold {_tins(ev['qty'])}"
    elif kind == "blocked_sale":
        text = f"{label} till refused a sale: {ev.get('text') or 'recalled batch, not for sale'}"
    elif kind == "customer_return":
        text = f"{label}: a shopper returned {_tins(ev['qty'])}, moved to held"
    else:
        text = f"{label}: delivery of {_tins(ev['qty'])}"
    if out.get("applied") and kind not in ("baseline", "blocked_sale") and not moved:
        text += " (no tin on the ledger to move)"
    feed.emit(db, "store_event", text, recall_id=out.get("recall_id"), party_id=out.get("party_id"), level=level,
              system=FEED_SYSTEM, detail={"store_system_id": store["_id"], "event_id": ev["event_id"], "kind": kind,
                                          "moves": out.get("moves"), "stock": out.get("stock")})
    if out.get("newly_verified"):
        n = len(out["blocks_sent"])
        feed.emit(db, "barcode_verified", f"Barcode verified at {label}'s till; blocks go out to {n} more "
                  f"store{'' if n == 1 else 's'}", recall_id=out.get("recall_id"), party_id=out.get("party_id"),
                  system=FEED_SYSTEM, detail={"store_system_id": store["_id"], "blocks_sent": out["blocks_sent"]})
