"""A live store a caller names on a real call (scenarios/live_stores.json).

The demo's caller says "we passed some to Ryan's store": that name must match
the live store's directory entry, so the new party is a voice party with the
number from .env and is due at once (the worker dials it when the call slot
frees, i.e. after the naming call hangs up with one slot). The number comes
only from the environment: with it unset, nothing about the store is a call.
Numbers below are in the fictional 555-01xx block.
"""
from __future__ import annotations

from levela.db.seed import live_store_entries, load_scenario, start_recall
from levela.ledger.claims import submit_claim
from levela.orchestrator.claim import set_max_live, slots

ENV = {"PHONE_HARBOUR": "+14165550101", "PHONE_CORNER": "+14165550102", "PHONE_RYAN": "+14165550149"}


def test_a_live_store_needs_its_number_in_the_environment():
    assert live_store_entries({}, set()) == []
    assert live_store_entries({"PHONE_RYAN": "not a number"}, set()) == []
    (ryan,) = live_store_entries({"PHONE_RYAN": "+14165550149"}, set())
    assert (ryan["party_id"], ryan["channel"], ryan["phone"], ryan["tier"], ryan["hold_for_demo"]) == \
        ("ryans-store", "voice", "+14165550149", 3, False)
    assert "phone_env" not in ryan
    assert live_store_entries({"PHONE_RYAN": "+14165550149"}, {"ryans-store"}) == []


def test_the_live_store_joins_a_demo_recall_only(client, db):
    demo = start_recall(client, db, load_scenario("recall_1"), mode="demo", env=ENV, recall_id="R-DEMO")
    directory = db.recalls.find_one({"_id": demo["recall_id"]})["directory"]
    assert "ryans-store" in {e["party_id"] for e in directory}
    sim = start_recall(client, db, load_scenario("recall_1"), all_sim=True, env=ENV, recall_id="R-SIM")
    assert "ryans-store" not in {e["party_id"] for e in db.recalls.find_one({"_id": sim["recall_id"]})["directory"]}


def test_naming_ryan_store_on_a_call_creates_a_voice_party_due_now(client, db):
    rid = start_recall(client, db, load_scenario("recall_1"), mode="demo", env=ENV, recall_id="R-RYAN",
                       hold_slice=False)["recall_id"]
    out = submit_claim(client, db, recall_id=rid, party_id="harbour-market", attempt_id="at-ryan-1", source="voice",
                       raw={"sold": 10, "on_shelf": 0, "held": 20, "returned": 0, "forwarded_qty": 10,
                            "forwarded_to_name": "Ryan store", "batch_seen": "B-4471", "sold_source": "till_report",
                            "block_test": True})
    assert out["accepted"], out
    ryan = db.parties.find_one({"recall_id": rid, "party_id": "ryans-store"})
    assert ryan is not None, "the spoken name matched the live store's directory entry"
    assert (ryan["name"], ryan["channel"], ryan["phone"], ryan["tier"], ryan["hold_for_demo"]) == \
        ("Ryan's Store", "voice", "+14165550149", 3, False)
    edge = db.edges.find_one({"recall_id": rid, "from_party": "harbour-market", "to_party": "ryans-store"})
    assert edge["sender_qty"] == 10 and edge["state"] == "awaiting_recipient"
    assert ryan.get("next_action_at") is not None


def test_one_call_at_a_time(db):
    set_max_live(db, 1)
    assert slots(db)["max_live"] == 1
