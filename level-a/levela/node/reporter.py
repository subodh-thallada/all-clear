"""The child node's reports to its parent (spec "The report").

A report goes after every change to the child's own totals and at least
every 12 simulated hours, and whenever the parent asked for one (its
quiet-agent ladder calls GET /agent/health?ask=report). Each new report takes
the next sequence number on the link (last_seq_sent). The report is kept on
the link (`pending_report`) before it is sent, so a worker killed before the
answer resends the same seq, and the parent's dedupe_key writes one claim.

The report carries numbers only: buckets, evidence, four buyer numbers and
the party counts. No contact, no name of a buyer, no phone number.

POST /admin/peer/over-report arms `over_report_next` on the link: the next
report claims that many more tins held than its records show (still adding
up to what it received), for test 46 and a judge.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from pymongo import ReturnDocument

from .. import feed
from . import links
from .book import BUCKETS, EVIDENCE

REPORT_EVERY_H = 12.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _edges_open(db, recall_id: str) -> int:
    n = 0
    for e in db.edges.find({"recall_id": recall_id}):
        if e["state"] == "awaiting_recipient":
            n += e["sender_qty"] - e["corroborated_qty"]
        elif e["state"] == "disputed":
            n += e["disputed_qty"]
    return n


def totals(db, recall_id: str) -> dict:
    """The child's own totals: what the report says, without seq and time."""
    recall = db.recalls.find_one({"_id": recall_id})
    rows = list(db.parties.find({"recall_id": recall_id}))
    b = {k: sum(r[k] for r in rows) for k in BUCKETS if k != "forwarded"}
    b["unresolved"] += _edges_open(db, recall_id)
    b["forwarded"] = 0
    ev = {k: sum(int(r.get(k) or 0) for r in rows) for k in EVIDENCE}
    buyers = list(db.buyers.find({}, {"state": 1, "qty": 1}))
    identified_qty = sum(x.get("qty", 1) for x in buyers)
    settled = sum(1 for r in rows if r["unresolved"] == 0 and r["on_shelf"] == 0)
    return {
        "recall": (recall.get("parent") or {}).get("recall_id"),
        "units_received": int(recall.get("received") or 0),
        "buckets": b, "evidence": ev,
        "buyers": {"identified": len(buyers), "told": sum(1 for x in buyers if x["state"] == "told"),
                   "unreachable": sum(1 for x in buyers if x["state"] == "unreachable"),
                   "anonymous_sales": max(b["sold"] - identified_qty, 0)},
        "parties": {"total": len(rows), "reconciled": settled, "open": len(rows) - settled},
        "state": recall["status"],
    }


def _over_report(t: dict, n: int) -> dict:
    """More held than the records show, still adding up. The tins come from
    unresolved, then sold, then returned, and only then from the shelf, so
    the lie is about held (the shelf count stays at the stock record)."""
    b = dict(t["buckets"])
    for k in ("unresolved", "sold", "returned", "on_shelf"):
        take = min(n, b[k])
        b[k] -= take
        b["held"] += take
        n -= take
    return {**t, "buckets": b}


def _link_for(db, recall: dict) -> dict:
    return links.get(db, (recall.get("parent") or {}).get("link_id"))


def due(db, recall_id: str, sim: float) -> bool:
    recall = db.recalls.find_one({"_id": recall_id})
    link = _link_for(db, recall)
    if link.get("pending_report") or link.get("report_requested") or link.get("over_report_next"):
        return True
    if link.get("last_totals") != totals(db, recall_id):
        return True
    if recall["status"] == "closed":
        return False             # its closed report was accepted: nothing more to say
    return link.get("last_sent_sim") is None or sim - link["last_sent_sim"] >= REPORT_EVERY_H


def send(db, settings, http, recall_id: str, *, sim: float, force: bool = False, now: datetime | None = None) -> dict:
    """Send the next report if one is due (or force). Never raises on the
    network: an unreachable parent keeps the report pending."""
    now = now or utcnow()
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None or recall.get("match_state") != "taken":
        return {"sent": False, "reason": "no sub-recall to report on"}
    link = _link_for(db, recall)
    if not force and not due(db, recall_id, sim):
        return {"sent": False, "reason": "nothing changed"}
    report = link.get("pending_report")
    if report is None:
        t = totals(db, recall_id)
        over = int(link.get("over_report_next") or 0)
        body = _over_report(t, over) if over else t
        link = db.links.find_one_and_update({"_id": link["_id"]}, {"$inc": {"last_seq_sent": 1}},
                                            return_document=ReturnDocument.AFTER)
        report = {"link": link["_id"], "seq": link["last_seq_sent"], **body, "at": round(float(sim), 4)}
        db.links.update_one({"_id": link["_id"]}, {"$set": {"pending_report": report, "pending_totals": t,
                                                           "over_report_next": 0, "report_requested": False}})
    secret = links.link_secret(settings, link["peer_node"])
    raw = links.body_bytes(report)
    try:
        resp = http.post(f"{link['peer_url'].rstrip('/')}/agent/reports", content=raw,
                         headers=links.signed_headers(raw, secret))
        reply = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except (httpx.HTTPError, ValueError) as exc:
        return {"sent": False, "seq": report["seq"], "reason": f"parent unreachable: {exc}"}
    if resp.status_code >= 500 or resp.status_code == 401:
        return {"sent": False, "seq": report["seq"], "status": resp.status_code, "reply": reply}
    accepted = bool(reply.get("accepted"))
    current = db.links.find_one({"_id": link["_id"]})
    db.links.update_one({"_id": link["_id"]}, {"$set": {
        "pending_report": None, "last_reply": reply, "last_sent_sim": sim, "last_seen_at": now,
        # A refused report is sent again, truthfully, at the next tick.
        "last_totals": current.get("pending_totals") if accepted else None}})
    feed.emit(db, "agent_report_sent", f"report {report['seq']} to {link['peer_node']}: "
              + ("accepted" if accepted else f"refused ({reply.get('rule')}: {reply.get('values')})"),
              recall_id=recall_id, level="info" if accepted else "bad", source="worker", system="agent", sim=sim)
    return {"sent": True, "seq": report["seq"], "accepted": accepted, "reply": reply, "report": report}
