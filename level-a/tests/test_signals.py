"""Signals: the complaint-count rule (spec test 34) and the approval gate
(spec test 35)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import WriteError

from levela.api.main import create_app
from levela.config import AGENT_DIR
from levela.db.client import run_txn
from levela.db.seed import load_scenario
from levela.harness import budget
from levela.harness import tools as harness_tools
from levela.ledger.reconciler import VALIDATION_FAILED
from levela.memory.embed import HashEmbedder
from levela.monitoring import classify, signals
from levela.db.schema_store import STORE_COLLECTIONS
from levela.db.setup import setup
from support import BATCH

EMB = HashEmbedder()


@pytest.fixture(autouse=True)
def _store_schema(db):
    """The store collections with their validators and indexes, even if an
    earlier test in the session dropped them."""
    setup(db, STORE_COLLECTIONS)
STORES = ["Maple Court Market", "Bayview Grocer", "Spadina Foods", "Dundas Fresh"]


def _complain(db, cid, store, *, day=0, batch=BATCH, code="SEAL"):
    classify.receive(db, {"complaint_id": cid, "batch_code": batch, "bought_at": store, "reason_code": code,
                          "text": "lid seal lifted, powder clumped", "day": day})
    cls = classify.classify(db, cid, embedder=EMB)
    assert cls["defect"] == "seal_fault"
    return signals.evaluate(db, batch_code=batch)


def test_spec_34_signal_rule(db):
    """Spec 34: two complaints raise nothing; a third from a third store
    creates one proposed signal; a fourth is added to the same signal."""
    assert _complain(db, "c-1", STORES[0]) == []
    assert _complain(db, "c-2", STORES[1]) == []
    assert db.signals.count_documents({}) == 0
    made = _complain(db, "c-3", STORES[2])
    assert len(made) == 1
    sig = db.signals.find_one({})
    assert sig["status"] == "proposed" and sig["defect"] == "seal_fault" and sig["batch_code"] == BATCH
    assert sig["complaints"] == ["c-1", "c-2", "c-3"] and sig["stores"] == STORES[:3]
    assert sig["approved_by"] is None and sig["rule"] == "3 complaints from 3 stores within 30 days"
    _complain(db, "c-4", STORES[3], day=2)
    assert db.signals.count_documents({}) == 1
    sig = db.signals.find_one({})
    assert sig["complaints"] == ["c-1", "c-2", "c-3", "c-4"] and len(sig["stores"]) == 4
    assert db.complaints.count_documents({"signal_id": sig["_id"]}) == 4


def test_spec_34_three_complaints_need_three_stores_and_the_window(db):
    _complain(db, "c-1", "Bayview Grocer")
    _complain(db, "c-2", "bayview grocer")          # the same store, the shopper's spelling
    assert _complain(db, "c-3", "Spadina Foods") == []
    # Old complaints fall out of the 30-day window.
    _complain(db, "c-5", "Dundas Fresh", day=40)
    assert db.signals.count_documents({}) == 0
    # Unclassified complaints never count.
    classify.receive(db, {"complaint_id": "c-6", "batch_code": BATCH, "bought_at": "Queen Fresh", "text": "?"})
    assert signals.evaluate(db, batch_code=BATCH) == []


def test_spec_34_shipped_to_from_the_shipment_records(db):
    db.shipments.insert_many([{"_id": f"ship-{i}", "recall_id": None, "party_id": f"p{i}", "batch_code": BATCH,
                               "name": n, "qty": 10} for i, n in enumerate(["Harbour Market", "Queen St Grocer"])])
    for i, store in enumerate(STORES[:3]):
        _complain(db, f"c-{i}", store)
    assert db.signals.find_one({})["shipped_to"] == ["Harbour Market", "Queen St Grocer"]


def _proposed(db):
    for i, store in enumerate(STORES[:3]):
        _complain(db, f"c-{i}", store)
    return db.signals.find_one({})["_id"]


def test_spec_35_no_recall_from_a_proposed_signal(db, client):
    """Spec 35: a recall with a signal source cannot be created while the signal is proposed."""
    sig_id = _proposed(db)
    scenario = load_scenario("recall_1")
    with pytest.raises(signals.SignalError) as err:
        run_txn(client, lambda s: signals.recall_from_signal(client, db, sig_id, scenario, session=s, all_sim=True))
    assert err.value.code == "not_approved"
    assert db.recalls.count_documents({}) == 0


def test_spec_35_atlas_rejects_approved_without_approved_by(db):
    """Spec 35: Atlas rejects `approved` without `approved_by`."""
    sig_id = _proposed(db)
    with pytest.raises(WriteError) as err:
        db.signals.update_one({"_id": sig_id}, {"$set": {"status": "approved"}})
    assert err.value.code == VALIDATION_FAILED
    with pytest.raises(WriteError):
        db.signals.update_one({"_id": sig_id}, {"$set": {"status": "approved", "approved_by": ""}})


def test_spec_35_no_agent_tool_can_approve():
    """Spec 35: no agent tool list contains approve."""
    lists = {kind: list(names) for kind, names in budget.RUN_TOOLS.items()}
    lists["registry"] = list(harness_tools.REGISTRY)
    agent_tools = []
    for path in sorted(AGENT_DIR.glob("tool_*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        agent_tools.append(str(spec.get("name") or spec.get("tool_config", {}).get("name") or path.stem))
    lists["voice_agent"] = agent_tools
    for kind, names in lists.items():
        assert names, kind
        assert not [n for n in names if "approve" in n.lower()], (kind, names)


def test_spec_35_person_approves_and_the_recall_opens(db, client):
    """A person's approval sets approved_by and opens the recall (source
    signal, source_signal) in one transaction; a second approval is refused."""
    sig_id = _proposed(db)
    out = signals.approve(client, db, sig_id, by="operator-1", scenario=load_scenario("recall_1"),
                          recall_id="R-sig", all_sim=True)
    sig = db.signals.find_one({"_id": sig_id})
    assert sig["status"] == "approved" and sig["approved_by"] == "operator-1" and sig["recall_id"] == "R-sig"
    recall = db.recalls.find_one({"_id": "R-sig"})
    assert recall["source"] == "signal" and recall["source_signal"] == sig_id and recall["approved_by"] == "operator-1"
    assert recall["defect"] == "seal_fault" and recall["barcode_verified"] is False
    assert out["recall_id"] == "R-sig"
    plant = db.parties.find_one({"recall_id": "R-sig", "type": "plant"})
    assert plant is not None and plant["tier"] == 0 and len(plant["open_questions"]) == 3
    assert sig["shipped_to"]                                    # the recall's shipment list
    with pytest.raises(signals.SignalError) as err:
        signals.approve(client, db, sig_id, by="operator-2", scenario=load_scenario("recall_1"), recall_id="R-2")
    assert err.value.code == "not_proposed"
    assert db.recalls.count_documents({"_id": "R-2"}) == 0


def test_spec_35_through_the_api(db, settings, client):
    app = create_app(settings, client=client, sim_network=object())
    secret, admin = {"X-Tool-Secret": settings.tool_secret}, {"X-Admin-Token": settings.admin_token}
    with TestClient(app) as http:
        body = {"batch_code": BATCH, "reason_code": "SEAL", "text": "seal lifted"}
        assert http.post("/complaints", json={**body, "bought_at": STORES[0]}).status_code == 401
        for store in STORES[:3]:
            r = http.post("/complaints", json={**body, "bought_at": store}, headers=secret)
            assert r.status_code == 200, r.text
        assert r.json()["signal"]["status"] == "proposed"
        sig_id = r.json()["signal"]["_id"]
        assert http.post(f"/admin/signals/{sig_id}/approve", json={"by": "operator-1"}).status_code == 401
        r = http.post(f"/admin/signals/{sig_id}/approve",
                      json={"by": "operator-1", "recall_id": "R-api", "all_sim": True}, headers=admin)
        assert r.status_code == 200, r.text
        assert db.recalls.find_one({"_id": "R-api"})["source_signal"] == sig_id
        again = http.post(f"/admin/signals/{sig_id}/approve", json={"by": "x", "recall_id": "R-x"}, headers=admin)
        assert again.status_code == 409
        listed = http.get("/admin/signals", headers=admin).json()["signals"]
        assert listed[0]["approved_by"] == "operator-1" and len(listed[0]["evidence"]) == 3


def test_store_script_day0_through_the_api(db, settings, client):
    """scripts/store_script.py: connectors, three day-0 complaints, a person's
    approval, then the stores' removal events (linked when a party names them)."""
    import argparse
    import importlib.util

    from levela.config import ROOT
    spec = importlib.util.spec_from_file_location("store_script", ROOT / "scripts" / "store_script.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    app = create_app(settings, client=client, sim_network=object())
    scenario = load_scenario("recall_long")
    with TestClient(app) as http:
        api = script.Api.__new__(script.Api)
        api.http, api.tool, api.admin = http, {"X-Tool-Secret": settings.tool_secret}, \
            {"X-Admin-Token": settings.admin_token}
        script.register(api, scenario)
        assert db.store_systems.count_documents({}) == len(scenario["connected_stores"])
        script.complaints(api, scenario)
        sig = db.signals.find_one({"batch_code": BATCH})
        assert sig["status"] == "proposed" and len(sig["complaints"]) == 3 and len(sig["stores"]) == 3
        assert all(c["classification"]["defect"] == "seal_fault" for c in db.complaints.find())
        script.approve(api, argparse.Namespace(by="operator-1", scenario="recall_long", all_sim=True,
                                               recall_id="R-long"))
        recall = db.recalls.find_one({"_id": "R-long"})
        assert recall["source_signal"] == sig["_id"]
        assert db.parties.count_documents({"recall_id": "R-long", "type": "plant"}) == 1
        script.removals(api, scenario, None)
        script.removals(api, scenario, None)            # fixed event ids: nothing moves twice
    for store in db.store_systems.find({"party_id": {"$ne": None}}):
        p = db.parties.find_one({"recall_id": "R-long", "party_id": store["party_id"]})
        assert p["held"] <= p["held_by_system"] and p["on_shelf"] >= p["system_on_shelf"]
