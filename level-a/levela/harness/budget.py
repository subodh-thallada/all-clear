"""The task run's contract (V2 §6): which tools a run may use and how much it
may spend.

One `task_runs` document per run:

    {_id: "tr-<hex>", worker, recall_id, party_id, lease_epoch, lease{coll, _id},
     kind, tools[], budget{model_calls, tool_calls, sim_hours},
     used{model_calls, tool_calls, sim_hours}, status, result, failure_class,
     decider, saboteur, started_sim, ended_sim, started_at, ended_at}

`lease` names the document whose lease_owner / lease_epoch the gate compares
with the run's: the party for party runs, `worker_state` "planner" for the planner.

Spending is counted in Atlas, never in memory: `take()` is one `$inc` guarded
by `$expr used < budget`, so a run that loops is refused by the database at
its limit even if the process lost count (or never kept one).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_BUDGET = {"model_calls": 4, "tool_calls": 6, "sim_hours": 2.0}
# The time budget is simulated hours, but a run also always gets this much
# real time: on a fast clock (0.05 s per simulated hour) two simulated hours
# are 0.1 s, less than one Atlas round trip, and every run would be stopped
# before it could act. At demo speed (30 s per hour) the floor never binds.
DEFAULT_MIN_REAL_S = 20.0

# Each kind of run has its own tool list. close_recall is in none of them:
# only /admin/close may try to close a recall, and Atlas decides.
RUN_TOOLS = {
    "contact_party": ["place_call", "send_form_link", "schedule_callback", "escalate"],
    "followup": ["place_call", "schedule_callback"],
    "ask_question": ["place_call"],
    "order_system_sync": ["place_hold", "confirm_hold", "read_accounts", "read_inventory"],
    "planner": ["set_priority", "add_question", "escalate", "propose_adapter", "draft_report"],
}
KINDS = tuple(RUN_TOOLS)
COUNTED = ("model_calls", "tool_calls")
UNIT_WORDS = {"model_calls": "model calls", "tool_calls": "tool calls", "sim_hours": "simulated hours"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return f"tr-{secrets.token_hex(6)}"


@dataclass
class TaskRun:
    """A task run document plus this process's trace sequence counter. Only
    the worker that started a run writes its traces, so the counter needs no
    database round trip (the unique (run_id, seq) index still guards it)."""
    doc: dict
    seq: int = field(default=0)

    @property
    def id(self) -> str:
        return self.doc["_id"]

    @property
    def worker(self) -> str:
        return self.doc["worker"]

    @property
    def kind(self) -> str:
        return self.doc["kind"]

    @property
    def tools(self) -> list:
        return list(self.doc.get("tools") or [])

    @property
    def recall_id(self) -> str:
        return self.doc["recall_id"]

    @property
    def party_id(self) -> str | None:
        return self.doc.get("party_id")

    @property
    def lease_epoch(self) -> int:
        return int(self.doc.get("lease_epoch") or 0)

    @property
    def saboteur(self) -> str | None:
        return self.doc.get("saboteur")

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


def new_run_doc(*, worker: str, recall_id: str, party_id: str | None, lease: dict, lease_epoch: int, kind: str,
                sim_now: float, decider: str, saboteur: str | None = None, budget: dict | None = None,
                now: datetime | None = None) -> dict:
    """The document a run starts with (status running, nothing used yet)."""
    if kind not in RUN_TOOLS:
        raise ValueError(f"unknown run kind {kind!r}; choose one of {', '.join(KINDS)}")
    limits = {**DEFAULT_BUDGET, **(budget or {})}
    return {
        "_id": new_run_id(), "worker": worker, "recall_id": recall_id, "party_id": party_id,
        "lease": lease, "lease_epoch": int(lease_epoch), "kind": kind, "tools": list(RUN_TOOLS[kind]),
        "budget": limits, "used": {"model_calls": 0, "tool_calls": 0, "sim_hours": 0.0},
        "status": "running", "result": None, "failure_class": None, "decider": decider, "saboteur": saboteur,
        "started_sim": round(float(sim_now), 4), "ended_sim": None, "started_at": now or utcnow(), "ended_at": None,
    }


def open_run(db, **fields) -> TaskRun:
    doc = new_run_doc(**fields)
    db.task_runs.insert_one(doc)
    return TaskRun(doc)


def take(db, run_id: str, what: str, *, session=None) -> bool:
    """Spend one model call or tool call. False at the limit (or when the run
    is no longer running): the caller stops the run."""
    if what not in COUNTED:
        raise ValueError(f"only {', '.join(COUNTED)} are counted per call")
    res = db.task_runs.update_one(
        {"_id": run_id, "status": "running", "$expr": {"$lt": [f"$used.{what}", f"$budget.{what}"]}},
        {"$inc": {f"used.{what}": 1}}, session=session)
    return res.modified_count == 1


def elapsed_h(run: TaskRun, sim_now: float) -> float:
    return max(0.0, float(sim_now) - float(run.doc.get("started_sim") or sim_now))


# What time_left_h answers while only the real-time floor keeps a run going:
# positive (the run may continue) but too small to stretch any wait.
FLOOR_ONLY_H = 1e-9


def real_elapsed_s(run: TaskRun, now: datetime | None = None) -> float:
    started = run.doc.get("started_at")
    if started is None:
        return float("inf")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return ((now or utcnow()) - started).total_seconds()


def time_left_h(run: TaskRun, sim_now: float) -> float:
    """Simulated hours left in the run's time budget. Past the simulated
    limit, a run still inside its real-time floor (budget.min_real_s) is
    given FLOOR_ONLY_H, so it is not stopped yet."""
    limits = run.doc.get("budget") or DEFAULT_BUDGET
    left = float(limits.get("sim_hours", DEFAULT_BUDGET["sim_hours"])) - elapsed_h(run, sim_now)
    if left > 0:
        return left
    floor = float(limits.get("min_real_s", 0.0) or 0.0)
    return FLOOR_ONLY_H if real_elapsed_s(run) < floor else 0.0


def limit_of(run: TaskRun, what: str):
    return (run.doc.get("budget") or DEFAULT_BUDGET).get(what, DEFAULT_BUDGET[what])


def exceeded_text(run: TaskRun, what: str) -> str:
    """'budget_exceeded after 6 tool calls' (the wall's STOPPED line)."""
    limit = limit_of(run, what)
    shown = f"{limit:g}" if isinstance(limit, float) else str(limit)
    return f"budget_exceeded after {shown} {UNIT_WORDS[what]}"
