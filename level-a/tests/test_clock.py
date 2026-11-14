"""Simulated time (V2 §2): formatting, the calendar helpers, pausing,
changing speed without a jump, and the database clock's offset."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from levela import clock
from levela.clock import Clock, day, fmt, hour_of_day, next_at_hour, next_morning, server_now

T0 = datetime(2026, 11, 14, 16, 55, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ---- calendar --------------------------------------------------------------------

@pytest.mark.parametrize("hours, label", [
    (177.0, "d7 09:00"), (0.0, "d0 00:00"), (9.5, "d0 09:30"), (158.0, "d6 14:00"),
    (23.9999, "d1 00:00"), (81.5, "d3 09:30"), (None, "-"),
])
def test_fmt(hours, label):
    assert fmt(hours) == label


def test_day_and_hour_of_day():
    assert (day(177.0), hour_of_day(177.0)) == (7, 9.0)
    assert (day(23.99), day(24.0)) == (0, 1)


def test_next_at_hour():
    assert next_at_hour(170.0, 9) == 177.0          # later the same day
    assert next_at_hour(177.0, 9) == 177.0          # now counts
    assert next_at_hour(177.5, 9) == 201.0          # passed: tomorrow
    assert next_at_hour(150.0, 14.5) == 158.5


def test_next_morning_is_always_the_following_day():
    assert next_morning(170.0) == 201.0             # day 7, 02:00 -> day 8 09:00
    assert next_morning(177.0) == 201.0
    assert next_morning(190.0, hour=8) == 200.0


# ---- the clock -------------------------------------------------------------------

def test_sim_now_counts_from_the_three_numbers():
    c = Clock(real_start=T0, sim_start=9.0, seconds_per_sim_hour=30.0)
    assert c.sim_now(T0) == 9.0 and c.sim_now(at(60)) == 11.0
    assert c.sim_now(at(-60)) == 9.0                # never before the start
    assert c.real_at(11.0) == at(60) and c.seconds_until(11.0, T0) == 60.0
    assert c.seconds_until(8.0, T0) == 0.0
    assert (c.real_seconds(2.0), c.sim_hours(60.0)) == (60.0, 2.0)


def test_pause_and_resume():
    c = Clock(real_start=T0, sim_start=9.0, seconds_per_sim_hour=30.0)
    p = c.paused(at(60))
    assert p.paused(at(90)) is p                    # pausing twice changes nothing
    assert p.sim_now(at(600)) == 11.0 and math.isinf(p.seconds_until(12.0, at(600)))
    r = p.resumed(at(600))
    assert r.resumed(at(700)) is r and r.paused_total_s == 540.0
    assert r.sim_now(at(600)) == 11.0 and r.sim_now(at(660)) == 13.0
    assert r.real_at(13.0) == at(660) and r.seconds_until(14.0, at(660)) == 30.0


def test_changing_speed_never_jumps():
    c = Clock(real_start=T0, sim_start=9.0, seconds_per_sim_hour=30.0)
    fast = c.with_speed(15.0, at(60))
    assert fast.sim_now(at(60)) == 11.0 and fast.sim_now(at(90)) == 13.0
    assert fast.real_start == at(60) and fast.sim_start == 11.0
    # Changing speed while paused stays paused, at the same simulated time.
    held = c.paused(at(60)).with_speed(0.05, at(300))
    assert held.sim_now(at(900)) == 11.0
    assert held.resumed(at(900)).sim_now(at(901)) == pytest.approx(31.0)
    with pytest.raises(ValueError):
        c.with_speed(0, at(60))


def test_clock_document_round_trip():
    c = Clock(real_start=T0, sim_start=9.0, seconds_per_sim_hour=0.05).paused(at(5))
    assert Clock.from_doc(c.to_doc()) == c
    naive = Clock.from_doc({"real_start": T0.replace(tzinfo=None), "sim_start": 2})
    assert naive.real_start == T0 and naive.sim_start == 2.0 and naive.seconds_per_sim_hour == 30.0
    assert Clock.from_doc(None).sim_start == clock.DEFAULT_SIM_START


# ---- the database's clock -------------------------------------------------------------

class _FakeAdmin:
    def __init__(self, skew_s):
        self.skew_s, self.calls = skew_s, 0

    def command(self, name):
        assert name == "hello"
        self.calls += 1
        return {"localTime": datetime.now(timezone.utc) + timedelta(seconds=self.skew_s)}


class _FakeClient:
    def __init__(self, skew_s):
        self.admin = _FakeAdmin(skew_s)


def test_server_now_applies_the_databases_offset_once_measured():
    fake = _FakeClient(skew_s=100.0)
    first = server_now(fake) - datetime.now(timezone.utc)
    second = server_now(fake) - datetime.now(timezone.utc)
    assert first.total_seconds() == pytest.approx(100.0, abs=2.0)
    assert second.total_seconds() == pytest.approx(100.0, abs=2.0)
    assert fake.admin.calls == 1                     # measured once per client
    assert abs((server_now(None) - datetime.now(timezone.utc)).total_seconds()) < 1.0


def test_server_now_on_the_test_cluster(client):
    assert abs((server_now(client) - datetime.now(timezone.utc)).total_seconds()) < 120
