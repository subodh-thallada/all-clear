"""Spec tests 18 (order system change) and 19 (bad mapping refused), and the
repairers behind them (V2 §7 "Contract and repair").

18: after the switch only intent and sent operations become needs_reverify;
the repaired adapter passes the canary; only those operations retry, and the
verified ones keep their adapter version.
19: a wrong mapping fails the canary; no adapter is activated and order system work
stays paused.

The repairers are tested without a database or a network: the rules work
from the mock order system's own openapi.json, and the Claude repairer talks to an
httpx.MockTransport.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from levela.config import AGENT_DIR
from levela.orchestrator.planner import RuleDecider, snapshot
from levela.orchestrator.failpoints import Failpoints, WorkerCrash, arm as arm_failpoint, raise_crash
from levela.systems import adapters, canary
from levela.systems import operations as ops
from levela.systems.contract import ContractError
from levela.systems.repair import (PROPOSAL_SCHEMA, ClaudeRepairer, RepairError, RulesRepairer, ScriptedRepairer,
                                   default_repairer, request_repair)
from test_mock_order_system import BATCH, NORTH, V2_MAPPING, V2_PATHS, V2_PROPOSAL, arm, make_order_system, switch
from test_order_system_hold import RID, book_ledger, feed_texts, make_ctx, order_system_ops, start

V1 = adapters.builtin("north")
FAILURE = "GET /v1/accounts answered 410 Gone (API v1 was retired)"


def _failure_after_switch():
    """(app, order system client, ContractError) for an order system that just moved to v2."""
    app, order_system = make_order_system()
    switch(order_system)
    with pytest.raises(ContractError) as exc:
        order_system.list_accounts(BATCH, adapter=V1)
    return app, order_system, exc.value


def _reply(proposal, stop: str = "end_turn") -> dict:
    """A Messages API response carrying one JSON text block."""
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": json.dumps(proposal)}], "stop_reason": stop,
            "usage": {"input_tokens": 5120, "output_tokens": 240}}


# -- the repairers --------------------------------------------------------------------------

def test_the_rules_repairer_finds_the_v2_contract_in_openapi():
    _, order_system, err = _failure_after_switch()
    assert err.failure_text == FAILURE
    proposal = RulesRepairer().propose(err.failure_text, err.raw_body, order_system.openapi(), V1)
    assert proposal.paths == V2_PATHS
    assert proposal.mapping == V2_MAPPING
    assert (proposal.status_in_force, proposal.status_pending) == (["active"], ["pending_confirm"])
    assert proposal.defaults == {"reason_code": "recall"}
    assert (proposal.source, proposal.model_calls) == ("rules", 0)
    assert "qty_in_stock -> inventory.available" in proposal.rationale
    assert "a hold now needs POST /v2/holds/{hold_id}/confirm" in proposal.rationale


def test_the_rules_repairer_refuses_to_guess_between_equal_fields():
    _, order_system, err = _failure_after_switch()
    doc = copy.deepcopy(order_system.openapi())
    same = {"type": "integer", "description": "Units of the batch."}
    doc["components"]["schemas"]["InventoryV2"]["properties"] = {"bin_a": dict(same), "bin_b": dict(same)}
    with pytest.raises(RepairError, match="match equally well"):
        RulesRepairer().propose(err.failure_text, err.raw_body, doc, V1)


def test_the_claude_repairer_sends_one_structured_request_and_validates_the_answer():
    _, order_system, err = _failure_after_switch()
    openapi = order_system.openapi()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply(V2_PROPOSAL))

    repairer = ClaudeRepairer(api_key="sk-test", transport=httpx.MockTransport(handler), env={})
    proposal = repairer.propose(err.failure_text, err.raw_body, openapi, V1)
    (request,) = seen
    assert (request.url.host, request.url.path) == ("api.anthropic.com", "/v1/messages")
    assert request.headers["x-api-key"] == "sk-test"
    assert request.headers["anthropic-version"] == "2026-10-21"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": PROPOSAL_SCHEMA}
    assert body["system"] == (AGENT_DIR / "repair_prompt.md").read_text(encoding="utf-8")
    brief = body["messages"][0]["content"]
    assert FAILURE in brief and err.raw_body in brief and '"/v2/accounts"' in brief
    assert proposal.mapping == V2_MAPPING and proposal.paths == V2_PATHS
    assert proposal.defaults == {"reason_code": "recall"}
    assert (proposal.source, proposal.model_calls) == ("model", 1)
    assert proposal.usage == {"input_tokens": 5120, "output_tokens": 240}
    assert ClaudeRepairer(api_key="k", env={"REPAIR_MODEL": "claude-haiku-5-5"}).model == "claude-haiku-5-5"


def test_the_claude_repairer_never_guesses_past_a_refusal_or_bad_output():
    _, order_system, err = _failure_after_switch()
    openapi = order_system.openapi()

    def repairer(*responses):
        queue = list(responses)
        return ClaudeRepairer(api_key="k", env={}, transport=httpx.MockTransport(lambda _r: queue.pop(0)))

    refused = repairer(httpx.Response(200, json={**_reply({}, "refusal"), "stop_details": {"category": "cyber"}}))
    with pytest.raises(RepairError, match="declined") as exc:
        refused.propose(err.failure_text, err.raw_body, openapi, V1)
    assert exc.value.model_calls == 1
    wrong = repairer(httpx.Response(200, json=_reply({**V2_PROPOSAL, "status_in_force": []})))
    with pytest.raises(RepairError, match="status_in_force is empty"):
        wrong.propose(err.failure_text, err.raw_body, openapi, V1)
    overloaded = repairer(httpx.Response(529, headers={"retry-after": "0"}, json={"type": "error"}),
                          httpx.Response(200, json=_reply(V2_PROPOSAL)))
    assert overloaded.propose(err.failure_text, err.raw_body, openapi, V1).mapping == V2_MAPPING
    bad_request = repairer(httpx.Response(400, json={"type": "error", "error": {"message": "bad"}}))
    with pytest.raises(RepairError, match="answered 400"):
        bad_request.propose(err.failure_text, err.raw_body, openapi, V1)
    assert isinstance(default_repairer(env={}), RulesRepairer)
    assert isinstance(default_repairer(env={"ANTHROPIC_API_KEY": "k"}), ClaudeRepairer)


# -- spec test 18 ---------------------------------------------------------------------------

def test_18_only_open_operations_are_flagged_and_only_they_retry_under_the_repaired_adapter(
        client, db, settings, assert_balance):
    app, order_system = make_order_system("north")
    dist = start(client, db)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    ids = book_ledger(db, dist, NORTH)
    account = {a: db.parties.find_one({"recall_id": RID, "party_id": sid}) for a, sid in ids.items()}

    # Under v1: two reads verified, a hold the order system dropped (sent), two reads killed after their intent.
    done = [ops.read_inventory(ctx, account[a]) for a in ("N-009", "N-014")]
    assert [r["action"] for r in done] == ["verified", "verified"]
    arm(order_system, "accepted_then_dropped")
    assert ops.place_hold(ctx, dist)["state"] == "sent"
    dying = make_ctx(settings, client, db, ctx.order_systems, failpoints=Failpoints(db, crash=raise_crash))
    for a in ("N-021", "N-030"):
        arm_failpoint(db, "after_op_intent", party_id=ids[a])
        with pytest.raises(WorkerCrash):
            ops.read_inventory(dying, account[a])
    before = {op["_id"]: op for op in order_system_ops(db, state="verified")}

    # The switch: the next order system call meets a 410 and contains the damage.
    switch(order_system)
    trigger = ops.read_inventory(ctx, account["N-041"])
    assert trigger["action"] == "contract_failed"
    states = {op["party_id"] + ":" + op["kind"]: (op["state"], op.get("reverify_from")) for op in order_system_ops(db)}
    assert states == {
        f"{ids['N-009']}:read_inventory": ("verified", None), f"{ids['N-014']}:read_inventory": ("verified", None),
        "dist-north:place_hold": ("needs_reverify", "sent"),
        f"{ids['N-021']}:read_inventory": ("needs_reverify", "intent"),
        f"{ids['N-030']}:read_inventory": ("needs_reverify", "intent"),
        f"{ids['N-041']}:read_inventory": ("needs_reverify", "intent")}
    assert feed_texts(db, "order_system_contract_failed") == [f"contract failed: {FAILURE}. 4 operations flagged, 2 untouched"]
    assert db.parties.find_one({"_id": dist["_id"]})["order_system_paused"] is True
    assert db.adapters.find_one({"_id": "north:v1"})["last_failure"]["text"] == FAILURE

    # Repair (rules), canary, activate, retry.
    res = request_repair(ctx, "north", repairer=RulesRepairer())
    assert (res["status"], res["adapter_id"], res["source"], res["model_calls"]) == ("activated", "north:v2", "rules", 0)
    assert {k: res["canary"][k] for k in ("account", "ledger_value", "order_system_value", "passed", "basis")} == {
        "account": "N-009", "ledger_value": 48, "order_system_value": 48, "passed": True, "basis": "corroborated_qty"}
    assert feed_texts(db, "adapter_activated") == ["adapter v2 active: canary 48 = 48. Retrying 4 operations"]
    assert sorted(r["action"] for r in res["retried"]) == ["verified"] * 4
    hold = next(r for r in res["retried"] if r["kind"] == "place_hold")
    assert hold["in_force"] is True and hold["confirm"]["action"] == "verified", "v2 added the confirm step"

    v1, v2 = db.adapters.find_one({"_id": "north:v1"}), db.adapters.find_one({"_id": "north:v2"})
    assert (v1["status"], v2["status"], v2["replaces"]) == ("retired", "active", "north:v1")
    assert v2["mapping"] == V2_MAPPING and v2["steps"]["place_hold"] == [V2_PATHS["place_hold"], V2_PATHS["confirm_hold"]]
    assert v2["expected_fields"]["accounts"] == ["account", "name", "phone", "shipped", "inventory.available"]
    for op in order_system_ops(db):
        if op["_id"] in before:
            assert op == before[op["_id"]], "a verified operation is never touched again"
        else:
            assert (op["state"], op["adapter_version"]) == ("verified", 2), op["_id"]
    assert db.parties.find_one({"recall_id": RID, "party_id": ids["N-021"]})["order_system_available"] == 30
    assert [h["idempotency_key"] for h in order_system.holds_log()] == [f"{RID}:dist-north:place_hold"]
    assert db.parties.find_one({"_id": dist["_id"]})["order_system_paused"] is False
    assert db.episodes.count_documents({"kind": "adapter", "order_system": "north"}) == 1
    assert ops.order_system_summary(db, "north", RID) == {
        "system": "North orders", "adapter_version": 2, "adapter_source": "rules", "ops_verified": 7,
        "ops_flagged": 0, "ops_open": 0, "paused": False}
    assert_balance(RID)


# -- spec test 19 ---------------------------------------------------------------------------

def test_19_a_wrong_mapping_fails_the_canary_so_nothing_is_activated_and_work_stays_paused(client, db, settings):
    app, order_system = make_order_system("north")
    dist = start(client, db)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    ids = book_ledger(db, dist, NORTH)
    account = {a: db.parties.find_one({"recall_id": RID, "party_id": sid}) for a, sid in ids.items()}
    switch(order_system)
    assert ops.read_inventory(ctx, account["N-014"])["action"] == "contract_failed"

    swapped = {**V2_PROPOSAL, "mapping": {**V2_MAPPING, "qty_shipped": "inventory.available", "qty_in_stock": "shipped"}}
    model = ScriptedRepairer(swapped, source="model", model_calls=1)
    res = request_repair(ctx, "north", repairer=model)
    assert res["status"] == "rejected" and len(model.calls) == 1
    assert (res["canary"]["ledger_value"], res["canary"]["order_system_value"], res["canary"]["passed"]) == (48, 12, False)
    assert feed_texts(db, "adapter_rejected") == [
        "adapter v2 rejected: canary 48 != 12 for account N-009. Order system stays paused"]
    assert [a["_id"] for a in db.adapters.find({"order_system": "north", "status": "active"})] == ["north:v1"]
    assert db.adapters.find_one({"_id": "north:v2"})["status"] == "rejected"
    assert db.parties.find_one({"_id": dist["_id"]})["order_system_paused"] is True
    assert db.runs.find_one({"recall_id": RID})["model_calls"] == 1, "the failed proposal still cost a call"

    # Order system work stays paused: a new operation waits with the flagged ones and never reaches the order system.
    waiting = ops.read_inventory(ctx, account["N-021"])
    assert (waiting["action"], waiting["state"]) == ("paused", "needs_reverify")
    assert ops.get(db, waiting["op_id"])["sends"] == 0
    assert {op["state"] for op in order_system_ops(db)} == {"needs_reverify"}

    # A correct proposal later reuses the version and keeps the rejected attempt in its history.
    fixed = request_repair(ctx, "north", repairer=RulesRepairer())
    assert fixed["status"] == "activated"
    v2 = db.adapters.find_one({"_id": "north:v2"})
    assert v2["status"] == "active" and [h["source"] for h in v2["history"]] == ["model"]
    assert v2["history"][0]["canary"]["passed"] is False
    assert {op["state"] for op in order_system_ops(db)} == {"verified"}


# -- what a contract failure flags, and a retry that does not finish -------------------------

def _broken_north(client, db, settings, failpoints=None):
    """North after its switch to v2, contained: (ctx, order system, account parties by account id).
    Bloor Alarm's read met the 410 and Dundas Pantry's read waited with it."""
    _, order_system = make_order_system("north")
    dist = start(client, db)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system}, failpoints=failpoints)
    ids = book_ledger(db, dist, NORTH)
    account = {a: db.parties.find_one({"recall_id": RID, "party_id": sid}) for a, sid in ids.items()}
    switch(order_system)
    assert ops.read_inventory(ctx, account["N-014"])["action"] == "contract_failed"
    assert ops.read_inventory(ctx, account["N-021"])["action"] == "paused"
    return ctx, order_system, account


def test_a_rep_escalation_on_the_order_system_is_neither_flagged_nor_retried_and_the_episode_is_written(
        client, db, settings):
    """systems-2 / crash-3: an escalate_rep op carries its distributor's
    order system and rests in `sent` until the rep reports. contain() flagged it,
    and the retry after the repair raised KeyError on it (the oldest op):
    the ops after it stayed flagged and no adapter episode was written."""
    _, order_system = make_order_system("north")
    dist = start(client, db)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    ids = book_ledger(db, dist, NORTH)
    tallpine = ids["N-041"]
    db.operations.insert_one({"_id": "op-rep", "recall_id": RID, "run_id": "tr-x", "kind": "escalate_rep",
                              "order_system": "north", "party_id": tallpine, "state": "sent",
                              "idempotency_key": f"{RID}:{tallpine}:escalate_rep",
                              "request": {"distributor": "dist-north"},
                              "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})   # the oldest: retried first
    switch(order_system)
    bloor = db.parties.find_one({"recall_id": RID, "party_id": ids["N-014"]})
    assert ops.read_inventory(ctx, bloor)["action"] == "contract_failed"
    assert feed_texts(db, "order_system_contract_failed") == [f"contract failed: {FAILURE}. 1 operation flagged, 0 untouched"]
    assert ops.get(db, "op-rep")["state"] == "sent", "the escalation waits for the rep, not for an adapter"

    res = request_repair(ctx, "north", repairer=RulesRepairer())
    assert res["status"] == "activated" and [(r["kind"], r["action"]) for r in res["retried"]] == [
        ("read_inventory", "verified")]
    assert res["episode_id"] and db.episodes.count_documents({"kind": "adapter", "order_system": "north"}) == 1
    assert ops.get(db, "op-rep")["state"] == "sent"
    assert ops.order_system_summary(db, "north", RID)["ops_flagged"] == 0


def test_a_flagged_operation_that_raises_does_not_strand_the_rest_or_lose_the_episode(
        client, db, settings, monkeypatch):
    ctx, _, account = _broken_north(client, db, settings)
    bloor_op = db.operations.find_one({"party_id": account["N-014"]["party_id"]})
    real = ops._HANDLERS["read_inventory"]

    def flaky(ctx_, op, **kw):
        if op["_id"] == bloor_op["_id"]:
            raise RuntimeError("account row unreadable")
        return real(ctx_, op, **kw)

    monkeypatch.setitem(ops._HANDLERS, "read_inventory", flaky)
    res = request_repair(ctx, "north", repairer=RulesRepairer())
    assert res["status"] == "activated"
    assert [(r["party_id"], r["action"]) for r in res["retried"]] == [
        (account["N-014"]["party_id"], "error"), (account["N-021"]["party_id"], "verified")]
    failed = ops.get(db, bloor_op["_id"])
    assert (failed["state"], failed["error"]) == ("intent", "RuntimeError: account row unreadable"), \
        "left in intent: claim-time recovery reads it back later"
    assert db.episodes.count_documents({"kind": "adapter", "order_system": "north"}) == 1

    monkeypatch.setitem(ops._HANDLERS, "read_inventory", real)
    assert ops.resolve_in_doubt(ctx, failed)["action"] == "verified"


def test_a_repair_killed_after_activation_is_resumed_by_the_planner_with_no_second_episode(client, db, settings):
    """systems-4 (a): activate() commits before the retry. A kill in the
    retry left the rest of the flagged ops stranded (the order system is unpaused,
    so nothing asked for a repair again, and request_repair said 'healthy')
    and no episode written."""
    dying = Failpoints(db, crash=raise_crash)
    ctx, _, account = _broken_north(client, db, settings, failpoints=dying)
    bloor, dundas = account["N-014"]["party_id"], account["N-021"]["party_id"]
    arm_failpoint(db, "after_op_sent", party_id=bloor)
    with pytest.raises(WorkerCrash):
        request_repair(ctx, "north", repairer=RulesRepairer())

    assert db.adapters.find_one({"_id": "north:v2"})["status"] == "active"
    assert db.parties.find_one({"recall_id": RID, "party_id": "dist-north"})["order_system_paused"] is False
    [episode] = db.episodes.find({"kind": "adapter", "order_system": "north"})
    assert episode["adapter_id"] == "north:v2", "remembered before the retry began"
    states = {op["party_id"]: op["state"] for op in order_system_ops(db)}
    assert (states[bloor], states[dundas]) == ("intent", "needs_reverify")

    sim = ops.sim_now(ctx, RID)
    assert RuleDecider().repairs(snapshot(db, RID, sim)) == ["north"], "the planner asks for north again"
    resumed = request_repair(ctx, "north", repairer=RulesRepairer())
    assert (resumed["status"], resumed["adapter_id"], resumed["episode_id"]) == ("resumed", "north:v2", episode["_id"])
    assert [(r["party_id"], r["action"]) for r in resumed["retried"]] == [(dundas, "verified")]
    assert feed_texts(db, "adapter_resumed") == [
        "adapter v2 active; its repair was interrupted. Retrying the 1 operation still flagged"]
    assert ops.resolve_in_doubt(ctx, ops.get(db, next(op["_id"] for op in order_system_ops(db)
                                                    if op["party_id"] == bloor)))["action"] == "verified"
    assert db.episodes.count_documents({"kind": "adapter"}) == 1
    assert RuleDecider().repairs(snapshot(db, RID, sim)) == []
    assert request_repair(ctx, "north")["status"] == "healthy"


# -- the canary's basis ---------------------------------------------------------------------

def test_the_canary_uses_the_shipped_count_only_when_no_account_has_corroborated(client, db):
    dist = start(client, db)["north"]
    assert canary.ledger_basis(db, RID, "dist-north") is None, "no ledger, no basis: never activate on trust"
    book_ledger(db, dist, NORTH, corroborated=())
    basis = canary.ledger_basis(db, RID, "dist-north")
    assert (basis["account"], basis["ledger_value"], basis["basis"]) == ("N-041", 80, "sender_qty")
    assert basis["note"] == canary.SENDER_NOTE
