"""The mock distributor order system (V2 §7): one process per distributor.

    python -m levela.mock.order_system --name north --port 8101 [--scenario recall_long]

It plays a partner API whose contract changes mid-run, so the agent has to
notice, contain, repair and verify instead of trusting its first mapping:

  v1  POST /v1/holds {batch, account_scope} + Idempotency-Key -> 202 {hold_id}
      GET  /v1/holds?batch=      -> [{hold_id, batch, account_scope, status}]
      GET  /v1/accounts?batch=   -> [{account, name, phone, qty_shipped, qty_in_stock}]
  v2  POST /v2/holds {batch_code, scope, reason_code} -> 202 {hold_id, state: pending_confirm}
      POST /v2/holds/{hold_id}/confirm -> 200 {hold_id, state: active}
      GET  /v2/holds?batch_code= -> [{hold_id, batch_code, scope, state}]
      GET  /v2/accounts?batch_code= -> [{account, name, phone, shipped, inventory: {available, quarantined}}]

After the switch every v1 path answers 410 with a body that names v2: that
body is the error text a repairer reads. /openapi.json is FastAPI's, with a
description on every field, so a rules repairer can match old fields to new.

Idempotency: a key always returns the hold it created, across versions. The
three faults model real partner APIs, each consumed by the next write:

  accepted_then_dropped  202 with a hold id, but nothing is stored (and the
                         key stays free, so a resend with it creates the hold)
  timeout_after_commit   the write commits, then the answer is held back past
                         the client's timeout
  stale_read             the new hold is invisible to reads for 10 simulated
                         minutes (the order system documents that reads may lag)

GET /_log/holds is the order system's own record of every hold by key (invariant 2).
/_admin/* (header X-Admin-Token) switches the version, arms faults, sets the
clock speed and resets the state. Everything lives in memory behind one lock.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

VENDOR_TITLE = "Distributor order system"
FAULTS = ("accepted_then_dropped", "timeout_after_commit", "stale_read")
STALE_SIM_HOURS = 10 / 60            # stale_read hides a new hold this long
DEFAULT_TIMEOUT_SLEEP_S = 30.0       # timeout_after_commit holds the answer this long
FIRST_HOLD_NUMBER = 7781
SCOPE_DOC = "Which accounts the hold covers: 'all' for every account that received the batch, or one account id."


# ------------------------------------------------------------------ v1 models

class HoldRequestV1(BaseModel):
    batch: str = Field(description="Batch code of the product to put on hold, as printed on the case.")
    account_scope: str = Field(description=SCOPE_DOC)


class HoldAcceptedV1(BaseModel):
    hold_id: str = Field(description="Identifier of the hold.")


class HoldV1(BaseModel):
    hold_id: str = Field(description="Identifier of the hold.")
    batch: str = Field(description="Batch code the hold applies to.")
    account_scope: str = Field(description="Which accounts the hold covers.")
    status: Literal["on_hold"] = Field(description="Hold status. on_hold: the hold is in force.")


class AccountV1(BaseModel):
    account: str = Field(description="Account identifier.")
    name: str = Field(description="Account (party) name.")
    phone: str = Field(description="Account phone number.")
    qty_shipped: int = Field(description="Units of the batch shipped to this account.")
    qty_in_stock: int = Field(description="Units of the batch the account has in stock and available for sale.")


# ------------------------------------------------------------------ v2 models

class HoldRequestV2(BaseModel):
    batch_code: str = Field(description="Batch code of the product to put on hold, as printed on the case.")
    scope: str = Field(description=SCOPE_DOC)
    reason_code: Literal["recall", "quality", "other"] = Field(
        description="Why the hold is placed. Use recall for a product recall.")


class HoldAcceptedV2(BaseModel):
    hold_id: str = Field(description="Identifier of the hold.")
    state: Literal["pending_confirm", "active"] = Field(
        description="pending_confirm for a new hold: it has no effect until confirmed with "
                    "POST /v2/holds/{hold_id}/confirm. A repeated request returns the hold's current state.")


class HoldConfirmedV2(BaseModel):
    hold_id: str = Field(description="Identifier of the hold.")
    state: Literal["active"] = Field(description="active: the hold is in force.")


class HoldV2(BaseModel):
    hold_id: str = Field(description="Identifier of the hold.")
    batch_code: str = Field(description="Batch code the hold applies to.")
    scope: str = Field(description="Which accounts the hold covers.")
    state: Literal["pending_confirm", "active"] = Field(
        description="Hold state: pending_confirm until confirmed, then active (in force).")


class InventoryV2(BaseModel):
    available: int = Field(description="Units of the batch on hand and available for sale.")
    quarantined: int = Field(description="Units of the batch on hand but set aside in quarantine.")


class AccountV2(BaseModel):
    account: str = Field(description="Account identifier.")
    name: str = Field(description="Account (party) name.")
    phone: str = Field(description="Account phone number.")
    shipped: int = Field(description="Units of the batch shipped to this account.")
    inventory: InventoryV2 = Field(description="The account's current stock of the batch.")


# --------------------------------------------------------------- admin models

class VersionBody(BaseModel):
    version: Literal[1, 2]


class FaultBody(BaseModel):
    fault: Literal["accepted_then_dropped", "timeout_after_commit", "stale_read"]
    count: int = Field(1, ge=0, description="How many writes the fault applies to; 0 disarms it.")
    sleep_s: float | None = Field(None, gt=0, description="timeout_after_commit only: how long to hold the answer.")


class ClockBody(BaseModel):
    seconds_per_sim_hour: float = Field(gt=0)


# --------------------------------------------------------------------- state

@dataclass
class Hold:
    hold_id: str
    key: str
    batch: str
    scope: str
    in_force: bool
    visible_after: float            # time.monotonic() before which reads do not show it


class Conflict(Exception):
    """An Idempotency-Key reused with a different request."""


class Gone(Exception):
    """A retired API version was called."""


class OrderSystemState:
    """Everything one order system knows. Every method takes the lock; callers never do."""

    def __init__(self, name: str, accounts: list[dict], *, batch: str, seconds_per_sim_hour: float):
        self.name, self.batch = name, batch
        self.accounts = [AccountV1(**a).model_dump() for a in accounts]
        self.lock = threading.Lock()
        self.seconds_per_sim_hour = float(seconds_per_sim_hour)
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.version = 1
            self.holds: dict[str, Hold] = {}
            self.by_key: dict[str, str] = {}
            self.log: list[dict] = []
            self.faults: dict[str, int] = {}
            self.timeout_sleep_s = DEFAULT_TIMEOUT_SLEEP_S
            self.next_number = FIRST_HOLD_NUMBER
            self.writes = 0

    # The lock is held by the caller of these two helpers.
    def _take(self, fault: str) -> bool:
        left = self.faults.get(fault, 0)
        if left <= 0:
            return False
        self.faults[fault] = left - 1
        return True

    def _new_id(self) -> str:
        hold_id = f"H-{self.next_number}"
        self.next_number += 1
        return hold_id

    def stale_window_s(self) -> float:
        return STALE_SIM_HOURS * self.seconds_per_sim_hour

    def place(self, key: str, batch: str, scope: str, *, in_force: bool) -> tuple[Hold, float]:
        """Create (or return) the hold for this key. Returns (hold, seconds to
        hold the answer back). A dropped request returns a hold nobody stored."""
        with self.lock:
            self.writes += 1
            if key in self.by_key:
                hold = self.holds[self.by_key[key]]
                if (hold.batch, hold.scope) != (batch, scope):
                    raise Conflict(f"Idempotency-Key {key} was used for batch {hold.batch} scope {hold.scope}")
                return hold, 0.0
            now = datetime.now(timezone.utc)
            if self._take("accepted_then_dropped"):
                return Hold(self._new_id(), key, batch, scope, in_force, 0.0), 0.0
            delay = self.stale_window_s() if self._take("stale_read") else 0.0
            hold = Hold(self._new_id(), key, batch, scope, in_force, time.monotonic() + delay)
            self.holds[hold.hold_id] = hold
            self.by_key[key] = hold.hold_id
            self.log.append({"idempotency_key": key, "hold_id": hold.hold_id, "batch": batch, "scope": scope,
                             "version": self.version, "created_at": now.isoformat()})
            return hold, self._sleep_after_commit()

    def confirm(self, hold_id: str) -> tuple[Hold, float]:
        with self.lock:
            self.writes += 1
            hold = self.holds.get(hold_id)
            if hold is None:
                raise KeyError(hold_id)
            if self._take("accepted_then_dropped"):
                return replace(hold, in_force=True), 0.0
            hold.in_force = True
            return hold, self._sleep_after_commit()

    def _sleep_after_commit(self) -> float:
        return self.timeout_sleep_s if self._take("timeout_after_commit") else 0.0

    def visible_holds(self, batch: str) -> list[Hold]:
        now = time.monotonic()
        with self.lock:
            return [h for h in self.holds.values() if h.batch == batch and h.visible_after <= now]

    def accounts_for(self, batch: str) -> list[dict]:
        return [dict(a) for a in self.accounts] if batch == self.batch else []

    def log_view(self) -> list[dict]:
        with self.lock:
            return [{**row, "state": _v2_state(self.holds[row["hold_id"]])} for row in self.log]

    def view(self) -> dict:
        with self.lock:
            return {"name": self.name, "version": self.version, "batch": self.batch,
                    "faults": {k: v for k, v in self.faults.items() if v > 0},
                    "holds": len(self.holds), "writes": self.writes,
                    "seconds_per_sim_hour": self.seconds_per_sim_hour,
                    "stale_window_s": self.stale_window_s()}


def _v2_state(hold: Hold) -> str:
    return "active" if hold.in_force else "pending_confirm"


# ----------------------------------------------------------------------- app

def create_order_system(name: str, accounts: list[dict], *, batch: str, seconds_per_sim_hour: float = 30.0,
                  admin_token: str) -> FastAPI:
    """The order system app for one distributor. `accounts` are v1-shaped rows
    ({account, name, phone, qty_shipped, qty_in_stock}); `app.state.order system`
    is the OrderSystemState, for tests that need to look inside."""
    state = OrderSystemState(name, accounts, batch=batch, seconds_per_sim_hour=seconds_per_sim_hour)
    app = FastAPI(title=VENDOR_TITLE, version="2",
                  description=f"Partner API of distributor {name}: holds on recalled batches and the accounts "
                              "that received them. Holds may take up to 10 minutes to appear in GET /holds.")
    app.state.order_system = state

    @app.exception_handler(Gone)
    async def gone(_request: Request, exc: Gone) -> JSONResponse:
        return JSONResponse(status_code=410, content={
            "detail": str(exc), "current_version": 2, "docs": "/openapi.json"})

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.include_router(_v1_router(state))
    app.include_router(_v2_router(state))
    app.include_router(_admin_router(state, admin_token))
    return app


def _require_key(key: str | None) -> str:
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required on writes")
    return key


def _known_batch(state: OrderSystemState, batch: str) -> None:
    if batch != state.batch:
        raise HTTPException(status_code=404, detail=f"unknown batch {batch}")


def _v1_router(state: OrderSystemState) -> APIRouter:
    async def v1_only() -> None:
        if state.version != 1:
            raise Gone("API v1 was retired. Use v2: POST /v2/holds, POST /v2/holds/{hold_id}/confirm, "
                       "GET /v2/holds, GET /v2/accounts. Field changes are described in /openapi.json.")

    router = APIRouter(prefix="/v1", tags=["v1"], deprecated=True, dependencies=[Depends(v1_only)])

    @router.post("/holds", status_code=202, response_model=HoldAcceptedV1,
                 summary="Put a batch on hold for some accounts")
    async def place_hold_v1(body: HoldRequestV1, idempotency_key: str | None = Header(
            None, alias="Idempotency-Key", description="Repeat a write safely: the same key returns the same hold.")):
        _known_batch(state, body.batch)
        hold, sleep_s = state.place(_require_key(idempotency_key), body.batch, body.account_scope, in_force=True)
        await asyncio.sleep(sleep_s)
        return {"hold_id": hold.hold_id}

    @router.get("/holds", response_model=list[HoldV1], summary="Holds on a batch")
    async def list_holds_v1(batch: str = Query(description="Batch code to list holds for.")):
        # v1 has no pending holds: every v1 hold is in force from the start.
        return [{"hold_id": h.hold_id, "batch": h.batch, "account_scope": h.scope, "status": "on_hold"}
                for h in state.visible_holds(batch) if h.in_force]

    @router.get("/accounts", response_model=list[AccountV1], summary="Accounts that received a batch")
    async def list_accounts_v1(batch: str = Query(description="Batch code to list receiving accounts for.")):
        return state.accounts_for(batch)

    return router


def _v2_router(state: OrderSystemState) -> APIRouter:
    async def v2_available() -> None:
        if state.version < 2:
            raise HTTPException(status_code=404, detail="API v2 is not available on this order system yet")

    router = APIRouter(prefix="/v2", tags=["v2"], dependencies=[Depends(v2_available)])

    @router.post("/holds", status_code=202, response_model=HoldAcceptedV2,
                 summary="Request a hold on a batch (it must then be confirmed)")
    async def place_hold_v2(body: HoldRequestV2, idempotency_key: str | None = Header(
            None, alias="Idempotency-Key", description="Repeat a write safely: the same key returns the same hold.")):
        _known_batch(state, body.batch_code)
        hold, sleep_s = state.place(_require_key(idempotency_key), body.batch_code, body.scope, in_force=False)
        await asyncio.sleep(sleep_s)
        return {"hold_id": hold.hold_id, "state": _v2_state(hold)}

    @router.post("/holds/{hold_id}/confirm", response_model=HoldConfirmedV2,
                 summary="Confirm a pending hold. A hold has no effect until it is confirmed")
    async def confirm_hold_v2(hold_id: str, idempotency_key: str | None = Header(
            None, alias="Idempotency-Key", description="Repeat a write safely.")):
        _require_key(idempotency_key)
        try:
            hold, sleep_s = state.confirm(hold_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown hold {hold_id}") from None
        await asyncio.sleep(sleep_s)
        return {"hold_id": hold.hold_id, "state": "active"}

    @router.get("/holds", response_model=list[HoldV2], summary="Holds on a batch")
    async def list_holds_v2(batch_code: str = Query(description="Batch code to list holds for.")):
        return [{"hold_id": h.hold_id, "batch_code": h.batch, "scope": h.scope, "state": _v2_state(h)}
                for h in state.visible_holds(batch_code)]

    @router.get("/accounts", response_model=list[AccountV2], summary="Accounts that received a batch")
    async def list_accounts_v2(batch_code: str = Query(description="Batch code to list receiving accounts for.")):
        return [{"account": a["account"], "name": a["name"], "phone": a["phone"], "shipped": a["qty_shipped"],
                 "inventory": {"available": a["qty_in_stock"], "quarantined": 0}}
                for a in state.accounts_for(batch_code)]

    return router


def _admin_router(state: OrderSystemState, admin_token: str) -> APIRouter:
    async def admin_only(x_admin_token: str | None = Header(None, alias="X-Admin-Token")) -> None:
        # An unset token refuses everyone, so an order system started without one is not open.
        if not (x_admin_token and admin_token
                and hmac.compare_digest(x_admin_token.encode(), admin_token.encode())):
            raise HTTPException(status_code=401, detail="bad or missing X-Admin-Token")

    router = APIRouter(include_in_schema=False)
    admin = APIRouter(prefix="/_admin", include_in_schema=False, dependencies=[Depends(admin_only)])

    @router.get("/_log/holds")
    async def holds_log() -> dict:
        return {"order_system": state.name, "holds": state.log_view()}

    @admin.get("/state")
    async def admin_state() -> dict:
        return state.view()

    @admin.post("/version")
    async def set_version(body: VersionBody) -> dict:
        with state.lock:
            state.version = body.version
        return state.view()

    @admin.post("/fault")
    async def arm_fault(body: FaultBody) -> dict:
        with state.lock:
            state.faults[body.fault] = body.count
            if body.sleep_s is not None:
                state.timeout_sleep_s = body.sleep_s
        return state.view()

    @admin.post("/clock")
    async def set_clock(body: ClockBody) -> dict:
        with state.lock:
            state.seconds_per_sim_hour = body.seconds_per_sim_hour
        return state.view()

    @admin.post("/reset")
    async def reset() -> dict:
        state.reset()
        return state.view()

    router.include_router(admin)
    return router


# ----------------------------------------------------------------------- CLI

def load_order_system_config(scenario: str | Path, name: str) -> dict:
    """{"batch", "accounts", "seconds_per_sim_hour"} for one order system from a
    scenario file's "order systems" section (a scenario name or a path to its JSON)."""
    from ..config import SCENARIO_DIR

    path = Path(scenario)
    if path.suffix != ".json":
        path = SCENARIO_DIR / f"{scenario}.json"
    if not path.exists():
        raise SystemExit(f"scenario file {path} not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    order_systems = data.get("order_systems") or {}
    if name not in order_systems:
        raise SystemExit(f"{path.name} has no order system {name!r} (order systems: {', '.join(sorted(order_systems)) or 'none'})")
    entry = order_systems[name]
    try:
        accounts = [AccountV1(**a).model_dump() for a in entry["accounts"]]
    except (KeyError, TypeError, ValidationError) as exc:
        raise SystemExit(f"{path.name} order systems.{name}.accounts is malformed: {exc}") from None
    speed = (data.get("schedule") or {}).get("seconds_per_sim_hour") or 30.0
    return {"batch": entry["batch"], "accounts": accounts, "seconds_per_sim_hour": float(speed)}


def main(argv=None) -> None:
    import uvicorn

    from ..config import Settings
    from ..systems.order_client import DEFAULT_PORTS

    ap = argparse.ArgumentParser(prog="python -m levela.mock.order_system", description="Run one mock distributor order system.")
    ap.add_argument("--name", required=True, help="order system name, for example north or east")
    ap.add_argument("--port", type=int, help="default: 8101 for north, 8102 for east")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--scenario", default="recall_long", help="scenario name or path whose order systems section to serve")
    ap.add_argument("--admin-token", help="default: ADMIN_TOKEN from the environment")
    ap.add_argument("--seconds-per-sim-hour", type=float, help="default: the scenario's schedule, else 30")
    args = ap.parse_args(argv)

    port = args.port or DEFAULT_PORTS.get(args.name)
    if port is None:
        raise SystemExit(f"no default port for order system {args.name!r}: pass --port")
    config = load_order_system_config(args.scenario, args.name)
    app = create_order_system(args.name, config["accounts"], batch=config["batch"],
                        seconds_per_sim_hour=args.seconds_per_sim_hour or config["seconds_per_sim_hour"],
                        admin_token=args.admin_token or Settings.from_env().admin_token)
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
