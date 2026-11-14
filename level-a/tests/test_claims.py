"""Spec test 3 (claim posting) and the claim path's rules.

A valid claim books once; an exception mid-transaction writes nothing; the
same delivery twice posts once. Plus: Atlas-decided rejections with the two
numbers, the rejection codes, the dedupe key (V2: sorted destinations), the
sources and the evidence the form, store system, agent, order system and rep
channels log; the three checks a store answers, the refusals that name the
two values that disagree, and the removal check a claim leaves. The mass
balance is asserted after every write.
"""
from __future__ import annotations

import threading

import pytest

from levela.clock import get_clock, server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger.claims import (CHALLENGE_CODES, EVIDENCE_KIND, MAX_COUNT, REMOVAL_CHECK_H, SOURCES,
                                  challenges_used, checks, counts, dedupe_key, forwards, post_claim, speakable,
                                  storable, submit_claim)
from levela.ledger.rebuild import rebuild

RID = "RCL-2026-017"
BATCH = "B-4471"
HELD = {"batch_seen": BATCH, "block_test": True}
TILL = {"sold_source": "till_report"}
# Lakeside Foods' scripted answer: 20 sold (till report), 60 held (batch read, till refused).
LAKESIDE = {"sold": 20, "held": 60, **TILL, **HELD}


@pytest.fixture
def recall(client, db, assert_balance):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, emit=False)
    assert_balance(RID)
    return RID


def claim(client, db, party_id, attempt="att_c1", source="sim", **raw):
    return submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=attempt, source=source, raw=raw)


def party(db, party_id):
    return db.parties.find_one({"recall_id": RID, "party_id": party_id})


# ---- spec test 3 --------------------------------------------------------------

def test_spec_3_valid_claim_is_one_claim_and_one_party_update(client, db, recall, assert_balance):
    res = claim(client, db, "lakeside-foods", **LAKESIDE)
    assert res["accepted"] and res["txn_runs"] == 1 and res["open_units"] == 160
    assert res["message"] == speakable(res) == "Recorded. Thank them and move on."
    s = party(db, "lakeside-foods")
    assert (s["sold"], s["held"], s["unresolved"], s["state"]) == (20, 60, 0, "reconciled")
    assert (s["batch_seen"], s["sold_source"], s["block_test"], s["batch_code"]) == (BATCH, "till_report", True, BATCH)
    assert db.claims.count_documents({"recall_id": RID}) == 1
    assert db.claims.find_one({"_id": res["claim_id"]})["status"] == "accepted"
    assert db.recalls.find_one({"_id": RID})["open_units"] == 160
    assert_balance(RID)


def test_spec_3_exception_mid_transaction_writes_nothing(client, db, recall, assert_balance):
    before = party(db, "lakeside-foods")
    with pytest.raises(RuntimeError, match="failpoint"):
        submit_claim(client, db, recall_id=RID, party_id="lakeside-foods", attempt_id="att_fail", source="sim",
                     raw=LAKESIDE, fail_after_party_update=True)
    assert party(db, "lakeside-foods") == before
    assert db.claims.count_documents({}) == 0
    assert db.recalls.find_one({"_id": RID})["open_units"] == 240
    assert_balance(RID)


def test_spec_3_same_claim_twice_posts_once(client, db, recall, assert_balance):
    first = claim(client, db, "lakeside-foods", **LAKESIDE)
    again = claim(client, db, "lakeside-foods", **{**LAKESIDE, "sold": "20", "held": 60.0, "block_test": "yes"})
    assert again["duplicate"] and again["accepted"] and again["claim_id"] == first["claim_id"]
    assert again["message"] == "Already recorded. Do not ask again."
    assert db.claims.count_documents({}) == 1
    assert_balance(RID)


def test_going_back_to_an_earlier_answer_on_one_call_is_booked_again(client, db, recall, assert_balance):
    """A, then B, then A again on one call: the last answer is the one the
    party confirmed, and claims are absolute, so it is booked."""
    a, b = {"held": 20, "returned": 60, **HELD}, {"held": 30, "returned": 50, **HELD}
    first = claim(client, db, "lakeside-foods", attempt="att_aba", **a)
    second = claim(client, db, "lakeside-foods", attempt="att_aba", **b)
    back = claim(client, db, "lakeside-foods", attempt="att_aba", **a)
    assert back["accepted"] and not back.get("duplicate")
    assert back["claim_id"] not in (first["claim_id"], second["claim_id"])
    s = party(db, "lakeside-foods")
    assert (s["held"], s["returned"]) == (20, 60)
    assert_balance(RID)
    # Delivered again, the correction itself is the duplicate.
    again = claim(client, db, "lakeside-foods", attempt="att_aba", **{**a, "held": "20"})
    assert again["duplicate"] and again["claim_id"] == back["claim_id"]
    assert db.claims.count_documents({"attempt_id": "att_aba"}) == 3
    assert_balance(RID)


def test_corrections_and_withdrawals_rebuild_identically(client, db, recall, assert_balance):
    """The booking rules read only ledger documents, so replaying the claims
    log reproduces them: a correction booked again, a withdrawn transfer and
    the stub deleted with it."""
    booked = [claim(client, db, "lakeside-foods", attempt="att_l", **LAKESIDE),
              claim(client, db, "lakeside-foods", attempt="att_l", **{**LAKESIDE, "sold": 30, "held": 50}),
              claim(client, db, "lakeside-foods", attempt="att_l", **LAKESIDE),
              claim(client, db, "harbour-market", attempt="att_h", on_shelf=30, forwarded_qty=10,
                    forwarded_to_name="Bloor Grocer"),
              claim(client, db, "harbour-market", attempt="att_h", on_shelf=40)]
    assert all(r["accepted"] and not r.get("duplicate") for r in booked)
    assert booked[-1]["removed_parties"] == ["bloor-grocer"]
    assert_balance(RID)
    result = rebuild(client, db, RID, scratch_db_name=f"{db.name}_rb")
    assert result["identical"], result["diffs"]
    assert result["claims_replayed"] == 5


@pytest.mark.parametrize("value", [10**20, -(10**20), 2**63, "99999999999999999999", 1e20, MAX_COUNT + 1])
def test_a_count_too_large_to_be_tins_is_a_bad_number_not_a_crash(client, db, recall, assert_balance, value):
    res = claim(client, db, "lakeside-foods", on_shelf=value)
    assert not res["accepted"] and res["code"] == "bad_number" and res["code"] not in CHALLENGE_CODES
    row = db.claims.find_one({"_id": res["claim_id"]})
    assert row["status"] == "rejected" and str(row["raw"]["on_shelf"]) == str(value)
    listed = claim(client, db, "lakeside-foods", attempt="att_big", on_shelf=1,
                   forwards=[{"to_name": "Corner Mart", "qty": value}])
    assert listed["code"] == "bad_number"
    assert party(db, "lakeside-foods")["unresolved"] == 80
    assert_balance(RID)


def test_storable_turns_only_out_of_range_integers_into_text():
    body = {"a": 2**63 - 1, "b": -(2**63), "c": 2**63, "d": [{"qty": -(2**63) - 1}, True, 1.5, "x"], "e": None}
    assert storable(body) == {"a": 2**63 - 1, "b": -(2**63), "c": str(2**63),
                              "d": [{"qty": str(-(2**63) - 1)}, True, 1.5, "x"], "e": None}


def test_a_delivery_racing_itself_posts_once(client, db, recall, assert_balance):
    results = []

    def deliver():
        results.append(claim(client, db, "lakeside-foods", attempt="att_race", **LAKESIDE))

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(bool(r.get("duplicate")) for r in results) == [False, True]
    assert all(r["accepted"] for r in results)
    assert db.claims.count_documents({}) == 1
    assert_balance(RID)


# ---- rejections: Atlas decides --------------------------------------------------

def test_over_count_is_refused_by_atlas_with_both_numbers(client, db, recall, assert_balance):
    before = party(db, "harbour-market")
    res = claim(client, db, "harbour-market", on_shelf=60)
    assert not res["accepted"] and res["code"] == "over_count"
    assert (res["reported_total"], res["shipped_in"]) == (60, 40)
    assert res["values"] == {"reported": 60, "shipped_in": 40} and res["pair"] == [60, 40]
    assert res["atlas_error"]["code"] == 121 and res["atlas_error"]["errInfo"]
    assert "60 tins" in res["message"] and "40 tins" in res["message"]
    after = party(db, "harbour-market")
    assert {k: after[k] for k in ("on_shelf", "unresolved")} == {k: before[k] for k in ("on_shelf", "unresolved")}
    assert after["state"] == "reached"
    row = db.claims.find_one({"_id": res["claim_id"]})
    assert row["status"] == "rejected" and row["atlas_error"]["code"] == 121
    assert challenges_used(db, "att_c1") == 1
    assert_balance(RID)
    ok = claim(client, db, "harbour-market", sold=10, held=30, **TILL, **HELD)
    assert ok["accepted"] and ok["open_units"] == 200
    assert_balance(RID)


@pytest.mark.parametrize("raw, code", [
    ({"on_shelf": -5}, "negative"),
    ({"on_shelf": 10.5}, "not_integer"),
    ({"on_shelf": "batches"}, "bad_number"),
    ({"on_shelf": True}, "bad_number"),
    ({"batch_seen": True}, "bad_answer"),
    ({"block_test": "maybe"}, "bad_answer"),
])
def test_bad_numbers_are_refused_with_a_code(client, db, recall, assert_balance, raw, code):
    res = claim(client, db, "lakeside-foods", **raw)
    assert not res["accepted"] and res["code"] == code
    if code in ("negative", "not_integer"):
        assert res["atlas_error"]["code"] == 121          # the validator refused it, not Python
    assert (code in CHALLENGE_CODES) == (code not in ("bad_number", "bad_answer"))
    assert party(db, "lakeside-foods")["unresolved"] == 80
    assert_balance(RID)


def test_claims_on_a_closed_unknown_or_missing_party_are_refused(client, db, recall, assert_balance):
    assert claim(client, db, "nobody")["code"] == "unknown_party"
    res = submit_claim(client, db, recall_id="RCL-missing", party_id="x", attempt_id="a", source="sim", raw={})
    assert res["code"] == "unknown_recall"
    # Closed by hand (try_close would recompute 240 open tins and refuse).
    db.recalls.update_one({"_id": RID}, {"$set": {"open_units": 0, "status": "closed"}})
    res = claim(client, db, "lakeside-foods", attempt="att_late", **LAKESIDE)
    assert res["code"] == "recall_closed" and res["message"].startswith("This recall is already closed")
    assert party(db, "lakeside-foods")["unresolved"] == 80


def test_delivery_note_is_checked_in_the_transaction(client, db, recall, assert_balance):
    def note(n, attempt):
        return submit_claim(client, db, recall_id=RID, party_id="harbour-market", attempt_id=attempt, source="sim",
                            raw={"on_shelf": 40, **({"delivery_note": n} if n else {})}, require_delivery_note=True)
    assert note(None, "att_n1")["code"] == "delivery_note_required"
    assert note("DN-99999", "att_n2")["code"] == "delivery_note_mismatch"
    ok = note("dn 48213", "att_n3")
    assert ok["accepted"]
    assert_balance(RID)


def test_unknown_source_is_a_programming_error(client, db, recall):
    with pytest.raises(ValueError, match="unknown claim source"):
        claim(client, db, "lakeside-foods", source="email", returned=80)


# ---- evidence (invariant 3) and the sources ------------------------------------------------

def test_channels_without_a_webhook_log_their_own_evidence_once(client, db, recall, assert_balance):
    for i, source in enumerate(("form", "store_system", "agent", "order_system", "rep")):
        raw = {"returned": 80 - i}
        first = claim(client, db, "lakeside-foods", attempt=f"att_{source}", source=source, **raw)
        again = claim(client, db, "lakeside-foods", attempt=f"att_{source}", source=source, **raw)
        assert first["accepted"] and again["duplicate"]
        events = list(db.tool_events.find({"attempt_id": f"att_{source}"}))
        assert [(e["kind"], e["source"], e["payload"]) for e in events] == [(EVIDENCE_KIND[source], source, raw)]
        assert db.claims.find_one({"_id": first["claim_id"]})["source"] == source
        assert_balance(RID)
    # Calls are logged by POST /tools/submit_count before it books, not here.
    claim(client, db, "harbour-market", attempt="att_voice", source="voice", on_shelf=40)
    assert db.tool_events.count_documents({"attempt_id": "att_voice"}) == 0
    assert set(SOURCES) == {"voice", "sim", "form", "store_system", "agent", "order_system", "rep"}


def test_post_claim_books_another_nodes_report_with_the_link_and_sequence_as_its_key(client, db, recall,
                                                                                     assert_balance):
    """WS5 books a signed report as a claim with source agent; the attempt id
    is the link plus the sequence number, so a repeated report posts once."""
    db.parties.update_one({"recall_id": RID, "party_id": "lakeside-foods"},
                          {"$set": {"channel": "agent", "link_id": "link-lakeside"}})
    report = {"sold": 20, "held": 50, "on_shelf": 10, **TILL, **HELD}
    first = post_claim(client, db, recall_id=RID, party_id="lakeside-foods", attempt_id="link-lakeside:1",
                       source="agent", raw=report)
    again = post_claim(client, db, recall_id=RID, party_id="lakeside-foods", attempt_id="link-lakeside:1",
                       source="agent", raw=report)
    assert first["accepted"] and again["duplicate"] and again["claim_id"] == first["claim_id"]
    assert db.claims.find_one({"_id": first["claim_id"]})["dedupe_key"].startswith("link-lakeside:1:")
    assert party(db, "lakeside-foods")["on_shelf"] == 10
    assert_balance(RID)


# ---- parsing and the dedupe key (no database) -------------------------------------------

def test_counts_sum_every_forward():
    assert counts({"held": "5", "forwards": [{"to_name": "A", "qty": 3}, {"to_party_id": "b", "qty": "4"}]}) == {
        "sold": 0, "on_shelf": 0, "held": 5, "returned": 0, "forwarded_qty": 7, "received_qty": None}
    assert counts({"forwarded_qty": 40, "forwarded_to_name": "Corner Mart"})["forwarded_qty"] == 40
    assert counts({"received_qty": 0})["received_qty"] == 0


def test_forwards_parse_both_shapes():
    assert forwards({"forwarded_qty": 40, "forwarded_to_name": " Corner Mart "}) == [
        {"qty": 40, "to_name": "Corner Mart"}]
    assert forwards({"forwarded_qty": 0, "forwarded_to_name": "Corner Mart"}) == []
    assert forwards({"forwards": [{"to_name": "A", "qty": 0}, {"to_party_id": "b", "to_name": "B", "qty": 2}],
                     "forwarded_qty": 0}) == [{"qty": 2, "to_party_id": "b", "to_name": "B"}]
    assert forwards({"forwards": [{"to_name": "A", "qty": -3}]}) == [{"qty": -3, "to_name": "A"}]


@pytest.mark.parametrize("raw, match", [
    ({"forwards": [{"to_name": "A"}]}, "no count was given for the tins sent to A"),
    ({"forwards": [{"to_name": "A", "qty": 3}], "forwarded_qty": 5}, "does not match"),
    ({"forwards": "Corner Mart"}, "must be a list"),
    ({"forwards": [["A", 3]]}, "must be a list"),
])
def test_unreadable_forwards_are_bad_numbers(raw, match):
    with pytest.raises(Exception, match=match) as info:
        forwards(raw)
    assert info.value.code == "bad_number"


def test_dedupe_key_ignores_the_order_of_destinations():
    a = {"held": 1, "forwards": [{"to_name": "Corner Mart", "qty": 5}, {"to_party_id": "bloor-grocer", "qty": 7}]}
    b = {"held": "1", "forwards": [{"to_party_id": "bloor-grocer", "qty": "7"}, {"to_name": "corner  mart", "qty": 5}]}
    assert dedupe_key("att", a) == dedupe_key("att", b)
    assert dedupe_key("att", a) != dedupe_key("att2", a)
    changed = {**a, "forwards": [{"to_name": "Corner Mart", "qty": 6}, {"to_party_id": "bloor-grocer", "qty": 7}]}
    assert dedupe_key("att", a) != dedupe_key("att", changed)


def test_dedupe_key_treats_the_single_pair_as_one_entry():
    pair = {"held": 80, "forwarded_qty": 40, "forwarded_to_name": "Corner Mart"}
    listed = {"held": 80, "forwards": [{"to_name": "Corner Mart", "qty": 40}]}
    assert dedupe_key("att", pair) == dedupe_key("att", listed)


def test_dedupe_key_includes_the_delivery_note_the_checks_and_survives_bad_numbers():
    assert dedupe_key("att", {"held": 1}) != dedupe_key("att", {"held": 1, "delivery_note": "DN-1"})
    assert dedupe_key("att", {"held": 1, "delivery_note": "DN-1"}) == dedupe_key(
        "att", {"held": 1, "delivery_note": "dn 1"})
    assert dedupe_key("att", {"held": "batches"}) != dedupe_key("att", {"held": "many"})
    assert dedupe_key("att", {"held": 1, "block_test": "yes"}) != dedupe_key("att", {"held": 1, "block_test": "no"})


def test_speakable_covers_the_codes_the_agent_must_act_on():
    assert "delivery note" in speakable({"accepted": False, "code": "delivery_note_required"})
    assert "end the call" in speakable({"accepted": False, "code": "no_inbound_edge"})
    assert "name of the store" in speakable({"accepted": False, "code": "forward_missing_name"})
    assert "item sales report" in speakable({"accepted": False, "code": "sold_needs_till", "reason": "x"})
    assert "scan one tin" in speakable({"accepted": False, "code": "block_not_confirmed", "reason": "x"})
    assert "batch code" in speakable({"accepted": False, "code": "batch_mismatch", "reason": "x"})
    assert "stock record" in speakable({"accepted": False, "code": "still_on_shelf", "reason": "x"})
    # Stop codes (api/tools.py): asking again on this call cannot settle them.
    for code in ("forward_changed", "forward_restate", "receipt_sender_ambiguous"):
        said = speakable({"accepted": False, "code": code, "reason": "Corner Mart is missing"})
        assert "Corner Mart is missing" in said and "end the call" in said, code


def test_dedupe_key_tells_receipts_from_different_senders_apart():
    base = {"received_qty": 10, "on_shelf": 10}
    assert dedupe_key("att", {**base, "received_from": "Harbour Market"}) == dedupe_key(
        "att", {**base, "received_from": "harbour  market"})
    assert dedupe_key("att", {**base, "received_from": "Harbour Market"}) != dedupe_key(
        "att", {**base, "received_from": "Queen St Grocer"})
    assert dedupe_key("att", base) == dedupe_key("att", {**base, "received_from": " "})


# ---- the three checks and "removal needs evidence" ------------------------------------------

def _corner(client, db, **raw):
    """Queen St Grocer passes 40 to Corner Mart; Corner Mart answers it received 25."""
    assert claim(client, db, "queen-st-grocer", attempt="att_q", sold=50, held=30, forwarded_qty=40,
                 forwarded_to_name="Corner Mart", **TILL, **HELD)["accepted"]
    return claim(client, db, "corner-mart", attempt="att_cm", received_qty=25, **raw)


def test_checks_parse_the_closed_answers():
    assert checks({"batch_seen": " b 4471 ", "sold_source": "Till report", "block_test": "Yes"}) == {
        "batch_seen": "B4471", "sold_source": "till_report", "block_test": True}
    assert checks({"sold_source": "estimate", "block_test": False}) == {"sold_source": "estimate",
                                                                         "block_test": False}
    assert checks({"sold_source": "till_report", "block_test": "no"}) == {"sold_source": "till_report",
                                                                           "block_test": False}
    assert checks({"on_shelf": 3, "batch_seen": "", "block_test": " "}) == {}


def test_a_store_books_on_shelf_with_its_checks_and_a_removal_check(client, db, recall, assert_balance):
    res = _corner(client, db, on_shelf=25, batch_seen="b 4471", block_test=False)
    assert res["accepted"] and res["open_units"] == 160          # 120 not called yet + 15 disputed + 25 on a shelf
    cm = party(db, "corner-mart")
    assert (cm["on_shelf"], cm["batch_seen"], cm["block_test"]) == (25, BATCH, False)
    assert cm["state"] == "claimed"                              # tins on a shelf: not reconciled
    sim = get_clock(db, RID).sim_now(server_now(client))
    assert sim + REMOVAL_CHECK_H - 1 < cm["removal_check_at"] <= sim + REMOVAL_CHECK_H + 1
    # Its own claim opened a dispute (25 of 40): the follow-up it was already
    # due for stands, the removal check keeps its own hour.
    assert cm["next_action_at"] <= sim < cm["removal_check_at"]
    assert_balance(RID)


def _sim(client, db):
    return get_clock(db, RID).sim_now(server_now(client))


def _dispute_then_queen_st_due(client, db) -> float:
    """Queen St passes 40 to Corner Mart, Corner Mart counts 25 (held): the
    ledger wakes Queen St for its dispute follow-up. Returns that due time."""
    assert _corner(client, db, held=25, **HELD)["accepted"]
    assert db.edges.find_one({"recall_id": RID})["state"] == "disputed"
    due = party(db, "queen-st-grocer")["next_action_at"]
    assert due is not None and due <= _sim(client, db)
    return due


def test_the_removal_check_does_not_push_back_a_dispute_followup(client, db, recall, assert_balance):
    """Lead loose end 2: a claim that leaves tins on a shelf sets the removal check
    (4 h on) but keeps the earlier follow-up the party is due for now."""
    due = _dispute_then_queen_st_due(client, db)
    res = claim(client, db, "queen-st-grocer", attempt="att_q2", sold=50, held=20, on_shelf=10, forwarded_qty=40,
                forwarded_to_name="Corner Mart", **TILL, **HELD)
    assert res["accepted"], res
    qs = party(db, "queen-st-grocer")
    sim = _sim(client, db)
    assert sim + REMOVAL_CHECK_H - 1 < qs["removal_check_at"] <= sim + REMOVAL_CHECK_H + 1
    assert qs["next_action_at"] == due, "the dispute follow-up still fires now"
    assert_balance(RID)


def test_a_due_time_later_than_the_removal_check_gives_way_to_it(client, db, recall, assert_balance):
    _dispute_then_queen_st_due(client, db)
    later = _sim(client, db) + REMOVAL_CHECK_H + 6
    db.parties.update_one({"recall_id": RID, "party_id": "queen-st-grocer"}, {"$set": {"next_action_at": later}})
    assert claim(client, db, "queen-st-grocer", attempt="att_q2", sold=50, held=20, on_shelf=10, forwarded_qty=40,
                 forwarded_to_name="Corner Mart", **TILL, **HELD)["accepted"]
    qs = party(db, "queen-st-grocer")
    assert qs["next_action_at"] == qs["removal_check_at"] < later
    assert_balance(RID)


def test_with_nothing_else_owed_the_removal_check_replaces_an_earlier_timer(client, db, recall, assert_balance):
    """No dispute, no question, no receipt owed: an old ladder timer is not kept."""
    db.parties.update_one({"recall_id": RID, "party_id": "lakeside-foods"},
                          {"$set": {"next_action_at": _sim(client, db) - 1.0}})
    assert claim(client, db, "lakeside-foods", attempt="att_l", sold=20, on_shelf=60, **TILL)["accepted"]
    lf = party(db, "lakeside-foods")
    assert lf["next_action_at"] == lf["removal_check_at"] > _sim(client, db)
    assert_balance(RID)


def test_an_open_question_keeps_its_due_time_past_a_removal_check(client, db, recall, assert_balance):
    due = _sim(client, db)
    db.parties.update_one({"recall_id": RID, "party_id": "lakeside-foods"},
                          {"$set": {"next_action_at": due, "open_questions": ["q-01"]}})
    assert claim(client, db, "lakeside-foods", attempt="att_l", sold=20, on_shelf=60, **TILL)["accepted"]
    lf = party(db, "lakeside-foods")
    assert lf["next_action_at"] == due < lf["removal_check_at"]
    assert_balance(RID)


def test_spec_2_a_claim_of_held_with_the_wrong_batch_is_refused_with_the_two_values(client, db, recall,
                                                                                     assert_balance):
    res = _corner(client, db, held=25, batch_seen="B-4472", block_test=True)
    assert not res["accepted"] and res["code"] == "batch_mismatch"
    assert res["reason"] == "batch_seen B-4472, recalled batch B-4471"
    assert res["values"] == {"batch_seen": "B-4472", "batch_code": BATCH} and res["pair"] == ["B-4472", BATCH]
    assert res["atlas_error"]["code"] == 121 and res["code"] in CHALLENGE_CODES
    assert party(db, "corner-mart")["held"] == 0
    assert_balance(RID)


@pytest.mark.parametrize("raw, code, reason", [
    ({"held": 25}, "held_needs_evidence", "held 25, batch_seen missing"),
    ({"held": 25, "batch_seen": BATCH}, "block_not_confirmed", "held 25, block_test missing"),
    ({"held": 25, "batch_seen": BATCH, "block_test": "no"}, "block_not_confirmed", "held 25, block_test no"),
    ({"sold": 25}, "sold_needs_till", "sold 25, sold_by_till 0 and no till report"),
    ({"sold": 25, "sold_source": "a guess"}, "sold_needs_till", "sold 25, sold_by_till 0 and no till report"),
])
def test_every_missing_piece_of_evidence_is_named(client, db, recall, assert_balance, raw, code, reason):
    res = _corner(client, db, **raw)
    assert (res["accepted"], res["code"], res["reason"]) == (False, code, reason)
    assert res["atlas_error"]["code"] == 121 and len(res["pair"]) == 2
    assert_balance(RID)


def test_held_with_the_batch_and_a_refused_sale_settles_the_store(client, db, recall, assert_balance):
    assert _corner(client, db, on_shelf=25, batch_seen=BATCH, block_test=False)["accepted"]
    held = claim(client, db, "corner-mart", attempt="att_cm2", received_qty=25, held=25, **HELD)
    assert held["accepted"] and held["open_units"] == 135       # 120 not called yet + the dispute
    cm = party(db, "corner-mart")
    assert (cm["on_shelf"], cm["held"], cm["removal_check_at"]) == (0, 25, None)
    assert_balance(RID)


def test_a_check_the_claim_leaves_out_keeps_its_earlier_answer(client, db, recall, assert_balance):
    assert _corner(client, db, on_shelf=25, **HELD)["accepted"]
    # A later count without the checks still rests on them: held is allowed.
    res = claim(client, db, "corner-mart", attempt="att_cm3", received_qty=25, held=25)
    assert res["accepted"]
    assert (party(db, "corner-mart")["batch_seen"], party(db, "corner-mart")["held"]) == (BATCH, 25)
    assert_balance(RID)


def test_spec_37_the_stock_record_outranks_a_phone_claim_of_all_pulled(client, db, recall, assert_balance):
    """The claim-path half of test 37: while the linked store's own stock
    record shows a tin on the shelf, all pulled is refused with the two
    values; after its quarantine moves the same claim is accepted."""
    assert _corner(client, db, on_shelf=25, batch_seen=BATCH, block_test=False)["accepted"]
    db.parties.update_one({"recall_id": RID, "party_id": "corner-mart"}, {"$set": {"system_on_shelf": 1}})
    res = claim(client, db, "corner-mart", attempt="att_cm4", received_qty=25, held=25, **HELD)
    assert (res["accepted"], res["code"]) == (False, "still_on_shelf")
    assert res["values"] == {"on_shelf": 0, "system_on_shelf": 1} and res["pair"] == [0, 1]
    assert party(db, "corner-mart")["on_shelf"] == 25
    db.parties.update_one({"recall_id": RID, "party_id": "corner-mart"}, {"$set": {"system_on_shelf": 0}})
    ok = claim(client, db, "corner-mart", attempt="att_cm5", received_qty=25, held=25, **HELD)
    assert ok["accepted"]
    assert_balance(RID)
