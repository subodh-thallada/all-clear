"""When the other node's agent goes quiet (spec "When the agent goes quiet").

Each accepted report sets the party's next_action_at 12 simulated hours
later (levela.node.report). If that timer fires before the next report, the
party walks this ladder, one step per firing (`quiet_step` on the party):

  1  GET /agent/health?ask=report (signed): asks the child for a report.
     An answer means the agent is alive: wait 12 more hours. No answer: step 2.
  2  Phone the business: "your agent has not reported since day N". One
     attempt (key "<recall>:<party>:quiet:<since>"), then wait 24 hours.
  3  Escalate to a person at the food company (a feed line and
     `escalated` on the party).

The party's tins are never touched: they stay open, and the close gate
refuses the recall. The next accepted report resets the ladder (quiet_step 0)
and marks the link active again.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .. import feed
from ..clock import day, get_clock, server_now
from ..orchestrator import attempts
from ..providers.base import ProviderError, ProviderTimeout
from . import links

WAIT_AFTER_HEALTH_H = 12.0
WAIT_AFTER_CALL_H = 24.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def health(settings, http, link: dict, *, ask_report: bool = False) -> dict | None:
    """The peer's /agent/health, or None when it does not answer."""
    secret = links.link_secret(settings, link["peer_node"])
    url = f"{link['peer_url'].rstrip('/')}/agent/health" + ("?ask=report" if ask_report else "")
    try:
        resp = http.get(url, headers=links.signed_headers(b"", secret))
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def due(db, recall_id: str, sim: float) -> list[dict]:
    return list(db.parties.find({"recall_id": recall_id, "channel": "agent", "next_action_at": {"$lte": sim},
                                 "sub.state": {"$ne": "closed"}}))


def step(db, settings, http, voice, party: dict, *, sim: float | None = None, worker: str = "node") -> dict:
    """Walk one step of the ladder for a party whose report is overdue."""
    rid, pid = party["recall_id"], party["party_id"]
    sim = get_clock(db, rid).sim_now(server_now(db.client)) if sim is None else sim
    if party.get("next_action_at") is not None and party["next_action_at"] > sim:
        return {"step": party.get("quiet_step", 0), "action": "not_due"}
    link = links.get(db, party["link_id"])
    since = party.get("last_report_at")
    since_txt = f"day {day(since)}" if since is not None else "the handoff"
    n = int(party.get("quiet_step") or 0) + 1
    if n == 1:
        alive = health(settings, http, link, ask_report=True)
        if alive is not None:
            db.parties.update_one({"_id": party["_id"]}, {"$set": {"next_action_at": sim + WAIT_AFTER_HEALTH_H}})
            return {"step": 1, "action": "asked_for_report", "health": alive}
        db.links.update_one({"_id": link["_id"]}, {"$set": {"status": "quiet"}})
        _set_step(db, party, 1, sim, sim)
        feed.emit(db, "agent_quiet", f"{party['name']}: its agent does not answer /agent/health. Its tins stay open",
                  recall_id=rid, party_id=pid, level="warn", source="worker", system="agent", sim=sim)
        n = 2
    if n == 2:
        call = phone_business(db, voice, party, since_txt=since_txt, sim=sim, worker=worker)
        _set_step(db, party, 2, sim, sim + WAIT_AFTER_CALL_H)
        return {"step": 2, "action": "phoned_business", "attempt": call}
    db.parties.update_one({"_id": party["_id"]}, {"$set": {"quiet_step": 3, "escalated": True,
                                                         "escalated_at": sim, "next_action_at": None}})
    feed.emit(db, "agent_escalated", f"{party['name']}: no report since {since_txt}. Escalated to a person at "
              "Maple Farm Foods. Its tins stay open and the recall cannot close", recall_id=rid, party_id=pid,
              level="bad", source="worker", system="agent", sim=sim)
    return {"step": 3, "action": "escalated"}


def _set_step(db, party: dict, n: int, sim: float, next_at: float) -> None:
    db.parties.update_one({"_id": party["_id"]}, {"$set": {"quiet_step": n, "quiet_since": party.get(
        "quiet_since") or sim, "next_action_at": next_at}})


def phone_business(db, voice, party: dict, *, since_txt: str, sim: float, worker: str) -> dict | None:
    phone = party.get("phone")
    key = f"{party['recall_id']}:{party['party_id']}:quiet:{party.get('last_report_at')}"
    existing = db.attempts.find_one({"idempotency_key": key})
    if existing is not None:
        return {"attempt_id": existing["_id"], "state": existing["state"], "duplicate": True}
    try:
        a = attempts.open_attempt(db, recall_id=party["recall_id"], party_id=party["party_id"], channel="voice",
                                  purpose="agent_quiet", owner=worker, to_number=phone,
                                  extra={"idempotency_key": key})
    except attempts.AttemptRefused:
        return None
    line = f"Your agent has not reported since {since_txt}. Please restart it or call us back."
    state = "completed"
    try:
        placed = voice.place_call(to_number=phone or "", attempt_id=a["_id"], dynamic_variables={
            "party_id": party["party_id"], "purpose": "agent_quiet", "message": line})
        attempts.mark_dialed(db, a["_id"], conversation_id=placed.conversation_id, call_sid=placed.call_sid)
        attempts.complete(db, a["_id"], "completed", reason="message left")
    except ProviderTimeout:
        state = "intent"
    except ProviderError as exc:
        attempts.complete(db, a["_id"], "failed", reason=str(exc))
        state = "failed"
    feed.emit(db, "agent_quiet_call", f"phoned {party['name']}: \"{line}\"", recall_id=party["recall_id"],
              party_id=party["party_id"], attempt_id=a["_id"], level="warn", source="worker", system="phone", sim=sim)
    return {"attempt_id": a["_id"], "state": state}
