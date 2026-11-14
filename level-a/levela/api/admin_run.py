"""/admin/* controls of the long run (V2 §10, spec "Admin endpoints added"):

  POST /admin/questions    add a question for every matching party
  GET  /admin/questions    the recall's questions with open/answered counts
  POST /admin/clock        speed, pause, resume or start the run's clock
  POST /admin/order-system       switch a mock order system to v2, or arm one of its faults
  POST /admin/supervisor   pause or resume random kills
  POST /admin/saboteur     run the rule-breaking worker for a few seconds

These write to the run document and the feed; none of them touches the
ledger. The supervisor and the saboteur read their switches from the run.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import clock, feed, runs
from ..clock import Clock
from . import order_admin, questions
from .deps import jsonable, require_admin, sim_hour

log = logging.getLogger("levela.api.admin_run")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class QuestionIn(BaseModel):
    recall_id: str
    text: str
    answer_type: str = "yes_no_count"
    applies_to: dict = {}


class ClockIn(BaseModel):
    recall_id: str
    seconds_per_sim_hour: float | None = Field(None, gt=0)
    action: Literal["start", "pause", "resume"] | None = None


class OrderSystemIn(BaseModel):
    order_system: str
    action: Literal["switch_v2", "fault"]
    fault: str | None = None
    count: int = Field(1, ge=1, le=100)


class SupervisorIn(BaseModel):
    recall_id: str
    paused: bool
    reason: str | None = None


class SaboteurIn(BaseModel):
    recall_id: str
    on: bool
    seconds: float = Field(20.0, gt=0, le=3600)


def _run(db, recall_id: str) -> dict:
    run = runs.get_run(db, recall_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run for recall {recall_id}")
    return run


# ---- questions ----------------------------------------------------------------

@router.post("/questions")
def add_question(body: QuestionIn, request: Request) -> dict:
    """One document and one update (spec "Questions as data"); wakes the planner."""
    st = request.app.state
    recall = st.db.recalls.find_one({"_id": body.recall_id}, {"status": 1})
    if recall is None:
        raise HTTPException(status_code=404, detail=f"unknown recall {body.recall_id}")
    if recall["status"] != "open":
        raise HTTPException(status_code=409, detail=f"recall {body.recall_id} is closed")
    try:
        doc = questions.add_question(st.db, recall_id=body.recall_id, text=body.text, answer_type=body.answer_type,
                                     applies_to=body.applies_to, sim_now=sim_hour(st, body.recall_id), by="admin")
    except questions.QuestionError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    runs.wake_planner(st.db, body.recall_id, f"question {doc['question_id']} added")
    st.wall_cache.clear()
    return jsonable(doc)


@router.get("/questions")
def list_questions(recall_id: str, request: Request) -> dict:
    return jsonable({"recall_id": recall_id, "questions": questions.list_questions(request.app.state.db, recall_id)})


# ---- clock --------------------------------------------------------------------

def _next_clock(current: Clock, body: ClockIn, real_now) -> Clock:
    """Speed first (re-anchored, so simulated time never jumps), then the action.
    "start" runs the clock from now at the current simulated time."""
    c = current.with_speed(body.seconds_per_sim_hour, real_now) if body.seconds_per_sim_hour else current
    if body.action == "pause":
        return c.paused(real_now)
    if body.action == "resume":
        return c.resumed(real_now)
    if body.action == "start":
        return Clock(real_start=real_now, sim_start=c.sim_now(real_now), seconds_per_sim_hour=c.seconds_per_sim_hour)
    return c


@router.post("/clock")
def set_clock(body: ClockIn, request: Request) -> dict:
    st = request.app.state
    if body.seconds_per_sim_hour is None and body.action is None:
        raise HTTPException(status_code=400, detail="give seconds_per_sim_hour, action, or both")
    run = _run(st.db, body.recall_id)
    real_now = clock.server_now(st.client)
    new = _next_clock(Clock.from_doc(run.get("clock")), body, real_now)
    stored = runs.set_clock(st.db, body.recall_id, new)
    # Report the clock as stored (BSON dates keep milliseconds): it is what every process reads.
    new = Clock.from_doc(stored["clock"]) if stored else new
    sim = new.sim_now(real_now)
    order_systems = _push_speed(st, body.recall_id, new.seconds_per_sim_hour) if body.seconds_per_sim_hour else {}
    _announce_clock(st.db, body, new, sim)
    st.wall_cache.clear()
    return jsonable({"recall_id": body.recall_id, "clock": new.to_doc(), "sim_now": sim, "sim_label": clock.fmt(sim),
                     "paused": new.paused_at is not None, "order_systems": order_systems})


def _push_speed(st, recall_id: str, seconds_per_sim_hour: float) -> dict:
    """The recall's order systems time their stale reads in simulated minutes. A
    order system that is down keeps its old speed; the reply says so."""
    out = {}
    for order_system in st.db.parties.distinct("order_system", {"recall_id": recall_id, "order_system": {"$type": "string"}}):
        url = st.settings.order_urls.get(order_system)
        reply = (order_admin.set_speed(st.http, url, st.settings.admin_token, seconds_per_sim_hour) if url
                 else order_admin.OrderReply(ok=False, error="no URL configured for this order system"))
        out[order_system] = reply.view()
    return out


def _announce_clock(db, body: ClockIn, new: Clock, sim: float) -> None:
    parts = []
    if body.seconds_per_sim_hour:
        parts.append(f"{new.seconds_per_sim_hour:g} s per simulated hour")
    if body.action == "pause":
        parts.append(f"paused at {clock.fmt(sim)}")
    elif body.action in ("resume", "start"):
        parts.append(f"running from {clock.fmt(sim)}")
    feed.emit(db, "clock_changed", "Clock: " + ", ".join(parts), source="admin", recall_id=body.recall_id,
              system="supervisor", sim=sim, detail={"action": body.action, "clock": new.to_doc()})


# ---- order systems ------------------------------------------------------------------

@router.post("/order-system")
def order_system(body: OrderSystemIn, request: Request) -> dict:
    """Proxy the operator's order system buttons to the mock order system's admin API.

    A switch also makes each open recall's distributor on that order system due
    now with a contract probe owed (order_admin.owe_probe), so the change
    is detected within seconds even when every one of its accounts is
    reconciled and no order system call was otherwise due for a day or ever."""
    st = request.app.state
    url = st.settings.order_urls.get(body.order_system)
    if url is None:
        raise HTTPException(status_code=400, detail=f"unknown order system {body.order_system!r}; "
                                                    f"choose one of {sorted(st.settings.order_urls)}")
    if body.action == "fault" and body.fault not in order_admin.FAULTS:
        raise HTTPException(status_code=400, detail=f"fault must be one of {list(order_admin.FAULTS)}")
    token = st.settings.admin_token
    if body.action == "switch_v2":
        reply = order_admin.switch_version(st.http, url, token)
        line = f"{feed.orders_label(body.order_system)} switched to v2 by the operator"
    else:
        reply = order_admin.arm_fault(st.http, url, token, body.fault, body.count)
        line = f"{feed.orders_label(body.order_system)}: fault {body.fault} armed ({body.count})"
    recall_ids = _order_system_recalls(st.db, body.order_system)
    if not reply.ok:
        for rid in recall_ids or [None]:
            feed.emit(st.db, "order_system_admin_failed", f"{feed.orders_label(body.order_system)}: {body.action} failed, "
                      f"{reply.error}", level="bad", source="admin", recall_id=rid,
                      system=feed.orders_label(body.order_system), detail=reply.view())
        raise HTTPException(status_code=502, detail=f"{feed.orders_label(body.order_system)}: {reply.error}")
    probes = {}
    if body.action == "switch_v2":
        _mark_switched(st.db, recall_ids, body.order_system, clock.server_now(st.client))
        probes = {rid: order_admin.owe_probe(st.db, rid, body.order_system, sim_hour(st, rid)) for rid in recall_ids}
    for rid in recall_ids or [None]:
        feed.emit(st.db, "order_system_switched" if body.action == "switch_v2" else "order_system_fault", line, level="warn",
                  source="admin", recall_id=rid, system=feed.orders_label(body.order_system),
                  detail={"action": body.action, "fault": body.fault, "count": body.count, "order_system_reply": reply.body,
                          "probes_owed": probes.get(rid)})
    st.wall_cache.clear()
    return jsonable({"order_system": body.order_system, "action": body.action, "recalls": recall_ids, "order_system_reply": reply.body,
                     "probes_owed": probes})


def _order_system_recalls(db, order_system: str) -> list[str]:
    """Open run-time recalls with a distributor on this order system (an order system
    process is shared by every recall that uses it)."""
    open_ids = [r["_id"] for r in db.recalls.find({"status": "open"}, {"_id": 1})]
    if not open_ids:
        return []
    return sorted(db.parties.distinct("recall_id", {"recall_id": {"$in": open_ids}, "order_system": order_system}))


def _mark_switched(db, recall_ids: list[str], order_system: str, real_now) -> None:
    """A switch by hand counts as the run's order system change: the scheduler does not fire it again."""
    if recall_ids:
        db.runs.update_many({"recall_id": {"$in": recall_ids}, f"events.order_system_changed.{order_system}": {"$exists": False}},
                            {"$set": {f"events.order_system_changed.{order_system}": {"at": real_now, "by": "admin"}}})


# ---- supervisor and saboteur --------------------------------------------------

@router.post("/supervisor")
def supervisor(body: SupervisorIn, request: Request) -> dict:
    """Random kills pause from the judge's call until the wrap (spec "Demo controls")."""
    st = request.app.state
    _run(st.db, body.recall_id)
    reason = body.reason or ("paused by the operator" if body.paused else None)
    st.db.runs.update_one({"_id": runs.run_id(body.recall_id)},
                          {"$set": {"supervisor.paused": body.paused, "supervisor.kills_paused_reason": reason}})
    line = "Random kills paused" + (f": {reason}" if body.reason else "") if body.paused else "Random kills resumed"
    feed.emit(st.db, "supervisor", line, level="warn" if body.paused else "info", source="admin",
              recall_id=body.recall_id, system="supervisor", detail={"paused": body.paused, "reason": reason})
    st.wall_cache.clear()
    return {"recall_id": body.recall_id, "supervisor": {"paused": body.paused, "kills_paused_reason": reason}}


@router.post("/saboteur")
def saboteur(body: SaboteurIn, request: Request) -> dict:
    """Switch the saboteur on for `seconds` (database time), or off now. The
    harness's saboteur runs while saboteur.on and saboteur.until is ahead."""
    st = request.app.state
    _run(st.db, body.recall_id)
    real_now = clock.server_now(st.client)
    until = real_now + timedelta(seconds=body.seconds) if body.on else None
    st.db.runs.update_one({"_id": runs.run_id(body.recall_id)},
                          {"$set": {"saboteur.on": body.on, "saboteur.until": until, "saboteur.changed_at": real_now}})
    line = (f"Saboteur on for {body.seconds:g} s: one worker breaks a rule on purpose; the gate must refuse it"
            if body.on else "Saboteur off")
    feed.emit(st.db, "saboteur", line, level="warn" if body.on else "info", source="admin",
              recall_id=body.recall_id, system="gate", detail={"on": body.on, "seconds": body.seconds})
    st.wall_cache.clear()
    return jsonable({"recall_id": body.recall_id, "saboteur": {"on": body.on, "until": until}})
