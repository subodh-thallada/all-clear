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
    """queen -> corner-mart -> dundas-lofts -> spadina-works, queen -> bloor-alarm,
    lakeside -> spadina-works (a shorter second path), and a memory suspect."""
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, emit=False)
    claims = [
        ("queen-st-grocer", {"held": 60, "forwards": [{"to_name": "Corner Mart", "qty": 40},
                                                                    {"to_name": "Bloor Alarm", "qty": 20}]}),
        ("corner-mart", {"received_qty": 40, "held": 10, "forwards": [{"to_name": "Dundas Lofts", "qty": 30}]}),
        ("dundas-lofts", {"received_qty": 30, "held": 5, "forwarded_qty": 25,
                           "forwarded_to_name": "Spadina Works"}),
        ("lakeside-foods", {"held": 20, "returned": 55, "forwards": [{"to_name": "Spadina Works", "qty": 5}]}),
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
        ("bloor-alarm", 1), ("corner-mart", 1), ("dundas-lofts", 2), ("spadina-works", 3)]
    assert downstream(db, RID, "corner-mart") == [("dundas-lofts", 1), ("spadina-works", 2)]
    assert downstream(db, RID, "spadina-works") == []


def test_tiers_take_the_shortest_path_from_the_shipment_list(db, chain):
    assert tiers(db, RID) == {
        "harbour-market": 0, "queen-st-grocer": 0, "lakeside-foods": 0,
        "bloor-alarm": 1, "corner-mart": 1, "spadina-works": 1, "dundas-lofts": 2,
        "suspect-party": None,
    }
