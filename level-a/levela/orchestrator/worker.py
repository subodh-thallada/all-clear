"""The worker pool (V2 section 5).

    python -m levela worker [--workers N] [--recall RCL] [--name NAME]

One process runs N worker threads (ids "<worker_name>/w1" .. "/wN"), one
maintenance thread, one planner thread and one saboteur thread. A worker thread
loops:

    claim     claim.claim_next: the most urgent due party, leased by $$NOW
    recover   attempts and order system operations an earlier lease left in doubt,
              settled from evidence before anything new; a call that
              outlived its run has its outcome applied to the ladder
    memory    before a party's first contact: retrieval and the fixed policy
    execute   harness.execute: one task run through the tool gate, which ends
              in timers.release with the party's next timer

and when nothing is due it sleeps until the earliest timer, at most a second.
Nothing is planned from history: on restart the pool asks Atlas what is due
and what is in doubt, and says so on the wall:

    restart 9: 3 due, 1 in doubt, 4 documents read, 0 steps replayed

Recovery at claim time. Holding the lease proves that whoever left an
attempt in doubt is gone (its lease ended), so no heartbeat is consulted:
recovery.resolve_in_doubt(fenced=True) adopts the call from evidence, or
abandons the attempt when there is none. Two cases cannot be settled yet and
the party is released to look again: an in-doubt attempt still inside its
grace period (the provider may be creating the call), and a call in flight
(a simulated call outlives the worker, like a real one). In-doubt order system
operations (operations.ORDER_SYSTEM_KINDS only: a form link or a rep escalation
rests in `sent` until a person answers it, which is not doubt) go to
levela.systems.operations.resolve_in_doubt. If recovery itself raises, the
error is reported and the party released with a retry time, so one bad
document cannot hold a party's lease forever.

A call that outlived its run. Only the run that waited for a call applies
its outcome to the ladder (no answer: next step in 4 h; "call me tomorrow":
the callback hour). A kill, an await timeout or a lost lease ends the run
first, and the call ends later. At the next claim, the party's latest call,
once final, is applied exactly once: its run is not `done`, and the release
that writes the ladder's step and timer also marks that run
`ladder_applied` in the same transaction (the fence makes it one writer).

Memory before first contact (recall option memory). A suspect_party action
creates the named party as a stub due at once, one priority above the party
that remembered it, and that party is released with a short head start
(SUSPECT_HEAD_START_S of real time) so the suspect is contacted first: in
recall 2, Corner Properties' attempt starts before Queen St Alarm's claim. That
needs a second worker thread when Corner Properties' reply waits for Queen St
Alarm's claim; with one thread the suspect's run holds until its budget
ends. ask_delivery_note is recorded on parties.memory, where the call's
dynamic variables read it.

The maintenance thread heartbeats this boot (a newer boot with the same
name supersedes it), closes stale calls (recovery.check_stale) and writes an
episode for every attempt that ended.

Saboteur mode (POST /admin/saboteur). While a run's saboteur.on is set and
saboteur.until is ahead of the database's clock, the saboteur thread runs
harness.saboteur.run_once about once a real second, cycling through its
modes, as worker "<worker_name>/w5" (the first number after the real
threads when the pool has five or more). Every refusal comes from the same
gate and Atlas validator the real workers face, and is on the feed as
"REFUSED w5 (saboteur): ...".

Crashes: failpoints raise WorkerCrash in tests and call os._exit(137) in
production. Either stops the pool mid-sentence: run() re-raises the crash
without any shutdown write. Everything else a thread raises is reported on
the feed and the thread carries on.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

from pymongo.errors import PyMongoError

from .. import clock as simclock
from .. import feed, runs
from ..config import FINAL_ATTEMPT_STATES, LIVE_ATTEMPT_STATES
from ..ledger.transfers import FREE_LEASE, resolve_party, stub_party
from . import attempts, ladder, recovery, timers
from .claim import claim_next, fence
from .failpoints import Failpoints

log = logging.getLogger("levela.worker")

DEFAULT_WORKERS = 4
IDLE_CAP_S = 1.0               # longest sleep when nothing is due
MIN_IDLE_S = 0.25              # due-but-leased parties must not turn idle threads into a hot loop
MIN_RECHECK_S = 0.2            # real seconds before a party released to look again is due
CALL_RECHECK_H = 0.1           # simulated hours between looks at a call still in flight
ERROR_RETRY_H = 1.0            # simulated hours before a party whose run raised is tried again
SUSPECT_HEAD_START_S = 3.0     # real seconds a suspect gets before the party that named it
MAINTENANCE_S = 1.0
RECALLS_TTL_S = 1.0
NEXT_DUE_TTL_S = 0.5
EPISODES_PER_PASS = 20
JOIN_S = 30.0
OPS_IN_DOUBT = ("intent", "sent")
CALL_ENDED = ("completed", "no_answer", "failed")      # a dialled call that is over
RUN_OUTLIVED = ("running", "stopped", "refused")       # a run that ended (or died) without applying its call
SABOTEUR_EVERY_S = 1.0         # real seconds between saboteur misdeeds while the switch is on
SABOTEUR_SLOT = 5              # the saboteur is "w5" on the wall (the demo pool has four threads)


def _order_system_kinds() -> list[str]:
    """operations.ORDER_SYSTEM_KINDS, imported when first needed (the pool runs
    without levela.systems until a recall has an order system)."""
    from ..systems.operations import ORDER_SYSTEM_KINDS

    return list(ORDER_SYSTEM_KINDS)


def ops_in_doubt(recall_id: str, party_id: str | None = None) -> dict:
    """Order system calls left in intent or sent. Only these can be in doubt: a form
    link or a rep escalation rests in `sent` until someone answers it."""
    flt = {"recall_id": recall_id, "state": {"$in": list(OPS_IN_DOUBT)}, "kind": {"$in": _order_system_kinds()}}
    if party_id is not None:
        flt["party_id"] = party_id
    return flt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class MemoryHooks:
    """The pool's door to levela.memory, imported lazily so the worker needs
    neither an embedder nor Vector Search until a recall asks for memory."""

    def __init__(self, db, embedder, *, k: int = 3, mode: str = "auto"):
        self.db, self.embedder, self.k, self.mode = db, embedder, k, mode

    def check_party(self, recall: dict, party: dict) -> dict:
        """The parties.memory record: {checked_at, mode, hits, actions, reason?}."""
        from ..memory import retrieve

        mode = self.mode if (recall.get("options") or {}).get("memory") else "off"
        return retrieve.check_party(self.db, self.embedder, party, k=self.k, mode=mode)

    def write_episode(self, attempt: dict, now: datetime | None = None) -> dict | None:
        from ..memory import episodes

        return episodes.write_episode_for_attempt(self.db, self.embedder, attempt, now=now)


class WorkerPool:
    """One worker process. `providers`/`probes` are keyed by channel ("voice",
    "sim"); `order systems` by order system name; `memory` is a MemoryHooks (or a stub,
    or None); `harness` is anything with execute() and HarnessContext
    (default: levela.harness); `planner` anything with run(stop_event);
    `saboteur` anything with MODES and run_once(ctx, mode, *, worker_id,
    recall_id) (default: levela.harness.saboteur)."""

    def __init__(self, settings, db, client, *, providers: dict, probes: dict, order_systems: dict | None = None,
                 memory=None, failpoints=None, decider=None, harness=None, planner=None, saboteur=None,
                 workers: int = DEFAULT_WORKERS, recall_id: str | None = None, now=None):
        if workers < 1:
            raise ValueError("a pool needs at least one worker thread")
        self.settings, self.db, self.client = settings, db, client
        self.providers, self.probes = dict(providers or {}), dict(probes or {})
        self.order_systems = dict(order_systems or {})
        self.memory, self.decider, self.planner = memory, decider, planner
        self.name = settings.worker_name
        self.failpoints = failpoints or Failpoints(db, worker=self.name)
        self.harness = harness
        self.recall_id = recall_id
        self.ids = [f"{self.name}/w{i}" for i in range(1, workers + 1)]
        # Never one of the real threads' ids: their leases and its misdeeds stay apart.
        self.saboteur_id = f"{self.name}/w{max(SABOTEUR_SLOT, workers + 1)}"
        self._saboteur = saboteur
        self._sabotage_turn = 0
        self.now = now or _utcnow
        self.boot_id: str | None = None
        self.superseded = False
        self._halt = threading.Event()
        self._crash: BaseException | None = None
        self._cache_lock = threading.Lock()
        self._recalls: tuple[float, list[str]] = (float("-inf"), [])
        self._next_due: dict[str, tuple[float, float | None]] = {}
        self._last_error: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------------

    @property
    def key(self) -> str:
        return f"worker:{self.name}"

    def me(self, worker_id: str) -> dict:
        return {"worker": worker_id, "boot": self.boot_id}

    def startup(self) -> dict:
        """Register this boot, free the leases the previous boot of this name
        left behind, and report what is due and in doubt. Reads no history:
        the returned counts are every document fetched, per recall."""
        now = self.now()
        self.boot_id = uuid.uuid4().hex
        self.superseded = False
        self.db.worker_state.update_one({"_id": self.key}, {
            "$set": {"boot_id": self.boot_id, "pid": os.getpid(), "host": socket.gethostname(),
                     "started_at": now, "heartbeat_at": now, "workers": self.ids},
            "$unset": {"stopped_at": ""}}, upsert=True)
        feed.emit(self.db, "worker_started",
                  f"Worker {self.name} started: {len(self.ids)} worker thread{'s' if len(self.ids) != 1 else ''} "
                  f"(boot {self.boot_id[:8]}, pid {os.getpid()})",
                  source="worker", system="worker",
                  detail={"worker": self.name, "boot": self.boot_id, "workers": self.ids})
        self._free_previous_repair_locks()
        summary = {rid: self._resume(rid) for rid in self._open_recalls(fresh=True)}
        if not summary:
            self._announce_resume(None, {"kills": 0, "due": 0, "in_doubt": 0, "read": 0, "freed": 0})
        totals = {k: sum(c[k] for c in summary.values()) for k in ("due", "in_doubt", "read")}
        self.db.worker_state.update_one({"_id": self.key, "boot_id": self.boot_id}, {"$set": {"resumed": {
            "at": self.now(), "due": totals["due"], "in_doubt": totals["in_doubt"],
            "documents_read": totals["read"], "steps_replayed": 0, "recalls": summary}}})
        return summary

    def run(self, stop: threading.Event | None = None) -> None:
        """Run every thread until `stop` is set, this boot is superseded, or a
        thread crashes (then the crash is re-raised, with no cleanup)."""
        stop = stop or threading.Event()
        if self.boot_id is None:
            self.startup()
        threads = [threading.Thread(target=self._guard, args=(self._worker_loop, wid), name=wid, daemon=True)
                   for wid in self.ids]
        threads.append(threading.Thread(target=self._guard, args=(self._maintenance_loop,),
                                        name=f"{self.name}/maintenance", daemon=True))
        if self.planner is not None:
            threads.append(threading.Thread(target=self._guard, args=(self.planner.run, self._halt),
                                            name=f"{self.name}/planner", daemon=True))
        threads.append(threading.Thread(target=self._guard, args=(self._saboteur_loop,),
                                        name=f"{self.name}/saboteur", daemon=True))
        for t in threads:
            t.start()
        while not stop.is_set() and not self._halt.is_set():
            stop.wait(0.2)
        self._halt.set()
        for t in threads:
            t.join(JOIN_S)
        if self._crash is not None:
            raise self._crash
        if not self.superseded:
            self.shutdown("stop requested")

    def shutdown(self, reason: str) -> None:
        """Clean stop: record stopped_at and say so on the wall. Leases still
        held lapse by themselves, and the next boot of this name frees them."""
        now = self.now()
        try:
            self.db.worker_state.update_one({"_id": self.key, "boot_id": self.boot_id},
                                            {"$set": {"stopped_at": now, "heartbeat_at": now}})
        except PyMongoError as exc:
            log.warning("could not record the stop: %s", exc)
        feed.emit(self.db, "worker_stopped", f"Worker {self.name} stopped ({reason})", source="worker",
                  system="worker", detail={"worker": self.name, "boot": self.boot_id, "reason": reason})

    def _guard(self, loop, *args) -> None:
        """A thread body. A crash (BaseException) halts the whole pool."""
        try:
            loop(*args)
        except BaseException as exc:  # noqa: BLE001 - WorkerCrash / SystemExit: the process is dying
            self._crash = self._crash or exc
            self._halt.set()

    # -- the resume line -------------------------------------------------------

    def _resume(self, recall_id: str) -> dict:
        clock = simclock.get_clock(self.db, recall_id, max_age_s=0)
        sim_now = clock.sim_now(simclock.server_now(self.client))
        freed = self._free_previous_leases(recall_id, sim_now)
        due = list(self.db.parties.find(timers.due_filter(sim_now, recall_id), {"_id": 1}))
        doubtful = list(self.db.attempts.find(recovery.in_doubt_filter([recall_id]), {"_id": 1}))
        ops = list(self.db.operations.find(ops_in_doubt(recall_id), {"_id": 1}))
        run = self.db.runs.find_one({"_id": runs.run_id(recall_id)}, {"kills": 1}) or {}
        counts = {"kills": int(run.get("kills") or 0), "due": len(due), "in_doubt": len(doubtful) + len(ops),
                  "read": len(due) + len(doubtful) + len(ops), "freed": freed}
        self._announce_resume(recall_id, counts)
        # A repair killed after activating its adapter leaves operations
        # flagged that only the planner resumes; wake it rather than wait for its
        # next scheduled pass.
        if self.db.operations.find_one({"recall_id": recall_id, "state": "needs_reverify",
                                        "kind": {"$in": _order_system_kinds()}}, {"_id": 1}):
            runs.wake_planner(self.db, recall_id, "order system operations still flagged at restart")
        # Spec: on restart the worker resolves in-doubt attempts, then runs the
        # due query. A party reconciled while the worker was down (the call's
        # claim landed meanwhile) is never claimed again, so its attempt is
        # adopted here, not at claim time.
        self.settle_orphans([recall_id])
        return counts

    def settle_orphans(self, recall_ids: list[str]) -> int:
        """Settle in-doubt attempts whose run is over: the party's lease has
        lapsed or moved to a newer epoch than the attempt's (epoch fencing; a
        live run's attempt is left to that run). Evidence decides, exactly as
        at claim time; an attempt inside its grace period is looked at again
        on a later pass. Returns how many were adopted or abandoned."""
        if not recall_ids:
            return 0
        now = self.now()
        settled = 0
        for att in self.db.attempts.find(recovery.in_doubt_filter(recall_ids)):
            party = self.db.parties.find_one({"recall_id": att["recall_id"], "party_id": att["party_id"]},
                                            {"lease_until": 1, "lease_epoch": 1})
            epoch = (att.get("owner") or {}).get("epoch")
            held = party is not None and _aware(party.get("lease_until") or FREE_LEASE) > now
            if held and epoch is not None and party.get("lease_epoch") == epoch:
                continue
            res = recovery.resolve_in_doubt(self.db, att, self.probes.get(att["channel"]), self.settings, now,
                                            self.me(self.ids[0]), fenced=True)
            settled += res["action"] in ("adopted", "abandoned")
        return settled

    def _announce_resume(self, recall_id: str | None, c: dict) -> None:
        feed.emit(self.db, "worker_resumed",
                  f"restart {c['kills']}: {c['due']} due, {c['in_doubt']} in doubt, {c['read']} documents read, "
                  "0 steps replayed",
                  recall_id=recall_id, level="good", source="worker", system="worker",
                  detail={"worker": self.name, "boot": self.boot_id, "kills": c["kills"], "due": c["due"],
                          "in_doubt": c["in_doubt"], "documents_read": c["read"], "steps_replayed": 0,
                          "leases_freed": c["freed"]})

    def _free_previous_leases(self, recall_id: str, sim_now: float) -> int:
        """The previous boot of this worker name is dead by definition (a newer
        boot has registered), so its leases need not run out first. Only due
        parties can carry one: a run that ended released its party."""
        mine = {**timers.due_filter(sim_now, recall_id), "lease_owner": {"$regex": f"^{re.escape(self.name)}/"},
                "lease_until": {"$gt": FREE_LEASE}}
        return self.db.parties.update_many(mine, {"$set": {"lease_until": FREE_LEASE}}).modified_count

    def _free_previous_repair_locks(self) -> int:
        """The same for order system repair locks: a repair killed mid-way would
        otherwise keep its order system from being repaired (or its retry resumed)
        until the lock's TTL ran out."""
        try:
            from ..systems import repair
        except ImportError:
            return 0
        return repair.release_locks(self.db, self.name)

    # -- one claim -------------------------------------------------------------

    def work_once(self, worker_id: str) -> str | None:
        """Claim one due party and act on it. Returns what happened (executed,
        recheck, ladder, suspects, closed, error), or None when nothing was due."""
        for recall_id in self._open_recalls():
            clock = simclock.get_clock(self.db, recall_id)
            sim_now = clock.sim_now(simclock.server_now(self.client))
            party = claim_next(self.db, worker_id=worker_id, sim_now=sim_now, recall_id=recall_id,
                               lease_s=self.settings.lease_s)
            if party is not None:
                return self._handle(worker_id, party, clock)
        return None

    def _handle(self, worker_id: str, party: dict, clock: simclock.Clock) -> str:
        token = fence(party)
        recall = self.db.recalls.find_one({"_id": party["recall_id"]})
        if recall is None or recall.get("status") != "open":
            self._release(party, token, None)
            return "closed"
        try:
            look_again = self._recover_at_claim(worker_id, party, recall, clock)
            late = look_again is None and self._apply_late_call(worker_id, party, token, clock)
        except Exception as exc:  # noqa: BLE001 - one bad document must not hold the party's lease forever
            retry_at = self._sim_now(clock) + max(ERROR_RETRY_H, clock.sim_hours(MIN_RECHECK_S))
            self._report_error(worker_id, exc, party=party, retry_at=retry_at, what="recovery")
            self._release(party, token, retry_at)
            return "error"
        if late:
            return "ladder"
        if look_again is not None:
            self._release(party, token, look_again)
            return "recheck"
        if self._first_contact_needs_memory(recall, party) and self._memory_first(recall, party, token, clock):
            self._release(party, token, self._sim_now(clock) + clock.sim_hours(SUSPECT_HEAD_START_S))
            return "suspects"
        try:
            self.harness.execute(self.context(recall, clock), party, worker_id=worker_id, fence=token,
                                 decider=self.decider)
        except Exception as exc:  # noqa: BLE001 - one broken run must not stop the pool
            retry_at = self._sim_now(clock) + max(ERROR_RETRY_H, clock.sim_hours(MIN_RECHECK_S))
            self._report_error(worker_id, exc, party=party, retry_at=retry_at)
            self._release(party, token, retry_at,
                          outcome={"outcome": "failed", "at": self._sim_now(clock)})
            return "error"
        return "executed"

    def context(self, recall: dict, clock: simclock.Clock):
        """The HarnessContext for one run (V2 section 6)."""
        return self.harness.HarnessContext(settings=self.settings, client=self.client, db=self.db, clock=clock,
                                           providers=self.providers, probes=self.probes, order_systems=self.order_systems,
                                           memory=self.memory, failpoints=self.failpoints, recall=recall)

    def _release(self, party: dict, token: dict, next_action_at: float | None, *, outcome=None) -> bool:
        """End a claim that ran no task: the ladder step stays where it was."""
        return timers.release(self.client, self.db, party, token, next_action_at=next_action_at,
                              ladder_step=party.get("ladder_step") or 1,
                              last_outcome=outcome if outcome is not None else party.get("last_outcome"))

    def _sim_now(self, clock: simclock.Clock) -> float:
        return clock.sim_now(simclock.server_now(self.client))

    # -- recovery at claim time ------------------------------------------------

    def _recover_at_claim(self, worker_id: str, party: dict, recall: dict, clock: simclock.Clock) -> float | None:
        """Settle what earlier leases left on this party. Every attempt and
        operation found here predates this claim, so it belongs to an earlier
        epoch. Returns the simulated hour to look again, or None to go on."""
        rid, sid = party["recall_id"], party["party_id"]
        now = self.now()
        wait_s: float | None = None
        for att in self.db.attempts.find(_unsettled(rid, sid)).sort("seq", 1):
            if attempts.in_doubt(att):
                res = recovery.resolve_in_doubt(self.db, att, self.probes.get(att["channel"]), self.settings, now,
                                                self.me(worker_id), fenced=True)
                if res["action"] == "abandoned":
                    continue
                if res["action"] == "deferred":
                    wait_s = max(wait_s or 0.0, self._grace_left(att, now))
                    continue
                att = attempts.get(self.db, att["_id"]) or att
            if att["state"] in LIVE_ATTEMPT_STATES:
                wait_s = max(wait_s or 0.0, clock.real_seconds(CALL_RECHECK_H))
        ops = list(self.db.operations.find(ops_in_doubt(rid, sid)))
        if ops:
            from ..systems import operations

            ctx = self.context(recall, clock)
            for op in ops:
                operations.resolve_in_doubt(ctx, op)
        if wait_s is None:
            return None
        return self._sim_now(clock) + clock.sim_hours(max(wait_s, MIN_RECHECK_S))

    def _grace_left(self, attempt: dict, now: datetime) -> float:
        age = (now - _aware(attempt["intent_at"])).total_seconds()
        return max(0.0, self.settings.indoubt_grace_s - age)

    def _apply_late_call(self, worker_id: str, party: dict, token: dict, clock: simclock.Clock) -> bool:
        """Apply the outcome of a call that outlived its run, once. Returns
        True when it did (the party was released with the ladder's step and
        timer, so no new contact starts in this claim).

        The candidate is the party's latest call, dialled (dial_confirmed)
        and over, from a run that ended without applying it: still `running`
        (its worker died), `stopped` (the wait for the call timed out, or the
        answer was lost) or `refused` (the lease was lost). Its outcome is
        derived from the facts exactly as the run would have (harness.result)
        and fed to ladder.after from the time the call ended, so "no answer"
        means the next step 4 h after the call and a requested callback keeps
        its hour. A timer already past is due now: the next claim acts on the
        new step. The release marks the run `ladder_applied` in the same
        transaction as the party's new step, under this claim's fence, so a
        second claim can never apply it again."""
        if party.get("channel") == "order_system" or party.get("state") == "pending":
            return False          # a distributor is never called; a pending party has never had an attempt
        rid, sid = party["recall_id"], party["party_id"]
        last = self.db.attempts.find_one({"recall_id": rid, "party_id": sid, "channel": {"$ne": "form"}},
                                         sort=[("seq", -1)])
        if (last is None or not last.get("run_id") or not last.get("dial_confirmed")
                or last.get("state") not in CALL_ENDED):
            return False
        run = self.db.task_runs.find_one({"_id": last["run_id"], "status": {"$in": list(RUN_OUTLIVED)},
                                          "ladder_applied": {"$exists": False}})
        if run is None:
            return False
        from ..harness import result

        facts = result.Facts(run_id=run["_id"], attempts=[last], party=party, run_doc=run,
                             claims=list(self.db.claims.find({"attempt_id": last["_id"]})))
        outcome = result.derive(facts)["outcome"]
        sim_now = self._sim_now(clock)
        ended = min(clock.sim_now(_aware(last["ended_at"])), sim_now) if last.get("ended_at") else sim_now
        step, next_at, _action = ladder.after(party, outcome, ended, callback_at=result.callback_at(facts),
                                              memory_hour=party.get("memory_hour"))
        planned = run.get("callback_at")
        if planned is not None and next_at is not None:
            next_at = max(float(planned), ended)
        # No timer (counts accepted) is also due now: harness.execute then
        # decides from the facts whether anything is still owed (a dispute,
        # a question) or releases the party with no timer.
        due = next_at if next_at is not None and next_at > sim_now else sim_now
        applied = {"at": round(sim_now, 4), "by": worker_id, "lease_epoch": token["lease_epoch"],
                   "attempt_id": last["_id"], "outcome": outcome, "ladder_step": step, "next_action_at": due}
        released = timers.release(self.client, self.db, party, token, next_action_at=due, ladder_step=step,
                                  last_outcome={"outcome": outcome, "at": round(sim_now, 4), "run_id": run["_id"],
                                                "late": True},
                                  run_update={"_id": run["_id"], "$set": {"ladder_applied": applied}})
        if released:
            feed.emit(self.db, "run_ended",
                      f"{party.get('name', sid)}: attempt {last.get('seq', '?')} ended after its run did "
                      f"({outcome.replace('_', ' ')}). Ladder step {step}, next action {simclock.fmt(due)}",
                      recall_id=rid, party_id=sid, level="warn" if outcome in ladder.UNANSWERED else "info",
                      source="worker", system="phone", sim=sim_now,
                      detail={"run_id": run["_id"], "attempt_id": last["_id"], "run_status": run.get("status"),
                              "late": True, "outcome": outcome, "ladder_step": step, "next_action_at": due})
        return True

    # -- memory before first contact -------------------------------------------

    def _first_contact_needs_memory(self, recall: dict, party: dict) -> bool:
        """Pending means no attempt was ever opened (the first intent moves a
        party to contacting); a memory record means the check already ran."""
        return (self.memory is not None and self.settings.memory_enabled
                and bool((recall.get("options") or {}).get("memory"))
                and party.get("state") == "pending" and party.get("channel") != "order_system"
                and not (party.get("memory") or {}).get("checked_at"))

    def _memory_first(self, recall: dict, party: dict, token: dict, clock: simclock.Clock) -> list[dict]:
        """Run retrieval and the policy once for this party (the fenced,
        conditional write decides once). Returns the suspected parties it created.
        The record goes on parties.memory, and a remembered answering hour on
        parties.memory_hour, where the contact ladder reads it."""
        from ..memory.retrieve import party_update

        record = self._retrieve(recall, party)
        fields = party_update(record)
        won = self.db.parties.update_one({**token, "memory.checked_at": {"$exists": False}},
                                        {"$set": fields}).modified_count == 1
        if not won:
            return []
        party.update(fields)
        self._announce_memory(party, record)
        created = [stub for act in record["actions"] if act.get("action") == "suspect_party"
                   if (stub := self._suspect(recall, party, act, clock)) is not None]
        if created:
            runs.wake_planner(self.db, party["recall_id"], f"memory suspected {', '.join(s['name'] for s in created)}")
        return created

    def _retrieve(self, recall: dict, party: dict) -> dict:
        record = {"checked_at": self.now(), "mode": "off", "hits": [], "actions": []}
        try:
            found = self.memory.check_party(recall, party) or {}
        except Exception as exc:  # noqa: BLE001 - memory only ever adds calls; never let it block one
            log.warning("memory check for %s failed: %s", party["party_id"], exc)
            return {**record, "error": str(exc)}
        return {**record, **{k: v for k, v in found.items() if k != "checked_at"},
                "hits": list(found.get("hits") or []), "actions": list(found.get("actions") or [])}

    def _announce_memory(self, party: dict, record: dict) -> None:
        rid, sid, name = party["recall_id"], party["party_id"], party["name"]
        if record.get("error"):
            feed.emit(self.db, "memory_note", f"Memory unavailable for {name} ({record['error']}): contacting "
                      "without it", recall_id=rid, party_id=sid, level="warn", source="worker", system="memory")
            return
        hits = record["hits"]
        if hits:
            tail = "" if record["actions"] else "; nothing to act on"
            feed.emit(self.db, "memory_hit", f"Memory ({record['mode']}): {len(hits)} similar past "
                      f"episode{'s' if len(hits) != 1 else ''} for {name}{tail}", recall_id=rid, party_id=sid,
                      source="worker", system="memory", detail={"mode": record["mode"], "hits": hits})
        if any(a.get("action") == "ask_delivery_note" for a in record["actions"]):
            feed.emit(self.db, "memory_note", f"Memory: {name} or a party like it over-reported before - the agent "
                      "will ask for the delivery note before taking counts", recall_id=rid, party_id=sid,
                      source="worker", system="memory")

    def _suspect(self, recall: dict, party: dict, act: dict, clock: simclock.Clock) -> dict | None:
        """Upsert the party a forwards_to episode names, under the id a claim
        naming it would use, due at once and ahead of the party that named it."""
        name = (act.get("name") or "").strip()
        if not name:
            return None
        rid = recall["_id"]
        to_id, entry = resolve_party(name, recall.get("directory"))
        if to_id == party["party_id"]:
            return None
        source = {"episode_id": act.get("episode_id"), "from_party": act.get("from_party") or party["party_id"]}
        doc = stub_party(rid, to_id, name, entry, "memory", self.now(),
                         extra={"memory_source": source, "priority": int(party.get("priority") or 0) + 1},
                         next_action_at=self._sim_now(clock))
        res = self.db.parties.update_one({"recall_id": rid, "party_id": to_id}, {"$setOnInsert": doc}, upsert=True)
        if res.upserted_id is None:
            return None
        feed.emit(self.db, "memory_suspect", f"Memory: {party['name']} forwarded to {doc['name']} last time - "
                  f"contacting {doc['name']} now", recall_id=rid, party_id=to_id, level="good", source="worker",
                  system="memory", detail={**source, "mode": (party.get("memory") or {}).get("mode")})
        return doc

    # -- the threads -----------------------------------------------------------

    def _worker_loop(self, worker_id: str) -> None:
        while not self._halt.is_set():
            try:
                did = self.work_once(worker_id)
            except Exception as exc:  # noqa: BLE001 - reported; the thread carries on
                self._report_error(worker_id, exc)
                self._halt.wait(IDLE_CAP_S)
                continue
            if did is None:
                self._halt.wait(self._idle_wait())

    def _maintenance_loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.maintain_once()
            except Exception as exc:  # noqa: BLE001 - reported; retried next pass
                self._report_error(f"{self.name}/maintenance", exc)
            self._halt.wait(MAINTENANCE_S)

    def _saboteur_loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.sabotage_once()
            except Exception as exc:  # noqa: BLE001 - reported; the switch is read again next pass
                self._report_error(self.saboteur_id, exc, what="saboteur")
            self._halt.wait(SABOTEUR_EVERY_S)

    @property
    def saboteur(self):
        """The saboteur module (levela.harness.saboteur unless one was given)."""
        if self._saboteur is None:
            from ..harness import saboteur

            self._saboteur = saboteur
        return self._saboteur

    def sabotage_once(self) -> list[dict]:
        """One misdeed for every open recall whose saboteur switch is on: on,
        and saboteur.until ahead of $$NOW (the database's clock, the one the
        API set it by). Each call takes the next mode in turn. Returns what
        run_once reported, per recall; a recall whose run_once raised is
        reported on the feed and the others still go."""
        rids = self._open_recalls()
        if not rids:
            return []
        switched_on = list(self.db.runs.find(
            {"_id": {"$in": [runs.run_id(r) for r in rids]}, "saboteur.on": True,
             "$expr": {"$gt": ["$saboteur.until", "$$NOW"]}}, {"recall_id": 1}))
        out = []
        for row in switched_on:
            rid = row["recall_id"]
            recall = self.db.recalls.find_one({"_id": rid})
            if recall is None or recall.get("status") != "open":
                continue
            modes = self.saboteur.MODES
            mode = modes[self._sabotage_turn % len(modes)]
            self._sabotage_turn += 1
            ctx = self.context(recall, simclock.get_clock(self.db, rid))
            try:
                res = self.saboteur.run_once(ctx, mode, worker_id=self.saboteur_id, recall_id=rid)
            except Exception as exc:  # noqa: BLE001 - one misdeed gone wrong must not stop the others
                self._report_error(self.saboteur_id, exc, what=f"saboteur {mode}")
                res = {"mode": mode, "error": f"{type(exc).__name__}: {exc}"}
            log.info("saboteur %s on %s: %s", mode, rid, res)
            out.append({"recall_id": rid, "mode": mode, **res})
        return out

    def maintain_once(self) -> dict:
        """Heartbeat, stale calls, episodes. Returns what it did."""
        if not self._heartbeat():
            return {"superseded": True}
        rids = self._open_recalls()
        return {"orphans": self.settle_orphans(rids), "stale": self._check_stale(rids),
                "episodes": self._write_episodes(rids)}

    def _idle_wait(self) -> float:
        """Until the earliest timer of any open recall, between MIN_IDLE_S and IDLE_CAP_S."""
        wait = IDLE_CAP_S
        for rid in self._open_recalls():
            clock = simclock.get_clock(self.db, rid)
            wait = min(wait, timers.seconds_until(clock, self._next_due_at(rid), simclock.server_now(self.client),
                                                  cap_s=IDLE_CAP_S))
        return max(MIN_IDLE_S, wait)

    def _next_due_at(self, recall_id: str) -> float | None:
        """timers.next_due_at, shared by the idle threads for NEXT_DUE_TTL_S."""
        now = time.monotonic()
        with self._cache_lock:
            hit = self._next_due.get(recall_id)
        if hit and now - hit[0] < NEXT_DUE_TTL_S:
            return hit[1]
        value = timers.next_due_at(self.db, recall_id)
        with self._cache_lock:
            self._next_due[recall_id] = (now, value)
        return value

    def _open_recalls(self, fresh: bool = False) -> list[str]:
        """Open run-time recalls (or just --recall), oldest first, cached briefly."""
        now = time.monotonic()
        with self._cache_lock:
            at, ids = self._recalls
        if not fresh and now - at < RECALLS_TTL_S:
            return ids
        flt = {"status": "open"}
        if self.recall_id:
            flt["_id"] = self.recall_id
        ids = [r["_id"] for r in self.db.recalls.find(flt, {"_id": 1}).sort([("opened_at", 1), ("_id", 1)])]
        with self._cache_lock:
            self._recalls = (now, ids)
        return ids

    # -- maintenance -----------------------------------------------------------

    def _heartbeat(self) -> bool:
        """False once a newer boot with the same name has registered: this
        process must stop acting, and the pool halts."""
        res = self.db.worker_state.update_one({"_id": self.key, "boot_id": self.boot_id},
                                              {"$set": {"heartbeat_at": self.now()}})
        if res.matched_count == 0 and not self.superseded:
            self.superseded = True
            self._halt.set()
            feed.emit(self.db, "worker_heartbeat_lost",
                      f"Worker {self.name} boot {self.boot_id[:8]} was replaced by a newer boot with the same "
                      "name: this process stops", level="warn", source="worker", system="worker",
                      detail={"worker": self.name, "boot": self.boot_id})
        return not self.superseded

    def _check_stale(self, rids: list) -> list:
        """A dialed or connected call whose post-call never came: close it to
        match the probe (its party cannot be called again until it closes)."""
        out, now = [], self.now()
        live = {"recall_id": {"$in": rids}, "channel": {"$ne": "form"}, "state": {"$in": ["dialed", "connected"]}}
        for att in self.db.attempts.find(live):
            res = recovery.check_stale(self.db, att, self.probes.get(att["channel"]), self.settings, now,
                                       self.me(f"{self.name}/maintenance"))
            if res["action"] not in ("fresh", "skipped"):
                out.append({"attempt_id": att["_id"], **res})
        return out

    def _write_episodes(self, rids: list) -> int:
        """An episode for every attempt that ended and has none. In-doubt
        attempts wait for recovery, so the episode tells what really happened.
        The memory module links attempts.episode_id itself (also when the
        party has no profile to embed); an attempt it could not write yet,
        say during an embedding outage, is simply tried again next pass."""
        if self.memory is None or not self.settings.memory_enabled:
            return 0
        written = 0
        ended = {"recall_id": {"$in": rids}, "state": {"$in": list(FINAL_ATTEMPT_STATES)},
                 "episode_id": {"$exists": False},
                 "$or": [{"dial_confirmed": True}, {"channel": "form"}, {"state": "abandoned"}]}
        for att in self.db.attempts.find(ended).sort("ended_at", 1).limit(EPISODES_PER_PASS):
            try:
                episode = self.memory.write_episode(att, now=self.now())
            except Exception as exc:  # noqa: BLE001 - memory is best effort; retried next pass
                log.warning("episode for %s failed: %s", att["_id"], exc)
                continue
            if episode is not None:
                written += 1
                self.db.attempts.update_one({"_id": att["_id"], "episode_id": {"$exists": False}},
                                            {"$set": {"episode_id": episode["_id"]}})
        return written

    def _report_error(self, who: str, exc: Exception, *, party: dict | None = None,
                      retry_at: float | None = None, what: str = "run") -> None:
        log.error("%s: %s", who, exc, exc_info=exc)
        text = f"{type(exc).__name__}: {exc}"
        if self._last_error.get(who) == text:
            return
        self._last_error[who] = text
        where = f" on {party['name']}" if party else ""
        tail = f"; trying again at {simclock.fmt(retry_at)}" if retry_at is not None else "; retrying"
        feed.emit(self.db, "error", f"{who}: {what}{where} failed ({text}){tail}", level="bad", source="worker",
                  system="worker", recall_id=(party or {}).get("recall_id"), party_id=(party or {}).get("party_id"),
                  detail={"worker": who, "error": text})


def _unsettled(recall_id: str, party_id: str) -> dict:
    """A party's attempts that are live, or in doubt (attempts.in_doubt)."""
    return {"recall_id": recall_id, "party_id": party_id, "channel": {"$ne": "form"},
            "$or": [{"state": {"$in": list(LIVE_ATTEMPT_STATES)}},
                    {"dial_confirmed": {"$ne": True}, "state": {"$ne": "abandoned"}}]}


# -- the process ---------------------------------------------------------------

def main(settings, *, workers: int = DEFAULT_WORKERS, recall_id: str | None = None) -> None:
    """`python -m levela worker`: build providers, order systems, memory, failpoints,
    the decider, the harness and the planner, then run until SIGINT / SIGTERM
    (SIGBREAK on Windows)."""
    from .. import harness
    from ..db.client import get_db
    from ..db.setup import setup
    from ..providers import build_providers
    from .planner import Planner

    _configure_logging()
    client, db = get_db(settings, appname="levela-worker")
    setup(db)
    providers, probes = build_providers(settings, db)
    memory = None
    if settings.memory_enabled:
        from ..memory.embed import get_embedder

        memory = MemoryHooks(db, get_embedder(settings))
    pool = WorkerPool(settings, db, client, providers=providers, probes=probes, order_systems=build_order_systems(),
                      memory=memory, failpoints=Failpoints(db, worker=settings.worker_name),
                      decider=build_decider(settings), harness=harness, workers=workers, recall_id=recall_id)
    pool.planner = Planner(settings, db, client, planner_id=f"{settings.worker_name}/planner", context=pool.context,
                     recall_id=recall_id)
    stop = threading.Event()
    _install_stop_signals(stop)
    log.info("worker %s: %d threads, channels %s, order systems %s, memory %s", settings.worker_name, workers,
             sorted(providers), sorted(pool.order_systems), "on" if memory else "off")
    pool.run(stop)


def build_decider(settings):
    """The subagent decider the settings ask for (harness.decide: the rules
    by default, Claude with PLANNER=claude and a key)."""
    from ..harness import decide

    return decide.build_decider(settings)


def build_order_systems() -> dict:
    """{order system name: client} from levela.systems.order_client (URLs from
    ORDER_<NAME>_URL); none when the systems package is not installed (the
    four-party slice needs no order system)."""
    try:
        from ..systems import order_client
    except ImportError:
        log.info("levela.systems is not available: no distributor order systems")
        return {}
    return order_client.build_order_systems()


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")


def _install_stop_signals(stop: threading.Event) -> None:
    def handler(signum, _frame):
        log.info("%s received: stopping after the runs in progress", signal.Signals(signum).name)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)
