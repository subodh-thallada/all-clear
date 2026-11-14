"""Simulated time (V2 §2).

Simulated time is a float: simulated hours since the run's day 0, 00:00, so
177.0 is day 7 at 09:00. It is computed, never ticked, from three settings
kept on the run document:

    sim_now = sim_start + (real_now - real_start - paused) / seconds_per_sim_hour

Real time comes from the database's clock (local time plus an offset taken
from `hello.localTime`), so the API, every worker, the simulator and the
order system agree on "now" without comparing machine clocks. Changing the speed
re-anchors the clock at the current simulated time, so it never jumps.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_SECONDS_PER_SIM_HOUR = 30.0
DEFAULT_SIM_START = 9.0          # day 0, 09:00: parties are open

_offset_lock = threading.Lock()
_offsets: dict[int, float] = {}  # id(client) -> seconds to add to local UTC


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def server_now(client=None) -> datetime:
    """The database's current time (UTC). Falls back to local time without a client."""
    local = datetime.now(timezone.utc)
    if client is None:
        return local
    key = id(client)
    with _offset_lock:
        offset = _offsets.get(key)
    if offset is None:
        t0 = time.monotonic()
        hello = client.admin.command("hello")
        rtt = time.monotonic() - t0
        server = _aware(hello["localTime"]) + timedelta(seconds=rtt / 2)
        offset = (server - datetime.now(timezone.utc)).total_seconds()
        with _offset_lock:
            _offsets[key] = offset
    return local + timedelta(seconds=offset)


@dataclass(frozen=True)
class Clock:
    real_start: datetime
    sim_start: float = DEFAULT_SIM_START
    seconds_per_sim_hour: float = DEFAULT_SECONDS_PER_SIM_HOUR
    paused_at: datetime | None = None
    paused_total_s: float = 0.0

    # ------------------------------------------------------------- documents
    @classmethod
    def from_doc(cls, doc: dict | None) -> "Clock":
        doc = doc or {}
        return cls(real_start=_aware(doc.get("real_start") or datetime.now(timezone.utc)),
                   sim_start=float(doc.get("sim_start", DEFAULT_SIM_START)),
                   seconds_per_sim_hour=float(doc.get("seconds_per_sim_hour", DEFAULT_SECONDS_PER_SIM_HOUR)),
                   paused_at=_aware(doc["paused_at"]) if doc.get("paused_at") else None,
                   paused_total_s=float(doc.get("paused_total_s", 0.0)))

    def to_doc(self) -> dict:
        return {"real_start": self.real_start, "sim_start": self.sim_start,
                "seconds_per_sim_hour": self.seconds_per_sim_hour, "paused_at": self.paused_at,
                "paused_total_s": self.paused_total_s}

    # ------------------------------------------------------------ conversion
    def sim_now(self, real_now: datetime | None = None) -> float:
        real_now = _aware(real_now) if real_now else datetime.now(timezone.utc)
        if self.paused_at is not None:
            real_now = min(real_now, self.paused_at)
        elapsed = (real_now - self.real_start).total_seconds() - self.paused_total_s
        return self.sim_start + max(0.0, elapsed) / self.seconds_per_sim_hour

    def real_at(self, sim_hour: float) -> datetime:
        """When (real, database time) the clock will read sim_hour, ignoring future pauses."""
        return self.real_start + timedelta(
            seconds=(sim_hour - self.sim_start) * self.seconds_per_sim_hour + self.paused_total_s)

    def seconds_until(self, sim_hour: float, real_now: datetime | None = None) -> float:
        if self.paused_at is not None:
            return math.inf
        real_now = _aware(real_now) if real_now else datetime.now(timezone.utc)
        return max(0.0, (self.real_at(sim_hour) - real_now).total_seconds())

    def real_seconds(self, sim_hours: float) -> float:
        return sim_hours * self.seconds_per_sim_hour

    def sim_hours(self, real_seconds: float) -> float:
        return real_seconds / self.seconds_per_sim_hour

    # ---------------------------------------------------------------- control
    def with_speed(self, seconds_per_sim_hour: float, real_now: datetime) -> "Clock":
        """Re-anchor at the current simulated time so changing speed never jumps."""
        if seconds_per_sim_hour <= 0:
            raise ValueError("seconds_per_sim_hour must be positive")
        now_sim = self.sim_now(real_now)
        paused_at = real_now if self.paused_at is not None else None
        return Clock(real_start=_aware(real_now), sim_start=now_sim, seconds_per_sim_hour=seconds_per_sim_hour,
                     paused_at=paused_at, paused_total_s=0.0)

    def paused(self, real_now: datetime) -> "Clock":
        if self.paused_at is not None:
            return self
        return Clock(self.real_start, self.sim_start, self.seconds_per_sim_hour, _aware(real_now), self.paused_total_s)

    def resumed(self, real_now: datetime) -> "Clock":
        if self.paused_at is None:
            return self
        extra = (_aware(real_now) - self.paused_at).total_seconds()
        return Clock(self.real_start, self.sim_start, self.seconds_per_sim_hour, None,
                     self.paused_total_s + max(0.0, extra))


# ------------------------------------------------------------------ calendar

def day(sim_hour: float) -> int:
    return int(math.floor(sim_hour / 24.0))


def hour_of_day(sim_hour: float) -> float:
    return sim_hour - 24.0 * day(sim_hour)


def fmt(sim_hour: float | None) -> str:
    """177.0 -> "d7 09:00"."""
    if sim_hour is None:
        return "-"
    d = day(sim_hour)
    minutes = int(round((sim_hour - 24.0 * d) * 60))
    if minutes >= 24 * 60:
        d, minutes = d + 1, minutes - 24 * 60
    return f"d{d} {minutes // 60:02d}:{minutes % 60:02d}"


def next_at_hour(sim_hour: float, hour: float) -> float:
    """The first time at or after sim_hour whose time of day is `hour`."""
    candidate = 24.0 * day(sim_hour) + hour
    return candidate if candidate >= sim_hour - 1e-9 else candidate + 24.0


def next_morning(sim_hour: float, hour: float = 9.0) -> float:
    """The next day's `hour` (strictly the following calendar day)."""
    return 24.0 * (day(sim_hour) + 1) + hour


# ------------------------------------------------------------------- lookups

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, Clock]] = {}
CACHE_S = 0.5


def _db_key(db) -> tuple:
    """The database's identity: client and name, not id(db). pymongo makes a
    new Database object on every client[name], and an id can be reused by
    another database once the first object is collected."""
    return (id(db.client), db.name)


def get_clock(db, recall_id: str, *, max_age_s: float = CACHE_S) -> Clock:
    """The clock of a recall's run (cached briefly: every worker asks often)."""
    key = (*_db_key(db), recall_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < max_age_s:
        return hit[1]
    run = db.runs.find_one({"_id": f"run:{recall_id}"}, {"clock": 1})
    clock = Clock.from_doc((run or {}).get("clock"))
    with _cache_lock:
        _cache[key] = (now, clock)
    return clock


def forget(db=None) -> None:
    """Drop cached clocks (after /admin/clock, and in tests)."""
    with _cache_lock:
        if db is None:
            _cache.clear()
        else:
            for k in [k for k in _cache if k[:2] == _db_key(db)]:
                _cache.pop(k, None)


def sim_now(db, recall_id: str, client=None) -> float:
    return get_clock(db, recall_id).sim_now(server_now(client))
