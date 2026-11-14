"""Random kills: the supervisor (V2 section 11), spec test 22 (ten kills
during a fast long run) and spec test 31 (the same with eight workers).

The supervisor tests drive scripts/supervisor.py's Supervisor with intervals
of a fraction of a second and a stand-in child process. Tests 22 and 31 run
the whole system: the API (a real HTTP server in this process, hosting the
simulator and the invariant scheduler), the two mock order systems and the worker
as separate processes, and the supervisor killing the worker with
Popen.kill(). They pass when all four invariants held at every check,
duplicates stayed at 0, and the run ended as test 16 does (the helpers are
test_long_run.py's, loaded by path).
"""
from __future__ import annotations

import importlib.util
import random
import sys
import threading
import time

import pytest

from levela.config import ROOT
from levela.db.seed import load_scenario, start_recall

RID = "RCL-2026-017"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
DIES = [sys.executable, "-c", "import os, time; time.sleep(0.3); os._exit(137)"]
SPEED = 0.3                 # seconds per simulated hour: ten days in 72 real seconds, room for ten kills
KILL_EVERY_S = (3.0, 6.0)   # long enough for each boot to start and work before it dies
DEADLINE_S = 600


def _load(name: str, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervisor = _load("levela_scripts_supervisor", ROOT / "scripts" / "supervisor.py")
long_run = _load("levela_tests_long_run", ROOT / "tests" / "test_long_run.py")


@pytest.fixture
def recall(client, db):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    return RID


def _lines(db):
    return [(x["text"], x["system"]) for x in db.feed.find({"kind": "supervisor"}).sort([("at", 1), ("_id", 1)])]


def _kills(db, rid=RID):
    return db.runs.find_one({"_id": f"run:{rid}"})["kills"]


# -- the supervisor ------------------------------------------------------------------

def test_the_command_is_the_worker_pool():
    assert supervisor.worker_command(8, "RCL-X")[1:] == ["-m", "levela", "worker", "--workers", "8", "--recall", "RCL-X"]
    assert supervisor.worker_command(1, None)[-2:] == ["--workers", "1"]


def test_the_supervisor_kills_at_random_counts_each_kill_and_restarts(db, recall):
    sup = supervisor.Supervisor(db, SLEEPER, recall_id=RID, min_s=0.3, max_s=0.5, max_kills=2, poll_s=0.05,
                                check_s=0.1, rng=random.Random(1))
    try:
        assert sup.run() == "max_kills"
        assert sup.proc.poll() is None, "the last worker is left running"
    finally:
        sup.stop_worker()
    assert _kills(db) == 2
    assert _lines(db) == [("worker killed (1)", "supervisor"), ("worker restarted", "supervisor"),
                          ("worker killed (2)", "supervisor"), ("worker restarted", "supervisor")]


def test_paused_kills_nothing_and_a_failed_invariant_stops_the_kills(db, recall):
    db.runs.update_one({"_id": f"run:{RID}"}, {"$set": {"supervisor.paused": True}})
    sup = supervisor.Supervisor(db, SLEEPER, recall_id=RID, min_s=0.2, max_s=0.3, poll_s=0.05, check_s=0.05)
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("why", sup.run()), daemon=True)
    thread.start()
    try:
        time.sleep(1.2)
        assert sup.kills == 0 and _kills(db) == 0, "no kill while paused"
        db.runs.update_one({"_id": f"run:{RID}"}, {"$set": {"invariant_failed": True}})
        thread.join(10)
        assert out["why"] == "invariant_failed"
        assert sup.proc.poll() is None, "the worker keeps running, so the failing state stays visible"
    finally:
        sup.stop_worker()
    assert _lines(db)[-1] == ("invariant check failed: kills stopped, the worker keeps running", "supervisor")


def test_a_worker_that_dies_like_a_kill_is_counted_and_restarted(db, recall):
    sup = supervisor.Supervisor(db, DIES, recall_id=RID, min_s=60, max_s=60, max_kills=1, poll_s=0.05, check_s=0.05)
    try:
        assert sup.run() == "max_kills"
    finally:
        sup.stop_worker()
    assert _kills(db) == 1
    assert _lines(db)[:2] == [("worker died with exit 137 (1)", "supervisor"), ("worker restarted", "supervisor")]


# -- tests 22 and 31: the whole system under random kills -----------------------------

def _kills_run(client, db, settings, test_uri, monkeypatch, workers: int, kills: int = 10):
    missing = long_run.missing_v2()
    if missing:
        pytest.skip(f"needs the other wave-2 modules: {', '.join(missing)}")
    with long_run.api_server(settings, client) as api, \
            long_run.mock_order_systems(long_run.worker_env(api.settings, test_uri, api.url), monkeypatch, SPEED):
        env = long_run.worker_env(api.settings, test_uri, api.url)
        rid = long_run.start_long_run(client, db, SPEED)
        sup = supervisor.Supervisor(db, supervisor.worker_command(workers, rid), recall_id=rid,
                                    min_s=KILL_EVERY_S[0], max_s=KILL_EVERY_S[1], max_kills=kills, env=env,
                                    rng=random.Random(22 + workers))
        t0 = time.monotonic()
        with long_run.BalanceSampler(client, db, rid) as sampler:
            try:
                assert sup.run() == "max_kills", "an invariant failed during the kills"
                long_run.wait_settled(client, db, rid, t0, DEADLINE_S)
            finally:
                sup.stop_worker()
        check = long_run.last_check(db, rid)
    assert _kills(db, rid) == kills
    long_run.assert_ends_like_test_16(db, rid, sampler.samples, check)


@pytest.mark.slow
@pytest.mark.timeout(900)
def test_22_ten_random_kills_during_a_fast_long_run(client, db, settings, test_uri, monkeypatch):
    _kills_run(client, db, settings, test_uri, monkeypatch, workers=1)


@pytest.mark.slow
@pytest.mark.timeout(900)
def test_31_eight_workers_with_random_kills(client, db, settings, test_uri, monkeypatch):
    _kills_run(client, db, settings, test_uri, monkeypatch, workers=8)
