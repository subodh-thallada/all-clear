"""The child node's side of a sub-recall: Summit Foods' copy of the agent.

    seed_node()        Summit's records into its own database: the item file
                       (episodes of kind "item"), its buyers, its link to us
    take_sub_recall()  POST /agent/recalls: once per key, match the item,
                       create its own recall (parent set) and its ledger from
                       its receiving records; answer with the tins received
    read_back()        GET /agent/recalls/{key}
    store_event()      a store's till or stock record moving one tin (or the
                       connector's baseline), with the ledger change, in one
                       transaction (WS4's store_events shape)
    staff_claim()      a store manager's count, judged by the same parties
                       validator in Summit's database: the till outranks the staff
    tick()             Summit's scripted run up to a simulated hour: tills
                       read, tins pulled, the Over-reporter's "all pulled",
                       the last tins pulled, its own close

Summit's ledger: the depot is one party of type `self` (received 120: 18
held on its own stock record, 22 returned to East, 80 forwarded on
corroborated edges to its six stores); the six stores are parties of type
`store`, all connected, and start unresolved until their connector's
baseline is read. A low-confidence match books nothing: the recall is
stored with match_state "needs_person", units 0 and no parties, and a feed
line asks a person to confirm the item.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from .. import feed
from ..config import SCENARIO_DIR
from ..db.client import run_txn
from ..ledger.transfers import edge_id, ledger_fields, v2_fields
from . import links, match as matching
from .book import book, refresh_open_units

SUMMIT_FILE = SCENARIO_DIR / "summit.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_scenario(path=None) -> dict:
    p = path or SUMMIT_FILE
    if not p.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("summit_scenario", SCENARIO_DIR / "summit.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.build_summit()
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- seeding

def seed_node(db, scn: dict, embedder, *, parent_url: str = "", now: datetime | None = None) -> dict:
    """Summit's own records into its database. Safe to re-run."""
    now = now or utcnow()
    node_id = scn["node"]["node_id"]
    n_items = matching.load_items(db, embedder, scn["items"], node_id=node_id)
    for b in scn["buyers"]:
        db.buyers.update_one({"_id": b["_id"]}, {"$setOnInsert": {
            "_id": b["_id"], "store_id": b["store_id"], "contact": b["contact"], "bought_at": str(b["bought_at"]),
            "qty": b["qty"], "state": "pending", "attempt_id": None, "purchase_id": b["purchase_id"]}}, upsert=True)
    p = scn["parent"]
    link = links.ensure_link(db, links.link_doc(link_id=p["link_id"], role="child", peer_node=p["node_id"],
                                                peer_url=parent_url, match=p["match"], status="active", now=now))
    if parent_url and link.get("peer_url") != parent_url.rstrip("/"):
        db.links.update_one({"_id": link["_id"]}, {"$set": {"peer_url": parent_url.rstrip("/")}})
    return {"items": n_items, "buyers": len(scn["buyers"]), "link": link["_id"]}


# ---------------------------------------------------------------- taking a sub-recall

def child_recall_id(node_id: str, parent_recall: str) -> str:
    return f"{node_id}-{parent_recall}"


def summary(recall: dict | None) -> dict | None:
    """What the parent reads back: never a buyer, never a phone number."""
    if recall is None:
        return None
    m = recall.get("match") or {}
    return {"key": (recall.get("parent") or {}).get("key"), "sub_recall_id": recall["_id"],
            "state": recall.get("match_state", "taken"), "matched_item": recall.get("matched_item"),
            "match": {"decided_by": m.get("decided_by"), "cited": m.get("cited", []),
                      "confidence": m.get("confidence"), "reason": m.get("reason")},
            "received": recall.get("received"), "status": recall.get("status")}


def read_back(db, key: str) -> dict | None:
    return summary(db.recalls.find_one({"parent.key": key}))


def _received(scn: dict) -> int:
    return int(scn.get("received", scn["depot"]["received"]))


def take_sub_recall(client, db, scn: dict, embedder, body: dict, *, sim: float = 0.0,
                    now: datetime | None = None) -> dict:
    """Create Summit's own recall for the parent's key, once. A second call
    with the same key returns the first answer and writes nothing."""
    now = now or utcnow()
    key = str(body.get("key") or "")
    if not key:
        raise ValueError("a sub-recall needs its handoff key")
    found = read_back(db, key)
    if found is not None:
        return {**found, "duplicate": True}
    node_id = scn["node"]["node_id"]
    rid = child_recall_id(node_id, str(body.get("recall") or key.split(":")[0]))
    m = matching.match(db, embedder, {"barcode": body.get("barcode"), "product": body.get("product")})
    batch = body.get("batch_code") or (body.get("affected_batches") or [None])[0]
    recall = {
        "_id": rid, "product": body.get("product") or "", "barcode": body.get("barcode"), "batch_code": batch,
        "affected_batches": list(body.get("affected_batches") or [batch]), "action": body.get("action"),
        "barcode_verified": bool(m["decided_by"] == "barcode"), "defect": body.get("defect"),
        "checks": body.get("checks") if isinstance(body.get("checks"), list) else None,
        "source": "manual", "source_signal": None,
        "parent": {"link_id": str(body.get("link") or scn["parent"]["link_id"]), "recall_id": str(body.get("recall")),
                   "key": key, "node": str(body.get("from_node") or scn["parent"]["node_id"]),
                   "units_sent": body.get("units")},
        "matched_item": m["item_id"], "match": {k: m.get(k) for k in ("decided_by", "cited", "confidence",
                                                                     "margin", "mode", "reason")},
        "created_at": now, "status": "open",
    }
    if m["needs_person"]:
        recall.update(match_state="needs_person", received=None, units_total=0, open_units=0)
        try:
            db.recalls.insert_one(recall)
        except DuplicateKeyError:
            return {**read_back(db, key), "duplicate": True}
        feed.emit(db, "match_needs_person", f"Sub-recall {key}: no confident match for \"{recall['product']}\". "
                  f"A person must confirm the item ({m.get('reason')})", recall_id=rid, level="warn",
                  source="api", system="agent", sim=sim, detail={"cited": m["cited"]})
        return read_back(db, key)

    received = _received(scn)
    depot, stores = scn["depot"], scn["stores"]
    delivered = sum(s["received"] for s in stores)
    returned = depot["returned"]
    held = received - returned - delivered
    recall.update(match_state="taken", received=received, units_total=received, open_units=received)
    product = {"batch_code": batch}

    def party(pid, name, *, type_, tier, shipped_in, **over):
        doc = {"_id": f"{rid}:{pid}", "recall_id": rid, "party_id": pid, "name": name, "type": type_,
               "tier": tier, "channel": "sim", "state": "pending", **ledger_fields(shipped_in, product),
               **v2_fields(tier=tier, found_via="shipment", next_action_at=sim)}
        doc.update(over)
        return doc

    rows = [party(depot["party_id"], depot["name"], type_="self", tier=1, shipped_in=received, held=held,
                  held_by_system=held, returned=returned, forwarded=delivered, unresolved=0, state="reached")]
    edges = []
    for s in stores:
        rows.append(party(s["party_id"], s["name"], type_="store", tier=2, shipped_in=s["received"],
                          store_system_id=s["store_system_id"], connected=True, profile=s["profile"]))
        edges.append({"_id": edge_id(rid, depot["party_id"], s["party_id"]), "recall_id": rid,
                      "from_party": depot["party_id"], "to_party": s["party_id"], "sender_qty": s["received"],
                      "recipient_qty": s["received"], "corroborated_qty": s["received"], "disputed_qty": 0,
                      "state": "corroborated", "source": "delivery_records", "created_at": now})

    def txn(session):
        db.recalls.insert_one(recall, session=session)
        db.parties.insert_many(rows, session=session)
        if edges:
            db.edges.insert_many(edges, session=session)
        refresh_open_units(db, rid, session)

    try:
        run_txn(client, txn)
    except DuplicateKeyError:
        return {**read_back(db, key), "duplicate": True}
    how = (f"by barcode ({m['cited'][0]})" if m["decided_by"] == "barcode"
           else f"by description, citing {', '.join(m['cited'])}")
    feed.emit(db, "sub_recall_taken", f"Took sub-recall {key}: matched {m['item_id']} {how}. "
              f"{received} tins received", recall_id=rid, level="good", source="api", system="agent", sim=sim)
    return read_back(db, key)


# ---------------------------------------------------------------- store records

def _store(scn: dict, store_id: str) -> dict:
    return next(s for s in scn["stores"] if s["store_id"] == store_id)


def store_event(client, db, recall_id: str, store: dict, kind: str, *, event_id: str, sim: float,
                now: datetime | None = None) -> dict:
    """One event from a store's till or stock record and its ledger change, in
    one transaction. A repeated event_id counts once."""
    now = now or utcnow()
    ssid, pid = store["store_system_id"], store["party_id"]
    if kind not in ("baseline", "quarantine", "sale"):
        raise ValueError(f"store event {kind!r} is not booked by the node")

    def txn(session):
        if db.store_events.find_one({"store_system_id": ssid, "event_id": event_id}, session=session):
            return {"duplicate": True}
        p = db.parties.find_one({"recall_id": recall_id, "party_id": pid}, session=session)
        if kind == "baseline":
            sold, on_shelf = store["sold"], store["on_shelf_at_recall"]
            update = {"$set": {"sold": sold, "on_shelf": on_shelf, "sold_by_till": sold,
                               "system_on_shelf": on_shelf, "baseline_at": sim,
                               "unresolved": p["shipped_in"] - p["held"] - p["returned"] - p["forwarded"]
                               - sold - on_shelf}}
            qty = on_shelf
        elif kind == "quarantine":
            update = {"$inc": {"on_shelf": -1, "held": 1, "held_by_system": 1, "system_on_shelf": -1}}
            qty = 1
        else:
            update = {"$inc": {"on_shelf": -1, "sold": 1, "sold_by_till": 1, "system_on_shelf": -1}}
            qty = 1
        db.parties.update_one({"_id": p["_id"]}, update, session=session)
        db.store_events.insert_one({"store_system_id": ssid, "event_id": event_id, "kind": kind,
                                    "batch_code": p.get("batch_code") or "", "qty": qty, "at": now,
                                    "recall_id": recall_id, "party_id": pid, "sim": sim}, session=session)
        refresh_open_units(db, recall_id, session)
        return {"duplicate": False}

    return run_txn(client, txn)


def staff_claim(client, db, recall_id: str, store_id: str, counts: dict, *, label: str,
                now: datetime | None = None) -> dict:
    """A store manager's count. The same parties validator judges it in
    Summit's database: on_shelf below the store's stock record is refused."""
    party = db.parties.find_one({"recall_id": recall_id, "party_id": store_id})
    c = {k: int(counts.get(k, 0)) for k in ("sold", "on_shelf", "held", "returned")}
    sets = {**c, "unresolved": party["shipped_in"] - party["forwarded"] - sum(c.values())}
    for k in ("batch_seen", "sold_source", "block_test"):
        if k in counts:
            sets[k] = counts[k]
    digest = hashlib.sha1(json.dumps(sets, sort_keys=True).encode()).hexdigest()[:10]
    attempt_id = f"staff-{store_id}-{label}"
    claim = {"dedupe_key": f"{recall_id}:{store_id}:{label}:{digest}", "source": "sim", "attempt_id": attempt_id,
             "raw": {**counts}, "numbers": c}
    evidence = {"kind": "tool_call", "source": "sim", "conversation_id": None, "payload": {"counts": counts}}
    out = book(client, db, recall_id=recall_id, party_id=store_id, sets=sets, claim=claim, evidence=evidence,
               now=now)
    if not out["accepted"] and not out.get("duplicate"):
        a, b = out["values"] or [None, None]
        feed.emit(db, "claim_rejected", f"{party['name']}: \"all pulled\" refused, {out['rule']} ({a} against {b})",
                  recall_id=recall_id, party_id=store_id, level="bad", source="api", system="atlas")
    return out


def tins_open(db, recall_id: str) -> dict:
    rows = list(db.parties.find({"recall_id": recall_id}))
    return {"on_shelf": sum(r["on_shelf"] for r in rows), "unresolved": sum(r["unresolved"] for r in rows),
            "stores_with_tins_on_shelf": sum(1 for r in rows if r["on_shelf"])}


# ---------------------------------------------------------------- Summit's scripted run

def tick(client, db, scn: dict, recall_id: str, sim: float) -> dict:
    """Everything Summit's stores do up to simulated hour `sim` (hours since
    the sub-recall opened), each step once: tills read at once (baseline),
    staff pull each store's tins at its pull hour (one quarantine event a
    tin), the Over-reporter keeps 9 on the shelf, says "all pulled" on day 5
    (refused) and pulls the last 9 on day 9; then Summit closes its recall."""
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None or recall.get("match_state") != "taken":
        return {"events": 0}
    events, claims = 0, []
    for s in scn["stores"]:
        r = store_event(client, db, recall_id, s, "baseline", event_id=f"{recall_id}:{s['store_id']}:baseline",
                        sim=sim)
        events += not r["duplicate"]
        late = (s.get("late_pull") or {}).get("qty", 0)
        if sim >= s["pull_at"]:
            for n in range(s["on_shelf_at_recall"] - late):
                r = store_event(client, db, recall_id, s, "quarantine",
                                event_id=f"{recall_id}:{s['store_id']}:q{n + 1}", sim=sim)
                events += not r["duplicate"]
        if s.get("all_pulled_claim_at") is not None and sim >= s["all_pulled_claim_at"]:
            claims.append(staff_claim(client, db, recall_id, s["store_id"], {
                "sold": s["sold"], "on_shelf": 0, "held": s["on_shelf_at_recall"], "returned": 0,
                "batch_seen": recall["batch_code"], "block_test": True, "sold_source": "till_report"},
                label="all-pulled"))
        if late and sim >= s["late_pull"]["at"]:
            for n in range(s["on_shelf_at_recall"] - late, s["on_shelf_at_recall"]):
                r = store_event(client, db, recall_id, s, "quarantine",
                                event_id=f"{recall_id}:{s['store_id']}:q{n + 1}", sim=sim)
                events += not r["duplicate"]
    _settle_states(db, recall_id)
    closed = None
    left = tins_open(db, recall_id)
    if left["on_shelf"] == 0 and left["unresolved"] == 0 and recall["status"] == "open":
        closed = close(db, recall_id)
    return {"events": events, "claims": claims, "open": left, "closed": closed}


def _settle_states(db, recall_id: str) -> None:
    db.parties.update_many({"recall_id": recall_id, "unresolved": 0, "on_shelf": 0},
                           {"$set": {"state": "reconciled"}})
    db.parties.update_many({"recall_id": recall_id, "state": "pending", "unresolved": 0},
                           {"$set": {"state": "reached"}})


def close(db, recall_id: str) -> dict:
    """Summit closes its own recall through its own close gate."""
    from ..ledger.reconciler import try_close
    res = try_close(db, recall_id)
    feed.emit(db, "recall_closed" if res.get("closed") else "close_refused",
              "Summit's sub-recall is closed" if res.get("closed") else f"Close refused: {res.get('reason')}",
              recall_id=recall_id, level="good" if res.get("closed") else "bad", source="worker", system="ledger")
    return res
