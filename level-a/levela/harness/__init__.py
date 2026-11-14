"""The harness (V2 §6): how a worker turns one leased party into one task run.

    execute(ctx, party, *, worker_id, fence, decider) -> dict

is the engine's single entry point, called after a party was claimed (and
recovered at claim time):

    1 the memory check before a party's first contact, then the facts and
      the kind of run the party needs (nothing owed -> the lease is released
      with the right timer and no run is started)
    2 start the task run (its tools and budget)
    3 brief -> decider.decide -> gate.call, until the decider is done, the
      run's one effect is made (contact kinds), or a budget runs out
    4 a call is waited for until the post-call webhook closes its attempt,
      extending the lease with the fence every 15 real seconds
    5 the outcome is derived from the facts in Atlas, checked (result.check),
      and the run's result and the party's next timer (ladder.after) are
      written with the lease release in one transaction (timers.release)

Failures end as one failure class (an episode the party's next brief shows):
a budget stop releases the lease with the party due again now; a refusal on
`run` or `lease` releases nothing, because the lease is no longer this
worker's to release.

run_planner() is the planner's run (the planner lease, the planner allowlist), and
start_run() opens a run on a party without a lease of its own: the saboteur's
way to break the rules on purpose.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .. import feed, runs
from ..clock import Clock, day, fmt, get_clock, server_now
from ..orchestrator import ladder
from . import brief as brief_
from . import budget, gate, result, trace
from .budget import TaskRun
from .decide import Decision
from .tools import REGISTRY


EXTEND_EVERY_S = 15.0       # real seconds between lease extensions while a call is live
LEASE_S = 60.0              # what each extension asks for
POLL_S = 0.2                # real seconds between reads of a live attempt
DECIDE_FLOOR_S = 1.0        # a decider always gets at least this long to answer
RETRY_SOON_H = 0.25         # a refused or deferred party looks again in 15 simulated minutes
ORDER_SYSTEM_RETRY_H = 1.0        # a paused or unsettled order system is looked at hourly
SINGLE_EFFECT_KINDS = ("contact_party", "followup", "ask_question")


class _NoFailpoints:
    def hit(self, name: str, attempt: dict | None = None) -> None:
        return None


@dataclass
class HarnessContext:
    """What a run needs from the process. `clock` None follows the recall's
    run clock (re-read, cached half a second, so /admin/clock takes effect);
    `http` is the client the form-link tool posts to the simulator with."""
    settings: Any
    client: Any
    db: Any
    clock: Clock | None = None
    providers: dict = field(default_factory=dict)
    probes: dict = field(default_factory=dict)
    order_systems: dict = field(default_factory=dict)
    memory: Any = None
    failpoints: Any = None
    recall: dict | None = None
    http: Any = None

    def __post_init__(self):
        if self.failpoints is None:
            self.failpoints = _NoFailpoints()

    def _clock(self) -> Clock:
        return self.clock or get_clock(self.db, self.recall["_id"])

    def sim_now(self) -> float:
        """Simulated hours now, from the database's clock."""
        return self._clock().sim_now(server_now(self.client))

    def real_seconds(self, sim_hours: float) -> float:
        return self._clock().real_seconds(sim_hours)

    def for_recall(self, recall_id: str) -> "HarnessContext":
        """This context bound to a recall (a worker serves every open recall)."""
        if self.recall is not None and self.recall.get("_id") == recall_id:
            return self
        recall = self.db.recalls.find_one({"_id": recall_id})
        if recall is None:
            raise LookupError(f"no recall {recall_id}")
        return replace(self, recall=recall)


@dataclass
class _Options:
    wait_floor_s: float
    extend_every_s: float
    lease_s: float


# ------------------------------------------------------------------ entry

def execute(ctx: HarnessContext, party: dict, *, worker_id: str, fence: dict, decider, saboteur: str | None = None,
            wait_floor_s: float | None = None, extend_every_s: float | None = None,
            lease_s: float | None = None) -> dict:
    """One task run on a party this worker has leased (see the module docstring).
    `wait_floor_s` is the least real time a call is waited for (default
    settings.stale_call_s: a real call can outlast a fast clock's budget)."""
    ctx = ctx.for_recall(party["recall_id"])
    sim_now = ctx.sim_now()
    party = brief_.remember_before_first_contact(ctx, party, sim_now)
    facts = brief_.gather(ctx, party, sim_now)
    if facts["kind"] is None:
        return _defer(ctx, party, fence, facts, sim_now)
    run = start_run(ctx, party, worker_id=worker_id, lease_epoch=fence["lease_epoch"], kind=facts["kind"],
                    decider_name=getattr(decider, "name", type(decider).__name__), saboteur=saboteur, sim_now=sim_now)
    floor = ctx.settings.stale_call_s if wait_floor_s is None else wait_floor_s
    # Lease timing follows the settings the claim used (LEASE_S, LEASE_EXTEND_S).
    extend = extend_every_s if extend_every_s is not None else getattr(ctx.settings, "lease_extend_s", EXTEND_EVERY_S)
    lease = lease_s if lease_s is not None else getattr(ctx.settings, "lease_s", LEASE_S)
    return _PartyRun(ctx, run, party, fence, decider, facts, _Options(floor, extend, lease)).drive()


def start_run(ctx: HarnessContext, party: dict, *, worker_id: str, lease_epoch: int, kind: str, decider_name: str,
              saboteur: str | None = None, sim_now: float | None = None) -> TaskRun:
    """Open a task run on a party. The gate, not this function, decides
    whether the run may act: a run whose epoch is not the party's is refused
    on its first tool call."""
    sim_now = ctx.sim_now() if sim_now is None else sim_now
    floor = getattr(ctx.settings, "run_min_real_s", budget.DEFAULT_MIN_REAL_S)
    return budget.open_run(ctx.db, worker=worker_id, recall_id=party["recall_id"], party_id=party["party_id"],
                           lease={"coll": "parties", "_id": party["_id"]}, lease_epoch=lease_epoch, kind=kind,
                           sim_now=sim_now, decider=decider_name, saboteur=saboteur,
                           budget={"min_real_s": float(floor)})


# ---------------------------------------------------------------- decider

def _decide(ctx: HarnessContext, run: TaskRun, decider, b: brief_.Brief, now_h: float) -> Decision | str:
    """One decision, or the name of the budget that ran out ("model_calls",
    "sim_hours"). A decider that does not answer within the run's remaining
    simulated time is abandoned there: its thread may never return."""
    if decider.is_model and not budget.take(ctx.db, run.id, "model_calls"):
        return "model_calls"
    timeout = max(ctx.real_seconds(budget.time_left_h(run, now_h)), DECIDE_FLOOR_S)
    started = time.monotonic()
    decision = _within(decider.decide, b, timeout)
    latency = int((time.monotonic() - started) * 1000)
    if decision is None:
        trace.record(ctx.db, run, kind="decision", sim=now_h, brief=b.to_doc(), output=None, latency_ms=latency,
                     tokens=b.tokens)
        return "sim_hours"
    tokens = decision.tokens or b.tokens
    runs.record_tokens(ctx.db, run.recall_id, tokens)
    if decision.model_calls:
        runs.inc(ctx.db, run.recall_id, "model_calls", decision.model_calls)
    trace.record(ctx.db, run, kind="decision", sim=now_h, brief=b.to_doc(), output=decision.to_doc(),
                 latency_ms=latency, tokens=tokens)
    return decision


def _within(fn, arg, timeout_s: float):
    """fn(arg) in a daemon thread; None if it has not returned in time.
    An exception inside fn is raised here."""
    panel: dict = {}

    def target():
        try:
            panel["value"] = fn(arg)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller's thread below
            panel["error"] = exc

    worker = threading.Thread(target=target, daemon=True, name="decider")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return None
    if "error" in panel:
        raise panel["error"]
    return panel.get("value")


# --------------------------------------------------------------- party run

class _PartyRun:
    """The loop of one party run and every way it can end."""

    def __init__(self, ctx, run: TaskRun, party: dict, fence: dict, decider, facts: dict, opts: _Options):
        self.ctx, self.db, self.run, self.party, self.fence = ctx, ctx.db, run, party, fence
        self.decider, self.facts, self.opts = decider, facts, opts

    def drive(self) -> dict:
        done: list[dict] = []
        claimed = None
        while True:
            now_h = self.ctx.sim_now()
            if budget.time_left_h(self.run, now_h) <= 0:
                return self.stop("sim_hours")
            decision = _decide(self.ctx, self.run, self.decider,
                               brief_.render(self.facts, tools=self.run.tools, done=done), now_h)
            if isinstance(decision, str):
                return self.stop(decision)
            if decision.tool is None:
                claimed = decision.result
                break
            gd = gate.call(self.ctx, self.run, decision.tool, decision.args, sim_now=now_h)
            if not gd.allowed:
                return self.stop("tool_calls") if gd.rule == "budget" else self.refused(gd)
            done.append({"tool": decision.tool, "state": (gd.outcome or {}).get("state"), "duplicate": gd.duplicate})
            if decision.tool == "place_call":
                waited = self.await_call(gd.outcome or {})
                if waited == "timeout":
                    return self.stop("sim_hours")
                if waited == "in_doubt":
                    return self.in_doubt()
                if waited == "lease_lost":
                    return self.lease_lost()
            if self.finishes_after(decision.tool):
                break
            if self.run.kind == "order_system_sync":
                self.refresh()
                if not self.facts["order_system"]["next_op"]:
                    break            # nothing more is owed: no need to ask the decider
        return self.finish(claimed)

    def finishes_after(self, tool: str) -> bool:
        """A contact run makes one effect; only a decider that asks for more
        (the double-send saboteur) is asked again."""
        return (self.run.kind in SINGLE_EFFECT_KINDS and REGISTRY[tool].kind == "effect"
                and not getattr(self.decider, "continues_after_effect", False))

    def refresh(self) -> None:
        self.party = self.db.parties.find_one({"_id": self.party["_id"]}) or self.party
        self.facts = {**brief_.gather(self.ctx, self.party, self.ctx.sim_now()), "kind": self.run.kind}

    # -- waiting for a call

    def await_call(self, outcome: dict) -> str:
        """settled | in_doubt | timeout | lease_lost. The post-call webhook (the
        API) closes the attempt; this only watches, and keeps the lease."""
        from ..orchestrator import claim

        attempt_id = outcome.get("attempt_id")
        if not attempt_id or outcome.get("state") == "failed":
            return "settled"
        deadline = time.monotonic() + max(self.ctx.real_seconds(budget.time_left_h(self.run, self.ctx.sim_now())),
                                          self.opts.wait_floor_s)
        next_extend = time.monotonic() + self.opts.extend_every_s
        while True:
            att = self.db.attempts.find_one({"_id": attempt_id}, {"state": 1, "in_doubt": 1, "dial_confirmed": 1})
            if att is None or att["state"] in result.FINAL_ATTEMPT:
                return "settled"
            if att.get("in_doubt") and not att.get("dial_confirmed"):
                return "in_doubt"
            if time.monotonic() >= deadline:
                return "timeout"
            if time.monotonic() >= next_extend:
                if not claim.extend(self.db, self.fence["_id"], self.run.worker, self.run.lease_epoch,
                                    self.opts.lease_s):
                    return "lease_lost"
                next_extend += self.opts.extend_every_s
            time.sleep(POLL_S)

    # -- endings

    def finish(self, claimed: dict | None) -> dict:
        sim_now = self.ctx.sim_now()
        facts = result.snapshot(self.db, self.run)
        party = facts.party or self.party
        claim = claimed if claimed is not None else result.derive(facts)
        ok, why = result.check(facts, claim)
        if ok and claimed is None and facts.nothing_done():
            # The decider found nothing owed: no failure, same step, look again shortly.
            status, fcls, label = "done", None, "no_action"
            step, next_at = ladder.clamp(party.get("ladder_step")), sim_now + RETRY_SOON_H
            res, note = {**claim, "next": {"action": ladder.action_for(step), "at": next_at}}, ""
        elif ok:
            status, fcls, label = "done", result.failure_class(claim, self.run.kind), claim["outcome"]
            step, next_at = self.next_timer(party, claim, facts, sim_now)
            res = {**claim, "next": {"action": ladder.action_for(step) if next_at is not None else None, "at": next_at}}
            note = claim.get("note") or label
        else:
            # The party keeps its step and is due again: the run did not do its job.
            status, fcls, label = "failed", "unsupported_result", "unsupported_result"
            step, next_at = ladder.clamp(party.get("ladder_step")), sim_now
            res = {"claimed": claim, "rejected": why}
            note = why
        released = self.release(party, status=status, failure_cls=fcls, res=res, next_at=next_at, step=step,
                                label=label, sim_now=sim_now)
        if released and fcls:
            result.save_failure_episode(self.db, self.run, fcls, note, sim_now=sim_now)
        self.announce(party, label, claim, ok, why, next_at, facts)
        return self.summary(status, fcls, res, next_at, step, released)

    def next_timer(self, party: dict, res: dict, facts: result.Facts, sim_now: float) -> tuple[int, float | None]:
        """The ladder's timer, moved by a callback the decider scheduled, and
        brought forward to now when the run ended the ladder but left work
        owed: a claim that opened a dispute (the ledger wakes only the
        counterpart), or questions still open."""
        step = ladder.clamp(party.get("ladder_step"))
        if self.run.kind == "order_system_sync":
            if facts.ops_of(result.HOLD_OPS + result.READ_OPS, result.UNSETTLED_OPS):
                return step, sim_now + ORDER_SYSTEM_RETRY_H
            return step, _order_system_timer(brief_.order_system_facts(self.ctx, party, day(sim_now)), sim_now)
        new_step, next_at, _action = ladder.after(party, res["outcome"], sim_now, callback_at=result.callback_at(facts),
                                                  memory_hour=party.get("memory_hour"))
        planned = facts.run_doc.get("callback_at")
        if planned is not None and next_at is not None:
            next_at = max(float(planned), sim_now)
        if next_at is None and brief_.gather(self.ctx, party, sim_now)["kind"] in ("followup", "ask_question"):
            next_at = sim_now
        return new_step, next_at

    def stop(self, what: str) -> dict:
        """A budget ran out: stopped, released, due again now."""
        sim_now = self.ctx.sim_now()
        party = self.db.parties.find_one({"_id": self.party["_id"]}) or self.party
        text = budget.exceeded_text(self.run, what)
        res = result.make("failed", note=text)
        released = self.release(party, status="stopped", failure_cls="budget_exceeded", res=res, next_at=sim_now,
                                step=ladder.clamp(party.get("ladder_step")), label="budget_exceeded", sim_now=sim_now)
        if released:
            result.save_failure_episode(self.db, self.run, "budget_exceeded", text, sim_now=sim_now)
        tail = f"Party {self.run.party_id} is due again" if released else "The lease was already lost"
        feed.emit(self.db, "gate_stopped", f"STOPPED {gate.who(self.run)}: {text}. {tail}", recall_id=self.run.recall_id,
                  party_id=self.run.party_id, level="bad", source="worker", system="gate", sim=sim_now,
                  detail={"run_id": self.run.id, "budget": what})
        return self.summary("stopped", "budget_exceeded", res, sim_now, ladder.clamp(party.get("ladder_step")), released)

    def refused(self, gd) -> dict:
        """The gate said no. Lost the lease (rule run/lease): nothing is ours
        to release. Otherwise release, due again shortly."""
        sim_now = self.ctx.sim_now()
        res = result.make("failed", note=f"gate refused {gd.rule}: {gd.reason}")
        if gd.rule in ("run", "lease"):
            self._end_without_release("refused", "gate_refused", res, sim_now)
            released, next_at, step = False, None, None
        else:
            party = self.db.parties.find_one({"_id": self.party["_id"]}) or self.party
            step, next_at = ladder.clamp(party.get("ladder_step")), sim_now + RETRY_SOON_H
            released = self.release(party, status="refused", failure_cls="gate_refused", res=res, next_at=next_at,
                                    step=step, label="gate_refused", sim_now=sim_now)
        result.save_failure_episode(self.db, self.run, "gate_refused", res["note"], sim_now=sim_now)
        trace.record(self.db, self.run, kind="result", sim=sim_now, output={"status": "refused", "result": res})
        return self.summary("refused", "gate_refused", res, next_at, step, released, rule=gd.rule)

    def in_doubt(self) -> dict:
        """The provider's answer was lost: the call may exist. The party is
        due again now, and recovery at the next claim decides from evidence."""
        sim_now = self.ctx.sim_now()
        party = self.db.parties.find_one({"_id": self.party["_id"]}) or self.party
        res = result.make("failed", note="the provider's answer was lost: recovery decides at the next claim")
        step = ladder.clamp(party.get("ladder_step"))
        released = self.release(party, status="stopped", failure_cls=None, res=res, next_at=sim_now, step=step,
                                label="in_doubt", sim_now=sim_now)
        return self.summary("stopped", None, res, sim_now, step, released)

    def lease_lost(self) -> dict:
        sim_now = self.ctx.sim_now()
        res = result.make("failed", note="the lease was lost while the call was live; another worker holds the party")
        self._end_without_release("refused", "gate_refused", res, sim_now)
        result.save_failure_episode(self.db, self.run, "gate_refused", res["note"], sim_now=sim_now)
        return self.summary("refused", "gate_refused", res, None, None, False, rule="lease")

    # -- writes

    def release(self, party: dict, *, status: str, failure_cls, res: dict, next_at, step: int, label: str,
                sim_now: float) -> bool:
        """The run's end and the party's timer, with the lease, in one
        transaction (timers.release). False when the fence no longer
        matches: then nothing was written there, and the run records why."""
        from ..orchestrator import timers

        fields = self._run_end(status, failure_cls, res, sim_now)
        ok = timers.release(self.ctx.client, self.db, party, self.fence, next_action_at=next_at, ladder_step=step,
                            last_outcome={"outcome": label, "at": round(sim_now, 4), "run_id": self.run.id},
                            extra={}, run_update={"_id": self.run.id, "$set": fields})
        if not ok:
            self._end_without_release("refused", "gate_refused", {**res, "lost_lease": True}, sim_now)
        trace.record(self.db, self.run, kind="result", sim=sim_now,
                     output={"status": status, "failure_class": failure_cls, "result": res, "released": ok,
                             "next_action_at": next_at, "ladder_step": step})
        return bool(ok)

    def _run_end(self, status: str, failure_cls, res: dict, sim_now: float) -> dict:
        return {"status": status, "failure_class": failure_cls, "result": res, "ended_sim": round(sim_now, 4),
                "ended_at": feed.utcnow(), "used.sim_hours": round(budget.elapsed_h(self.run, sim_now), 4)}

    def _end_without_release(self, status: str, failure_cls: str, res: dict, sim_now: float) -> None:
        self.db.task_runs.update_one({"_id": self.run.id, "status": "running"},
                                     {"$set": self._run_end(status, failure_cls, res, sim_now)})

    def announce(self, party: dict, label: str, res: dict, ok: bool, why: str, next_at, facts: result.Facts) -> None:
        """The run's line on the wall, for endings no other component reports."""
        name, when = party.get("name", self.run.party_id), fmt(next_at) if next_at is not None else "none"
        if not ok:
            claimed = res.get("outcome") if isinstance(res, dict) else res
            text, level, system = (f"REJECTED {gate.who(self.run)}: result {claimed} for {self.run.party_id} "
                                   f"is unsupported_result ({why})"), "bad", "gate"
        elif label == "no_answer":
            n = facts.attempts[-1]["seq"] if facts.attempts else "?"
            text, level, system = f"{name}: no answer, attempt {n}. Next action {when}", "warn", "phone"
        elif label == "claim_rejected":
            text, level, system = f"{name}: counts refused on this call. Next action {when}", "warn", "phone"
        elif label == "callback_requested":
            text, level, system = f"{name} asked to be called back: next action {when}", "info", "phone"
        else:
            return
        feed.emit(self.db, "run_ended", text, recall_id=self.run.recall_id, party_id=self.run.party_id, level=level,
                  source="worker", system=system, sim=self.ctx.sim_now(), detail={"run_id": self.run.id})

    def summary(self, status, failure_cls, res, next_at, step, released, **extra) -> dict:
        return {"run_id": self.run.id, "kind": self.run.kind, "party_id": self.run.party_id, "status": status,
                "failure_class": failure_cls, "result": res, "next_action_at": next_at, "ladder_step": step,
                "released": released, **extra}


def _order_system_timer(pf: dict, sim_now: float) -> float | None:
    if pf.get("next_op"):
        return sim_now
    idle = pf.get("idle")
    if idle in ("paused", "unsettled"):
        return sim_now + ORDER_SYSTEM_RETRY_H
    if idle == "read_today":
        return ladder.order_system_next(sim_now, accounts_open=True)
    return None


def _defer(ctx: HarnessContext, party: dict, fence: dict, facts: dict, sim_now: float) -> dict:
    """Nothing owed now: release the lease with the timer the reason implies,
    and start no run."""
    from ..orchestrator import timers

    if facts.get("live_attempt"):
        next_at = sim_now + RETRY_SOON_H
    elif facts.get("order_system") is not None:
        next_at = _order_system_timer(facts["order_system"], sim_now)
    else:
        next_at = facts.get("fix_check_at")
    released = timers.release(ctx.client, ctx.db, party, fence, next_action_at=next_at,
                              ladder_step=ladder.clamp(party.get("ladder_step")),
                              last_outcome=party.get("last_outcome"), extra={}, run_update=None)
    return {"run_id": None, "kind": None, "party_id": party["party_id"], "status": "deferred",
            "reason": facts.get("reason"), "next_action_at": next_at, "released": bool(released)}


# -------------------------------------------------------------------- planner

def run_planner(ctx: HarnessContext, *, worker_id: str, fence: dict, decider) -> dict:
    """One planner run under the planner lease (`worker_state` "planner", fenced like a
    party): read the one view, then set priorities / add questions / escalate
    / propose an adapter / draft the report, every call through the gate.
    The planner never touches counts, claims, attempts or operations."""
    sim_now = ctx.sim_now()
    run = budget.open_run(ctx.db, worker=worker_id, recall_id=ctx.recall["_id"], party_id=None,
                          lease={"coll": "worker_state", "_id": fence["_id"]}, lease_epoch=fence["lease_epoch"],
                          kind="planner", sim_now=sim_now, decider=getattr(decider, "name", type(decider).__name__))
    facts = brief_.gather_planner(ctx, sim_now)
    done: list[dict] = []
    status, fcls, stopped_on = "done", None, None
    while True:
        now_h = ctx.sim_now()
        decision = ("sim_hours" if budget.time_left_h(run, now_h) <= 0 else
                    _decide(ctx, run, decider, brief_.render(facts, tools=run.tools, done=done), now_h))
        if isinstance(decision, str):
            status, fcls, stopped_on = "stopped", "budget_exceeded", decision
            break
        if decision.tool is None:
            break
        gd = gate.call(ctx, run, decision.tool, decision.args, sim_now=now_h)
        if not gd.allowed:
            status, fcls = ("stopped", "budget_exceeded") if gd.rule == "budget" else ("refused", "gate_refused")
            stopped_on = "tool_calls" if gd.rule == "budget" else None
            break
        done.append({"tool": decision.tool, "target": decision.args.get("party_id"),
                     "state": "duplicate" if gd.duplicate else "ok"})
    sim_now = ctx.sim_now()
    res = {"actions": done, "note": budget.exceeded_text(run, stopped_on) if stopped_on else ""}
    ctx.db.task_runs.update_one({"_id": run.id, "status": "running"}, {"$set": {
        "status": status, "failure_class": fcls, "result": res, "ended_sim": round(sim_now, 4),
        "ended_at": feed.utcnow(), "used.sim_hours": round(budget.elapsed_h(run, sim_now), 4)}})
    if stopped_on:
        feed.emit(ctx.db, "gate_stopped", f"STOPPED {gate.who(run)}: {res['note']} (planner run)",
                  recall_id=run.recall_id, level="bad", source="worker", system="gate", sim=sim_now,
                  detail={"run_id": run.id})
    trace.record(ctx.db, run, kind="result", sim=sim_now, output={"status": status, "failure_class": fcls, "result": res})
    return {"run_id": run.id, "kind": "planner", "status": status, "failure_class": fcls, "result": res}


__all__ = ["HarnessContext", "execute", "run_planner", "start_run", "EXTEND_EVERY_S", "LEASE_S"]
