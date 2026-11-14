"""Traces (V2 §6): one `traces` document per decider decision and per tool
call, so any run can be read back, and replayed, step by step.

    {_id: "<run_id>:<seq>", run_id, recall_id, party_id, worker, seq,
     kind ("decision" | "tool" | "result"), brief{text, tokens, facts}, output,
     tool, args, decision{allowed, rule, outcome, evidence_id, duplicate},
     latency_ms, tokens, at, sim}

A refusal is a "tool" trace whose decision says allowed false and names the
rule. `replay()` feeds each recorded brief to a decider again and compares
its decision with the recorded one. The tools are not called: what they
returned is already inside the next recorded brief, which is exactly the
"tools stubbed with the recorded outputs" a prompt change is tested against.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("levela.harness.trace")

KINDS = ("decision", "tool", "result")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record(db, run, *, kind: str, sim: float | None = None, brief: dict | None = None, output=None,
           tool: str | None = None, args: dict | None = None, decision: dict | None = None,
           latency_ms: int | None = None, tokens: int | None = None) -> dict:
    """Write one trace for `run` (a TaskRun). Never raises on a write error:
    a lost trace must not undo an effect that already happened."""
    if kind not in KINDS:
        raise ValueError(f"trace kind must be one of {KINDS}")
    doc = {"run_id": run.id, "recall_id": run.recall_id, "party_id": run.party_id, "worker": run.worker,
           "kind": kind, "brief": brief, "output": output, "tool": tool, "args": args, "decision": decision,
           "latency_ms": latency_ms, "tokens": tokens, "at": utcnow(),
           "sim": round(float(sim), 4) if sim is not None else None}
    for _ in range(3):
        seq = run.next_seq()
        try:
            db.traces.insert_one({"_id": f"{run.id}:{seq:03d}", "seq": seq, **doc})
            return {"seq": seq, **doc}
        except DuplicateKeyError:
            continue   # a seq taken by an earlier copy of this run: take the next one
        except Exception as exc:  # noqa: BLE001 - traces are evidence, not control flow
            log.warning("trace %s:%s not written: %s", run.id, seq, exc)
            return {"seq": seq, **doc}
    return doc


def load(db, run_id: str) -> list[dict]:
    """Every trace of a run in order."""
    return list(db.traces.find({"run_id": run_id}).sort("seq", ASCENDING))


@dataclass
class ReplayStep:
    seq: int
    recorded: dict
    replayed: dict
    match: bool


def replay(db, run_id: str, decider) -> dict:
    """Re-decide every recorded decision of a run with `decider`.

    Returns {"run": the task run, "steps": [ReplayStep], "matched", "total",
    "result": the recorded result trace or None}."""
    from .brief import Brief
    from .decide import Decision

    run = db.task_runs.find_one({"_id": run_id})
    if run is None:
        raise LookupError(f"no task run {run_id}")
    traces = load(db, run_id)
    steps = []
    for t in traces:
        if t["kind"] != "decision" or not t.get("brief"):
            continue
        recorded = Decision.from_doc(t.get("output") or {})
        replayed = decider.decide(Brief.from_doc(t["brief"]))
        steps.append(ReplayStep(seq=t["seq"], recorded=recorded.to_doc(), replayed=replayed.to_doc(),
                                match=recorded.same_action(replayed)))
    result = next((t for t in reversed(traces) if t["kind"] == "result"), None)
    return {"run": run, "steps": steps, "matched": sum(s.match for s in steps), "total": len(steps),
            "result": result}
