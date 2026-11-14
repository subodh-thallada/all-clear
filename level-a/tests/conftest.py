"""Shared fixtures.

Tests run against the cluster named by LEVELA_TEST_URI (read from the
environment or level-a/.env). LEVELA_TEST_CLUSTER=n picks LEVELA_TEST_URI_n
instead, so parallel test sessions can be spread over several free clusters
(each M0 cluster is capped at 100 operations per second). CI sets
LEVELA_TEST_URI to a MongoDB service container.

Each test session gets one scratch database (setup() once: creating eleven
collections with validators and their indexes costs seconds on Atlas). The
`db` fixture empties every collection before each test, so tests still start
from a clean slate. A test that needs a database of its own (for example to
exercise setup() itself) uses `fresh_db`. All scratch databases are dropped
at the end of the session.
"""
from __future__ import annotations

import os
import uuid

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from levela.config import Settings, load_dotenv
from levela.db.setup import COLLECTIONS, setup
from levela.ledger.reconciler import mass_balance

load_dotenv()
DEFAULT_URI = "mongodb://127.0.0.1:27017/?replicaSet=rs0"
RUN_ID = uuid.uuid4().hex[:8]


def _uri():
    n = os.environ.get("LEVELA_TEST_CLUSTER")
    if n and os.environ.get(f"LEVELA_TEST_URI_{n}"):
        return os.environ[f"LEVELA_TEST_URI_{n}"]
    return os.environ.get("LEVELA_TEST_URI") or os.environ.get("ATLAS_TEST_URI") or DEFAULT_URI


@pytest.fixture(scope="session")
def test_uri():
    return _uri()


@pytest.fixture(scope="session")
def mongo_client(test_uri):
    client = MongoClient(test_uri, tz_aware=True, serverSelectionTimeoutMS=10000, appname="levela-tests")
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        pytest.skip(f"no MongoDB reachable for tests: {type(exc).__name__}")
    hello = client.admin.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        pytest.skip("tests need a replica set (transactions, change streams)")
    yield client
    for name in client.list_database_names():
        if name.startswith(f"levela_t_{RUN_ID}"):
            client.drop_database(name)
    client.close()


@pytest.fixture(scope="session")
def on_atlas(mongo_client) -> bool:
    try:
        return any("mongodb.net" in h for h, _ in mongo_client.nodes)
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def db_name():
    return f"levela_t_{RUN_ID}"


@pytest.fixture(scope="session")
def _session_db(mongo_client, db_name):
    database = mongo_client[db_name]
    setup(database)
    return database


@pytest.fixture
def db(_session_db):
    """The session's scratch database, emptied before the test."""
    for name in _session_db.list_collection_names():
        _session_db[name].delete_many({})
    return _session_db


@pytest.fixture
def fresh_db(mongo_client):
    """A brand-new database of the test's own (not set up), dropped afterwards."""
    name = f"levela_t_{RUN_ID}_{uuid.uuid4().hex[:6]}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


@pytest.fixture
def client(mongo_client):
    """The MongoClient the code under test uses (same as mongo_client)."""
    return mongo_client


@pytest.fixture
def settings(db_name, test_uri):
    """Settings pointing at the scratch database, with fast timings."""
    return Settings.from_env(env={}, atlas_uri=test_uri, db_name=db_name,
                             tool_secret="test-tool-secret", webhook_secret="test-webhook-secret",
                             admin_token="test-admin-token", worker_name="worker-test",
                             heartbeat_s=0.2, worker_dead_after_s=1.0, reconcile_every_s=0.2,
                             indoubt_grace_s=0.0, probe_skew_s=1.0, stale_call_s=5.0,
                             retry_backoff_s=0.5, sim_time_scale=0.0, voice_provider="sim",
                             embed_provider="hash", memory_enabled=True)


@pytest.fixture
def assert_balance(db):
    """assert_balance(recall_id) checks the global mass balance equals units_total."""
    def check(recall_id):
        r = db.recalls.find_one({"_id": recall_id})
        got = mass_balance(db, recall_id)
        assert got == r["units_total"], f"mass balance {got} != units_total {r['units_total']}"
        return got
    return check


__all__ = ["COLLECTIONS"]
