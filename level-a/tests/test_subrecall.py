"""Sub-recall, agent to agent: spec tests 43, 44, 45, 46, 47 and 49.

Two databases on the test server (ours and Summit Foods'), the two apps
called in-process, the fake voice provider. No network, no real call.
"""
from __future__ import annotations

import json

import pytest

from levela.db.setup import setup
from levela.ledger.reconciler import try_close
from levela.node import buyers, child, handoff, links, quiet, reporter
from levela.node.report import book_report
from levela.orchestrator.failpoints import WorkerCrash
from levela.providers.fake_voice import FakeVoice

from node_support import (LINK, RECALL, SECRET, SUMMIT, child_recall_id, make_nodes, scenario, seed_parent,
                          summit_party)


@pytest.fixture(scope="module")
def _summit_session_db(mongo_client, db_name):
    database = mongo_client[f"{db_name}_summit"]
    setup(database)
    return database


@pytest.fixture
def summit_db(_summit_session_db):
    for name in _summit_session_db.list_collection_names():
        _summit_session_db[name].delete_many({})
    return _summit_session_db


@pytest.fixture
def nodes(client, settings, db, summit_db):
    seed_parent(db)
    return make_nodes(client, settings, db, summit_db)


def _crash(name):
    raise WorkerCrash(name)


def _hand_off(n, sim=48.0):
    return handoff.run(n.ctx, summit_party(n.db), sim=sim)


def _post_report(n, report, *, secret=SECRET):
    raw = links.body_bytes(report)
    return n.parent_http.inner.post("/agent/reports", content=raw, headers=links.signed_headers(raw, secret))


def _report(seq, *, sold=31, on_shelf=9, held=58, returned=22, unresolved=0, evidence=None, state="open"):
    return {"link": LINK, "seq": seq, "recall": RECALL, "units_received": 120,
            "buckets": {"sold": sold, "on_shelf": on_shelf, "held": held, "returned": returned,
                        "unresolved": unresolved},
            "evidence": evidence or {"sold_by_till": 31, "held_by_system": 58, "system_on_shelf": 9},
            "buyers": {"identified": 19, "told": 15, "unreachable": 4, "anonymous_sales": 12},
            "parties": {"total": 7, "reconciled": 4, "open": 3}, "state": state, "at": 158}


# --------------------------------------------------------------------- 43

def test_spec_43_handoff_once_after_kill(nodes):
    """Killed after sending: the adopting worker reads back first and Summit holds exactly one sub-recall."""
    n = nodes
    n.ctx.failpoints = _crash
    with pytest.raises(WorkerCrash):
        _hand_off(n)
    op = n.db.operations.find_one({"kind": "handoff"})
    assert op["state"] == "sent" and op["idempotency_key"] == f"{RECALL}:{SUMMIT}:handoff"
    n.ctx.failpoints = None
    out = handoff.resolve(n.ctx, op, sim=49.0)
    assert out["action"] == "verified", out
    assert n.summit_db.recalls.count_documents({"parent.key": op["idempotency_key"]}) == 1
    assert n.child_http.count("POST", "/agent/recalls") == 1
    assert n.db.operations.find_one({"_id": op["_id"]})["readback_value"]["received"] == 120
    # A second worker asking for the same handoff finds the verified operation.
    again = _hand_off(n)
    assert again["action"] == "duplicate" and n.child_http.count("POST", "/agent/recalls") == 1
    assert n.db.operations.count_documents({"kind": "handoff"}) == 1


def test_spec_43_lost_answer_is_read_back_not_resent(nodes):
    n = nodes
    # The child takes the sub-recall, but the answer never reaches our worker.
    real_post = n.child_http.inner.post

    def lossy(url, **kw):
        real_post(url, **kw)
        import httpx
        raise httpx.ReadTimeout("answer lost")
    n.child_http.inner.post = lossy
    first = _hand_off(n)
    assert first["state"] == "sent"
    n.child_http.inner.post = real_post
    out = handoff.resolve(n.ctx, n.db.operations.find_one({"kind": "handoff"}), sim=49.0)
    assert out["action"] == "verified"
    assert n.child_http.count("POST", "/agent/recalls") == 1
    assert n.summit_db.recalls.count_documents({}) == 1


# --------------------------------------------------------------------- 44

def test_spec_44_match_by_barcode_and_book_120(nodes):
    n = nodes
    out = _hand_off(n)
    assert out["action"] == "verified"
    sub = n.summit_db.recalls.find_one({"_id": child_recall_id()})
    assert sub["matched_item"] == "SF-10233" and sub["match"]["decided_by"] == "barcode"
    assert sub["parent"] == {"link_id": LINK, "recall_id": RECALL, "key": f"{RECALL}:{SUMMIT}:handoff",
                             "node": "maple-farm", "units_sent": 120}
    edge = n.db.edges.find_one({"to_party": SUMMIT})
    assert edge["state"] == "corroborated" and edge["corroborated_qty"] == 120 and edge["disputed_qty"] == 0
    p = summit_party(n.db)
    assert p["shipped_in"] == 120 and p["unresolved"] == 120 and p["channel"] == "agent"
    assert n.db.claims.find_one({"dedupe_key": f"{LINK}:handoff"})["source"] == "agent"
    assert n.db.recalls.find_one({"_id": RECALL})["open_units"] == 120


def test_spec_44_match_by_description_cites_ids(nodes):
    n = nodes
    body = {"key": "r-9:summit-foods:handoff", "recall": "r-9", "link": LINK, "product": "Maple Farm milk powder 800 g",
            "barcode": None, "batch_code": "B-4471", "affected_batches": ["B-4471"], "action": "return", "units": 120}
    out = child.take_sub_recall(n.client, n.summit_db, n.scn, n.embedder, body)
    assert out["state"] == "taken" and out["matched_item"] == "SF-10233"
    assert out["match"]["decided_by"] == "description"
    assert out["match"]["cited"][0] == "item:SF-10233" and len(out["match"]["cited"]) == 2


def test_spec_44_low_confidence_books_nothing_and_asks_a_person(nodes):
    n = nodes
    n.db.recalls.update_one({"_id": RECALL}, {"$set": {"barcode": None, "product": "milk powder"}})
    out = _hand_off(n)
    assert out["action"] == "waiting"
    sub = n.summit_db.recalls.find_one({"_id": child_recall_id()})
    assert sub["match_state"] == "needs_person" and sub["units_total"] == 0 and sub["matched_item"] is None
    assert n.summit_db.parties.count_documents({}) == 0
    assert n.summit_db.feed.count_documents({"kind": "match_needs_person"}) == 1
    p = summit_party(n.db)
    assert p["shipped_in"] == 0 and n.db.operations.find_one({"kind": "handoff"})["state"] == "sent"
    assert n.db.edges.find_one({"to_party": SUMMIT})["state"] == "awaiting_recipient"


def test_spec_44_received_110_opens_dispute_of_10(client, settings, db, summit_db):
    seed_parent(db)
    n = make_nodes(client, settings, db, summit_db, scn=scenario(received=110))
    out = _hand_off(n)
    assert out["action"] == "verified" and out["disputes"][0]["disputed_qty"] == 10
    edge = db.edges.find_one({"to_party": SUMMIT})
    assert (edge["state"], edge["recipient_qty"], edge["corroborated_qty"], edge["disputed_qty"]) == \
        ("disputed", 110, 110, 10)
    p = summit_party(db)
    assert p["shipped_in"] == 110 and p["unresolved"] == 110
    assert db.recalls.find_one({"_id": RECALL})["open_units"] == 120          # 110 unresolved + 10 disputed


# --------------------------------------------------------------------- 45

def test_spec_45_till_outranks_the_staff_in_summits_database(nodes):
    n = nodes
    _hand_off(n)
    rid = child_recall_id()
    child.tick(n.client, n.summit_db, n.scn, rid, 0.0)              # connectors read: baseline per store
    store = next(s for s in n.scn["stores"] if s["profile"] == "over_reporter")
    p = n.summit_db.parties.find_one({"recall_id": rid, "party_id": store["party_id"]})
    assert p["system_on_shelf"] == 9 and p["on_shelf"] == 9
    all_pulled = {"sold": store["sold"], "on_shelf": 0, "held": 9, "returned": 0, "batch_seen": "B-4471",
                  "block_test": True, "sold_source": "till_report"}
    refused = child.staff_claim(n.client, n.summit_db, rid, store["store_id"], all_pulled, label="call-1")
    assert not refused["accepted"] and refused["rule"] == "still_on_shelf" and refused["values"] == [0, 9]
    assert n.summit_db.claims.find_one({"_id": refused["claim_id"]})["enforced_by"] == "atlas"
    for k in range(9):
        child.store_event(n.client, n.summit_db, rid, store, "quarantine", event_id=f"q-{k}", sim=200.0)
    accepted = child.staff_claim(n.client, n.summit_db, rid, store["store_id"], all_pulled, label="call-2")
    assert accepted["accepted"], accepted
    p = n.summit_db.parties.find_one({"recall_id": rid, "party_id": store["party_id"]})
    assert (p["on_shelf"], p["held"], p["held_by_system"], p["system_on_shelf"]) == (0, 9, 9, 0)


# --------------------------------------------------------------------- 46

def test_spec_46_conservation_evidence_replay_and_signature(nodes):
    n = nodes
    _hand_off(n)
    r = _post_report(n, _report(1, held=57)).json()                     # adds up to 119
    assert not r["accepted"] and r["rule"] == "under_count" and r["values"] == [119, 120]
    r = _post_report(n, _report(2, held=60, returned=20)).json()        # held above held_by_system, no checks
    assert not r["accepted"] and r["rule"] == "held_needs_evidence" and r["values"] == [60, 58]
    r = _post_report(n, _report(3, state="closed")).json()              # closed with 9 on a shelf
    assert not r["accepted"] and r["rule"] == "sub_closed"
    ok = _post_report(n, _report(5)).json()
    assert ok["accepted"], ok
    again = _post_report(n, _report(5)).json()
    assert again["accepted"] and again["claim_id"] == ok["claim_id"] and again["duplicate"]
    assert n.db.claims.count_documents({"dedupe_key": f"{LINK}:5"}) == 1
    lower = _post_report(n, _report(4, on_shelf=0, held=67, evidence={
        "sold_by_till": 31, "held_by_system": 67, "system_on_shelf": 0})).json()
    assert lower["ignored"] and lower["rule"] == "stale_seq"
    assert n.db.claims.count_documents({"dedupe_key": f"{LINK}:4"}) == 0
    p = summit_party(n.db)
    assert (p["on_shelf"], p["held"], p["sub"]["seq"]) == (9, 58, 5)
    assert p["sub"]["buyers"] == {"identified": 19, "told": 15, "unreachable": 4, "anonymous_sales": 12}
    before = n.db.traces.count_documents({"tool": "agent_signature"})
    bad = _post_report(n, _report(6), secret="not-the-secret")
    assert bad.status_code == 401
    assert n.db.traces.count_documents({"tool": "agent_signature"}) == before + 1
    raw = links.body_bytes(_report(6))
    old = n.parent_http.inner.post("/agent/reports", content=raw,
                                   headers=links.signed_headers(raw, SECRET, t=1_000_000_000))
    assert old.status_code == 401
    assert summit_party(n.db)["sub"]["seq"] == 5


def test_spec_46_over_report_button_is_refused_by_our_ledger(nodes):
    n = nodes
    _hand_off(n)
    rid = child_recall_id()
    child.tick(n.client, n.summit_db, n.scn, rid, 0.0)
    first = reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=1.0)
    assert first["accepted"], first
    press = n.child_http.inner.post("/admin/peer/over-report", headers={"X-Admin-Token": "test-admin-token"})
    assert press.status_code == 200
    over = reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=2.0)
    assert not over["accepted"] and over["reply"]["rule"] == "held_needs_evidence"
    held, by_system = over["reply"]["values"]
    assert held == by_system + 2
    honest = reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=3.0)
    assert honest["accepted"] and honest["seq"] == over["seq"] + 1


# --------------------------------------------------------------------- 47

def test_spec_47_quiet_agent_walks_the_ladder_and_recovers(nodes):
    n = nodes
    _hand_off(n)
    rid = child_recall_id()
    child.tick(n.client, n.summit_db, n.scn, rid, 0.0)
    assert reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=1.0)["accepted"]
    p = summit_party(n.db)
    due_at = p["next_action_at"]
    voice = FakeVoice()
    n.child_http.stopped = True                                       # Summit's node is down
    first = quiet.step(n.db, n.parent_settings, n.child_http, voice, p, sim=due_at + 0.1)
    assert first["step"] == 2 and first["action"] == "phoned_business"
    assert voice.count(SUMMIT) == 1 and voice.calls[0]["to"] == "555-0400"
    assert n.db.links.find_one({"_id": LINK})["status"] == "quiet"
    p = summit_party(n.db)
    third = quiet.step(n.db, n.parent_settings, n.child_http, voice, p, sim=p["next_action_at"] + 0.1)
    assert third["step"] == 3 and summit_party(n.db)["escalated"]
    assert voice.count(SUMMIT) == 1
    left = summit_party(n.db)
    assert left["unresolved"] + left["on_shelf"] > 0
    assert try_close(n.db, RECALL)["closed"] is False
    n.child_http.stopped = False                                      # restarted
    alive = quiet.health(n.parent_settings, n.child_http, n.db.links.find_one({"_id": LINK}), ask_report=True)
    assert alive["ok"]
    sent = reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=80.0)
    assert sent["sent"] and sent["accepted"], sent
    p = summit_party(n.db)
    assert p["quiet_step"] == 0 and n.db.links.find_one({"_id": LINK})["status"] == "active"


# --------------------------------------------------------------------- 49

def test_spec_49_buyers_told_once_even_after_a_kill(nodes):
    n = nodes
    _hand_off(n)
    rid = child_recall_id()
    child.tick(n.client, n.summit_db, n.scn, rid, 0.0)
    unreachable = [b["_id"] for b in n.scn["buyers"] if not b["reachable"]]
    voice = FakeVoice(faults={b: "reject" for b in unreachable})
    first = n.scn["buyers"][0]["_id"] if n.scn["buyers"][0]["reachable"] else n.scn["buyers"][1]["_id"]
    killed = {"done": False}

    def crash_once(name):
        if not killed["done"]:
            killed["done"] = True
            raise WorkerCrash(name)
    with pytest.raises(WorkerCrash):
        buyers.tell_all(n.summit_db, voice, rid, failpoints=crash_once)
    assert n.summit_db.buyers.find_one({"_id": first})["state"] == "pending"
    out = buyers.tell_all(n.summit_db, voice, rid)                    # the adopting worker
    rows = list(n.summit_db.buyers.find())
    assert len(rows) == 19
    assert sum(b["state"] == "told" for b in rows) == 15 and sum(b["state"] == "unreachable" for b in rows) == 4
    for b in rows:
        asked = [c for c in voice.calls if c["party_id"] == b["_id"]]
        assert len(asked) == 1, b["_id"]                              # one request to the provider, ever
        assert voice.count(b["_id"]) == (1 if b["state"] == "told" else 0)
        live = [a for a in n.summit_db.attempts.find({"party_id": b["_id"]}) if a["state"] != "abandoned"]
        assert len(live) == 1 and live[0]["state"] in ("completed", "failed") and b["attempt_id"] == live[0]["_id"]
    assert out["pending"] == 0
    assert n.summit_db.attempts.find_one({"party_id": first})["adopted"]["evidence"].endswith("call log")
    sent = reporter.send(n.summit_db, n.child_settings, n.parent_http, rid, sim=30.0)
    assert sent["report"]["buyers"] == {"identified": 19, "told": 15, "unreachable": 4, "anonymous_sales": 12}
    text = json.dumps(sent["report"])
    assert not any(b["contact"] in text for b in n.scn["buyers"])


def test_book_report_signature_is_fixed(nodes):
    """WS2's simulator calls book_report directly (fixed signature and reply keys)."""
    n = nodes
    _hand_off(n)
    out = book_report(n.db, recall_id=RECALL, party_id=SUMMIT, report=_report(1))
    assert set(out) >= {"accepted", "claim_id", "rule", "values"} and out["accepted"]


def test_worker_act_hands_off_then_waits_for_reports(nodes):
    """The dispatch hook: a due agent party is handed off once, then left alone until its report is overdue."""
    from levela.node import worker
    n = nodes
    voice = FakeVoice()
    first = worker.act(n.ctx, summit_party(n.db), voice=voice, sim=48.0)
    assert first["action"] == "verified"
    second = worker.act(n.ctx, summit_party(n.db), voice=voice, sim=50.0)
    assert second["action"] == "not_due" and voice.count() == 0
    assert n.child_http.count("POST", "/agent/recalls") == 1
