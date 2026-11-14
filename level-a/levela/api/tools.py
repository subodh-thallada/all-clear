"""/tools/*: what reaches the ledger from outside, and book_claim(), the one
claim path that the voice agent, the simulator, the fallback form and the
reports all take.

  POST /tools/submit_count      the voice agent's counts (and the simulator's)
  POST /tools/submit_answer     the voice agent's answer to an open question
  POST /tools/request_callback  the party asked to be called back at a time
  POST /tools/report            counts that arrive without a call: the
                                distributor's rep, a form submission relayed
                                by the simulator, an order system read

The agent never knows shipped_in or the affected batches. It
learns of a mismatch only from the submit_count reply, which names the two
values that disagree, so every challenge it speaks originates in Atlas. The
agent's tools always answer HTTP 200 for a well-formed call because the agent
reads the JSON, not the status code.

The challenge cap is enforced here rather than trusted to the prompt: a party
whose counts are refused more than MAX_CHALLENGES times on one call goes to a
person (parties.escalation = "human") and the agent is told to stop asking.
Rejections that asking again on the same call cannot fix (STOP_CODES) end the
call at once, so they cannot loop either.

Every body is read through a capped reader after the tool secret is checked
(deps.tool_body), and every number is made storable before it is logged:
an unauthenticated request costs no memory, and a 20-digit count is a
rejection, not a 500.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Literal

from bson.errors import InvalidDocument
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from pymongo.errors import PyMongoError

from .. import clock, feed
from ..config import MAX_CHALLENGES
from ..ledger.claims import CHALLENGE_CODES, ClaimError, challenges_used, checks, counts, storable, submit_claim
from ..orchestrator import attempts
from . import evidence, questions
from .deps import jsonable, json_object, require_tool_secret, sim_hour, text, tool_body, utcnow

log = logging.getLogger("levela.api.tools")
router = APIRouter()

# Body keys that are the claim itself (the rest identify the call). `forwards`
# is the multi-destination form of forwarded_qty/forwarded_to_name (V2 §9);
# `received_from` names the sender of a receipt for a party with several;
# batch_seen, sold_source and block_test are a store's three checks.
CLAIM_KEYS = ("sold", "on_shelf", "held", "returned", "forwarded_qty", "forwarded_to_name",
              "received_qty", "received_from", "batch_seen", "sold_source", "block_test", "delivery_note",
              "forwards")
BUCKET_LABELS = (("sold", "sold"), ("on_shelf", "on the shelf"), ("held", "held"), ("returned", "returned"))
# Rejections after which the call has nothing left to do: asking again cannot
# change the answer on this call. forward_changed / forward_restate mean the
# receiving party already counted a transfer the party now leaves out, and
# receipt_sender_ambiguous needs the sending party named, which the voice tool
# does not carry; left as plain rejections they let a call loop with no cap.
STOP_CODES = {"no_inbound_edge", "recall_closed", "forward_changed", "forward_restate", "receipt_sender_ambiguous"}
# Errors that mean "not recorded" without being the agent's fault: the
# database is unreachable, or a value could not be encoded (a backstop: the
# claim path makes every number storable first).
UNRECORDED = (PyMongoError, OverflowError, InvalidDocument)
# A callback further out than this is a misheard time, not a plan.
MAX_CALLBACK_AHEAD_H = 24.0 * 14
# Where a report's feed line belongs on the wall.
REPORT_SYSTEMS = {"rep": "phone", "form": "form"}

UNMATCHED_MESSAGE = ("This call could not be matched to an open recall contact, so nothing was recorded. "
                     "Thank them, say a person from the recall team will follow up, and end the call.")
CAP_MESSAGE = ("The counts still do not match our records after two checks. Do not ask again. "
               "Thank them, tell them a person from the recall team will follow up, and end the call.")
OUTAGE_MESSAGE = ("The system could not record that answer just now. Thank them, say the recall team "
                  "will call back shortly, and end the call.")


def _outage(db, tool: str, payload: dict, exc: Exception) -> JSONResponse:
    """Transient errors were already retried by the driver and the transaction
    helper; tell the agent to end politely so the worker can call back,
    rather than leaving it waiting on a 500."""
    log.exception("%s failed for attempt %r", tool, payload.get("attempt_id"))
    feed.emit(db, "error", f"{tool} could not be recorded: {exc.__class__.__name__}", level="bad",
              attempt_id=text(payload.get("attempt_id")), party_id=text(payload.get("party_id")),
              detail={"error": str(exc)})
    return JSONResponse({"accepted": False, "message": OUTAGE_MESSAGE, "challenges_left": 0, "stop": True})


@router.post("/tools/submit_count", dependencies=[Depends(require_tool_secret)])
def submit_count(request: Request, body: bytes = Depends(tool_body)) -> JSONResponse:
    """The ElevenLabs webhook tool (and the simulator, which posts the same body)."""
    payload = json_object(body)
    st = request.app.state
    try:
        return JSONResponse(handle_tool_call(st.client, st.db, payload))
    except UNRECORDED as exc:
        return _outage(st.db, "submit_count", payload, exc)


@router.post("/tools/submit_answer", dependencies=[Depends(require_tool_secret)])
def submit_answer(request: Request, body: bytes = Depends(tool_body)) -> JSONResponse:
    """Body: attempt_id, party_id, question_id, value. Reply: accepted, message, questions_left."""
    payload = json_object(body)
    st = request.app.state
    try:
        return JSONResponse(handle_answer(st, payload))
    except UNRECORDED as exc:
        return _outage(st.db, "submit_answer", payload, exc)


@router.post("/tools/request_callback", dependencies=[Depends(require_tool_secret)])
def request_callback(request: Request, body: bytes = Depends(tool_body)) -> JSONResponse:
    """Body: attempt_id, party_id, and callback_at (simulated hour) or
    hour_of_day with an optional day_offset. The harness reads
    attempts.callback_at to set the party's timer."""
    payload = json_object(body)
    st = request.app.state
    try:
        return JSONResponse(handle_callback(st, payload))
    except UNRECORDED as exc:
        return _outage(st.db, "request_callback", payload, exc)


# ---- the call's identity --------------------------------------------------------

def _source(body: dict) -> str:
    """Only the simulator (which holds the tool secret too) says it is the simulator."""
    return "sim" if body.get("source") == "sim" else "voice"


def _attempt(db, body: dict) -> dict | None:
    attempt_id = text(body.get("attempt_id"))
    return db.attempts.find_one({"_id": attempt_id}) if attempt_id else None


def _log_call_event(db, body: dict, attempt: dict | None, kind: str, now: datetime) -> None:
    """Written before anything else: it is the evidence recovery uses to adopt
    an in-doubt attempt instead of redialling. The payload is made storable
    first: an integer beyond 64 bits made the insert raise, so the call got a
    500 and left no evidence at all."""
    body = storable(body)
    evidence.log_event(db, kind=kind, attempt_id=body.get("attempt_id"), party_id=body.get("party_id"),
                       recall_id=attempt["recall_id"] if attempt else None, source=_source(body), payload=body,
                       now=now, conversation_id=body.get("system__conversation_id"),
                       call_sid=body.get("system__call_sid"))


def _matched_call(db, body: dict, kind: str, now: datetime) -> tuple[dict | None, dict | None]:
    """Log the delivery, then (attempt, None) when it belongs to a known
    attempt for the named party, else (None, the reply that stops the call).
    A matched delivery marks the call connected: the party is on the line."""
    attempt = _attempt(db, body)
    _log_call_event(db, body, attempt, kind, now)
    if attempt is None or attempt["party_id"] != body.get("party_id"):
        return None, _refuse_unmatched(db, attempt, body)
    attempts.mark_connected(db, attempt["_id"], conversation_id=text(body.get("system__conversation_id")),
                            call_sid=text(body.get("system__call_sid")), now=now)
    return attempt, None


def _refuse_unmatched(db, attempt: dict | None, body: dict) -> dict:
    attempt_id, party_id = body.get("attempt_id"), body.get("party_id")
    if attempt is None:
        line = f"Tool call for unknown attempt {attempt_id!r} ignored"
        recall_id = None
    else:
        line = (f"Tool call for attempt {attempt_id} names party {party_id!r}, "
                f"but the attempt is for {attempt['party_id']}: ignored")
        recall_id = attempt["recall_id"]
    feed.emit(db, "error", line, level="bad", recall_id=recall_id, party_id=text(party_id),
              attempt_id=text(attempt_id), detail={"payload": body})
    return {"accepted": False, "message": UNMATCHED_MESSAGE, "challenges_left": 0, "stop": True}


def _mark_reached(db, recall_id: str, party_id: str) -> None:
    db.parties.update_one({"recall_id": recall_id, "party_id": party_id,
                          "state": {"$in": ["pending", "contacting"]}}, {"$set": {"state": "reached"}})


# ---- submit_count ---------------------------------------------------------------

def handle_tool_call(client, db, body: dict, *, now: datetime | None = None) -> dict:
    """Log the delivery, check it belongs to a known attempt, book the claim,
    and build the reply the agent reads."""
    now = now or utcnow()
    attempt, refusal = _matched_call(db, body, "tool_call", now)
    if refusal:
        return refusal
    raw = {k: body[k] for k in CLAIM_KEYS if k in body}
    result, party = book_claim(client, db, attempt, raw, source=_source(body), now=now)
    return _tool_reply(db, attempt, party, result)


def book_claim(client, db, attempt: dict, raw: dict, *, source: str, now: datetime | None = None,
               require_delivery_note: bool | None = None) -> tuple[dict, dict | None]:
    """Book one claim for an attempt (or a report's evidence) and announce it on the wall.

    Shared by the voice tool, the fallback form and /tools/report so all
    produce the same ledger rows and the same feed lines. `attempt` needs
    `_id`, `recall_id`, `party_id` and optionally `context`. The delivery
    note is required when `require_delivery_note` says so, or, when it is
    None, when the call's context asked for it (the voice path). Returns
    (submit_claim result, the party as it was before the claim).
    """
    recall_id, party_id = attempt["recall_id"], attempt["party_id"]
    party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id})
    _mark_reached(db, recall_id, party_id)
    raw = _without_idle_receipt(db, raw, party)
    if require_delivery_note is None:
        require_delivery_note = (attempt.get("context") or {}).get("ask_delivery_note") == "yes"
    result = submit_claim(client, db, recall_id=recall_id, party_id=party_id, attempt_id=attempt["_id"],
                          source=source, raw=raw, now=now, require_delivery_note=require_delivery_note)
    announce(db, result, attempt=attempt, party=party, raw=raw, now=now)
    return result, party


def receives_transfers(db, party: dict | None) -> bool:
    """Whether a party has a receipt to give: any party some sender reported
    forwarding tins to, whatever its tier. A party below tier 1 is asked
    even before its sender reports (the ledger then answers no_inbound_edge)."""
    if party is None:
        return False
    if party.get("tier") != 1:
        return True
    return db.edges.find_one({"recall_id": party["recall_id"], "to_party": party["party_id"]}, {"_id": 1}) is not None


def _without_idle_receipt(db, raw: dict, party: dict | None) -> dict:
    """A tier 1 party that nobody forwarded tins to has no transfer to
    confirm. An LLM that fills the optional received_qty with 0 must not turn
    its counts into a receipt (the ledger would refuse it as no_inbound_edge
    and end the call). A tier 1 party with an inbound edge keeps its receipt,
    0 included: "we got none from them" is its side of that transfer."""
    if party is None or party.get("tier") != 1 or "received_qty" not in raw:
        return raw
    try:
        idle = counts({"received_qty": raw["received_qty"]})["received_qty"] in (None, 0)
    except ClaimError:
        return raw
    if not idle or receives_transfers(db, party):
        return raw
    return {k: v for k, v in raw.items() if k != "received_qty"}


def _tool_reply(db, attempt: dict, party: dict | None, result: dict) -> dict:
    used = challenges_used(db, attempt["_id"])
    code = None if result["accepted"] else result.get("code")
    message, stop = result["message"], False
    if code in CHALLENGE_CODES and used > MAX_CHALLENGES:
        message, stop = CAP_MESSAGE, True
        _escalate(db, attempt, party, used)
    elif code in CHALLENGE_CODES:
        last = " This is the last time you may ask." if used == MAX_CHALLENGES else ""
        message = f"{message} Challenge {used} of {MAX_CHALLENGES}.{last}"
    elif code in STOP_CODES:
        stop = True
    reply = {
        "accepted": result["accepted"], "message": message, "reason": result.get("reason"),
        "reported_total": result.get("reported_total"), "shipped_in": result.get("shipped_in"),
        "values": result.get("values"), "duplicate": result.get("duplicate"),
        "challenges_left": max(0, MAX_CHALLENGES - used), "stop": stop,
    }
    return {k: v for k, v in reply.items() if v is not None}


def _escalate(db, attempt: dict, party: dict | None, used: int) -> None:
    """Hand the party to a person. Conditional, so a redelivered third
    rejection does not announce it twice."""
    res = db.parties.update_one({"recall_id": attempt["recall_id"], "party_id": attempt["party_id"],
                                "escalation": {"$ne": "human"}}, {"$set": {"escalation": "human"}})
    if res.modified_count:
        name = (party or {}).get("name") or attempt["party_id"]
        feed.emit(db, "needs_human",
                  f"{name} needs a person: counts refused {used} times on one call. "
                  "The recall team will follow up",
                  level="bad", recall_id=attempt["recall_id"], party_id=attempt["party_id"],
                  attempt_id=attempt["_id"], detail={"challenges": used, "max_challenges": MAX_CHALLENGES})


# ---- submit_answer --------------------------------------------------------------

def handle_answer(state, body: dict, *, now: datetime | None = None) -> dict:
    """Record one answer to an open question (spec "Questions as data" step 4)."""
    db, now = state.db, now or utcnow()
    attempt, refusal = _matched_call(db, body, "answer", now)
    if refusal:
        return refusal
    rid, sid = attempt["recall_id"], attempt["party_id"]
    qid = text(body.get("question_id"))
    question = questions.get_question(db, rid, qid) if qid else None
    if question is None:
        return {"accepted": False, "stop": False, "questions_left": None,
                "message": f"There is no question {qid!r} for this recall. Do not ask it; carry on with the call."}
    _mark_reached(db, rid, sid)
    party = db.parties.find_one({"recall_id": rid, "party_id": sid})
    sim = sim_hour(state, rid)
    try:
        out = questions.record_answer(db, party=party, attempt_id=attempt["_id"], question=question,
                                      value=body.get("value"), sim_now=sim)
    except questions.QuestionError as exc:
        return {"accepted": False, "stop": False, "questions_left": len(party.get("open_questions") or []),
                "message": f"Not recorded: {exc.message}. Read the question back and ask again."}
    if not out["duplicate"]:
        _announce_answer(db, party, attempt, question, out, sim)
    left = out["questions_left"]
    rest = f" {left} more question{'s' if left != 1 else ''} to ask." if left else " No more questions."
    reply = {"accepted": True, "message": "Recorded." + rest, "questions_left": left}
    return {**reply, "duplicate": True} if out["duplicate"] else reply


def _announce_answer(db, party: dict, attempt: dict, question: dict, out: dict, sim: float) -> None:
    unit = (db.recalls.find_one({"_id": party["recall_id"]}, {"unit": 1}) or {}).get("unit") or "tin"
    said = questions.answer_text(question["answer_type"], out["value"], unit)
    verb = "corrected" if out["corrected"] else "answered"
    feed.emit(db, "answer_recorded", f"{party['name']} {verb} {question['question_id']}: {said}", level="good",
              recall_id=party["recall_id"], party_id=party["party_id"], attempt_id=attempt["_id"], system="phone",
              sim=sim, detail={"question_id": question["question_id"], "value": out["value"],
                               "questions_left": out["questions_left"]})


# ---- request_callback -----------------------------------------------------------

def _hours(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a number")
    return number


def callback_hour(body: dict, sim_now: float) -> float:
    """The simulated hour a callback was asked for: `callback_at` as given, or
    `hour_of_day` on the day `day_offset` days from today (the next time that
    hour comes round when no day is given)."""
    if body.get("callback_at") not in (None, ""):
        at = _hours(body["callback_at"], "callback_at")
    elif body.get("hour_of_day") not in (None, ""):
        hour = _hours(body["hour_of_day"], "hour_of_day")
        if not 0 <= hour < 24:
            raise ValueError("hour_of_day must be from 0 to 23.99")
        if body.get("day_offset") in (None, ""):
            at = clock.next_at_hour(sim_now, hour)
        else:
            offset = _hours(body["day_offset"], "day_offset")
            if offset < 0 or not offset.is_integer():
                raise ValueError("day_offset must be a whole number of days, 0 or more")
            at = 24.0 * (clock.day(sim_now) + int(offset)) + hour
    else:
        raise ValueError("give callback_at, or hour_of_day with an optional day_offset")
    if at < sim_now:
        raise ValueError(f"{clock.fmt(at)} has already passed (it is {clock.fmt(sim_now)})")
    if at > sim_now + MAX_CALLBACK_AHEAD_H:
        raise ValueError("a callback must be within 14 days")
    return round(at, 4)


def spoken_time(at: float, sim_now: float) -> str:
    """'tomorrow at 09:00' - for the agent to confirm with the party."""
    days = clock.day(at) - clock.day(sim_now)
    when = {0: "today", 1: "tomorrow"}.get(days, f"in {days} days")
    return f"{when} at {clock.fmt(at).split(' ')[1]}"


def handle_callback(state, body: dict, *, now: datetime | None = None) -> dict:
    db, now = state.db, now or utcnow()
    attempt, refusal = _matched_call(db, body, "callback", now)
    if refusal:
        return refusal
    rid, sid = attempt["recall_id"], attempt["party_id"]
    sim = sim_hour(state, rid)
    try:
        at = callback_hour(body, sim)
    except ValueError as exc:
        return {"accepted": False, "stop": False, "message": f"Not booked: {exc}. Ask them for another time."}
    _mark_reached(db, rid, sid)
    db.attempts.update_one({"_id": attempt["_id"]}, {"$set": {"callback_at": at, "callback_requested_at": now}})
    name = (db.parties.find_one({"recall_id": rid, "party_id": sid}, {"name": 1}) or {}).get("name") or sid
    feed.emit(db, "callback_requested", f"{name} asked to be called back {spoken_time(at, sim)} ({clock.fmt(at)})",
              recall_id=rid, party_id=sid, attempt_id=attempt["_id"], system="phone", sim=sim,
              detail={"callback_at": at})
    return {"accepted": True, "callback_at": at, "callback_label": clock.fmt(at),
            "message": f"Callback booked for {spoken_time(at, sim)}. Confirm the time, thank them and end the call."}


# ---- report ---------------------------------------------------------------------

class Report(BaseModel):
    """Counts that arrive without a call. `evidence_id` names what carried them
    (the escalate_rep or send_form_link operation, the order system read): the claim
    is booked with attempt_id = evidence_id, so the evidence is the claim's
    provenance and its tool event."""
    recall_id: str
    party_id: str
    source: Literal["rep", "form", "order_system"]
    evidence_id: str = Field(min_length=1)
    sold: Any = None
    on_shelf: Any = None
    held: Any = None
    returned: Any = None
    forwarded_qty: Any = None
    forwarded_to_name: str | None = None
    received_qty: Any = None
    received_from: str | None = None
    batch_seen: str | None = None
    sold_source: str | None = None
    block_test: Any = None
    delivery_note: str | None = None
    forwards: list[dict] | None = None


@router.post("/tools/report", dependencies=[Depends(require_tool_secret)],
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/json": {"schema": Report.model_json_schema()}}}})
def report(request: Request, raw: bytes = Depends(tool_body)) -> dict:
    """The body is validated here rather than declared as a parameter:
    FastAPI reads and parses a declared body before any dependency runs, so
    an unauthenticated caller could make the API buffer any size of body
    before the tool secret was checked."""
    try:
        body = Report.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None
    st = request.app.state
    party = st.db.parties.find_one({"recall_id": body.recall_id, "party_id": body.party_id})
    if party is None:
        raise HTTPException(status_code=404, detail=f"unknown party {body.party_id} in {body.recall_id}")
    return jsonable(handle_report(st, body, party))


def handle_report(state, body: Report, party: dict, *, now: datetime | None = None) -> dict:
    db, now = state.db, now or utcnow()
    raw = {k: v for k, v in body.model_dump(include=set(CLAIM_KEYS)).items() if v is not None}
    # The ledger logs a rep/form/order system report as its claim's evidence (keyed by
    # the dedupe key, so a redelivery logs once) before booking it.
    _announce_report(db, party, body, raw)
    carrier = {"_id": body.evidence_id, "recall_id": body.recall_id, "party_id": body.party_id}
    result, _ = book_claim(state.client, db, carrier, raw, source=body.source)
    verified = []
    if result["accepted"]:
        sim = sim_hour(state, body.recall_id)
        if evidence.verify_operation(db, body.evidence_id, result["claim_id"], sim):
            verified.append(body.evidence_id)
        if body.source == "form":
            evidence.verify_form_links(db, party, result["claim_id"], sim)
    keys = ("accepted", "message", "claim_id", "duplicate", "code", "reason", "reported_total", "shipped_in", "values")
    return {**{k: result[k] for k in keys if result.get(k) is not None}, "verified": verified}


def _announce_report(db, party: dict, body: Report, raw: dict) -> None:
    order_system = party.get("order_system")
    system = REPORT_SYSTEMS.get(body.source) or (feed.orders_label(order_system) if order_system else "orders")
    line = {"rep": f"{party['name']}: the distributor's rep reported the counts",
            "form": f"{party['name']} submitted counts through the web form",
            "order_system": f"{party['name']}: counts read from the order system"}[body.source]
    feed.emit(db, "form_claim" if body.source == "form" else "report_received", line, recall_id=body.recall_id,
              party_id=body.party_id, attempt_id=body.evidence_id, system=system,
              detail={"source": body.source, "evidence_id": body.evidence_id, "raw": raw})


# ---- feed lines -------------------------------------------------------------

def describe_counts(raw: dict, forwarded_to: str | None = None) -> str:
    """'30 held, 10 returned': the non-zero buckets of a booked claim,
    plus a receipt, the transfers and the check answers when the claim has them."""
    c = counts(raw)
    sender = text(raw.get("received_from"))
    parts = ([f"received {c['received_qty']}" + (f" from {sender}" if sender else "")]
             if c["received_qty"] is not None else [])
    parts += [f"{c[key]} {label}" for key, label in BUCKET_LABELS if c[key]]
    forwards = [f for f in raw.get("forwards") or [] if isinstance(f, dict)]
    if forwards:
        parts += [f"{f.get('qty')} forwarded to {f.get('to_name') or f.get('to_party_id')}" for f in forwards]
    elif c["forwarded_qty"]:
        parts.append(f"{c['forwarded_qty']} forwarded to {forwarded_to or raw.get('forwarded_to_name')}")
    ck = checks(raw)
    if "batch_seen" in ck:
        parts.append(f"batch {ck['batch_seen']}")
    if "sold_source" in ck:
        parts.append("sold read off the till" if ck["sold_source"] == "till_report" else "sold estimated")
    if "block_test" in ck:
        parts.append(f"till refused the sale: {'yes' if ck['block_test'] else 'no'}")
    return ", ".join(parts) or "no tins sold, on the shelf, held or returned"


def announce(db, result: dict, *, attempt: dict, party: dict | None, raw: dict,
             now: datetime | None = None) -> None:
    """Write the claim's feed lines. They are spaced a millisecond apart (BSON
    dates are millisecond precision) so the wall shows them in order."""
    name = (party or {}).get("name") or attempt["party_id"]
    base = now or utcnow()
    for i, (kind, line, level, detail, sid) in enumerate(_claim_lines(db, result, attempt, name, raw)):
        feed.emit(db, kind, line, level=level, detail=detail, recall_id=attempt["recall_id"],
                  party_id=sid or attempt["party_id"], attempt_id=attempt["_id"],
                  now=base + timedelta(milliseconds=i))


def _claim_lines(db, result: dict, attempt: dict, name: str, raw: dict):
    """Yields (kind, text, level, detail, party_id or None) per line."""
    if result.get("duplicate"):
        yield ("claim_duplicate", f"Duplicate delivery ignored for {name} (same dedupe key)", "info",
               {"claim_id": result.get("claim_id"), "accepted": result["accepted"]}, None)
        return
    if not result["accepted"]:
        yield _rejection_line(result, name)
    else:
        yield from _acceptance_lines(db, result, attempt, name, raw)
    if result.get("txn_runs", 1) > 1:
        yield ("info", f"Claim transaction retried after a write conflict ({result['txn_runs']} runs)", "info",
               {"claim_id": result.get("claim_id"), "txn_runs": result["txn_runs"]}, None)


def _rejection_line(result: dict, name: str):
    err = result.get("atlas_error")
    if err is not None and result.get("values"):
        return ("claim_rejected", f"{name} - Rejected by Atlas: {result.get('reason')}", "bad", err, None)
    detail = err if err is not None else {"code": result.get("code"), "reason": result.get("reason")}
    return ("claim_rejected", f"{name} - rejected: {result.get('reason')}", "bad" if err else "warn", detail, None)


def _as_list(value) -> list:
    """The ledger reports one discovered party / edge, or a list of them for a
    claim with several destinations (V2 §9)."""
    if not value:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _acceptance_lines(db, result: dict, attempt: dict, name: str, raw: dict):
    edges = _as_list(result.get("edges")) or _as_list(result.get("edge"))
    found = _as_list(result.get("discovered"))
    names = _party_names(db, attempt["recall_id"]) if (edges or found) else {}
    outbound = [e for e in edges if e["from_party"] == attempt["party_id"] and e["state"] != "withdrawn"]
    forwarded_to = names.get(outbound[0]["to_party"]) if len(outbound) == 1 else None
    yield ("claim_accepted", f"{name} - accepted: {describe_counts(raw, forwarded_to)}", "good",
           {"claim_id": result.get("claim_id"), "open_units": result.get("open_units")}, None)
    for sid in found:
        yield ("party_discovered", f"New party discovered: {names.get(sid, sid)} (named by {name})", "warn",
               {"party_id": sid, "named_by": attempt["party_id"]}, sid)
    for edge in edges:
        line = _edge_line(edge, names)
        if line:
            yield line


def _party_names(db, recall_id: str) -> dict:
    return {s["party_id"]: s["name"] for s in db.parties.find({"recall_id": recall_id}, {"party_id": 1, "name": 1})}


def _edge_line(edge: dict, names: dict):
    """A line when the transfer opened or changed state; None when it did not."""
    before, state = edge.get("before"), edge["state"]
    sender = names.get(edge["from_party"], edge["from_party"])
    recipient = names.get(edge["to_party"]) or edge.get("to_name") or edge["to_party"]
    detail = {k: v for k, v in edge.items() if k != "before"}
    if state == "withdrawn":
        # A transfer the sender took back before the recipient counted it.
        gone = f"; {recipient} is no longer in the run (nothing else named it)" if edge.get("removed") else ""
        return ("info", f"Transfer withdrawn: {sender} no longer reports sending {edge['sender_qty']} tins to "
                f"{recipient}{gone}", "warn", detail, None)
    if before is None:
        return ("edge_opened", f"Transfer opened: {sender} -> {recipient}, {edge['sender_qty']} tins "
                f"awaiting {recipient}'s count", "info", detail, None)
    if state == before:
        return None
    if state == "disputed":
        return ("edge_disputed", f"Transfer disputed: {sender} says {edge['sender_qty']}, {recipient} says "
                f"{edge['recipient_qty']} - {edge['disputed_qty']} tins disputed", "bad", detail, None)
    if state == "corroborated":
        return ("edge_corroborated", f"Transfer corroborated: {sender} and {recipient} agree on "
                f"{edge['corroborated_qty']} tins", "good", detail, None)
    return None
