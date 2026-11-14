"""Mongo client, database handle, transaction helper."""
from __future__ import annotations

import threading

from pymongo import MongoClient
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from ..config import Settings

_lock = threading.Lock()
_clients: dict[str, MongoClient] = {}


def get_client(settings: Settings, appname: str = "levela") -> MongoClient:
    """One MongoClient per URI per process (MongoClient is thread-safe and pools)."""
    key = f"{settings.atlas_uri}|{appname}"
    with _lock:
        if key not in _clients:
            _clients[key] = MongoClient(
                settings.atlas_uri, appname=appname, tz_aware=True,
                serverSelectionTimeoutMS=20000, retryWrites=True,
            )
        return _clients[key]


def get_db(settings: Settings, appname: str = "levela"):
    client = get_client(settings, appname)
    return client, client[settings.db_name]


def run_txn(client: MongoClient, fn, *, counter: dict | None = None):
    """Run fn(session) in a transaction with majority read/write concern.

    with_transaction retries the whole callback on TransientTransactionError
    (write conflicts) and retries the commit on UnknownTransactionCommitResult.
    If `counter` is given, counter["runs"] records how many times fn ran, so a
    caller can show that a write conflict was retried.
    """
    runs = {"n": 0}

    def wrapped(session):
        runs["n"] += 1
        if counter is not None:
            counter["runs"] = runs["n"]
        return fn(session)

    with client.start_session() as session:
        return session.with_transaction(
            wrapped,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
        )


def is_atlas(client: MongoClient) -> bool:
    """True when connected to Atlas (where $vectorSearch exists)."""
    try:
        hosts = [f"{h}:{p}" for h, p in client.nodes] or []
    except Exception:  # noqa: BLE001
        hosts = []
    return any("mongodb.net" in h for h in hosts)
