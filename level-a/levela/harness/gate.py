"""The tool gate (V2 §6): every tool call from every subagent, the planner and
the saboteur passes through `call()`. Its checks, in order:

    1 run        the task run is still `running`
    2 lease      the leased document's lease_owner and lease_epoch are the
                 run's, and the lease has not expired (judged by $$NOW, the
                 database's clock, never a worker's)
    3 allowlist  the tool is in the run's tools
    4 budget     `$inc used.tool_calls` guarded by `$lt budget.tool_calls`
    5 intent     the attempt / operation is found by its idempotency key (a
                 duplicate returns the stored outcome and does not act) or
                 inserted; a record tool makes its write here
    6 act        the provider is called (after the commit)
    7 record     the outcome and the provider's ids are stored; never `verified`

Checks 1 to 5 run in one transaction, so a worker that lost its lease, or a
run at its limit, writes nothing at all. Every refusal writes a trace naming
the rule and, except a budget stop (the run reports that itself, once the
party is due again), a feed line on the wall:

    REFUSED w5 (saboteur): place_call on queen-st-grocer. Rule: lease. Holder is w2, epoch 9
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from pymongo.errors import DuplicateKeyError

from .. import feed, runs
from ..db.client import run_txn
from . import budget, trace
from .tools import REGISTRY, Call, Intent, Refused, stored_outcome

log = logging.getLogger("levela.harness.gate")

LIVE_INDEX = "one_live_attempt_per_party"


@dataclass
class GateDecision:
    allowed: bool
    rule: str | None = None
    outcome: dict | None = None
    evidence_id: str | None = None
    duplicate: bool = False
    reason: str = ""

    def to_doc(self) -> dict:
        return asdict(self)


def short_worker(worker_id: str | None) -> str:
    """'worker-host/w5' -> 'w5' (the wall's name for a worker thread)."""
    return (worker_id or "nobody").rsplit("/", 1)[-1]


def who(run) -> str:
    return short_worker(run.worker) + (" (saboteur)" if run.saboteur else "")


def call(ctx, run, tool: str, args: dict | None = None, *, sim_now: float | None = None) -> GateDecision:
    """Check, then act, then record one tool call for `run` (a TaskRun)."""
    sim_now = ctx.sim_now() if sim_now is None else sim_now
    c = Call(ctx=ctx, run=run, tool=tool, args=dict(args or {}), sim_now=sim_now, now=feed.utcnow())
    spec = REGISTRY.get(tool)
    try:
        intent = _admit(ctx, run, spec, c)
    except Refused as refusal:
        return _refuse(ctx, run, c, refusal)
    if intent.duplicate:
        decision = GateDecision(True, outcome=stored_outcome(intent), evidence_id=_evidence(intent), duplicate=True,
                                reason="duplicate idempotency key: the stored outcome is returned, nothing is sent")
    else:
        if spec.counts_as_step:
            runs.inc(ctx.db, run.recall_id, "steps")
        acted = spec.act(c, intent) if spec.act else None
        outcome = spec.record(c, intent, acted) if spec.record else dict(intent.doc)
        decision = GateDecision(True, outcome=outcome, evidence_id=_evidence(intent))
    trace.record(ctx.db, run, kind="tool", sim=sim_now, tool=tool, args=c.args, decision=_trace_view(decision),
                 output=decision.outcome)
    return decision


def _trace_view(decision: GateDecision) -> dict:
    """The decision as traced: the outcome is the trace's `output`, not repeated here."""
    return {k: v for k, v in decision.to_doc().items() if k != "outcome"}


def _evidence(intent: Intent) -> str | None:
    return intent.doc.get("_id") if intent.collection in ("attempts", "operations") else None


def _admit(ctx, run, spec, c: Call) -> Intent:
    """Checks 1 to 5 in one transaction. A concurrent insert of the same
    idempotency key surfaces as a duplicate-key error: the second pass then
    finds the stored document and returns it."""
    def txn(session):
        c.session = session
        _check_run(ctx.db, run, session)
        c.party = _check_lease(ctx.db, run, session)
        if spec is None or c.tool not in run.tools:
            raise Refused("allowlist", f"{c.tool} is not in {run.kind}'s tools ({', '.join(run.tools)})")
        if not budget.take(ctx.db, run.id, "tool_calls", session=session):
            raise Refused("budget", budget.exceeded_text(run, "tool_calls"))
        return spec.intent(c)

    try:
        try:
            return run_txn(ctx.client, txn)
        except DuplicateKeyError as exc:
            if LIVE_INDEX in _errmsg(exc):
                raise _atlas_refusal(exc) from exc
        try:
            return run_txn(ctx.client, txn)
        except DuplicateKeyError as exc:
            raise _atlas_refusal(exc) from exc
    finally:
        c.session = None


def _errmsg(exc: DuplicateKeyError) -> str:
    return (exc.details or {}).get("errmsg", str(exc))


def _atlas_refusal(exc: DuplicateKeyError) -> Refused:
    """Atlas's own words (the duplicate-key message) are the reason shown."""
    return Refused("intent", f"Atlas refused the intent: {_errmsg(exc)}", {"errmsg": _errmsg(exc)})


def _check_run(db, run, session) -> None:
    doc = db.task_runs.find_one({"_id": run.id}, {"status": 1}, session=session)
    if doc is None or doc.get("status") != "running":
        raise Refused("run", f"run {run.id} is {(doc or {}).get('status', 'missing')}")


def _check_lease(db, run, session) -> dict | None:
    """The leased document as it is now, if this run still holds it. $$NOW is
    the server's clock, so no worker's clock is ever compared."""
    lease = run.doc.get("lease") or {}
    rows = list(db[lease.get("coll", "parties")].aggregate(
        [{"$match": {"_id": lease.get("_id")}}, {"$addFields": {"_lease_live": {"$gt": ["$lease_until", "$$NOW"]}}}],
        session=session))
    holder = rows[0] if rows else None
    if holder is None:
        raise Refused("lease", f"nothing to lease: no {lease.get('coll')} document {lease.get('_id')}")
    owner, epoch = holder.get("lease_owner"), int(holder.get("lease_epoch") or 0)
    detail = {"holder": owner, "holder_epoch": epoch, "run_epoch": run.lease_epoch}
    if owner != run.worker or epoch != run.lease_epoch:
        held = f"Holder is {short_worker(owner)}, epoch {epoch}" if owner else f"No holder (epoch {epoch})"
        raise Refused("lease", held, detail)
    if not holder.pop("_lease_live", False):
        raise Refused("lease", f"Lease expired (epoch {epoch})", detail)
    return holder if lease.get("coll", "parties") == "parties" else None


def _refuse(ctx, run, c: Call, refusal: Refused) -> GateDecision:
    decision = GateDecision(False, rule=refusal.rule, reason=refusal.reason, outcome=refusal.detail or None)
    trace.record(ctx.db, run, kind="tool", sim=c.sim_now, tool=c.tool, args=c.args, decision=_trace_view(decision),
                 output=decision.outcome)
    if refusal.rule != "budget":
        target = run.party_id or "the recall"
        feed.emit(ctx.db, "gate_refused", f"REFUSED {who(run)}: {c.tool} on {target}. Rule: {refusal.rule}. "
                                          f"{refusal.reason}", recall_id=run.recall_id, party_id=run.party_id,
                  level="bad", source="worker", system="gate", sim=c.sim_now,
                  detail={"run_id": run.id, "rule": refusal.rule, "tool": c.tool, **refusal.detail})
    log.info("gate refused %s for %s on %s: %s", c.tool, run.worker, run.party_id, refusal)
    return decision
