"""Calling the plant (spec test 39, translated: part maker -> plant, fix
canary -> barcode_verified)."""
from __future__ import annotations

import pytest

from levela.api import questions
from levela.db.seed import load_scenario, start_recall
from levela.ledger.reconciler import open_units, try_close
from levela.monitoring import link, plant
from levela.monitoring.store_events import post_event
from levela.db.schema_store import STORE_COLLECTIONS
from levela.db.setup import setup
from support import BATCH

RID = "R-plant"


@pytest.fixture(autouse=True)
def _store_schema(db):
    """The store collections with their validators and indexes, even if an
    earlier test in the session dropped them."""
    setup(db, STORE_COLLECTIONS)


def _recall(db, client):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, recall_id=RID, emit=False)
    recall = db.recalls.find_one({"_id": RID})
    made = plant.ensure_plant_party(db, recall)
    assert made["created"] and set(made["questions"]) == {"other_batches", "shipped_count", "action"}
    return made


def _answer(db, made, key, value, attempt_id="att-plant-1"):
    p = db.parties.find_one({"recall_id": RID, "party_id": made["party_id"]})
    q = questions.get_question(db, RID, made["questions"][key])
    return questions.record_answer(db, party=p, attempt_id=attempt_id, question=q, value=value, sim_now=1.0)


def test_spec_39_plant_party_is_tier_0_and_due_at_once(db, client):
    made = _recall(db, client)
    p = db.parties.find_one({"recall_id": RID, "party_id": made["party_id"]})
    assert p["type"] == "plant" and p["tier"] == 0 and p["shipped_in"] == 0 and p["channel"] == "sim"
    assert p["next_action_at"] is not None
    assert sorted(p["open_questions"]) == sorted(made["questions"].values())
    # Stores do not get the plant's questions.
    assert not db.parties.count_documents({"recall_id": RID, "type": "store", "open_questions": {"$ne": []}})
    again = plant.ensure_plant_party(db, db.recalls.find_one({"_id": RID}))
    assert not again["created"] and again["questions"] == made["questions"]
    assert db.questions.count_documents({"recall_id": RID}) == 3


def test_spec_39_three_answers_stored_against_the_attempt(db, client):
    """Spec 39: the plant's three answers are stored against its attempt;
    a batch it names and the action wait for a person."""
    made = _recall(db, client)
    _answer(db, made, "other_batches", "B-4480 ran on the same line")
    _answer(db, made, "shipped_count", 240)
    _answer(db, made, "action", "destroy them")
    p = db.parties.find_one({"recall_id": RID, "party_id": made["party_id"]})
    assert p["open_questions"] == []
    for qid in made["questions"].values():
        assert p["answers"][qid]["attempt_id"] == "att-plant-1"
    out = plant.apply_answers(db, RID)
    assert out["shipped_count"]["disputed_qty"] == 0
    recall = db.recalls.find_one({"_id": RID})
    assert recall["proposed_batches"] == ["B-4480"] and "B-4480" not in recall["affected_batches"]
    assert recall["proposed_action"] == "destroy" and recall.get("action") != "destroy"
    plant.confirm(db, RID, by="quality-lead", batch="B-4480", action="destroy")
    recall = db.recalls.find_one({"_id": RID})
    assert "B-4480" in recall["affected_batches"] and recall["action"] == "destroy"
    with pytest.raises(ValueError):
        plant.confirm(db, RID, by="quality-lead", batch="B-9999")


def test_spec_39_shipped_count_differing_from_receiving_opens_a_dispute(db, client):
    """Spec 39: a shipped count that differs from the receiving records opens
    a dispute, and it blocks all clear."""
    made = _recall(db, client)
    before = open_units(db, RID)
    _answer(db, made, "shipped_count", 230)
    out = plant.apply_answers(db, RID)
    assert out["shipped_count"] == {"plant": 230, "receiving": 240, "disputed_qty": 10, "state": "disputed"}
    edge = db.edges.find_one({"recall_id": RID, "from_party": made["party_id"]})
    assert edge["state"] == "disputed" and edge["disputed_qty"] == 10
    assert open_units(db, RID) == before + 10
    # Even with every tin accounted for, the dispute alone keeps the recall open.
    db.parties.update_many({"recall_id": RID}, [{"$set": {"unresolved": 0, "returned": "$shipped_in"}}])
    db.edges.delete_many({"recall_id": RID, "kind": {"$ne": "plant_count"}})
    assert open_units(db, RID) == 10
    assert try_close(db, RID)["closed"] is False
    # The plant corrects its count: the dispute settles.
    _answer(db, made, "shipped_count", 240, attempt_id="att-plant-2")
    plant.apply_answers(db, RID)
    assert db.edges.find_one({"_id": edge["_id"]})["state"] == "corroborated"
    assert try_close(db, RID)["closed"] is True


def test_spec_39_no_block_until_a_till_recognises_the_tin(db, client):
    """Spec 39: barcode_verified stays false until one connected till
    recognises a scanned tin; until then no block goes to another store."""
    _recall(db, client)
    stores = [p for p in db.parties.find({"recall_id": RID, "type": "store"})][:2]
    for p in stores:
        link.register_store(db, f"ss-{p['party_id']}", p["name"], batch_code=BATCH,
                            stock={"received": p["shipped_in"], "on_shelf": p["shipped_in"]})
    assert len(link.link_recall(db, RID)) == 2
    first, second = (f"ss-{p['party_id']}" for p in stores)
    assert link.request_block(db, first)["requested"]
    assert link.request_block(db, second)["requested"] is False
    # A plain sale proves nothing about the barcode.
    post_event(db, first, {"event_id": "s-0", "kind": "sale", "batch_code": BATCH})
    assert db.recalls.find_one({"_id": RID})["barcode_verified"] is False
    # A scan of a different product proves nothing either.
    post_event(db, first, {"event_id": "s-1", "kind": "blocked_sale", "batch_code": BATCH, "barcode": "0 11111 11111 1"})
    assert db.recalls.find_one({"_id": RID})["barcode_verified"] is False
    assert db.store_systems.find_one({"_id": second})["blocked"] is False
    res = post_event(db, first, {"event_id": "s-2", "kind": "blocked_sale", "batch_code": BATCH,
                                 "barcode": "0 62345 18803 5"})
    assert res["barcode_verified"] and res["blocks_sent"] == [second]
    assert db.recalls.find_one({"_id": RID})["barcode_verified"] is True
    assert db.store_systems.find_one({"_id": second})["blocked"] is True
