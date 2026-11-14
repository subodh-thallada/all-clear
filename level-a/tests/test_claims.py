"""Spec test 3 (claim posting) and the claim path's rules.

A valid claim books once; an exception mid-transaction writes nothing; the
same delivery twice posts once. Plus: Atlas-decided rejections with the two
numbers, the rejection codes, the dedupe key (V2: sorted destinations), the
five sources and the evidence the form, order system and rep channels log; the
three checks a store answers, the refusals that name the two values
that disagree, and the removal check a claim leaves. The mass balance is asserted
after every write.
"""
from __future__ import annotations

import threading

import pytest

from levela.db.seed import load_scenario, start_recall
from levela.clock import get_clock, server_now
from levela.ledger.claims import (CHALLENGE_CODES, REMOVAL_CHECK_H, MAX_COUNT, SOURCES, challenges_used, checks, counts,
                                  dedupe_key, forwards, speakable, storable, submit_claim)
from levela.ledger.rebuild import rebuild

RID = "RCL-2026-017"


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

def test_valid_claim_is_one_claim_and_one_party_update(client, db, recall, assert_balance):
    res = claim(client, db, "lakeside-foods", held=20, returned=60)
    assert res["accepted"] and res["txn_runs"] == 1 and res["open_units"] == 160
    assert res["message"] == speakable(res) == "Recorded. Thank them and move on."
    s = party(db, "lakeside-foods")
    assert (s["held"], s["returned"], s["unresolved"], s["state"]) == (20, 60, 0, "reconciled")
    assert db.claims.count_documents({"recall_id": RID}) == 1
    assert db.claims.find_one({"_id": res["claim_id"]})["status"] == "accepted"
    assert db.recalls.find_one({"_id": RID})["open_units"] == 160
    assert_balance(RID)


def test_exception_mid_transaction_writes_nothing(client, db, recall, assert_balance):
    before = party(db, "lakeside-foods")
    with pytest.raises(RuntimeError, match="failpoint"):
        submit_claim(client, db, recall_id=RID, party_id="lakeside-foods", attempt_id="att_fail", source="sim",
                     raw={"held": 20, "returned": 60}, fail_after_party_update=True)
    assert party(db, "lakeside-foods") == before
    assert db.claims.count_documents({}) == 0
    assert db.recalls.find_one({"_id": RID})["open_units"] == 240
    assert_balance(RID)


def test_same_claim_twice_posts_once(client, db, recall, assert_balance):
    first = claim(client, db, "lakeside-foods", held=20, returned=60)
    again = claim(client, db, "lakeside-foods", held="20", returned=60.0)
    assert again["duplicate"] and again["accepted"] and again["claim_id"] == first["claim_id"]
    assert again["message"] == "Already recorded. Do not ask again."
    assert db.claims.count_documents({}) == 1
    assert_balance(RID)


def test_going_back_to_an_earlier_answer_on_one_call_is_booked_again(client, db, recall, assert_balance):
    """A, then B, then A again on one call: the last answer is the one the
    party confirmed, and claims are absolute, so it is booked (it used to be
    swallowed as a duplicate of the first A while the party kept B)."""
    first = claim(client, db, "lakeside-foods", attempt="att_aba", held=20, returned=60)
    second = claim(client, db, "lakeside-foods", attempt="att_aba", held=30, returned=50)
    back = claim(client, db, "lakeside-foods", attempt="att_aba", held=20, returned=60)
    assert back["accepted"] and not back.get("duplicate")
    assert back["claim_id"] not in (first["claim_id"], second["claim_id"])
    assert back["message"] == "Recorded. Thank them and move on."
    s = party(db, "lakeside-foods")
    assert (s["held"], s["returned"]) == (20, 60)
    assert_balance(RID)
    # Delivered again, the correction itself is the duplicate.
    again = claim(client, db, "lakeside-foods", attempt="att_aba", held="20", returned=60)
    assert again["duplicate"] and again["claim_id"] == back["claim_id"]
    assert db.claims.count_documents({"attempt_id": "att_aba"}) == 3
    assert_balance(RID)


def test_corrections_and_withdrawals_rebuild_identically(client, db, recall, assert_balance):
    """The new booking rules read only ledger documents, so replaying the
    claims log reproduces them: a correction booked again, a withdrawn
    transfer and the stub deleted with it."""
    booked = [claim(client, db, "lakeside-foods", attempt="att_l", held=20, returned=60),
              claim(client, db, "lakeside-foods", attempt="att_l", held=30, returned=50),
              claim(client, db, "lakeside-foods", attempt="att_l", held=20, returned=60),
              claim(client, db, "harbour-market", attempt="att_h", held=30, forwarded_qty=10,
                    forwarded_to_name="Bloor Alarm"),
              claim(client, db, "harbour-market", attempt="att_h", held=40)]
    assert all(r["accepted"] and not r.get("duplicate") for r in booked)
    assert booked[-1]["removed_parties"] == ["bloor-alarm"]
    assert_balance(RID)
    result = rebuild(client, db, RID, scratch_db_name=f"{db.name}_rb")
    assert result["identical"], result["diffs"]
    assert result["claims_replayed"] == 5


@pytest.mark.parametrize("value", [10**20, -(10**20), 2**63, "99999999999999999999", 1e20, MAX_COUNT + 1])
def test_a_count_too_large_to_be_panels_is_a_bad_number_not_a_crash(client, db, recall, assert_balance, value):
    """A 20-digit count used to reach the BSON encoder inside the claim
    transaction and raise OverflowError (a 500 on the form and the tool)."""
    res = claim(client, db, "lakeside-foods", held=value)
    assert not res["accepted"] and res["code"] == "bad_number" and res["code"] not in CHALLENGE_CODES
    row = db.claims.find_one({"_id": res["claim_id"]})
    assert row["status"] == "rejected" and str(row["raw"]["held"]) == str(value)
    listed = claim(client, db, "lakeside-foods", attempt="att_big", held=1,
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
        results.append(claim(client, db, "lakeside-foods", attempt="att_race", held=20, returned=60))

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
    res = claim(client, db, "harbour-market", held=60)
    assert not res["accepted"] and res["code"] == "over_count"
    assert (res["reported_total"], res["shipped_in"]) == (60, 40)
    assert res["atlas_error"]["code"] == 121 and res["atlas_error"]["errInfo"]
    assert "60 tins" in res["message"] and "40 tins" in res["message"]
    after = party(db, "harbour-market")
    assert {k: after[k] for k in ("held", "unresolved")} == {k: before[k] for k in ("held", "unresolved")}
    assert after["state"] == "reached"
    row = db.claims.find_one({"_id": res["claim_id"]})
    assert row["status"] == "rejected" and row["atlas_error"]["code"] == 121
    assert challenges_used(db, "att_c1") == 1
    assert_balance(RID)
    fixed = claim(client, db, "harbour-market", held=35, returned=5)
    assert fixed["accepted"] and fixed["open_units"] == 200
    assert_balance(RID)


@pytest.mark.parametrize("raw, code", [
    ({"held": -5}, "negative"),
    ({"held": 10.5}, "not_integer"),
    ({"held": "batches"}, "bad_number"),
    ({"held": True}, "bad_number"),
    ({"sold_source": "two point four"}, "bad_answer"),
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
    res = claim(client, db, "lakeside-foods", attempt="att_late", held=20, returned=60)
    assert res["code"] == "recall_closed" and res["message"].startswith("This recall is already closed")
    assert party(db, "lakeside-foods")["unresolved"] == 80


def test_delivery_note_is_checked_in_the_transaction(client, db, recall, assert_balance):
    def note(n, attempt):
        return submit_claim(client, db, recall_id=RID, party_id="harbour-market", attempt_id=attempt, source="sim",
                            raw={"held": 40, **({"delivery_note": n} if n else {})}, require_delivery_note=True)
    assert note(None, "att_n1")["code"] == "delivery_note_required"
    assert note("DN-99999", "att_n2")["code"] == "delivery_note_mismatch"
    ok = note("dn 48213", "att_n3")
    assert ok["accepted"]
    assert_balance(RID)


def test_unknown_source_is_a_programming_error(client, db, recall):
    with pytest.raises(ValueError, match="unknown claim source"):
        claim(client, db, "lakeside-foods", source="email", held=80)


# ---- evidence (invariant 3) ----------------------------------------------------------

def test_form_order_system_and_rep_claims_log_their_own_evidence_once(client, db, recall, assert_balance):
    for i, source in enumerate(("form", "order_system", "rep")):
        raw = {"held": 80 - i}
        first = claim(client, db, "lakeside-foods", attempt=f"att_{source}", source=source, **raw)
        again = claim(client, db, "lakeside-foods", attempt=f"att_{source}", source=source, **raw)
        assert first["accepted"] and again["duplicate"]
        events = list(db.tool_events.find({"attempt_id": f"att_{source}"}))
        assert [(e["kind"], e["source"], e["payload"]) for e in events] == [(source, source, raw)]
        assert_balance(RID)
    # Calls are logged by POST /tools/submit_count before it books, not here.
    claim(client, db, "harbour-market", attempt="att_voice", source="voice", held=40)
    assert db.tool_events.count_documents({"attempt_id": "att_voice"}) == 0
    assert set(SOURCES) == {"voice", "sim", "form", "order_system", "rep"}


# ---- parsing and the dedupe key (no database) -------------------------------------------

def test_counts_sum_every_forward():
    assert counts({"held": "5", "forwards": [{"to_name": "A", "qty": 3}, {"to_party_id": "b", "qty": "4"}]}) == {
        "held": 5, "on_shelf": 0, "held": 0, "returned": 0, "forwarded_qty": 7, "received_qty": None}
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
    a = {"held": 1, "forwards": [{"to_name": "Corner Mart", "qty": 5}, {"to_party_id": "bloor-alarm", "qty": 7}]}
    b = {"held": "1", "forwards": [{"to_party_id": "bloor-alarm", "qty": "7"},
                                        {"to_name": "corner  properties", "qty": 5}]}
    assert dedupe_key("att", a) == dedupe_key("att", b)
    assert dedupe_key("att", a) != dedupe_key("att2", a)
    changed = {**a, "forwards": [{"to_name": "Corner Mart", "qty": 6}, {"to_party_id": "bloor-alarm", "qty": 7}]}
    assert dedupe_key("att", a) != dedupe_key("att", changed)


def test_dedupe_key_treats_the_single_pair_as_one_entry():
    pair = {"held": 80, "forwarded_qty": 40, "forwarded_to_name": "Corner Mart"}
    listed = {"held": 80, "forwards": [{"to_name": "Corner Mart", "qty": 40}]}
    assert dedupe_key("att", pair) == dedupe_key("att", listed)


def test_dedupe_key_includes_the_delivery_note_and_survives_bad_numbers():
    assert dedupe_key("att", {"held": 1}) != dedupe_key("att", {"held": 1, "delivery_note": "DN-1"})
    assert dedupe_key("att", {"held": 1, "delivery_note": "DN-1"}) == dedupe_key(
        "att", {"held": 1, "delivery_note": "dn 1"})
    assert dedupe_key("att", {"held": "batches"}) != dedupe_key("att", {"held": "many"})


def test_speakable_covers_the_codes_the_agent_must_act_on():
    assert "delivery note" in speakable({"accepted": False, "code": "delivery_note_required"})
    assert "end the call" in speakable({"accepted": False, "code": "no_inbound_edge"})
    assert "store or store" in speakable({"accepted": False, "code": "forward_missing_name"})
    # Stop codes (api/tools.py): asking again on this call cannot settle them.
    for code in ("forward_changed", "forward_restate", "receipt_sender_ambiguous"):
        said = speakable({"accepted": False, "code": code, "reason": "Corner Mart is missing"})
        assert "Corner Mart is missing" in said and "end the call" in said, code


def test_dedupe_key_tells_receipts_from_different_senders_apart():
    base = {"received_qty": 10, "held": 10}
    assert dedupe_key("att", {**base, "received_from": "Harbour Market"}) == dedupe_key(
        "att", {**base, "received_from": "harbour  fire"})
    assert dedupe_key("att", {**base, "received_from": "Harbour Market"}) != dedupe_key(
        "att", {**base, "received_from": "Queen St Grocer"})
    assert dedupe_key("att", base) == dedupe_key("att", {**base, "received_from": " "})


# ---- the three checks and "fixed needs evidence" ------------------------------------------

def _corner(client, db, **raw):
    """Queen St Grocer installs 40 at Corner Mart; Corner Mart answers."""
    assert claim(client, db, "queen-st-grocer", attempt="att_q", held=50, returned=30, forwarded_qty=40,
                 forwarded_to_name="Corner Mart")["accepted"]
    return claim(client, db, "corner-mart", attempt="att_cp", received_qty=25, **raw)


def test_checks_parse_the_closed_answers():
    assert checks({"batch_seen": " demo-7 ", "sold_source": "v2.4", "block_test": "Yes"}) == {
        "batch_seen": "B-4471", "sold_source": "2.4", "block_test": True}
    assert checks({"sold_source": 2.5, "block_test": False}) == {"sold_source": "2.5", "block_test": False}
    assert checks({"sold_source": "version 2 . 5", "block_test": "no"}) == {"sold_source": "2.5",
                                                                                "block_test": False}
    assert checks({"held": 3}) == {}


def test_an_owner_books_installed_with_its_checks_and_a_fix_check(client, db, recall, assert_balance):
    res = _corner(client, db, on_shelf=25, batch_seen="demo 7", sold_source="2.4", block_test=False)
    assert res["accepted"] and res["open_units"] == 160          # 120 not called yet + 15 disputed + 25 on_shelf
    cp = party(db, "corner-mart")
    assert (cp["on_shelf"], cp["batch_seen"], cp["sold_source"], cp["block_test"]) == (25, "B-4471", "2.4", False)
    assert cp["state"] == "claimed"                              # on_shelf and not fixed: not reconciled
    sim = get_clock(db, RID).sim_now(server_now(client))
    assert sim + REMOVAL_CHECK_H - 1 < cp["removal_check_at"] <= sim + REMOVAL_CHECK_H + 1
    assert_balance(RID)


def test_2_a_claim_of_fixed_on_an_affected_version_is_refused_with_the_two_values(client, db, recall, assert_balance):
    res = _corner(client, db, held=25, batch_seen="B-4471", sold_source="2.4", block_test=True)
    assert not res["accepted"] and res["code"] == "version_affected"
    assert res["reason"] == "fixed 25, sold_source 2.4 is affected"
    assert res["values"] == {"held": 25, "sold_source": "2.4"}
    assert res["atlas_error"]["code"] == 121 and res["code"] in CHALLENGE_CODES
    assert "2.3" not in res["message"]                         # the agent never hears the affected list
    assert party(db, "corner-mart")["held"] == 0
    assert_balance(RID)


@pytest.mark.parametrize("raw, code, reason", [
    ({"held": 25}, "fixed_needs_evidence", "fixed 25, sold_source missing"),
    ({"held": 25, "sold_source": "2.5"}, "sounder_not_confirmed", "fixed 25, block_test missing"),
    ({"held": 25, "sold_source": "2.5", "block_test": "no"}, "sounder_not_confirmed", "fixed 25, block_test no"),
    ({"on_shelf": 25, "batch_seen": "DEMO-9"}, "model_mismatch", "batch_seen DEMO-9, recalled model B-4471"),
])
def test_every_missing_piece_of_evidence_is_named(client, db, recall, assert_balance, raw, code, reason):
    res = _corner(client, db, **raw)
    assert (res["accepted"], res["code"], res["reason"]) == (False, code, reason)
    assert res["atlas_error"]["code"] == 121
    assert_balance(RID)


def test_fixed_with_the_fixed_version_and_a_sounder_that_rang_settles_the_party(client, db, recall, assert_balance):
    assert _corner(client, db, on_shelf=25, sold_source="2.4", block_test=False)["accepted"]
    fixed = claim(client, db, "corner-mart", attempt="att_cp2", received_qty=25, held=25,
                  sold_source="2.5", block_test=True)
    assert fixed["accepted"] and fixed["open_units"] == 135       # 120 not called yet + the dispute
    cp = party(db, "corner-mart")
    assert (cp["on_shelf"], cp["held"], cp["removal_check_at"]) == (0, 25, None)
    assert_balance(RID)


def test_a_check_the_claim_leaves_out_keeps_its_earlier_answer(client, db, recall, assert_balance):
    assert _corner(client, db, on_shelf=25, batch_seen="B-4471", sold_source="2.5", block_test=True)["accepted"]
    # A later count without the checks still rests on them: fixed is allowed.
    res = claim(client, db, "corner-mart", attempt="att_cp3", received_qty=25, held=25)
    assert res["accepted"]
    assert (party(db, "corner-mart")["sold_source"], party(db, "corner-mart")["held"]) == ("2.5", 25)
    assert_balance(RID)


def test_a_connected_panel_without_a_pass_keeps_its_panel_installed(client, db, recall, assert_balance):
    assert _corner(client, db, on_shelf=25, sold_source="2.4", block_test=False)["accepted"]
    db.parties.update_one({"recall_id": RID, "party_id": "corner-mart"}, {"$set": {"system_on_shelf": 1}})
    res = claim(client, db, "corner-mart", attempt="att_cp4", received_qty=25, held=25, sold_source="2.5",
                block_test=True)
    assert (res["accepted"], res["code"]) == (False, "panel_not_fixed")
    assert res["values"] == {"on_shelf": 0, "system_on_shelf": 1}
    # The earlier accepted answers (2.4, no sound) cannot carry a fixed tin either.
    assert claim(client, db, "corner-mart", attempt="att_cp4", received_qty=25, on_shelf=1,
                 held=24)["code"] == "version_affected"
    ok = claim(client, db, "corner-mart", attempt="att_cp4", received_qty=25, on_shelf=1, held=24,
               sold_source="2.5", block_test=True)
    assert ok["accepted"]
    assert_balance(RID)
