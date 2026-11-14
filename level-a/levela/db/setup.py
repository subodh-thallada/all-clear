"""Collections, validators, indexes and the vector index. Safe to re-run.

What is on_shelf is defined in levela.db.schema, which the preflight
compares a live database with, so the two can never drift.

Atlas detail: `collMod` needs the dbAdmin role, which the default
"read and write to any database" user does not have. So a collection that
does not exist yet is created *with* its validator (createCollection is part
of readWrite), and collMod is only used to update an existing collection
whose validator differs. If that collMod is refused, setup says exactly
which role is missing instead of crashing halfway.
"""
from __future__ import annotations

from pymongo.errors import OperationFailure

from .schema import (ALL_VALIDATORS, COLLECTIONS, EXTRA_INDEXES, INDEXES, LEDGER_COLLECTIONS, OPERATION_VALIDATOR,
                     V2_COLLECTIONS, VALIDATORS, VECTOR_INDEX, ensure_vector_index)

__all__ = ["COLLECTIONS", "LEDGER_COLLECTIONS", "V2_COLLECTIONS", "VALIDATORS", "ALL_VALIDATORS",
           "OPERATION_VALIDATOR", "VECTOR_INDEX", "INDEXES", "EXTRA_INDEXES", "SetupError", "setup",
           "ensure_vector_index"]


class SetupError(Exception):
    pass


def setup(db, collections=None) -> dict:
    """Create collections (with validators), apply validators, build indexes.

    `collections` limits the work to those collections (default: all). A
    database made by an earlier setup is upgraded in place: missing
    collections are created, changed validators are applied with collMod and
    new indexes are built; documents are never touched."""
    names = [c for c in COLLECTIONS if collections is None or c in collections]
    infos = {c["name"]: c for c in db.list_collections()}
    created, updated = [], []
    for name in names:
        if name in infos:
            continue
        opts = {}
        if name in ALL_VALIDATORS:
            opts = {"validator": ALL_VALIDATORS[name], "validationLevel": "strict", "validationAction": "error"}
        db.create_collection(name, **opts)
        created.append(name)
    for name, validator in ALL_VALIDATORS.items():
        if name in created or name not in names:
            continue
        opts = infos[name].get("options", {})
        if (opts.get("validator") == validator and opts.get("validationLevel", "strict") == "strict"
                and opts.get("validationAction", "error") == "error"):
            continue
        try:
            db.command("collMod", name, validator=validator, validationLevel="strict", validationAction="error")
            updated.append(name)
        except OperationFailure as exc:
            if exc.code in (13, 8000):
                raise SetupError(
                    f"collection {name} exists with a different validator and this user may not run collMod "
                    f"(needs dbAdmin on {db.name}). Drop the collection and re-run setup, or grant dbAdmin.") from exc
            raise
    indexes = [ix for ix in INDEXES if ix[0] in names]
    for coll, keys, opts in indexes:
        db[coll].create_index(keys, **opts)
    return {"created": created, "validators_updated": updated,
            "validators": sorted(n for n in ALL_VALIDATORS if n in names), "indexes": len(indexes)}
