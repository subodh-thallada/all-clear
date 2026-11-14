"""The schema (db/schema.py, db/setup.py) and starting a recall (db/seed.py).

The rules the long run adds in Atlas (an operation is verified only with a
read-back, one operation per idempotency key, one active adapter per order
system, the due index), setup() re-run and upgrading an older database in
place, and start_recall() on a scenario with the shape of recall_long.json.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError, WriteError

from levela.clock import get_clock, server_now
from levela.db.seed import ScenarioError, load_scenario, reset, start_recall
from levela.db.schema import SPEC_COLLECTIONS, SPEC_INDEXES, VALIDATORS
from levela.db.setup import COLLECTIONS, EXTRA_INDEXES, LEDGER_COLLECTIONS, V2_COLLECTIONS, setup
from levela.ledger.reconciler import VALIDATION_FAILED
from levela.runs import get_run
from support import party_doc

LONG = Path(__file__).parent / "fixtures" / "recall_long_mini.json"
LONG_RID = "RCL-LONG-MINI"
NOW = datetime(2026, 11, 14, 21, 55, tzinfo=timezone.utc)

# What an older setup on_shelf: the party validator with conservation alone
# (before "fixed needs evidence"), the ledger collections and their indexes.
WAVE1_PARTY_VALIDATOR = copy.deepcopy(VALIDATORS["parties"])
WAVE1_PARTY_VALIDATOR["$expr"] = WAVE1_PARTY_VALIDATOR["$expr"]["$and"][0]
WAVE1_COLLECTIONS = SPEC_COLLECTIONS + ["feed", "sim_calls"]
WAVE1_INDEXES = SPEC_INDEXES + [ix for ix in EXTRA_INDEXES if ix[0] in WAVE1_COLLECTIONS and ix[2]["name"] not in (
    "by_recall_due", "by_due", "uniq_attempt_idempotency_key", "by_recall_system_time")]


def _op(**over):
    return {"recall_id": "R", "kind": "place_hold", "order_system": "north", "party_id": "dist-north",
            "idempotency_key": "R:dist-north:place_hold", "state": "intent", "readback_at": None,
            "readback_value": None, **over}


def _party(party_id="dist-north", shipped_in=300, **over):
    return party_doc("R", party_id, shipped_in, **{"type_": "distributor", "channel": "order_system", **over})


# ---- the V2 rules -----------------------------------------------------------------

def test_an_operation_is_verified_only_with_a_read_back(db):
    for i, partial in enumerate(({}, {"readback_at": 2.5}, {"readback_value": {"hold_id": "H-7781"}})):
        with pytest.raises(WriteError) as info:
            db.operations.insert_one(_op(_id=f"op-{i}", state="verified", **partial))
        assert info.value.code == VALIDATION_FAILED
    db.operations.insert_one(_op(_id="op-31", state="sent", sent_at=2.4))
    with pytest.raises(WriteError):
        db.operations.update_one({"_id": "op-31"}, {"$set": {"state": "verified"}})
    db.operations.update_one({"_id": "op-31"}, {"$set": {"state": "verified", "readback_at": 2.5,
                                                         "readback_value": {"hold_id": "H-7781", "status": "on_hold"}}})
    assert db.operations.find_one({"_id": "op-31"})["state"] == "verified"
    with pytest.raises(WriteError):
        db.operations.insert_one(_op(_id="op-x", idempotency_key="k-x", state="done"))


def test_one_operation_per_idempotency_key(db):
    db.operations.insert_one(_op(_id="op-1"))
    with pytest.raises(DuplicateKeyError, match="uniq_idempotency_key"):
        db.operations.insert_one(_op(_id="op-2"))


def test_one_active_adapter_per_order_system(db):
    db.adapters.insert_many([{"_id": "north:v1", "order_system": "north", "version": 1, "status": "active"},
                             {"_id": "north:v2", "order_system": "north", "version": 2, "status": "candidate"},
                             {"_id": "east:v1", "order_system": "east", "version": 1, "status": "active"}])
    with pytest.raises(DuplicateKeyError, match="one_active_adapter_per_order_system"):
        db.adapters.update_one({"_id": "north:v2"}, {"$set": {"status": "active"}})
    db.adapters.update_one({"_id": "north:v1"}, {"$set": {"status": "retired"}})
    db.adapters.update_one({"_id": "north:v2"}, {"$set": {"status": "active"}})
    assert db.adapters.count_documents({"status": "active"}) == 2


def test_attempt_idempotency_keys_are_unique_when_present(db):
    base = {"recall_id": "R", "state": "completed"}
    db.attempts.insert_many([{**base, "_id": "a1", "party_id": "s1", "seq": 1},
                             {**base, "_id": "a2", "party_id": "s2", "seq": 1},
                             {**base, "_id": "a3", "party_id": "s3", "seq": 1, "idempotency_key": "tr-1:place_call"}])
    with pytest.raises(DuplicateKeyError, match="uniq_attempt_idempotency_key"):
        db.attempts.insert_one({**base, "_id": "a4", "party_id": "s4", "seq": 1, "idempotency_key": "tr-1:place_call"})


def test_parties_have_the_timer_indexes(db):
    info = db.parties.index_information()
    assert info["by_recall_due"]["key"] == [("recall_id", 1), ("next_action_at", 1)]
    assert info["by_due"]["key"] == [("next_action_at", 1)]


def test_setup_is_idempotent(db):
    again = setup(db)
    assert (again["created"], again["validators_updated"]) == ([], [])
    assert set(db.list_collection_names()) >= set(COLLECTIONS)


# ---- upgrading a wave-1 database ----------------------------------------------------

def test_setup_upgrades_an_older_party_validator_in_place(fresh_db):
    fresh_db.create_collection("parties", validator=WAVE1_PARTY_VALIDATOR, validationLevel="strict",
                               validationAction="error")
    fresh_db.parties.insert_one(_party("queen", 120, type_="store", channel="sim"))
    # The older rule let a phone claim fixed tins with no evidence.
    fresh_db.parties.insert_one(_party("owner", 25, type_="store", channel="sim", held=25, unresolved=0))
    out = setup(fresh_db, collections=["parties", "operations"])
    assert (out["created"], out["validators_updated"], out["validators"]) == (
        ["operations"], ["parties"], ["operations", "parties"])
    assert set(fresh_db.list_collection_names()) == {"parties", "operations"}
    assert fresh_db.parties.find_one({"_id": "R:queen"})["unresolved"] == 120     # documents untouched
    with pytest.raises(WriteError):
        fresh_db.parties.insert_one(_party("owner-2", 25, type_="store", channel="sim", held=25, unresolved=0))
    fresh_db.parties.insert_one(_party())
    assert "by_recall_due" in fresh_db.parties.index_information()
    with pytest.raises(WriteError):
        fresh_db.operations.insert_one(_op(state="verified"))


@pytest.mark.slow
def test_setup_upgrades_a_whole_older_database(fresh_db):
    for name in WAVE1_COLLECTIONS:
        validator = WAVE1_PARTY_VALIDATOR if name == "parties" else VALIDATORS.get(name)
        fresh_db.create_collection(name, **({"validator": validator} if validator else {}))
    for coll, keys, opts in WAVE1_INDEXES:
        fresh_db[coll].create_index(keys, **opts)
    out = setup(fresh_db)
    assert sorted(out["created"]) == sorted(V2_COLLECTIONS) and out["validators_updated"] == ["parties"]
    assert set(fresh_db.list_collection_names()) == set(COLLECTIONS)
    again = setup(fresh_db)
    assert (again["created"], again["validators_updated"]) == ([], [])


def test_setup_can_install_only_the_ledger(fresh_db):
    out = setup(fresh_db, collections=LEDGER_COLLECTIONS)
    assert sorted(out["created"]) == sorted(LEDGER_COLLECTIONS)
    assert out["validators"] == ["edges", "parties", "recalls"]
    assert "uniq_dedupe_key" in fresh_db.claims.index_information()


# ---- start_recall on the long-run shape ---------------------------------------------

def _parties(db, rid):
    return {s["party_id"]: s for s in db.parties.find({"recall_id": rid})}


def test_start_recall_long_in_demo_mode_holds_the_slice(client, db):
    out = start_recall(client, db, load_scenario(LONG), mode="demo", sim_parties=["harbour-market", "corner-mart"],
                       emit=False)
    assert out["mode"] == "demo" and out["options"]["followups"] == "manual"
    parties = _parties(db, LONG_RID)
    dist, harbour, queen = parties["dist-north"], parties["harbour-market"], parties["queen-st-grocer"]
    assert (dist["channel"], dist["phone"], dist["order_system"], dist["type"], dist["tier"]) == (
        "order_system", None, "north", "distributor", 1)
    assert (dist["slice"], dist["hold_for_demo"], dist["followups"], dist["profile"]) == (False, False, "auto", None)
    assert (harbour["type"], harbour["tier"], harbour["channel"], harbour["phone"]) == (
        "store", 1, "sim", "+14165550141")
    assert (harbour["model"], harbour["affected_batches"], harbour["unresolved"]) == ("B-4471", ["2.3", "2.4"], 40)
    for s in (harbour, queen):
        assert (s["slice"], s["hold_for_demo"], s["followups"]) == (True, True, "manual")
    for s in parties.values():
        assert (s["found_via"], s["next_action_at"], s["ladder_step"], s["lease_epoch"]) == ("shipment", 9.0, 1, 0)
    directory = {e["party_id"]: e for e in db.recalls.find_one({"_id": LONG_RID})["directory"]}
    assert (directory["corner-mart"]["hold_for_demo"], directory["corner-mart"]["tier"]) == (True, 3)
    assert (directory["corner-mart"]["type"], directory["annex-grocer"]["tier"]) == ("owner", 2)
    assert (directory["bloor-foods"]["account"], directory["bloor-foods"]["hold_for_demo"]) == ("N-014", False)
    assert "phone_env" not in directory["corner-mart"]

    run = get_run(db, LONG_RID)
    assert run["parties_at_start"] == 3 and run["schedule"]["hold_slice_in_demo"] is True
    assert run["events"]["order_system_change_at"] == {"north": 81.5}
    clock = get_clock(db, LONG_RID, max_age_s=0)
    assert (clock.sim_start, clock.seconds_per_sim_hour) == (9.0, 30.0)
    # Anchored on the database's clock, not this machine's.
    assert abs((clock.real_start - server_now(client)).total_seconds()) < 30


def test_start_recall_holds_follow_the_mode_and_the_override(client, db):
    long_ = load_scenario(LONG)

    def held(rid, scenario, **kw):
        start_recall(client, db, scenario, recall_id=rid, emit=False, now=NOW, **kw)
        return {sid for sid, s in _parties(db, rid).items() if s["hold_for_demo"]}

    assert held("L-sim", long_, all_sim=True) == set()
    assert held("L-demo-off", long_, mode="demo", all_sim=True, hold_slice=False) == set()
    recall_1 = load_scenario("recall_1")          # no schedule: nothing is held unless asked
    assert held("R1-demo", recall_1, mode="demo", all_sim=True) == set()
    assert held("R1-hold", recall_1, mode="demo", all_sim=True, hold_slice=True,
                seconds_per_sim_hour=0.05) == {"harbour-market", "queen-st-grocer", "lakeside-foods"}
    assert get_clock(db, "R1-hold", max_age_s=0).seconds_per_sim_hour == 0.05
    assert get_clock(db, "R1-hold", max_age_s=0).real_start == NOW


def test_start_recall_refuses_a_bad_scenario(client, db):
    long_ = load_scenario(LONG)
    with pytest.raises(ScenarioError, match="is a voice party: set PHONE_"):
        start_recall(client, db, long_, mode="demo", env={}, emit=False)
    short = copy.deepcopy(long_)
    short["parties"][0]["shipped_in"] = 39
    with pytest.raises(ScenarioError, match="do not add up"):
        start_recall(client, db, short, all_sim=True, emit=False)
    with pytest.raises(ScenarioError, match="mode"):
        start_recall(client, db, long_, mode="live", emit=False)
    assert db.recalls.count_documents({}) == 0 and db.runs.count_documents({}) == 0


def test_reset_removes_the_run_and_everything_under_it(client, db):
    start_recall(client, db, load_scenario(LONG), all_sim=True, emit=False)
    db.operations.insert_one(_op(recall_id=LONG_RID))
    removed = reset(db)
    assert (removed["recalls"], removed["runs"], removed["parties"], removed["operations"]) == (1, 1, 3, 1)
    assert db.parties.count_documents({}) == 0 and get_run(db, LONG_RID) is None
