"""Store records: removal by the store's own record (spec test 36) and the
till outranking the phone (spec test 37). Also the connector link and the
simulated store page."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import WriteError

from levela.api.main import create_app
from levela.ledger.claims import submit_claim
from levela.ledger.reconciler import VALIDATION_FAILED
from levela.monitoring import link
from levela.monitoring.store_events import post_event
from levela.db.schema_store import STORE_COLLECTIONS
from levela.db.setup import setup
from support import BATCH, HELD_EVIDENCE, party_doc, recall_doc

RID = "R-store"
STORE = "corner-mart"
BUCKETS = ("sold", "on_shelf", "held", "returned", "forwarded", "unresolved")


@pytest.fixture(autouse=True)
def _store_schema(db):
    """The store collections with their validators and indexes, even if an
    earlier test in the session dropped them."""
    setup(db, STORE_COLLECTIONS)


def _party(db):
    return db.parties.find_one({"recall_id": RID, "party_id": STORE})


def _connected(db, *, shipped_in=25, on_shelf=25, stock=None, **over):
    """Corner Mart: 25 tins on the ledger, its connector linked with a baseline
    of 25 on the shelf per its own stock record."""
    db.recalls.insert_one(recall_doc(RID, units_total=shipped_in))
    db.parties.insert_one(party_doc(RID, STORE, shipped_in, name="Corner Mart", on_shelf=on_shelf,
                                    unresolved=shipped_in - on_shelf, **over))
    link.register_store(db, STORE, "Corner Mart", batch_code=BATCH,
                        stock=stock or {"received": 25, "sold": 0, "on_shelf": 25, "held": 0})
    out = link.link_store(db, STORE, RID, STORE)
    assert out["linked"] and out["baseline"]["accepted"], out
    return out


def _balanced(p) -> None:
    assert p["shipped_in"] == sum(p[b] for b in BUCKETS)


def test_spec_36_quarantine_moves_exactly_one_tin(db):
    """Spec 36: a quarantine event moves exactly one tin from on_shelf to held."""
    _connected(db)
    p = _party(db)
    assert (p["on_shelf"], p["held"], p["system_on_shelf"], p["held_by_system"]) == (25, 0, 25, 0)
    res = post_event(db, STORE, {"event_id": "ev-1", "kind": "quarantine", "batch_code": BATCH})
    assert res["accepted"] and not res["duplicate"]
    assert res["moves"] == [{"from": "on_shelf", "to": "held", "qty": 1}]
    p = _party(db)
    assert (p["on_shelf"], p["held"], p["held_by_system"], p["system_on_shelf"]) == (24, 1, 1, 24)
    _balanced(p)
    store = db.store_systems.find_one({"_id": STORE})
    assert store["stock"]["on_shelf"] == 24 and store["stock"]["held"] == 1
    assert db.store_events.count_documents({"store_system_id": STORE, "kind": "quarantine"}) == 1


def test_spec_36_same_event_id_moves_nothing(db):
    """Spec 36: posting the same event_id again moves nothing."""
    _connected(db)
    post_event(db, STORE, {"event_id": "ev-1", "kind": "quarantine", "batch_code": BATCH})
    again = post_event(db, STORE, {"event_id": "ev-1", "kind": "quarantine", "batch_code": BATCH})
    assert again["accepted"] and again["duplicate"] and again["moves"] == []
    p = _party(db)
    assert (p["on_shelf"], p["held"], p["held_by_system"]) == (24, 1, 1)
    assert db.store_events.count_documents({"store_system_id": STORE, "event_id": "ev-1"}) == 1


def test_spec_36_blocked_sale_moves_nothing(db):
    """Spec 36: a blocked_sale moves nothing (and proves the barcode)."""
    _connected(db)
    link.set_store(db, STORE, blocked=True)
    before = _party(db)
    res = post_event(db, STORE, {"event_id": "ev-b", "kind": "blocked_sale", "batch_code": BATCH,
                                 "barcode": "0 62345 18803 5", "text": "recalled batch, not for sale"})
    assert res["accepted"] and res["moves"] == []
    after = _party(db)
    assert {k: after[k] for k in BUCKETS} == {k: before[k] for k in BUCKETS}
    assert after["system_on_shelf"] == before["system_on_shelf"] and after["sold_by_till"] == before["sold_by_till"]
    assert db.recalls.find_one({"_id": RID})["barcode_verified"] is True


def test_spec_36_sale_after_block_reopens_the_store(db):
    """Spec 36: a sale after the block was read back reopens the store, in red."""
    _connected(db)
    sale = post_event(db, STORE, {"event_id": "ev-s1", "kind": "sale", "batch_code": BATCH})
    assert not sale["breach"]
    p = _party(db)
    assert (p["sold"], p["sold_by_till"], p["on_shelf"]) == (1, 1, 24)
    db.parties.update_one({"_id": p["_id"]}, {"$set": {"state": "claimed"}})
    link.set_store(db, STORE, blocked=True)
    assert db.store_systems.find_one({"_id": STORE})["block_readback_at"] is not None
    breach = post_event(db, STORE, {"event_id": "ev-s2", "kind": "sale", "batch_code": BATCH})
    assert breach["accepted"] and breach["breach"] and breach["reopened"]
    p = _party(db)
    assert p["state"] == "reached" and p["breach"]["event_id"] == "ev-s2"
    assert (p["sold"], p["sold_by_till"], p["on_shelf"]) == (2, 2, 23)
    _balanced(p)
    line = db.feed.find_one({"kind": "store_event", "detail.event_id": "ev-s2"})
    assert line["level"] == "error" and line["text"].startswith("BREACH")


def test_spec_36_customer_return_goes_to_held_never_the_shelf(db):
    _connected(db)
    post_event(db, STORE, {"event_id": "ev-s1", "kind": "sale", "batch_code": BATCH})
    res = post_event(db, STORE, {"event_id": "ev-r1", "kind": "customer_return", "batch_code": BATCH})
    assert res["moves"] == [{"from": "sold", "to": "held", "qty": 1}]
    p = _party(db)
    assert (p["sold"], p["held"], p["on_shelf"], p["held_by_system"]) == (0, 1, 24, 1)


def test_spec_37_till_outranks_the_phone(db, client):
    """Spec 37: a claim of all pulled is rejected by Atlas while the linked
    store's stock record shows a tin on the shelf; after its quarantine
    events the same claim is accepted."""
    _connected(db)
    all_pulled = {"sold": 0, "on_shelf": 0, "held": 25, "returned": 0, **HELD_EVIDENCE}
    first = submit_claim(client, db, recall_id=RID, party_id=STORE, attempt_id="att-1", source="voice",
                         raw=all_pulled)
    assert first["accepted"] is False, first
    p = _party(db)
    assert (p["on_shelf"], p["held"]) == (25, 0)
    # The same refusal straight at Atlas: on_shelf below system_on_shelf.
    with pytest.raises(WriteError) as err:
        db.parties.update_one({"_id": p["_id"]}, {"$set": {"on_shelf": 0, "held": 25, **HELD_EVIDENCE}})
    assert err.value.code == VALIDATION_FAILED
    for i in range(25):
        assert post_event(db, STORE, {"event_id": f"q-{i}", "kind": "quarantine", "batch_code": BATCH})["accepted"]
    p = _party(db)
    assert (p["on_shelf"], p["held"], p["held_by_system"], p["system_on_shelf"]) == (0, 25, 25, 0)
    again = submit_claim(client, db, recall_id=RID, party_id=STORE, attempt_id="att-2", source="voice",
                         raw=all_pulled)
    assert again["accepted"] is True, again


def test_link_by_store_label_and_baseline(db):
    """A connector is linked when a party carries its store name (normalised)."""
    db.recalls.insert_one(recall_doc(RID, units_total=25))
    db.parties.insert_one(party_doc(RID, STORE, 25, name="Corner Mart"))
    link.register_store(db, "ss-1", "The Corner-Mart", batch_code=BATCH,
                        stock={"received": 25, "sold": 3, "on_shelf": 22, "held": 0})
    made = link.link_recall(db, RID)
    assert [m["party_id"] for m in made] == [STORE]
    p = _party(db)
    assert p["store_system_id"] == "ss-1"
    assert (p["sold"], p["on_shelf"], p["unresolved"], p["sold_by_till"], p["system_on_shelf"]) == (3, 22, 0, 3, 22)
    assert link.link_recall(db, RID) == []      # already linked


def test_block_waits_for_the_barcode(db):
    """Only the first store is blocked before a till recognises the product."""
    db.recalls.insert_one(recall_doc(RID, units_total=50))
    for sid, name in (("a", "Store A"), ("b", "Store B")):
        db.parties.insert_one(party_doc(RID, sid, 25, name=name))
        link.register_store(db, f"ss-{sid}", name, batch_code=BATCH, stock={"received": 25, "on_shelf": 25})
        link.link_store(db, f"ss-{sid}", RID, sid)
    assert link.request_block(db, "ss-a")["requested"] is True
    later = link.request_block(db, "ss-b")
    assert later["requested"] is False and "barcode not verified" in later["reason"]
    res = post_event(db, "ss-a", {"event_id": "scan-1", "kind": "blocked_sale", "batch_code": BATCH,
                                  "barcode": "0 62345 18803 5"})
    assert res["barcode_verified"] and res["blocks_sent"] == ["ss-b"]
    assert db.store_systems.find_one({"_id": "ss-b"})["blocked"] is True


def test_store_page_and_buttons(db, settings, client):
    _connected(db)
    app = create_app(settings, client=client, sim_network=object())
    with TestClient(app) as http:
        page = http.get(f"/store/{STORE}")
        assert page.status_code == 200 and "SIMULATED" in page.text and "25 on the shelf" in page.text
        assert "Move one tin to quarantine" not in page.text          # no key, no buttons
        key = settings.admin_token
        page = http.get(f"/store/{STORE}?key={key}")
        assert "Move one tin to quarantine" in page.text and "Scan a tin at the till" in page.text
        r = http.post(f"/store/{STORE}/press?key={key}", data={"action": "quarantine"}, follow_redirects=False)
        assert r.status_code == 303
        assert _party(db)["held"] == 1
        link.set_store(db, STORE, blocked=True)
        r = http.post(f"/store/{STORE}/press?key={key}", data={"action": "scan"}, headers={"accept": "application/json"})
        assert r.json()["kind"] == "blocked_sale"
        assert "BLOCKED" in http.get(f"/store/{STORE}").text
        # The connector's own endpoint needs the shared secret; a repeat counts once.
        ev = {"event_id": "api-1", "kind": "quarantine", "batch_code": BATCH}
        assert http.post(f"/stores/{STORE}/events", json=ev).status_code == 401
        h = {"X-Tool-Secret": settings.tool_secret}
        assert http.post(f"/stores/{STORE}/events", json=ev, headers=h).json()["moves"][0]["qty"] == 1
        assert http.post(f"/stores/{STORE}/events", json=ev, headers=h).json()["duplicate"] is True
        bad = http.post(f"/stores/{STORE}/events", json={"event_id": "x", "kind": "teleport", "batch_code": BATCH},
                        headers=h)
        assert bad.status_code == 400
        r = http.post(f"/admin/stores/{STORE}", json={"stock": {"on_shelf": 5}},
                      headers={"X-Admin-Token": key})
        assert r.status_code == 200 and r.json()["store"]["stock"]["on_shelf"] == 5
    assert _party(db)["held"] == 2
