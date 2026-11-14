"""The tool registry (V2 §6). Every tool any decider may call is declared
here, with:

    kind     effect  changes the outside world (a call, a form link, a hold)
             record  writes only Atlas (a callback, a priority, a question)
             read    reads the outside world (an order system's accounts)
    intent   runs inside the gate's transaction: finds the intent by its
             idempotency key (a duplicate returns the stored document) or
             inserts it; a record tool makes its write here, atomically with
             the lease and budget checks
    act      after the commit: call the provider (never inside a transaction)
    record   store the outcome and the provider's ids (never `verified`)

Idempotency keys (V2 §6): place_call "<run_id>:place_call"; order system operations
"<recall>:<party>:<kind>" (read_inventory adds ":d<day>"); form link
"<recall>:<party>:form:<ladder_step>"; escalation to a rep
"<recall>:<party>:escalate_rep".

Placing a call is the spec's sequence with the wave-1 failpoints:
intent (the attempt, carrying the run id and lease epoch) -> after_intent ->
provider.place_call -> after_dial_request -> mark_dialed -> after_mark_dialed.
ProviderError: no call exists, the attempt is failed. ProviderTimeout (or
anything unexpected): the call may exist, the attempt is flagged in doubt and
only recovery, from evidence, may dial again.

The order system tools hand the operation to `levela.systems.operations.perform`
(imported when first used), which sends it, reads it back and records the
result; only a read-back that shows the effect makes it `verified`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

from .. import feed
from ..api import questions as questions_
from ..clock import day, fmt
from ..orchestrator import attempts as attempts_
from ..orchestrator import ladder
from .variables import dynamic_variables
from ..providers.base import ProviderError, ProviderTimeout
from .brief import open_dispute

log = logging.getLogger("levela.harness.tools")

LIVE = ("intent", "dialed", "connected")
FORM_LINK_PATH = "/sim/form_link"
TOOL_SECRET_HEADER = "X-Tool-Secret"
HTTP_TIMEOUT_S = 10.0
ANSWER_TYPES = questions_.ANSWER_TYPES


class Refused(Exception):
    """A gate check (or an intent builder) said no. `rule` is what the wall shows."""

    def __init__(self, rule: str, reason: str, detail: dict | None = None):
        super().__init__(f"{rule}: {reason}")
        self.rule, self.reason, self.detail = rule, reason, detail or {}


@dataclass
class Call:
    """One tool call as the gate sees it. `party` is the leased party read
    inside the gate's transaction (None on planner runs); `session` is that
    transaction while the intent is built."""
    ctx: Any
    run: Any
    tool: str
    args: dict
    sim_now: float
    now: datetime
    party: dict | None = None
    session: Any = None

    @property
    def db(self):
        return self.ctx.db


@dataclass
class Intent:
    """What the intent step produced. `collection` and `doc` name the attempt
    or operation (None for record tools, whose write is already made);
    `duplicate` means the key existed and nothing new was written."""
    collection: str | None
    key: str | None
    doc: dict
    duplicate: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str
    description: str
    input_schema: dict
    intent: Callable[[Call], Intent]
    act: Callable[[Call, Intent], dict] | None = None
    record: Callable[[Call, Intent, dict | None], dict] | None = None

    @property
    def counts_as_step(self) -> bool:
        """runs.steps counts effect and record calls (V2 §6), not reads."""
        return self.kind in ("effect", "record")


def _schema(properties: dict | None = None, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


NOTE = {"note": {"type": "string", "description": "One sentence: why this action now."}}


# ------------------------------------------------------------------ shared

def _party_key(call: Call) -> tuple[str, str]:
    if call.party is None:
        raise Refused("intent", f"{call.tool} needs a party run")
    return call.party["recall_id"], call.party["party_id"]


def _find_key(call: Call, collection: str, key: str) -> dict | None:
    return call.db[collection].find_one({"idempotency_key": key}, session=call.session)


def _simulated(ctx, party: dict) -> bool:
    """Played by the simulator rather than a real phone."""
    return party.get("channel") == "sim" or (party.get("channel") == "voice" and ctx.settings.voice_provider == "sim")


def _real_voice(ctx, party: dict) -> bool:
    return party.get("channel") == "voice" and ctx.settings.voice_provider != "sim"


def new_op_id() -> str:
    return f"op-{secrets.token_hex(6)}"


def operation_doc(call: Call, *, kind: str, key: str, party: str, order_system=None, state="intent",
                  adapter_version=None, request=None, extra=None) -> dict:
    """An `operations` document (V2 §7) for this run."""
    return {"_id": new_op_id(), "recall_id": call.run.recall_id, "run_id": call.run.id, "kind": kind,
            "order_system": order_system, "party_id": party, "idempotency_key": key, "adapter_version": adapter_version,
            "state": state, "lease_epoch": call.run.lease_epoch, "sent_at": None, "readback_at": None,
            "readback_value": None, "request": request or {}, "response": None, "error": None,
            "created_at": call.now, **(extra or {})}


def stored_outcome(intent: Intent) -> dict:
    """What a duplicate call returns: the stored document's state and ids."""
    doc = intent.doc or {}
    if intent.collection == "attempts":
        return {"attempt_id": doc.get("_id"), "state": doc.get("state"), "call_sid": doc.get("call_sid"),
                "conversation_id": doc.get("conversation_id")}
    if intent.collection == "operations":
        return {"op_id": doc.get("_id"), "kind": doc.get("kind"), "state": doc.get("state"),
                "readback_value": doc.get("readback_value")}
    return dict(doc)


# -------------------------------------------------------------- place_call

def _place_call_intent(call: Call) -> Intent:
    rid, sid = _party_key(call)
    key = f"{call.run.id}:place_call"
    db, s, party = call.db, call.session, call.party
    history = list(db.attempts.find({"recall_id": rid, "party_id": sid}, session=s).sort("seq", -1))
    mine = next((a for a in history if a.get("idempotency_key") == key), None)
    if mine is not None:
        return Intent("attempts", key, mine, duplicate=True)
    live = next((a for a in history if a["state"] in LIVE), None)
    if live is not None:
        raise Refused("intent", f"attempt {live['_id']} to {sid} is still {live['state']}",
                      {"existing_attempt": live["_id"]})
    if call.ctx.providers.get(party.get("channel")) is None or not party.get("phone"):
        raise Refused("intent", f"{sid} cannot be dialled (channel {party.get('channel')}, "
                                f"{'no provider' if party.get('phone') else 'no phone'})")
    doc = _attempt_doc(call, history, key)
    if _real_voice(call.ctx, party):
        from ..orchestrator import claim

        if not claim.take_slot_and_intent(db, doc, session=s):
            raise Refused("slot", "no free call slot: every real phone line is busy")
    else:
        db.attempts.insert_one(doc, session=s)
    db.parties.update_one({"_id": party["_id"], "state": "pending"}, {"$set": {"state": "contacting"}}, session=s)
    return Intent("attempts", key, doc)


def _attempt_doc(call: Call, history: list, key: str) -> dict:
    party, run, s = call.party, call.run, call.session
    edges = list(call.db.edges.find({"recall_id": party["recall_id"],
                                     "$or": [{"from_party": party["party_id"]}, {"to_party": party["party_id"]}]},
                                    session=s))
    ids = {e["from_party"] for e in edges} | {e["to_party"] for e in edges}
    names = {d["party_id"]: d["name"] for d in call.db.parties.find(
        {"recall_id": party["recall_id"], "party_id": {"$in": sorted(ids)}}, {"party_id": 1, "name": 1},
        session=s)} if ids else {}
    purpose, extra, told, note = _purpose(run.kind, party, history, edges, names)
    inbound = [e for e in edges if e["to_party"] == party["party_id"]]
    # A party asked about panels it received - any party below tier 1, or a
    # tier 1 party a forwarder shipped to - is told who sent them and asked
    # for the receipt (the prompt asks for received_qty when receipt is yes).
    owed = [e for e in inbound if e.get("state") == "awaiting_recipient"]
    asks_receipt = party.get("tier") != 1 or bool(owed)
    sender_edge = (owed or inbound)[0] if inbound and asks_receipt else None
    sender = names.get(sender_edge["from_party"], "") if sender_edge else ""
    asks_note = any(a.get("action") == "ask_delivery_note" for a in ((party.get("memory") or {}).get("actions") or []))
    variables = dynamic_variables(call.ctx.recall, party, purpose, sender_name=sender, told_qty=told,
                                  followup_note=note, ask_delivery_note=asks_note, receipt=asks_receipt)
    aid = attempts_.new_attempt_id()
    variables.update({
        "attempt_id": aid, "ladder_step": str(ladder.clamp(party.get("ladder_step"))),
        "attempt_number": str(len(history) + 1), "sim_time": fmt(call.sim_now),
    })
    # questions, question_ids, ask_counts: the form agent/prompt.md reads
    # ("q-02: text (answer: yes or no, and how many panels)."), built by the
    # module that owns the question schema. A question-only run takes no counts.
    variables.update(questions_.call_variables(call.db, party, take_counts=run.kind != "ask_question", session=s))
    return {"_id": aid, "recall_id": party["recall_id"], "party_id": party["party_id"],
            "seq": (history[0]["seq"] + 1) if history else 1, "channel": party["channel"], "state": "intent",
            "purpose": purpose, "intent_at": call.now, "conversation_id": None, "call_sid": None,
            "to_number": party.get("phone"), "owner": {"worker": run.worker, "epoch": run.lease_epoch},
            "dial_confirmed": False, "context": variables, "idempotency_key": key, "run_id": run.id,
            "lease_epoch": run.lease_epoch, "ladder_step": ladder.clamp(party.get("ladder_step")),
            "intent_sim": round(call.sim_now, 4), **extra}


def _purpose(kind: str, party: dict, history: list, edges: list, names: dict) -> tuple[str, dict, str, str]:
    """(purpose, extra attempt fields, told_qty, followup_note) for the call."""
    if kind == "followup":
        dispute = open_dispute(party, history, edges, names)
        if dispute is None:
            raise Refused("intent", f"{party['party_id']} has no open dispute to follow up")
        return "dispute_followup", {"dispute_key": dispute["key"], "told": dispute["told"]}, str(dispute["told"]), \
            dispute["note"]
    if kind == "ask_question":
        # The run kind is the purpose: the simulator plays a question-only call
        # (no script, no counts) and the profiles answer it by this name.
        return "ask_question", {}, "", ""
    if party.get("state") in ("claimed", "reconciled") and party.get("installed"):
        # Counts are on the books with panels installed and not fixed: the
        # fix check asks the counts and the three checks again.
        return "fix_check", {}, "", ""
    return ("retry" if history else "initial"), {}, "", ""


def _place_call_act(call: Call, intent: Intent) -> dict:
    attempt, ctx = intent.doc, call.ctx
    provider = ctx.providers[attempt["channel"]]
    ctx.failpoints.hit("after_intent", attempt)
    try:
        placed = provider.place_call(to_number=attempt["to_number"], dynamic_variables=attempt["context"],
                                     attempt_id=attempt["_id"])
    except ProviderTimeout as exc:
        return _in_doubt(call, attempt, exc)
    except ProviderError as exc:
        attempts_.complete(call.db, attempt["_id"], "failed", reason=f"provider error: {exc}", dial_confirmed=True)
        _emit(call, "call_failed", f"Call to {call.party['name']} failed: {exc}. Attempt {attempt['_id']} closed as "
                                   "failed; no call was placed", level="warn", system="phone",
              attempt_id=attempt["_id"])
        return {"state": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - an unknown failure may still have placed the call
        log.exception("place_call raised for %s", attempt["_id"])
        return _in_doubt(call, attempt, exc)
    return {"state": "placed", "conversation_id": placed.conversation_id, "call_sid": placed.call_sid,
            "provider": getattr(provider, "name", None)}


def _in_doubt(call: Call, attempt: dict, exc: Exception) -> dict:
    why = str(exc) or type(exc).__name__
    call.db.attempts.update_one({"_id": attempt["_id"], "dial_confirmed": {"$ne": True}},
                                {"$set": {"in_doubt": True, "in_doubt_at": feed.utcnow(), "in_doubt_reason": why}})
    _emit(call, "call_in_doubt", f"No answer from the provider for attempt {attempt['_id']} to {call.party['name']} "
                                 f"({why}): the call may exist, so recovery decides from evidence before any redial",
          level="warn", system="phone", attempt_id=attempt["_id"])
    return {"state": "in_doubt", "error": why}


def _place_call_record(call: Call, intent: Intent, acted: dict | None) -> dict:
    attempt = intent.doc
    out = {"attempt_id": attempt["_id"], **(acted or {})}
    if (acted or {}).get("state") != "placed":
        return out
    call.ctx.failpoints.hit("after_dial_request", attempt)
    attempts_.mark_dialed(call.db, attempt["_id"], conversation_id=acted["conversation_id"],
                          call_sid=acted["call_sid"])
    sid = f": call {acted['call_sid']}" if acted.get("call_sid") else ""
    _emit(call, "call_placed", f"Calling {call.party['name']} (attempt {attempt['_id']}, {attempt['purpose']}, "
                               f"ladder step {attempt['ladder_step']}){sid}", system="phone", attempt_id=attempt["_id"],
          detail={"call_sid": acted.get("call_sid"), "conversation_id": acted.get("conversation_id"),
                  "run_id": call.run.id, "seq": attempt["seq"]})
    call.ctx.failpoints.hit("after_mark_dialed", attempt)
    return {**out, "state": "dialed"}


# ----------------------------------------------------------- send_form_link

def _form_link_intent(call: Call) -> Intent:
    rid, sid = _party_key(call)
    step = ladder.clamp(call.party.get("ladder_step"))
    key = f"{rid}:{sid}:form:{step}"
    existing = _find_key(call, "operations", key)
    if existing is not None:
        return Intent("operations", key, existing, duplicate=True)
    token = call.party.get("form_token")
    request = {"form_token": token, "form_url": f"{call.ctx.settings.webhook_base}/form/{token}",
               "ladder_step": step}
    doc = operation_doc(call, kind="send_form_link", key=key, party=sid, request=request, extra={"system": "form"})
    call.db.operations.insert_one(doc, session=call.session)
    return Intent("operations", key, doc)


def _form_link_act(call: Call, intent: Intent) -> dict:
    """The simulator delivers the link to a simulated party (POST /sim/form_link);
    a real party gets it from the operator, who sees it on the wall."""
    op, party, settings = intent.doc, call.party, call.ctx.settings
    if not _simulated(call.ctx, party):
        return {"delivered_via": "operator"}
    # POST /sim/form_link takes the party's form token as `token`.
    body = {"recall_id": op["recall_id"], "party_id": party["party_id"], "token": op["request"]["form_token"],
            "op_id": op["_id"], "form_url": op["request"]["form_url"], "ladder_step": op["request"]["ladder_step"]}
    http = call.ctx.http or httpx.Client(timeout=HTTP_TIMEOUT_S)
    try:
        resp = http.post(settings.api_base_url.rstrip("/") + FORM_LINK_PATH, json=body,
                         headers={TOOL_SECRET_HEADER: settings.tool_secret})
    except httpx.HTTPError as exc:
        return {"error": f"simulator unreachable: {type(exc).__name__}: {exc}"}
    finally:
        if call.ctx.http is None:
            http.close()
    if resp.status_code >= 300:
        return {"error": f"simulator answered {resp.status_code}: {resp.text[:200]}"}
    return {"delivered_via": "simulator", "response": _json_or_text(resp)}


def _json_or_text(resp) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _form_link_record(call: Call, intent: Intent, acted: dict | None) -> dict:
    op, acted = intent.doc, acted or {}
    if acted.get("error"):
        call.db.operations.update_one({"_id": op["_id"], "state": "intent"},
                                      {"$set": {"state": "failed", "error": acted["error"]}})
        _emit(call, "form_link_failed", f"Form link for {call.party['name']} not delivered: {acted['error']}",
              level="warn", system="form")
        return {"op_id": op["_id"], "state": "failed", "error": acted["error"]}
    call.db.operations.update_one({"_id": op["_id"], "state": "intent"}, {"$set": {
        "state": "sent", "sent_at": round(call.sim_now, 4), "sent_wall_at": feed.utcnow(),
        "delivered_via": acted.get("delivered_via"), "response": acted.get("response")}})
    via = "the simulator" if acted.get("delivered_via") == "simulator" else "the operator (link on the wall)"
    _emit(call, "form_link_sent", f"Form link sent to {call.party['name']} through {via}: "
                                  f"{op['request']['form_url']}. Waiting {ladder.FORM_WAIT_H:g} simulated hours "
                                  "for a submission", system="form", detail={"op_id": op["_id"]})
    return {"op_id": op["_id"], "state": "sent", "delivered_via": acted.get("delivered_via")}


# -------------------------------------------------------- schedule_callback

def _callback_intent(call: Call) -> Intent:
    try:
        at = float(call.args.get("at"))
    except (TypeError, ValueError):
        raise Refused("intent", "schedule_callback needs `at`, a simulated hour") from None
    at = max(at, call.sim_now)
    call.db.task_runs.update_one({"_id": call.run.id}, {"$set": {"callback_at": round(at, 4)}}, session=call.session)
    return Intent(None, None, {"callback_at": round(at, 4), "label": fmt(at)})


# ----------------------------------------------------------------- escalate

def _escalate_target(call: Call) -> dict:
    if call.party is not None:
        return call.party
    sid = call.args.get("party_id")
    target = call.db.parties.find_one({"recall_id": call.run.recall_id, "party_id": sid}, session=call.session)
    if target is None:
        raise Refused("intent", f"escalate needs a party of {call.run.recall_id}; {sid!r} is not one")
    return target


def _escalate_intent(call: Call) -> Intent:
    """Ladder step 5: a distributor's account goes to that distributor's rep
    (an escalate_rep operation the simulator answers); any other party goes to
    a person (parties.escalation = human)."""
    target, s = _escalate_target(call), call.session
    rid, sid = target["recall_id"], target["party_id"]
    reason = call.args.get("reason") or call.args.get("note") or "the contact ladder ran out"
    if target.get("distributor"):
        key = f"{rid}:{sid}:escalate_rep"
        existing = _find_key(call, "operations", key)
        if existing is not None:
            return Intent("operations", key, existing, duplicate=True)
        dist = call.db.parties.find_one({"recall_id": rid, "party_id": target["distributor"]}, {"order_system": 1, "name": 1},
                                       session=s) or {}
        doc = operation_doc(call, kind="escalate_rep", key=key, party=sid, order_system=dist.get("order_system"), state="sent",
                            request={"reason": reason, "distributor": target["distributor"],
                                     "account": target.get("account")},
                            extra={"sent_at": round(call.sim_now, 4), "sent_wall_at": call.now})
        call.db.operations.insert_one(doc, session=s)
        call.db.parties.update_one({"_id": target["_id"]}, {"$set": {"escalation": "rep", "ladder_step": ladder.LAST_STEP,
                                                                   "escalated_run": call.run.id}}, session=s)
        return Intent("operations", key, {**doc, "_target": target["name"], "_to": dist.get("name")})
    res = call.db.parties.update_one(
        {"_id": target["_id"], "escalation": {"$ne": "human"}},
        {"$set": {"escalation": "human", "escalation_reason": reason, "escalated_at": call.now,
                  "escalated_run": call.run.id, "ladder_step": ladder.LAST_STEP}}, session=s)
    return Intent(None, None, {"target": sid, "_target": target["name"], "escalation": "human",
                               "changed": res.modified_count == 1, "reason": reason})


def _escalate_record(call: Call, intent: Intent, acted: dict | None) -> dict:
    doc = intent.doc
    sid = doc.get("party_id") or doc.get("target")
    if intent.collection == "operations":
        _emit(call, "escalated_rep", f"{doc['_target']}: escalated to {doc.get('_to') or 'the distributor'}'s rep "
                                     "(ladder step 5). Its units stay unresolved until the rep reports",
              level="warn", system="worker", party_id=sid, detail={"op_id": doc["_id"]})
        return {"op_id": doc["_id"], "state": doc["state"], "target": sid}
    if doc.get("changed"):
        _emit(call, "needs_human", f"{doc['_target']} needs a person: {doc['reason']}. Its units stay unresolved",
              level="warn", system="worker", party_id=sid, detail={"reason": doc["reason"]})
    return {"target": sid, "escalation": "human", "changed": doc.get("changed")}


# ------------------------------------------------------------ order system tools

def _order_system_intent(kind: str) -> Callable[[Call], Intent]:
    """The intent comes from levela.systems.operations.intent_for (the one
    place that knows each order system operation's request and key) and is written
    inside the gate's transaction (check 5)."""
    def build(call: Call) -> Intent:
        rid, sid = _party_key(call)
        if not call.party.get("order_system"):
            raise Refused("intent", f"{sid} has no order system")
        from ..systems import operations
        try:
            doc = operations.intent_for(call.ctx, kind, call.party, run_id=call.run.id,
                                        lease_epoch=call.run.lease_epoch)
        except ValueError as exc:
            raise Refused("intent", str(exc)) from None
        existing = _find_key(call, "operations", doc["idempotency_key"])
        if existing is not None:
            return Intent("operations", doc["idempotency_key"], existing, duplicate=True)
        call.db.operations.insert_one(doc, session=call.session)
        return Intent("operations", doc["idempotency_key"], doc)
    return build


def _order_system_act(call: Call, intent: Intent) -> dict:
    """Carry the operation out (after_op_intent / after_op_sent failpoints fire
    inside operations.execute). Order system trouble never raises from execute; the
    operation's state is the record."""
    from ..systems import operations
    try:
        return {"outcome": operations.execute(call.ctx, intent.doc, fresh=True)}
    except Exception as exc:  # noqa: BLE001 - recorded on the operation; the facts decide the outcome
        log.exception("order system operation %s failed", intent.doc["_id"])
        return {"error": f"{type(exc).__name__}: {exc}"}


def _order_system_record(call: Call, intent: Intent, acted: dict | None) -> dict:
    op_id = intent.doc["_id"]
    if (acted or {}).get("error"):
        call.db.operations.update_one({"_id": op_id, "state": "intent"},
                                      {"$set": {"state": "failed", "error": acted["error"]}})
    op = call.db.operations.find_one({"_id": op_id}) or intent.doc
    return {"op_id": op_id, "kind": op["kind"], "state": op["state"], "readback_value": op.get("readback_value"),
            "error": op.get("error")}


# --------------------------------------------------------------- planner tools

def _set_priority_intent(call: Call) -> Intent:
    sid = call.args.get("party_id")
    try:
        priority = int(call.args.get("priority"))
    except (TypeError, ValueError):
        raise Refused("intent", "set_priority needs an integer priority") from None
    res = call.db.parties.update_one({"recall_id": call.run.recall_id, "party_id": sid}, {"$set": {"priority": priority}},
                                    session=call.session)
    if res.matched_count == 0:
        raise Refused("intent", f"no party {sid!r} in {call.run.recall_id}")
    return Intent(None, None, {"target": sid, "priority": priority})


def _add_question_intent(call: Call) -> Intent:
    """One document and one update (spec "Questions as data"), written by the
    same code as POST /admin/questions (levela.api.questions), inside the
    gate's transaction: the recall's next question "q-NN", then
    `open_questions += "q-NN"` and due now on every party it applies to that
    has not answered it. The same text asked again is the same question."""
    rid, s = call.run.recall_id, call.session
    text = questions_.normalize_text(call.args.get("text"))
    existing = questions_.find_same(call.db, rid, text, session=s) if text else None
    if existing is not None:
        return Intent("questions", existing["_id"], existing, duplicate=True)
    try:
        doc = questions_.insert_question(call.db, recall_id=rid, text=text,
                                         answer_type=call.args.get("answer_type") or "text",
                                         applies_to=call.args.get("applies_to"), sim_now=call.sim_now,
                                         by=call.run.worker, now=call.now, session=s)
    except questions_.QuestionError as exc:
        raise Refused("intent", f"add_question: {exc.message}") from None
    due = questions_.fan_out(call.db, doc, call.sim_now, session=s)
    return Intent("questions", doc["_id"], {**doc, "parties": due})


def _propose_adapter_intent(call: Call) -> Intent:
    """A candidate adapter only: the canary in levela.systems decides whether
    it ever becomes active."""
    order_system, mapping = call.args.get("order_system"), call.args.get("mapping")
    if not order_system or not isinstance(mapping, dict) or not mapping:
        raise Refused("intent", "propose_adapter needs an order system and a mapping")
    s = call.session
    key = f"{order_system}:candidate:" + hashlib.sha1(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:8]
    existing = call.db.adapters.find_one({"order_system": order_system, "idempotency_key": key}, session=s)
    if existing is not None:
        return Intent("adapters", key, existing, duplicate=True)
    versions = [a.get("version") or 0 for a in call.db.adapters.find({"order_system": order_system}, {"version": 1, "status": 1},
                                                                    session=s)]
    active = call.db.adapters.find_one({"order_system": order_system, "status": "active"}, {"_id": 1}, session=s)
    version = max(versions, default=0) + 1
    doc = {"_id": f"{order_system}:v{version}", "order_system": order_system, "version": version, "status": "candidate",
           "replaces": (active or {}).get("_id"), "source": "planner", "idempotency_key": key,
           "expected_fields": call.args.get("expected_fields") or {}, "mapping": mapping, "paths": {},
           "steps": call.args.get("steps") or {}, "canary": None, "note": call.args.get("note") or "",
           "created_at": call.now}
    call.db.adapters.insert_one(doc, session=s)
    return Intent("adapters", key, doc)


def _draft_report_intent(call: Call) -> Intent:
    text = str(call.args.get("text") or "").strip()
    if not text:
        raise Refused("intent", "draft_report needs text")
    report = {"text": text[:2000], "at": call.now, "sim": round(call.sim_now, 4), "label": fmt(call.sim_now),
              "by": call.run.worker, "run_id": call.run.id}
    call.db.runs.update_one({"_id": f"run:{call.run.recall_id}"}, {"$set": {"report": report}}, session=call.session)
    return Intent(None, None, {"report": fmt(call.sim_now)})


def _planner_record(call: Call, intent: Intent, acted: dict | None) -> dict:
    """Planner writes are quiet unless the room should notice them."""
    doc = intent.doc
    if call.tool == "add_question" and not intent.duplicate:
        _emit(call, "question_added", f"Planner added question {doc['question_id']}: {doc['text']} "
                                      f"({doc.get('parties', 0)} parties now due)", system="planner", party_id=None)
    if call.tool == "propose_adapter" and not intent.duplicate:
        _emit(call, "planner", f"Planner proposed adapter {doc['_id']} (candidate; the canary decides)", system="planner",
              party_id=None)
    return {k: v for k, v in doc.items() if not k.startswith("_") or k == "_id"}


# ----------------------------------------------------------------- the feed

def _emit(call: Call, kind: str, text: str, *, level: str = "info", system: str, party_id: Any = "",
          attempt_id: str | None = None, detail: dict | None = None) -> None:
    sid = (call.party or {}).get("party_id") if party_id == "" else party_id
    feed.emit(call.db, kind, text, recall_id=call.run.recall_id, party_id=sid, attempt_id=attempt_id, level=level,
              source="worker", system=system, sim=call.sim_now, detail={"run_id": call.run.id, **(detail or {})})


# ----------------------------------------------------------------- registry

def _specs() -> list[ToolSpec]:
    order_system = {"place_hold": ("effect", "Place the recall hold on the batch at this distributor's order system."),
              "confirm_hold": ("effect", "Confirm the hold (order system v2 needs a second call)."),
              "read_accounts": ("read", "Read the distributor's accounts for the batch; each becomes a party."),
              "read_inventory": ("read", "Read today's on-hand counts for the distributor's accounts.")}
    specs = [
        ToolSpec("place_call", "effect", "Call this party now (the agent asks for its counts, or only the open "
                 "questions when its counts are on the books).", _schema(NOTE), _place_call_intent, _place_call_act,
                 _place_call_record),
        ToolSpec("send_form_link", "effect", "Send this party its form link (ladder step 3).", _schema(NOTE),
                 _form_link_intent, _form_link_act, _form_link_record),
        ToolSpec("schedule_callback", "record", "Record when to contact this party next, as a simulated hour.",
                 _schema({"at": {"type": "number", "description": "Simulated hour (177 is day 7, 09:00)."}, **NOTE},
                         ("at",)), _callback_intent),
        ToolSpec("escalate", "effect", "Ladder step 5: hand the party to its distributor's rep (accounts) or a "
                 "person. Its units stay unresolved.",
                 _schema({"party_id": {"type": "string"}, "reason": {"type": "string"}}), _escalate_intent,
                 record=_escalate_record),
        ToolSpec("set_priority", "record", "Set a party's priority (higher is worked first).",
                 _schema({"party_id": {"type": "string"}, "priority": {"type": "integer"}}, ("party_id", "priority")),
                 _set_priority_intent, record=_planner_record),
        ToolSpec("add_question", "record", "Ask every matching party one more question.",
                 _schema({"text": {"type": "string"}, "answer_type": {"type": "string", "enum": list(ANSWER_TYPES)},
                          "applies_to": {"type": "object", "properties": {"tier": {"type": "array",
                                                                                    "items": {"type": "integer"}}},
                                         "additionalProperties": False}}, ("text", "answer_type")),
                 _add_question_intent, record=_planner_record),
        ToolSpec("propose_adapter", "record", "Propose a candidate order system adapter (the canary decides).",
                 _schema({"order_system": {"type": "string"}, "mapping": {"type": "object"},
                          "expected_fields": {"type": "object"}, "steps": {"type": "object"},
                          "note": {"type": "string"}}, ("order_system", "mapping")),
                 _propose_adapter_intent, record=_planner_record),
        ToolSpec("draft_report", "record", "Write the draft status report for the operator.",
                 _schema({"text": {"type": "string"}}, ("text",)), _draft_report_intent, record=_planner_record),
    ]
    for name, (kind, text) in order_system.items():
        specs.append(ToolSpec(name, kind, text, _schema(), _order_system_intent(name), _order_system_act, _order_system_record))
    return specs


REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _specs()}
