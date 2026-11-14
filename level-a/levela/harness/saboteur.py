"""The saboteur (V2 §6, spec "Saboteur tests"): a subagent that breaks one
rule on purpose, so the room can watch the harness refuse it.

    wrong_party    calls place_call on a party another worker leased  -> refused: lease
    wrong_tool     a contact run calls close_recall                   -> refused: allowlist
    double_send    sends the same effect twice                        -> one attempt / one
                                                                         hold; the second call
                                                                         returns the stored outcome
    false_success  writes operations.state = verified with no read-back -> Atlas refuses it (121);
                   then reports claim_accepted with no accepted claim -> unsupported_result
    loop           never stops calling a tool                         -> stopped at its budget,
                                                                         the party is due again
    stale          acts with an old lease epoch, as a worker paused
                   past its lease would                               -> refused: lease; its
                                                                         release writes nothing

`run_once(ctx, mode)` is what the worker calls while `runs.saboteur.on` is
set (POST /admin/saboteur). Modes that need a lease of their own claim a due
party like any worker (`orchestrator.claim.claim_next`) and run it through
`harness.execute`, so every refusal comes from the same gate the real
workers use, and the party is released for them afterwards. wrong_party and
stale never hold the party, so they write no failure episode on it: what
they did wrong is not the party's history.
"""
from __future__ import annotations

import logging
import secrets

from pymongo.errors import WriteError

from .. import feed
from ..ledger.reconciler import VALIDATION_FAILED, atlas_error
from . import gate, result
from .decide import Decision, RuleDecider

log = logging.getLogger("levela.harness.saboteur")

MODES = ("wrong_party", "wrong_tool", "double_send", "false_success", "loop", "stale")
LEASE_S = 60.0


class SaboteurDecider:
    """A decider that breaks `mode`'s rule. It never touches the database
    itself: every misdeed goes through the gate (or, for the forged
    verification, straight at Atlas's validator)."""

    is_model = False

    def __init__(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"saboteur mode must be one of {', '.join(MODES)}")
        self.mode = mode
        self.name = f"saboteur:{mode}"
        self.continues_after_effect = mode in ("double_send", "loop")
        self._rules = RuleDecider()
        self._first: Decision | None = None

    def decide(self, brief) -> Decision:
        done = brief.facts.get("done") or []
        if self.mode == "wrong_tool":
            return Decision("close_recall", note="saboteur: close the recall from a contact run")
        if self.mode == "false_success":
            return Decision(None, note="saboteur: report success without doing anything",
                            result=result.make("claim_accepted", note="counts accepted (they were not)"))
        if self.mode == "double_send":
            if len(done) >= 2:
                return Decision(None, note="saboteur: sent it twice")
            self._first = self._first or self._rules.decide(brief)
            return Decision(self._first.tool, self._first.args, note="saboteur: the same effect again")
        if self.mode == "loop":
            tools = brief.facts.get("tools") or []
            if "schedule_callback" in tools:
                return Decision("schedule_callback", {"at": brief.facts.get("sim_now", 0.0) + 1.0},
                                note="saboteur: one more time")
            self._first = self._first or self._rules.decide(brief)
            return Decision(self._first.tool or tools[0], self._first.args, note="saboteur: one more time")
        return self._rules.decide(brief)


def run_once(ctx, mode: str, *, worker_id: str | None = None, recall_id: str | None = None) -> dict:
    """One misdeed in the live run. Returns what happened (for the worker's log)."""
    if mode not in MODES:
        raise ValueError(f"saboteur mode must be one of {', '.join(MODES)}")
    ctx = ctx.for_recall(recall_id or ctx.recall["_id"])
    me = worker_id or f"{ctx.settings.worker_name}/saboteur"
    if mode == "wrong_party":
        return _wrong_party(ctx, me)
    if mode == "stale":
        return _stale(ctx)
    return _with_lease(ctx, me, mode)


def _wrong_party(ctx, me: str) -> dict:
    from . import start_run

    target = _leased_by_another(ctx, me) or ctx.db.parties.find_one(
        {"recall_id": ctx.recall["_id"], "state": {"$ne": "reconciled"}, "channel": {"$ne": "order_system"}},
        sort=[("priority", -1), ("party_id", 1)])
    if target is None:
        return {"mode": "wrong_party", "skipped": "no party to target"}
    run = start_run(ctx, target, worker_id=me, lease_epoch=int(target.get("lease_epoch") or 0),
                    kind="contact_party", decider_name="saboteur:wrong_party", saboteur="wrong_party")
    decision = gate.call(ctx, run, "place_call", {})
    _close(ctx, run, decision)
    return {"mode": "wrong_party", "run_id": run.id, "party_id": target["party_id"], "decision": decision.to_doc()}


def _stale(ctx) -> dict:
    """Act, then try to release, with the epoch before the current one."""
    from ..orchestrator import timers
    from . import start_run

    target = _leased_by_another(ctx, None) or ctx.db.parties.find_one(
        {"recall_id": ctx.recall["_id"], "lease_epoch": {"$gte": 1}, "channel": {"$ne": "order_system"}},
        sort=[("lease_epoch", -1)])
    if target is None:
        return {"mode": "stale", "skipped": "no party has been leased yet"}
    holder = target.get("lease_owner") or f"{ctx.settings.worker_name}/stale"
    old_epoch = int(target["lease_epoch"]) - 1
    run = start_run(ctx, target, worker_id=holder, lease_epoch=old_epoch, kind="contact_party",
                    decider_name="saboteur:stale", saboteur="stale")
    decision = gate.call(ctx, run, "place_call", {})
    stale_fence = {"_id": target["_id"], "lease_owner": holder, "lease_epoch": old_epoch}
    released = timers.release(ctx.client, ctx.db, target, stale_fence, next_action_at=None,
                              ladder_step=target.get("ladder_step", 1), last_outcome={"outcome": "stale"},
                              extra={}, run_update=None)
    _close(ctx, run, decision)
    return {"mode": "stale", "run_id": run.id, "party_id": target["party_id"], "decision": decision.to_doc(),
            "release_wrote": bool(released)}


def _with_lease(ctx, me: str, mode: str) -> dict:
    from ..orchestrator import claim
    from . import execute

    party = claim.claim_next(ctx.db, worker_id=me, sim_now=ctx.sim_now(), recall_id=ctx.recall["_id"],
                             lease_s=LEASE_S)
    if party is None:
        return {"mode": mode, "skipped": "nothing is due"}
    forged = forge_verified(ctx, party, me) if mode == "false_success" else None
    out = execute(ctx, party, worker_id=me, fence=claim.fence(party), decider=SaboteurDecider(mode), saboteur=mode)
    return {"mode": mode, "party_id": party["party_id"], "run": out, **({"forged": forged} if forged else {})}


def forge_verified(ctx, party: dict, me: str) -> dict:
    """Write an operation as `verified` with no read-back. The operations
    validator refuses it (code 121); the refusal goes on the wall verbatim."""
    doc = {"_id": f"op-sab-{secrets.token_hex(4)}", "recall_id": party["recall_id"], "kind": "place_hold",
           "party_id": party["party_id"], "idempotency_key": f"saboteur:{secrets.token_hex(6)}",
           "state": "verified", "readback_at": None, "readback_value": None, "saboteur": True}
    try:
        ctx.db.operations.insert_one(doc)
    except WriteError as exc:
        if exc.code != VALIDATION_FAILED:
            raise
        err = atlas_error(exc)
        feed.emit(ctx.db, "atlas_rejected", f"REJECTED {gate.short_worker(me)} (saboteur): operations.state = "
                                            "verified without readback_value", recall_id=party["recall_id"],
                  party_id=party["party_id"], level="bad", source="worker", system="atlas", detail=err)
        return {"rejected": True, "code": err.get("code")}
    log.error("the operations validator accepted a verified operation without a read-back: %s", doc["_id"])
    return {"rejected": False, "op_id": doc["_id"]}


def _leased_by_another(ctx, me: str | None) -> dict | None:
    """A party someone else holds right now ($$NOW: the database's clock)."""
    query = {"recall_id": ctx.recall["_id"], "lease_owner": {"$nin": [None, me]},
             "$expr": {"$gt": ["$lease_until", "$$NOW"]}}
    return ctx.db.parties.find_one(query)


def _close(ctx, run, decision) -> None:
    """A run that never held its party ends as refused, with nothing released."""
    ctx.db.task_runs.update_one({"_id": run.id, "status": "running"}, {"$set": {
        "status": "refused" if not decision.allowed else "done", "ended_at": feed.utcnow(),
        "failure_class": "gate_refused" if not decision.allowed else None,
        "result": result.make("failed", note=f"gate {decision.rule}: {decision.reason}")}})
