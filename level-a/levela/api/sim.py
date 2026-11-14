"""/sim/*: the simulator's telephony vendor, hosted by the API process.

POST /sim/calls stands in for ElevenLabs' outbound-call API (the worker's
SimVoice posts here), and GET /sim/calls stands in for Twilio's Calls list.
POST /sim/form_link tells the network a simulated party received its form
link; its behaviour profile decides whether and when it submits.
The network lives in the API process so that killing the worker does not
kill a simulated call, exactly as with a real phone call.

The network is the `sim_network` passed to create_app (tests inject one), or
a SimNetwork built on first use that posts back to this API over HTTP.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pymongo import DESCENDING

from ..providers.base import ProviderError
from .deps import jsonable, require_tool_secret

log = logging.getLogger("levela.api.sim")
router = APIRouter(prefix="/sim", dependencies=[Depends(require_tool_secret)])

MAX_ROWS = 200


class PlaceCall(BaseModel):
    to_number: str
    attempt_id: str
    dynamic_variables: dict[str, Any] = {}
    faults: dict[str, Any] | None = None


class FormLink(BaseModel):
    recall_id: str
    party_id: str
    token: str


def network(state):
    """The simulated phone network, built once per app on first use. Imported
    lazily so the API starts (and tests run) without the simulator module."""
    if state.sim is None:
        with state.sim_lock:
            if state.sim is None:
                from ..providers.sim import HttpPoster, SimNetwork
                s = state.settings
                state.sim = SimNetwork(state.db, HttpPoster(s.api_base_url, s.tool_secret), s)
    return state.sim


def _placed_ids(placed) -> tuple[str | None, str | None]:
    """(conversation_id, call_sid) from a PlacedCall or a dict reply."""
    if isinstance(placed, dict):
        return placed.get("conversation_id"), placed.get("call_sid") or placed.get("callSid")
    return getattr(placed, "conversation_id", None), getattr(placed, "call_sid", None)


@router.post("/calls")
def place_call(body: PlaceCall, request: Request) -> dict:
    """Same reply shape as ElevenLabs' outbound call: success, message,
    conversation_id, callSid (camelCase, as the vendor spells it)."""
    try:
        placed = network(request.app.state).place_call(
            to_number=body.to_number, dynamic_variables=body.dynamic_variables,
            attempt_id=body.attempt_id, faults=body.faults)
    except ProviderError as exc:
        log.warning("simulated call to %s refused: %s", body.to_number, exc)
        return {"success": False, "message": str(exc), "conversation_id": None, "callSid": None}
    conversation_id, call_sid = _placed_ids(placed)
    return {"success": True, "message": "Simulated call placed", "conversation_id": conversation_id,
            "callSid": call_sid}


@router.post("/form_link")
def form_link(body: FormLink, request: Request) -> dict:
    """The simulated counterpart of texting a party its form link (V2 section 8):
    the party's behaviour profile decides whether and when it submits through
    POST /form/{token}. Delivering the same link twice schedules one submission."""
    try:
        return network(request.app.state).form_link_delivered(
            recall_id=body.recall_id, party_id=body.party_id, token=body.token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _since(value: str) -> datetime:
    """ISO 8601 (a date, or a time with "Z" or an offset); naive means UTC."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be an ISO 8601 date or time") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row(doc: dict) -> dict:
    return jsonable({"sid": doc["_id"], **doc})


@router.get("/calls")
def list_calls(request: Request, to: str | None = None, since: str | None = None) -> dict:
    """Calls newest first, optionally to one number and starting at or after `since`.
    An unencoded "+" in the query arrives as a space, so it is restored."""
    query: dict = {}
    if to:
        number = to.strip()
        query["to"] = f"+{number}" if number[:1].isdigit() else number
    if since:
        query["start_time"] = {"$gte": _since(since)}
    rows = request.app.state.db.sim_calls.find(query).sort("start_time", DESCENDING).limit(MAX_ROWS)
    return {"calls": [_row(doc) for doc in rows], "next_page_uri": None}


@router.get("/calls/{sid}")
def get_call(sid: str, request: Request) -> dict:
    doc = request.app.state.db.sim_calls.find_one({"_id": sid})
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no simulated call {sid}")
    return _row(doc)
