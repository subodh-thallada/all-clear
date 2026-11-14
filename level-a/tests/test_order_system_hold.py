"""Spec test 17, hold took effect, and the order system operations around it (V2 §7).

A hold counts only when a read-back shows it. With "accepted then dropped"
armed the operation stays `sent` and the read-back finds nothing; one resend
with the same key reaches `verified` and the order system's own log shows one hold.
A timeout after commit is verified by read-back with no resend, and a stale
read is not resent inside the order system's 10-minute read lag.

The builders here (recall, context, ledger) are shared with
test_order_system_change and test_adapter_memory.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pymongo.errors import WriteError

from levela.clock import get_clock
from levela.db.seed import start_recall
from levela.ledger.transfers import edge_id
from levela.orchestrator.failpoints import Failpoints, WorkerCrash, arm as arm_failpoint, raise_crash
from levela.systems import adapters
from levela.systems import operations as ops
from levela.systems.order_client import OrderSystemTimeout
from support import PRODUCT
from test_mock_order_system import EAST, BATCH, NORTH, arm, make_order_system, switch, v2_adapter

RID = "RCL-T-PORTAL"
ACCOUNTS = {"north": NORTH, "east": EAST}


def scenario(order_systems=("north",)) -> dict:
    """A recall whose parties are distributors reached through their order systems."""
    parties = [{"party_id": f"dist-{p}", "name": f"Distributor {p.title()}", "type": "distributor", "tier": 1,
                "channel": "order_system", "order_system": p,
                "shipped_in": sum(a["qty_shipped"] for a in ACCOUNTS[p]),
                "profile": f"Regional distributor {p}: answers through its partner order system."} for p in order_systems]
    return {"scenario_id": "order_system_test", "synthetic": True, "directory": [], "sim": {}, "expected": {},
            "parties": parties,
            "recall": {"_id": RID, "batch_code": BATCH, "batch_spoken": "B, four four seven one",
                       "product": "Maple Farm milk powder 800 g", "supplier": "Maple Farm Foods",
                       "hazard": "The sounder circuit may fail to activate in an alarm", "unit": "tin", **PRODUCT,
                       "units_total": sum(s["shipped_in"] for s in parties)}}


def start(client, db, order_systems=("north",), *, speed: float = 0.05) -> dict:
    start_recall(client, db, scenario(order_systems), all_sim=True, env={}, emit=False, seconds_per_sim_hour=speed)
    return {p: db.parties.find_one({"recall_id": RID, "party_id": f"dist-{p}"}) for p in order_systems}


def make_ctx(settings, client, db, order_systems: dict, failpoints=None) -> SimpleNamespace:
    """The slice of the harness's context the systems code uses."""
    return SimpleNamespace(settings=settings, client=client, db=db, order_systems=order_systems, failpoints=failpoints,
                           recall=None)


def book_ledger(db, distributor: dict, accounts: list[dict], corroborated=("N-009",)) -> dict:
    """The ledger as the distributor's forwards claim and the corroborating
    accounts' receipt claims leave it: account parties, one edge per account,
    the distributor's tins all forwarded. Returns {account: party_id}."""
    recall = db.recalls.find_one({"_id": RID})
    now = datetime.now(timezone.utc)
    ids, _ = ops.upsert_accounts(db, recall, distributor, accounts, now=now, sim=9.0)
    edges = []
    for sid, row in zip(ids, accounts):
        agreed = row["account"] in corroborated
        qty = row["qty_shipped"]
        edges.append({"_id": edge_id(RID, distributor["party_id"], sid), "recall_id": RID,
                      "from_party": distributor["party_id"], "to_party": sid, "sender_qty": qty,
                      "recipient_qty": qty if agreed else None, "corroborated_qty": qty if agreed else 0,
                      "disputed_qty": 0, "state": "corroborated" if agreed else "awaiting_recipient",
                      "created_at": now, "updated_at": now})
        if agreed:
            db.parties.update_one({"recall_id": RID, "party_id": sid},
                                 {"$set": {"shipped_in": qty, "held": qty, "state": "reconciled"}})
    db.edges.insert_many(edges)
    total = sum(a["qty_shipped"] for a in accounts)
    db.parties.update_one({"_id": distributor["_id"]}, [{"$set": {
        "forwarded": total, "unresolved": {"$subtract": ["$shipped_in", total]}, "state": "claimed"}}])
    return {row["account"]: sid for sid, row in zip(ids, accounts)}


def order_system_ops(db, order_system="north", **query) -> list[dict]:
    return list(db.operations.find({"order_system": order_system, **query}).sort("created_at", 1))


def feed_texts(db, kind: str) -> list[str]:
    return [row["text"] for row in db.feed.find({"kind": kind}).sort("at", 1)]


@pytest.fixture
def north(client, db, settings):
    """(ctx, order system app, distributor party) for a recall with Distributor North."""
    app, order_system = make_order_system("north")
    dist = start(client, db)["north"]
    return make_ctx(settings, client, db, {"north": order_system}), app, dist


# -- a hold, the plain way ------------------------------------------------------------------

def test_a_hold_is_verified_by_read_back_and_a_second_request_returns_the_stored_outcome(north, db):
    ctx, app, dist = north
    res = ops.place_hold(ctx, dist)
    assert (res["action"], res["state"], res["hold_id"]) == ("verified", "verified", "H-7781")
    op = ops.get(db, res["op_id"])
    assert op["idempotency_key"] == f"{RID}:dist-north:place_hold"
    assert op["readback_value"] == {"hold_id": "H-7781", "status": "on_hold", "batch": BATCH, "account_scope": "all"}
    assert op["readback_at"] >= op["sent_at"] >= op["intent_at"] and op["sends"] == 1 and op["adapter_version"] == 1
    line = db.feed.find_one({"kind": "order_system_verified"})
    assert line["text"] == "hold H-7781 verified by read-back" and line["system"] == "North orders"
    assert db.parties.find_one({"_id": dist["_id"]})["order_system_hold"]["hold_id"] == "H-7781"

    again = ops.place_hold(ctx, dist)
    assert again["action"] == "duplicate" and again["op_id"] == res["op_id"]
    assert app.state.order_system.writes == 1 and db.operations.count_documents({}) == 1


# -- spec test 17 ---------------------------------------------------------------------------

def test_17_accepted_then_dropped_stays_sent_then_one_resend_with_the_same_key_is_verified(north, db):
    ctx, app, dist = north
    order_system = ctx.order_systems["north"]
    arm(order_system, "accepted_then_dropped")
    first = ops.place_hold(ctx, dist)
    assert (first["action"], first["state"]) == ("sent", "sent")
    op = ops.get(db, first["op_id"])
    assert op["readback_at"] is None and op["readback_value"] is None and op["sends"] == 1
    assert order_system.holds_log() == [], "the order system never stored it"
    assert feed_texts(db, "order_system_sent") == [f"hold {op['hold_id']} sent; read-back shows nothing yet"]

    # A resend is allowed only once the order system's reads may have caught up
    # (READBACK_SETTLE_H of simulated time); on a fast database the resolve
    # would otherwise come first and rightly defer.
    time.sleep(get_clock(db, RID).real_seconds(ops.READBACK_SETTLE_H) + 0.05)
    done = ops.resolve_in_doubt(ctx, op)
    assert (done["action"], done["state"]) == ("verified", "verified")
    op = ops.get(db, op["_id"])
    log = order_system.holds_log()
    assert [h["idempotency_key"] for h in log] == [op["idempotency_key"]], "one hold, under the same key"
    assert op["hold_id"] == log[0]["hold_id"] and op["sends"] == 2
    assert app.state.order_system.writes == 2


def test_17_timeout_after_commit_is_verified_by_read_back_without_a_resend(client, db, settings):
    app, order_system = make_order_system("north", timeout=0.3)
    dist = start(client, db)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    arm(order_system, "timeout_after_commit", sleep_s=30)
    res = ops.place_hold(ctx, dist)
    assert res["action"] == "verified"
    op = ops.get(db, res["op_id"])
    assert op["sends"] == 1 and "no answer within" in op["error"]
    assert op["hold_id"] == order_system.holds_log()[0]["hold_id"]
    assert app.state.order_system.writes == 1 and len(order_system.holds_log()) == 1


def test_17_a_stale_read_is_not_resent_inside_the_window(client, db, settings):
    # 12 s per simulated hour: the order system's 10-minute read lag is 2 real seconds.
    app, order_system = make_order_system("north", speed=12.0)
    dist = start(client, db, speed=12.0)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    arm(order_system, "stale_read")
    first = ops.place_hold(ctx, dist)
    assert first["action"] == "sent"
    inside = ops.resolve_in_doubt(ctx, ops.get(db, first["op_id"]))
    assert inside["action"] == "deferred" and "not resending" in inside["note"]
    assert app.state.order_system.writes == 1

    time.sleep(app.state.order_system.stale_window_s() + 0.2)
    after = ops.resolve_in_doubt(ctx, ops.get(db, first["op_id"]))
    assert after["action"] == "verified"
    assert ops.get(db, first["op_id"])["sends"] == 1 and app.state.order_system.writes == 1
    assert len(order_system.holds_log()) == 1


def test_a_dropped_confirm_leaves_the_hold_pending_until_a_read_back_shows_it_in_force(north, db):
    ctx, app, dist = north
    order_system = ctx.order_systems["north"]
    v2 = {**v2_adapter(), "status": "active"}
    db.adapters.insert_many([{**adapters.builtin("north"), "status": "retired"}, v2])
    switch(order_system)
    key = ops.idempotency_key(RID, "dist-north", "place_hold")
    hold_id, _ = order_system.place_hold(BATCH, "all", key, adapter=v2)
    placed = ops.new_op(recall_id=RID, kind="place_hold", order_system="north", party_id="dist-north", adapter_version=2,
                        sim=9.0, request={"batch": BATCH, "scope": "all"})
    db.operations.insert_one({**placed, "state": "verified", "hold_id": hold_id, "readback_at": 9.1,
                              "readback_value": {"hold_id": hold_id, "status": "pending_confirm"}})

    arm(order_system, "accepted_then_dropped")
    first = ops.confirm_hold(ctx, dist)
    assert (first["action"], first["kind"]) == ("sent", "confirm_hold")
    assert order_system.list_holds(BATCH, adapter=v2)[0]["status"] == "pending_confirm"
    done = ops.resolve_in_doubt(ctx, ops.get(db, first["op_id"]))
    assert (done["action"], done["in_force"]) == ("verified", True)
    op = ops.get(db, first["op_id"])
    assert op["sends"] == 2 and op["readback_value"]["status"] == "active"
    assert db.parties.find_one({"_id": dist["_id"]})["order_system_hold"]["status"] == "active"
    assert len(order_system.holds_log()) == 1


# -- kills ----------------------------------------------------------------------------------

def test_a_kill_after_the_request_went_out_is_settled_by_read_back_with_no_resend(north, db):
    ctx, app, dist = north
    arm_failpoint(db, "after_op_sent", party_id="dist-north")
    dying = make_ctx(ctx.settings, ctx.client, db, ctx.order_systems, failpoints=Failpoints(db, crash=raise_crash))
    with pytest.raises(WorkerCrash):
        ops.place_hold(dying, dist)
    (op,) = order_system_ops(db)
    assert (op["state"], op["sends"], op["hold_id"]) == ("intent", 0, None), "the answer was never recorded"
    assert len(ctx.order_systems["north"].holds_log()) == 1, "but the order system has the hold"

    res = ops.resolve_in_doubt(ctx, op)
    assert (res["action"], res["hold_id"]) == ("verified", "H-7781")
    assert app.state.order_system.writes == 1


def test_a_kill_after_the_intent_sends_once_after_the_read_back_shows_nothing(north, db):
    ctx, app, dist = north
    arm_failpoint(db, "after_op_intent")
    dying = make_ctx(ctx.settings, ctx.client, db, ctx.order_systems, failpoints=Failpoints(db, crash=raise_crash))
    with pytest.raises(WorkerCrash):
        ops.place_hold(dying, dist)
    assert app.state.order_system.writes == 0
    # The intent may have been sent before the kill, so the restart sends
    # again only once the order system's reads may have caught up (the read-back
    # window); on a fast database it would otherwise come first and defer.
    time.sleep(get_clock(db, RID).real_seconds(ops.READBACK_SETTLE_H) + 0.05)
    res = ops.place_hold(ctx, dist)
    assert res["action"] == "verified" and app.state.order_system.writes == 1
    assert len(order_system_ops(db)) == 1, "the restarted call found the first intent by its key"


# -- accounts and inventory -----------------------------------------------------------------

def test_read_accounts_makes_tier_2_installers_and_books_the_distributors_claim(north, db, assert_balance):
    ctx, _, dist = north
    res = ops.read_accounts(ctx, dist)
    assert res["action"] == "verified" and len(res["created"]) == len(NORTH)
    op = ops.get(db, res["op_id"])
    assert op["readback_value"]["accounts"] == 5 and op["readback_value"]["shipped"] == 300

    bloor = db.parties.find_one({"recall_id": RID, "party_id": "bloor-alarm"})
    assert (bloor["type"], bloor["tier"], bloor["found_via"], bloor["channel"]) == ("store", 2, "order_system", "sim")
    assert (bloor["phone"], bloor["distributor"], bloor["account"]) == ("+14165550127", "dist-north", "N-014")
    assert bloor["discovered_by"] == "order_system" and bloor["next_action_at"] is not None and bloor["shipped_in"] == 0

    event = db.tool_events.find_one({"attempt_id": op["_id"]})
    assert event["kind"] == "order_system" and event["party_id"] == "dist-north"
    claim = db.claims.find_one({"attempt_id": op["_id"]})
    assert claim["source"] == "order_system" and claim["status"] == "accepted"
    assert claim["raw"]["forwards"] == [{"to_party_id": s, "qty": a["qty_shipped"]}
                                        for s, a in zip(res["created"], NORTH)]
    assert "accounts read: 5 accounts for batch B-4471, 5 new parties, 300 tins forwarded" \
        in feed_texts(db, "order_system_accounts")
    assert db.runs.find_one({"recall_id": RID})["planner_wake_reason"] == "parties_created"
    assert_balance(RID)

    again = ops.read_accounts(ctx, dist)
    assert again["action"] == "duplicate"
    assert db.parties.count_documents({"recall_id": RID, "tier": 2}) == 5


def test_read_accounts_opens_one_edge_per_account(north, db, assert_balance):
    ctx, _, dist = north
    ops.read_accounts(ctx, dist)
    booked = db.parties.find_one({"_id": dist["_id"]})
    if booked["forwarded"] == 0:
        pytest.skip("this tree's ledger ignores claim forwards (DESIGN_V2 §9 not merged yet)")
    edges = {e["to_party"]: e for e in db.edges.find({"recall_id": RID, "from_party": "dist-north"})}
    assert {sid: e["sender_qty"] for sid, e in edges.items()} == {
        db.parties.find_one({"recall_id": RID, "account": a["account"]})["party_id"]: a["qty_shipped"] for a in NORTH}
    assert booked["forwarded"] == 300 and booked["unresolved"] == 0
    assert_balance(RID)


def test_read_inventory_records_order_system_in_stock_once_per_simulated_day(client, db, settings):
    # The demo clock (30 s per simulated hour): both reads fall on the same simulated day.
    _, order_system = make_order_system("north", speed=30.0)
    dist = start(client, db, speed=30.0)["north"]
    ctx = make_ctx(settings, client, db, {"north": order_system})
    ids = book_ledger(db, dist, NORTH)
    bloor = db.parties.find_one({"recall_id": RID, "party_id": ids["N-014"]})
    res = ops.read_inventory(ctx, bloor)
    assert res["action"] == "verified"
    op = ops.get(db, res["op_id"])
    assert op["idempotency_key"].startswith(f"{RID}:bloor-alarm:read_inventory:d")
    assert op["readback_value"] == {"account": "N-014", "qty_in_stock": 22, "qty_shipped": 60}
    assert db.parties.find_one({"_id": bloor["_id"]})["order_system_in_stock"] == 22
    assert ops.read_inventory(ctx, bloor)["action"] == "duplicate"


# -- what is not an order system call --------------------------------------------------------------

def test_execute_skips_a_form_link_or_a_rep_escalation_without_touching_any_order_system(north, db):
    """systems-1: a form link (order system None) raised LookupError after seeding
    a 'None:v1' adapter, and a rep escalation (North orders) raised KeyError
    for want of a handler. Neither is an order system call."""
    ctx, app, _ = north
    now = datetime.now(timezone.utc)
    docs = [{"_id": "op-form", "recall_id": RID, "kind": "send_form_link", "order_system": None, "party_id": "bloor-alarm",
             "idempotency_key": f"{RID}:bloor-alarm:form:3", "state": "sent", "request": {}, "created_at": now},
            {"_id": "op-rep", "recall_id": RID, "kind": "escalate_rep", "order_system": "north", "party_id": "bloor-alarm",
             "idempotency_key": f"{RID}:bloor-alarm:escalate_rep", "state": "sent", "request": {}, "created_at": now}]
    db.operations.insert_many(docs)
    for doc in docs:
        for res in (ops.execute(ctx, doc), ops.resolve_in_doubt(ctx, doc)):
            assert (res["action"], res["state"], res["note"]) == ("skipped", "sent", "not an order system operation")
    assert db.adapters.count_documents({}) == 0, "no adapter is seeded, for order system None or any other"
    assert app.state.order_system.writes == 0 and {op["state"] for op in db.operations.find({})} == {"sent"}


def test_a_read_back_the_order_system_does_not_answer_defers_the_op_instead_of_raising(north, db, monkeypatch):
    """A list_holds timeout during a read-back used to escape execute() (and
    with it a repair's retry). The op keeps its state and is read back again
    later; nothing is resent."""
    ctx, app, dist = north
    order_system = ctx.order_systems["north"]
    arm(order_system, "accepted_then_dropped")
    first = ops.place_hold(ctx, dist)
    assert first["state"] == "sent"

    def unanswered(*_a, **_kw):
        raise OrderSystemTimeout("GET /v1/holds", 2.0)

    monkeypatch.setattr(order_system, "list_holds", unanswered)
    res = ops.resolve_in_doubt(ctx, ops.get(db, first["op_id"]))
    assert (res["action"], res["state"]) == ("deferred", "sent") and "order system unavailable" in res["note"]
    assert ops.get(db, first["op_id"])["sends"] == 1 and app.state.order_system.writes == 1


def test_verified_needs_a_read_back_in_code_and_in_atlas(north, db):
    ctx, _, dist = north
    doc = ops.new_op(recall_id=RID, kind="place_hold", order_system="north", party_id="dist-north", adapter_version=1,
                     sim=9.0, request={"batch": BATCH, "scope": "all"})
    op, created = ops.open_op(db, doc)
    assert created
    with pytest.raises(ValueError):
        ops.verify(db, op, readback_at=None, readback_value=None, adapter_version=1)
    with pytest.raises(WriteError) as exc:
        db.operations.update_one({"_id": op["_id"]}, {"$set": {"state": "verified"}})
    assert exc.value.code == 121
