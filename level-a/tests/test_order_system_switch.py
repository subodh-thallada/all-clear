"""An order system switch is noticed within seconds (V2 §7 "Detect"), and the
order system beats of the demo (spec "Demo script" 1:45 and 2:20).

A switch is detected only by an order system call. After its accounts are read a
distributor calls its order system once a simulated day, and never again once
every account is reconciled, so the switch (the operator's button or the
scheduler) makes the distributor due now with one contract probe owed: a
read through the active adapter under its own key. Its ContractError is
contained and repaired like any operation's.

The end-to-end test runs the API, a pool of worker threads with the real
harness and the planner, and both mock order systems in this process (behind
httpx.ASGITransport, as tests/test_order_system_change.py does), on a recall
whose accounts are all reconciled: North is switched by the button and
repaired by the model; East, switched afterwards, repairs from North's
remembered adapter episode with no model call.

The facts tests also cover the confirm step after a repair: a hold already
in force needs no second write, and a hold still awaiting its read-back is
waited for rather than refused at the gate.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from levela import clock, runs
from levela.api import order_admin
from levela.clock import Clock
from levela.clock import day as sim_day
from levela.harness import brief
from levela.ledger import submit_claim
from levela.orchestrator.claim import claim_next
from levela.systems import adapters
from levela.systems import operations as ops
from levela.systems.repair import ScriptedRepairer, request_repair
from test_mock_order_system import EAST, BATCH, NORTH, TOKEN, V2_PROPOSAL, make_order_system, v2_adapter
from test_order_system_change import FAILURE
from test_order_system_hold import RID, feed_texts, make_ctx, start

ADMIN = {"X-Admin-Token": TOKEN}
PORTS = {8101: "north", 8102: "east"}
SPEED = 1.0          # real seconds per simulated hour in the end-to-end run
DETECT_S = 30.0      # real seconds a switch may take to be detected (Atlas latency included)
REPAIR_S = 60.0


def pin(db, hour: float) -> None:
    """Freeze the run's clock at `hour`, so every simulated time is exact."""
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, RID, Clock(real_start=t0, sim_start=hour, paused_at=t0))
    clock.forget()


def dist(db, order_system: str = "north") -> dict:
    return db.parties.find_one({"recall_id": RID, "party_id": f"dist-{order_system}"})


def put_op(db, kind: str, state: str, *, status: str | None = None, party: str = "dist-north",
           order_system: str = "north", key: str | None = None, **fields) -> dict:
    """An operation as an earlier run left it."""
    doc = ops.new_op(recall_id=RID, kind=kind, order_system=order_system, party_id=party, adapter_version=1, sim=9.0)
    doc.update(state=state, **fields)
    if key is not None:
        doc["idempotency_key"] = key
    if state == "verified":
        doc.update(readback_at=9.1, readback_value={"status": status} if status else {"accounts": 0})
    db.operations.insert_one(doc)
    return doc


def activate_v2(db, order_system: str = "north") -> None:
    """The repaired adapter is the active one."""
    db.adapters.insert_one({**v2_adapter(order_system), "status": "active"})


# -- the confirm step after a repair (systems-7) ----------------------------------------------

@pytest.mark.parametrize("hold, owed, idle", [
    # In force under v1 ("on_hold"): v2's confirm step has nothing to confirm.
    ({"state": "verified", "status": "on_hold"}, None, "reconciled"),
    # Placed under v2 and read back pending: the confirm is owed.
    ({"state": "verified", "status": "pending_confirm"}, "confirm_hold", None),
    # Not read back yet: no verified hold to confirm, so wait for the read-back.
    ({"state": "sent"}, None, "unsettled"),
    ({"state": "needs_reverify"}, None, "unsettled"),
])
def test_the_confirm_step_is_owed_only_for_a_hold_read_back_pending(client, db, settings, hold, owed, idle):
    start(client, db)
    activate_v2(db)
    put_op(db, "place_hold", hold["state"], status=hold.get("status"))
    put_op(db, "read_accounts", "verified")
    ctx = make_ctx(settings, client, db, {})
    facts = brief.order_system_facts(ctx, dist(db), 0)
    assert (facts["next_op"], facts["idle"]) == (owed, idle), facts
    if hold["state"] != "verified":
        with pytest.raises(ValueError, match="no verified hold to confirm"):
            ops.intent_for(ctx, "confirm_hold", dist(db))     # what the gate would have refused


def test_a_confirm_already_asked_for_is_not_owed_again(client, db, settings):
    start(client, db)
    activate_v2(db)
    put_op(db, "place_hold", "verified", status="pending_confirm")
    put_op(db, "confirm_hold", "sent")
    put_op(db, "read_accounts", "verified")
    facts = brief.order_system_facts(make_ctx(settings, client, db, {}), dist(db), 0)
    assert (facts["next_op"], facts["idle"]) == (None, "reconciled")


# -- the probe a switch owes ------------------------------------------------------------------

def test_a_switch_makes_a_reconciled_distributor_due_with_a_probe_under_its_own_key(client, db, settings):
    start(client, db)
    pin(db, 30.0)                                                    # d1 06:00
    db.adapters.insert_one(adapters.builtin("north"))
    put_op(db, "place_hold", "verified", status="on_hold")
    put_op(db, "read_accounts", "verified")
    db.parties.update_one({"_id": dist(db)["_id"]}, {"$set": {"state": "reconciled", "next_action_at": None}})
    ctx = make_ctx(settings, client, db, {})
    assert brief.order_system_facts(ctx, dist(db), 1)["idle"] == "reconciled"
    assert claim_next(db, worker_id="w-test", sim_now=10_000.0, recall_id=RID) is None, "nothing calls the order system"

    assert order_admin.owe_probe(db, RID, "north", 30.0) == 1
    party = dist(db)
    assert (party["order_system_probe_due"], party["order_system_probes"], party["next_action_at"]) == (True, 1, 30.0)
    facts = brief.order_system_facts(ctx, party, 1)
    assert (facts["next_op"], facts["probe"]) == ("read_inventory", {"n": 1, "state": None})
    probe = ops.intent_for(ctx, "read_inventory", party)
    assert probe["idempotency_key"] == f"{RID}:dist-north:read_inventory:d1:probe1"
    assert (probe["probe"], probe["request"]) == (1, {"batch": BATCH})
    claimed = claim_next(db, worker_id="w-test", sim_now=30.0, recall_id=RID)
    assert claimed is not None and claimed["party_id"] == "dist-north", "a reconciled distributor owing a probe is due"

    # Opened on day 1 and not read back by day 2: waited for, and asking again finds it rather than reading twice.
    ops.open_op(db, probe)
    pin(db, 57.0)                                                    # d2 09:00
    facts = brief.order_system_facts(ctx, dist(db), 2)
    assert (facts["next_op"], facts["idle"], facts["probe"]) == (None, "unsettled", {"n": 1, "state": "intent"})
    assert ops.intent_for(ctx, "read_inventory", dist(db))["idempotency_key"] == probe["idempotency_key"]

    # Read back: the debt is settled, and the probe counted as day 1's inventory read.
    recall = db.recalls.find_one({"_id": RID})
    ops.upsert_accounts(db, recall, dist(db), NORTH[:1], now=datetime.now(timezone.utc), sim=30.0)
    db.operations.update_one({"_id": probe["_id"]}, {"$set": {"state": "verified", "readback_at": 57.0,
                                                              "readback_value": {"accounts": 1}}})
    facts = brief.order_system_facts(ctx, dist(db), 1)
    assert (facts["next_op"], facts["idle"], facts["probe"]) == (None, "read_today", None)
    assert dist(db)["order_system_probe_due"] is False
    daily = ops.intent_for(ctx, "read_inventory", dist(db))
    assert brief.order_system_facts(ctx, dist(db), 2)["next_op"] == "read_inventory"
    assert daily["idempotency_key"] == f"{RID}:dist-north:read_inventory:d2" and "probe" not in daily

    # The next switch owes the next probe.
    order_admin.owe_probe(db, RID, "north", 57.0)
    party = dist(db)
    assert (party["order_system_probe_due"], party["order_system_probes"]) == (True, 2)
    assert brief.order_system_facts(ctx, party, 2)["probe"] == {"n": 2, "state": None}
    assert ops.intent_for(ctx, "read_inventory", party)["idempotency_key"] == (
        f"{RID}:dist-north:read_inventory:d2:probe2")


def test_a_probe_flagged_by_contain_settles_the_debt(client, db, settings):
    """The switch was noticed: the repair's retry finishes the read, the
    distributor does not owe a second probe."""
    start(client, db)
    put_op(db, "place_hold", "verified", status="on_hold")
    put_op(db, "read_accounts", "verified")
    order_admin.owe_probe(db, RID, "north", 9.0)
    put_op(db, "read_inventory", "needs_reverify", probe=1, key=f"{RID}:dist-north:read_inventory:d0:probe1")
    db.parties.update_one({"_id": dist(db)["_id"]}, {"$set": {"order_system_paused": True}})
    facts = brief.order_system_facts(make_ctx(settings, client, db, {}), dist(db), 0)
    assert (facts["idle"], facts["probe"]) == ("paused", None)
    assert dist(db)["order_system_probe_due"] is False and dist(db)["order_system_probes"] == 1


# -- end to end: switch -> detect -> contain -> repair -> retry, then the second order system ---------

def forward_to(apps: dict) -> httpx.Client:
    """The API's order system client: each request goes to the in-process order system
    app its URL's port names (settings' default order system URLs)."""
    def handler(request: httpx.Request) -> httpx.Response:
        app = apps[PORTS[request.url.port]]

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://order-system") as http:
                return await http.request(request.method, request.url.raw_path.decode(), content=request.content,
                                          headers={k: v for k, v in request.headers.items()
                                                   if k.lower() in ("content-type", "x-admin-token")})
        answer = asyncio.run(go())
        return httpx.Response(answer.status_code, content=answer.content,
                              headers={"content-type": answer.headers.get("content-type", "application/json")})
    return httpx.Client(transport=httpx.MockTransport(handler))


def reconcile(client, db, ctx, distributor: dict) -> None:
    """Under v1: the hold verified, the accounts read (a party each, the
    distributor's forwards claim), then every account's receipt corroborated
    by its own claim, so every party of this distributor is reconciled."""
    assert ops.place_hold(ctx, distributor)["action"] == "verified"
    read = ops.read_accounts(ctx, distributor)
    assert read["action"] == "verified" and read["claim"]["accepted"], read
    for row in read["readback_value"]["rows"]:
        sid = db.parties.find_one({"recall_id": RID, "distributor": distributor["party_id"],
                                  "account": row["account"]})["party_id"]
        res = submit_claim(client, db, recall_id=RID, party_id=sid, attempt_id=f"form-{sid}", source="form",
                           raw={"received_qty": row["qty_shipped"], "in_stock": row["qty_in_stock"],
                                "returned": row["qty_shipped"] - row["qty_in_stock"]})
        assert res["accepted"], res


def wait_for(check, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, what
        time.sleep(0.2)


def verified_probe(db, order_system: str) -> dict | None:
    """The distributor's first contract probe, once a read-back verified it."""
    return db.operations.find_one({"party_id": f"dist-{order_system}", "probe": 1, "state": "verified"})


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_switch_detect_repair_retry_then_the_second_order_system_repairs_from_memory(client, db, settings, assert_balance):
    from levela import harness
    from levela.api.main import create_app
    from levela.harness.decide import RuleDecider
    from levela.orchestrator.planner import Planner
    from levela.orchestrator.failpoints import Failpoints
    from levela.orchestrator.worker import WorkerPool

    north_app, north = make_order_system("north", NORTH, speed=SPEED)
    east_app, east = make_order_system("east", EAST, speed=SPEED)
    order_systems = {"north": north, "east": east}
    start(client, db, ("north", "east"), speed=SPEED)
    ctx = make_ctx(settings, client, db, order_systems)
    for name in ("north", "east"):
        reconcile(client, db, ctx, dist(db, name))
    assert db.parties.count_documents({"recall_id": RID, "state": {"$ne": "reconciled"}}) == 0
    assert claim_next(db, worker_id="w-test", sim_now=10_000.0, recall_id=RID) is None, \
        "every party is reconciled: without the probe no order system call would ever happen"
    before = {op["_id"]: op for op in db.operations.find({})}

    api = TestClient(create_app(replace(settings, scheduler_enabled=False), client=client, sim_network=object(),
                                http=forward_to({"north": north_app, "east": east_app})))
    model = ScriptedRepairer(V2_PROPOSAL, source="model", model_calls=1)
    pool = WorkerPool(settings, db, client, providers={}, probes={}, order_systems=order_systems,
                      failpoints=Failpoints(db, env={}, worker=settings.worker_name), decider=RuleDecider(),
                      harness=harness, workers=2, recall_id=RID)
    pool.planner = Planner(settings, db, client, planner_id=f"{settings.worker_name}/planner", context=pool.context,
                     recall_id=RID, repair=lambda c, order_system: request_repair(c, order_system, repairer=model))
    stop = threading.Event()
    thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
    thread.start()
    try:
        # 1:45 -- North is switched by the operator's button.
        t0 = time.monotonic()
        r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "north", "action": "switch_v2"})
        assert r.status_code == 200 and r.json()["probes_owed"] == {RID: 1}, r.text
        wait_for(lambda: db.feed.find_one({"kind": "order_system_contract_failed", "system": "North orders"}), DETECT_S,
                 "North's switch was not detected")
        assert time.monotonic() - t0 < DETECT_S
        assert feed_texts(db, "order_system_contract_failed") == [f"contract failed: {FAILURE}. 1 operation flagged, "
                                                            "2 untouched"]
        probe = wait_for(lambda: verified_probe(db, "north"), REPAIR_S,
                         "North was not repaired and its probe not retried")
        day = sim_day(probe["intent_at"])
        assert probe["idempotency_key"] == f"{RID}:dist-north:read_inventory:d{day}:probe1"
        assert (probe["adapter_version"], probe["reverify_from"], probe["retries"]) == (2, "intent", 1)
        north_v2 = db.adapters.find_one({"_id": "north:v2"})
        assert (north_v2["status"], north_v2["source"], north_v2["model_calls"]) == ("active", "model", 1)
        assert model.calls == [FAILURE]
        [activated] = feed_texts(db, "adapter_activated")
        assert activated.startswith("adapter v2 active: canary ") and activated.endswith("Retrying 1 operation")
        episode = db.episodes.find_one({"kind": "adapter"})
        assert episode["order_system"] == "north"

        # 2:20 -- East fails the same way and repairs from memory, with no model call.
        t1 = time.monotonic()
        r = api.post("/admin/order-system", headers=ADMIN, json={"order_system": "east", "action": "switch_v2"})
        assert r.status_code == 200 and r.json()["probes_owed"] == {RID: 1}, r.text
        wait_for(lambda: db.feed.find_one({"kind": "order_system_contract_failed", "system": "East orders"}), DETECT_S,
                 "East's switch was not detected")
        assert time.monotonic() - t1 < DETECT_S
        east_probe = wait_for(lambda: verified_probe(db, "east"), REPAIR_S, "East was not repaired from memory")
        assert (east_probe["adapter_version"], east_probe["retries"]) == (2, 1)
        east_v2 = db.adapters.find_one({"_id": "east:v2"})
        assert (east_v2["status"], east_v2["source"], east_v2["model_calls"]) == (
            "active", f"episode {episode['_id']}", 0)
        assert model.calls == [FAILURE], "East's repair made no model call"
        east_line = db.feed.find_one({"kind": "adapter_activated", "system": "East orders"})["text"]
        assert east_line.startswith(f"contract failed. Episode {episode['_id']} retrieved. Adapter v2 active, "
                                    "0 model calls") and east_line.endswith("Retrying 1 operation")
        assert db.runs.find_one({"recall_id": RID})["model_calls"] == 1
        assert db.episodes.count_documents({"kind": "adapter"}) == 1
        assert db.episodes.find_one({"_id": episode["_id"]})["reused"] == 1

        # Only the probes ran again; the operations verified under v1 were never touched.
        for op in db.operations.find({}):
            if op["_id"] in before:
                assert op == before[op["_id"]], op["_id"]
            else:
                assert op.get("probe") == 1 and op["state"] == "verified", op
        for app in (north_app, east_app):
            assert app.state.order_system.writes == 1, "one hold each; no confirm for a hold in force under v1"
        for name in ("north", "east"):
            party = dist(db, name)
            assert (party["order_system_paused"], party["order_system_probe_due"], party["order_system_probes"]) == (False, False, 1)
        assert_balance(RID)
        assert [line["text"] for line in db.feed.find({"kind": {"$in": ["error", "gate_refused"]}})] == []
    finally:
        stop.set()
        thread.join(30)
        north.close()
        east.close()
        api.app.state.http.close()
    assert not thread.is_alive(), "the pool stopped"
