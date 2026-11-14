"""Spec test 4 (transfers) and V2 §9 (multi-destination forwards, wakes).

The sender passes 40 to Corner Mart, the recipient reports 25: the edge shows
25 corroborated and 15 disputed, the recipient's shipped_in is 25, and the
global equation equals 240 after every write. Plus: one edge per destination,
what a later claim may and may not change (a transfer the recipient has not
counted can be withdrawn, with the stub it created), a party two senders
shipped to, stubs from directory entries (order system accounts, stores at
tier 2), a store's tins passed to another store booked as a transfer and on
the second store's shelf, and the counterpart's timer set to the recall's
simulated now inside the claim transaction.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levela.clock import get_clock, server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import CHALLENGE_CODES, submit_claim
from levela.ledger.transfers import stub_party
from support import HELD_EVIDENCE as HELD
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

def test_spec_4_forward_then_receipt_puts_the_gap_on_the_edge(client, db, recall, assert_balance):
    sent = claim(client, db, "queen-st-grocer", "att_q1", held=80, **HELD, forwarded_qty=40,
                 forwarded_to_name="the corner mart")
    assert sent["accepted"] and sent["discovered"] == "corner-mart" and sent["open_units"] == 160
    assert sent["edge"]["before"] is None and sent["edge"]["state"] == "awaiting_recipient"
    assert_balance(RID)
    cm = party(db, "corner-mart")
    assert (cm["name"], cm["type"], cm["tier"], cm["shipped_in"], cm["state"]) == (
        "Corner Mart", "store", 3, 0, "pending")
    assert (cm["found_via"], cm["discovered_by"], cm["slice"]) == ("claim", "claim:queen-st-grocer", True)
    assert (cm["batch_code"], cm["held_by_system"], cm["sold_by_till"], cm["system_on_shelf"]) == ("B-4471", 0, 0, 0)
    assert cm["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)   # due at once
    assert numbers(edge(db, "queen-st-grocer", "corner-mart")) == {
        "sender_qty": 40, "recipient_qty": None, "corroborated_qty": 0, "disputed_qty": 0,
        "state": "awaiting_recipient"}

    set_timer(db, "queen-st-grocer", 100.0)          # the engine parked the sender after its claim
    got = claim(client, db, "corner-mart", "att_cm1", received_qty=25, held=25, **HELD)
    assert got["accepted"] and got["open_units"] == 135 and got["woken"] == ["queen-st-grocer"]
    assert (got["edge"]["before"], got["edge"]["state"]) == ("awaiting_recipient", "disputed")
    assert_balance(RID)
    assert numbers(edge(db, "queen-st-grocer", "corner-mart")) == {
        "sender_qty": 40, "recipient_qty": 25, "corroborated_qty": 25, "disputed_qty": 15, "state": "disputed"}
    cm = party(db, "corner-mart")
    assert (cm["shipped_in"], cm["held"], cm["unresolved"], cm["state"]) == (25, 25, 0, "claimed")
    queen = party(db, "queen-st-grocer")
    assert queen["state"] == "claimed" and queen["forwarded"] == 40
    assert queen["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)

    # The sender's follow-up corrects to 25. The recipient is reconciled, but an
    # open question still needs it, so it is woken too.
    set_timer(db, "corner-mart", None, open_questions=["q-02"])
    corrected = claim(client, db, "queen-st-grocer", "att_q2", held=95, **HELD, forwarded_qty=25,
                  forwarded_to_name="Corner Mart")
    assert corrected["accepted"] and corrected["open_units"] == 120 and corrected["woken"] == ["corner-mart"]
    assert_balance(RID)
    assert numbers(edge(db, "queen-st-grocer", "corner-mart"))["state"] == "corroborated"
    assert [party(db, s)["state"] for s in ("queen-st-grocer", "corner-mart")] == ["reconciled", "reconciled"]
    assert party(db, "corner-mart")["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)


def test_a_reconciled_counterpart_sleeps_and_a_changed_sender_wakes_the_recipient(client, db, recall,
                                                                                   assert_balance):
    claim(client, db, "queen-st-grocer", "att_q1", held=80, **HELD, forwarded_qty=40,
          forwarded_to_name="Corner Mart")
    set_timer(db, "queen-st-grocer", None)
    got = claim(client, db, "corner-mart", "att_cm1", received_qty=40, held=40, **HELD)
    assert got["accepted"] and "woken" not in got        # the sender is reconciled: nothing to do
    assert party(db, "queen-st-grocer")["next_action_at"] is None
    assert_balance(RID)

    set_timer(db, "corner-mart", None)
    more = claim(client, db, "queen-st-grocer", "att_q2", held=75, **HELD, forwarded_qty=45,
                 forwarded_to_name="Corner Mart")
    assert more["accepted"] and more["woken"] == ["corner-mart"]
    assert numbers(edge(db, "queen-st-grocer", "corner-mart")) == {
        "sender_qty": 45, "recipient_qty": 40, "corroborated_qty": 40, "disputed_qty": 5, "state": "disputed"}
    assert party(db, "corner-mart")["next_action_at"] == pytest.approx(sim_now(client, db), abs=0.5)
    assert_balance(RID)

    # Lowering the transfer below what the party already documented would make
    # its unresolved negative: Atlas refuses and nothing changes.
    before = numbers(edge(db, "queen-st-grocer", "corner-mart"))
    low = claim(client, db, "queen-st-grocer", "att_q3", held=90, **HELD, forwarded_qty=30,
                forwarded_to_name="Corner Mart")
    assert not low["accepted"] and low["code"] == "recipient_overdocumented"
    assert low["atlas_error"]["code"] == 121
    assert numbers(edge(db, "queen-st-grocer", "corner-mart")) == before
    assert party(db, "corner-mart")["shipped_in"] == 40
    assert_balance(RID)


def test_a_receipt_needs_a_sender_first(client, db, recall, assert_balance):
    # Memory suspected Corner Mart; it answers before Queen St Grocer has.
    db.parties.insert_one(stub_party(RID, "corner-mart", "Corner Mart", None, "memory", server_now(client),
                                     product=PRODUCT))
    early = claim(client, db, "corner-mart", "att_cm0", received_qty=25, held=25, **HELD)
    assert not early["accepted"] and early["code"] == "no_inbound_edge"
    assert party(db, "corner-mart")["shipped_in"] == 0
    assert_balance(RID)


def test_a_direct_party_receiving_a_transfer_keeps_its_own_shipment(client, db, recall, assert_balance):
    set_timer(db, "lakeside-foods", 5.0)           # already due: an earlier timer stands
    sent = claim(client, db, "queen-st-grocer", "att_q1", held=110, **HELD, forwarded_qty=10,
                 forwarded_to_name="Lakeside Foods")
    assert sent["accepted"] and "discovered" not in sent and sent["woken"] == ["lakeside-foods"]
    assert party(db, "lakeside-foods")["next_action_at"] == 5.0
    assert_balance(RID)
    got = claim(client, db, "lakeside-foods", "att_l1", received_qty=10, held=30, **HELD, returned=60)
    assert got["accepted"]
    lake = party(db, "lakeside-foods")
    assert (lake["shipped_in"], lake["unresolved"], lake["state"]) == (90, 0, "reconciled")
    assert_balance(RID)


# ---- V2: several destinations -------------------------------------------------------

def test_one_edge_per_destination_and_what_a_later_claim_may_change(client, db, recall, assert_balance):
    first = claim(client, db, "queen-st-grocer", "att_q1", held=80, **HELD, forwards=[{"to_name": "Corner Mart", "qty": 30}, {"to_name": "Bloor Foods", "qty": 10}])
    assert first["accepted"] and first["discovered_parties"] == ["corner-mart", "bloor-foods"]
    assert sorted((e["to_party"], e["sender_qty"]) for e in first["edges"]) == [("bloor-foods", 10),
                                                                                 ("corner-mart", 30)]
    assert party(db, "queen-st-grocer")["forwarded"] == 40
    bloor = party(db, "bloor-foods")
    assert (bloor["name"], bloor["channel"], bloor["tier"], bloor["found_via"]) == ("Bloor Foods", "form", 3, "claim")
    assert_balance(RID)

    # Change a quantity (by party id this time), keep a party, add a party, and
    # name one party twice: its two counts are one transfer.
    later = claim(client, db, "queen-st-grocer", "att_q2", held=75, **HELD, forwards=[{"to_party_id": "corner-mart", "qty": 25}, {"to_name": "bloor foods", "qty": 10},
                            {"to_name": "Parkdale Pantry", "qty": 6}, {"to_name": "Parkdale Pantry", "qty": 4}])
    assert later["accepted"] and later["discovered_parties"] == ["parkdale-pantry"]
    assert {(e["to_party"], e["before"]) for e in later["edges"]} == {("parkdale-pantry", None),
                                                                      ("corner-mart", "awaiting_recipient")}
    sent = {e["to_party"]: e["sender_qty"] for e in db.edges.find({"recall_id": RID})}
    assert sent == {"corner-mart": 25, "bloor-foods": 10, "parkdale-pantry": 10}
    assert party(db, "queen-st-grocer")["forwarded"] == 45
    assert_balance(RID)

    # Once Bloor Foods has counted its tins, dropping it is refused, and so is
    # restating no transfer at all: its count is booked against the edge.
    assert claim(client, db, "bloor-foods", "att_b1", received_qty=10, held=10, **HELD)["accepted"]
    assert_balance(RID)
    dropped = claim(client, db, "queen-st-grocer", "att_q3", held=85, **HELD, forwards=[{"to_name": "Corner Mart", "qty": 25}, {"to_name": "Parkdale Pantry", "qty": 10}])
    assert not dropped["accepted"] and dropped["code"] == "forward_changed"
    assert "Bloor Foods" in dropped["reason"] and "end the call" in dropped["message"]
    none = claim(client, db, "queen-st-grocer", "att_q4", held=120, **HELD)
    assert none["code"] == "forward_restate" and "Bloor Foods" in none["reason"]
    assert party(db, "queen-st-grocer")["forwarded"] == 45
    assert db.edges.count_documents({"recall_id": RID}) == 3
    assert_balance(RID)


def test_a_transfer_its_recipient_has_not_counted_can_be_withdrawn(client, db, recall, assert_balance):
    """The judge playing Harbour Market lies ("10 went to Mike's Market"), then
    tells the truth. Claims are absolute: the phantom transfer and the stub it
    created go in the same transaction, instead of every later claim being
    refused and the phantom tins staying open for good."""
    lie = claim(client, db, "harbour-market", "att_h1", held=30, **HELD, forwarded_qty=10,
                forwarded_to_name="Mike's Market")
    stub = lie["discovered"]
    assert lie["accepted"] and lie["open_units"] == 210 and party(db, stub)["discovered_by"] == "claim:harbour-market"
    assert_balance(RID)

    truth = claim(client, db, "harbour-market", "att_h1", held=40, **HELD)
    assert truth["accepted"] and truth["open_units"] == 200 and truth["removed_parties"] == [stub]
    assert (truth["edge"]["state"], truth["edge"]["before"], truth["edge"]["to_name"]) == (
        "withdrawn", "awaiting_recipient", "Mike's Market")
    assert db.edges.count_documents({"recall_id": RID}) == 0 and party(db, stub) is None
    harbour = party(db, "harbour-market")
    assert (harbour["held"], harbour["forwarded"], harbour["unresolved"], harbour["state"]) == (
        40, 0, 0, "reconciled")
    assert db.recalls.find_one({"_id": RID})["open_units"] == 200
    assert_balance(RID)

    # Named again later, the party comes back as a fresh stub.
    again = claim(client, db, "harbour-market", "att_h2", held=35, **HELD, forwarded_qty=5,
                  forwarded_to_name="Mike's Market")
    assert again["accepted"] and again["discovered"] == stub and party(db, stub)["state"] == "pending"
    assert_balance(RID)


def test_withdrawing_a_destination_keeps_parties_that_are_still_named(client, db, recall, assert_balance):
    claim(client, db, "queen-st-grocer", "att_q1", held=90, **HELD, forwards=[{"to_name": "Corner Mart", "qty": 20}, {"to_name": "Lakeside Foods", "qty": 10}])
    claim(client, db, "harbour-market", "att_h1", held=30, **HELD, forwarded_qty=10, forwarded_to_name="Corner Mart")
    assert_balance(RID)

    # A quantity of 0 withdraws the party too. Lakeside Foods is a shipment
    # party, not a stub this claim created, so it stays.
    zero = claim(client, db, "queen-st-grocer", "att_q2", held=100, **HELD, forwards=[{"to_name": "Corner Mart", "qty": 20}, {"to_name": "Lakeside Foods", "qty": 0}])
    assert zero["accepted"] and "removed_parties" not in zero
    assert [(e["to_party"], e["state"], e["removed"]) for e in zero["edges"]] == [
        ("lakeside-foods", "withdrawn", False)]
    assert party(db, "lakeside-foods")["shipped_in"] == 80
    assert_balance(RID)

    # Harbour Market withdraws Corner Mart, which Queen St Grocer's claim
    # created and still names: the edge goes, the party stays.
    gone = claim(client, db, "harbour-market", "att_h2", held=40, **HELD)
    assert gone["accepted"] and "removed_parties" not in gone and party(db, "corner-mart") is not None
    assert {(e["from_party"], e["to_party"]) for e in db.edges.find({"recall_id": RID})} == {
        ("queen-st-grocer", "corner-mart")}
    assert_balance(RID)


def test_a_party_two_senders_shipped_to_names_the_sender_and_counts_both(client, db, recall, assert_balance):
    """Corner Mart received 25 from Queen St Grocer and 10 from Harbour
    Market. Its receipt must say which sender it confirms, and its books
    count both transfers: booking whichever edge sorted first refused its
    true count, and counting one edge dropped the other's tins."""
    claim(client, db, "queen-st-grocer", "att_q1", held=95, **HELD, forwarded_qty=25,
          forwarded_to_name="Corner Mart")
    assert claim(client, db, "corner-mart", "att_cm1", received_qty=25, held=25, **HELD)["accepted"]   # one sender
    claim(client, db, "harbour-market", "att_h1", held=30, **HELD, forwarded_qty=10,
          forwarded_to_name="Corner Mart")
    assert_balance(RID)

    vague = claim(client, db, "corner-mart", "att_cm2", received_qty=25, held=25, **HELD)
    assert not vague["accepted"] and vague["code"] == "receipt_sender_ambiguous"
    assert "Queen St Grocer" in vague["reason"] and "Harbour Market" in vague["reason"]
    assert vague["code"] not in CHALLENGE_CODES and "end the call" in vague["message"]
    wrong = claim(client, db, "corner-mart", "att_cm2", received_qty=10, received_from="Bloor Foods", held=35, **HELD)
    assert wrong["code"] == "receipt_sender_ambiguous" and "Bloor Foods" in wrong["reason"]
    assert numbers(edge(db, "harbour-market", "corner-mart"))["state"] == "awaiting_recipient"
    assert party(db, "corner-mart")["shipped_in"] == 25
    assert_balance(RID)

    got = claim(client, db, "corner-mart", "att_cm2", received_qty=10, received_from="harbour market", held=35, **HELD)
    assert got["accepted"] and got["edge"]["from_party"] == "harbour-market"
    assert numbers(edge(db, "harbour-market", "corner-mart"))["state"] == "corroborated"
    assert numbers(edge(db, "queen-st-grocer", "corner-mart"))["state"] == "corroborated"
    cm = party(db, "corner-mart")
    assert (cm["shipped_in"], cm["unresolved"], cm["state"]) == (35, 0, "reconciled")
    assert_balance(RID)

    # One sender's correction re-books the party from every inbound edge.
    more = claim(client, db, "queen-st-grocer", "att_q2", held=90, **HELD, forwarded_qty=30,
                 forwarded_to_name="Corner Mart")
    assert more["accepted"]
    assert numbers(edge(db, "queen-st-grocer", "corner-mart"))["disputed_qty"] == 5
    assert party(db, "corner-mart")["shipped_in"] == 35
    assert_balance(RID)


def test_forwarding_to_itself_or_to_nobody_is_refused(client, db, recall, assert_balance):
    selfish = claim(client, db, "queen-st-grocer", "att_q1", held=80, **HELD,
                    forwards=[{"to_name": "Queen St Grocer", "qty": 40}])
    assert selfish["code"] == "forward_self"
    nameless = claim(client, db, "queen-st-grocer", "att_q2", held=80, **HELD, forwarded_qty=40)
    assert nameless["code"] == "forward_missing_name"
    assert db.edges.count_documents({}) == 0 and party(db, "queen-st-grocer")["forwarded"] == 0
    assert_balance(RID)


def test_a_negative_transfer_is_refused_by_atlas_and_leaves_no_stub(client, db, recall, assert_balance):
    res = claim(client, db, "queen-st-grocer", "att_q1", held=80, **HELD,
                forwards=[{"to_name": "Corner Mart", "qty": 50}, {"to_name": "Bloor Foods", "qty": -10}])
    assert not res["accepted"] and res["code"] == "negative" and res["atlas_error"]["code"] == 121
    assert db.parties.count_documents({"recall_id": RID}) == 3 and db.edges.count_documents({}) == 0
    assert_balance(RID)


# ---- V2: order system accounts and the directory --------------------------------------------

def test_a_distributors_order_system_claim_opens_one_edge_per_account(client, db, long_recall, assert_balance):
    res = claim(client, db, "dist-north", "op-accounts-1", rid=LONG_RID, source="order_system",
                forwards=[{"to_party_id": "bloor-foods", "qty": 120}, {"to_party_id": "annex-grocer", "qty": 180}])
    assert res["accepted"] and res["discovered_parties"] == ["bloor-foods", "annex-grocer"]
    assert_balance(LONG_RID)
    dist = party(db, "dist-north", LONG_RID)
    assert (dist["forwarded"], dist["unresolved"], dist["channel"], dist["order_system"]) == (300, 0, "order_system", "north")
    bloor = party(db, "bloor-foods", LONG_RID)
    assert (bloor["type"], bloor["tier"], bloor["found_via"], bloor["discovered_by"]) == (
        "store", 2, "order_system", "order_system:dist-north")
    assert (bloor["account"], bloor["distributor"], bloor["channel"], bloor["phone"]) == (
        "N-014", "dist-north", "sim", "+14165550127")
    assert bloor["next_action_at"] == pytest.approx(sim_now(client, db, LONG_RID), abs=0.5)
    assert db.tool_events.count_documents({"attempt_id": "op-accounts-1", "kind": "order_system"}) == 1

    # An account confirms its delivery and passes tins on to a small store: a
    # transfer to a tier 3 store. The distributor still waits on the other
    # account, so it is woken.
    set_timer(db, "dist-north", None, rid=LONG_RID)
    got = claim(client, db, "annex-grocer", "att_a1", rid=LONG_RID, received_qty=180, held=100, **HELD, forwards=[{"to_name": "Dundas Mini Mart", "qty": 80}])
    assert got["accepted"] and got["discovered"] == "dundas-mini-mart" and got["woken"] == ["dist-north"]
    lofts = party(db, "dundas-mini-mart", LONG_RID)
    assert (lofts["type"], lofts["tier"], lofts["found_via"], lofts["channel"]) == ("store", 3, "claim", "sim")
    annex = party(db, "annex-grocer", LONG_RID)
    assert (annex["shipped_in"], annex["forwarded"], annex["unresolved"]) == (180, 80, 0)
    assert party(db, "dist-north", LONG_RID)["state"] == "claimed"
    assert_balance(LONG_RID)


def test_tins_passed_to_another_store_are_a_transfer_and_land_on_its_shelf(client, db, recall, assert_balance):
    """Queen St Grocer passed 40 to Corner Mart: its `forwarded`, an edge,
    and a store that books them on the shelf. Tins on a shelf stay open, so
    the store is claimed, not reconciled, and its removal check is set."""
    sent = claim(client, db, "queen-st-grocer", "att_q1", held=50, **HELD, returned=30, forwarded_qty=40,
                 forwarded_to_name="Corner Mart")
    assert sent["accepted"] and party(db, "queen-st-grocer")["forwarded"] == 40
    got = claim(client, db, "corner-mart", "att_cm1", received_qty=40, on_shelf=40)
    assert got["accepted"] and numbers(edge(db, "queen-st-grocer", "corner-mart"))["state"] == "corroborated"
    cm = party(db, "corner-mart")
    assert (cm["shipped_in"], cm["on_shelf"], cm["unresolved"], cm["state"]) == (40, 40, 0, "claimed")
    assert cm["removal_check_at"] is not None and cm["next_action_at"] == cm["removal_check_at"]
    assert party(db, "queen-st-grocer")["state"] == "reconciled"
    assert got["open_units"] == 160          # Harbour 40 and Lakeside 80 not called, 40 on a shelf
    assert_balance(RID)
