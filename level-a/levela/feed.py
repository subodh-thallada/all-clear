"""The event feed the wall shows. Append-only, written by the API, the worker
and the simulator. Text is the line the room reads; `detail` carries raw
evidence (for example the Atlas validation error, verbatim) for the wall to
show underneath.

Kinds in use (the screen in web/ styles lines by kind and level):

  recall_started  recall_closed  close_refused
  worker_started  worker_resumed  worker_stopped  worker_heartbeat_lost
  memory_hit  memory_suspect  memory_note
  attempt_opened  attempt_refused  call_placed  call_failed  call_in_doubt
  attempt_adopted  attempt_abandoned  attempt_closed  attempt_stale
  tool_event  claim_accepted  claim_rejected  claim_duplicate
  party_discovered  edge_opened  edge_disputed  edge_corroborated
  followup_opened  followups_released  needs_human
  failpoint_armed  failpoint_fired  sim_reply  sim_hold  sim_released
  post_call  form_claim  info  error
  store_event  complaint_received  complaint_classified  signal_proposed  signal_approved
  plant_called  plant_answer  agent_handoff  agent_report  buyers_told

`system` tags each line with the outside system it concerns, as the screen
prints it in its left column (spec "What the run looks like"): phone, form,
North orders, East orders, store, stores, signal, agent, ledger, worker,
supervisor, invariants, gate, planner, memory. A line about a simulated party
says so: `simulated` is set from the party's channel by the wall.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("levela.feed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def orders_label(order_system: str) -> str:
    """The screen's name for a distributor's order system: "North orders"."""
    return f"{order_system.capitalize()} orders"


def emit(db, kind: str, text: str, *, recall_id: str | None = None, party_id: str | None = None,
         attempt_id: str | None = None, level: str = "info", detail=None, source: str = "api",
         now: datetime | None = None, system: str | None = None, sim: float | None = None) -> dict:
    """Write one feed line. Never raises: the feed must not break the claim path.

    `system` tags the line for the wall (V2 §12: phone, North orders, order system
    east, ledger, worker, supervisor, invariants, gate, atlas, planner, memory,
    form); when omitted it is derived from `kind`. `sim` is the simulated hour
    the line belongs to; when omitted and the recall has a run, it is read
    from the run's clock.
    """
    doc = {
        "at": now or utcnow(), "kind": kind, "text": text, "level": level, "source": source,
        "recall_id": recall_id, "party_id": party_id, "attempt_id": attempt_id,
        "system": system or SYSTEM_BY_KIND.get(kind, source),
    }
    if sim is None and recall_id:
        try:
            from .clock import get_clock
            sim = get_clock(db, recall_id).sim_now(doc["at"])
        except Exception:  # noqa: BLE001 - a label is never worth failing the write
            sim = None
    if sim is not None:
        from .clock import fmt
        doc["sim"] = round(float(sim), 4)
        doc["sim_label"] = fmt(sim)
    if detail is not None:
        doc["detail"] = detail
    try:
        db.feed.insert_one(doc)
    except Exception as exc:  # noqa: BLE001 - logging only
        log.warning("feed write failed: %s", exc)
    log.info("[%s] %s%s", kind, text, f" ({recall_id})" if recall_id else "")
    doc.pop("_id", None)
    return doc


SYSTEM_BY_KIND = {
    "claim_accepted": "ledger", "claim_rejected": "ledger", "claim_duplicate": "ledger", "close_refused": "ledger",
    "recall_closed": "ledger", "party_discovered": "ledger", "edge_opened": "ledger", "edge_disputed": "ledger",
    "edge_corroborated": "ledger", "attempt_opened": "phone", "attempt_refused": "atlas", "call_placed": "phone",
    "call_failed": "phone", "call_in_doubt": "phone", "attempt_adopted": "worker", "attempt_abandoned": "worker",
    "attempt_closed": "phone", "attempt_stale": "phone", "post_call": "phone", "tool_event": "phone",
    "worker_started": "worker", "worker_resumed": "worker", "worker_stopped": "worker",
    "failpoint_fired": "worker", "failpoint_armed": "worker", "memory_hit": "memory", "memory_suspect": "memory",
    "memory_note": "memory", "form_claim": "form", "sim_reply": "phone", "sim_hold": "phone", "sim_released": "phone",
    "gate_refused": "gate", "gate_stopped": "gate", "invariants": "invariants", "supervisor": "supervisor",
    "planner": "planner", "question_added": "planner", "answer_recorded": "phone",
    "store_event": "store", "complaint_received": "stores", "complaint_classified": "stores",
    "signal_proposed": "signal", "signal_approved": "signal", "plant_called": "phone", "plant_answer": "phone",
    "agent_handoff": "agent", "agent_report": "agent", "buyers_told": "agent", "needs_human": "phone",
    "callback_requested": "phone", "report_received": "phone", "error": "api",
}


def recent(db, recall_id: str | None = None, limit: int = 60) -> list[dict]:
    """Newest first. Lines for this recall plus global lines (recall_id null).
    Each row keeps its `_id` (the wall sends it as the line's stable `id`, so
    two lines with the same time, kind and text stay two lines on screen);
    `_id` also breaks ties between lines written in the same millisecond."""
    q = {} if recall_id is None else {"recall_id": {"$in": [recall_id, None]}}
    rows = list(db.feed.find(q).sort([("at", -1), ("_id", -1)]).limit(limit))
    return rows
