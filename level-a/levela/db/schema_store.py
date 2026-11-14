"""Store records: complaints, store_systems, store_events, signals (spec "Store records").

Merged into levela.db.schema (COLLECTIONS, ALL_VALIDATORS, INDEXES). Index
entries use the same shape as schema.SPEC_INDEXES: (collection, keys, options).
Do not import levela.db.schema from here (it imports this module).

Rules enforced by Atlas here:

  complaints     a classification.defect needs a reason code or at least one evidence id
  store_systems  one connector per store_label (unique index)
  store_events   kind is one of six; (store_system_id, event_id) is unique, so a
                 repeated post counts once. A regular collection, not time series:
                 the event and the ledger change share one transaction.
  signals        status approved needs approved_by; one open signal per batch
                 (partial unique index on batch_code while proposed or approved)
"""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING

STORE_COLLECTIONS: list[str] = ["complaints", "store_systems", "store_events", "signals"]

EVENT_KINDS = ["baseline", "sale", "blocked_sale", "quarantine", "customer_return", "delivery"]
SIGNAL_STATES = ["proposed", "approved", "dismissed"]
OPEN_SIGNAL_STATES = ["proposed", "approved"]

_STRING_OR_NULL = {"bsonType": ["string", "null"]}
_QTY = {"bsonType": ["int", "long"], "minimum": 0}

COMPLAINT_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["batch_code", "bought_at", "text", "at"],
        "properties": {
            "batch_code": {"bsonType": "string"},
            "bought_at": {"bsonType": "string"},
            "reason_code": _STRING_OR_NULL,
            "text": {"bsonType": "string"},
            "classification": {"bsonType": ["object", "null"], "properties": {
                "defect": _STRING_OR_NULL,
                "confidence": {"bsonType": ["double", "int", "long", "decimal", "null"]},
                "evidence": {"bsonType": "array", "items": {"bsonType": "string"}},
            }},
        },
    },
    # A classified complaint names its grounds: the reason code, or the
    # episodes (or the person's finding) it relied on.
    "$or": [
        {"classification.defect": None},
        {"reason_code": {"$type": "string"}},
        {"classification.evidence.0": {"$exists": True}},
    ],
}

STORE_SYSTEM_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["store_label", "connects", "blocked"],
        "properties": {
            "store_label": {"bsonType": "string", "minLength": 1},
            "party_id": _STRING_OR_NULL,
            "recall_id": _STRING_OR_NULL,
            "connects": {"bsonType": "array", "items": {"enum": ["till", "stock"]}},
            "blocked": {"bsonType": "bool"},
            "block_readback_at": {"bsonType": ["date", "null"]},
            "last_event_at": {"bsonType": ["date", "null"]},
            "stock": {"bsonType": "object", "properties": {
                "received": _QTY, "sold": _QTY, "on_shelf": _QTY, "held": _QTY}},
        },
    },
}

STORE_EVENT_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["store_system_id", "event_id", "kind", "batch_code", "qty", "at"],
        "properties": {
            "store_system_id": {"bsonType": "string"},
            "event_id": {"bsonType": "string", "minLength": 1},
            "kind": {"enum": EVENT_KINDS},
            "batch_code": {"bsonType": "string"},
            "qty": _QTY,
            "at": {"bsonType": "date"},
        },
    },
}

SIGNAL_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["defect", "batch_code", "complaints", "stores", "status"],
        "properties": {
            "defect": {"bsonType": "string"},
            "batch_code": {"bsonType": "string"},
            "complaints": {"bsonType": "array", "items": {"bsonType": "string"}},
            "stores": {"bsonType": "array", "items": {"bsonType": "string"}},
            "shipped_to": {"bsonType": "array", "items": {"bsonType": "string"}},
            "status": {"enum": SIGNAL_STATES},
            "approved_by": _STRING_OR_NULL,
            "recall_id": _STRING_OR_NULL,
        },
    },
    # A person approves; no model can. Approved needs a non-empty approved_by.
    "$or": [
        {"status": {"$ne": "approved"}},
        {"approved_by": {"$type": "string", "$ne": ""}},
    ],
}

STORE_VALIDATORS: dict[str, dict] = {
    "complaints": COMPLAINT_VALIDATOR,
    "store_systems": STORE_SYSTEM_VALIDATOR,
    "store_events": STORE_EVENT_VALIDATOR,
    "signals": SIGNAL_VALIDATOR,
}

STORE_INDEXES: list[tuple] = [
    ("complaints", [("batch_code", ASCENDING), ("at", DESCENDING)], {"name": "by_batch_time"}),
    ("complaints", [("classification.defect", ASCENDING), ("batch_code", ASCENDING)], {"name": "by_defect_batch"}),
    ("store_systems", [("store_label", ASCENDING)], {"unique": True, "name": "uniq_store_label"}),
    ("store_systems", [("recall_id", ASCENDING), ("party_id", ASCENDING)], {"name": "by_recall_party"}),
    ("store_events", [("store_system_id", ASCENDING), ("event_id", ASCENDING)],
     {"unique": True, "name": "uniq_store_event"}),
    ("store_events", [("store_system_id", ASCENDING), ("at", DESCENDING)], {"name": "by_store_time"}),
    ("store_events", [("recall_id", ASCENDING), ("at", DESCENDING)], {"name": "by_recall_time"}),
    ("signals", [("batch_code", ASCENDING)],
     {"unique": True, "name": "one_open_signal_per_batch",
      "partialFilterExpression": {"status": {"$in": OPEN_SIGNAL_STATES}}}),
]
