"""Sub-recall between nodes: links, buyers (spec "Sub-recall, agent to agent").

Merged into levela.db.schema (COLLECTIONS, ALL_VALIDATORS, INDEXES). Index
entries use the same shape as schema.SPEC_INDEXES: (collection, keys, options).
Do not import levela.db.schema from here (it imports this module).

`links`: one document per pairing, on both nodes. `role` is THIS node's role
in the pairing (our node holds role "parent" for Summit Foods; Summit's node
holds role "child" for us), so the unique (role, peer_node) index means a
pairing exists once on each side. The shared secret is an environment
variable (levela.node.links.link_secret) and is never stored.

`buyers`: shoppers a store can identify (membership, online orders). They
live only on the node that sold to them. A buyer is `told` only with the
attempt that told them (`attempt_id`).
"""
from __future__ import annotations

from pymongo import ASCENDING, DESCENDING  # noqa: F401

_INT0 = {"bsonType": ["int", "long"], "minimum": 0}
_INT0_OR_NULL = {"bsonType": ["int", "long", "null"], "minimum": 0}

LINK_ROLES = ["parent", "child"]
LINK_STATUSES = ["pending", "active", "quiet"]
BUYER_STATES = ["pending", "told", "unreachable"]

LINK_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["role", "peer_node", "peer_url", "match", "status", "last_seq_sent", "last_seq_accepted"],
        "properties": {
            "role": {"enum": LINK_ROLES},
            "peer_node": {"bsonType": "string"},
            "peer_url": {"bsonType": "string"},
            "match": {"bsonType": "object", "properties": {
                "system": {"bsonType": ["string", "null"]}, "account": {"bsonType": ["string", "null"]}}},
            "party_id": {"bsonType": ["string", "null"]},
            "last_seq_sent": _INT0, "last_seq_accepted": _INT0,
            "status": {"enum": LINK_STATUSES},
        },
    },
}

# Rule 3 of the section: state "told" needs an attempt_id.
BUYER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["store_id", "contact", "bought_at", "qty", "state"],
        "properties": {
            "store_id": {"bsonType": "string"},
            "contact": {"bsonType": "string"},
            "qty": {"bsonType": ["int", "long"], "minimum": 1},
            "state": {"enum": BUYER_STATES},
            "attempt_id": {"bsonType": ["string", "null"]},
        },
    },
    "$or": [{"state": {"$ne": "told"}}, {"attempt_id": {"$type": "string"}}],
}

NODE_COLLECTIONS: list[str] = ["links", "buyers"]
NODE_VALIDATORS: dict[str, dict] = {"links": LINK_VALIDATOR, "buyers": BUYER_VALIDATOR}
NODE_INDEXES: list[tuple] = [
    # Rule 2 of the section: a pairing exists once.
    ("links", [("role", ASCENDING), ("peer_node", ASCENDING)], {"unique": True, "name": "uniq_role_peer"}),
    ("links", [("match.system", ASCENDING), ("match.account", ASCENDING)], {"name": "by_match"}),
    ("buyers", [("store_id", ASCENDING), ("state", ASCENDING)], {"name": "by_store_state"}),
    # The child's recall, found by the parent's handoff key (GET /agent/recalls/{key}).
    ("recalls", [("parent.key", ASCENDING)], {"unique": True, "name": "uniq_parent_key",
                                             "partialFilterExpression": {"parent.key": {"$type": "string"}}}),
]
