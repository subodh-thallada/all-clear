"""Spec test 48: after a full run, no buyer contact or staff phone number from
summit.json is in any document of our database, and our recall closes only
after Summit reports closed (scripts/scan_names.py)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from levela.db.setup import setup
from levela.ledger.reconciler import try_close
from levela.node import buyers, child, handoff, reporter
from levela.providers.fake_voice import FakeVoice

from node_support import RECALL, SUMMIT, child_recall_id, make_nodes, seed_parent, summit_party

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan_names.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("scan_names", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def summit_db(mongo_client, db_name):
    database = mongo_client[f"{db_name}_summit48"]
    for name in database.list_collection_names():
        database[name].delete_many({})
    setup(database)
    return database


def test_spec_48_buyer_list_stays_home_after_a_full_run(client, settings, db, summit_db):
    scan_names = _load_script()
    seed_parent(db)
    n = make_nodes(client, settings, db, summit_db)
    assert handoff.run(n.ctx, summit_party(db), sim=48.0)["action"] == "verified"
    rid = child_recall_id()
    voice = FakeVoice(faults={b["_id"]: "reject" for b in n.scn["buyers"] if not b["reachable"]})
    seqs = []
    for sim in (0.0, 20.0, 40.0, 5 * 24 + 11.0):
        child.tick(client, summit_db, n.scn, rid, sim)
        buyers.tell_all(summit_db, voice, rid, sim=sim)
        sent = reporter.send(summit_db, n.child_settings, n.parent_http, rid, sim=sim)
        assert sent["accepted"], sent
        seqs.append(sent["seq"])
    day5 = summit_party(db)
    assert day5["on_shelf"] == 9 and day5["sub"]["state"] == "open"
    assert summit_db.claims.find_one({"status": "rejected", "rule": "still_on_shelf"}) is not None
    assert try_close(db, RECALL)["closed"] is False                         # Summit has not reported closed
    assert scan_names.closed_too_early(db, RECALL) == []
    last = child.tick(client, summit_db, n.scn, rid, 9 * 24 + 10.0)
    assert last["closed"]["closed"], last
    final = reporter.send(summit_db, n.child_settings, n.parent_http, rid, sim=9 * 24 + 10.0)
    assert final["accepted"] and final["report"]["state"] == "closed", final
    p = summit_party(db)
    assert (p["sold"], p["on_shelf"], p["held"], p["returned"], p["unresolved"]) == (31, 0, 67, 22, 0)
    assert p["sub"]["buyers"] == {"identified": 19, "told": 15, "unreachable": 4, "anonymous_sales": 12}
    assert try_close(db, RECALL)["closed"] is True
    assert scan_names.closed_too_early(db, RECALL) == []
    res = scan_names.scan(db, n.scn)
    assert res["clean"], res["hits"]
    assert res["scanned"] > 10 and res["needles"] == 19 + 1 + 6
    # Summit's own database does hold them: the scan does find what is there.
    assert not scan_names.scan(summit_db, n.scn)["clean"]


def test_spec_48_scan_finds_a_planted_contact(db):
    scan_names = _load_script()
    scn = child.load_scenario()
    db.feed.insert_one({"text": f"called {scn['buyers'][3]['contact'].replace('-', '')}"})
    res = scan_names.scan(db, scn)
    assert not res["clean"] and res["hits"][0]["needle"] == scn["buyers"][3]["contact"]
