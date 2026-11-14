"""The contact ladder (V2 §5): pure functions, checked against the table by hand.

    step 1 call -> +4 h (or the memory hour) | 2 call -> next day 09:00 |
    3 form link -> +24 h | 4 third call -> +48 h | 5 escalate -> no timer
"""
from __future__ import annotations

import pytest

from levela.clock import fmt
from levela.orchestrator import ladder


def party(step, **fields):
    return {"ladder_step": step, "state": "contacting", **fields}


@pytest.mark.parametrize("step, now, expected", [
    (1, 9.0, (2, 13.0, "place_call")),             # d0 09:00 -> +4 h
    (2, 13.0, (3, 33.0, "send_form_link")),        # d0 13:00 -> d1 09:00
    (3, 33.0, (4, 57.0, "place_call")),            # +24 h
    (4, 57.0, (5, 105.0, "escalate")),             # +48 h
    (5, 105.0, (5, None, None)),                   # nothing scheduled: the units stay unresolved
])
def test_each_unanswered_step_moves_on_at_the_tables_timer(step, now, expected):
    assert ladder.after(party(step), "no_answer", now) == expected


def test_a_delivered_form_link_waits_a_day_for_a_submission():
    assert ladder.after(party(3), "form_sent", 33.0) == (4, 57.0, "place_call")


def test_the_memory_hour_moves_the_second_call_to_that_hour():
    step, at, action = ladder.after(party(1), "no_answer", 9.25, memory_hour=14)
    assert (step, fmt(at), action) == (2, "d0 14:00", "place_call")
    # Past today's hour: tomorrow at that hour, never a minute later.
    assert fmt(ladder.after(party(1), "no_answer", 14.0, memory_hour=14)[1]) == "d1 14:00"
    # The party's own memory_hour is used when none is passed.
    assert fmt(ladder.after(party(1, memory_hour=16), "no_answer", 9.0)[1]) == "d0 16:00"


@pytest.mark.parametrize("step, now, expected", [
    (2, 14.0, (3, 33.0, "send_form_link")),
    (3, 33.0, (4, 57.0, "place_call")),
    (4, 57.0, (5, 105.0, "escalate")),
])
def test_the_memory_hour_moves_only_the_second_call(step, now, expected):
    """parties.memory_hour (learned from the party's own answered call in an
    earlier recall) times step 2 only; the later steps keep the table's timers."""
    assert ladder.after(party(step, memory_hour=14), "no_answer", now) == expected


def test_a_callback_keeps_the_step_and_sets_the_timer_to_the_requested_hour():
    assert ladder.after(party(2), "callback_requested", 30.0, callback_at=33.0) == (2, 33.0, "place_call")
    # A callback hour already past is due now, never in the past.
    assert ladder.after(party(2), "callback_requested", 40.0, callback_at=33.0) == (2, 40.0, "place_call")


@pytest.mark.parametrize("outcome, expected", [
    ("claim_accepted", (3, None, None)),
    ("escalated", (5, None, None)),
    ("hold_verified", (3, None, None)),
    ("accounts_read", (3, None, None)),
])
def test_settled_outcomes_end_the_ladder(outcome, expected):
    assert ladder.after(party(3), outcome, 50.0) == expected


def test_an_answer_ends_the_ladder_only_once_the_counts_are_on_the_books():
    assert ladder.after(party(1, state="reconciled"), "answer_recorded", 9.0) == (1, None, None)
    assert ladder.after(party(1, state="contacting"), "answer_recorded", 9.0) == (2, 13.0, "place_call")


def test_a_party_handed_to_a_person_gets_no_timer_whatever_happened():
    assert ladder.after(party(2, escalation="human"), "no_answer", 13.0) == (2, None, None)


def test_a_run_that_did_not_finish_leaves_the_party_due_again_on_the_same_step():
    assert ladder.after(party(2), "budget_exceeded", 20.0) == (2, 20.0, "place_call")


@pytest.mark.parametrize("raw, expected", [(None, 1), (0, 1), (3, 3), (9, 5), ("x", 1)])
def test_a_missing_or_odd_step_is_read_as_a_real_one(raw, expected):
    assert ladder.clamp(raw) == expected


def test_a_order_system_is_read_once_a_day_while_accounts_are_open():
    assert fmt(ladder.order_system_next(81.5, accounts_open=True)) == "d4 09:00"
    assert ladder.order_system_next(81.5, accounts_open=False) is None
