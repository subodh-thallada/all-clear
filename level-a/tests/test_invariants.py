"""The four run invariants (V2 §9) and how a check is recorded on the run.

Each check is made to pass and then to fail on purpose. Check 1 needs a
database without the one-live-attempt index (Atlas would refuse the second
attempt), so it runs on a database of its own.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from levela.clock import get_clock, server_now
from levela.db.seed import load_scenario, start_recall
from levela.ledger import invariants
from levela.ledger.claims import submit_claim
from levela.runs import create_run, get_run

RID = "RCL-2026-017"
ALL_PASS = [True, True, True, True]


@pytest.fixture
def recall(client, db, assert_balance):
    """recall_1 with a form claim (the ledger logs its evidence) and a
    simulated call whose tool event is logged as POST /tools/submit_count does."""
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, emit=False)
    db.tool_events.insert_one({"kind": "tool_call", "attempt_id": "att_lake", "party_id": "lakeside-systems",
                               "recall_id": RID, "payload": {}, "received_at": server_now(client)})
    for party_id, attempt, source, raw in (("lakeside-systems", "att_lake", "sim", {"in_stock": 20, "returned": 60}),
                                           ("harbour-fire", "att_form", "form", {"in_stock": 40})):
        assert submit_claim(client, db, recall_id=RID, party_id=party_id, attempt_id=attempt, source=source,
                            raw=raw)["accepted"]
        assert_balance(RID)
    return RID


def test_all_four_hold_on_a_clean_run(client, db, recall):
    db.attempts.insert_one({"_id": "att_q", "recall_id": RID, "party_id": "queen-st-alarm", "seq": 1,
                            "state": "dialed"})
    logs = {"north": [{"idempotency_key": "RCL:dist-north:place_hold", "hold_id": "H1"}], "east": []}
    result = invariants.check(db, RID, logs)
    assert result["passed"] == ALL_PASS
    assert result["at"] == pytest.approx(get_clock(db, RID).sim_now(server_now(client)), abs=0.5)
    d = result["details"]
    assert d["mass_balance"] == {"balance": 240, "units_total": 240, "parties": 240, "edges": 0}
    assert (d["order_systems_checked"], d["duplicates"], d["duplicate_effects"]) == (["east", "north"], 0, [])
    assert invariants.describe(result) == "4 of 4 hold"


def test_a_claim_without_a_tool_event_fails_check_3(client, db, recall):
    submit_claim(client, db, recall_id=RID, party_id="queen-st-alarm", attempt_id="att_ghost", source="voice",
                 raw={"in_stock": 120})
    result = invariants.check(db, RID, {})
    assert result["passed"] == [True, True, False, True]
    [row] = result["details"]["claims_without_evidence"]
    assert (row["party_id"], row["attempt_id"], row["source"]) == ("queen-st-alarm", "att_ghost", "voice")
    assert isinstance(row["claim_id"], str)
    assert "1 accepted claim(s) without a tool event" in invariants.describe(result)
    # A post-call event is not evidence of the numbers; the tool call is.
    db.tool_events.insert_one({"kind": "post_call", "attempt_id": "att_ghost"})
    assert invariants.check(db, RID, {})["passed"][2] is False
    db.tool_events.insert_one({"kind": "tool_call", "attempt_id": "att_ghost"})
    assert invariants.check(db, RID, {})["passed"] == ALL_PASS


def test_a_write_around_the_ledger_fails_check_4(db, recall):
    # Each party still conserves, so no validator objects; the recall does not.
    db.parties.update_one({"recall_id": RID, "party_id": "queen-st-alarm"},
                         {"$inc": {"shipped_in": 1, "unresolved": 1}})
    result = invariants.check(db, RID, {}, at=158.0)
    assert result["at"] == 158.0 and result["passed"] == [True, True, True, False]
    assert result["details"]["mass_balance"]["balance"] == 241
    assert invariants.describe(result).endswith("mass balance 241 != units_total 240")


def test_two_holds_under_one_key_fail_check_2(db, recall):
    logs = {
        "north": [{"idempotency_key": "k1", "hold_id": "H2"}, {"idempotency_key": "k1", "hold_id": "H1"},
                  {"key": "k2", "hold_id": "H3"}, {"hold_id": "H4"}],
        "east": None,                                                 # its log could not be read
        "west": {"k9": [{"hold_id": "H9"}], "k8": {"hold_id": "H8"}},  # already grouped by key
    }
    result = invariants.check(db, RID, logs)
    assert result["passed"] == [True, False, True, True]
    d = result["details"]
    assert d["duplicate_holds"] == [{"order_system": "north", "key": "k1", "holds": 2, "hold_ids": ["H1", "H2"]}]
    assert (d["order_systems_checked"], d["order_systems_unreadable"], d["holds_without_key"]) == (["north", "west"], ["east"], 1)
    assert d["duplicate_effects"] == ["hold:north:k1:H2"] and d["duplicates"] == 1
    assert "duplicate holds: north k1 (2 holds)" in invariants.describe(result)


def test_an_unreadable_order_system_log_leaves_check_2_unknown_not_held(db, recall):
    """Nothing was checked against an order system whose log could not be read, so
    the wall must not show 4 of 4; but unknown is not a failure: it neither
    sets the sticky invariant_failed nor stops the supervisor."""
    result = invariants.check(db, RID, {"east": None, "north": []})
    assert result["passed"] == [True, None, True, True]
    assert result["details"]["order_systems_unreadable"] == ["east"] and result["details"]["order_systems_checked"] == ["north"]
    assert invariants.describe(result) == "3 of 4 hold, 1 unknown (East orders unreadable)"
    assert not invariants.failed(result)
    assert invariants.record(db, RID, result) == {"duplicates": 0, "invariant_failed": False}
    assert get_run(db, RID)["last_invariant_check"]["passed"] == [True, None, True, True]
    # Every log read and clean: held. No order systems at all (recall_1): held.
    assert invariants.check(db, RID, {"east": [], "north": []})["passed"] == ALL_PASS
    assert invariants.check(db, RID, {})["passed"] == ALL_PASS
    # A duplicate in a log that was read fails it, unread logs or not.
    dup = [{"idempotency_key": "k", "hold_id": "H1"}, {"idempotency_key": "k", "hold_id": "H2"}]
    assert invariants.check(db, RID, {"east": None, "north": dup})["passed"][1] is False


def test_two_live_attempts_for_one_party_fail_check_1(fresh_db):
    fresh_db.recalls.insert_one({"_id": RID, "batch_code": "B-4471", "units_total": 0, "open_units": 0,
                                 "status": "open"})
    fresh_db.attempts.insert_many([
        {"_id": "att_1", "recall_id": RID, "party_id": "harbour-fire", "seq": 1, "state": "dialed"},
        {"_id": "att_2", "recall_id": RID, "party_id": "harbour-fire", "seq": 2, "state": "intent"},
        {"_id": "att_3", "recall_id": RID, "party_id": "harbour-fire", "seq": 3, "state": "completed"},
        {"_id": "att_4", "recall_id": RID, "party_id": "lakeside-systems", "seq": 1, "state": "connected"},
    ])
    result = invariants.check(fresh_db, RID, {}, at=10.0)
    assert result["passed"] == [False, True, True, True]
    assert result["details"]["live_attempts"] == [{"party_id": "harbour-fire", "attempts": ["att_1", "att_2"]}]
    assert result["details"]["duplicate_effects"] == ["attempt:att_2"]
    assert invariants.describe(result) == "3 of 4 hold. FAILED: two live attempts at harbour-fire"


def test_an_unknown_recall_cannot_be_checked(db):
    with pytest.raises(ValueError, match="unknown recall"):
        invariants.check(db, "RCL-none", {})


# ---- record ---------------------------------------------------------------------

def _result(at, effects=(), passed=ALL_PASS):
    return {"at": at, "passed": list(passed), "details": {"duplicate_effects": list(effects),
                                                          "duplicates": len(effects)}}


def test_record_counts_each_duplicate_once_and_keeps_a_failure(db):
    create_run(db, RID, real_start=datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc))
    db.runs.update_one({"_id": f"run:{RID}"}, {"$set": {"planner_wake": False}})
    assert invariants.record(db, RID, _result(10.0)) == {"duplicates": 0, "invariant_failed": False}
    assert get_run(db, RID)["last_invariant_check"] == {"at": 10.0, "passed": ALL_PASS,
                                                       "details": {"duplicate_effects": [], "duplicates": 0}}
    assert get_run(db, RID)["planner_wake"] is False

    bad = _result(11.0, ["hold:north:k1:H2"], [True, False, True, True])
    assert invariants.record(db, RID, bad) == {"duplicates": 1, "invariant_failed": True}
    assert invariants.record(db, RID, _result(12.0, ["hold:north:k1:H2"], bad["passed"]))["duplicates"] == 1
    more = _result(13.0, ["hold:north:k1:H2", "attempt:att_9"], [False, False, True, True])
    assert invariants.record(db, RID, more)["duplicates"] == 2
    # A later clean check does not clear the failure: the supervisor stays stopped.
    assert invariants.record(db, RID, _result(14.0)) == {"duplicates": 2, "invariant_failed": True}
    run = get_run(db, RID)
    assert run["invariant_failed_at"] == 11.0 and run["planner_wake"] is True
    assert run["planner_wake_reason"] == "invariant_failed"
    assert run["last_invariant_check"]["at"] == 14.0 and run["last_invariant_check"]["passed"] == ALL_PASS
    assert sorted(run["duplicate_effects"]) == ["attempt:att_9", "hold:north:k1:H2"]


def test_record_without_a_run(db):
    assert invariants.record(db, "RCL-none", _result(1.0)) is None


def test_the_mock_order_systems_own_log_shape_is_read_row_by_row(db, recall):
    """/_log/holds answers {"order_system": name, "holds": [rows]}: distinct keys
    must not be mistaken for one key called "holds" (a false duplicate would
    turn the wall red and stop the supervisor)."""
    rows = [{"idempotency_key": f"{recall}:dist-north:place_hold", "hold_id": "H-1"},
            {"idempotency_key": f"{recall}:north-a:place_hold", "hold_id": "H-2"}]
    result = invariants.check(db, recall, {"north": {"order_system": "north", "holds": rows}})
    assert result["passed"][1] is True
    rows.append({"idempotency_key": rows[0]["idempotency_key"], "hold_id": "H-3"})
    result = invariants.check(db, recall, {"north": {"order_system": "north", "holds": rows}})
    assert result["passed"][1] is False
