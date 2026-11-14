"""Spec test 1 (Atlas basics) on the test cluster.

A two-collection transaction commits and aborts; a validator rejects an
insert and an update; a change stream receives an insert; `$$NOW` works
inside `$expr` in a findOneAndUpdate filter (the lease claim relies on it);
whether Atlas Search is available is noted; the vector index becomes
queryable (Atlas only: a plain mongod has no search process).
"""
from __future__ import annotations

import time

import pytest
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import OperationFailure, WriteError

from levela.db.client import run_txn
from levela.ledger.reconciler import VALIDATION_FAILED
from support import recall_doc
from levela.db.schema import VECTOR_INDEX, ensure_vector_index


class Boom(Exception):
    pass


def test_two_collection_transaction_commits(client, db):
    counter: dict = {}

    def txn(session):
        db.shipments.insert_one({"_id": "s1", "recall_id": "R", "party_id": "a", "qty": 1}, session=session)
        db.tool_events.insert_one({"_id": "t1", "attempt_id": "x", "kind": "tool_call"}, session=session)
        return "done"

    assert run_txn(client, txn, counter=counter) == "done" and counter["runs"] == 1
    assert db.shipments.find_one({"_id": "s1"}) and db.tool_events.find_one({"_id": "t1"})


def test_two_collection_transaction_aborts(client, db):
    def txn(session):
        db.shipments.insert_one({"_id": "s2", "recall_id": "R", "party_id": "b", "qty": 1}, session=session)
        db.tool_events.insert_one({"_id": "t2", "attempt_id": "y", "kind": "tool_call"}, session=session)
        raise Boom()

    with pytest.raises(Boom):
        run_txn(client, txn)
    assert db.shipments.find_one({"_id": "s2"}) is None and db.tool_events.find_one({"_id": "t2"}) is None


def test_validator_rejects_an_insert_and_an_update(db):
    recall = recall_doc("R1", units_total=10)
    with pytest.raises(WriteError) as bad_insert:
        db.recalls.insert_one({**recall, "open_units": -1})
    assert bad_insert.value.code == VALIDATION_FAILED
    db.recalls.insert_one(recall)
    with pytest.raises(WriteError) as bad_update:
        db.recalls.update_one({"_id": "R1"}, {"$set": {"status": "closed"}})
    assert bad_update.value.code == VALIDATION_FAILED
    assert db.recalls.find_one({"_id": "R1"})["status"] == "open"


def test_change_stream_receives_an_insert(db):
    with db.watch([{"$match": {"operationType": "insert", "ns.coll": "feed"}}], max_await_time_ms=500) as stream:
        db.feed.insert_one({"_id": "f1", "kind": "info", "text": "hello"})
        deadline = time.monotonic() + 20
        change = None
        while change is None and time.monotonic() < deadline:
            change = stream.try_next()
    assert change is not None and change["documentKey"] == {"_id": "f1"}
    assert change["fullDocument"]["text"] == "hello" and stream.resume_token


def test_now_works_inside_expr_in_a_find_one_and_update_filter(db):
    """Spec test 1: a lease that expired by the server's clock is taken, one
    that has not is left alone."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.worker_state.insert_many([{"_id": "free", "lease_until": past}, {"_id": "held", "lease_until": future}])
    taken = [db.worker_state.find_one_and_update(
        {"_id": sid, "$expr": {"$lt": ["$lease_until", "$$NOW"]}},
        [{"$set": {"lease_owner": "w1", "lease_until": {"$add": ["$$NOW", 8000]}}}],
        return_document=ReturnDocument.AFTER) for sid in ("free", "held")]
    assert taken[0]["lease_owner"] == "w1" and taken[0]["lease_until"] > past
    assert taken[1] is None and db.worker_state.find_one({"_id": "held"}).get("lease_owner") is None


def test_atlas_search_availability_is_noted(db, on_atlas, record_property):
    """Spec test 1: note whether $search runs here. Names fall back to a
    normalised exact match where it does not (levela.ledger.names)."""
    try:
        list(db.feed.aggregate([{"$search": {"index": "default", "text": {"query": "x", "path": "text"}}},
                                {"$limit": 1}]))
        available = True
    except OperationFailure:
        available = False
    record_property("atlas_search_available", available)
    print(f"Atlas Search available: {available} (on Atlas: {on_atlas})")


@pytest.mark.atlas
@pytest.mark.slow
def test_vector_index_becomes_queryable(fresh_db, on_atlas):
    if not on_atlas:
        pytest.skip("needs Atlas Vector Search (plain mongod has no search process)")
    dims = 4
    fresh_db.episodes.insert_many([
        {"_id": "near", "valid": True, "kind": "contact", "embedding": [1.0, 0.0, 0.0, 0.0]},
        {"_id": "far", "valid": True, "kind": "contact", "embedding": [0.0, 1.0, 0.0, 0.0]},
        {"_id": "stale", "valid": False, "kind": "contact", "embedding": [1.0, 0.01, 0.0, 0.0]},
    ])
    built = ensure_vector_index(fresh_db, dims, wait_s=240)
    assert built["ok"], built
    query = {"$vectorSearch": {"index": VECTOR_INDEX, "path": "embedding", "queryVector": [0.9, 0.1, 0.0, 0.0],
                               "numCandidates": 10, "limit": 2, "filter": {"valid": True}}}
    deadline = time.monotonic() + 120
    hits: list = []
    while len(hits) < 2 and time.monotonic() < deadline:   # documents are indexed shortly after it is queryable
        hits = list(fresh_db.episodes.aggregate([query, {"$project": {"_id": 1}}]))
        time.sleep(0 if len(hits) == 2 else 2)
    assert [h["_id"] for h in hits] == ["near", "far"]
