"""A child node's report, booked as a claim on our ledger (spec "The report").

    book_report(db, *, recall_id, party_id, report) -> {"accepted", "claim_id", "rule", "values"}

The report is the JSON of the spec: link, seq, recall, units_received,
buckets, evidence, buyers (four numbers), parties, state, at. The signature
is checked by the API before this runs (levela.api.node); WS2's simulator
calls this directly for Summit Foods.

1. The sequence number: the claim's dedupe_key is "<link>:<seq>", unique on
   `claims`, so the same seq twice writes one claim (the second returns the
   first, accepted or not). A seq at or below the link's last accepted one
   is ignored: nothing is written.
2. One transaction (levela.node.book) inserts the claim with source "agent"
   and sets the party's buckets, evidence numbers and `sub` from the report,
   advances the link's last_seq_accepted and recomputes open_units.
3. Atlas accepts or rejects the party update: conservation against the
   tins booked at the handoff, held against held_by_system, sold against
   sold_by_till, on_shelf against system_on_shelf, and sub.state "closed"
   only with nothing on a shelf or unresolved. A rejected report is stored
   as a rejected claim naming the rule and the two values.

Only whitelisted numbers are kept from a report, so nothing a child sends
beyond the spec's fields (a buyer's contact, say) can land in our database.
An accepted report sets next_action_at 12 simulated hours ahead: the
quiet-agent ladder's timer (levela.node.quiet).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import feed
from ..clock import get_clock, server_now
from . import links
from .book import BUCKETS, EVIDENCE, Refused, book

REPORT_EVERY_H = 12.0
BUYER_NUMBERS = ("identified", "told", "unreachable", "anonymous_sales")
SUB_STATES = ("open", "closed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BadReport(Exception):
    pass


def _int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        raise BadReport(f"{name} must be a whole number of tins, not {v!r}")
    if v < 0 or v > 10**9:
        raise BadReport(f"{name} {v} is not a count")
    return v


def clean(report: dict) -> dict:
    """The report reduced to the spec's numbers and words (raises BadReport)."""
    if not isinstance(report, dict):
        raise BadReport("a report is a JSON object")
    b, e = report.get("buckets") or {}, report.get("evidence") or {}
    buyers, parties = report.get("buyers") or {}, report.get("parties") or {}
    state = report.get("state") or "open"
    if state not in SUB_STATES:
        raise BadReport(f"state must be open or closed, not {state!r}")
    out = {
        "link": str(report.get("link") or ""), "seq": _int(report.get("seq"), "seq"),
        "recall": str(report.get("recall") or ""),
        "units_received": _int(report.get("units_received", 0), "units_received"),
        "buckets": {k: _int(b.get(k, 0), k) for k in BUCKETS},
        "evidence": {k: _int(e.get(k, 0), k) for k in EVIDENCE},
        "buyers": {k: _int(buyers.get(k, 0), f"buyers.{k}") for k in BUYER_NUMBERS},
        "parties": {k: _int(parties.get(k, 0), f"parties.{k}") for k in ("total", "reconciled", "open")},
        "state": state,
    }
    at = report.get("at")
    out["at"] = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    return out


def _sim(db, recall_id: str) -> float:
    return get_clock(db, recall_id).sim_now(server_now(db.client))


def book_report(db, *, recall_id: str, party_id: str, report: dict, sim: float | None = None,
                now: datetime | None = None) -> dict:
    now = now or utcnow()
    party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id})
    if party is None:
        return {"accepted": False, "claim_id": None, "rule": "unknown_party", "values": [party_id, None]}
    try:
        r = clean(report)
    except BadReport as exc:
        return {"accepted": False, "claim_id": None, "rule": "bad_report", "values": [str(exc), None]}
    link_id = r["link"] or party.get("link_id")
    link = links.get(db, link_id) if link_id else None
    if link is None or (party.get("link_id") and party["link_id"] != link_id):
        return {"accepted": False, "claim_id": None, "rule": "unknown_link", "values": [link_id, party.get("link_id")]}
    seq, key = r["seq"], f"{link_id}:{r['seq']}"
    prior = db.claims.find_one({"dedupe_key": key})
    if prior is not None:
        return {"accepted": bool(prior.get("accepted")), "claim_id": prior["_id"], "rule": prior.get("rule"),
                "values": prior.get("values"), "duplicate": True}
    last = int(link.get("last_seq_accepted") or 0)
    if seq <= last:
        return {"accepted": False, "claim_id": None, "rule": "stale_seq", "values": [seq, last], "ignored": True}

    sim = _sim(db, recall_id) if sim is None else sim
    closed = r["state"] == "closed"
    sub = {"seq": seq, "parties_total": r["parties"]["total"], "parties_open": r["parties"]["open"],
           "state": r["state"], "buyers": r["buyers"], "units_received": r["units_received"], "at": r["at"]}
    settled = r["buckets"]["on_shelf"] == 0 and r["buckets"]["unresolved"] == 0
    inbound_open = db.edges.count_documents({"recall_id": recall_id, "to_party": party_id,
                                             "state": {"$ne": "corroborated"}})
    state = ("reconciled" if closed and settled and not inbound_open else "claimed" if closed else "reached")
    sets = {**r["buckets"], **r["evidence"], "sub": sub, "state": state, "last_report_at": sim,
            "next_action_at": sim + REPORT_EVERY_H, "quiet_step": 0, "quiet_since": None}
    claim = {"dedupe_key": key, "source": "agent", "attempt_id": f"rpt-{link_id}-{seq}", "link_id": link_id,
             "seq": seq, "raw": r, "numbers": {**r["buckets"], **r["evidence"]}}
    evidence = {"kind": "agent", "source": "agent", "conversation_id": None,
                "payload": {"link": link_id, "seq": seq, "report": r}}

    def advance_link(session):
        res = db.links.update_one({"_id": link_id, "last_seq_accepted": {"$lt": seq}},
                                  {"$set": {"last_seq_accepted": seq, "last_seen_at": now, "status": "active"}},
                                  session=session)
        if res.modified_count != 1:
            raise Refused("stale_seq", [seq, last])

    out = book(db.client, db, recall_id=recall_id, party_id=party_id, sets=sets, claim=claim, evidence=evidence,
               extra=advance_link, now=now)
    name = party.get("name") or party_id
    if out["accepted"]:
        b = r["buckets"]
        text = (f"{name} report {seq}: {r['units_received']} tins, {b['on_shelf']} on a shelf, "
                f"{b['unresolved']} unresolved. Buyers told {r['buyers']['told']} of {r['buyers']['identified']}. "
                f"Names not shared" + (". Its sub-recall is closed" if closed else ""))
        feed.emit(db, "agent_report", text, recall_id=recall_id, party_id=party_id, level="good" if closed else "info",
                  source="api", system="agent", sim=sim, detail={"seq": seq, "claim_id": out["claim_id"]})
    else:
        a, b = (out.get("values") or [None, None]) + [None, None][len(out.get("values") or []):]
        feed.emit(db, "agent_report_rejected", f"{name} report {seq} refused: {out['rule']} ({a} against {b})",
                  recall_id=recall_id, party_id=party_id, level="bad", source="api", system="atlas", sim=sim,
                  detail={"seq": seq, "claim_id": out["claim_id"], "rule": out["rule"], "values": out["values"]})
    return out
