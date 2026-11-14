"""The tool gate (V2 §6), spec tests 25 to 28, and its other rules.

25 wrong party     a call on a party another worker leased is refused on `lease`
26 wrong tool      a contact run calling close_recall is refused on `allowlist`
27 double send     the same effect twice: one attempt (one hold), the second
                   call returns the stored outcome
28 false success   `verified` with no read-back is refused by Atlas (code 121);
                   claim_accepted with no accepted claim is `unsupported_result`
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest
from pymongo.errors import WriteError

from levela.harness import budget, execute, gate, result, saboteur, start_run, trace
from levela.harness.decide import RuleDecider
from levela.harness.verify import mark_verified, operation_verified
from test_harness import RID, W2, W5, engine, feed_texts, lease, make_distributor, set_party, world  # noqa: F401

VALIDATION_FAILED = 121


def new_run(world, party, worker=W2, *, epoch=None, kind="contact_party", **kw):
    return start_run(world.ctx, party, worker_id=worker, lease_epoch=party["lease_epoch"] if epoch is None else epoch,
                     kind=kind, decider_name="test", **kw)


def refusals(db, run_id):
    return [t["decision"] for t in trace.load(db, run_id) if t["decision"] and not t["decision"]["allowed"]]


# ---------------------------------------------------------------- test 25

def test_25_the_saboteur_calling_a_party_it_did_not_lease_is_refused_on_lease(db, world):
    lease(db, "queen-st-grocer", W2, epoch=9)
    out = saboteur.run_once(world.ctx, "wrong_party", worker_id=W5)

    assert out["party_id"] == "queen-st-grocer"
    assert out["decision"]["allowed"] is False and out["decision"]["rule"] == "lease"
    assert feed_texts(db, "gate_refused") == [
        "REFUSED w5 (saboteur): place_call on queen-st-grocer. Rule: lease. Holder is w2, epoch 9"]
    assert refusals(db, out["run_id"])[0]["rule"] == "lease"
    assert db.attempts.count_documents({}) == 0 and world.voice.count() == 0
    run = db.task_runs.find_one({"_id": out["run_id"]})
    assert run["status"] == "refused" and run["failure_class"] == "gate_refused" and run["used"]["tool_calls"] == 0
    party = db.parties.find_one({"_id": f"{RID}:queen-st-grocer"})
    assert party["lease_owner"] == W2 and party["lease_epoch"] == 9


def test_an_expired_lease_is_refused_even_with_the_right_epoch(db, world):
    party, _fence = lease(db, "harbour-market", seconds=-1)
    decision = gate.call(world.ctx, new_run(world, party), "place_call", {})
    assert not decision.allowed and decision.rule == "lease" and decision.reason.startswith("Lease expired")
    assert world.voice.count() == 0


# ---------------------------------------------------------------- test 26

def test_26_a_contact_run_calling_close_recall_is_refused_on_allowlist(db, world):
    party, _fence = lease(db, "harbour-market")
    run = new_run(world, party)
    decision = gate.call(world.ctx, run, "close_recall", {})

    assert not decision.allowed and decision.rule == "allowlist"
    assert "close_recall is not in contact_party's tools" in decision.reason
    assert db.recalls.find_one({"_id": RID})["status"] == "open"
    assert refusals(db, run.id)[0]["rule"] == "allowlist"
    assert feed_texts(db, "gate_refused")[0].startswith("REFUSED w2: close_recall on harbour-market. Rule: allowlist.")
    assert db.task_runs.find_one({"_id": run.id})["used"]["tool_calls"] == 0


def test_close_recall_is_in_no_kind_of_run(db):
    assert all("close_recall" not in tools for tools in budget.RUN_TOOLS.values())


def test_a_run_that_has_ended_may_not_call_anything(db, world):
    party, _fence = lease(db, "harbour-market")
    run = new_run(world, party)
    db.task_runs.update_one({"_id": run.id}, {"$set": {"status": "stopped"}})
    decision = gate.call(world.ctx, run, "place_call", {})
    assert not decision.allowed and decision.rule == "run" and world.voice.count() == 0


# ---------------------------------------------------------------- test 27

def test_27_the_same_call_twice_is_one_attempt_and_the_second_returns_the_stored_outcome(db, world):
    party, _fence = lease(db, "lakeside-foods")
    run = new_run(world, party)
    first = gate.call(world.ctx, run, "place_call", {})
    second = gate.call(world.ctx, run, "place_call", {})

    assert first.allowed and not first.duplicate and second.allowed and second.duplicate
    assert second.outcome["attempt_id"] == first.outcome["attempt_id"] == first.evidence_id
    assert second.outcome["state"] == "completed"          # what Atlas holds now, not what was sent
    assert db.attempts.count_documents({"party_id": "lakeside-foods"}) == 1
    assert world.voice.count("lakeside-foods") == 1
    assert db.task_runs.find_one({"_id": run.id})["used"]["tool_calls"] == 2


@pytest.fixture
def recording_order_system(monkeypatch):
    """An order system that keeps its own log of holds by Idempotency-Key (what
    GET /_log/holds returns), behind a stand-in for
    levela.systems.operations.execute (send, read back, record)."""
    log: dict[str, list] = {}

    def perform(ctx, op):
        log.setdefault(op["idempotency_key"], []).append(op["_id"])
        ctx.db.operations.update_one({"_id": op["_id"]}, {"$set": {"state": "sent", "sent_at": 9.0}})
        mark_verified(ctx.db, op["_id"], readback_value={"hold_id": "H-1", "status": "on_hold"}, readback_at=9.1)

    from levela.systems import operations as real_operations

    def execute(ctx, op, *, fresh=False):
        perform(ctx, op)
        return {"state": ctx.db.operations.find_one({"_id": op["_id"]})["state"]}

    # The real intent_for builds the operation; only carrying it out is faked.
    monkeypatch.setattr(real_operations, "execute", execute)
    return log


def test_27_a_hold_placed_twice_is_one_operation_and_one_hold_in_the_order_system_log(db, world, recording_order_system):
    make_distributor(db)
    party, _fence = lease(db, "dist-north")
    first_run = new_run(world, party, kind="order_system_sync")
    first = gate.call(world.ctx, first_run, "place_hold", {})
    # A later run (another day, another worker epoch) asks again: same key.
    party, _fence = lease(db, "dist-north")
    second = gate.call(world.ctx, new_run(world, party, kind="order_system_sync"), "place_hold", {})

    assert first.outcome["state"] == "verified" and second.duplicate and second.outcome["op_id"] == first.evidence_id
    assert db.operations.count_documents({"kind": "place_hold"}) == 1
    assert recording_order_system == {f"{RID}:dist-north:place_hold": [first.evidence_id]}


def test_27_against_the_mock_order_system_the_order_system_log_shows_one_hold(db, world):
    """The same double send against the real mock order system (in-process through
    its ASGI app): one operation, and the order system's own /_log/holds has one
    hold for the key."""
    import httpx
    from test_mock_order_system import NORTH, TOKEN

    from levela.mock.order_system import create_order_system
    from levela.systems.order_client import OrderClient

    batch = db.recalls.find_one({"_id": RID})["batch_code"]
    app = create_order_system("north", NORTH, batch=batch, seconds_per_sim_hour=0.05, admin_token=TOKEN)
    world.ctx.order_systems["north"] = OrderClient(transport=httpx.ASGITransport(app=app), timeout=2.0, name="north")
    make_distributor(db)
    for _ in range(2):
        party, _fence = lease(db, "dist-north")
        gate.call(world.ctx, new_run(world, party, kind="order_system_sync"), "place_hold", {})
    key = f"{RID}:dist-north:place_hold"
    log = app.state.order_system.log_view()
    assert db.operations.count_documents({"kind": "place_hold"}) == 1
    assert [row["idempotency_key"] for row in log].count(key) == 1


# ---------------------------------------------------------------- test 28

def test_28_verified_without_a_read_back_is_refused_by_atlas(db):
    op = {"_id": "op-t28", "recall_id": RID, "kind": "place_hold", "party_id": "dist-north",
          "idempotency_key": f"{RID}:dist-north:place_hold", "state": "sent", "readback_at": None,
          "readback_value": None}
    db.operations.insert_one(op)
    with pytest.raises(WriteError) as refused:
        db.operations.update_one({"_id": "op-t28"}, {"$set": {"state": "verified"}})
    assert refused.value.code == VALIDATION_FAILED
    with pytest.raises(WriteError) as forged:
        db.operations.insert_one({**op, "_id": "op-t28b", "idempotency_key": "k2", "state": "verified"})
    assert forged.value.code == VALIDATION_FAILED
    assert not operation_verified(db, "op-t28")
    # With the read-back that proves it, the same write is accepted.
    assert mark_verified(db, "op-t28", readback_value={"hold_id": "H-7781"}, readback_at=2.5)
    assert operation_verified(db, "op-t28")


def test_28_claim_accepted_with_no_accepted_claim_is_an_unsupported_result(db, world):
    party, fence = lease(db, "harbour-market")
    out = execute(world.ctx, party, worker_id=W5, fence=fence, decider=saboteur.SaboteurDecider("false_success"),
                  saboteur="false_success")

    assert out["status"] == "failed" and out["failure_class"] == "unsupported_result"
    assert out["result"]["claimed"]["outcome"] == "claim_accepted"
    assert "not supported by the facts" in out["result"]["rejected"]
    after = db.parties.find_one({"_id": party["_id"]})
    assert after["state"] == "pending" and after["ladder_step"] == 1      # nothing moved on
    assert after["next_action_at"] <= world.ctx.sim_now() and out["released"]   # due again
    assert db.episodes.find_one({"_id": f"ep_{out['run_id']}"})["failure_class"] == "unsupported_result"
    assert any("REJECTED w5 (saboteur): result claim_accepted for harbour-market is unsupported_result" in t
               for t in feed_texts(db, "run_ended"))


def test_28_evidence_from_another_run_does_not_support_a_result(db, world):
    party, fence = lease(db, "lakeside-foods")
    done = execute(world.ctx, party, worker_id=W2, fence=fence, decider=RuleDecider())
    party, _fence = lease(db, "lakeside-foods")
    other = new_run(world, party)
    facts = result.snapshot(db, other)
    borrowed = result.make("claim_accepted", done["result"]["evidence"])
    ok, why = result.check(facts, borrowed)
    assert not ok and "is not this run's" in why
    assert result.check(result.snapshot(db, done_run(db, done)), borrowed) == (True, "supported")
    assert result.problems({"outcome": "done!", "evidence": "att_1"}) == [
        "outcome 'done!' is not one of " + ", ".join(result.OUTCOMES), "evidence must be a list of ids"]


def done_run(db, out):
    return budget.TaskRun(db.task_runs.find_one({"_id": out["run_id"]}))


# --------------------------------------------------------- other gate rules

def test_a_second_live_attempt_is_refused_at_intent(db, world):
    world.voice.script["queen-st-grocer"] = ["hang"]
    party, _fence = lease(db, "queen-st-grocer")
    assert gate.call(world.ctx, new_run(world, party), "place_call", {}).allowed
    party, _fence = lease(db, "queen-st-grocer")
    decision = gate.call(world.ctx, new_run(world, party), "place_call", {})
    assert not decision.allowed and decision.rule == "intent" and "still dialed" in decision.reason
    assert world.voice.count() == 1


def test_a_record_tool_writes_inside_the_gate_and_counts_as_a_step(db, world):
    party, _fence = lease(db, "harbour-market")
    run = new_run(world, party)
    decision = gate.call(world.ctx, run, "schedule_callback", {"at": 33.0}, sim_now=10.0)
    assert decision.allowed and decision.outcome == {"callback_at": 33.0, "label": "d1 09:00"}
    assert db.task_runs.find_one({"_id": run.id})["callback_at"] == 33.0
    assert db.runs.find_one({"_id": f"run:{RID}"})["steps"] == 1


def test_the_budget_is_counted_by_atlas_and_refuses_at_its_limit(db, world):
    party, _fence = lease(db, "harbour-market")
    run = new_run(world, party)
    db.task_runs.update_one({"_id": run.id}, {"$set": {"used.tool_calls": 6}})
    decision = gate.call(world.ctx, run, "schedule_callback", {"at": 40.0})
    assert not decision.allowed and decision.rule == "budget"
    assert decision.reason == "budget_exceeded after 6 tool calls"
    assert "callback_at" not in db.task_runs.find_one({"_id": run.id})       # the refused write never happened
    assert feed_texts(db, "gate_refused") == []                                # the run reports the stop itself


def test_refusals_name_the_worker_the_saboteur_and_the_holder(db, world):
    party, _fence = lease(db, "harbour-market")
    assert gate.who(new_run(world, party)) == "w2"
    assert gate.who(new_run(world, party, saboteur="loop")) == "w2 (saboteur)"
    set_party(db, "harbour-market", lease_owner=None)
    decision = gate.call(world.ctx, new_run(world, party), "place_call", {})
    assert decision.reason == f"No holder (epoch {party['lease_epoch']})"
