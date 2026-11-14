"""The API's background scheduler (V2 §10).

Once per simulated hour, for every open run-time recall that has a run:

  1. hand existing questions to parties found since they were added
     (questions.fan_out_all),
  2. read each of the recall's order systems' own hold log (GET /_log/holds) and run
     the four invariants (levela.ledger.invariants.check), record them
     (invariants.record), and put the result on the wall. A failure sets
     runs.invariant_failed (the supervisor stops killing) and wakes the planner.
     An order system that cannot be read makes invariant 2 unknown, not failed.

On every tick it also fires the run's scheduled order system change
(runs.events.order_system_change_at.<order system>, simulated hours; null means "by
hand") by switching that order system to v2 once the simulated time passes it,
and makes the recall's distributor on that order system due now with a contract
probe owed, so the switch is detected within seconds.

Nothing here keeps state that matters across restarts. The hour already
checked is claimed on the run document (`invariants_hour`) with a conditional
update, and a fired order system change is recorded under events.order_system_changed, so
a restarted API, or two API processes, check each hour once and switch each
order system once. A check that cannot run at all (the database or the invariants
module failing) is shown on the wall as a warning and runs again the next
simulated hour.

The thread is started by the app's lifespan (create_app) unless
settings.scheduler_enabled is false; tests call tick() directly.
"""
from __future__ import annotations

import importlib
import logging
import math
import threading
from datetime import datetime

from .. import clock, feed, runs
from ..clock import Clock
from . import order_admin, questions

log = logging.getLogger("levela.api.scheduler")

POLL_S = 1.0             # real seconds between ticks, at most
MIN_POLL_S = 0.2         # fast clocks tick this often, at most
SWITCH_RETRY_SIM_H = 1.0  # a failed scheduled switch is retried an hour later
INVARIANTS = ("one live attempt per party", "one hold per idempotency key",
              "every accepted claim has a tool event", "mass balance equals units total")


def _detail(details, i: int):
    """The explanation for check i+1: `details` is a list in check order, or a
    dict keyed "1".."4"."""
    if isinstance(details, (list, tuple)):
        return details[i] if i < len(details) else None
    if isinstance(details, dict):
        return details.get(str(i + 1))
    return None


class Scheduler:
    def __init__(self, settings, client, db, *, http, invariants=None, poll_s: float = POLL_S):
        """`invariants` is any object with check(db, recall_id, order systems) and
        record(db, recall_id, result); default levela.ledger.invariants,
        imported on first use."""
        self.settings, self.client, self.db, self.http = settings, client, db, http
        self.poll_s = poll_s
        self._invariants = invariants
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._switch_failed: dict[tuple[str, str], float] = {}
        self._wait_s = poll_s

    # ---- thread --------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="levela-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started (tick at most every %.1f s)", self.poll_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the thread must outlive any one bad tick
                log.exception("scheduler tick failed")
            self._stop.wait(self._wait_s)

    # ---- one pass ------------------------------------------------------------

    def tick(self, *, force: bool = False) -> list[dict]:
        """One pass over the open run-time recalls. force=True runs the hourly
        work even if this simulated hour was already checked. Returns what was
        done per recall."""
        recalls = [r["_id"] for r in self.db.recalls.find({"status": "open"},
                                                          {"_id": 1})]
        if not recalls:
            return []
        rows = {r["recall_id"]: r for r in self.db.runs.find({"_id": {"$in": [runs.run_id(r) for r in recalls]}})}
        real_now = clock.server_now(self.client)
        speeds = [Clock.from_doc(r.get("clock")).seconds_per_sim_hour for r in rows.values()]
        self._wait_s = max(MIN_POLL_S, min([self.poll_s, *(s / 2 for s in speeds)]))
        done = []
        for rid in recalls:
            if rid not in rows:
                continue
            try:
                done.append(self._tick_run(rid, rows[rid], real_now, force))
            except Exception:  # noqa: BLE001 - one recall's failure must not starve the others
                log.exception("scheduler tick failed for %s", rid)
        return done

    def _tick_run(self, rid: str, run: dict, real_now: datetime, force: bool) -> dict:
        sim = Clock.from_doc(run.get("clock")).sim_now(real_now)
        fired = self._fire_order_system_changes(rid, run, sim, real_now)
        hour = math.floor(sim)
        if not (force or self._claim_hour(rid, hour)):
            return {"recall_id": rid, "sim": sim, "checked": False, "fired": fired}
        given = questions.fan_out_all(self.db, rid, sim)
        from ..monitoring import link as store_link    # WS4: connectors of stores named since the last hour
        store_link.tick(self.db, rid, client=self.client)
        try:
            result = self.check_invariants(rid, sim)
        except Exception as exc:  # noqa: BLE001 - shown on the wall; the next simulated hour checks again
            log.exception("invariant check failed to run for %s", rid)
            feed.emit(self.db, "invariants", f"Invariant check could not run: {exc.__class__.__name__}. "
                      "Next check in a simulated hour", level="warn", recall_id=rid, system="invariants", sim=sim,
                      detail={"error": str(exc)})
            result = None
        return {"recall_id": rid, "sim": sim, "checked": True, "result": result, "fired": fired, "questions": given}

    def _claim_hour(self, rid: str, hour: int) -> bool:
        res = self.db.runs.update_one(
            {"_id": runs.run_id(rid), "$or": [{"invariants_hour": None}, {"invariants_hour": {"$lt": hour}}]},
            {"$set": {"invariants_hour": hour}})
        return res.modified_count == 1

    # ---- invariants ----------------------------------------------------------

    def _checker(self):
        if self._invariants is None:
            self._invariants = importlib.import_module("levela.ledger.invariants")
        return self._invariants

    def _holds_logs(self, rid: str) -> dict:
        """{order system: its /_log/holds JSON, or None when it could not be read},
        for the order systems this recall's distributors use."""
        logs = {}
        for name in sorted(self.db.parties.distinct("order_system", {"recall_id": rid, "order_system": {"$type": "string"}})):
            url = self.settings.order_urls.get(name)
            reply = order_admin.holds_log(self.http, url) if url else None
            logs[name] = reply.body if reply is not None and reply.ok else None
        return logs

    def check_invariants(self, rid: str, sim: float) -> dict:
        """Run, record and announce the four checks for one recall."""
        logs = self._holds_logs(rid)
        checker = self._checker()
        result = checker.check(self.db, rid, logs)
        checker.record(self.db, rid, result)
        self._announce(rid, result, logs, sim)
        return result

    def _announce(self, rid: str, result: dict, logs: dict, sim: float) -> None:
        passed = list(result.get("passed") or [])
        failed = [i for i, ok in enumerate(passed) if ok is False]
        unknown = [i for i, ok in enumerate(passed) if ok is None]
        holding = sum(ok is True for ok in passed)
        details = result.get("details")
        unreachable = sorted(name for name, body in logs.items() if body is None)
        if failed:
            what = "; ".join(f"{i + 1} {INVARIANTS[i]}" + (f" ({_detail(details, i)})" if _detail(details, i) else "")
                             for i in failed)
            text, level = f"FAILED {what}. {holding} of 4 hold", "bad"
            self.db.runs.update_one({"_id": runs.run_id(rid)}, {"$set": {"invariant_failed": True}})
            runs.wake_planner(self.db, rid, f"invariant failed: {', '.join(str(i + 1) for i in failed)}")
        elif unknown:
            why = f": {', '.join(feed.orders_label(p) for p in unreachable)} unreachable" if unreachable else ""
            text, level = f"{holding} of 4 hold, {len(unknown)} unknown{why}", "warn"
        else:
            text, level = f"{holding} of 4 hold", "good"
        feed.emit(self.db, "invariants", text, level=level, recall_id=rid, source="api", system="invariants", sim=sim,
                  detail={"passed": passed, "details": details, "unreachable": unreachable})

    # ---- scheduled order system changes ------------------------------------------

    def _fire_order_system_changes(self, rid: str, run: dict, sim: float, real_now: datetime) -> list[str]:
        events = run.get("events") or {}
        changed = events.get("order_system_changed") or {}
        fired = []
        for order_system, at in sorted((events.get("order_system_change_at") or {}).items()):
            if not isinstance(at, (int, float)) or sim < at or order_system in changed:
                continue
            last_failure = self._switch_failed.get((rid, order_system))
            if last_failure is not None and sim < last_failure + SWITCH_RETRY_SIM_H:
                continue
            if self._switch(rid, order_system, at, sim, real_now):
                fired.append(order_system)
        return fired

    def _switch(self, rid: str, order_system: str, at: float, sim: float, real_now: datetime) -> bool:
        """Claim the change on the run, then switch the order system. An order system that
        does not answer releases the claim, to be retried an hour later.

        A switch that took makes the recall's distributor on that order system due
        now with a contract probe owed (order_admin.owe_probe): the change is
        detected by the agent's next order system call, and without the probe that
        call would wait for the next morning's inventory read, or never come
        once every account is reconciled."""
        key = f"events.order_system_changed.{order_system}"
        claimed = self.db.runs.update_one({"_id": runs.run_id(rid), key: {"$exists": False}},
                                          {"$set": {key: {"at": real_now, "sim": round(sim, 4), "by": "scheduler"}}})
        if not claimed.modified_count:
            return False
        url = self.settings.order_urls.get(order_system)
        reply = (order_admin.switch_version(self.http, url, self.settings.admin_token) if url
                 else order_admin.OrderReply(ok=False, error="no URL configured for this order system"))
        title = feed.orders_label(order_system)
        if reply.ok:
            self._switch_failed.pop((rid, order_system), None)
            owed = order_admin.owe_probe(self.db, rid, order_system, sim)
            feed.emit(self.db, "order_system_switched", f"{title} switched to v2 (scheduled for {clock.fmt(at)})",
                      level="warn", recall_id=rid, system=feed.orders_label(order_system), sim=sim,
                      detail={"scheduled_at": at, "order_system_reply": reply.body, "probes_owed": owed})
            return True
        self.db.runs.update_one({"_id": runs.run_id(rid)}, {"$unset": {key: ""}})
        self._switch_failed[(rid, order_system)] = sim
        feed.emit(self.db, "order_system_admin_failed", f"{title}: scheduled switch to v2 failed, {reply.error}. "
                  "Retrying in a simulated hour", level="bad", recall_id=rid, system=feed.orders_label(order_system), sim=sim,
                  detail=reply.view())
        return False
