"""What Atlas holds and enforces: collections, validators, indexes and the
vector index. levela.db.setup installs it; the preflight compares a live
database with it (describe()).

The spec's rules enforced by Atlas:

  1  party conservation          validator on parties
  2  fixed needs evidence        validator on parties
  3  edge sanity                 validator on edges
  4  close gate                  validator on recalls
  5  one live attempt per party  partial unique index on attempts

and the long run's: an operation is verified only with a read-back
(validator on operations), one operation per idempotency key, one active
adapter per order system, and the due index on parties.
"""
from __future__ import annotations

import time

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from ..config import LIVE_ATTEMPT_STATES

# The spec's nine collections, then the feed and the simulator's call log,
# then the long run's and the harness's.
SPEC_COLLECTIONS = ["recalls", "shipments", "parties", "edges", "claims",
                    "attempts", "tool_events", "episodes", "worker_state"]
EXTRA_COLLECTIONS = ["feed", "sim_calls"]
V2_COLLECTIONS = ["operations", "adapters", "runs", "task_runs", "traces", "questions", "capacity"]
COLLECTIONS = SPEC_COLLECTIONS + EXTRA_COLLECTIONS + V2_COLLECTIONS
# What the claim path reads and writes: enough for a ledger rebuild's scratch
# database, at a third of the cost of a full setup on a free cluster.
LEDGER_COLLECTIONS = ["recalls", "shipments", "parties", "edges", "claims", "tool_events", "worker_state"]

VECTOR_INDEX = "episodes_vec"
# Atlas Search over party names, for matching a spoken name (levela.ledger.names).
PARTY_NAME_INDEX = "parties_name"
PARTY_NAME_INDEX_DEFINITION = {"mappings": {"dynamic": False, "fields": {
    "name": {"type": "string"}, "recall_id": {"type": "token"}}}}

_QTY = {"bsonType": ["int", "long"], "minimum": 0}
_QTY_OR_NULL = {"bsonType": ["int", "long", "null"], "minimum": 0}

# The six buckets a party's panels fall into. `installed` means installed and
# not yet confirmed fixed.
BUCKETS = ("in_stock", "installed", "fixed", "returned", "forwarded", "unresolved")
PARTY_TYPES = ["distributor", "installer", "owner"]
_STRING_OR_NULL = {"bsonType": ["string", "null"]}

# Rule 1. Party conservation: every quantity is a non-negative integer and
#   shipped_in = in_stock + installed + fixed + returned + forwarded + unresolved.
# Rule 2. Fixed needs evidence: `fixed` is at most `fixed_by_panel` (connected
# panels that passed their own test), unless the party read a version outside
# its affected_versions off the display AND the sounder rang; `installed` is
# never below `connected_unfixed` (a connected panel outranks the phone); and
# a model read off the panel must be the recalled model.
_FIXED_BY_PHONE = {"$and": [
    {"$eq": [{"$type": "$version_seen"}, "string"]},
    {"$not": [{"$in": ["$version_seen", {"$ifNull": ["$affected_versions", []]}]}]},
    {"$eq": ["$sounder_test", True]},
]}
PARTY_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "recall_id", "party_id", "name", "type", "tier", "channel", "state", "shipped_in", *BUCKETS,
            "model", "affected_versions", "fixed_by_panel", "connected_unfixed",
        ],
        "properties": {
            "recall_id": {"bsonType": "string"},
            "party_id": {"bsonType": "string"},
            "type": {"enum": PARTY_TYPES},
            "tier": {"bsonType": "int", "enum": [1, 2, 3]},
            "shipped_in": _QTY, **{b: _QTY for b in BUCKETS},
            "fixed_by_panel": _QTY, "connected_unfixed": _QTY,
            "state": {"enum": ["pending", "contacting", "reached", "claimed", "reconciled"]},
            "channel": {"enum": ["voice", "sim", "form", "order_system"]},
            "model": {"bsonType": "string"},
            "affected_versions": {"bsonType": "array", "items": {"bsonType": "string"}},
            "model_seen": _STRING_OR_NULL,
            "version_seen": _STRING_OR_NULL,
            "sounder_test": {"bsonType": ["bool", "null"]},
        },
    },
    "$expr": {"$and": [
        {"$eq": ["$shipped_in", {"$add": [f"${b}" for b in BUCKETS]}]},
        {"$or": [{"$lte": ["$fixed", "$fixed_by_panel"]}, _FIXED_BY_PHONE]},
        {"$gte": ["$installed", "$connected_unfixed"]},
        {"$or": [{"$eq": [{"$ifNull": ["$model_seen", None]}, None]}, {"$eq": ["$model_seen", "$model"]}]},
    ]},
}

# Rule 3. Edge sanity: non-negative integers, corroborated_qty <= sender_qty.
EDGE_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "from_party", "to_party", "sender_qty",
                     "corroborated_qty", "disputed_qty", "state"],
        "properties": {
            "sender_qty": _QTY, "recipient_qty": _QTY_OR_NULL,
            "corroborated_qty": _QTY, "disputed_qty": _QTY,
            "state": {"enum": ["awaiting_recipient", "corroborated", "disputed"]},
        },
    },
    "$expr": {"$lte": ["$corroborated_qty", "$sender_qty"]},
}

# Rule 4. Close gate: status may be "closed" only when open_units is 0. The
# recall also carries what the checks compare against (model, affected and
# fixed versions), copied onto each party, and where it came from: a recall
# whose source is a signal names it.
RECALL_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["batch_code", "model", "affected_versions", "fixed_version", "source", "units_total",
                     "open_units", "status"],
        "properties": {
            "batch_code": {"bsonType": "string"},
            "model": {"bsonType": "string"},
            "affected_versions": {"bsonType": "array", "items": {"bsonType": "string"}},
            "fixed_version": {"bsonType": "string"},
            "source": {"enum": ["signal", "manual"]},
            "source_signal": _STRING_OR_NULL,
            "units_total": _QTY, "open_units": _QTY,
            "status": {"enum": ["open", "closed"]},
        },
    },
    "$and": [
        {"$or": [{"status": "open"}, {"open_units": 0}]},
        {"$or": [{"source": "manual"}, {"source_signal": {"$type": "string"}}]},
    ],
}

# The long run's rule: an operation can be `verified` only with a read-back recorded.
OPERATION_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "kind", "idempotency_key", "state"],
        "properties": {
            "state": {"enum": ["intent", "sent", "verified", "needs_reverify", "failed"]},
            "idempotency_key": {"bsonType": "string"},
        },
    },
    "$or": [
        {"state": {"$ne": "verified"}},
        {"$and": [{"readback_at": {"$exists": True, "$ne": None}},
                  {"readback_value": {"$exists": True, "$ne": None}}]},
    ],
}

# The ledger's validators (the spec's rules 1 to 4), then every validator setup installs.
VALIDATORS = {"parties": PARTY_VALIDATOR, "edges": EDGE_VALIDATOR, "recalls": RECALL_VALIDATOR}
ALL_VALIDATORS = {**VALIDATORS, "operations": OPERATION_VALIDATOR}

# (collection, keys, options). Rule 5 is the partial unique index on attempts.
SPEC_INDEXES = [
    ("shipments", [("recall_id", ASCENDING), ("party_id", ASCENDING)],
     {"unique": True, "name": "uniq_recall_party"}),
    ("parties", [("recall_id", ASCENDING), ("party_id", ASCENDING)],
     {"unique": True, "name": "uniq_recall_party"}),
    ("edges", [("recall_id", ASCENDING), ("from_party", ASCENDING), ("to_party", ASCENDING)],
     {"unique": True, "name": "uniq_recall_from_to"}),
    ("claims", [("dedupe_key", ASCENDING)], {"unique": True, "name": "uniq_dedupe_key"}),
    ("attempts", [("recall_id", ASCENDING), ("party_id", ASCENDING), ("seq", ASCENDING)],
     {"unique": True, "name": "uniq_recall_party_seq"}),
    ("attempts", [("recall_id", ASCENDING), ("party_id", ASCENDING)],
     {"unique": True, "name": "one_live_attempt_per_party",
      "partialFilterExpression": {"state": {"$in": list(LIVE_ATTEMPT_STATES)}}}),
    ("tool_events", [("attempt_id", ASCENDING)], {"name": "by_attempt"}),
]

EXTRA_INDEXES = [
    ("feed", [("recall_id", ASCENDING), ("at", DESCENDING)], {"name": "by_recall_time"}),
    ("feed", [("at", DESCENDING)], {"name": "by_time"}),
    ("sim_calls", [("to", ASCENDING), ("start_time", DESCENDING)], {"name": "by_to_time"}),
    ("sim_calls", [("attempt_id", ASCENDING)], {"name": "by_attempt"}),
    ("parties", [("form_token", ASCENDING)], {"name": "by_form_token", "sparse": True}),
    ("attempts", [("recall_id", ASCENDING), ("state", ASCENDING)], {"name": "by_recall_state"}),
    ("attempts", [("call_sid", ASCENDING)], {"name": "by_call_sid", "sparse": True}),
    ("attempts", [("conversation_id", ASCENDING)], {"name": "by_conversation", "sparse": True}),
    ("tool_events", [("conversation_id", ASCENDING)], {"name": "by_conversation"}),
    ("claims", [("recall_id", ASCENDING), ("created_at", ASCENDING)], {"name": "by_recall_time"}),
    ("claims", [("attempt_id", ASCENDING)], {"name": "by_attempt"}),
    ("edges", [("recall_id", ASCENDING), ("to_party", ASCENDING)], {"name": "by_recall_to"}),
    # The long run and the harness.
    ("parties", [("recall_id", ASCENDING), ("next_action_at", ASCENDING)], {"name": "by_recall_due"}),
    ("parties", [("next_action_at", ASCENDING)], {"name": "by_due"}),
    ("operations", [("idempotency_key", ASCENDING)], {"unique": True, "name": "uniq_idempotency_key"}),
    ("operations", [("recall_id", ASCENDING), ("order_system", ASCENDING), ("state", ASCENDING)],
     {"name": "by_recall_order_system_state"}),
    ("adapters", [("order_system", ASCENDING)], {"unique": True, "name": "one_active_adapter_per_order_system",
                                           "partialFilterExpression": {"status": "active"}}),
    ("attempts", [("idempotency_key", ASCENDING)], {"unique": True, "name": "uniq_attempt_idempotency_key",
                                                    "partialFilterExpression": {"idempotency_key": {"$type": "string"}}}),
    ("task_runs", [("party_id", ASCENDING), ("status", ASCENDING)], {"name": "by_party_status"}),
    ("task_runs", [("recall_id", ASCENDING), ("started_at", DESCENDING)], {"name": "by_recall_time"}),
    ("traces", [("run_id", ASCENDING), ("seq", ASCENDING)], {"unique": True, "name": "uniq_run_seq"}),
    ("questions", [("recall_id", ASCENDING)], {"name": "by_recall"}),
    ("feed", [("recall_id", ASCENDING), ("system", ASCENDING), ("at", DESCENDING)], {"name": "by_recall_system_time"}),
]
INDEXES = SPEC_INDEXES + EXTRA_INDEXES


def vector_index_definition(dims: int) -> dict:
    """The episodes vector index: cosine over `embedding`, filterable on
    `valid` and `kind` (contact episodes and adapter repairs share the
    collection)."""
    return {"fields": [
        {"type": "vector", "path": "embedding", "numDimensions": int(dims), "similarity": "cosine"},
        {"type": "filter", "path": "valid"},
        {"type": "filter", "path": "kind"},
    ]}


def ensure_vector_index(db, dims: int, wait_s: int = 180) -> dict:
    """Create (or rebuild on a dimension change) the episodes vector index and
    wait until it is queryable.

    Works on Atlas. A plain mongod has no search process, so this reports
    ok=False there instead of raising; memory then scores locally."""
    coll = db.episodes
    try:
        current = list(coll.list_search_indexes(VECTOR_INDEX))
    except OperationFailure as exc:
        return {"ok": False, "reason": f"search indexes unavailable here ({exc.code})"}

    wanted = vector_index_definition(dims)
    if current:
        have = current[0].get("latestDefinition", {}).get("fields", [])
        have_dims = next((f.get("numDimensions") for f in have if f.get("type") == "vector"), None)
        have_filters = sorted(f.get("path") for f in have if f.get("type") == "filter")
        want_filters = sorted(f["path"] for f in wanted["fields"] if f["type"] == "filter")
        if have_dims == int(dims) and have_filters != want_filters:
            try:
                coll.update_search_index(VECTOR_INDEX, wanted)
            except OperationFailure as exc:
                return {"ok": False, "reason": f"update failed: {exc.details or exc}"}
        if have_dims != int(dims):
            coll.drop_search_index(VECTOR_INDEX)
            deadline = time.time() + wait_s
            while list(coll.list_search_indexes(VECTOR_INDEX)) and time.time() < deadline:
                time.sleep(2)
            current = []
    if not current:
        try:
            coll.create_search_index(
                SearchIndexModel(definition=wanted, name=VECTOR_INDEX, type="vectorSearch"))
        except OperationFailure as exc:
            return {"ok": False, "reason": f"create failed: {exc.details or exc}"}

    deadline = time.time() + wait_s
    while time.time() < deadline:
        info = list(coll.list_search_indexes(VECTOR_INDEX))
        if info and info[0].get("queryable"):
            return {"ok": True, "name": VECTOR_INDEX, "dims": int(dims), "queryable": True}
        time.sleep(2)
    return {"ok": False, "reason": f"index not queryable after {wait_s}s (still building)"}


def ensure_party_name_index(db, wait_s: int = 180) -> dict:
    """Create the Atlas Search index on party names and wait until it is
    queryable. ok=False where there is no search process: names are then
    matched by their normalised form."""
    coll = db.parties
    try:
        current = list(coll.list_search_indexes(PARTY_NAME_INDEX))
    except OperationFailure as exc:
        return {"ok": False, "reason": f"search indexes unavailable here ({exc.code})"}
    if not current:
        try:
            coll.create_search_index(SearchIndexModel(definition=PARTY_NAME_INDEX_DEFINITION, name=PARTY_NAME_INDEX))
        except OperationFailure as exc:
            return {"ok": False, "reason": f"create failed: {exc.details or exc}"}
    elif current[0].get("latestDefinition") != PARTY_NAME_INDEX_DEFINITION:
        try:
            coll.update_search_index(PARTY_NAME_INDEX, PARTY_NAME_INDEX_DEFINITION)
        except OperationFailure as exc:
            return {"ok": False, "reason": f"update failed: {exc.details or exc}"}
    deadline = time.time() + wait_s
    while time.time() < deadline:
        info = list(coll.list_search_indexes(PARTY_NAME_INDEX))
        if info and info[0].get("queryable"):
            return {"ok": True, "name": PARTY_NAME_INDEX, "queryable": True}
        time.sleep(2)
    return {"ok": False, "reason": f"index not queryable after {wait_s}s (still building)"}


def describe(db) -> dict:
    """What is actually installed: validators and index names per collection
    (None for a collection that does not exist)."""
    out = {}
    infos = {c["name"]: c for c in db.list_collections()}
    for name in COLLECTIONS:
        if name not in infos:
            out[name] = None
            continue
        out[name] = {
            "validator": infos[name].get("options", {}).get("validator"),
            "indexes": {ix["name"]: ix for ix in db[name].list_indexes()},
        }
    return out
