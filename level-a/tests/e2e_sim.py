"""Spec test 10 on the v2 engine: recall 1 with every party simulated.

The API (TestClient, hosting the SimNetwork) and a pool of four worker
threads with the real harness and the planner run in this process. Follow-ups
are manual, as in the demo, so the dispute stays open until released.

Pass: once the run settles the metrics are exact (4 of 4 checked, 225 of 240
documented, 65 recovered), the one dispute is 15 tins, every party was
called once, and Atlas refuses to close; after POST /admin/followups the
follow-up books Queen St Grocer's corrected count and the recall closes.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from levela.config import LIVE_ATTEMPT_STATES
from levela.ledger.reconciler import metrics
from levela.orchestrator.planner import Planner
from levela.orchestrator.failpoints import Failpoints
from levela.orchestrator.worker import WorkerPool
from levela.providers import ClientPoster, FakeVoice, SimNetwork

RID = "RCL-2026-017"
PARTIES = ("harbour-market", "queen-st-grocer", "lakeside-foods", "corner-mart")
FINISH_S = 30.0


def _quiet(db) -> bool:
    """No call in flight and no party leased."""
    return (db.attempts.count_documents({"recall_id": RID, "state": {"$in": list(LIVE_ATTEMPT_STATES)}}) == 0
            and db.parties.count_documents({"recall_id": RID, "$expr": {"$gt": ["$lease_until", "$$NOW"]}}) == 0)


def _wait(check, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.2)


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_10_recall_1_all_simulated_on_the_v2_engine(client, db, settings, assert_balance):
    harness = pytest.importorskip("levela.harness", reason="needs levela.harness (V2 section 6)")
    from levela.api.main import create_app
    from levela.orchestrator.worker import build_decider

    s = settings
    admin = {"X-Admin-Token": s.admin_token}
    api = TestClient(create_app(s, client=client))
    net = SimNetwork(db, ClientPoster(api, s.tool_secret), s)
    api.app.state.sim = net
    voice = FakeVoice(net)
    pool = WorkerPool(s, db, client, providers={"sim": voice, "voice": voice},
                      probes={"sim": voice.probe, "voice": voice.probe},
                      failpoints=Failpoints(db, env={}, worker=s.worker_name), decider=build_decider(s),
                      harness=harness, workers=4, recall_id=RID)
    pool.planner = Planner(s, db, client, planner_id=f"{s.worker_name}/planner", context=pool.context, recall_id=RID)
    stop = threading.Event()
    thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)

    started = api.post("/admin/recalls", headers=admin, json={"scenario": "recall_1", "all_sim": True,
                                                              "mode": "sim", "followups": "manual", "memory": False})
    assert started.status_code == 200, started.text
    t0 = time.monotonic()
    thread.start()
    try:
        _wait(lambda: metrics(db, RID)["checks_completed"] == [4, 4] and _quiet(db), 60,
              f"recall 1 did not settle: {metrics(db, RID)}")
        elapsed = time.monotonic() - t0
        m = metrics(db, RID)
        assert (m["checks_completed"], m["units_documented"], m["units_recovered"]) == ([4, 4], [225, 240], [65, 240])
        assert (m["open_units"], m["disputed_units"]) == (15, 15)
        assert_balance(RID)
        edge = db.edges.find_one({"recall_id": RID, "from_party": "queen-st-grocer", "to_party": "corner-mart"})
        assert (edge["sender_qty"], edge["recipient_qty"], edge["corroborated_qty"], edge["disputed_qty"],
                edge["state"]) == (40, 25, 25, 15, "disputed")
        assert {sid: voice.count(sid) for sid in PARTIES} == dict.fromkeys(PARTIES, 1), "one call per party"
        harbour = list(db.claims.find({"recall_id": RID, "party_id": "harbour-market"}).sort("created_at", 1))
        assert [(c["status"], c.get("code")) for c in harbour] == [("rejected", "over_count"), ("accepted", None)]
        refused = api.post("/admin/close", headers=admin, json={"recall_id": RID}).json()
        assert (refused["closed"], refused["open_units"]) == (False, 15)
        assert refused["atlas_error"]["code"] == 121, "Atlas's close gate refused it"
        assert elapsed < FINISH_S, f"recall 1 took {elapsed:.1f} s"

        assert api.post("/admin/followups", headers=admin, json={"recall_id": RID}).status_code == 200
        _wait(lambda: metrics(db, RID)["open_units"] == 0 and _quiet(db), 60,
              f"after /admin/followups the dispute was never followed up: {metrics(db, RID)}")
        assert_balance(RID)
        closed = api.post("/admin/close", headers=admin, json={"recall_id": RID}).json()
        assert closed["closed"] is True
        assert metrics(db, RID)["units_documented"] == [240, 240]
        assert db.runs.find_one({"_id": f"run:{RID}"}).get("planner_last_at") is not None, "the planner ranked the work"
        assert [line["text"] for line in db.feed.find({"kind": "error"})] == []
    finally:
        stop.set()
        thread.join(30)
        net.stop()
        net.wait_idle(10)
