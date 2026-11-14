"""The fallback form, GET/POST /form/{token}. Spec test 13: submitting 60 and
then valid counts through the form gives the same rejection and acceptance as
the voice path. The mass balance is asserted after every ledger write."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from levela import clock, runs
from levela.api.main import create_app
from levela.clock import Clock
from levela.db.seed import load_scenario, start_recall
from levela.ledger.transfers import stub_party
from levela.orchestrator import attempts

TOOL = {"X-Tool-Secret": "test-tool-secret"}
PARTY_FIELDS = ("shipped_in", "on_hand", "sold", "returned", "destroyed", "forwarded", "unresolved", "state")


@pytest.fixture
def api(settings, client):
    return TestClient(create_app(settings, client=client, sim_network=object()))


def start(client, db, recall_id=None):
    return start_recall(client, db, load_scenario("recall_1"), all_sim=True, recall_id=recall_id)["recall_id"]


def token(db, recall_id, party_id):
    return db.parties.find_one({"recall_id": recall_id, "party_id": party_id})["form_token"]


def dialed(db, recall_id, party_id, **context):
    att = attempts.open_attempt(db, recall_id=recall_id, party_id=party_id, channel="sim", context=context)
    attempts.mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CA{att['_id']}")
    return att


def submit(api, tok, **fields) -> str:
    r = api.post(f"/form/{tok}", data={k: str(v) for k, v in fields.items()})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    return r.text


def outcome(db, recall_id):
    """What a judge can compare between the two paths: the claims, Atlas's
    error, the booked party row and the feed lines."""
    rejected, accepted = db.claims.find({"recall_id": recall_id}).sort("created_at", 1)
    err = rejected["atlas_error"]
    party = db.parties.find_one({"recall_id": recall_id, "party_id": "harbour-fire"})
    lines = db.feed.find({"recall_id": recall_id, "kind": {"$in": ["claim_rejected", "claim_accepted"]}}).sort("at", 1)
    return {
        "claims": [(c["status"], c.get("code"), c.get("reject_reason"), c.get("reported_total"), c.get("shipped_in"))
                   for c in (rejected, accepted)],
        "atlas_error": (err["code"], err["codeName"], err["errmsg"], err["errInfo"]["details"]),
        "party": {k: party[k] for k in PARTY_FIELDS},
        "feed": [line["text"] for line in lines],
    }


def test_spec_13_form_gives_the_same_rejection_and_acceptance_as_voice(api, client, db, assert_balance):
    voice, web = start(client, db, "RCL-T13-VOICE"), start(client, db, "RCL-T13-FORM")

    att = dialed(db, voice, "harbour-fire", ask_delivery_note="no")
    for claim in ({"in_stock": 60}, {"in_stock": 35, "returned": 5}):
        r = api.post("/tools/submit_count", headers=TOOL,
                     json={"attempt_id": att["_id"], "party_id": "harbour-fire", **claim})
        assert r.status_code == 200
        assert_balance(voice)

    tok = token(db, web, "harbour-fire")
    page = submit(api, tok, in_stock=60)
    assert "Rejected by Atlas" in page and "<b>60</b>reported" in page and "<b>40</b>shipped" in page
    assert "121 DocumentValidationFailure: Document failed validation" in page
    assert "<details><summary>Raw Atlas error (errInfo)</summary><pre>" in page and "failingDocumentId" in page
    assert 'value="60"' in page                    # the form keeps what was typed, ready to correct
    assert_balance(web)
    page = submit(api, tok, in_stock=35, returned=5)
    assert "Accepted" in page and "Recorded: 25 on hand, 10 sold, 5 returned." in page
    assert "Still unexplained <b>0</b>" in page
    assert_balance(web)

    assert outcome(db, web) == outcome(db, voice)
    assert outcome(db, web)["feed"] == ["Harbour Fire - Rejected by Atlas: 60 reported, 40 shipped",
                                        "Harbour Fire - accepted: 25 on hand, 10 sold, 5 returned"]
    rejected = db.claims.find_one({"recall_id": web, "status": "rejected"})
    assert db.feed.find_one({"recall_id": web, "kind": "claim_rejected"})["detail"] == rejected["atlas_error"]

    # Each submission is one form attempt, closed once handled, whatever came of it.
    form_atts = list(db.attempts.find({"recall_id": web}).sort("seq", 1))
    assert [(a["channel"], a["purpose"], a["dial_confirmed"], a["state"]) for a in form_atts] == [
        ("form", "form", True, "completed")] * 2
    assert all(a["connected_at"] and a["ended_at"] for a in form_atts)
    assert [a["outcome_reason"] for a in form_atts] == ["form claim rejected: over_count", "form claim accepted"]
    claims = list(db.claims.find({"recall_id": web}).sort("created_at", 1))
    assert [c["attempt_id"] for c in claims] == [a["_id"] for a in form_atts]
    assert {c["source"] for c in claims} == {"form"}
    assert db.feed.count_documents({"recall_id": web, "kind": "form_claim"}) == 2


def test_form_logs_its_evidence_and_verifies_the_parties_form_link(api, client, db, assert_balance):
    rid = start(client, db)
    t0 = datetime.now(timezone.utc)
    runs.set_clock(db, rid, Clock(real_start=t0, sim_start=105.0, paused_at=t0))      # d4 09:00, ladder step 3
    clock.forget()
    db.operations.insert_one({"_id": "op-link", "recall_id": rid, "kind": "send_form_link",
                              "party_id": "harbour-fire", "idempotency_key": f"{rid}:harbour-fire:form:3",
                              "state": "sent"})
    tok = token(db, rid, "harbour-fire")

    submit(api, tok, in_stock=60)                     # refused by Atlas: the link has not yet produced a count
    assert db.operations.find_one({"_id": "op-link"})["state"] == "sent"
    assert_balance(rid)
    submit(api, tok, in_stock=35, returned=5)
    assert_balance(rid)

    claim = db.claims.find_one({"recall_id": rid, "status": "accepted"})
    op = db.operations.find_one({"_id": "op-link"})
    assert (op["state"], op["readback_at"], op["readback_value"]) == ("verified", 105.0, claim["_id"])
    events = list(db.tool_events.find({"recall_id": rid}).sort("received_at", 1))
    claims = list(db.claims.find({"recall_id": rid}).sort("created_at", 1))
    assert [e["kind"] for e in events] == ["form", "form"]
    assert [e["attempt_id"] for e in events] == [c["attempt_id"] for c in claims]
    assert events[1]["attempt_id"] == claim["attempt_id"]
    assert events[1]["payload"] == {"in_stock": "35", "returned": "5"}
    line = db.feed.find_one({"kind": "form_link_verified"})
    assert (line["text"], line["system"], line["sim_label"]) == (
        "Harbour Fire: form link verified, a submission arrived with the party's link", "form", "d4 09:00")
    assert db.feed.find_one({"kind": "form_claim"})["system"] == "form"


def test_get_shows_the_recall_and_a_direct_party_fields(api, client, db):
    rid = start(client, db)
    r = api.get(f"/form/{token(db, rid, 'harbour-fire')}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    for text in ("Harbour Fire", "Maple Oat Granola Bars, panel of 6", "B-4471", "Undeclared peanut",
                 'name="viewport"', '<label for="on_hand">', 'name="sold"', 'name="returned"', 'name="destroyed"',
                 'name="forwarded_qty"', 'name="forwarded_to_name"', 'inputmode="numeric"', "<html lang=\"en\">"):
        assert text in page
    assert 'name="received_qty"' not in page and 'name="delivery_note"' not in page
    for external in ("<script", "<link", "http://", "https://"):
        assert external not in page


def test_form_uses_the_live_attempt_and_its_delivery_note_rule(api, client, db, assert_balance):
    rid = start(client, db)
    att = dialed(db, rid, "harbour-fire", ask_delivery_note="yes")
    tok = token(db, rid, "harbour-fire")
    assert 'name="delivery_note"' in api.get(f"/form/{tok}").text

    page = submit(api, tok, in_stock=35, returned=5)
    assert "Not recorded" in page and "delivery note number" in page
    assert db.claims.find_one({"recall_id": rid})["code"] == "delivery_note_required"
    assert_balance(rid)
    page = submit(api, tok, in_stock=35, returned=5, delivery_note="DN-48213")
    assert "Accepted" in page
    assert_balance(rid)

    assert {c["attempt_id"] for c in db.claims.find({"recall_id": rid})} == {att["_id"]}
    assert db.attempts.count_documents({"recall_id": rid, "party_id": "harbour-fire"}) == 1
    assert db.attempts.find_one({"_id": att["_id"]})["state"] == "dialed"   # the call is the worker's to close


def test_a_refused_form_attempt_falls_back_to_the_live_one(api, client, db, monkeypatch, assert_balance):
    rid = start(client, db)
    att = dialed(db, rid, "harbour-fire")
    monkeypatch.setattr(attempts, "live_attempt", lambda *a, **k: None)   # lose the race to the worker
    page = submit(api, token(db, rid, "harbour-fire"), on_hand=25, sold=10, returned=5)
    assert "Accepted" in page
    assert_balance(rid)
    assert db.claims.find_one({"recall_id": rid})["attempt_id"] == att["_id"]
    assert db.attempts.count_documents({"recall_id": rid, "party_id": "harbour-fire"}) == 1


def test_a_rejected_submission_does_not_hold_the_party(api, client, db, assert_balance):
    """A judge types 60 for Harbour Fire, sees the rejection and walks away.
    The form attempt is closed, so the party can still be called (a live one
    held its one-live-attempt slot for the rest of the run)."""
    rid = start(client, db)
    page = submit(api, token(db, rid, "harbour-fire"), on_hand=60)
    assert "Rejected by Atlas" in page
    assert_balance(rid)
    assert attempts.live_attempt(db, rid, "harbour-fire") is None
    [att] = db.attempts.find({"recall_id": rid})
    assert (att["channel"], att["state"], att["outcome_reason"]) == ("form", "completed",
                                                                     "form claim rejected: over_count")
    call = attempts.open_attempt(db, recall_id=rid, party_id="harbour-fire", channel="sim")
    assert call["seq"] == 2


def test_the_same_numbers_after_the_sender_reports_are_booked_not_the_stale_rejection(api, client, db,
                                                                                      assert_balance):
    rid = start(client, db)
    db.parties.insert_one(stub_party(rid, "corner-properties", "Corner Properties", None, "memory", datetime.now(timezone.utc)))
    tok = token(db, rid, "corner-properties")
    assert "No party has reported sending panels to you yet" in submit(api, tok, received_qty=25, in_stock=25)
    submit(api, token(db, rid, "queen-st-alarm"), on_hand=50, sold=30, forwarded_qty=40,
           forwarded_to_name="Corner Properties")
    page = submit(api, tok, received_qty=25, in_stock=25)
    assert "Accepted" in page and "already checked and refused" not in page
    assert db.edges.find_one({"recall_id": rid})["state"] == "disputed"
    assert_balance(rid)


def test_a_submission_that_cannot_be_booked_is_refused_and_closes_its_attempt(api, client, db, monkeypatch,
                                                                             assert_balance):
    rid = start(client, db)
    tok = token(db, rid, "lakeside-systems")
    # A 20-digit count is a rejection the judge can read, not a server error.
    page = submit(api, tok, in_stock="99999999999999999999")
    assert "Not recorded" in page and "is not a count" in page
    claim = db.claims.find_one({"recall_id": rid})
    assert (claim["code"], claim["raw"]) == ("bad_number", {"in_stock": "99999999999999999999"})
    # A database failure while booking: a 503 page, and the attempt still closes.
    from levela.api import form as form_module

    def unreachable(*args, **kwargs):
        raise AutoReconnect("no primary")
    monkeypatch.setattr(form_module, "book_claim", unreachable)
    r = api.post(f"/form/{tok}", data={"in_stock": "20", "returned": "60"})
    assert r.status_code == 503 and "could not be recorded just now" in r.text and 'value="60"' in r.text
    assert attempts.live_attempt(db, rid, "lakeside-systems") is None
    reasons = [a["outcome_reason"] for a in db.attempts.find({"recall_id": rid}).sort("seq", 1)]
    assert reasons == ["form claim rejected: bad_number", "form submission not recorded"]
    assert_balance(rid)


def test_a_party_memory_flagged_must_give_its_delivery_note_on_the_form(api, client, db, assert_balance):
    """No call is live, so no call context asks for the note; memory's flag
    on the party does (DESIGN §10.4)."""
    rid = start(client, db)
    db.parties.update_one({"recall_id": rid, "party_id": "harbour-fire"}, {"$set": {"memory": {"actions": [
        {"action": "ask_delivery_note", "episode_id": "ep-1", "from_party": "harbour-fire"}]}}})
    tok = token(db, rid, "harbour-fire")
    assert 'name="delivery_note"' in api.get(f"/form/{tok}").text
    page = submit(api, tok, in_stock=35, returned=5)
    assert "Not recorded" in page and "delivery note number" in page
    assert db.claims.find_one({"recall_id": rid})["code"] == "delivery_note_required"
    assert "Accepted" in submit(api, tok, in_stock=35, returned=5, delivery_note="DN-48213")
    assert_balance(rid)


def test_a_direct_party_that_received_a_transfer_can_confirm_it(api, client, db, assert_balance):
    rid = start(client, db)
    submit(api, token(db, rid, "queen-st-alarm"), on_hand=80, sold=30, forwarded_qty=10,
           forwarded_to_name="Lakeside Systems")
    tok = token(db, rid, "lakeside-systems")
    page = api.get(f"/form/{tok}").text
    assert 'name="received_qty"' in page and "From Queen St Alarm" in page
    page = submit(api, tok, received_qty=10, in_stock=30, returned=60)
    assert "Accepted" in page and "Recorded: received 10, 30 on hand, 60 destroyed." in page
    lake = db.parties.find_one({"recall_id": rid, "party_id": "lakeside-systems"})
    assert (lake["shipped_in"], lake["unresolved"], lake["state"]) == (90, 0, "reconciled")
    assert db.edges.find_one({"recall_id": rid})["state"] == "corroborated"
    assert_balance(rid)


def test_a_party_two_senders_shipped_to_picks_the_sender(api, client, db, assert_balance):
    rid = start(client, db)
    submit(api, token(db, rid, "queen-st-alarm"), on_hand=50, sold=45, forwarded_qty=25,
           forwarded_to_name="Corner Properties")
    submit(api, token(db, rid, "harbour-fire"), on_hand=20, sold=10, forwarded_qty=10,
           forwarded_to_name="Corner Properties")
    tok = token(db, rid, "corner-properties")
    page = api.get(f"/form/{tok}").text
    assert '<select id="received_from" name="received_from">' in page
    assert '<option value="Harbour Fire">' in page and '<option value="Queen St Alarm">' in page
    page = submit(api, tok, received_qty=25, in_stock=25)
    assert "Panels reached you from more than one party" in page
    page = submit(api, tok, received_from="Queen St Alarm", received_qty=25, in_stock=25)
    assert "Accepted" in page and "Recorded: received 25 from Queen St Alarm, 25 on hand." in page
    page = submit(api, tok, received_from="Harbour Fire", received_qty=10, in_stock=35)
    assert "Accepted" in page and '<option value="Harbour Fire" selected>' in page
    cm = db.parties.find_one({"recall_id": rid, "party_id": "corner-properties"})
    assert (cm["shipped_in"], cm["unresolved"], cm["state"]) == (35, 0, "reconciled")
    assert_balance(rid)


def test_downstream_form_asks_for_the_receipt_and_books_the_dispute(api, client, db, assert_balance):
    rid = start(client, db)
    page = submit(api, token(db, rid, "queen-st-alarm"), on_hand=50, sold=30, forwarded_qty=40,
                  forwarded_to_name="Corner Properties")
    assert "Recorded: 50 on hand, 30 sold, 40 forwarded to Corner Properties." in page
    assert_balance(rid)

    tok = token(db, rid, "corner-properties")
    page = api.get(f"/form/{tok}").text
    assert 'name="received_qty"' in page and "From Queen St Alarm" in page
    page = submit(api, tok, received_qty=25, in_stock=25)
    assert "Accepted" in page and "Recorded: received 25, 25 on hand." in page
    assert_balance(rid)
    edge = db.edges.find_one({"recall_id": rid})
    assert (edge["state"], edge["disputed_qty"]) == ("disputed", 15)
    assert db.feed.find_one({"kind": "edge_disputed"})["text"] == (
        "Transfer disputed: Queen St Alarm says 40, Corner Properties says 25 - 15 panels disputed")


def test_party_names_are_escaped(api, client, db, assert_balance):
    rid = start(client, db)
    submit(api, token(db, rid, "queen-st-alarm"), on_hand=50, sold=30, forwarded_qty=40,
           forwarded_to_name='<b>Evil</b> & "Co"')
    assert_balance(rid)
    stub = db.parties.find_one({"recall_id": rid, "tier": "downstream"})
    page = api.get(f"/form/{stub['form_token']}").text
    assert "<b>Evil</b>" not in page and "&lt;b&gt;Evil&lt;/b&gt; &amp; &quot;Co&quot;" in page


def test_blank_or_oversized_submissions_book_nothing(api, client, db):
    rid = start(client, db)
    tok = token(db, rid, "harbour-fire")
    assert "Enter at least one number." in submit(api, tok, on_hand="", sold="  ")
    big = api.post(f"/form/{tok}", content=b"on_hand=" + b"9" * 20000,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert big.status_code == 413 and "That submission is too large." in big.text
    assert db.attempts.count_documents({}) == 0 and db.claims.count_documents({}) == 0


def test_closed_recall_shows_a_notice_and_takes_no_claims(api, client, db):
    rid = start(client, db)
    db.recalls.update_one({"_id": rid}, {"$set": {"status": "closed", "open_units": 0}})
    tok = token(db, rid, "harbour-fire")
    for page in (api.get(f"/form/{tok}").text, submit(api, tok, in_stock=25)):
        assert "This recall is closed" in page and "<form" not in page
    assert db.attempts.count_documents({}) == 0


def test_unknown_link_and_the_qr_code(api, client, db):
    rid = start(client, db)
    assert api.get("/form/nope").status_code == 404
    assert api.post("/form/nope", data={"in_stock": "1"}).status_code == 404
    assert api.get("/form/nope/qr.svg").status_code == 404
    r = api.get(f"/form/{token(db, rid, 'harbour-fire')}/qr.svg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
    assert r.text.lstrip().startswith("<svg")
