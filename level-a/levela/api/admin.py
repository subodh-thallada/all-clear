"""/admin/*: start, close, reset and steer a recall (header X-Admin-Token, or
?key= for the wall's buttons). The long run's controls (clock, questions,
order systems, supervisor, saboteur) are in admin_run.py.

Every action the room should notice writes a feed line. The close button is
the closing-gate beat: Atlas's close-gate validator, not this code, decides
whether a recall may close, and its raw refusal goes on the wall.
"""
from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pymongo.errors import BulkWriteError, DuplicateKeyError

from .. import clock, feed, runs
from ..config import FAILPOINTS
from ..db import seed
from ..ledger.reconciler import close_line, try_close
from ..orchestrator import attempts, failpoints
from . import order_admin
from .deps import form_url, jsonable, require_admin, sim_hour, party_name, utcnow, worker_view

log = logging.getLogger("levela.api.admin")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

DUPLICATE_KEY = 11000
FAILPOINT_CHANNELS = ("voice", "sim")
SLICE = "slice"


class StartRecall(BaseModel):
    scenario: str
    all_sim: bool = False
    sim_parties: list[str] = []
    mode: str | None = None
    followups: str | None = None
    memory: bool | None = None          # default: settings.memory_enabled
    recall_id: str | None = None
    seconds_per_sim_hour: float | None = Field(None, gt=0)   # default: the scenario's schedule, else 30
    hold_slice: bool | None = None      # default: held in demo mode when the scenario asks for it
    hold_order_system_change: bool | None = None   # default: held (switched by hand) in demo mode


class FailpointRequest(BaseModel):
    name: str | None = None             # null disarms every failpoint
    party_id: str | None = None
    channel: str | None = None


class RecallRef(BaseModel):
    recall_id: str


class Release(BaseModel):
    recall_id: str
    party_id: str


def _is_duplicate(exc: Exception) -> bool:
    if isinstance(exc, DuplicateKeyError):
        return True
    errors = (exc.details or {}).get("writeErrors") or []
    return bool(errors) and all(e.get("code") == DUPLICATE_KEY for e in errors)


def _require_recall(db, recall_id: str) -> None:
    if db.recalls.find_one({"_id": recall_id}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail=f"unknown recall {recall_id}")


@router.post("/recalls")
def start_recall(body: StartRecall, request: Request) -> dict:
    st = request.app.state
    names = seed.list_scenarios()
    if body.scenario not in names:
        raise HTTPException(status_code=400, detail=f"unknown scenario {body.scenario!r}; choose one of {names}")
    memory = st.settings.memory_enabled if body.memory is None else body.memory
    scenario = seed.load_scenario(body.scenario)
    try:
        out = seed.start_recall(st.client, st.db, scenario, all_sim=body.all_sim, sim_parties=body.sim_parties,
                                mode=body.mode, followups=body.followups, memory=memory, recall_id=body.recall_id,
                                seconds_per_sim_hour=body.seconds_per_sim_hour, hold_slice=body.hold_slice)
    except seed.ScenarioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (DuplicateKeyError, BulkWriteError) as exc:
        if not _is_duplicate(exc):
            raise
        rid = body.recall_id or scenario["recall"]["_id"]
        raise HTTPException(status_code=409, detail=f"recall {rid} already exists; reset first or pass "
                                                    "another recall_id") from None
    hold_change = (out["mode"] == "demo") if body.hold_order_system_change is None else body.hold_order_system_change
    run = _hold_order_system_changes(st.db, out["recall_id"]) if hold_change else runs.get_run(st.db, out["recall_id"])
    base = st.settings.webhook_base
    out["parties"] = [{**s, "form_url": form_url(st.settings, s["form_token"]),
                      "qr_url": f"{base}/form/{s['form_token']}/qr.svg"} for s in out["parties"]]
    out["run"] = _run_summary(run)
    return jsonable(out)


def _hold_order_system_changes(db, recall_id: str) -> dict | None:
    """A null order_system_change_at means "switched by hand" (V2 §3): the scheduler
    leaves it to the operator's button. The scenario's time stays in
    runs.schedule."""
    run = runs.get_run(db, recall_id) or {}
    planned = (run.get("events") or {}).get("order_system_change_at") or {}
    if not planned:
        return run
    db.runs.update_one({"_id": runs.run_id(recall_id)},
                       {"$set": {f"events.order_system_change_at.{p}": None for p in planned}})
    return runs.get_run(db, recall_id)


def _run_summary(run: dict | None) -> dict | None:
    if not run:
        return None
    c = clock.Clock.from_doc(run.get("clock"))
    return {"_id": run["_id"], "sim_start": c.sim_start, "sim_start_label": clock.fmt(c.sim_start),
            "seconds_per_sim_hour": c.seconds_per_sim_hour,
            "order_system_change_at": (run.get("events") or {}).get("order_system_change_at") or {}}


@router.post("/failpoint")
def failpoint(body: FailpointRequest, request: Request) -> dict:
    db = request.app.state.db
    if body.name is None:
        failpoints.disarm(db)
        feed.emit(db, "failpoint_armed", "All failpoints disarmed", source="admin")
        return {"armed": jsonable(failpoints.armed(db))}
    if body.name not in FAILPOINTS:
        raise HTTPException(status_code=400, detail=f"unknown failpoint {body.name!r}; choose one of {FAILPOINTS}")
    if body.channel is not None and body.channel not in FAILPOINT_CHANNELS:
        raise HTTPException(status_code=400, detail=f"channel must be one of {FAILPOINT_CHANNELS}")
    spec = failpoints.arm(db, body.name, party_id=body.party_id, channel=body.channel, by="admin")
    scope = ", ".join(x for x in (body.party_id, body.channel) if x)
    feed.emit(db, "failpoint_armed", f"Failpoint {body.name} armed" + (f" ({scope})" if scope else ""),
              level="warn", source="admin", party_id=body.party_id,
              detail={"name": body.name, **jsonable(spec)})
    return {"armed": jsonable(failpoints.armed(db))}


@router.post("/reset")
def reset(request: Request) -> dict:
    """Remove every recall and its records, the memory included, and reset every configured mock order system to v1 with no holds or
    faults: the order systems keep their state in memory, so a rehearsal that
    switched them would otherwise hand the next run a v2 order system at day 0.
    An order system that could not be reset is named in the reply and on the wall."""
    st = request.app.state
    removed = seed.reset(st.db)
    order_systems = order_admin.reset_all(st.http, st.settings.order_urls, st.settings.admin_token)
    for name in order_admin.unreset(order_systems):
        feed.emit(st.db, "order_system_admin_failed", f"{feed.orders_label(name)}: reset failed, "
                  f"{order_systems[name]['error']}. Restart it before the next run", level="bad", source="admin",
                  system=feed.orders_label(name), detail=order_systems[name])
    st.wall_cache.clear()
    log.info("reset: %s; order systems %s", removed, {name: view["ok"] for name, view in order_systems.items()})
    return jsonable({"removed": removed, "order_systems": order_systems})


@router.post("/close")
def close(body: RecallRef, request: Request) -> dict:
    """Atlas's close gate decides. A refusal wakes the planner (spec "The
    planner": it runs when a close is refused), so the open panels are re-ranked
    now rather than at its next six-hour pass."""
    db = request.app.state.db
    _require_recall(db, body.recall_id)
    res = try_close(db, body.recall_id)
    if res.get("already"):
        return jsonable(res)
    # Atlas's close gate refuses on open units; try_close itself refuses a
    # ledger whose mass balance is off, and says why.
    kind, text, level = close_line(res)
    feed.emit(db, kind, text, level=level, source="admin", recall_id=body.recall_id, detail=res.get("atlas_error"))
    if not res["closed"]:
        runs.wake_planner(db, body.recall_id, f"close refused: {res.get('open_units')} open units")
    return jsonable(res)


@router.post("/release")
def release(body: Release, request: Request) -> dict:
    """Release a held live-slice party to the timers and let its held
    simulator reply go. party_id "slice" releases every slice party.

    A held party (hold_for_demo) becomes due now; a slice party not found yet
    loses the hold in the recall's directory, so it is created unheld. The
    simulator's demo_hold reply waits for the sim_release row written here."""
    st = request.app.state
    db = st.db
    recall = db.recalls.find_one({"_id": body.recall_id}, {"directory": 1})
    if recall is None:
        raise HTTPException(status_code=404, detail=f"unknown recall {body.recall_id}")
    ids = _release_targets(db, recall, body.party_id)
    sim = sim_hour(st, body.recall_id)
    unheld = _clear_holds(db, body.recall_id, ids, sim)
    now = utcnow()
    for sid in ids:
        db.worker_state.update_one({"_id": f"sim_release:{body.recall_id}:{sid}"},
                                   {"$set": {"released_at": now}}, upsert=True)
    _announce_release(db, body, ids, unheld, sim)
    return {"released": True, "recall_id": body.recall_id, "party_id": body.party_id, "parties": ids,
            "unheld": unheld, "next_action_at": sim if unheld else None, "released_at": now.isoformat()}


def _release_targets(db, recall: dict, party_id: str) -> list[str]:
    """The parties a release names: one party (known, or still only in the
    directory), or every slice party of the recall."""
    directory = recall.get("directory") or []
    if party_id == SLICE:
        found = db.parties.distinct("party_id", {"recall_id": recall["_id"], "slice": True})
        return sorted(set(found) | {e["party_id"] for e in directory if e.get("slice")})
    known = db.parties.find_one({"recall_id": recall["_id"], "party_id": party_id}, {"_id": 1})
    if known is None and not any(e["party_id"] == party_id for e in directory):
        raise HTTPException(status_code=404, detail=f"unknown party {party_id} in {recall['_id']}")
    return [party_id]


def _clear_holds(db, recall_id: str, ids: list[str], sim: float) -> list[str]:
    """Held parties among ids lose the hold and are due now. Parties that were
    not held keep their timers (a second press must not pull a call forward)."""
    held = sorted(db.parties.distinct("party_id", {"recall_id": recall_id, "party_id": {"$in": ids},
                                                  "hold_for_demo": True}))
    if held:
        db.parties.update_many({"recall_id": recall_id, "party_id": {"$in": held}, "hold_for_demo": True},
                              {"$set": {"hold_for_demo": False, "next_action_at": sim}})
    db.recalls.update_one({"_id": recall_id, "directory.party_id": {"$in": ids}},
                          {"$set": {"directory.$[e].hold_for_demo": False}},
                          array_filters=[{"e.party_id": {"$in": ids}}])
    return held


def _announce_release(db, body: Release, ids: list[str], unheld: list[str], sim: float) -> None:
    if body.party_id == SLICE:
        text = f"Released the live slice ({len(ids)} parties): {len(unheld)} due now at {clock.fmt(sim)}"
    elif unheld:
        text = f"Released {party_name(db, body.recall_id, body.party_id)} to the timers: due now at {clock.fmt(sim)}"
    else:
        text = f"Released the held reply from {party_name(db, body.recall_id, body.party_id)}"
    feed.emit(db, "sim_released", text, source="admin", recall_id=body.recall_id, system="phone", sim=sim,
              party_id=None if body.party_id == SLICE else body.party_id,
              detail={"parties": ids, "unheld": unheld})


@router.post("/followups")
def followups(body: RecallRef, request: Request) -> dict:
    """With followups "manual", dispute follow-up calls wait for this."""
    db = request.app.state.db
    _require_recall(db, body.recall_id)
    db.recalls.update_one({"_id": body.recall_id}, {"$set": {"followups_released": True}})
    # The engine acts only on due parties, so releasing follow-ups must also
    # wake both parties of every disputed transfer (V2 §5): set their timers
    # to now. A party mid-run keeps its lease; it is due again on release.
    parties = sorted({sid for e in db.edges.find({"recall_id": body.recall_id, "state": "disputed"},
                                                 {"from_party": 1, "to_party": 1})
                      for sid in (e["from_party"], e["to_party"])})
    woken = 0
    if parties:
        from ..clock import get_clock, server_now
        sim_now = get_clock(db, body.recall_id).sim_now(server_now(request.app.state.client))
        woken = db.parties.update_many({"recall_id": body.recall_id, "party_id": {"$in": parties}},
                                      {"$set": {"next_action_at": sim_now}}).modified_count
    feed.emit(db, "followups_released",
              f"Dispute follow-ups released: {len(parties)} parties on disputed transfers are due now",
              source="admin", recall_id=body.recall_id, system="planner")
    return {"followups_released": True, "recall_id": body.recall_id, "woken": woken, "parties": parties}


@router.get("/status")
def status(request: Request) -> dict:
    st = request.app.state
    db, now = st.db, utcnow()
    recalls = list(db.recalls.find({},
                                   {"directory": 0}).sort("opened_at", -1))
    workers = [{**worker_view(doc, now, st.settings), "pid": doc.get("pid"), "host": doc.get("host"),
                "started_at": doc.get("started_at"), "stopped_at": doc.get("stopped_at")}
               for doc in db.worker_state.find({"_id": {"$regex": "^worker:"}}, {"resume_token": 0})]
    live = list(db.attempts.find({"state": {"$in": attempts.LIVE}},
                                 {"recall_id": 1, "party_id": 1, "state": 1, "purpose": 1, "channel": 1,
                                  "seq": 1, "intent_at": 1, "dial_confirmed": 1}))
    by_status = Counter(row.get("status") for row in db.sim_calls.find({}, {"status": 1}))
    return jsonable({
        "now": now, "db": st.settings.db_name, "recalls": recalls, "workers": workers,
        "failpoints": failpoints.armed(db), "live_attempts": live,
        "sim_calls": {"total": sum(by_status.values()), "by_status": dict(by_status)},
    })
