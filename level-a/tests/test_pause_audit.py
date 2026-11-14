"""Spec tests 40 and 41 (spec "Borrowed from Agents You Can Audit").

40 Stop mid-run: after a pause no worker claims new work, and attempts in
   flight still reach a final state. After a resume the run carries on and
   every party is worked (the full long run of test 16 is WS2's
   test_long_run; here the pool works a four-store run on the fast clock).
41 Audit reconstruction: for one party GET /admin/audit/{party_id} returns
   every action in order, with worker, task run, gate decision, approval and
   Atlas outcome. A refusal of the saboteur appears in it.

Claims in test 41 are written as the claim path stores them (accepted, and
rejected with the two values that disagree): the audit only reads them.
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from levela import harness, runs
from levela.api import audit as audit_mod
from levela.api.main import create_app
from levela.harness import gate, saboteur
from levela.ledger.transfers import ledger_fields, stub_party
from levela.orchestrator import claim, timers
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.orchestrator.failpoints import Failpoints, raise_crash
from levela.orchestrator.worker import WorkerPool
from levela.providers import FakeVoice

from support import recall_doc

R = "RCL-PAUSE"
ADMIN = {"X-Admin-Token": "test-admin-token"}
STORES = ["harbour-market", "queen-st-grocer", "lakeside-foods", "corner-mart"]


def _seed(db, *, approved_by=None, shipped=40):
    db.recalls.insert_one(recall_doc(R, units_total=shipped * len(STORES),
                                     **({"approved_by": approved_by} if approved_by else {})))
    recall = db.recalls.find_one({"_id": R})
    for i, sid in enumerate(STORES):
        name = sid.replace("-", " ").title()
        doc = {**stub_party(R, sid, name, None, "shipment", None, product=recall, tier=1, type_="store",
                            next_action_at=0.0),
               **ledger_fields(shipped, recall), "channel": "sim", "phone": f"+1416555010{i}"}
        doc.pop("created_at", None)
        db.parties.insert_one(doc)
    runs.create_run(db, R, seconds_per_sim_hour=0.05)
    return recall


@pytest.fixture
def api(settings, client):
    return TestClient(create_app(settings, client=client, sim_network=object()))


# ------------------------------------------------------------------- test 40

class CallHarness:
    """harness.execute reduced to one call that waits for its post-call
    result (a call in flight), then releases the party with no timer."""

    def __init__(self, voice):
        self.voice, self.started, self.finished, self.lock = voice, [], [], threading.Lock()

    @staticmethod
    def HarnessContext(**fields):  # noqa: N802 - mirrors the harness's class name
        return SimpleNamespace(**fields)

    def execute(self, ctx, party, *, worker_id, fence, decider):
        with self.lock:
            self.started.append((party["party_id"], time.monotonic()))
        att = open_attempt(ctx.db, recall_id=party["recall_id"], party_id=party["party_id"], channel="sim",
                           owner={"worker": worker_id, "epoch": fence["lease_epoch"]}, to_number=party["phone"])
        placed = self.voice.place_call(to_number=party["phone"], dynamic_variables={}, attempt_id=att["_id"])
        mark_dialed(ctx.db, att["_id"], conversation_id=placed.conversation_id, call_sid=placed.call_sid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ctx.db.attempts.find_one({"_id": att["_id"], "state": "completed"}):
                break
            time.sleep(0.05)
        timers.release(ctx.client, ctx.db, party, fence, next_action_at=None, ladder_step=1,
                       last_outcome={"outcome": "claim_accepted", "at": 0.0})
        with self.lock:
            self.finished.append(party["party_id"])


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _post_call(db, attempt_id):
    """What the post-call webhook does when a call ends."""
    assert complete(db, attempt_id, "completed")


def test_spec_40_the_claim_requires_a_running_run(db):
    _seed(db)
    runs.set_state(db, R, runs.PAUSED, by="test")
    assert claim.claim_next(db, worker_id="w1", sim_now=1.0, recall_id=R) is None
    assert claim.claim_next(db, worker_id="w1", sim_now=1.0) is None, "a paused recall is left out of the filter"
    assert db.parties.count_documents({"recall_id": R, "lease_epoch": 0}) == len(STORES)
    runs.set_state(db, R, runs.RUNNING, by="test")
    got = claim.claim_next(db, worker_id="w1", sim_now=1.0, recall_id=R)
    assert got is not None and got["lease_epoch"] == 1


def test_spec_40_a_claim_that_races_a_pause_hands_its_lease_back(db, monkeypatch):
    _seed(db)
    calls = []
    real = runs.is_running

    def paused_after_the_claim(db_, rid, **kw):
        calls.append(rid)
        runs.set_state(db_, rid, runs.PAUSED, by="race")
        return real(db_, rid, **kw)

    monkeypatch.setattr(runs, "is_running", paused_after_the_claim)
    assert claim.claim_next(db, worker_id="w1", sim_now=1.0, recall_id=R) is None and calls == [R]
    leased = list(db.parties.find({"recall_id": R, "lease_owner": {"$ne": None}}))
    assert leased == [], "the raced claim was released under its fence"


def test_spec_40_pause_stops_new_work_calls_in_flight_finish_resume_carries_on(api, client, db, settings):
    _seed(db)
    voice = FakeVoice()
    h = CallHarness(voice)
    s = replace(settings, lease_s=30)
    pool = WorkerPool(s, db, client, providers={"sim": voice}, probes={"sim": voice.probe},
                      failpoints=Failpoints(db, crash=raise_crash, env={}, worker=s.worker_name),
                      harness=h, workers=2, recall_id=R)
    stop = threading.Event()
    t = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    t.start()
    try:
        assert _wait(lambda: db.attempts.count_documents({"recall_id": R, "state": "dialed"}) == 2)
        in_flight = [a["_id"] for a in db.attempts.find({"recall_id": R, "state": "dialed"})]
        res = api.post("/admin/pause", json={"recall_id": R, "reason": "judge question"}, headers=ADMIN)
        assert res.status_code == 200 and res.json()["runs"][0]["in_flight"]["attempts"] == 2
        assert runs.get_run(db, R)["state"] == "paused"
        epochs = {p["party_id"]: p["lease_epoch"] for p in db.parties.find({"recall_id": R})}
        # The calls in flight end while paused, and are recorded.
        for aid in in_flight:
            _post_call(db, aid)
        assert _wait(lambda: len(h.finished) == 2)
        time.sleep(1.5)                       # several idle passes of both threads
        assert len(h.started) == 2, "no worker claimed new work after the pause"
        assert {p["party_id"]: p["lease_epoch"] for p in db.parties.find({"recall_id": R})} == epochs
        assert db.attempts.count_documents({"_id": {"$in": in_flight}, "state": "completed"}) == 2
        assert db.feed.find_one({"recall_id": R, "kind": "run_paused"})
        res = api.post("/admin/resume", json={"recall_id": R}, headers=ADMIN)
        assert res.status_code == 200 and runs.get_run(db, R)["state"] == "running"
        assert _wait(lambda: db.attempts.count_documents({"recall_id": R, "state": "dialed"}) == 2)
        for a in db.attempts.find({"recall_id": R, "state": "dialed"}):
            _post_call(db, a["_id"])
        assert _wait(lambda: len(h.finished) == len(STORES))
        assert sorted(p for p, _ in h.started) == sorted(STORES), "each store worked once"
        assert len(voice.placed_calls()) == len(STORES), "one call per store, none lost to the pause"
    finally:
        stop.set()
        t.join(15)


def test_spec_40_pause_and_resume_need_the_admin_token(api, db):
    _seed(db)
    assert api.post("/admin/pause", json={"recall_id": R}).status_code == 401
    assert api.post("/admin/pause", json={"recall_id": "RCL-NONE"}, headers=ADMIN).status_code == 404
    out = api.post("/admin/pause", headers=ADMIN).json()          # every open recall's run
    assert [r["recall_id"] for r in out["runs"]] == [R] and out["state"] == "paused"
    assert api.post("/admin/resume", headers=ADMIN).json()["runs"][0]["state"] == "running"


# ------------------------------------------------------------------- test 41

def _ctx(settings, client, db, voice):
    return harness.HarnessContext(settings=settings, client=client, db=db, providers={"sim": voice},
                                  probes={"sim": voice.probe}, recall=db.recalls.find_one({"_id": R}))


def test_spec_41_the_audit_reconstructs_one_partys_history_with_the_saboteurs_refusal(api, client, db, settings):
    _seed(db, approved_by="Dana Ruiz, quality lead (Maple Farm Foods)")
    voice = FakeVoice()
    ctx = _ctx(settings, client, db, voice)
    party = claim.claim_next(db, worker_id="worker-test/w2", sim_now=1.0, recall_id=R, lease_s=60)
    sid = party["party_id"]
    # w2 holds the party and calls it through the gate.
    run = harness.start_run(ctx, party, worker_id="worker-test/w2", lease_epoch=party["lease_epoch"],
                            kind="contact_party", decider_name="rules")
    placed = gate.call(ctx, run, "place_call", {})
    assert placed.allowed, placed.reason
    attempt_id = placed.outcome["attempt_id"]
    # The saboteur, w5, acts on the party w2 holds: refused on lease.
    out = saboteur.run_once(ctx, "wrong_party", worker_id="worker-test/w5", recall_id=R)
    assert out["party_id"] == sid and out["decision"]["rule"] == "lease"
    # The store's first answer is refused by Atlas, the corrected one accepted.
    base = {"recall_id": R, "party_id": sid, "attempt_id": attempt_id, "source": "sim"}
    db.claims.insert_many([
        {**base, "_id": "cl-1", "dedupe_key": "k1", "status": "rejected", "code": "over_count",
         "reject_reason": "sold + on_shelf + held + returned + forwarded is 60 but 40 were shipped",
         "values": {"reported": 60, "shipped_in": 40}, "created_at": datetime.now(timezone.utc)},
        {**base, "_id": "cl-2", "dedupe_key": "k2", "status": "accepted", "code": None, "reject_reason": None,
         "created_at": datetime.now(timezone.utc)},
    ])
    complete(db, attempt_id, "completed")

    res = api.get(f"/admin/audit/{sid}", params={"recall_id": R}, headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    rows = body["history"]
    whats = [r["what"] for r in rows]
    assert whats.index("task_run") < whats.index("gate") < whats.index("claim"), whats
    ats = [r["at"] for r in rows]
    assert ats == sorted(ats), "in order"
    # Who acted, under which run and lease, under whose approval.
    legit = next(r for r in rows if r["what"] == "gate" and r["gate"]["allowed"])
    assert legit["worker"] == "worker-test/w2" and legit["tool"] == "place_call"
    assert legit["task_run"] == {"run_id": run.id, "kind": "contact_party", "lease_epoch": party["lease_epoch"]}
    assert legit["gate"]["evidence_id"] == attempt_id
    assert all(r["approval"]["approved_by"].startswith("Dana Ruiz") for r in rows)
    assert all(r["approval"]["source"] == "manual" for r in rows)
    # The saboteur's refusal, with the rule.
    refused = [r for r in rows if r["what"] == "gate" and not r["gate"]["allowed"]]
    assert len(refused) == 1 and refused[0]["saboteur"] == "wrong_party"
    assert refused[0]["worker"] == "worker-test/w5" and refused[0]["gate"]["rule"] == "lease"
    assert "Holder is w2" in refused[0]["gate"]["reason"]
    # What Atlas accepted and refused.
    claims = [r["atlas"] for r in rows if r["what"] == "claim"]
    assert [c["status"] for c in claims] == ["rejected", "accepted"]
    assert claims[0]["values"] == {"reported": 60, "shipped_in": 40}
    attempt = next(r for r in rows if r["what"] == "attempt")
    assert attempt["attempt"]["state"] == "completed" and attempt["worker"] == "worker-test/w2"
    assert body["summary"]["saboteur_refusals"] == 1 and body["summary"]["claims_rejected"] == 1
    assert api.get(f"/admin/audit/{sid}").status_code == 401
    assert api.get("/admin/audit/nobody", headers=ADMIN).status_code == 404


def test_spec_41_an_atlas_refusal_of_a_forged_verification_is_in_the_audit(client, db, settings):
    _seed(db)
    ctx = _ctx(settings, client, db, FakeVoice())
    party = db.parties.find_one({"recall_id": R, "party_id": "corner-mart"})
    forged = saboteur.forge_verified(ctx, party, "worker-test/w5")
    assert forged["rejected"] is True
    rows = audit_mod.audit(db, "corner-mart", R)
    assert [r["what"] for r in rows] == ["atlas"]
    assert rows[0]["atlas"]["status"] == "rejected" and "without readback_value" in rows[0]["atlas"]["reason"]
