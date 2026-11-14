"""Spec test 6 (one live attempt per party) and every attempt state transition.

The refusal is Atlas's rule, so it is asserted on Atlas's own duplicate-key
message. Every transition is a conditional update, so each test also checks
that a state the API, the worker or recovery already moved on never regresses.
"""
from __future__ import annotations

import pytest

from levela.db.seed import load_scenario, start_recall
from levela.orchestrator import attempts as A
from levela.orchestrator.attempts import AttemptRefused

RID, SID = "RCL-2026-017", "harbour-fire"
LIVE = ["intent", "dialed", "connected"]
CLOSING = ["completed", "no_answer", "failed"]


@pytest.fixture
def recall(client, db):
    start_recall(client, db, load_scenario("recall_1"), all_sim=True, env={}, emit=False)
    return RID


def _open(db, party_id=SID, **kw):
    return A.open_attempt(db, recall_id=RID, party_id=party_id, channel="sim", **kw)


def _state(db, attempt_id):
    return A.get(db, attempt_id)["state"]


def _put_in(db, attempt_id, state):
    """Walk a fresh attempt to `state` through the real transitions."""
    if state in ("dialed", "connected"):
        assert A.mark_dialed(db, attempt_id, conversation_id="conv_1", call_sid="CA1")
    if state == "connected":
        assert A.mark_connected(db, attempt_id)
    if state in CLOSING:
        assert A.complete(db, attempt_id, state)
    if state == "abandoned":
        assert A.abandon(db, attempt_id, "no evidence of a call")


# -- spec test 6 -------------------------------------------------------------

def test_second_live_attempt_is_refused_with_atlas_duplicate_key_error(db, recall):
    first = _open(db)
    with pytest.raises(AttemptRefused) as info:
        _open(db)
    exc = info.value
    assert exc.index == "one_live_attempt_per_party"
    assert exc.message.startswith("E11000 duplicate key error")
    assert "index: one_live_attempt_per_party" in exc.message
    assert exc.existing["_id"] == first["_id"]
    assert db.attempts.count_documents({"recall_id": RID, "party_id": SID}) == 1


@pytest.mark.parametrize("state", LIVE)
def test_every_live_state_blocks_a_second_attempt(db, recall, state):
    first = _open(db)
    _put_in(db, first["_id"], state)
    with pytest.raises(AttemptRefused) as info:
        _open(db)
    assert info.value.index == "one_live_attempt_per_party"


def test_next_attempt_opens_with_seq_2_once_the_first_ends(db, recall):
    first = _open(db)
    assert first["seq"] == 1
    assert A.complete(db, first["_id"], "no_answer")
    second = _open(db)
    assert second["seq"] == 2
    assert A.live_attempt(db, RID, SID)["_id"] == second["_id"]


@pytest.mark.parametrize("state", CLOSING + ["abandoned"])
def test_any_final_state_frees_the_party(db, recall, state):
    first = _open(db)
    _put_in(db, first["_id"], state)
    assert _open(db)["seq"] == 2


def test_a_pinned_seq_that_was_taken_is_refused(db, recall):
    """A stale plan (seq already used) must not dial, even after that attempt ended."""
    first = _open(db, seq=1)
    assert A.complete(db, first["_id"], "completed")
    with pytest.raises(AttemptRefused) as info:
        _open(db, seq=1)
    assert info.value.index == "uniq_recall_party_seq"
    assert "E11000 duplicate key error" in info.value.message
    assert _open(db, seq=2)["seq"] == 2


def test_other_parties_are_independent(db, recall):
    _open(db)
    other = _open(db, party_id="lakeside-systems")
    assert other["seq"] == 1


def test_opening_moves_a_pending_party_to_contacting(db, recall):
    _open(db)
    assert db.parties.find_one({"_id": f"{RID}:{SID}"})["state"] == "contacting"
    db.parties.update_one({"_id": f"{RID}:{SID}"}, {"$set": {"state": "claimed"}})
    A.complete(db, A.live_attempt(db, RID, SID)["_id"], "completed")
    _open(db)
    assert db.parties.find_one({"_id": f"{RID}:{SID}"})["state"] == "claimed"


def test_new_attempt_records_owner_number_and_context(db, recall):
    att = _open(db, owner={"worker": "w", "boot": "b"}, to_number="+14165550141",
                context={"attempt_id": "x"}, extra={"dispute_key": "k"})
    row = A.get(db, att["_id"])
    assert row["state"] == "intent" and row["dial_confirmed"] is False
    assert row["owner"] == {"worker": "w", "boot": "b"}
    assert row["to_number"] == "+14165550141"
    assert row["context"] == {"attempt_id": "x"} and row["dispute_key"] == "k"
    assert row["conversation_id"] is None and row["call_sid"] is None


# -- transitions ---------------------------------------------------------------

def test_mark_dialed_moves_intent_to_dialed_and_confirms(db, recall):
    att = _open(db)
    assert A.mark_dialed(db, att["_id"], conversation_id="conv_1", call_sid="CA1")
    row = A.get(db, att["_id"])
    assert row["state"] == "dialed" and row["dial_confirmed"] is True
    assert (row["conversation_id"], row["call_sid"]) == ("conv_1", "CA1")
    assert row["dialed_at"] is not None


@pytest.mark.parametrize("state", ["connected", *CLOSING])
def test_mark_dialed_after_the_call_moved_on_only_fills_ids(db, recall, state):
    """The post-call webhook can finish a call before the worker saves the
    provider's answer: the state stays, the identifiers are filled in."""
    att = _open(db)
    if state == "connected":
        A.transition(db, att["_id"], "connected")
    else:
        A.complete(db, att["_id"], state)
    assert not A.mark_dialed(db, att["_id"], conversation_id="conv_9", call_sid="CA9")
    row = A.get(db, att["_id"])
    assert row["state"] == state
    assert (row["conversation_id"], row["call_sid"]) == ("conv_9", "CA9")
    assert row["dial_confirmed"] is True


def test_mark_dialed_never_overwrites_known_ids(db, recall):
    att = _open(db)
    A.mark_dialed(db, att["_id"], conversation_id="conv_1", call_sid="CA1")
    assert not A.mark_dialed(db, att["_id"], conversation_id="conv_2", call_sid="CA2")
    row = A.get(db, att["_id"])
    assert (row["conversation_id"], row["call_sid"]) == ("conv_1", "CA1")


def test_mark_connected_moves_dialed_to_connected(db, recall):
    att = _open(db)
    A.mark_dialed(db, att["_id"])
    assert A.mark_connected(db, att["_id"], call_sid="CA1")
    row = A.get(db, att["_id"])
    assert row["state"] == "connected" and row["call_sid"] == "CA1"


def test_mark_connected_leaves_an_in_doubt_attempt_to_recovery(db, recall):
    att = _open(db)
    assert not A.mark_connected(db, att["_id"], conversation_id="conv_1", call_sid="CA1")
    row = A.get(db, att["_id"])
    assert row["state"] == "intent" and A.in_doubt(row)
    assert row["call_sid"] == "CA1"


@pytest.mark.parametrize("start", LIVE)
@pytest.mark.parametrize("end", CLOSING)
def test_complete_closes_from_every_live_state(db, recall, start, end):
    att = _open(db)
    _put_in(db, att["_id"], start)
    assert A.complete(db, att["_id"], end, reason="because")
    row = A.get(db, att["_id"])
    assert row["state"] == end and row["outcome_reason"] == "because" and row["ended_at"] is not None


@pytest.mark.parametrize("final", CLOSING + ["abandoned"])
@pytest.mark.parametrize("target", ["dialed", "connected", *CLOSING, "abandoned"])
def test_a_final_state_never_regresses(db, recall, final, target):
    att = _open(db)
    _put_in(db, att["_id"], final)
    if target in CLOSING:
        moved = A.complete(db, att["_id"], target)
    elif target == "abandoned":
        moved = A.abandon(db, att["_id"], "late")
    else:
        moved = A.transition(db, att["_id"], target)
    assert not moved
    assert _state(db, att["_id"]) == final


def test_connected_never_goes_back_to_dialed(db, recall):
    att = _open(db)
    _put_in(db, att["_id"], "connected")
    assert not A.transition(db, att["_id"], "dialed")
    assert _state(db, att["_id"]) == "connected"


def test_complete_refuses_a_non_closing_state(db, recall):
    att = _open(db)
    for bad in ("abandoned", "dialed", "intent"):
        with pytest.raises(ValueError):
            A.complete(db, att["_id"], bad)


def test_abandon_works_from_intent_and_records_why(db, recall):
    att = _open(db)
    assert A.abandon(db, att["_id"], "no evidence of a call", by={"worker": "w", "boot": "b"})
    row = A.get(db, att["_id"])
    assert row["state"] == "abandoned" and row["outcome_reason"] == "no evidence of a call"
    assert row["abandoned_by"] == {"worker": "w", "boot": "b"}
    assert not A.in_doubt(row)


@pytest.mark.parametrize("state", ["dialed", "connected"])
def test_abandon_never_touches_a_call_that_was_placed(db, recall, state):
    att = _open(db)
    _put_in(db, att["_id"], state)
    assert not A.abandon(db, att["_id"], "no evidence")
    assert _state(db, att["_id"]) == state


def test_adopt_records_evidence_and_moves_the_state(db, recall):
    att = _open(db)
    assert A.adopt(db, att["_id"], evidence="twilio", call_sid="CA7", state="dialed", by={"worker": "w"})
    row = A.get(db, att["_id"])
    assert row["state"] == "dialed" and row["call_sid"] == "CA7" and row["dial_confirmed"] is True
    assert row["adopted"]["evidence"] == "twilio" and row["adopted"]["by"] == {"worker": "w"}
    assert not A.in_doubt(row)


def test_adopt_into_a_final_state(db, recall):
    att = _open(db)
    assert A.adopt(db, att["_id"], evidence="sim probe", call_sid="CA8", state="no_answer")
    row = A.get(db, att["_id"])
    assert row["state"] == "no_answer" and row["outcome_reason"] == "adopted from sim probe"


def test_adopt_is_never_applied_to_an_abandoned_attempt(db, recall):
    att = _open(db)
    A.abandon(db, att["_id"], "no evidence")
    assert not A.adopt(db, att["_id"], evidence="tool_events", call_sid="CA1", state="connected")
    row = A.get(db, att["_id"])
    assert row["state"] == "abandoned" and row["call_sid"] is None and "adopted" not in row


def test_adopt_happens_once(db, recall):
    att = _open(db)
    assert A.adopt(db, att["_id"], evidence="tool_events", call_sid="CA1", state="connected")
    assert not A.adopt(db, att["_id"], evidence="twilio", call_sid="CA2", state="completed")
    row = A.get(db, att["_id"])
    assert row["adopted"]["evidence"] == "tool_events" and row["call_sid"] == "CA1"
    assert row["state"] == "connected"


def test_in_doubt_means_the_answer_was_never_saved(db, recall):
    att = _open(db)
    assert A.in_doubt(A.get(db, att["_id"]))
    A.complete(db, att["_id"], "completed")
    assert A.in_doubt(A.get(db, att["_id"])), "a webhook-closed call is still unconfirmed"
    assert not A.in_doubt({"channel": "form", "dial_confirmed": False, "state": "connected"})
    assert not A.in_doubt({"channel": "sim", "dial_confirmed": True, "state": "dialed"})
