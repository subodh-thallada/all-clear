"""The screen: GET /wall (the page, web/dist/index.html), /screen/* (its
built assets), GET /wall/state (the JSON it polls once a second) and GET
/wall/text (the same screen as plain text, the spec's "What the run looks
like" block, for a terminal or when the React screen is not built).

The food recall's numbers (spec "Metrics, never merged"): checks completed,
tins located (sold + on_shelf + held + returned) and held or returned, each
over the batch, and the headline

    NOT ALL CLEAR: n tins open (a not located, b still on a shelf, c disputed)

with only the non-zero parts, or ALL CLEAR once the recall is closed. They
are computed here from the same party and edge rows the wall lists (open =
unresolved + on_shelf + transfers awaiting their recipient + disputed), and
merged with levela.ledger.metrics when the ledger computes them too, so the
numbers on screen are the reconciler's numbers by construction. The run
block, the systems, the monitoring strip, the live workers and the calls in
flight are derived from one read of each collection (parties, edges,
attempts, sim_calls, worker_state, feed, runs, claims, operations, adapters,
task_runs, store_systems, store_events, complaints, signals). A collection
another workstream has not created yet reads as empty. The JSON is cached
for half a second per recall: the Atlas free tier allows 100 operations a
second and several screens may poll at once.

Every simulated party carries `simulated: true` and `tag: "SIMULATED"`, and
so does every feed line about one (spec "What must be visible on screen").

/wall/state is public (the room's screen polls it without a key), so it
carries nothing that acts as a credential: no party's form token or form URL.
The token is the only thing POST /form/{token} asks for, so listing it here
would let any viewer book counts for every party. The operator gets the links
from POST /admin/recalls (`make demo` prints them) and the parties from the
links the run sends them.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pymongo.errors import PyMongoError

from .. import clock, config, feed, runs
from ..clock import Clock
from ..ledger import headline as ledger_headline
from ..ledger import metrics as ledger_metrics
from ..orchestrator import attempts, failpoints
from .deps import aware, jsonable, utcnow, worker_view

log = logging.getLogger("levela.api.wall")
router = APIRouter()

SCREEN_DIR = config.SCREEN_DIR
CACHE_TTL_S = 0.5
DAYS_TOTAL = 10
RECALL_FIELDS = {k: 1 for k in ("batch_code", "product", "barcode", "company", "supplier", "defect", "defect_label",
                                "affected_batches", "action", "barcode_verified", "checks", "units_total",
                                "open_units", "status", "source", "source_signal", "parent", "matched_item", "mode",
                                "options", "opened_at", "closed_at")}
BUCKETS = ("sold", "on_shelf", "held", "returned", "forwarded", "unresolved")
LOCATED = ("sold", "on_shelf", "held", "returned")
PARTY_FIELDS = ("party_id", "name", "type", "channel", "state", "shipped_in", *BUCKETS, "batch_code", "batch_seen",
                "sold_source", "block_test", "held_by_system", "sold_by_till", "system_on_shelf", "discovered_by",
                "link_id", "sub")
PARTY_V2_FIELDS = ("tier", "found_via", "ladder_step", "next_action_at", "hold_for_demo", "slice", "order_system",
                   "priority")
EDGE_FIELDS = ("from_party", "to_party", "sender_qty", "recipient_qty", "corroborated_qty", "disputed_qty", "state")
FEED_FIELDS = ("at", "kind", "text", "level", "source", "party_id", "detail", "system", "sim_label")
PHONE_SOURCES = ("voice", "sim", "rep")
SENT_LINK_STATES = ("sent", "verified", "needs_reverify")
# Channels whose party is played by the simulator or a mock (spec "Scope": simulated).
SIMULATED_CHANNELS = ("sim", "order_system")
SIMULATED = "SIMULATED"
ADAPTER_SOURCE = {"model": "repaired by model", "rules": "repaired by rules"}


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


@router.get("/wall/text", response_class=PlainTextResponse)
def wall_text(request: Request, recall_id: str | None = None) -> PlainTextResponse:
    """The screen as the spec prints it ("What the run looks like")."""
    st = request.app.state
    state = st.wall_cache.get(recall_id or "", lambda: build_state(st.db, st.settings, recall_id))
    return PlainTextResponse("\n".join(state.get("screen") or []) + "\n")


def _safe_find(coll, query: dict, projection: dict | None = None, *, sort=None, limit: int = 0) -> list[dict]:
    """A read of a collection another workstream owns: empty when it is
    missing or unreadable, never a 500 on the room's screen."""
    try:
        cur = coll.find(query, projection)
        if sort:
            cur = cur.sort(sort)
        if limit:
            cur = cur.limit(limit)
        return list(cur)
    except PyMongoError as exc:
        log.warning("wall read of %s failed: %s", coll.name, exc)
        return []


def _rows(db, rid: str | None) -> dict:
    """One read per collection for the recall shown."""
    if not rid:
        return {"parties": [], "edges": [], "attempts": [], "sim_calls": [], "run": None, "claims": [], "ops": [],
                "task_runs": [], "store_systems": [], "last_store_event": None, "store_events_total": 0,
                "complaints_total": 0, "signals_approved": 0}
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
            {"$group": {"_id": {"order_system": "$order_system", "kind": "$kind", "state": "$state"},
                        "n": {"$sum": 1}}}])),
        "task_runs": list(db.task_runs.find({"recall_id": rid, "status": "running"},
                                            {"worker": 1, "party_id": 1, "kind": 1, "started_at": 1})),
    }


def _store_rows(db, party_ids: list[str]) -> dict:
    """The store records the strip and the stores line read (WS4's
    collections; empty until they exist): the connectors linked to a party of
    this recall, their newest event and their event count, and the
    complaints and approved signals."""
    linked = _safe_find(db.store_systems, {"party_id": {"$in": party_ids}}) if party_ids else []
    ids = [s["_id"] for s in linked]
    events = _safe_find(db.store_events, {"store_system_id": {"$in": ids}}, sort=[("at", -1), ("_id", -1)],
                        limit=1) if ids else []
    n_events = 0
    if ids:
        try:
            n_events = db.store_events.count_documents({"store_system_id": {"$in": ids}})
        except PyMongoError:
            n_events = 0
    complaints = signals = 0
    try:
        complaints = db.complaints.count_documents({})
        signals = db.signals.count_documents({"status": "approved"})
    except PyMongoError:
        pass
    return {"store_systems": linked, "last_store_event": events[0] if events else None,
            "store_events_total": n_events, "complaints_total": complaints, "signals_approved": signals}


def build_state(db, settings, recall_id: str | None = None) -> dict:
    """The screen's JSON for ?recall_id=, or for the most recently opened recall."""
    now = utcnow()
    db_now = clock.server_now(db.client)
    # Past recalls seeded for memory (seed_batch) are history, never the live screen.
    runtime = list(db.recalls.find({"seed_batch": {"$exists": False}}, RECALL_FIELDS).sort("opened_at", -1))
    if recall_id:
        recall = next((r for r in runtime if r["_id"] == recall_id), None) or \
            db.recalls.find_one({"_id": recall_id}, RECALL_FIELDS)
    else:
        recall = runtime[0] if runtime else None
    rid = recall["_id"] if recall else None
    rows = _rows(db, rid)
    parties = rows["parties"]
    rows.update(_store_rows(db, [s["party_id"] for s in parties]))
    order_systems = sorted({s["order_system"] for s in parties if s.get("order_system")})
    adapters = {a["order_system"]: a for a in db.adapters.find({"status": "active", "order_system": {"$in": order_systems}},
                                                         {"order_system": 1, "version": 1, "source": 1})} if order_systems else {}
    bookkeeping = list(db.worker_state.find(
        {"$or": [{"_id": {"$regex": "^(worker|badge):"}}, {"_id": {"$in": ["ablation", failpoints.DOC_ID]}}]},
        {"resume_token": 0}))
    by_party = defaultdict(list)
    for row in rows["attempts"]:
        by_party[row["party_id"]].append(row)
    numbers = _numbers(db, recall, parties, rows["edges"]) if rid else None
    sim_flags = {s["party_id"]: is_simulated(s, settings) for s in parties}
    state = {
        "now": now,
        "recall": {"_id": rid, **{k: recall.get(k) for k in RECALL_FIELDS}} if recall else None,
        "recalls": [{"_id": r["_id"], "status": r["status"], "batch_code": r["batch_code"]} for r in runtime],
        "metrics": numbers,
        "headline": numbers["headline"] if numbers else "",
        "parties": [_party_view(s, by_party[s["party_id"]], sim_flags[s["party_id"]]) for s in parties],
        "edges": [{k: e.get(k) for k in EDGE_FIELDS} for e in rows["edges"]],
        "feed": [_feed_view(line, sim_flags) for line in feed.recent(db, rid)],
        "worker": _newest_worker(bookkeeping, now, settings),
        "calls": _calls(rows["attempts"], rows["sim_calls"]),
        "failpoints": next((d.get("armed") or {} for d in bookkeeping if d["_id"] == failpoints.DOC_ID), {}),
        "badges": [{"key": d["_id"].removeprefix("badge:"), "text": d.get("text")}
                   for d in bookkeeping if d["_id"].startswith("badge:")],
        "memory_ablation": _ablation(bookkeeping),
        "run": _run_view(rows["run"], parties, numbers, db_now),
        "systems": _systems(rows, parties, order_systems, adapters),
        "monitoring": _monitoring(rows, parties),
        "workers": _workers(parties, rows["task_runs"], db_now),
        "calls_in_flight": _calls_in_flight(rows["attempts"], settings),
    }
    state["screen"] = screen_lines(state)
    return jsonable(state)


# ---- the food recall's numbers ---------------------------------------------------

def _n(row: dict, key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def food_metrics(recall: dict, parties: list[dict], edges: list[dict]) -> dict:
    """The three metrics, never merged, and what is open and why (spec
    "Metrics"): open = not located + still on a shelf + disputed, where not
    located is unresolved plus transfers still awaiting their recipient."""
    total = recall.get("units_total") or 0
    awaiting = sum(_n(e, "sender_qty") - _n(e, "corroborated_qty") for e in edges
                   if e.get("state") == "awaiting_recipient")
    disputed = sum(_n(e, "disputed_qty") for e in edges if e.get("state") == "disputed")
    unresolved = sum(_n(r, "unresolved") for r in parties)
    on_shelf = sum(_n(r, "on_shelf") for r in parties)
    in_transit = sum(_n(e, "sender_qty") - _n(e, "corroborated_qty") for e in edges)
    return {
        "units_total": total,
        "checks_completed": [sum(r.get("state") in ("claimed", "reconciled") for r in parties), len(parties)],
        "tins_located": [sum(_n(r, k) for r in parties for k in LOCATED), total],
        "held_or_returned": [sum(_n(r, "held") + _n(r, "returned") for r in parties), total],
        "open_units": unresolved + awaiting + on_shelf + disputed,
        "not_located": unresolved + awaiting,
        "on_shelf": on_shelf,
        "disputed_units": disputed,
        "mass_balance": sum(_n(r, k) for r in parties for k in BUCKETS if k != "forwarded") + in_transit,
        "status": recall.get("status"),
    }


def food_headline(m: dict) -> str:
    """ALL CLEAR for a closed recall, else "NOT ALL CLEAR: 40 tins open (15
    disputed, 25 still on a shelf)" style, non-zero parts only, in the order
    not located, still on a shelf, disputed."""
    if m.get("status") == "closed":
        return "ALL CLEAR"
    parts = [f"{n} {label}" for n, label in ((m.get("not_located", 0), "not located"),
                                              (m.get("on_shelf", 0), "still on a shelf"),
                                              (m.get("disputed_units", 0), "disputed")) if n]
    n = m.get("open_units", 0)
    text = f"NOT ALL CLEAR: {n} tin{'' if n == 1 else 's'} open"
    return f"{text} ({', '.join(parts)})" if parts else text


def _numbers(db, recall: dict, parties: list[dict], edges: list[dict]) -> dict:
    """The food metrics, with the ledger's metrics() values where both have
    a key (the reconciler is the authority).
    While the ledger is being converted its metrics() may not read these
    party rows; the wall then shows its own numbers rather than failing."""
    out = food_metrics(recall, parties, edges)
    try:
        ledger = ledger_metrics(db, recall["_id"], parties=parties, edges=edges) or {}
    except (KeyError, TypeError) as exc:
        log.debug("ledger metrics unavailable for the wall: %r", exc)
        ledger = {}
    out.update({k: v for k, v in ledger.items() if k in out})
    words = ""
    if ledger:
        try:
            words = ledger_headline(ledger)
        except (KeyError, TypeError):
            words = ""
    out["headline"] = words if words == "ALL CLEAR" or " tin" in words else food_headline(out)
    return out


def is_simulated(party: dict, settings) -> bool:
    """A party played by the simulator or a mock order system, or a voice
    party while the phone itself is simulated."""
    channel = party.get("channel")
    if channel in SIMULATED_CHANNELS:
        return True
    return channel == "voice" and getattr(settings, "voice_provider", "sim") != "elevenlabs"


def _party_view(party: dict, rows: list[dict], simulated: bool = False) -> dict:
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
        "simulated": simulated,
        "tag": SIMULATED if simulated else None,
    }


def _feed_view(line: dict, sim_flags: dict) -> dict:
    out = {k: line.get(k) for k in FEED_FIELDS}
    simulated = bool(sim_flags.get(line.get("party_id")))
    out["simulated"] = simulated
    out["tag"] = SIMULATED if simulated else None
    return out


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
        "day_label": f"Simulated day {clock.day(sim)} of {(run.get('schedule') or {}).get('days_total', DAYS_TOTAL)}, "
                     f"{clock.fmt(sim).split(' ')[1]}",
        "real_label": _mm_ss((db_now - aware(started)).total_seconds()) if started else None,
        "clock_label": f"Clock: {c.seconds_per_sim_hour:g} s per simulated hour" + (" (paused)" if c.paused_at
                                                                                       is not None else ""),
    }


def _mm_ss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


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
    phone_attempts, phone_claims = channels["voice"] + channels["sim"], sum(by_source[s] for s in PHONE_SOURCES)
    out = [
        {"system": "phone", "attempts": phone_attempts, "accepted_claims": phone_claims,
         "line": f"{phone_attempts} attempts, {phone_claims} accepted claims"},
        {"system": "form", "attempts": channels["form"], "accepted_claims": by_source["form"],
         "links_sent": sum(n for (_, kind, state), n in ops.items() if kind == "send_form_link"
                           and state in SENT_LINK_STATES),
         "links_verified": sum(n for (_, kind, state), n in ops.items()
                               if kind == "send_form_link" and state == "verified"),
         "line": f"{channels['form']} submissions, {by_source['form']} accepted claims"},
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
        line = out[-1]
        how = _adapter_label(adapter.get("source"))
        line["line"] = (f"adapter v{adapter.get('version') or 1}{f' ({how})' if how else ''}    "
                        f"{line['ops_verified']} operations verified"
                        + (f", {line['ops_flagged']} flagged" if line["ops_flagged"] else ""))
    events, complaints, approved = rows.get("store_events_total", 0), rows.get("complaints_total", 0), \
        rows.get("signals_approved", 0)
    out.append({"system": "stores", "store_events": events, "complaints": complaints, "signals_approved": approved,
                "line": f"{events} store event{'' if events == 1 else 's'}, {complaints} complaint"
                        f"{'' if complaints == 1 else 's'}, {approved} signal{'' if approved == 1 else 's'} approved"})
    agents = [_agent_view(party, claims) for party in parties if party.get("channel") == "agent"]
    if agents:
        out.append({"system": "agents", "nodes": agents, "line": "; ".join(a["line"] for a in agents)})
    return out


def _adapter_label(source) -> str:
    """How the active adapter came to be: repaired by model, from memory."""
    source = str(source or "")
    if source.startswith("episode"):
        return "from memory"
    return ADAPTER_SOURCE.get(source, "")


def _agent_view(party: dict, claims: Counter) -> dict:
    """One party behind another node (channel agent): its reports accepted,
    tins, tins on a shelf and buyers told, read from the party and its `sub`
    (spec "In the run", sub-recall). No buyer's name ever reaches this node."""
    sub = party.get("sub") or {}
    buyers = sub.get("buyers") or {}
    reports = claims.get(("agent", party["party_id"]), 0)
    tins, on_shelf = _n(party, "shipped_in"), _n(party, "on_shelf")
    told, identified = buyers.get("told"), buyers.get("identified", buyers.get("total"))
    line = (f"{party.get('name') or party['party_id']}: {reports} report{'' if reports == 1 else 's'} accepted. "
            f"{tins} tins, {on_shelf} on a shelf.")
    if told is not None and identified is not None:
        line += f" Buyers told {told} of {identified}."
    line += " Names not shared"
    return {"party_id": party["party_id"], "name": party.get("name"), "reports_accepted": reports, "tins": tins,
            "on_shelf": on_shelf, "buyers_told": told, "buyers_identified": identified,
            "state": sub.get("state"), "line": line}


def _monitoring(rows: dict, parties: list[dict]) -> dict:
    """The one strip (spec "In the run", store records): stores connected,
    tins still on a shelf at those stores, tins moved to held by their own
    stock moves, and the last store event."""
    linked = {s.get("party_id") for s in rows.get("store_systems") or [] if s.get("party_id")}
    mine = [p for p in parties if p["party_id"] in linked]
    on_shelf = sum(_n(p, "on_shelf") for p in mine)
    moved = sum(_n(p, "held_by_system") for p in mine)
    last = rows.get("last_store_event")
    last_view = None
    if last:
        name = next((p.get("name") for p in parties
                     for s in rows["store_systems"] if s["_id"] == last.get("store_system_id")
                     and s.get("party_id") == p["party_id"]), None)
        last_view = {"event_id": last.get("event_id"), "kind": last.get("kind"), "qty": last.get("qty"),
                     "store_system_id": last.get("store_system_id"), "store": name, "at": last.get("at"),
                     "text": _event_text(name or last.get("store_system_id"), last)}
    line = f"Stores connected {len(linked)}      tins on a shelf there {on_shelf}      moved to held {moved}"
    return {"connected": len(linked), "on_shelf": on_shelf, "moved_to_held": moved, "last_event": last_view,
            "stores": [{"store_system_id": s["_id"], "store_label": s.get("store_label"), "party_id": s.get("party_id"),
                        "blocked": s.get("blocked"), "last_event_at": s.get("last_event_at")}
                       for s in rows.get("store_systems") or []],
            "line": line + (f"      last store event: {last_view['text']}" if last_view else "")}


def _event_text(name, event: dict) -> str:
    """'Corner Mart: quarantine 1, event t-88213'."""
    qty = event.get("qty")
    return f"{name}: {event.get('kind')}{f' {qty}' if qty else ''}, event {event.get('event_id')}"


def screen_lines(state: dict) -> list[str]:
    """The screen as text, line for line as the spec prints it."""
    recall, m, run = state.get("recall"), state.get("metrics"), state.get("run")
    if not recall or not m:
        return ["All Clear", "No recall is open."]
    total = m.get("units_total") or 0
    lines = [f"All Clear   Recall of batch {recall.get('batch_code')}   {total:,} tins", state.get("headline") or ""]
    if run:
        lines.append(f"{run['day_label']}     Real time {run.get('real_label') or '-'}     {run['clock_label']}")
        parties = run["parties"]
        checked = run.get("checked") or [0, parties["total"]]
        lines.append(f"Parties   {parties['total']} ({parties['at_start']} at the start, {parties['found']} found)"
                     f"      Checked {checked[0]} of {checked[1]}      Steps {run.get('steps', 0)}")
        tokens = run.get("tokens_per_decision")
        lines.append(f"Kills survived {run.get('kills', 0)}      Duplicate effects {run.get('duplicates', 0)}"
                     f"      Context per decision {f'{tokens:,} tokens' if tokens else '-'}")
    else:
        checked = m.get("checks_completed") or [0, 0]
        lines.append(f"Parties   {len(state.get('parties') or [])}      Checked {checked[0]} of {checked[1]}")
    located, held = m.get("tins_located") or [0, total], m.get("held_or_returned") or [0, total]
    lines.append(f"Located {located[0]:,} of {total:,} ({_pct(located[0], total)})      "
                 f"Held or returned {held[0]:,} of {total:,} ({_pct(held[0], total)})")
    lines.append(state["monitoring"]["line"])
    lines += ["", "Systems"]
    for sys_ in state.get("systems") or []:
        label = sys_["system"]
        lines.append(f"  {label:<13} {sys_.get('line', '')}")
    lines += ["", "Feed, newest first"]
    for line in (state.get("feed") or [])[:12]:
        tag = f"  {SIMULATED}" if line.get("simulated") else ""
        lines.append(f"  {line.get('sim_label') or '':<9} {str(line.get('system') or ''):<13} {line.get('text')}{tag}")
    return lines


def _pct(n: int, total: int) -> str:
    return f"{round(100 * n / total)}%" if total else "0%"


def _workers(parties: list[dict], task_runs: list[dict], db_now: datetime) -> list[dict]:
    """Live leases (lease_until ahead of the database clock), with the task
    run the holder is executing when there is one."""
    by_holder = {(tr.get("worker"), tr.get("party_id")): tr for tr in task_runs}
    out = []
    for s in parties:
        holder, until = s.get("lease_owner"), s.get("lease_until")
        if not holder or until is None or aware(until) <= db_now:
            continue
        tr = by_holder.get((holder, s["party_id"])) or by_holder.get((holder, s["_id"])) or {}
        started = tr.get("started_at")
        out.append({"id": holder, "party_id": s["party_id"], "name": s.get("name"), "kind": tr.get("kind"),
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
