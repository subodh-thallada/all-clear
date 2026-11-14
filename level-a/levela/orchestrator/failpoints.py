"""Named failpoints that kill the worker when armed.

    after_intent        attempt inserted, provider not asked yet
    after_dial_request  provider asked and the call exists, answer not saved   (the demo kill)
    after_mark_dialed   answer saved

Arming is stored in Atlas (worker_state._id = "failpoints") so the API's
admin endpoint can arm a failpoint in a running worker. A failpoint is
one-shot: it is disarmed (conditionally, so exactly one process fires it)
before the process dies, so the restarted worker does not die again.

The default crash is os._exit(137), the same as kill -9: no finally blocks,
no cleanup, no chance to save the provider's answer. Tests pass a crash
function that raises WorkerCrash instead.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from ..config import FAILPOINTS
from .. import feed

log = logging.getLogger("levela.failpoints")
DOC_ID = "failpoints"


class WorkerCrash(BaseException):
    """Raised by the test crash function. BaseException so no `except Exception`
    in the worker can swallow it."""


def _hard_exit(name):
    log.critical("failpoint %s fired: exiting now", name)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(137)


def raise_crash(name):
    raise WorkerCrash(name)


def arm(db, name: str, *, party_id: str | None = None, channel: str | None = None, by: str = "admin") -> dict:
    if name not in FAILPOINTS:
        raise ValueError(f"unknown failpoint {name!r}; choose one of {', '.join(FAILPOINTS)}")
    spec = {"armed_at": datetime.now(timezone.utc), "party_id": party_id, "channel": channel, "by": by}
    db.worker_state.update_one({"_id": DOC_ID}, {"$set": {f"armed.{name}": spec}}, upsert=True)
    return spec


def disarm(db, name: str | None = None) -> None:
    if name is None:
        db.worker_state.update_one({"_id": DOC_ID}, {"$set": {"armed": {}}}, upsert=True)
    else:
        db.worker_state.update_one({"_id": DOC_ID}, {"$unset": {f"armed.{name}": ""}})


def armed(db) -> dict:
    return (db.worker_state.find_one({"_id": DOC_ID}) or {}).get("armed") or {}


class Failpoints:
    def __init__(self, db, *, crash=None, env=None, worker=None):
        self.db = db
        self.crash = crash or _hard_exit
        self.worker = worker
        env = os.environ if env is None else env
        # LEVELA_FAILPOINT=after_dial_request arms one from the environment (scripts).
        self.env_armed = {n.strip() for n in (env.get("LEVELA_FAILPOINT") or "").split(",") if n.strip()}

    def _matches(self, spec, attempt):
        if attempt is None:
            return True
        if spec.get("party_id") and spec["party_id"] != attempt.get("party_id"):
            return False
        if spec.get("channel") and spec["channel"] != attempt.get("channel"):
            return False
        return True

    def hit(self, name: str, attempt: dict | None = None) -> None:
        if name in self.env_armed:
            self.env_armed.discard(name)
            self._fire(name, attempt, source="env")
            return
        spec = armed(self.db).get(name)
        if not spec or not self._matches(spec, attempt):
            return
        res = self.db.worker_state.update_one({"_id": DOC_ID, f"armed.{name}": {"$exists": True}},
                                              {"$unset": {f"armed.{name}": ""}})
        if res.modified_count == 1:
            self._fire(name, attempt, source="atlas")

    def _fire(self, name, attempt, source):
        a = attempt or {}
        feed.emit(self.db, "failpoint_fired",
                  f"Failpoint {name} fired: the worker dies now"
                  + (f" (attempt {a.get('_id')} to {a.get('party_id')})" if a else ""),
                  recall_id=a.get("recall_id"), party_id=a.get("party_id"), attempt_id=a.get("_id"),
                  level="warn", source="worker", detail={"failpoint": name, "armed_via": source,
                                                         "worker": self.worker})
        self.crash(name)
