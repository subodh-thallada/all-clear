"""The planner (V2 section 5; spec "The planner").

A decider that runs on events. It is deliberately weak: it ranks work and
adds work, and it cannot touch the ledger. Its plan is data: if the planner is
killed, the priorities and questions it wrote are still in Atlas, and its
next pass reads the same aggregation.

    one at a time   worker_state "planner" is a lease document, claimed by $$NOW
                    exactly like a party (the holder renews it each pass)
    runs when       runs.planner_wake is true (set by whoever saw a party
                    created, a question added, an order system contract fail, a
                    close refused or an invariant fail), else every
                    PLANNER_EVERY_H simulated hours
    reads           one aggregation: parties by state and tier, the ten with
                    the most unresolved units, the oldest due time, free call
                    slots, failure classes from the last simulated day, and
                    what the rules below need (snapshot())
    writes          only through the tool gate, in a task run of kind "planner"
                    (set_priority, add_question, escalate, propose_adapter,
                    draft_report) fenced by the planner lease

RuleDecider, the default: priority = unresolved + 2 x disputed + 50 when the
party has open questions; a party stuck at ladder step 4 more than
STUCK_AFTER_H past its timer is escalated (ladder step 5). Outside the gate,
levela.systems.repair.request_repair(ctx, order system) is asked for
    - an order system paused by a contract failure with no candidate adapter in
      progress (a candidate older than CANDIDATE_TTL_S belongs to a repair
      that died before rejecting or activating it, and blocks nothing), and
    - an order system whose repair was interrupted after activation: unpaused, its
      active adapter's canary passed, and operations still needs_reverify
      (the repair resumes the retry).

Gate calls, as the planner sends them (gate.call(ctx, run, tool, args)):
    set_priority  {"party_id", "priority"}
    escalate      {"party_id", "reason"}
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from pymongo import ReturnDocument

from .. import clock as simclock
from .. import feed, runs
from ..config import LIVE_ATTEMPT_STATES
from ..ledger.transfers import FREE_LEASE
from .claim import CAPACITY_ID, DEFAULT_MAX_LIVE
from .timers import work_filter

log = logging.getLogger("levela.planner")

PLANNER_DOC = "planner"
PLANNER_EVERY_H = 6.0
PLANNER_LEASE_S = 30.0
STUCK_AFTER_H = 12.0
QUESTION_BONUS = 50
FAILURE_WINDOW_H = 24.0
TICK_S = 1.0
CANDIDATE_TTL_S = 300.0     # = systems.repair.LOCK_TTL_S: no live repair holds a candidate longer


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- what the planner reads -----------------------------------------------------------

def snapshot(db, recall_id: str, sim_now: float) -> dict:
    """The planner's one read: a single aggregation over the recall's parties,
    with the edges, slots, failures, adapters and flagged order system operations
    it needs looked up inside."""
    from ..systems.operations import ORDER_SYSTEM_KINDS

    since = sim_now - FAILURE_WINDOW_H
    once = [{"$limit": 1}]
    facets = {
        "by_state_tier": [{"$group": {"_id": {"state": "$state", "tier": "$tier"}, "n": {"$sum": 1}}},
                           {"$sort": {"_id.state": 1, "_id.tier": 1}}],
        "most_unresolved": [{"$match": {"unresolved": {"$gt": 0}}}, {"$sort": {"unresolved": -1, "_id": 1}},
                            {"$limit": 10}, {"$project": {"_id": 0, "party_id": 1, "name": 1, "unresolved": 1}}],
        "oldest_due": [{"$match": {**work_filter(), "next_action_at": {"$type": "number"}}},
                       {"$sort": {"next_action_at": 1}}, {"$limit": 1},
                       {"$project": {"_id": 0, "party_id": 1, "next_action_at": 1}}],
        "parties": [{"$project": {"_id": 0, "party_id": 1, "name": 1, "state": 1, "tier": 1, "priority": 1,
                                 "unresolved": 1, "open_questions": 1, "ladder_step": 1, "next_action_at": 1,
                                 "escalation": 1, "channel": 1, "order_system": 1, "order_system_paused": 1,
                                 "leased": {"$gt": ["$lease_until", "$$NOW"]}}}],
        "disputes": once + [
            {"$lookup": {"from": "edges", "as": "rows", "pipeline": [
                {"$match": {"recall_id": recall_id, "state": "disputed"}},
                {"$project": {"_id": 0, "from_party": 1, "to_party": 1, "disputed_qty": 1}}]}},
            {"$project": {"_id": 0, "rows": 1}}],
        "slots": once + [
            {"$lookup": {"from": "attempts", "as": "live", "pipeline": [
                {"$match": {"channel": "voice", "state": {"$in": list(LIVE_ATTEMPT_STATES)}}}, {"$count": "n"}]}},
            {"$lookup": {"from": "capacity", "as": "cap", "pipeline": [{"$match": {"_id": CAPACITY_ID}}]}},
            {"$project": {"_id": 0, "live": 1, "cap": 1}}],
        "failures": once + [
            {"$lookup": {"from": "task_runs", "as": "rows", "pipeline": [
                {"$match": {"recall_id": recall_id, "failure_class": {"$type": "string"},
                            "ended_sim": {"$gte": since}}},
                {"$group": {"_id": "$failure_class", "n": {"$sum": 1}}}]}},
            {"$project": {"_id": 0, "rows": 1}}],
        # A candidate is in progress only while a repair lock could still be
        # held for it; one with no created_at is taken as fresh.
        "candidates": once + [
            {"$lookup": {"from": "adapters", "as": "rows", "pipeline": [
                {"$match": {"status": "candidate", "$expr": {"$gt": [
                    {"$ifNull": ["$created_at", "$$NOW"]},
                    {"$subtract": ["$$NOW", int(CANDIDATE_TTL_S * 1000)]}]}}},
                {"$project": {"_id": 0, "order_system": 1}}]}},
            {"$project": {"_id": 0, "rows": 1}}],
        "reverify": once + [
            {"$lookup": {"from": "operations", "as": "rows", "pipeline": [
                {"$match": {"recall_id": recall_id, "state": "needs_reverify", "kind": {"$in": list(ORDER_SYSTEM_KINDS)}}},
                {"$group": {"_id": "$order_system", "n": {"$sum": 1}}}]}},
            {"$lookup": {"from": "adapters", "as": "verified", "pipeline": [
                {"$match": {"status": "active", "canary.passed": True}}, {"$project": {"_id": 0, "order_system": 1}}]}},
            {"$project": {"_id": 0, "rows": 1, "verified": 1}}],
    }
    [raw] = list(db.parties.aggregate([{"$match": {"recall_id": recall_id}}, {"$facet": facets}])) or [{}]
    return _shape(raw, recall_id, sim_now)


def _first(raw: dict, facet: str) -> dict:
    rows = raw.get(facet) or []
    return rows[0] if rows else {}


def _shape(raw: dict, recall_id: str, sim_now: float) -> dict:
    slots = _first(raw, "slots")
    cap = (slots.get("cap") or [{}])[0]
    max_live = int(cap.get("max_live", DEFAULT_MAX_LIVE))
    live = int(((slots.get("live") or [{}])[0]).get("n", 0))
    disputed: dict[str, int] = {}
    for e in _first(raw, "disputes").get("rows") or []:
        for sid in (e["from_party"], e["to_party"]):
            disputed[sid] = disputed.get(sid, 0) + int(e.get("disputed_qty") or 0)
    oldest = _first(raw, "oldest_due")
    reverify = _first(raw, "reverify")
    verified = {r["order_system"] for r in reverify.get("verified") or [] if r.get("order_system")}
    return {
        "recall_id": recall_id, "sim_now": sim_now,
        "by_state_tier": [{"state": r["_id"].get("state"), "tier": r["_id"].get("tier"), "n": r["n"]}
                           for r in raw.get("by_state_tier") or []],
        "most_unresolved": raw.get("most_unresolved") or [],
        "oldest_due": oldest.get("next_action_at"),
        "slots": {"max_live": max_live, "live": live, "free": max(0, max_live - live)},
        "failures": {r["_id"]: r["n"] for r in _first(raw, "failures").get("rows") or []},
        "parties": raw.get("parties") or [],
        "disputed": disputed,
        "candidate_order_systems": sorted({r["order_system"] for r in _first(raw, "candidates").get("rows") or []
                                     if r.get("order_system")}),
        # Order systems with flagged operations whose active adapter passed its canary.
        "reverify_order_systems": sorted({r["_id"] for r in reverify.get("rows") or []
                                    if r.get("_id") in verified and r.get("n")}),
    }


# -- what the planner decides ---------------------------------------------------------

def priority(party: dict, disputed: int) -> int:
    """unresolved + 2 x disputed + 50 when the party has open questions."""
    return (int(party.get("unresolved") or 0) + 2 * int(disputed)
            + (QUESTION_BONUS if party.get("open_questions") else 0))


class RuleDecider:
    """The deterministic planner: decisions from the snapshot, no model call."""

    name = "rules"

    def plan(self, snap: dict) -> list[dict]:
        """[{"tool", "args", "note"}] for the gate. A party under a live lease
        is being worked on: its priority waits for the next pass."""
        out = []
        sim_now = snap["sim_now"]
        for s in snap["parties"]:
            want = priority(s, snap["disputed"].get(s["party_id"], 0))
            if want != int(s.get("priority") or 0) and not s.get("leased"):
                out.append({"tool": "set_priority", "args": {"party_id": s["party_id"], "priority": want},
                            "note": f"{s['name']}: priority {want}"})
            if _stuck(s, sim_now):
                out.append({"tool": "escalate", "args": {
                    "party_id": s["party_id"],
                    "reason": f"no answer at ladder step 4 since {simclock.fmt(s['next_action_at'])}"},
                    "note": f"{s['name']}: escalate"})
        return out

    def repairs(self, snap: dict) -> list[str]:
        """Order systems paused by a contract failure with no candidate adapter in
        progress, and unpaused order systems whose repair stopped between
        activating its (canary-passed) adapter and retrying every flagged
        operation: request_repair resumes that retry."""
        order_systems = {s["order_system"] for s in snap["parties"] if s.get("order_system")}
        paused = {s["order_system"] for s in snap["parties"] if s.get("order_system") and s.get("order_system_paused")}
        interrupted = (set(snap.get("reverify_order_systems") or []) & order_systems) - paused
        return sorted((paused - set(snap["candidate_order_systems"])) | interrupted)


def _stuck(party: dict, sim_now: float) -> bool:
    due = party.get("next_action_at")
    return (party.get("ladder_step") == 4 and due is not None and due + STUCK_AFTER_H <= sim_now
            and not party.get("leased") and not party.get("escalation")
            and party.get("state") not in ("claimed", "reconciled"))


# -- the planner ----------------------------------------------------------------------

class Planner:
    """`context(recall, clock)` builds the HarnessContext the gate is called
    with; `gate` has call(ctx, run, tool, args, sim_now=) (default
    levela.harness.gate); `open_run(db, **fields)` opens the planner's task run
    with the planner lease as its fence (default levela.harness.budget.open_run);
    `repair(ctx, order system)` defaults to levela.systems.repair.request_repair."""

    def __init__(self, settings, db, client, *, planner_id: str, context, recall_id: str | None = None,
                 decider=None, gate=None, open_run=None, repair=None, now=None):
        self.settings, self.db, self.client = settings, db, client
        self.planner_id, self.context, self.recall_id = planner_id, context, recall_id
        self.decider = decider or RuleDecider()
        self._gate, self._open_run, self._repair = gate, open_run, repair
        self.now = now or _utcnow
        self._doc_ready = False

    # -- the loop

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the planner is advisory; the workers carry on without it
                log.exception("planner pass failed")
                feed.emit(self.db, "error", f"Planner pass failed ({type(exc).__name__}: {exc}); retrying",
                          level="bad", source="worker", system="planner")
            stop.wait(TICK_S)

    def tick(self) -> list[dict]:
        """One look at every open recall; a pass where one is due."""
        flt = {"status": "open"}
        if self.recall_id:
            flt["_id"] = self.recall_id
        done = []
        for recall in self.db.recalls.find(flt, {"_id": 1}).sort("opened_at", 1):
            rid = recall["_id"]
            run = self.db.runs.find_one({"_id": runs.run_id(rid)}, {"planner_wake": 1, "planner_last_at": 1})
            clock = simclock.get_clock(self.db, rid)
            sim_now = clock.sim_now(simclock.server_now(self.client))
            if run is not None and _due(run, sim_now):
                done.append(self.pass_once(rid))
        return done

    def acquire(self) -> int | None:
        """The planner lease's epoch, or None while another planner holds it."""
        if not self._doc_ready:
            self.db.worker_state.update_one({"_id": PLANNER_DOC}, {"$setOnInsert": {
                "lease_owner": None, "lease_until": FREE_LEASE, "lease_epoch": 0}}, upsert=True)
            self._doc_ready = True
        mine = {"$eq": ["$lease_owner", self.planner_id]}
        doc = self.db.worker_state.find_one_and_update(
            {"_id": PLANNER_DOC, "$or": [{"lease_owner": self.planner_id}, {"$expr": {"$lt": ["$lease_until", "$$NOW"]}}]},
            [{"$set": {"lease_epoch": {"$cond": [mine, "$lease_epoch", {"$add": [{"$ifNull": ["$lease_epoch", 0]}, 1]}]},
                       "lease_owner": self.planner_id,
                       "lease_until": {"$add": ["$$NOW", int(PLANNER_LEASE_S * 1000)]}}}],
            return_document=ReturnDocument.AFTER)
        return None if doc is None else int(doc["lease_epoch"])

    def pass_once(self, recall_id: str) -> dict:
        """Read, decide, write through the gate. Returns a summary."""
        epoch = self.acquire()
        if epoch is None:
            return {"recall_id": recall_id, "skipped": "another planner holds the lease"}
        self.db.runs.update_one({"_id": runs.run_id(recall_id), "planner_wake": True}, {"$set": {"planner_wake": False}})
        clock = simclock.get_clock(self.db, recall_id)
        sim_now = clock.sim_now(simclock.server_now(self.client))
        snap = snapshot(self.db, recall_id, sim_now)
        decisions, repairs = self.decider.plan(snap), self.decider.repairs(snap)
        summary = {"recall_id": recall_id, "sim_now": sim_now, "decisions": decisions, "repairs": repairs,
                   "allowed": 0, "refused": 0}
        if decisions or repairs:
            recall = self.db.recalls.find_one({"_id": recall_id})
            ctx = self.context(recall, clock)
            if decisions:
                summary.update(self._write(ctx, recall_id, epoch, sim_now, decisions))
            for order_system in repairs:
                self.repair(ctx, order_system)
            self._announce(snap, summary)
        self.db.runs.update_one({"_id": runs.run_id(recall_id)}, {"$set": {"planner_last_at": sim_now}})
        return summary

    def _write(self, ctx, recall_id: str, epoch: int, sim_now: float, decisions: list[dict]) -> dict:
        """One planner task run, fenced by the planner lease; every decision is one gate call."""
        run = self.open_run(self.db, worker=self.planner_id, recall_id=recall_id, party_id=None,
                            lease={"coll": "worker_state", "_id": PLANNER_DOC}, lease_epoch=epoch, kind="planner",
                            sim_now=sim_now, decider=getattr(self.decider, "name", type(self.decider).__name__),
                            budget={"tool_calls": len(decisions)})
        allowed = refused = 0
        for d in decisions:
            decision = self.gate.call(ctx, run, d["tool"], d["args"], sim_now=sim_now)
            if getattr(decision, "allowed", False):
                allowed += 1
            else:
                refused += 1
        note = f"{allowed} of {len(decisions)} planner writes allowed"
        self.db.task_runs.update_one({"_id": run.id, "status": "running"}, {"$set": {
            "status": "done", "ended_sim": round(sim_now, 4), "ended_at": self.now(),
            "result": {"outcome": "planned", "evidence": [], "next": None, "note": note}}})
        return {"run_id": run.id, "allowed": allowed, "refused": refused}

    def _announce(self, snap: dict, summary: dict) -> None:
        tools = [d["tool"] for d in summary["decisions"]]
        parts = []
        if tools.count("set_priority"):
            parts.append(f"{tools.count('set_priority')} priorities set")
        if tools.count("escalate"):
            parts.append(f"{tools.count('escalate')} escalated")
        if summary["repairs"]:
            parts.append(f"repair requested for {', '.join(feed.orders_label(o) for o in summary['repairs'])}")
        slots = snap["slots"]
        tail = f"oldest due {simclock.fmt(snap['oldest_due'])}, {slots['free']} of {slots['max_live']} call slots free"
        feed.emit(self.db, "planner", f"planner: {'; '.join(parts)}. {tail[0].upper()}{tail[1:]}",
                  recall_id=snap["recall_id"], source="worker", system="planner", sim=snap["sim_now"],
                  detail={"allowed": summary["allowed"], "refused": summary["refused"],
                          "run_id": summary.get("run_id"), "failures": snap["failures"]})

    # -- collaborators (imported lazily: the planner runs without them in tests)

    @property
    def gate(self):
        if self._gate is None:
            from ..harness import gate

            self._gate = gate
        return self._gate

    @property
    def open_run(self):
        """open_run(db, **run fields) -> the task run object the gate takes."""
        if self._open_run is None:
            from ..harness import budget

            self._open_run = budget.open_run
        return self._open_run

    def repair(self, ctx, order_system: str):
        if self._repair is None:
            from ..systems.repair import request_repair

            self._repair = request_repair
        return self._repair(ctx, order_system)


def _due(run: dict, sim_now: float) -> bool:
    last = run.get("planner_last_at")
    return bool(run.get("planner_wake")) or last is None or sim_now - float(last) >= PLANNER_EVERY_H
