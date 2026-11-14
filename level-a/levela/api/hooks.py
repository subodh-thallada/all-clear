"""POST /hooks/post_call: ElevenLabs' post-call webhook (the simulator signs
its payloads the same way, so both take this one path).

The signature is checked over the raw bytes before anything is parsed. The
endpoint is public, so a request whose signature header is missing or
malformed is refused before its body is read, and a body is never read past
MAX_BODY. After a valid signature the reply is always 200, even for an
attempt we cannot match, because ElevenLabs does not retry by default and a
non-200 would only lose the evidence. Redeliveries are harmless: closing an
attempt is a conditional update, and the transcript is saved once.

Event types:
  post_call_transcription  close the attempt (completed, or failed when the
                           conversation failed) and keep the transcript
  call_initiation_failure  the phone never connected: busy/unknown -> failed,
                           no-answer -> no_answer. It carries no dynamic
                           variables, so the attempt is matched by
                           conversation_id, then CallSid, then the dialled number
  post_call_audio          logged without the audio, otherwise ignored
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pymongo import DESCENDING

from .. import feed, signing
from ..orchestrator import attempts
from .deps import read_capped, party_name, utcnow

log = logging.getLogger("levela.api.hooks")
router = APIRouter()

# A transcription webhook is tens of kilobytes. The agent subscribes to
# transcript and call_initiation_failure only (agent/agent_config.json); 16 MiB
# still takes a long call's base64 audio should that event ever be switched on,
# while bounding what one forged request can make the host hold.
MAX_BODY = 16 * 1024 * 1024


async def signed_body(request: Request, signature: str | None = Header(None, alias=signing.HEADER)) -> bytes:
    """The webhook's raw bytes, read only when the signature header could be
    a signature at all, and never past MAX_BODY. The HMAC itself can only be
    checked over the whole body, in the endpoint."""
    if not signing.well_formed(signature):
        raise HTTPException(status_code=401, detail=f"bad or missing {signing.HEADER}")
    body = await read_capped(request, MAX_BODY)
    if body is None:
        raise HTTPException(status_code=413, detail=f"post-call webhook larger than {MAX_BODY} bytes")
    return body


@router.post("/hooks/post_call")
def post_call(request: Request, body: bytes = Depends(signed_body),
              signature: str | None = Header(None, alias=signing.HEADER)) -> dict:
    st = request.app.state
    if not signing.verify(body, signature, st.settings.webhook_secret):
        raise HTTPException(status_code=401, detail=f"bad or missing {signing.HEADER}")
    try:
        event = json.loads(body)
    except ValueError:
        log.warning("signed post-call webhook is not JSON (%d bytes)", len(body))
        return {"ok": False, "error": "body is not JSON"}
    if not isinstance(event, dict):
        return {"ok": False, "error": "body is not a JSON object"}
    return handle_post_call(st.db, event)


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _call_sid(data: dict) -> str | None:
    meta = _obj(data.get("metadata"))
    return _text(_obj(meta.get("phone_call")).get("call_sid")) or _text(_obj(meta.get("body")).get("CallSid"))


def handle_post_call(db, event: dict, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    kind = event.get("type")
    data = _obj(event.get("data"))
    attempt = find_attempt(db, kind, data)
    _log_event(db, event, data, attempt, now)
    if kind == "post_call_audio":
        return {"ok": True, "ignored": kind}
    if kind not in ("post_call_transcription", "call_initiation_failure"):
        log.info("post-call webhook of type %r ignored", kind)
        return {"ok": True, "ignored": kind}
    if attempt is None:
        conv = _text(data.get("conversation_id"))
        feed.emit(db, "post_call", f"Post-call webhook ({kind}) matched no attempt (conversation {conv})",
                  level="warn", detail={"type": kind, "conversation_id": conv, "call_sid": _call_sid(data)})
        return {"ok": True, "matched": False}
    if kind == "post_call_transcription":
        changed = _finish_transcription(db, attempt, data, now)
    else:
        changed = _finish_failure(db, attempt, data, now)
    state = db.attempts.find_one({"_id": attempt["_id"]}, {"state": 1})["state"]
    return {"ok": True, "matched": True, "attempt_id": attempt["_id"], "state": state, "changed": changed}


def find_attempt(db, kind, data: dict) -> dict | None:
    """The attempt a post-call event belongs to: the attempt_id we sent as a
    dynamic variable, else the conversation, else the Twilio call SID; for an
    initiation failure (no dynamic variables at all) finally the newest live
    attempt to the dialled number."""
    dyn = _obj(_obj(data.get("conversation_initiation_client_data")).get("dynamic_variables"))
    keys = (("_id", _text(dyn.get("attempt_id"))), ("conversation_id", _text(data.get("conversation_id"))),
            ("call_sid", _call_sid(data)))
    for field, value in keys:
        found = db.attempts.find_one({field: value}) if value else None
        if found:
            return found
    to_number = _text(_obj(_obj(data.get("metadata")).get("body")).get("To"))
    if kind == "call_initiation_failure" and to_number:
        return db.attempts.find_one({"to_number": to_number, "state": {"$in": attempts.LIVE}},
                                    sort=[("intent_at", DESCENDING)])
    return None


def _log_event(db, event: dict, data: dict, attempt: dict | None, now: datetime) -> None:
    """Evidence for recovery, like a tool call. The base64 audio is dropped:
    it is large and proves nothing the transcript does not."""
    payload = {**event, "data": {k: v for k, v in data.items() if k != "full_audio"}} if data else event
    db.tool_events.insert_one({
        "kind": "post_call",
        "attempt_id": attempt["_id"] if attempt else None,
        "party_id": attempt["party_id"] if attempt else None,
        "recall_id": attempt["recall_id"] if attempt else None,
        "conversation_id": _text(data.get("conversation_id")), "call_sid": _call_sid(data),
        "source": "sim" if attempt and attempt.get("channel") == "sim" else "voice",
        "payload": payload, "received_at": now,
    })


def _fill_ids(db, attempt_id: str, conversation_id: str | None, call_sid: str | None) -> None:
    """Save identifiers the attempt is still missing (the worker may have died
    before recording the provider's answer)."""
    for field, value in (("conversation_id", conversation_id), ("call_sid", call_sid)):
        if value:
            db.attempts.update_one({"_id": attempt_id, field: None}, {"$set": {field: value}})


def _transcript(turns) -> list[dict]:
    """[{role, message}] for the turns that said something (tool-call turns have no message)."""
    return [{"role": t.get("role"), "message": t["message"]} for t in turns or []
            if isinstance(t, dict) and isinstance(t.get("message"), str) and t["message"]]


def _finish_transcription(db, attempt: dict, data: dict, now: datetime) -> bool:
    meta = _obj(data.get("metadata"))
    reason = _text(meta.get("termination_reason"))
    state = "failed" if data.get("status") == "failed" else "completed"
    closed = attempts.complete(db, attempt["_id"], state, reason=reason, now=now)
    saved = db.attempts.update_one({"_id": attempt["_id"], "transcript": {"$exists": False}}, {"$set": {
        "transcript": _transcript(data.get("transcript")),
        "call_duration_secs": meta.get("call_duration_secs"),
        "termination_reason": reason,
        "summary": _obj(data.get("analysis")).get("transcript_summary"),
    }}).modified_count == 1
    _fill_ids(db, attempt["_id"], _text(data.get("conversation_id")), _call_sid(data))
    if closed or saved:
        name = party_name(db, attempt["recall_id"], attempt["party_id"])
        duration = meta.get("call_duration_secs")
        if state == "failed":
            text, level = f"Call with {name} failed" + (f" ({reason})" if reason else ""), "warn"
        elif isinstance(duration, (int, float)):
            text, level = f"Call with {name} ended after {round(duration)} s", "info"
        else:
            text, level = f"Call with {name} ended", "info"
        feed.emit(db, "post_call", text, level=level, recall_id=attempt["recall_id"], party_id=attempt["party_id"],
                  attempt_id=attempt["_id"], detail={"state": state, "termination_reason": reason,
                                                     "call_duration_secs": duration})
    return closed or saved


def _finish_failure(db, attempt: dict, data: dict, now: datetime) -> bool:
    reason = _text(data.get("failure_reason")) or "unknown"
    state = "no_answer" if reason.replace("_", "-") == "no-answer" else "failed"
    closed = attempts.complete(db, attempt["_id"], state, reason=reason, now=now)
    _fill_ids(db, attempt["_id"], _text(data.get("conversation_id")), _call_sid(data))
    if closed:
        name = party_name(db, attempt["recall_id"], attempt["party_id"])
        text = f"Call to {name} was not answered" if state == "no_answer" else f"Call to {name} failed: {reason}"
        feed.emit(db, "post_call", text, level="warn", recall_id=attempt["recall_id"], party_id=attempt["party_id"],
                  attempt_id=attempt["_id"], detail={"state": state, "failure_reason": reason})
    return closed
