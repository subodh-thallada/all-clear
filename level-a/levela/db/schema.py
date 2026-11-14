"""What Atlas holds and enforces: collections, validators, indexes and the
vector index. levela.db.setup installs it; the preflight compares a live
database with it (describe()).

The spec's rules enforced by Atlas:

  1  party conservation          validator on parties
  2  removal needs evidence      validator on parties
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
# Store records and the sub-recall keep their collections, validators and
# indexes in their own modules (one owner each); they are merged here.
from .schema_node import NODE_COLLECTIONS, NODE_INDEXES, NODE_VALIDATORS  # noqa: E402
from .schema_store import STORE_COLLECTIONS, STORE_INDEXES, STORE_VALIDATORS  # noqa: E402

COLLECTIONS = SPEC_COLLECTIONS + EXTRA_COLLECTIONS + V2_COLLECTIONS + STORE_COLLECTIONS + NODE_COLLECTIONS
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

# The six buckets a party's tins fall into. `on_shelf` is the open bucket
# (still for sale); `held` is pulled from sale and kept apart, and needs
# evidence; `sold` reached shoppers and is bounded by the till.
BUCKETS = ("sold", "on_shelf", "held", "returned", "forwarded", "unresolved")
# What a party's own records show (ints >= 0, default 0 when absent).
EVIDENCE_FIELDS = ("held_by_system", "sold_by_till", "system_on_shelf")
# The three check answers a store gives on a call (spec "Checks").
CHECK_FIELDS = ("batch_seen", "sold_source", "block_test")
# A sold count read from the till's item sales report.
TILL_REPORT = "till_report"
PARTY_TYPES = ["plant", "distributor", "store", "self"]
PARTY_TIERS = [0, 1, 2, 3]
PARTY_CHANNELS = ["voice", "sim", "form", "order_system", "agent"]
_STRING_OR_NULL = {"bsonType": ["string", "null"]}


def _evidence(name: str) -> dict:
    """An evidence count, 0 when the party has none recorded."""
    return {"$ifNull": [f"${name}", 0]}


# Rule 1. Party conservation: every quantity is a non-negative integer and
#   shipped_in = sold + on_shelf + held + returned + forwarded + unresolved.
# Rule 2. Removal needs evidence:
#   held <= held_by_system, unless the party read the recalled batch code
#     (batch_seen == batch_code, both strings) off a tin AND the till refused
#     the sale (block_test true);
#   sold <= sold_by_till, unless the count was read from the till report
#     (sold_source == "till_report");
#   on_shelf >= system_on_shelf (the store's stock record outranks the phone);
#   sub.state "closed" (a sub-recall) needs on_shelf == 0 and unresolved == 0.
_HELD_BY_PHONE = {"$and": [
    {"$eq": [{"$type": "$batch_seen"}, "string"]},
    {"$eq": [{"$type": "$batch_code"}, "string"]},
    {"$eq": ["$batch_seen", "$batch_code"]},
    {"$eq": ["$block_test", True]},
]}
_SOLD_FROM_TILL = {"$eq": ["$sold_source", TILL_REPORT]}
_SUB_CLOSED_IS_EMPTY = {"$or": [
    {"$ne": [{"$ifNull": ["$sub.state", None]}, "closed"]},
    {"$and": [{"$eq": ["$on_shelf", 0]}, {"$eq": ["$unresolved", 0]}]},
]}
PARTY_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "party_id", "name", "type", "tier", "channel", "state", "shipped_in", *BUCKETS],
        "properties": {
            "recall_id": {"bsonType": "string"},
            "party_id": {"bsonType": "string"},
            "type": {"enum": PARTY_TYPES},
            "tier": {"bsonType": "int", "enum": PARTY_TIERS},
            "shipped_in": _QTY, **{b: _QTY for b in BUCKETS},
            **{e: _QTY for e in EVIDENCE_FIELDS},
            "state": {"enum": ["pending", "contacting", "reached", "claimed", "reconciled"]},
            "channel": {"enum": PARTY_CHANNELS},
            "batch_code": {"bsonType": "string"},
            "batch_seen": _STRING_OR_NULL,
            "sold_source": _STRING_OR_NULL,
            "block_test": {"bsonType": ["bool", "null"]},
            "link_id": _STRING_OR_NULL,
            "sub": {"bsonType": ["object", "null"], "properties": {
                "seq": {"bsonType": ["int", "long"], "minimum": 0},
                "parties_total": _QTY, "parties_open": _QTY,
                "state": _STRING_OR_NULL,
            }},
        },
    },
    "$expr": {"$and": [
        {"$eq": ["$shipped_in", {"$add": [f"${b}" for b in BUCKETS]}]},
        {"$or": [{"$lte": ["$held", _evidence("held_by_system")]}, _HELD_BY_PHONE]},
        {"$or": [{"$lte": ["$sold", _evidence("sold_by_till")]}, _SOLD_FROM_TILL]},
        {"$gte": ["$on_shelf", _evidence("system_on_shelf")]},
        _SUB_CLOSED_IS_EMPTY,
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
# recall also carries what the checks compare against (product, barcode,
# affected batches, the action stores must take, the defect); its batch code
# is copied onto each party. A recall whose source is a signal names it; a
# child node's recall names its parent (link and recall id).
RECALL_ACTIONS = ["return", "destroy", None]
RECALL_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["batch_code", "product", "affected_batches", "source", "units_total", "open_units", "status"],
        "properties": {
            "batch_code": {"bsonType": "string"},
            "product": {"bsonType": "string"},
            "barcode": _STRING_OR_NULL,
            "affected_batches": {"bsonType": "array", "items": {"bsonType": "string"}},
            "action": {"enum": RECALL_ACTIONS},
            "barcode_verified": {"bsonType": "bool"},
            "defect": _STRING_OR_NULL,
            "checks": {"bsonType": ["array", "null"]},
            "source": {"enum": ["signal", "manual"]},
            "source_signal": _STRING_OR_NULL,
            "parent": {"bsonType": ["object", "null"], "properties": {
                "link_id": {"bsonType": "string"}, "recall_id": {"bsonType": "string"}}},
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

# Claims: what said it (spec "Data model"; `agent` is another node's signed
# report, `store_system` a connected store's own records; `order_system` and
# `rep` are the long run's) and whether it was booked.
CLAIM_SOURCES = ["voice", "sim", "form", "store_system", "agent", "order_system", "rep"]
CLAIM_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["recall_id", "party_id", "source", "status", "dedupe_key"],
        "properties": {
            "source": {"enum": CLAIM_SOURCES},
            "status": {"enum": ["accepted", "rejected"]},
            "dedupe_key": {"bsonType": "string"},
        },
    },
}

# The ledger's validators (the spec's rules 1 to 4), then every validator setup installs.
VALIDATORS = {"parties": PARTY_VALIDATOR, "edges": EDGE_VALIDATOR, "recalls": RECALL_VALIDATOR}
ALL_VALIDATORS = {**VALIDATORS, "claims": CLAIM_VALIDATOR, "operations": OPERATION_VALIDATOR, **STORE_VALIDATORS,
                  **NODE_VALIDATORS}

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
    # WS6 (governed memory): the episodes TTL index on expires_at goes here.
    ("episodes", [("expires_at", ASCENDING)], {"name": "episodes_ttl_expires_at", "expireAfterSeconds": 0}),
]
INDEXES = SPEC_INDEXES + EXTRA_INDEXES + STORE_INDEXES + NODE_INDEXES


def vector_index_definition(dims: int) -> dict:
    """The episodes vector index: cosine over `embedding`, filterable on
    `valid` and `kind` (contact episodes and adapter repairs share the
    collection).

    WS6 (governed memory) owns the filter list: add `scope`, `expires_at`
    and any other filter field here."""
    return {"fields": [
        {"type": "vector", "path": "embedding", "numDimensions": int(dims), "similarity": "cosine"},
        {"type": "filter", "path": "valid"},
        {"type": "filter", "path": "kind"},
        {"type": "filter", "path": "scope"},
        {"type": "filter", "path": "expires_at"},
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
    """What is actually in place: validators and index names per collection
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
