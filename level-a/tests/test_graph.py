"""The transfer graph ($graphLookup): a three-hop chain with branches, built
through the claim path so the mass balance is checked after every write."""
from __future__ import annotations

import pytest

from levela.clock import server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import submit_claim
from levela.ledger.graph import downstream, tiers
from levela.ledger.transfers import stub_party
from support import PRODUCT

RID = "RCL-2026-017"


@pytest.fixture
def chain(client, db, assert_balance):
    """queen -> corner-mart -> dundas-mini-mart -> spadina-foods, queen -> bloor-foods,
    lakeside -> spadina-foods (a shorter second path), and a memory suspect."""
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, emit=False)
    claims = [
        ("queen-st-grocer", {"returned": 60, "forwards": [{"to_name": "Corner Mart", "qty": 40},
                                                                    {"to_name": "Bloor Foods", "qty": 20}]}),
        ("corner-mart", {"received_qty": 40, "returned": 10, "forwards": [{"to_name": "Dundas Mini Mart", "qty": 30}]}),
        ("dundas-mini-mart", {"received_qty": 30, "returned": 5, "forwarded_qty": 25,
                           "forwarded_to_name": "Spadina Foods"}),
        ("lakeside-foods", {"returned": 75, "forwards": [{"to_name": "Spadina Foods", "qty": 5}]}),
    ]
    for i, (party_id, raw) in enumerate(claims):
        res = submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=f"att_g{i}", source="sim",
                           raw=raw)
        assert res["accepted"], res
        assert_balance(RID)
    db.parties.insert_one(stub_party(RID, "suspect-party", "Suspect Party", None, "memory", server_now(client),
                                     product=PRODUCT))
    return RID


def test_downstream_walks_every_branch_to_any_depth(db, chain):
    assert downstream(db, RID, "queen-st-grocer") == [
        ("bloor-foods", 1), ("corner-mart", 1), ("dundas-mini-mart", 2), ("spadina-foods", 3)]
    assert downstream(db, RID, "corner-mart") == [("dundas-mini-mart", 1), ("spadina-foods", 2)]
    assert downstream(db, RID, "spadina-foods") == []


def test_tiers_take_the_shortest_path_from_the_shipment_list(db, chain):
    assert tiers(db, RID) == {
        "harbour-market": 0, "queen-st-grocer": 0, "lakeside-foods": 0,
        "bloor-foods": 1, "corner-mart": 1, "spadina-foods": 1, "dundas-mini-mart": 2,
        "suspect-party": None,
    }
