"""Kill the worker at random and restart it (V2 section 11; spec "Kills and invariants").

    python scripts/supervisor.py [--recall RCL] [--workers 4] [--min-s 120] [--max-s 300]
                                 [--kills K] [--seed N]

Starts `python -m levela worker --workers N [--recall RCL]`, kills it hard
after a random 2 to 5 real minutes (Popen.kill: SIGKILL on POSIX,
TerminateProcess on Windows: no finally blocks, no cleanup), adds one to
runs.kills, says "worker killed (n)" on the wall (system supervisor) and
restarts it ("worker restarted"). The restarted worker reads what is due and
in doubt and carries on; nothing is replayed.

It honours runs.supervisor.paused: no kill while paused (the operator pauses
kills for the judge's call; the kill beat uses the failpoint instead). It
stops when runs.invariant_failed is true, leaving the worker running and
killing nothing more, so the failing state stays on the wall.

A worker that dies on its own is restarted too. When it died the way a kill
does (exit 137, the failpoint's os._exit, or SIGKILL) that death counts as a
kill survived; any other exit is reported and restarted with a backoff, so a
crash loop cannot spin.

--kills K stops after K kills and leaves the last worker running (rehearsals
and tests). Tests drive the Supervisor class with intervals of a second.
"""
from __future__ import annotations

import argparse
import logging
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

from pymongo import ReturnDocument

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levela import feed, runs  # noqa: E402
from levela.config import Settings  # noqa: E402

log = logging.getLogger("supervisor")

KILLED_EXIT_CODES = (137, -9)    # os._exit(137) (failpoints), SIGKILL on POSIX
POLL_S = 0.2
CHECK_S = 1.0                    # how often the run document is read (free tier: 100 ops/s)
STOP_WAIT_S = 30.0
QUICK_DEATH_S = 5.0
MAX_BACKOFF_S = 30.0


def worker_command(workers: int, recall_id: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "levela", "worker", "--workers", str(workers)]
    return cmd + (["--recall", recall_id] if recall_id else [])


class Supervisor:
    """Runs `command` as the worker and kills it at random.

    `run(stop)` returns why it ended: "invariant_failed", "max_kills" or
    "stopped". The worker is left running in the first two cases and
    terminated in the last.
    """

    def __init__(self, db, command: list[str], *, recall_id: str | None = None, min_s: float = 120.0,
                 max_s: float = 300.0, max_kills: int | None = None, rng: random.Random | None = None,
                 cwd: Path = ROOT, env: dict | None = None, poll_s: float = POLL_S, check_s: float = CHECK_S):
        if not 0 < min_s <= max_s:
            raise ValueError("need 0 < min_s <= max_s")
        self.db, self.command, self.recall_id = db, list(command), recall_id
        self.min_s, self.max_s, self.max_kills = min_s, max_s, max_kills
        self.rng = rng or random.Random()
        self.cwd, self.env = cwd, env
        self.poll_s, self.check_s = poll_s, check_s
        self.proc: subprocess.Popen | None = None
        self.kills = 0
        self._started_at = 0.0
        self._quick_deaths = 0

    # -- the loop

    def run(self, stop: threading.Event | None = None) -> str:
        stop = stop or threading.Event()
        self._start()
        kill_at = self._next_kill()
        run, checked = None, float("-inf")
        while not stop.is_set():
            if time.monotonic() - checked >= self.check_s:
                run, checked = self._run_doc(), time.monotonic()
                if run is not None and run.get("invariant_failed"):
                    self._say(run, "invariant check failed: kills stopped, the worker keeps running", level="bad")
                    return "invariant_failed"
            code = self.proc.poll()
            if code is not None:
                self._died(run, code)
                if self._done():
                    return "max_kills"
            elif time.monotonic() >= kill_at:
                if run is not None and not (run.get("supervisor") or {}).get("paused"):
                    self._kill(run)
                    if self._done():
                        return "max_kills"
                kill_at = self._next_kill()
            stop.wait(self.poll_s)
        self.stop_worker()
        return "stopped"

    def stop_worker(self) -> None:
        """Ask the worker to stop (the CLI's Ctrl+C); kill it if it will not."""
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(STOP_WAIT_S)

    # -- steps

    def _start(self) -> None:
        self.proc = subprocess.Popen(self.command, cwd=str(self.cwd), env=self.env)
        self._started_at = time.monotonic()
        log.info("worker started: pid %s", self.proc.pid)

    def _kill(self, run: dict) -> None:
        self.proc.kill()
        self.proc.wait(STOP_WAIT_S)
        n = self._count_kill(run)
        self._say(run, f"worker killed ({n})", level="warn")
        self._restart(run)

    def _died(self, run: dict | None, code: int) -> None:
        """The worker exited without being killed by us."""
        if code in KILLED_EXIT_CODES and run is not None:
            n = self._count_kill(run)
            self._say(run, f"worker died with exit {code} ({n})", level="warn")
            self._quick_deaths = 0
        else:
            quick = time.monotonic() - self._started_at < QUICK_DEATH_S
            self._quick_deaths = self._quick_deaths + 1 if quick else 0
            backoff = min(MAX_BACKOFF_S, 2.0 ** self._quick_deaths) if quick else 0.0
            self._say(run, f"worker exited with code {code}; restarting"
                           + (f" in {backoff:.0f} s" if backoff else ""), level="bad")
            time.sleep(backoff)
        self._restart(run)

    def _restart(self, run: dict | None) -> None:
        self._start()
        self._say(run, "worker restarted", level="info")

    def _count_kill(self, run: dict) -> int:
        self.kills += 1
        after = self.db.runs.find_one_and_update({"_id": run["_id"]}, {"$inc": {"kills": 1}},
                                                 projection={"kills": 1}, return_document=ReturnDocument.AFTER)
        return int((after or {}).get("kills") or self.kills)

    def _done(self) -> bool:
        return self.max_kills is not None and self.kills >= self.max_kills

    def _next_kill(self) -> float:
        return time.monotonic() + self.rng.uniform(self.min_s, self.max_s)

    def _run_doc(self) -> dict | None:
        """The run of --recall, else of the newest open run-time recall."""
        recall_id = self.recall_id
        if recall_id is None:
            recall = self.db.recalls.find_one({"status": "open"},
                                              {"_id": 1}, sort=[("opened_at", -1)])
            recall_id = recall["_id"] if recall else None
        if recall_id is None:
            return None
        return self.db.runs.find_one({"_id": runs.run_id(recall_id)},
                                     {"recall_id": 1, "kills": 1, "supervisor": 1, "invariant_failed": 1})

    def _say(self, run: dict | None, text: str, *, level: str) -> None:
        log.info(text)
        feed.emit(self.db, "supervisor", text, recall_id=(run or {}).get("recall_id"), level=level,
                  source="supervisor", system="supervisor",
                  detail={"pid": self.proc.pid if self.proc else None, "kills": self.kills})


def main(argv=None) -> str:
    p = argparse.ArgumentParser(prog="python scripts/supervisor.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recall", default=None, help="the recall whose run counts the kills (default: newest open)")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--min-s", type=float, default=120.0)
    p.add_argument("--max-s", type=float, default=300.0)
    p.add_argument("--kills", type=int, default=None, help="stop after this many kills")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")

    from levela.db.client import get_db

    _client, db = get_db(Settings.from_env(), appname="levela-supervisor")
    sup = Supervisor(db, worker_command(args.workers, args.recall), recall_id=args.recall, min_s=args.min_s,
                     max_s=args.max_s, max_kills=args.kills, rng=random.Random(args.seed))
    try:
        why = sup.run()
    except KeyboardInterrupt:
        sup.stop_worker()
        why = "interrupted"
    log.info("supervisor ended: %s (%d kills)", why, sup.kills)
    return why


if __name__ == "__main__":
    main()
