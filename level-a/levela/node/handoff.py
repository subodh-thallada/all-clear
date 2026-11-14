"""The handoff of a party's tins to the node that runs its own copy (spec "The handoff").

It reuses the operations pattern of a distributor hold (levela.systems.operations):

1. Write an `operations` document of kind `handoff` in state `intent`, key
   "<recall>:<party>:handoff" (unique, so a second worker finds the first).
2. POST /agent/recalls to the child, signed, with the key. Record `sent`.
3. GET /agent/recalls/{key}. Only a read-back that shows the sub-recall moves
   the operation to `verified`, and the same transaction books the answer:
   the edge from East is settled at the received quantity (110 received
   against 120 sent is a dispute of 10), the party's shipped_in is what it
   received, and all of it starts `unresolved`.
4. An operation left in `intent` or `sent` by a dead worker is resolved by
   reading back first. It is sent again only when the read-back shows
   nothing, with the same key, so the child never gets two sub-recalls.

A child that cannot match the item with confidence answers state
"needs_person": the read-back shows nothing to book, so the operation stays
`sent` (a person on the child's side confirms the item; the next resolve
reads back again).

ctx: any object with db, client, settings, http (an httpx.Client-like
object that reaches the peer: the tests pass a FastAPI TestClient) and
failpoints (None, or a callable(name) that may raise WorkerCrash).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

import httpx

from .. import feed
from ..clock import get_clock, server_now
from ..db.client import run_txn
from ..ledger.transfers import edge_numbers
from ..systems import operations
from . import links
from .book import new_claim_id, refresh_open_units

KIND = "handoff"
FAILPOINT_SENT = "after_handoff_sent"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def key_for(recall_id: str, party_id: str) -> str:
    return operations.idempotency_key(recall_id, party_id, KIND)


def _sim(ctx, recall_id: str) -> float:
    return get_clock(ctx.db, recall_id).sim_now(server_now(ctx.client))


def _units_sent(db, recall_id: str, party: dict) -> int:
    inbound = list(db.edges.find({"recall_id": recall_id, "to_party": party["party_id"]}))
    return sum(e["sender_qty"] for e in inbound) if inbound else int(party.get("shipped_in", 0))


def request_body(ctx, recall: dict, party: dict, link: dict, key: str) -> dict:
    """What the child needs to run its part, and nothing about anyone else."""
    return {"key": key, "recall": recall["_id"], "link": link["_id"], "from_node": ctx.settings.node_id,
            "product": recall.get("product"), "barcode": recall.get("barcode"),
            "batch_code": recall.get("batch_code"), "affected_batches": list(recall.get("affected_batches") or []),
            "action": recall.get("action"), "defect": recall.get("defect"), "checks": recall.get("checks"),
            "units": _units_sent(ctx.db, recall["_id"], party)}


def intent(ctx, party: dict, *, sim: float, run_id: str | None = None, lease_epoch: int | None = None) -> dict:
    db = ctx.db
    recall = db.recalls.find_one({"_id": party["recall_id"]})
    link = links.get(db, party.get("link_id"))
    if link is None:
        raise LookupError(f"party {party['party_id']} has no link to another node")
    now = utcnow()
    key = key_for(party["recall_id"], party["party_id"])
    return {"_id": f"op-{secrets.token_hex(6)}", "recall_id": party["recall_id"], "run_id": run_id, "kind": KIND,
            "order_system": None, "system": "agent", "party_id": party["party_id"], "link_id": link["_id"],
            "peer_node": link["peer_node"], "idempotency_key": key, "adapter_version": None, "state": "intent",
            "lease_epoch": lease_epoch, "intent_at": sim, "sent_at": None, "sends": 0, "readback_at": None,
            "readback_value": None, "request": request_body(ctx, recall, party, link, key), "response": None,
            "hold_id": None, "error": None, "created_at": now, "updated_at": now}


def _outcome(op: dict, action: str, **extra) -> dict:
    return {"op_id": op["_id"], "kind": KIND, "state": op["state"], "action": action, "party_id": op["party_id"],
            "readback_value": op.get("readback_value"), **extra}


# ---------------------------------------------------------------- HTTP to the peer

def _peer(ctx, op: dict) -> tuple[str, str]:
    link = links.get(ctx.db, op["link_id"])
    return link["peer_url"].rstrip("/"), links.link_secret(ctx.settings, link["peer_node"])


def _send(ctx, op: dict) -> dict:
    url, secret = _peer(ctx, op)
    raw = links.body_bytes(op["request"])
    resp = ctx.http.post(f"{url}/agent/recalls", content=raw, headers=links.signed_headers(raw, secret))
    resp.raise_for_status()
    return resp.json()


def _read_back(ctx, op: dict) -> dict | None:
    url, secret = _peer(ctx, op)
    resp = ctx.http.get(f"{url}/agent/recalls/{op['idempotency_key']}",
                        headers=links.signed_headers(b"", secret))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- the operation

def run(ctx, party: dict, *, run_id: str | None = None, lease_epoch: int | None = None,
        sim: float | None = None) -> dict:
    """Hand the party's tins to its node, or carry an earlier handoff on."""
    sim = _sim(ctx, party["recall_id"]) if sim is None else sim
    op, created = operations.open_op(ctx.db, intent(ctx, party, sim=sim, run_id=run_id, lease_epoch=lease_epoch))
    if not created and op["state"] in operations.FINAL_STATES:
        return _outcome(op, "duplicate", duplicate=True)
    return execute(ctx, op, fresh=created, sim=sim)


def resolve(ctx, op: dict, *, sim: float | None = None) -> dict:
    """The adopting worker: read back first, send again only on nothing."""
    return execute(ctx, op, fresh=False, sim=sim)


def open_handoffs(db, recall_id: str | None = None) -> list[dict]:
    q = {"kind": KIND, "state": {"$in": list(operations.OPEN_STATES)}}
    if recall_id:
        q["recall_id"] = recall_id
    return list(db.operations.find(q))


def execute(ctx, op: dict, *, fresh: bool, sim: float | None = None) -> dict:
    db = ctx.db
    current = operations.get(db, op["_id"])
    if current["state"] in operations.FINAL_STATES:
        return _outcome(current, "duplicate")
    sim = _sim(ctx, current["recall_id"]) if sim is None else sim
    if not fresh:
        try:
            seen = _read_back(ctx, current)
        except (httpx.HTTPError, ValueError) as exc:
            return _outcome(current, "deferred", note=f"peer unreachable on read-back: {exc}")
        if seen is not None:
            return _seen(ctx, current, seen, sim)
    try:
        answer = _send(ctx, current)
    except (httpx.HTTPError, ValueError) as exc:
        sent = operations.mark_sent(db, current, sim=sim, adapter_version=None, error=str(exc)) or current
        return _outcome(sent, "sent", note=f"answer lost: {exc}")
    sent = operations.mark_sent(db, current, sim=sim, adapter_version=None, response=answer)
    if sent is None:
        return _outcome(operations.get(db, current["_id"]), "moved")
    fp = getattr(ctx, "failpoints", None)
    if fp is not None:
        fp(FAILPOINT_SENT)
    try:
        seen = _read_back(ctx, sent)
    except (httpx.HTTPError, ValueError) as exc:
        return _outcome(sent, "sent", note=f"read-back failed: {exc}")
    if seen is None:
        return _outcome(sent, "sent", note="read-back shows nothing yet")
    return _seen(ctx, sent, seen, sim)


def _seen(ctx, op: dict, seen: dict, sim: float) -> dict:
    if seen.get("state") != "taken":
        feed.emit(ctx.db, "handoff_waiting", f"{_possessive(_name(ctx.db, op))} agent has the sub-recall but needs a person to "
                  "confirm the item. Nothing booked", recall_id=op["recall_id"], party_id=op["party_id"],
                  level="warn", source="worker", system="agent", sim=sim, detail={"op_id": op["_id"]})
        return _outcome(op, "waiting", note="the child's match needs a person", readback=seen)
    return book_handoff(ctx, op, seen, sim)


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _name(db, op: dict) -> str:
    p = db.parties.find_one({"recall_id": op["recall_id"], "party_id": op["party_id"]}, {"name": 1})
    return (p or {}).get("name") or op["party_id"]


def book_handoff(ctx, op: dict, seen: dict, sim: float) -> dict:
    """One transaction: the op verified by this read-back, the edge settled at
    the received quantity, the party's tins all unresolved, the claim."""
    db, now = ctx.db, utcnow()
    received = int(seen["received"])
    value = {"sub_recall_id": seen["sub_recall_id"], "received": received, "matched_item": seen.get("matched_item"),
             "decided_by": (seen.get("match") or {}).get("decided_by"), "cited": (seen.get("match") or {}).get("cited")}
    rid, pid = op["recall_id"], op["party_id"]
    result = {}

    def txn(session):
        res = db.operations.update_one(
            {"_id": op["_id"], "state": {"$in": list(operations.OPEN_STATES)}},
            {"$set": {"state": "verified", "readback_at": sim, "readback_value": value, "updated_at": now}},
            session=session)
        if res.modified_count != 1:
            result["moved"] = True
            return
        party = db.parties.find_one({"recall_id": rid, "party_id": pid}, session=session)
        inbound = sorted(db.edges.find({"recall_id": rid, "to_party": pid}, session=session), key=lambda e: e["_id"])
        corroborated, disputes = 0, []
        left = received
        for i, e in enumerate(inbound):
            mine = left if i == len(inbound) - 1 else min(left, e["sender_qty"])
            left -= mine
            corr, disp, state = edge_numbers(e["sender_qty"], mine)
            db.edges.update_one({"_id": e["_id"]}, {"$set": {"recipient_qty": mine, "corroborated_qty": corr,
                                                            "disputed_qty": disp, "state": state,
                                                            "settled_by": op["_id"]}}, session=session)
            corroborated += corr
            if disp:
                disputes.append({"edge": e["_id"], "from": e["from_party"], "disputed_qty": disp})
        shipped_in = int(party.get("shipment_qty", 0)) + corroborated if inbound else received
        sub = {**links.empty_sub(), "state": "open", "units_received": received,
               "sub_recall_id": seen["sub_recall_id"]}
        sets = {"shipped_in": shipped_in, "sold": 0, "on_shelf": 0, "held": 0, "returned": 0, "forwarded": 0,
                "unresolved": shipped_in, "held_by_system": 0, "sold_by_till": 0, "system_on_shelf": 0,
                "channel": "agent", "link_id": op["link_id"], "sub": sub, "state": "reached",
                "handoff": {"op_id": op["_id"], **value}, "next_action_at": sim + 12.0, "quiet_step": 0}
        db.parties.update_one({"_id": party["_id"]}, {"$set": sets}, session=session)
        claim_id = new_claim_id()
        db.claims.insert_one({"_id": claim_id, "recall_id": rid, "party_id": pid, "attempt_id": op["_id"],
                              "source": "agent", "dedupe_key": f"{op['link_id']}:handoff", "status": "accepted",
                              "accepted": True, "raw": {"received_qty": received, **value}, "created_at": now},
                             session=session)
        db.tool_events.insert_one({"_id": f"te-{op['_id']}", "attempt_id": op["_id"], "recall_id": rid,
                                   "party_id": pid, "kind": "agent", "source": "agent", "conversation_id": None,
                                   "payload": {"readback": value}, "received_at": now}, session=session)
        result.update(claim_id=claim_id, shipped_in=shipped_in, disputes=disputes,
                      open_units=refresh_open_units(db, rid, session))

    run_txn(ctx.client, txn)
    done = operations.get(db, op["_id"])
    if result.get("moved"):
        return _outcome(done, "duplicate" if done["state"] == "verified" else "moved")
    name = _name(db, op)
    feed.emit(db, "handoff_verified", f"handed {received} tins to {_possessive(name)} agent. Read back verified",
              recall_id=rid, party_id=pid, level="good", source="worker", system="agent", sim=sim,
              detail={"op_id": op["_id"], "readback_value": value})
    for d in result["disputes"]:
        feed.emit(db, "edge_disputed", f"{name} received {received}: {d['disputed_qty']} disputed with {d['from']}",
                  recall_id=rid, party_id=pid, level="bad", source="worker", system="ledger", sim=sim)
    return _outcome(done, "verified", claim_id=result["claim_id"], received=received, disputes=result["disputes"],
                    open_units=result["open_units"])
