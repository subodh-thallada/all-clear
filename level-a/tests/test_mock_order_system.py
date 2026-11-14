"""The mock distributor order system (V2 §7) and the client that speaks to it
through an adapter. No database: the order system runs in-process behind
httpx.ASGITransport, exactly as the DB-backed order system tests use it.

The builders here (accounts, order system, the v2 proposal) are shared with
test_order_system_hold, test_order_system_change and test_adapter_memory.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from levela.mock.order_system import VENDOR_TITLE, create_order_system, load_order_system_config
from levela.systems import adapters
from levela.systems.contract import ContractError
from levela.systems.order_client import OrderClient, OrderSystemError, OrderSystemTimeout, build_order_systems, order_url
from levela.systems.repair import validate_proposal

BATCH = "B-4471"
TOKEN = "test-admin-token"
# Fictional numbers (555-01xx). N-009 is the canary account in the change tests.
NORTH = [
    {"account": "N-009", "name": "Annex Grocer", "phone": "+14165550109", "qty_shipped": 48, "qty_in_stock": 12},
    {"account": "N-014", "name": "Bloor Foods", "phone": "+14165550127", "qty_shipped": 60, "qty_in_stock": 22},
    {"account": "N-021", "name": "Dundas Pantry", "phone": "+14165550131", "qty_shipped": 72, "qty_in_stock": 30},
    {"account": "N-030", "name": "College Market", "phone": "+14165550138", "qty_shipped": 40, "qty_in_stock": 5},
    {"account": "N-041", "name": "Ossington Foods", "phone": "+14165550146", "qty_shipped": 80, "qty_in_stock": 41},
]
EAST = [
    {"account": "E-003", "name": "Danforth Grocer", "phone": "+14165550151", "qty_shipped": 90, "qty_in_stock": 31},
    {"account": "E-007", "name": "Kingston Rd Market", "phone": "+14165550157", "qty_shipped": 110, "qty_in_stock": 64},
]
V2_PATHS = {"place_hold": "POST /v2/holds", "confirm_hold": "POST /v2/holds/{hold_id}/confirm",
            "list_holds": "GET /v2/holds", "list_accounts": "GET /v2/accounts"}
V2_MAPPING = {"batch": "batch_code", "account_scope": "scope", "hold_id": "hold_id", "status": "state",
              "account": "account", "name": "name", "phone": "phone", "qty_shipped": "shipped",
              "qty_in_stock": "inventory.available"}
V2_PROPOSAL = {"paths": V2_PATHS, "mapping": V2_MAPPING, "status_in_force": ["active"],
               "status_pending": ["pending_confirm"], "defaults": [{"field": "reason_code", "value": "recall"}],
               "rationale": "v2 renamed the batch, scope, status and quantity fields and added a confirm step"}


def make_order_system(name: str = "north", accounts=None, *, speed: float = 0.05, timeout: float = 2.0):
    """(app, OrderClient) for one in-process order system."""
    app = create_order_system(name, NORTH if accounts is None else accounts, batch=BATCH, seconds_per_sim_hour=speed,
                        admin_token=TOKEN)
    return app, OrderClient(transport=httpx.ASGITransport(app=app), timeout=timeout, name=name)


def v2_adapter(order_system: str = "north") -> dict:
    return adapters.build_candidate(order_system, validate_proposal(V2_PROPOSAL), adapters.builtin(order_system),
                                    source="test", vendor=VENDOR_TITLE)


def switch(client: OrderClient, version: int = 2) -> dict:
    return client.admin("version", token=TOKEN, body={"version": version})


def arm(client: OrderClient, fault: str, count: int = 1, **extra) -> dict:
    return client.admin("fault", token=TOKEN, body={"fault": fault, "count": count, **extra})


def _raw(app, method: str, path: str, **kwargs) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://order-system") as http:
            return await http.request(method, path, **kwargs)
    return asyncio.run(go())


V1 = adapters.builtin("north")


# -- v1 -------------------------------------------------------------------------------------

def test_a_v1_hold_is_idempotent_by_key_and_logged_once():
    app, order_system = make_order_system()
    first, raw = order_system.place_hold(BATCH, "all", "r-1:dist-north:place_hold", adapter=V1)
    again, _ = order_system.place_hold(BATCH, "all", "r-1:dist-north:place_hold", adapter=V1)
    assert first == again == "H-7781" and raw == {"hold_id": "H-7781"}
    assert order_system.list_holds(BATCH, adapter=V1) == [
        {"hold_id": "H-7781", "batch": BATCH, "account_scope": "all", "status": "on_hold"}]
    log = order_system.holds_log()
    assert [(h["idempotency_key"], h["hold_id"], h["batch"], h["scope"]) for h in log] == [
        ("r-1:dist-north:place_hold", "H-7781", BATCH, "all")]


def test_writes_need_a_key_and_a_key_cannot_be_reused_for_another_request():
    app, order_system = make_order_system()
    assert _raw(app, "POST", "/v1/holds", json={"batch": BATCH, "account_scope": "all"}).status_code == 400
    order_system.place_hold(BATCH, "all", "k1", adapter=V1)
    clash = _raw(app, "POST", "/v1/holds", json={"batch": BATCH, "account_scope": "N-009"},
                 headers={"Idempotency-Key": "k1"})
    assert clash.status_code == 409
    with pytest.raises(OrderSystemError) as unknown:
        order_system.place_hold("L-0000", "all", "k2", adapter=V1)
    assert unknown.value.status == 404 and not unknown.value.effect_unknown


def test_v1_accounts_are_canonical_rows():
    _, order_system = make_order_system()
    assert order_system.list_accounts(BATCH, adapter=V1) == NORTH
    assert order_system.list_accounts("L-0000", adapter=V1) == []


# -- the switch -----------------------------------------------------------------------------

def test_after_the_switch_v1_answers_410_naming_v2():
    _, order_system = make_order_system()
    assert switch(order_system)["version"] == 2
    with pytest.raises(ContractError) as exc:
        order_system.list_accounts(BATCH, adapter=V1)
    err = exc.value
    assert err.failure_text == "GET /v1/accounts answered 410 Gone (API v1 was retired)"
    assert err.status == 410 and err.endpoint == "GET /v1/accounts"
    body = json.loads(err.raw_body)
    assert "/v2/accounts" in body["detail"] and body["current_version"] == 2


def test_v2_is_not_served_before_the_switch():
    _, order_system = make_order_system()
    with pytest.raises(OrderSystemError) as exc:
        order_system.list_accounts(BATCH, adapter=v2_adapter())
    assert exc.value.status == 404


def test_a_v2_hold_is_pending_until_confirmed():
    _, order_system = make_order_system()
    switch(order_system)
    v2 = v2_adapter()
    hold_id, raw = order_system.place_hold(BATCH, "all", "k-v2", adapter=v2)
    assert raw == {"hold_id": hold_id, "state": "pending_confirm"}
    assert order_system.list_holds(BATCH, adapter=v2) == [
        {"hold_id": hold_id, "batch": BATCH, "account_scope": "all", "status": "pending_confirm"}]
    assert order_system.confirm_hold(hold_id, "k-v2:confirm", adapter=v2) == {"hold_id": hold_id, "state": "active"}
    assert order_system.list_holds(BATCH, adapter=v2)[0]["status"] == "active"


def test_a_v1_hold_is_the_same_hold_under_v2():
    _, order_system = make_order_system()
    hold_id, _ = order_system.place_hold(BATCH, "all", "same-key", adapter=V1)
    switch(order_system)
    v2 = v2_adapter()
    again, raw = order_system.place_hold(BATCH, "all", "same-key", adapter=v2)
    assert again == hold_id and raw["state"] == "active"
    assert len(order_system.holds_log()) == 1


def test_v2_accounts_read_through_the_repaired_mapping_and_fail_through_the_old_one():
    _, order_system = make_order_system()
    switch(order_system)
    assert order_system.list_accounts(BATCH, adapter=v2_adapter()) == NORTH
    stale = {**V1, "paths": V2_PATHS, "mapping": {**V1["mapping"], "batch": "batch_code"}}
    with pytest.raises(ContractError) as exc:
        order_system.list_accounts(BATCH, adapter=stale)
    assert exc.value.failure_text == "fields qty_shipped, qty_in_stock missing"
    assert '"shipped":48' in exc.value.raw_body


def test_a_v2_hold_without_reason_code_is_a_contract_failure():
    _, order_system = make_order_system()
    switch(order_system)
    no_reason = {**v2_adapter(), "defaults": {}}
    with pytest.raises(ContractError) as exc:
        order_system.place_hold(BATCH, "all", "k", adapter=no_reason)
    assert exc.value.status == 422 and "reason_code" in exc.value.failure_text


# -- faults ---------------------------------------------------------------------------------

def test_accepted_then_dropped_answers_202_but_parties_nothing():
    _, order_system = make_order_system()
    arm(order_system, "accepted_then_dropped")
    phantom, _ = order_system.place_hold(BATCH, "all", "k", adapter=V1)
    assert order_system.list_holds(BATCH, adapter=V1) == [] and order_system.holds_log() == []
    real, _ = order_system.place_hold(BATCH, "all", "k", adapter=V1)
    assert real != phantom
    assert [h["hold_id"] for h in order_system.holds_log()] == [real]


def test_timeout_after_commit_commits_then_outlasts_the_client():
    app, order_system = make_order_system(timeout=0.3)
    arm(order_system, "timeout_after_commit", sleep_s=30)
    started = time.monotonic()
    with pytest.raises(OrderSystemTimeout):
        order_system.place_hold(BATCH, "all", "k", adapter=V1)
    assert time.monotonic() - started < 5
    assert [h["idempotency_key"] for h in order_system.holds_log()] == ["k"]
    assert order_system.list_holds(BATCH, adapter=V1)[0]["status"] == "on_hold"


def test_stale_read_hides_a_new_hold_for_ten_simulated_minutes():
    app, order_system = make_order_system(speed=0.05)
    state = order_system.admin("clock", token=TOKEN, body={"seconds_per_sim_hour": 3.0})
    assert state["stale_window_s"] == pytest.approx(0.5)
    arm(order_system, "stale_read")
    order_system.place_hold(BATCH, "all", "k", adapter=V1)
    assert order_system.list_holds(BATCH, adapter=V1) == []
    assert len(order_system.holds_log()) == 1, "the order system's own log is never stale"
    time.sleep(0.6)
    assert len(order_system.list_holds(BATCH, adapter=V1)) == 1


def test_admin_needs_the_token_and_reset_clears_everything():
    _, order_system = make_order_system()
    with pytest.raises(OrderSystemError) as exc:
        order_system.admin("version", token="wrong", body={"version": 2})
    assert exc.value.status == 401
    order_system.place_hold(BATCH, "all", "k", adapter=V1)
    switch(order_system)
    arm(order_system, "stale_read", 3)
    assert order_system.admin("state", token=TOKEN)["faults"] == {"stale_read": 3}
    state = order_system.admin("reset", token=TOKEN)
    assert (state["version"], state["holds"], state["faults"]) == (1, 0, {})
    order_system.close()
    assert order_system.holds_log() == [], "a closed client starts a fresh loop on its next call"
    order_system.close()


def test_the_apis_reset_puts_a_switched_order_system_back_on_v1():
    """POST /admin/reset and `python -m levela reset` reset each order system
    through levela.api.order_admin: same path, same token header."""
    from fastapi.testclient import TestClient

    from levela.api import order_admin

    app, order_system = make_order_system()
    order_system.place_hold(BATCH, "all", "k", adapter=V1)
    switch(order_system)
    arm(order_system, "accepted_then_dropped", 2)
    http = TestClient(app)
    assert order_admin.reset(http, "http://testserver", "wrong").error == "order system answered 401"
    reply = order_admin.reset(http, "http://testserver", TOKEN)
    assert reply.ok and (reply.body["version"], reply.body["holds"], reply.body["faults"]) == (1, 0, {})
    assert order_system.list_accounts(BATCH, adapter=V1) == NORTH, "v1 answers again"
    assert order_admin.reset_all(http, {"north": "http://testserver"}, TOKEN)["north"]["ok"] is True
    order_system.close()


# -- the documents a repairer reads ---------------------------------------------------------

def test_openapi_describes_every_field_and_marks_v1_deprecated():
    app, order_system = make_order_system()
    doc = order_system.openapi()
    assert doc["info"]["title"] == VENDOR_TITLE
    for name, schema in doc["components"]["schemas"].items():
        if name.startswith(("Hold", "Account", "Inventory")):
            for field, prop in schema["properties"].items():
                assert prop.get("description"), f"{name}.{field} has no description"
    ops = {(m.upper(), p): op for p, item in doc["paths"].items() for m, op in item.items()}
    assert all(op.get("deprecated") for (m, p), op in ops.items() if p.startswith("/v1/"))
    assert not any(op.get("deprecated") for (m, p), op in ops.items() if p.startswith("/v2/"))
    assert ("POST", "/v2/holds/{hold_id}/confirm") in ops
    assert not any(p.startswith(("/_admin", "/_log")) for _, p in ops), "admin is not part of the partner API"


def test_the_cli_reads_its_accounts_from_the_scenario(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"schedule": {"seconds_per_sim_hour": 15},
                                "order_systems": {"north": {"batch": BATCH, "accounts": NORTH}}}), encoding="utf-8")
    config = load_order_system_config(str(path), "north")
    assert config == {"batch": BATCH, "accounts": NORTH, "seconds_per_sim_hour": 15.0}
    with pytest.raises(SystemExit, match="no order system 'east'"):
        load_order_system_config(str(path), "east")
    path.write_text(json.dumps({"order_systems": {"north": {"batch": BATCH, "accounts": [{"account": "N-1"}]}}}),
                    encoding="utf-8")
    with pytest.raises(SystemExit, match="malformed"):
        load_order_system_config(str(path), "north")


def test_order_system_urls_come_from_the_environment_or_the_default_ports():
    assert order_url("north", env={}) == "http://127.0.0.1:8101"
    assert order_url("east", env={"ORDER_EAST_URL": "http://order-system-east:9000/"}) == "http://order-system-east:9000"
    with pytest.raises(KeyError):
        order_url("west", env={})
    clients = build_order_systems(env={"ORDER_WEST_URL": "http://10.0.0.9:8103"})
    assert sorted(clients) == ["east", "north", "west"] and clients["west"].base_url == "http://10.0.0.9:8103"
