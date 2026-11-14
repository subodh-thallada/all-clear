"""The screen: GET /wall (the page, web/dist/index.html), /screen/* (its
built assets) and GET /wall/state (the JSON it polls once a second).

The metrics are computed by levela.ledger.metrics from the same party and
edge rows the wall lists, so the numbers on screen are the reconciler's
numbers by construction. The run block, the systems, the live workers and
the calls in flight are derived from one read of each collection (parties,
edges, attempts, sim_calls, worker_state, feed, runs, claims, operations,
adapters, task_runs). The JSON is cached for half a second per recall: the
Atlas free tier allows 100 operations a second and several screens may poll
at once.

/wall/state is public (the room's screen polls it without a key), so it
carries nothing that acts as a credential: no party's form token or form URL.
The token is the only thing POST /form/{token} asks for, so listing it here
would let any viewer book counts for every party. The operator gets the links
from POST /admin/recalls (`make demo` prints them) and the parties from the
links the run sends them.
"""
from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import clock, config, feed, runs
from ..clock import Clock
from ..ledger import headline, metrics
from ..orchestrator import attempts, failpoints
from .deps import aware, jsonable, utcnow, worker_view

router = APIRouter()

SCREEN_DIR = config.SCREEN_DIR
CACHE_TTL_S = 0.5
DAYS_TOTAL = 10
RECALL_FIELDS = {k: 1 for k in ("batch_code", "product", "barcode", "company", "hazard", "defect",
                                "affected_batches", "action", "barcode_verified", "units_total", "status",
                                "source", "source_signal", "mode", "options", "opened_at", "closed_at")}
PARTY_FIELDS = ("party_id", "name", "type", "channel", "state", "shipped_in", "sold", "on_shelf", "held",
                "returned", "forwarded", "unresolved", "batch_code", "batch_seen", "sold_source", "block_test",
                "held_by_system", "sold_by_till", "system_on_shelf", "discovered_by")
PARTY_V2_FIELDS = ("tier", "found_via", "ladder_step", "next_action_at", "hold_for_demo", "slice", "order_system",
                   "priority")
EDGE_FIELDS = ("from_party", "to_party", "sender_qty", "recipient_qty", "corroborated_qty", "disputed_qty", "state")
FEED_FIELDS = ("at", "kind", "text", "level", "source", "party_id", "detail", "system", "sim_label")
PHONE_SOURCES = ("voice", "sim", "rep")
SENT_LINK_STATES = ("sent", "verified", "needs_reverify")


class StateCache:
    """Built JSON per key, kept for ttl seconds. Two requests that miss at
    the same moment may both build; that costs one extra read, never a stale
    answer older than ttl."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[float, dict]] = {}

    def get(self, key: str, build) -> dict:
        with self._lock:
            hit = self._rows.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = build()
        with self._lock:
            now = time.monotonic()
            self._rows = {k: v for k, v in self._rows.items() if v[0] > now}
            self._rows[key] = (now + self.ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def mount_static(app: FastAPI) -> None:
    """Serve web/dist under /screen/. Skipped while the screen is not built
    (StaticFiles would fail every request instead of answering 404)."""
    if SCREEN_DIR.is_dir():
        app.mount("/screen", StaticFiles(directory=SCREEN_DIR), name="screen")


@router.get("/wall", include_in_schema=False)
def wall_page():
    index = SCREEN_DIR / "index.html"
    if not index.is_file():
        return PlainTextResponse("The screen is not built: run `npm run build` in web/ (web/dist/index.html "
                                 "is missing).", status_code=404)
    return FileResponse(index, media_type="text/html")


@router.get("/wall/state")
def wall_state(request: Request, recall_id: str | None = None) -> JSONResponse:
    st = request.app.state
    return JSONResponse(st.wall_cache.get(recall_id or "", lambda: build_state(st.db, st.settings, recall_id)))


def _rows(db, rid: str | None) -> dict:
    """One read per collection for the recall shown."""
    if not rid:
        return {"parties": [], "edges": [], "attempts": [], "sim_calls": [], "run": None, "claims": [], "ops": [],
                "task_runs": []}
    return {
        "parties": list(db.parties.find({"recall_id": rid}).sort([("created_at", 1), ("_id", 1)])),
        "edges": list(db.edges.find({"recall_id": rid}).sort([("created_at", 1), ("_id", 1)])),
        "attempts": list(db.attempts.find({"recall_id": rid}, {"party_id": 1, "state": 1, "purpose": 1, "seq": 1,
                                                               "call_sid": 1, "channel": 1})),
        "sim_calls": list(db.sim_calls.find({"recall_id": rid}, {"party_id": 1})),
        "run": db.runs.find_one({"_id": runs.run_id(rid)}),
        "claims": list(db.claims.aggregate([
            {"$match": {"recall_id": rid, "status": "accepted"}},
            {"$group": {"_id": {"source": "$source", "party_id": "$party_id"}, "n": {"$sum": 1}}}])),
        "ops": list(db.operations.aggregate([
            {"$match": {"recall_id": rid}},
            {"$group": {"_id": {"order_system": "$order system", "kind": "$kind", "state": "$state"}, "n": {"$sum": 1}}}])),
        "task_runs": list(db.task_runs.find({"recall_id": rid, "status": "running"},
                                            {"worker": 1, "party_id": 1, "kind": 1, "started_at": 1})),
    }


def build_state(db, settings, recall_id: str | None = None) -> dict:
    """The screen's JSON for ?recall_id=, or for the most recently opened recall."""
    now = utcnow()
    db_now = clock.server_now(db.client)
    runtime = list(db.recalls.find({}, RECALL_FIELDS).sort("opened_at", -1))
    if recall_id:
        recall = next((r for r in runtime if r["_id"] == recall_id), None) or \
            db.recalls.find_one({"_id": recall_id}, RECALL_FIELDS)
    else:
        recall = runtime[0] if runtime else None
    rid = recall["_id"] if recall else None
    rows = _rows(db, rid)
    parties = rows["parties"]
    order_systems = sorted({s["order_system"] for s in parties if s.get("order_system")})
    adapters = {a["order_system"]: a for a in db.adapters.find({"status": "active", "order_system": {"$in": order_systems}},
                                                         {"order_system": 1, "version": 1, "source": 1})} if order_systems else {}
    bookkeeping = list(db.worker_state.find(
        {"$or": [{"_id": {"$regex": "^(worker|badge):"}}, {"_id": {"$in": ["ablation", failpoints.DOC_ID]}}]},
        {"resume_token": 0}))
    by_party = defaultdict(list)
    for row in rows["attempts"]:
        by_party[row["party_id"]].append(row)
    numbers = metrics(db, rid, parties=parties, edges=rows["edges"]) if rid else None
    return jsonable({
        "now": now,
        "recall": {"_id": rid, **{k: recall.get(k) for k in RECALL_FIELDS}} if recall else None,
        "recalls": [{"_id": r["_id"], "status": r["status"], "batch_code": r["batch_code"]} for r in runtime],
        "metrics": numbers,
        "headline": headline(numbers) if numbers else "",
        "parties": [_party_view(s, by_party[s["party_id"]]) for s in parties],
        "edges": [{k: e.get(k) for k in EDGE_FIELDS} for e in rows["edges"]],
        "feed": [{k: line.get(k) for k in FEED_FIELDS} for line in feed.recent(db, rid)],
        "worker": _newest_worker(bookkeeping, now, settings),
        "calls": _calls(rows["attempts"], rows["sim_calls"]),
        "failpoints": next((d.get("armed") or {} for d in bookkeeping if d["_id"] == failpoints.DOC_ID), {}),
        "badges": [{"key": d["_id"].removeprefix("badge:"), "text": d.get("text")}
                   for d in bookkeeping if d["_id"].startswith("badge:")],
        "memory_ablation": _ablation(bookkeeping),
        "run": _run_view(rows["run"], parties, numbers, db_now),
        "systems": _systems(rows, parties, order_systems, adapters),
        "workers": _workers(parties, rows["task_runs"], db_now),
        "calls_in_flight": _calls_in_flight(rows["attempts"], settings),
    })


def _party_view(party: dict, rows: list[dict]) -> dict:
    """One party as the public wall shows it. Fields are listed, never copied
    wholesale, so the form token stays off the wall (module docstring)."""
    live = max((a for a in rows if a["state"] in attempts.LIVE), key=lambda a: a["seq"], default=None)
    due = party.get("next_action_at")
    return {
        **{k: party.get(k) for k in PARTY_FIELDS},
        "escalation": party.get("escalation"),
        "live_attempt": {k: live[k] for k in ("_id", "state", "purpose", "seq")} if live else None,
        "attempts": len(rows),
        "memory": party.get("memory"),
        **{k: party.get(k) for k in PARTY_V2_FIELDS},
        "next_label": clock.fmt(due) if isinstance(due, (int, float)) else None,
        "open_questions": party.get("open_questions") or [],
        "answered": sorted(party.get("answers") or {}),
    }


def _newest_worker(bookkeeping: list[dict], now, settings) -> dict | None:
    workers = [d for d in bookkeeping if d["_id"].startswith("worker:")]
    newest = max(workers, key=lambda d: aware(d.get("heartbeat_at") or d.get("started_at") or now), default=None)
    return worker_view(newest, now, settings)


def _calls(rows: list[dict], sim_rows: list[dict]) -> dict:
    """Distinct calls: every simulated call, plus every real call (an attempt
    with a call SID the simulator did not place)."""
    sids = {row["_id"]: row.get("party_id") for row in sim_rows}
    for row in rows:
        if row.get("call_sid") and row["call_sid"] not in sids:
            sids[row["call_sid"]] = row["party_id"]
    return {"total": len(sids), "by_party": dict(Counter(s for s in sids.values() if s))}


def _ablation(bookkeeping: list[dict]) -> dict | None:
    doc = next((d for d in bookkeeping if d["_id"] == "ablation"), None)
    return {"with": doc.get("with"), "without": doc.get("without")} if doc else None


# ---- V2 §12 -----------------------------------------------------------------

def _invariants_view(run: dict) -> dict:
    """The wall's invariant counter: red once any check has failed (the
    failure is sticky, like the supervisor's stop)."""
    check = run.get("last_invariant_check") or {}
    passed, at = check.get("passed"), check.get("at")
    ok = None if passed is None else (not run.get("invariant_failed") and False not in passed)
    return {"at": at, "at_label": clock.fmt(at) if isinstance(at, (int, float)) else None, "passed": passed, "ok": ok}


def _run_view(run: dict | None, parties: list[dict], numbers: dict | None, db_now: datetime) -> dict | None:
    if run is None:
        return None
    c = Clock.from_doc(run.get("clock"))
    sim = c.sim_now(db_now)
    started = run.get("started_at")
    tokens = run.get("tokens") or {}
    saboteur = run.get("saboteur") or {}
    until = saboteur.get("until")
    at_start = run.get("parties_at_start") or 0
    return {
        "sim_now": round(sim, 4), "sim_label": clock.fmt(sim), "day": clock.day(sim),
        "days_total": (run.get("schedule") or {}).get("days_total", DAYS_TOTAL),
        "real_elapsed_s": round((db_now - aware(started)).total_seconds(), 1) if started else None,
        "seconds_per_sim_hour": c.seconds_per_sim_hour, "paused": c.paused_at is not None,
        "parties": {"total": len(parties), "at_start": at_start, "found": max(0, len(parties) - at_start)},
        "checked": numbers["checks_completed"] if numbers else [0, len(parties)],
        "steps": run.get("steps", 0), "kills": run.get("kills", 0), "duplicates": run.get("duplicates", 0),
        "model_calls": run.get("model_calls", 0),
        "tokens_per_decision": tokens.get("last") if tokens.get("n") else None,
        "invariants": _invariants_view(run),
        "supervisor": {"paused": bool((run.get("supervisor") or {}).get("paused"))},
        "saboteur": {"on": bool(saboteur.get("on")) and (until is None or aware(until) > db_now)},
    }


def _systems(rows: dict, parties: list[dict], order_systems: list[str], adapters: dict) -> list[dict]:
    """One line per outside system the run talks to: the phone, the form, and
    each distributor order system with its active adapter."""
    claims = Counter()
    for row in rows["claims"]:
        claims[(row["_id"].get("source"), row["_id"].get("party_id"))] += row["n"]
    ops = Counter()
    for row in rows["ops"]:
        ops[(row["_id"].get("order_system"), row["_id"].get("kind"), row["_id"].get("state"))] += row["n"]
    channels = Counter(a.get("channel") for a in rows["attempts"])
    by_source = Counter()
    for (source, _), n in claims.items():
        by_source[source] += n
    out = [
        {"system": "phone", "attempts": channels["voice"] + channels["sim"],
         "accepted_claims": sum(by_source[s] for s in PHONE_SOURCES)},
        {"system": "form", "attempts": channels["form"], "accepted_claims": by_source["form"],
         "links_sent": sum(n for (_, kind, state), n in ops.items() if kind == "send_form_link"
                           and state in SENT_LINK_STATES),
         "links_verified": sum(n for (_, kind, state), n in ops.items()
                               if kind == "send_form_link" and state == "verified")},
    ]
    order_system_of = {s["party_id"]: s.get("order_system") for s in parties}
    for order_system in order_systems:
        adapter = adapters.get(order_system) or {}
        mine = {(kind, state): n for (p, kind, state), n in ops.items() if p == order_system}
        out.append({
            "system": feed.orders_label(order_system), "adapter_version": adapter.get("version"),
            "adapter_source": adapter.get("source"),
            "ops_verified": sum(n for (_, state), n in mine.items() if state == "verified"),
            "ops_flagged": sum(n for (_, state), n in mine.items() if state == "needs_reverify"),
            "ops_total": sum(mine.values()),
            "accepted_claims": sum(n for (source, sid), n in claims.items()
                                   if source == "order_system" and order_system_of.get(sid) == order_system),
            "paused": any(s.get("order_system_paused") for s in parties if s.get("order_system") == order_system),
        })
    return out


def _workers(parties: list[dict], task_runs: list[dict], db_now: datetime) -> list[dict]:
    """Live leases (lease_until ahead of the database clock), with the task
    run the holder is executing when there is one."""
    by_holder = {(tr.get("worker"), tr.get("party_id")): tr for tr in task_runs}
    out = []
    for s in parties:
        owner, until = s.get("lease_owner"), s.get("lease_until")
        if not owner or until is None or aware(until) <= db_now:
            continue
        tr = by_holder.get((owner, s["party_id"])) or by_holder.get((owner, s["_id"])) or {}
        started = tr.get("started_at")
        out.append({"id": owner, "party_id": s["party_id"], "name": s.get("name"), "kind": tr.get("kind"),
                    "since_s": round((db_now - aware(started)).total_seconds(), 1) if started else None,
                    "lease_epoch": s.get("lease_epoch")})
    return sorted(out, key=lambda w: (w["id"], w["party_id"]))


def _calls_in_flight(rows: list[dict], settings) -> dict:
    """Live call attempts, real phones apart from simulated parties. A voice
    party rings a real phone only with the ElevenLabs provider (the kill-demo
    fallback simulates it, and the simulator's SIDs start "CAsim")."""
    real = simulated = 0
    for a in rows:
        if a["state"] not in attempts.LIVE or a.get("channel") == "form":
            continue
        sid = a.get("call_sid") or ""
        if a.get("channel") == "voice" and settings.voice_provider == "elevenlabs" and not sid.startswith("CAsim"):
            real += 1
        else:
            simulated += 1
    return {"real": real, "simulated": simulated}
