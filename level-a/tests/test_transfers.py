"""Spec test 4 (transfers) and V2 §9 (multi-destination forwards, wakes).

Sender reports 40 forwarded, recipient reports 25: the edge shows 25
corroborated and 15 disputed, the recipient's shipped_in is 25, and the global
equation equals 240 after every write. Plus: one edge per destination, what a
later claim may and may not change (a transfer the recipient has not counted
can be withdrawn, with the stub it created), a party two senders shipped to,
stubs from directory entries (order system accounts, installers at tier 2),
an installer's installs booked as a transfer to the building owner, and the
counterpart's timer set to the recall's simulated now inside the claim
transaction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levela.clock import get_clock, server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import CHALLENGE_CODES, submit_claim
from levela.ledger.transfers import stub_party
from support import PRODUCT

RID = "RCL-2026-017"
LONG = Path(__file__).parent / "fixtures" / "recall_long_mini.json"
LONG_RID = "RCL-LONG-MINI"


@pytest.fixture
def recall(client, db, assert_balance):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, emit=False)
    assert_balance(RID)
    return RID


@pytest.fixture
def long_recall(client, db, assert_balance):
    start_recall(client, db, load_scenario(LONG), all_sim=True, emit=False)
    assert_balance(LONG_RID)
    return LONG_RID


def claim(client, db, party_id, attempt, *, rid=RID, source="sim", **raw):
    return submit_claim(client, db, recall_id=rid, party_id=party_id, attempt_id=attempt, source=source, raw=raw)


def party(db, party_id, rid=RID):
    return db.parties.find_one({"recall_id": rid, "party_id": party_id})


def edge(db, from_party, to_party, rid=RID):
    return db.edges.find_one({"recall_id": rid, "from_party": from_party, "to_party": to_party}, {"_id": 0})


def numbers(e):
    return {k: e[k] for k in ("sender_qty", "recipient_qty", "corroborated_qty", "disputed_qty", "state")}


def set_timer(db, party_id, at, rid=RID, **extra):
    db.parties.update_one({"recall_id": rid, "party_id": party_id}, {"$set": {"next_action_at": at, **extra}})


def sim_now(client, db, rid=RID):
    return get_clock(db, rid).sim_now(server_now(client))


# ---- spec test 4 ----------------------------------------------------------------

def test_4_forward_then_receipt_puts_the_gap_on_the_edge(client, db, recall, assert_balance):
    sent = claim(client, db, "queen-st-alarm", "att_q1", in_stock=80, forwarded_qty=40,
                 forwarded_to_name="the corner properties")
    assert sent["accepted"] and sent["discovered"] == "corner-properties" and sent["open_units"] == 160
    assert sent["edge"]["before"] is None and sent["edge"]["state"] == "awaiting_recipient"
    assert_balance(RID)
    cm = party(db, "corner-properties")
    assert (cm["name"], cm["type"], cm["tier"], cm["shipped_in"], cm["state"]) == (
        "Corner Properties", "owner", 3, 0, "pending")
    assert (cm["found_via"], cm["discovered_by"], cm["slice"]) == ("claim", "claim:queen-st-alarm", True)
    assert (cm["model"], cm["affected_versions"], cm["fixed_by_panel"]) == ("DEMO-7", ["2.3", "2.4"], 0)
    assert cm["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)   # due at once
    assert numbers(edge(db, "queen-st-alarm", "corner-properties")) == {
        "sender_qty": 40, "recipient_qty": None, "corroborated_qty": 0, "disputed_qty": 0,
        "state": "awaiting_recipient"}

    set_timer(db, "queen-st-alarm", 100.0)          # the engine parked the sender after its claim
    got = claim(client, db, "corner-properties", "att_cm1", received_qty=25, in_stock=25)
    assert got["accepted"] and got["open_units"] == 135 and got["woken"] == ["queen-st-alarm"]
    assert (got["edge"]["before"], got["edge"]["state"]) == ("awaiting_recipient", "disputed")
    assert_balance(RID)
    assert numbers(edge(db, "queen-st-alarm", "corner-properties")) == {
        "sender_qty": 40, "recipient_qty": 25, "corroborated_qty": 25, "disputed_qty": 15, "state": "disputed"}
    cm = party(db, "corner-properties")
    assert (cm["shipped_in"], cm["in_stock"], cm["unresolved"], cm["state"]) == (25, 25, 0, "claimed")
    queen = party(db, "queen-st-alarm")
    assert queen["state"] == "claimed" and queen["forwarded"] == 40
    assert queen["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)

    # The sender's follow-up corrects to 25. The recipient is reconciled, but an
    # open question still needs it, so it is woken too.
    set_timer(db, "corner-properties", None, open_questions=["q-02"])
    fixed = claim(client, db, "queen-st-alarm", "att_q2", in_stock=95, forwarded_qty=25,
                  forwarded_to_name="Corner Properties")
    assert fixed["accepted"] and fixed["open_units"] == 120 and fixed["woken"] == ["corner-properties"]
    assert_balance(RID)
    assert numbers(edge(db, "queen-st-alarm", "corner-properties"))["state"] == "corroborated"
    assert [party(db, s)["state"] for s in ("queen-st-alarm", "corner-properties")] == ["reconciled", "reconciled"]
    assert party(db, "corner-properties")["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)


def test_a_reconciled_counterpart_sleeps_and_a_changed_sender_wakes_the_recipient(client, db, recall,
                                                                                   assert_balance):
    claim(client, db, "queen-st-alarm", "att_q1", in_stock=80, forwarded_qty=40,
          forwarded_to_name="Corner Properties")
    set_timer(db, "queen-st-alarm", None)
    got = claim(client, db, "corner-properties", "att_cm1", received_qty=40, in_stock=40)
    assert got["accepted"] and "woken" not in got        # the sender is reconciled: nothing to do
    assert party(db, "queen-st-alarm")["next_action_at"] is None
    assert_balance(RID)

    set_timer(db, "corner-properties", None)
    more = claim(client, db, "queen-st-alarm", "att_q2", in_stock=75, forwarded_qty=45,
                 forwarded_to_name="Corner Properties")
    assert more["accepted"] and more["woken"] == ["corner-properties"]
    assert numbers(edge(db, "queen-st-alarm", "corner-properties")) == {
        "sender_qty": 45, "recipient_qty": 40, "corroborated_qty": 40, "disputed_qty": 5, "state": "disputed"}
    assert party(db, "corner-properties")["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)
    assert_balance(RID)

    # Lowering the transfer below what the party already documented would make
    # its unresolved negative: Atlas refuses and nothing changes.
    before = numbers(edge(db, "queen-st-alarm", "corner-properties"))
    low = claim(client, db, "queen-st-alarm", "att_q3", in_stock=90, forwarded_qty=30,
                forwarded_to_name="Corner Properties")
    assert not low["accepted"] and low["code"] == "recipient_overdocumented"
    assert low["atlas_error"]["code"] == 121
    assert numbers(edge(db, "queen-st-alarm", "corner-properties")) == before
    assert party(db, "corner-properties")["shipped_in"] == 40
    assert_balance(RID)


def test_a_receipt_needs_a_sender_first(client, db, recall, assert_balance):
    # Memory suspected Corner Properties; it answers before Queen St Alarm has.
    db.parties.insert_one(stub_party(RID, "corner-properties", "Corner Properties", None, "memory", server_now(client),
                                     product=PRODUCT))
    early = claim(client, db, "corner-properties", "att_cm0", received_qty=25, in_stock=25)
    assert not early["accepted"] and early["code"] == "no_inbound_edge"
    assert party(db, "corner-properties")["shipped_in"] == 0
    assert_balance(RID)


def test_a_direct_party_receiving_a_transfer_keeps_its_own_shipment(client, db, recall, assert_balance):
    set_timer(db, "lakeside-systems", 5.0)           # already due: an earlier timer stands
    sent = claim(client, db, "queen-st-alarm", "att_q1", in_stock=110, forwarded_qty=10,
                 forwarded_to_name="Lakeside Systems")
    assert sent["accepted"] and "discovered" not in sent and sent["woken"] == ["lakeside-systems"]
    assert party(db, "lakeside-systems")["next_action_at"] == 5.0
    assert_balance(RID)
    got = claim(client, db, "lakeside-systems", "att_l1", received_qty=10, in_stock=30, returned=60)
    assert got["accepted"]
    lake = party(db, "lakeside-systems")
    assert (lake["shipped_in"], lake["unresolved"], lake["state"]) == (90, 0, "reconciled")
    assert_balance(RID)


# ---- V2: several destinations -------------------------------------------------------

def test_one_edge_per_destination_and_what_a_later_claim_may_change(client, db, recall, assert_balance):
    first = claim(client, db, "queen-st-alarm", "att_q1", in_stock=80, forwards=[{"to_name": "Corner Properties", "qty": 30}, {"to_name": "Bloor Alarm", "qty": 10}])
    assert first["accepted"] and first["discovered_parties"] == ["corner-properties", "bloor-alarm"]
    assert sorted((e["to_party"], e["sender_qty"]) for e in first["edges"]) == [("bloor-alarm", 10),
                                                                                 ("corner-properties", 30)]
    assert party(db, "queen-st-alarm")["forwarded"] == 40
    bloor = party(db, "bloor-alarm")
    assert (bloor["name"], bloor["channel"], bloor["tier"], bloor["found_via"]) == ("Bloor Alarm", "form", 3, "claim")
    assert_balance(RID)

    # Change a quantity (by party id this time), keep a party, add a party, and
    # name one party twice: its two counts are one transfer.
    later = claim(client, db, "queen-st-alarm", "att_q2", in_stock=75, forwards=[{"to_party_id": "corner-properties", "qty": 25}, {"to_name": "bloor alarm", "qty": 10},
                            {"to_name": "Parkdale Court", "qty": 6}, {"to_name": "Parkdale Court", "qty": 4}])
    assert later["accepted"] and later["discovered_parties"] == ["parkdale-court"]
    assert {(e["to_party"], e["before"]) for e in later["edges"]} == {("parkdale-court", None),
                                                                      ("corner-properties", "awaiting_recipient")}
    sent = {e["to_party"]: e["sender_qty"] for e in db.edges.find({"recall_id": RID})}
    assert sent == {"corner-properties": 25, "bloor-alarm": 10, "parkdale-court": 10}
    assert party(db, "queen-st-alarm")["forwarded"] == 45
    assert_balance(RID)

    # Once Bloor Alarm has counted its panels, dropping it is refused, and so is
    # restating no transfer at all: its count is booked against the edge.
    assert claim(client, db, "bloor-alarm", "att_b1", received_qty=10, in_stock=10)["accepted"]
    assert_balance(RID)
    dropped = claim(client, db, "queen-st-alarm", "att_q3", in_stock=85, forwards=[{"to_name": "Corner Properties", "qty": 25}, {"to_name": "Parkdale Court", "qty": 10}])
    assert not dropped["accepted"] and dropped["code"] == "forward_changed"
    assert "Bloor Alarm" in dropped["reason"] and "end the call" in dropped["message"]
    none = claim(client, db, "queen-st-alarm", "att_q4", in_stock=120)
    assert none["code"] == "forward_restate" and "Bloor Alarm" in none["reason"]
    assert party(db, "queen-st-alarm")["forwarded"] == 45
    assert db.edges.count_documents({"recall_id": RID}) == 3
    assert_balance(RID)


def test_a_transfer_its_recipient_has_not_counted_can_be_withdrawn(client, db, recall, assert_balance):
    """The judge playing Harbour Fire lies ("10 went to Mike's Market"), then
    tells the truth. Claims are absolute: the phantom transfer and the stub it
    created go in the same transaction, instead of every later claim being
    refused and the phantom panels staying open for good."""
    lie = claim(client, db, "harbour-fire", "att_h1", in_stock=30, forwarded_qty=10,
                forwarded_to_name="Mike's Market")
    stub = lie["discovered"]
    assert lie["accepted"] and lie["open_units"] == 210 and party(db, stub)["discovered_by"] == "claim:harbour-fire"
    assert_balance(RID)

    truth = claim(client, db, "harbour-fire", "att_h1", in_stock=40)
    assert truth["accepted"] and truth["open_units"] == 200 and truth["removed_parties"] == [stub]
    assert (truth["edge"]["state"], truth["edge"]["before"], truth["edge"]["to_name"]) == (
        "withdrawn", "awaiting_recipient", "Mike's Market")
    assert db.edges.count_documents({"recall_id": RID}) == 0 and party(db, stub) is None
    harbour = party(db, "harbour-fire")
    assert (harbour["in_stock"], harbour["forwarded"], harbour["unresolved"], harbour["state"]) == (
        40, 0, 0, "reconciled")
    assert db.recalls.find_one({"_id": RID})["open_units"] == 200
    assert_balance(RID)

    # Named again later, the party comes back as a fresh stub.
    again = claim(client, db, "harbour-fire", "att_h2", in_stock=35, forwarded_qty=5,
                  forwarded_to_name="Mike's Market")
    assert again["accepted"] and again["discovered"] == stub and party(db, stub)["state"] == "pending"
    assert_balance(RID)


def test_withdrawing_a_destination_keeps_parties_that_are_still_named(client, db, recall, assert_balance):
    claim(client, db, "queen-st-alarm", "att_q1", in_stock=90, forwards=[{"to_name": "Corner Properties", "qty": 20}, {"to_name": "Lakeside Systems", "qty": 10}])
    claim(client, db, "harbour-fire", "att_h1", in_stock=30, forwarded_qty=10, forwarded_to_name="Corner Properties")
    assert_balance(RID)

    # A quantity of 0 withdraws the party too. Lakeside Systems is a shipment
    # party, not a stub this claim created, so it stays.
    zero = claim(client, db, "queen-st-alarm", "att_q2", in_stock=100, forwards=[{"to_name": "Corner Properties", "qty": 20}, {"to_name": "Lakeside Systems", "qty": 0}])
    assert zero["accepted"] and "removed_parties" not in zero
    assert [(e["to_party"], e["state"], e["removed"]) for e in zero["edges"]] == [
        ("lakeside-systems", "withdrawn", False)]
    assert party(db, "lakeside-systems")["shipped_in"] == 80
    assert_balance(RID)

    # Harbour Fire withdraws Corner Properties, which Queen St Alarm's claim
    # created and still names: the edge goes, the party stays.
    gone = claim(client, db, "harbour-fire", "att_h2", in_stock=40)
    assert gone["accepted"] and "removed_parties" not in gone and party(db, "corner-properties") is not None
    assert {(e["from_party"], e["to_party"]) for e in db.edges.find({"recall_id": RID})} == {
        ("queen-st-alarm", "corner-properties")}
    assert_balance(RID)


def test_a_party_two_senders_shipped_to_names_the_sender_and_counts_both(client, db, recall, assert_balance):
    """Corner Properties received 25 from Queen St Alarm and 10 from Harbour
    Market. Its receipt must say which sender it confirms, and its books
    count both transfers: booking whichever edge sorted first refused its
    true count, and counting one edge dropped the other's panels."""
    claim(client, db, "queen-st-alarm", "att_q1", in_stock=95, forwarded_qty=25,
          forwarded_to_name="Corner Properties")
    assert claim(client, db, "corner-properties", "att_cm1", received_qty=25, in_stock=25)["accepted"]   # one sender
    claim(client, db, "harbour-fire", "att_h1", in_stock=30, forwarded_qty=10,
          forwarded_to_name="Corner Properties")
    assert_balance(RID)

    vague = claim(client, db, "corner-properties", "att_cm2", received_qty=25, in_stock=25)
    assert not vague["accepted"] and vague["code"] == "receipt_sender_ambiguous"
    assert "Queen St Alarm" in vague["reason"] and "Harbour Fire" in vague["reason"]
    assert vague["code"] not in CHALLENGE_CODES and "end the call" in vague["message"]
    wrong = claim(client, db, "corner-properties", "att_cm2", received_qty=10, received_from="Bloor Alarm", in_stock=35)
    assert wrong["code"] == "receipt_sender_ambiguous" and "Bloor Alarm" in wrong["reason"]
    assert numbers(edge(db, "harbour-fire", "corner-properties"))["state"] == "awaiting_recipient"
    assert party(db, "corner-properties")["shipped_in"] == 25
    assert_balance(RID)

    got = claim(client, db, "corner-properties", "att_cm2", received_qty=10, received_from="harbour fire", in_stock=35)
    assert got["accepted"] and got["edge"]["from_party"] == "harbour-fire"
    assert numbers(edge(db, "harbour-fire", "corner-properties"))["state"] == "corroborated"
    assert numbers(edge(db, "queen-st-alarm", "corner-properties"))["state"] == "corroborated"
    cm = party(db, "corner-properties")
    assert (cm["shipped_in"], cm["unresolved"], cm["state"]) == (35, 0, "reconciled")
    assert_balance(RID)

    # One sender's correction re-books the party from every inbound edge.
    more = claim(client, db, "queen-st-alarm", "att_q2", in_stock=90, forwarded_qty=30,
                 forwarded_to_name="Corner Properties")
    assert more["accepted"]
    assert numbers(edge(db, "queen-st-alarm", "corner-properties"))["disputed_qty"] == 5
    assert party(db, "corner-properties")["shipped_in"] == 35
    assert_balance(RID)


def test_forwarding_to_itself_or_to_nobody_is_refused(client, db, recall, assert_balance):
    selfish = claim(client, db, "queen-st-alarm", "att_q1", in_stock=80,
                    forwards=[{"to_name": "Queen St Alarm", "qty": 40}])
    assert selfish["code"] == "forward_self"
    nameless = claim(client, db, "queen-st-alarm", "att_q2", in_stock=80, forwarded_qty=40)
    assert nameless["code"] == "forward_missing_name"
    assert db.edges.count_documents({}) == 0 and party(db, "queen-st-alarm")["forwarded"] == 0
    assert_balance(RID)


def test_a_negative_transfer_is_refused_by_atlas_and_leaves_no_stub(client, db, recall, assert_balance):
    res = claim(client, db, "queen-st-alarm", "att_q1", in_stock=80,
                forwards=[{"to_name": "Corner Properties", "qty": 50}, {"to_name": "Bloor Alarm", "qty": -10}])
    assert not res["accepted"] and res["code"] == "negative" and res["atlas_error"]["code"] == 121
    assert db.parties.count_documents({"recall_id": RID}) == 3 and db.edges.count_documents({}) == 0
    assert_balance(RID)


# ---- V2: order system accounts and the directory --------------------------------------------

def test_a_distributors_order_system_claim_opens_one_edge_per_account(client, db, long_recall, assert_balance):
    res = claim(client, db, "dist-north", "op-accounts-1", rid=LONG_RID, source="order_system",
                forwards=[{"to_party_id": "bloor-alarm", "qty": 120}, {"to_party_id": "annex-fire", "qty": 180}])
    assert res["accepted"] and res["discovered_parties"] == ["bloor-alarm", "annex-fire"]
    assert_balance(LONG_RID)
    dist = party(db, "dist-north", LONG_RID)
    assert (dist["forwarded"], dist["unresolved"], dist["channel"], dist["order_system"]) == (300, 0, "order_system", "north")
    bloor = party(db, "bloor-alarm", LONG_RID)
    assert (bloor["type"], bloor["tier"], bloor["found_via"], bloor["discovered_by"]) == (
        "installer", 2, "order_system", "order_system:dist-north")
    assert (bloor["account"], bloor["distributor"], bloor["channel"], bloor["phone"]) == (
        "N-014", "dist-north", "sim", "+14165550127")
    assert bloor["next_action_at"] == pytest.approx(sim_now(client, db, LONG_RID), abs=0.5)
    assert db.tool_events.count_documents({"attempt_id": "op-accounts-1", "kind": "order_system"}) == 1

    # An account confirms its delivery and installs panels at a building
    # owner's site: a transfer to a tier 3 owner. The distributor still waits
    # on the other account, so it is woken.
    set_timer(db, "dist-north", None, rid=LONG_RID)
    got = claim(client, db, "annex-fire", "att_a1", rid=LONG_RID, received_qty=180, in_stock=100, forwards=[{"to_name": "Dundas Lofts", "qty": 80}])
    assert got["accepted"] and got["discovered"] == "dundas-lofts" and got["woken"] == ["dist-north"]
    lofts = party(db, "dundas-lofts", LONG_RID)
    assert (lofts["type"], lofts["tier"], lofts["found_via"], lofts["channel"]) == ("owner", 3, "claim", "sim")
    annex = party(db, "annex-fire", LONG_RID)
    assert (annex["shipped_in"], annex["forwarded"], annex["unresolved"]) == (180, 80, 0)
    assert party(db, "dist-north", LONG_RID)["state"] == "claimed"
    assert_balance(LONG_RID)


def test_an_installers_installs_are_a_transfer_and_the_owner_books_them_installed(client, db, recall, assert_balance):
    """Queen St Alarm installed 40 at Corner Properties: its `forwarded`, an
    edge, and a building owner that books its panels installed. Installed
    panels stay open (awaiting fix), so the owner is claimed, not reconciled."""
    sent = claim(client, db, "queen-st-alarm", "att_q1", in_stock=50, returned=30, forwarded_qty=40,
                 forwarded_to_name="Corner Properties")
    assert sent["accepted"] and party(db, "queen-st-alarm")["forwarded"] == 40
    got = claim(client, db, "corner-properties", "att_cm1", received_qty=40, installed=40, version_seen="2.4",
                sounder_test=False)
    assert got["accepted"] and numbers(edge(db, "queen-st-alarm", "corner-properties"))["state"] == "corroborated"
    cm = party(db, "corner-properties")
    assert (cm["shipped_in"], cm["installed"], cm["unresolved"], cm["state"]) == (40, 40, 0, "claimed")
    assert party(db, "queen-st-alarm")["state"] == "reconciled"
    assert got["open_units"] == 160          # Harbour 40 and Lakeside 80 not called, 40 installed
    assert_balance(RID)
