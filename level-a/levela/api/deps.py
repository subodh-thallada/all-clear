"""Shared helpers for the API routers: auth, raw bodies, JSON encoding, views.

Every router reads the settings, the Mongo client and the database from
`request.app.state`, so one create_app() call wires the whole API and tests
can build as many isolated apps (one scratch database each) as they like.
"""
from __future__ import annotations

import hmac
import json
from contextlib import aclosing
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from .. import clock
from ..config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """Mongo datetimes are UTC; a client built without tz_aware returns them
    naive, and naive/aware arithmetic would raise in the age calculations."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def secret_matches(given: str | None, expected: str) -> bool:
    """Constant-time comparison. An unset secret never matches, so a host that
    forgot to configure one refuses everyone instead of admitting everyone."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_tool_secret(request: Request,
                        x_tool_secret: str | None = Header(None, alias="X-Tool-Secret")) -> None:
    """/tools/* and /sim/*: the ElevenLabs tool and the simulator share this secret."""
    if not secret_matches(x_tool_secret, request.app.state.settings.tool_secret):
        raise HTTPException(status_code=401, detail="bad or missing X-Tool-Secret")


def require_admin(request: Request,
                  x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
                  key: str | None = Query(None, description="admin token, for the wall's buttons")) -> None:
    """/admin/*: the API is on a public host, so every admin action is authenticated."""
    expected = request.app.state.settings.admin_token
    if not (secret_matches(x_admin_token, expected) or secret_matches(key, expected)):
        raise HTTPException(status_code=401, detail="bad or missing X-Admin-Token")


# Body caps. Neither uvicorn nor Starlette limits a request body, and the API
# runs on a 512 MB host beside the scheduler and the simulator's calls, so
# every raw body is read through read_capped(). A tool call is a few hundred
# bytes; 64 KiB is generous. The post-call webhook's own cap is in hooks.py
# and the form's in form.py.
MAX_TOOL_BODY = 64 * 1024


async def read_capped(request: Request, limit: int) -> bytes | None:
    """The exact bytes received, or None when the body is larger than `limit`.

    A declared Content-Length over the cap is refused before anything is
    read; otherwise the stream is read chunk by chunk and abandoned as soon as
    it passes the cap, so an oversized body never sits in memory whole
    (request.body() would buffer all of it first). Endpoints are sync
    (pymongo is sync) and cannot await the body themselves, and the post-call
    HMAC must be checked over these bytes before anything parses them."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None             # not a length a client that means well would send
    buf = bytearray()
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            buf += chunk
            if len(buf) > limit:
                return None
    return bytes(buf)


def capped_body(limit: int):
    """A dependency giving the raw body, or 413 when it is larger than `limit`.
    Put it after the route's auth dependency (route `dependencies=` run
    first), so an unauthenticated request is refused before its body is read."""
    async def dependency(request: Request) -> bytes:
        body = await read_capped(request, limit)
        if body is None:
            raise HTTPException(status_code=413, detail=f"request body larger than {limit} bytes")
        return body
    return dependency


tool_body = capped_body(MAX_TOOL_BODY)


def json_object(body: bytes) -> dict:
    """A tool body: a JSON object, else a 400 (the only non-200 a well-meant
    tool call can get, because the agent reads the JSON, not the status)."""
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return payload


def text(value) -> str | None:
    """Identifiers arrive from an LLM tool call: keep only non-empty strings."""
    return value if isinstance(value, str) and value else None


def sim_hour(state, recall_id: str) -> float:
    """The recall's simulated time now, on the database's clock (V2 §2)."""
    return clock.sim_now(state.db, recall_id, state.client)


def _iso(dt: datetime) -> str:
    return aware(dt).isoformat()


def jsonable(obj):
    """Mongo documents -> JSON-ready values (ObjectId as str, datetimes as ISO 8601 UTC)."""
    return jsonable_encoder(obj, custom_encoder={ObjectId: str, datetime: _iso})


def form_url(settings: Settings, token: str | None) -> str | None:
    """The fallback form for one party, on the host the parties can reach."""
    return f"{settings.webhook_base}/form/{token}" if token else None


def party_name(db, recall_id: str | None, party_id: str | None) -> str:
    """A party's display name for feed lines; the id when the party is unknown."""
    if recall_id and party_id:
        row = db.parties.find_one({"recall_id": recall_id, "party_id": party_id}, {"name": 1})
        if row and row.get("name"):
            return row["name"]
    return party_id or "unknown party"


def worker_view(doc: dict | None, now: datetime, settings: Settings) -> dict | None:
    """One worker_state "worker:<name>" document as the wall and status show it.
    Alive means a heartbeat younger than worker_dead_after_s and no clean stop
    recorded after that heartbeat."""
    if doc is None:
        return None
    beat = doc.get("heartbeat_at")
    age = (now - aware(beat)).total_seconds() if beat else None
    stopped = doc.get("stopped_at")
    stopped_after_beat = bool(stopped and beat and aware(stopped) >= aware(beat))
    alive = age is not None and age < settings.worker_dead_after_s and not stopped_after_beat
    return {
        "name": str(doc["_id"]).removeprefix("worker:"),
        "alive": alive,
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "boot_id": doc.get("boot_id"),
        "resumed": doc.get("resumed"),
    }
