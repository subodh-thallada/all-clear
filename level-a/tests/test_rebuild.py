"""The ledger rebuilt from its claims log equals the live ledger, field for
field; a write outside the claim path shows up as a named difference.

The run covers what the claims log must reproduce: a rejection (not
replayed), a transfer, a dispute and its correction, a claim forwarding to
several parties, a stub memory created and an order system distributor's accounts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levela.clock import server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import submit_claim
from levela.ledger.rebuild import BADGE_ID, RebuildError, rebuild
from levela.ledger.transfers import stub_party
from support import PRODUCT

LONG = Path(__file__).parent / "fixtures" / "recall_long_mini.json"
RID = "RCL-LONG-MINI"


@pytest.fixture
def run(client, db, assert_balance):
    start_recall(client, db, load_scenario(LONG), all_sim=True, emit=False)
    db.parties.insert_one(stub_party(RID, "parkdale-court", "Parkdale Court", None, "memory", server_now(client),
                                     product=PRODUCT))
    steps = [
        ("harbour-market", "sim", {"held": 60}),                                    # refused by Atlas
        ("harbour-market", "sim", {"held": 35, "returned": 5}),
        ("queen-st-grocer", "sim", {"held": 20, "forwarded_qty": 40,
                                    "forwarded_to_name": "Corner Mart"}),
        ("corner-mart", "form", {"received_qty": 25, "held": 25}),                  # disputed
        ("queen-st-grocer", "sim", {"held": 35, "forwards": [
            {"to_name": "Corner Mart", "qty": 25}]}),                                  # corrected
        ("dist-north", "order_system", {"forwards": [{"to_party_id": "bloor-foods", "qty": 120},
                                               {"to_party_id": "annex-grocer", "qty": 180}]}),
        ("annex-grocer", "sim", {"received_qty": 180, "held": 100, "forwards": [
            {"to_name": "Dundas Mini Mart", "qty": 50}, {"to_name": "Parkdale Court", "qty": 30}]}),
    ]
    for i, (party_id, source, raw) in enumerate(steps):
        submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_r{i}", source=source, raw=raw)
        assert_balance(RID)
    assert db.claims.count_documents({"recall_id": RID, "status": "accepted"}) == 6
    return RID


@pytest.mark.slow
def test_rebuild_from_the_claims_log_is_identical_until_someone_writes_around_it(client, db, run):
    first = rebuild(client, db, RID)
    assert first["identical"], first["diffs"]
    assert (first["parties"], first["edges"], first["claims_replayed"]) == (8, 5, 6)
    badge = db.worker_state.find_one({"_id": BADGE_ID})
    assert badge["text"].startswith("Ledger rebuilt from the claims log: identical (8 parties, 5 edges")
    line = db.feed.find_one({"kind": "info", "recall_id": RID})
    assert line["system"] == "ledger" and line["level"] == "good"
    assert db.client[f"{db.name}_rebuild"].list_collection_names() == []      # the scratch is gone

    # A write that bypasses submit_claim (it conserves, so no validator objects).
    db.parties.update_one({"recall_id": RID, "party_id": "bloor-foods"},
                         {"$set": {"shipped_in": 5, "held": 5}})
    second = rebuild(client, db, RID)
    assert not second["identical"]
    assert {(d["id"], d["field"], d["ledger"], d["rebuilt"]) for d in second["diffs"]} == {
        ("bloor-foods", "shipped_in", 5, 0), ("bloor-foods", "held", 5, 0)}
    assert db.worker_state.find_one({"_id": BADGE_ID}) is None
    assert db.feed.find_one({"kind": "error", "recall_id": RID})["detail"]["diffs"] == second["diffs"]


def test_rebuild_refuses_an_unsafe_scratch_database(client, db, mongo_client):
    with pytest.raises(RebuildError, match="must not be the live database"):
        rebuild(client, db, "RCL-any", scratch_db_name=db.name)
    other = mongo_client[f"{db.name}_notscratch"]
    other.keep.insert_one({"_id": 1})
    try:
        with pytest.raises(RebuildError, match="not a rebuild scratch database"):
            rebuild(client, db, "RCL-any", scratch_db_name=other.name)
        assert other.keep.find_one({"_id": 1})
    finally:
        mongo_client.drop_database(other.name)


def test_rebuild_of_an_unknown_recall_is_an_error(client, db):
    with pytest.raises(RebuildError, match="unknown recall"):
        rebuild(client, db, "RCL-none")
