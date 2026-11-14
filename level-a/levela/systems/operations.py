"""Order system operations: intent -> sent -> verified, and only by read-back (V2 §7).

One `operations` document per order system call. The Atlas validator refuses
`state: verified` without `readback_at` and `readback_value`, and the unique
index on `idempotency_key` makes a second intent for the same effect return
the first one:

    {_id: "op-…", recall_id, run_id, kind, order system, party, idempotency_key,
     adapter_version, state (intent|sent|verified|needs_reverify|failed),
     lease_epoch, intent_at, sent_at, sends, readback_at, readback_value,
     request, response, hold_id, error, created_at, updated_at}

Times in intent_at / sent_at / readback_at are simulated hours.

A hold: write intent -> POST with the key -> record `sent` -> read the
order system's holds back -> only a read-back that shows the hold is `verified`.
Under an adapter with a confirm step (v2) the verified place_hold opens a
confirm_hold operation, verified when the read-back shows the hold in force.
Reads (read_accounts, read_inventory) are their own read-back.

Recovery never trusts memory of what was sent. An operation in intent or sent
(a worker died, an answer was lost, a fault dropped the request) is resolved
by reading back first; it is sent again only when the read-back shows
nothing, only after READBACK_SETTLE_H (the order system documents that reads may
lag writes by 10 minutes), and always with the same key.

When an order system breaks its contract (a 410, a missing field), contain() pauses
the order system, flags every intent/sent operation of that order system
`needs_reverify` and leaves verified ones alone; after a repaired adapter is
activated, retry_needs_reverify() runs exactly those again.

Only ORDER_SYSTEM_KINDS are order system calls. send_form_link and escalate_rep share
this collection (and an escalation carries its distributor's order system), but
they rest in `sent` until a person or the simulator answers them: they are
never in doubt, never flagged by a contract failure and never executed here.

Every function takes `ctx`: any object with settings, client, db, order systems
({name: OrderClient}) and failpoints (or None) -- the harness's
HarnessContext is one.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from .. import feed, runs
from ..clock import day as sim_day
from ..clock import get_clock, server_now
from ..ledger import product_fields, resolve_party, stub_party, submit_claim
from . import adapters
from .contract import ContractError
from .order_client import OrderSystemError, OrderSystemTimeout

STATES = ("intent", "sent", "verified", "needs_reverify", "failed")
OPEN_STATES = ("intent", "sent")
FINAL_STATES = ("verified", "failed")
KINDS = ("place_hold", "confirm_hold", "read_accounts", "read_inventory", "send_form_link", "escalate_rep")
ORDER_SYSTEM_KINDS = ("place_hold", "confirm_hold", "read_accounts", "read_inventory")
READBACK_SETTLE_H = 10 / 60      # how long the order system's reads may lag its writes
HOLD_SCOPE = "all"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def system(order_system: str) -> str:
    """The screen's system tag for an order system's feed lines."""
    return feed.orders_label(order_system)


def idempotency_key(recall_id: str, party: str, kind: str, *, day: int | None = None,
                    probe: int | None = None) -> str:
    """"<recall>:<party>:<kind>", plus ":d<day>" for once-a-day reads (V2 §6),
    plus ":probe<n>" for the contract probe owed after the n-th order system switch.

    A probe is a read of its own, so it needs a key no daily read can take;
    keeping the day in front lets the brief count it as that day's
    inventory read (its key starts with the day's key)."""
    key = f"{recall_id}:{party}:{kind}"
    if day is not None:
        key = f"{key}:d{day}"
    return key if probe is None else f"{key}:probe{probe}"


def sim_now(ctx, recall_id: str) -> float:
    return get_clock(ctx.db, recall_id).sim_now(server_now(ctx.client))


# ------------------------------------------------------------ documents

def new_op(*, recall_id: str, kind: str, order_system: str, party_id: str, adapter_version: int, sim: float,
           run_id: str | None = None, lease_epoch: int | None = None, request: dict | None = None,
           day: int | None = None, now: datetime | None = None) -> dict:
    """An operation in state intent (not yet written)."""
    if kind not in KINDS:
        raise ValueError(f"unknown operation kind {kind!r}")
    now = now or utcnow()
    return {
        "_id": f"op-{secrets.token_hex(6)}", "recall_id": recall_id, "run_id": run_id, "kind": kind,
        "order_system": order_system, "party_id": party_id,
        "idempotency_key": idempotency_key(recall_id, party_id, kind, day=day),
        "adapter_version": adapter_version, "state": "intent", "lease_epoch": lease_epoch,
        "intent_at": sim, "sent_at": None, "sends": 0, "readback_at": None, "readback_value": None,
        "request": request or {}, "response": None, "hold_id": None, "error": None,
        "created_at": now, "updated_at": now,
    }


def open_op(db, doc: dict, *, session=None) -> tuple[dict, bool]:
    """Write the intent. (op, True) when written; (the existing op, False)
    when the idempotency key is taken: the effect was already asked for.

    Inside a transaction (the gate's), a racing insert surfaces as a
    transient error and the transaction is retried, so it is not caught here."""
    key = doc["idempotency_key"]
    existing = db.operations.find_one({"idempotency_key": key}, session=session)
    if existing is not None:
        return existing, False
    try:
        db.operations.insert_one(doc, session=session)
    except DuplicateKeyError:
        if session is not None:
            raise
        return db.operations.find_one({"idempotency_key": key}), False
    return doc, True


def get(db, op_id: str) -> dict | None:
    return db.operations.find_one({"_id": op_id})


def _move(db, op_id: str, from_states, fields: dict, *, inc: dict | None = None) -> dict | None:
    """Conditional transition: nothing happens unless the op is still in one
    of from_states, so racing workers and recovery can never regress it."""
    update = {"$set": {**fields, "updated_at": utcnow()}}
    if inc:
        update["$inc"] = inc
    return db.operations.find_one_and_update({"_id": op_id, "state": {"$in": list(from_states)}}, update,
                                             return_document=ReturnDocument.AFTER)


def mark_sent(db, op: dict, *, sim: float, adapter_version: int, hold_id: str | None = None,
              response=None, error: str | None = None) -> dict | None:
    fields = {"state": "sent", "sent_at": sim, "adapter_version": adapter_version, "response": response,
              "error": error}
    if hold_id is not None:
        fields["hold_id"] = hold_id
    return _move(db, op["_id"], OPEN_STATES, fields, inc={"sends": 1})


def verify(db, op: dict, *, readback_at: float, readback_value, adapter_version: int, **fields) -> dict | None:
    """intent/sent -> verified. Only a read-back may call this; Atlas refuses
    the write without readback_at and readback_value anyway."""
    if readback_at is None or readback_value is None:
        raise ValueError("an operation is verified only by a read-back (readback_at and readback_value)")
    return _move(db, op["_id"], OPEN_STATES, {"state": "verified", "readback_at": readback_at,
                                              "readback_value": readback_value,
                                              "adapter_version": adapter_version, **fields})


def fail(db, op: dict, error: str) -> dict | None:
    return _move(db, op["_id"], OPEN_STATES, {"state": "failed", "error": error})


def _outcome(op: dict, action: str, **extra) -> dict:
    """What every entry point returns. action: verified, sent, deferred,
    paused, contract_failed, failed, duplicate, moved, skipped (not an order system
    operation), error (a retry that raised; the op keeps its state)."""
    return {"op_id": op["_id"], "kind": op["kind"], "state": op["state"], "action": action,
            "order_system": op.get("order_system"), "party_id": op.get("party_id"), "adapter_version": op.get("adapter_version"),
            "hold_id": op.get("hold_id"), "readback_value": op.get("readback_value"), **extra}


def order_system_op_filter(**query) -> dict:
    """`query` restricted to the operations that are order system calls (ORDER_SYSTEM_KINDS)."""
    return {**query, "kind": {"$in": list(ORDER_SYSTEM_KINDS)}}


# ---------------------------------------------------------------- parties

def distributor_party(db, recall_id: str, order_system: str) -> dict | None:
    return db.parties.find_one({"recall_id": recall_id, "order_system": order_system, "channel": "order_system"})


def _order_system_of(db, party: dict) -> tuple[str, dict]:
    """(order system, distributor party) for a distributor or one of its accounts."""
    if party.get("order_system"):
        return party["order_system"], party
    if party.get("distributor"):
        dist = db.parties.find_one({"recall_id": party["recall_id"], "party_id": party["distributor"]})
        if dist and dist.get("order_system"):
            return dist["order_system"], dist
    raise ValueError(f"party {party.get('party_id')} is neither an order system distributor nor one of its accounts")


def account_party_doc(recall: dict, distributor: dict, row: dict, *, now: datetime, sim: float) -> dict:
    """A tier 2 store for one order system account, due at once. The id
    follows the directory (or the name's slug), so a party named earlier by a
    claim or by memory is the same party; the phone is the order system's, and
    the batch code is the distributor's (the recall's)."""
    party_id, entry = resolve_party(row["name"], recall.get("directory"))
    entry = {**(entry or {}), "name": row["name"], "channel": "sim", "phone": row["phone"], "tier": 2,
             "type": "store"}
    return stub_party(recall["_id"], party_id, row["name"], entry, "order_system", now,
                      product=product_fields(distributor), tier=2, type_="store", next_action_at=sim)


def upsert_accounts(db, recall: dict, distributor: dict, rows: list[dict], *, now: datetime,
                    sim: float) -> tuple[list[str], list[str]]:
    """Create the account parties that do not exist yet and tag every one with
    its distributor and account id, in one round trip. Returns (party ids in
    row order, party ids created now)."""
    ops, ids = [], []
    for row in rows:
        doc = account_party_doc(recall, distributor, row, now=now, sim=sim)
        ids.append(doc["party_id"])
        sets = {"distributor": distributor["party_id"], "account": row["account"]}
        # WS5's hook: an account that runs its own copy of the agent (a link
        # matches it, e.g. Summit Foods) is answered by that agent, not by phone.
        agent = _agent_fields(db, distributor.get("order_system"), row["account"])
        if agent:
            sets.update(agent)
            doc = {k: v for k, v in doc.items() if k not in sets}
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$setOnInsert": doc, "$set": sets}, upsert=True))
    if not ops:
        return [], []
    res = db.parties.bulk_write(ops, ordered=False)
    created = [ids[i] for i in sorted(res.upserted_ids)]
    return ids, created


def _agent_fields(db, order_system: str | None, account: str) -> dict | None:
    """levela.node.links.agent_party_fields, when the sub-recall package is present."""
    if not order_system:
        return None
    try:
        from ..node.links import agent_party_fields
    except ImportError:
        return None
    return agent_party_fields(db, order_system, account)


# ------------------------------------------------------------ the ledger

def book_forwards_claim(client, db, *, recall_id: str, party_id: str, evidence_id: str, forwards: list[dict],
                        now: datetime) -> dict:
    """The distributor's claim from its order system: every tin it shipped went to
    the listed accounts (V2 §9 forwards; one edge per destination, booked in
    the ledger's one transaction). This is the one place the order system books
    through the ledger, so a change to the ledger's claim interface is fixed
    here. evidence_id is the operation id, whose tool_events row (kind order system)
    is the claim's evidence."""
    return submit_claim(client, db, recall_id=recall_id, party_id=party_id, attempt_id=evidence_id,
                        source="order_system", raw={"forwards": forwards}, now=now)


def _log_evidence(db, op: dict, distributor: dict, rows: list[dict], endpoint: str, now: datetime) -> None:
    """The tool event behind the order system claim (invariant 3: every accepted claim has one)."""
    db.tool_events.replace_one({"_id": f"te-{op['_id']}"}, {
        "_id": f"te-{op['_id']}", "attempt_id": op["_id"], "party_id": distributor["party_id"],
        "recall_id": op["recall_id"], "conversation_id": None, "kind": "order_system", "source": "order_system",
        "payload": {"order_system": op["order_system"], "endpoint": endpoint, "adapter_version": op.get("adapter_version"),
                    "accounts": rows},
        "received_at": now}, upsert=True)


# --------------------------------------------------------------- context

def _client(ctx, order_system: str):
    try:
        return ctx.order_systems[order_system]
    except (KeyError, TypeError):
        raise LookupError(f"no client configured for order system {order_system!r} (ctx.order_systems)") from None


def _hit(ctx, name: str, op: dict) -> None:
    """Fire an order system failpoint (after_op_intent / after_op_sent) if one is armed."""
    fps = getattr(ctx, "failpoints", None)
    if fps is not None:
        fps.hit(name, {"_id": op["_id"], "party_id": op["party_id"], "recall_id": op["recall_id"],
                       "channel": "order_system"})


def _emit(ctx, op: dict, kind: str, text: str, *, level: str = "info", detail=None, sim: float | None = None):
    feed.emit(ctx.db, kind, text, recall_id=op["recall_id"], party_id=op["party_id"], level=level,
              source="worker", system=system(op["order_system"]), sim=sim, detail={"op_id": op["_id"], **(detail or {})})


# --------------------------------------------------------------- execute

def execute(ctx, op: dict, *, fresh: bool = False) -> dict:
    """Carry one operation as far as the facts allow and say how far.

    fresh=True means the intent was written just now by this caller, so the
    effect cannot exist yet and no read-back is needed first. Order system trouble
    never raises: a timeout leaves the op sent (or intent), a contract failure
    is contained, a refused request fails the op, and an unreachable order system
    during a read-back defers it.

    A form link or a rep escalation is not an order system call: it is returned as
    `skipped` before any order system, distributor or adapter is looked up, so no
    caller can seed an adapter for an order system that does not exist (order system None)."""
    db = ctx.db
    current = get(db, op["_id"])
    if current is None:
        raise LookupError(f"operation {op['_id']} does not exist")
    if current["state"] in FINAL_STATES:
        return _outcome(current, "duplicate")
    if current["state"] == "needs_reverify":
        return _outcome(current, "paused", note="waiting for a verified adapter")
    if current["kind"] not in ORDER_SYSTEM_KINDS:
        return _outcome(current, "skipped", note="not an order system operation")
    distributor = distributor_party(db, current["recall_id"], current["order_system"])
    if distributor is None:
        raise LookupError(f"recall {current['recall_id']} has no distributor on {feed.orders_label(current['order_system'])}")
    adapter = adapters.active(db, current["order_system"])
    if distributor.get("order_system_paused") or adapter is None:
        # Work asked for while the order system is paused waits with the flagged ops.
        flagged = _move(db, current["_id"], OPEN_STATES, {"state": "needs_reverify",
                                                          "reverify_from": current["state"]})
        return _outcome(flagged or current, "paused", note="order system paused: its contract failed")
    if fresh:
        _hit(ctx, "after_op_intent", current)
    handler = _HANDLERS[current["kind"]]
    try:
        return handler(ctx, current, adapter=adapter, order_system=_client(ctx, current["order_system"]),
                       distributor=distributor, fresh=fresh)
    except ContractError as exc:
        contain(ctx, current["order_system"], exc, op=current)
        return _outcome(get(db, current["_id"]), "contract_failed", note=exc.failure_text)
    except (OrderSystemTimeout, OrderSystemError) as exc:
        # A read-back (list_holds) the order system did not answer. The op keeps the
        # state it reached (intent, or sent if the POST went out), and the next
        # resolve reads back again before anything is resent.
        return _outcome(get(db, current["_id"]) or current, "deferred", note=f"order system unavailable: {exc}")


def resolve_in_doubt(ctx, op: dict) -> dict:
    """Settle an operation left in intent or sent by an earlier worker, a lost
    answer or a fault: read back first; resend only when the read-back shows
    nothing (after the settle window), with the same key."""
    return execute(ctx, op, fresh=False)


def run(ctx, kind: str, party: dict, *, run_id: str | None = None, lease_epoch: int | None = None) -> dict:
    """Open the operation for this effect and carry it out. A second call for
    the same effect finds the first operation by its key: a finished one
    returns its stored outcome without touching the order system, an unfinished one
    is resolved by read-back."""
    op, created = open_op(ctx.db, intent_for(ctx, kind, party, run_id=run_id, lease_epoch=lease_epoch))
    if not created and op["state"] in FINAL_STATES:
        return _outcome(op, "duplicate", duplicate=True)
    return execute(ctx, op, fresh=created)


def intent_for(ctx, kind: str, party: dict, *, run_id: str | None = None, lease_epoch: int | None = None) -> dict:
    """The intent document for one order system tool on one party (not yet written).
    The gate writes it with open_op() inside its own transaction, then calls
    execute(ctx, op, fresh=created).

    A distributor whose order system was switched owes a contract probe
    (parties.order_system_probe_due, the switch counted in order_system_probes): its
    read_inventory is then that probe, a read through the active adapter
    under the key "...:read_inventory:d<day>:probe<n>" with `probe: n` on the
    operation. Its ContractError is contained and repaired like any other
    operation's. A probe already opened (perhaps on an earlier simulated
    day) keeps its key, so asking again finds it instead of reading twice."""
    if kind not in ORDER_SYSTEM_KINDS:
        raise ValueError(f"{kind} is not an order system operation")
    db = ctx.db
    order_system, distributor = _order_system_of(db, party)
    recall = db.recalls.find_one({"_id": party["recall_id"]}, {"batch_code": 1})
    adapter = adapters.active(db, order_system)
    sim = sim_now(ctx, party["recall_id"])
    request = {"batch": recall["batch_code"]}
    day = probe = None
    if kind == "place_hold":
        request["scope"] = HOLD_SCOPE
    elif kind == "confirm_hold":
        placed = db.operations.find_one({"idempotency_key": idempotency_key(
            party["recall_id"], distributor["party_id"], "place_hold"), "state": "verified"})
        if placed is None:
            raise ValueError(f"no verified hold to confirm for {distributor['party_id']}")
        request.update(scope=HOLD_SCOPE, hold_id=placed["hold_id"])
    elif kind == "read_inventory":
        # One read per account, or (party = the distributor) one read of
        # every account a day: the harness's order_system_sync does the latter.
        if party.get("account"):
            request["account"] = party["account"]
        elif party.get("order_system_probe_due") and party.get("order_system_probes"):
            probe = int(party["order_system_probes"])
        day = sim_day(sim)
    doc = new_op(recall_id=party["recall_id"], kind=kind, order_system=order_system, party_id=party["party_id"],
                 adapter_version=(adapter or {}).get("version"), sim=sim, run_id=run_id,
                 lease_epoch=lease_epoch, request=request, day=day)
    if probe is not None:
        opened = db.operations.find_one({"recall_id": party["recall_id"], "party_id": party["party_id"],
                                         "kind": kind, "probe": probe}, {"idempotency_key": 1})
        doc.update(probe=probe, idempotency_key=(opened or {}).get("idempotency_key") or idempotency_key(
            party["recall_id"], party["party_id"], kind, day=day, probe=probe))
    return doc


def place_hold(ctx, distributor: dict, **kw) -> dict:
    return run(ctx, "place_hold", distributor, **kw)


def confirm_hold(ctx, distributor: dict, **kw) -> dict:
    return run(ctx, "confirm_hold", distributor, **kw)


def read_accounts(ctx, distributor: dict, **kw) -> dict:
    return run(ctx, "read_accounts", distributor, **kw)


def read_inventory(ctx, account: dict, **kw) -> dict:
    return run(ctx, "read_inventory", account, **kw)


# --------------------------------------------------------------- holds

def _settled(op: dict, sim: float) -> bool:
    """Has the order system had time to show a write made by this op? An op left in
    intent may still have been sent (a worker can die after the POST), so
    its intent time is the bound."""
    since = op.get("sent_at") if op.get("sent_at") is not None else op["intent_at"]
    return sim - since >= READBACK_SETTLE_H


def _read_hold(order_system, adapter: dict, op: dict) -> dict | None:
    """The op's hold as the order system shows it now: by hold id when the order system
    gave one, else the hold on this batch and scope (one key, one hold)."""
    hold_id = op.get("hold_id") or op["request"].get("hold_id")
    for row in order_system.list_holds(op["request"]["batch"], adapter=adapter):
        if hold_id is not None and row["hold_id"] == hold_id:
            return row
        if hold_id is None and row["account_scope"] == op["request"]["scope"]:
            return row
    return None


def _status_kind(adapter: dict, status: str, endpoint: str) -> str:
    if status in adapter["status_in_force"]:
        return "in_force"
    if status in adapter["status_pending"]:
        return "pending"
    raise ContractError(f"hold status {status} is not one the adapter knows", endpoint=endpoint)


def _send_write(ctx, op: dict, adapter: dict, sim: float, call) -> tuple[dict | None, dict | None]:
    """POST once with the op's key. (op after the send, failed outcome or None)."""
    try:
        hold_id, raw = call()
    except OrderSystemTimeout as exc:
        return mark_sent(ctx.db, op, sim=sim, adapter_version=adapter["version"], error=str(exc)), None
    except OrderSystemError as exc:
        if exc.effect_unknown:
            return mark_sent(ctx.db, op, sim=sim, adapter_version=adapter["version"], error=str(exc)), None
        failed = fail(ctx.db, op, str(exc)) or get(ctx.db, op["_id"])
        _emit(ctx, failed, "order_system_failed", f"{op['kind']} refused by the order system: {exc}", level="bad", sim=sim)
        return None, _outcome(failed, "failed", note=str(exc))
    _hit(ctx, "after_op_sent", op)
    return mark_sent(ctx.db, op, sim=sim, adapter_version=adapter["version"], hold_id=hold_id, response=raw), None


def _place_hold(ctx, op: dict, *, adapter: dict, order_system, distributor: dict, fresh: bool) -> dict:
    sim = sim_now(ctx, op["recall_id"])
    if not fresh:
        found = _read_hold(order_system, adapter, op)
        if found is not None:
            return _hold_seen(ctx, op, found, adapter, distributor, order_system)
        if not _settled(op, sim):
            return _outcome(op, "deferred", note="read-back shows nothing yet, inside the order system's 10-minute "
                                                 "read lag: not resending")
    req = op["request"]
    sent, refused = _send_write(ctx, op, adapter, sim, lambda: order_system.place_hold(
        req["batch"], req["scope"], op["idempotency_key"], adapter=adapter))
    if refused is not None:
        return refused
    if sent is None:
        return _outcome(get(ctx.db, op["_id"]), "moved", note="the operation changed while it was sent")
    found = _read_hold(order_system, adapter, sent)
    if found is not None:
        return _hold_seen(ctx, sent, found, adapter, distributor, order_system)
    label = sent.get("hold_id") or "request"
    _emit(ctx, sent, "order_system_sent", f"hold {label} sent; read-back shows nothing yet", level="warn",
          sim=sim, detail={"sends": sent["sends"], "error": sent.get("error")})
    return _outcome(sent, "sent", note="read-back shows nothing yet")


def _hold_seen(ctx, op: dict, found: dict, adapter: dict, distributor: dict, order_system) -> dict:
    """The read-back shows the hold: verify the op; confirm it if it is pending."""
    sim = sim_now(ctx, op["recall_id"])
    status = _status_kind(adapter, found["status"], adapter["paths"]["list_holds"])
    value = {"hold_id": found["hold_id"], "status": found["status"], "batch": found["batch"],
             "account_scope": found["account_scope"]}
    done = verify(ctx.db, op, readback_at=sim, readback_value=value, adapter_version=adapter["version"],
                  hold_id=found["hold_id"])
    if done is None:
        return _outcome(get(ctx.db, op["_id"]), "moved", note="the operation changed during the read-back")
    if status == "in_force":
        return _in_force(ctx, done, value, distributor, sim)
    _emit(ctx, done, "order_system_verified", f"hold {found['hold_id']} placed ({found['status']}), verified by "
                                        "read-back; confirming", sim=sim)
    confirm = new_op(recall_id=op["recall_id"], kind="confirm_hold", order_system=op["order_system"],
                     party_id=op["party_id"], adapter_version=adapter["version"], sim=sim,
                     run_id=op.get("run_id"), lease_epoch=op.get("lease_epoch"),
                     request={**op["request"], "hold_id": found["hold_id"]})
    child, created = open_op(ctx.db, confirm)
    res = _outcome(child, "duplicate") if child["state"] in FINAL_STATES else execute(ctx, child, fresh=created)
    return {**_outcome(done, res["action"], in_force=res["action"] in ("verified", "duplicate")), "confirm": res}


def _in_force(ctx, op: dict, value: dict, distributor: dict, sim: float) -> dict:
    ctx.db.parties.update_one({"_id": distributor["_id"]}, {"$set": {"order_system_hold": {
        **value, "op_id": op["_id"], "verified_at": sim}}})
    _emit(ctx, op, "order_system_verified", f"hold {value['hold_id']} verified by read-back", level="good", sim=sim,
          detail={"readback_value": value, "sends": op.get("sends")})
    return _outcome(op, "verified", in_force=True)


def _confirm_hold(ctx, op: dict, *, adapter: dict, order_system, distributor: dict, fresh: bool) -> dict:
    """Confirming is idempotent by nature, so a read-back that still shows the
    hold pending is reason enough to confirm again."""
    sim = sim_now(ctx, op["recall_id"])
    endpoint = adapter["paths"]["list_holds"]
    if not adapter["paths"].get("confirm_hold"):
        failed = fail(ctx.db, op, f"adapter v{adapter['version']} has no confirm step") or op
        return _outcome(failed, "failed", note="no confirm step")
    if not fresh:
        found = _read_hold(order_system, adapter, op)
        if found is not None and _status_kind(adapter, found["status"], endpoint) == "in_force":
            return _confirmed(ctx, op, found, adapter, distributor, sim)
    hold_id = op["request"]["hold_id"]
    sent, refused = _send_write(ctx, op, adapter, sim, lambda: (
        hold_id, order_system.confirm_hold(hold_id, op["idempotency_key"], adapter=adapter)))
    if refused is not None:
        return refused
    if sent is None:
        return _outcome(get(ctx.db, op["_id"]), "moved", note="the operation changed while it was sent")
    found = _read_hold(order_system, adapter, sent)
    if found is not None and _status_kind(adapter, found["status"], endpoint) == "in_force":
        return _confirmed(ctx, sent, found, adapter, distributor, sim)
    _emit(ctx, sent, "order_system_sent", f"hold {hold_id} confirm sent; read-back does not show it in force yet",
          level="warn", sim=sim)
    return _outcome(sent, "sent", note="read-back does not show the hold in force yet")


def _confirmed(ctx, op, found, adapter, distributor, sim) -> dict:
    value = {"hold_id": found["hold_id"], "status": found["status"], "batch": found["batch"],
             "account_scope": found["account_scope"]}
    done = verify(ctx.db, op, readback_at=sim, readback_value=value, adapter_version=adapter["version"],
                  hold_id=found["hold_id"])
    if done is None:
        return _outcome(get(ctx.db, op["_id"]), "moved", note="the operation changed during the read-back")
    return _in_force(ctx, done, value, distributor, sim)


# --------------------------------------------------------------- reads

def _read(ctx, op: dict, adapter: dict, order_system) -> tuple[list[dict] | None, dict | None]:
    """(canonical account rows, or None with the outcome to return)."""
    try:
        rows = order_system.list_accounts(op["request"]["batch"], adapter=adapter)
    except OrderSystemTimeout as exc:
        return None, _outcome(op, "deferred", note=str(exc))
    except OrderSystemError as exc:
        if exc.effect_unknown or not exc.sent:
            return None, _outcome(op, "deferred", note=str(exc))
        failed = fail(ctx.db, op, str(exc)) or get(ctx.db, op["_id"])
        return None, _outcome(failed, "failed", note=str(exc))
    _hit(ctx, "after_op_sent", op)
    return rows, None


def _read_accounts(ctx, op: dict, *, adapter: dict, order_system, distributor: dict, fresh: bool) -> dict:
    """Accounts become tier 2 stores, and the distributor's claim books every
    tin it shipped to them. The ledger is written before the op is verified,
    so a kill in between repeats idempotent writes instead of losing them."""
    rows, early = _read(ctx, op, adapter, order_system)
    if early is not None:
        return early
    db, now = ctx.db, utcnow()
    sim = sim_now(ctx, op["recall_id"])
    recall = db.recalls.find_one({"_id": op["recall_id"]}, {"directory": 1, "batch_code": 1})
    ids, created = upsert_accounts(db, recall, distributor, rows, now=now, sim=sim)
    forwards = [{"to_party_id": sid, "qty": row["qty_shipped"]} for sid, row in zip(ids, rows)
                if row["qty_shipped"] > 0]
    claim = None
    if forwards:
        _log_evidence(db, op, distributor, rows, adapter["paths"]["list_accounts"], now)
        claim = book_forwards_claim(ctx.client, db, recall_id=op["recall_id"], party_id=distributor["party_id"],
                                    evidence_id=op["_id"], forwards=forwards, now=now)
    shipped = sum(r["qty_shipped"] for r in rows)
    value = {"accounts": len(rows), "shipped": shipped, "rows": rows}
    done = verify(db, op, readback_at=sim, readback_value=value, adapter_version=adapter["version"],
                  sent_at=sim, claim_id=(claim or {}).get("claim_id"))
    if done is None:
        return _outcome(get(db, op["_id"]), "moved", note="the operation changed during the read")
    if created:
        runs.wake_planner(db, op["recall_id"], "parties_created")
    accepted = bool(claim and claim.get("accepted"))
    _emit(ctx, done, "order_system_accounts",
          f"accounts read: {len(rows)} accounts for batch {op['request']['batch']}, {len(created)} new parties, "
          f"{shipped:,} tins forwarded" + ("" if accepted or not forwards else
                                           f" (claim refused: {(claim or {}).get('reason')})"),
          level="good" if accepted or not forwards else "bad", sim=sim,
          detail={"created": created, "claim": claim})
    return _outcome(done, "verified", created=created, claim=claim)


def _read_inventory(ctx, op: dict, *, adapter: dict, order_system, distributor: dict, fresh: bool) -> dict:
    """What the order system says the account still has: recorded as order_system_available.
    An op on the distributor itself records it for every listed account."""
    db = ctx.db
    rows, early = _read(ctx, op, adapter, order_system)
    if early is not None:
        return early
    sim = sim_now(ctx, op["recall_id"])
    if op["party_id"] == distributor["party_id"]:
        return _read_all_inventory(ctx, op, rows, adapter, distributor, sim)
    account = db.parties.find_one({"recall_id": op["recall_id"], "party_id": op["party_id"]})
    row = next((r for r in rows if r["account"] == account.get("account")), None)
    if row is None:
        failed = fail(db, op, f"account {account.get('account')} is not in the order system's list") or op
        return _outcome(failed, "failed", note="account not listed")
    db.parties.update_one({"_id": account["_id"]}, {"$set": {"order_system_available": row["qty_in_stock"],
                                                            "order_system_available_at": sim}})
    value = {"account": row["account"], "qty_in_stock": row["qty_in_stock"], "qty_shipped": row["qty_shipped"]}
    done = verify(db, op, readback_at=sim, readback_value=value, adapter_version=adapter["version"], sent_at=sim)
    if done is None:
        return _outcome(get(db, op["_id"]), "moved", note="the operation changed during the read")
    return _outcome(done, "verified")


def _read_all_inventory(ctx, op: dict, rows: list[dict], adapter: dict, distributor: dict, sim: float) -> dict:
    db = ctx.db
    by_account = {r["account"]: r for r in rows}
    accounts = list(db.parties.find({"recall_id": op["recall_id"], "distributor": distributor["party_id"]},
                                   {"account": 1}))
    writes = [UpdateOne({"_id": a["_id"]}, {"$set": {"order_system_available": by_account[a["account"]]["qty_in_stock"],
                                                     "order_system_available_at": sim}})
              for a in accounts if a.get("account") in by_account]
    if writes:
        db.parties.bulk_write(writes, ordered=False)
    value = {"accounts": len(writes), "available": {a: r["qty_in_stock"] for a, r in sorted(by_account.items())}}
    done = verify(db, op, readback_at=sim, readback_value=value, adapter_version=adapter["version"], sent_at=sim)
    if done is None:
        return _outcome(get(db, op["_id"]), "moved", note="the operation changed during the read")
    return _outcome(done, "verified")


_HANDLERS = {"place_hold": _place_hold, "confirm_hold": _confirm_hold, "read_accounts": _read_accounts,
             "read_inventory": _read_inventory}


# -------------------------------------------------------- contain / retry

def mark_needs_reverify(db, order_system: str) -> dict:
    """Every intent/sent order system call of the order system -> needs_reverify
    (remembering where it was); verified and failed ops are untouched.
    {"flagged", "untouched"}.

    An escalate_rep op carries its distributor's order system and rests in `sent`
    until the rep reports; it is not an order system call, so it is neither flagged
    nor counted (retrying it would have no order system handler to run)."""
    res = db.operations.update_many(order_system_op_filter(order_system=order_system, state={"$in": list(OPEN_STATES)}),
                                    [{"$set": {"reverify_from": "$state", "state": "needs_reverify",
                                               "flagged_at": "$$NOW", "updated_at": "$$NOW"}}])
    untouched = db.operations.count_documents(order_system_op_filter(order_system=order_system, state={"$in": list(FINAL_STATES)}))
    return {"flagged": res.modified_count, "untouched": untouched}


def flagged_count(db, order_system: str) -> int:
    """How many of the order system's calls wait for a verified adapter (needs_reverify)."""
    return db.operations.count_documents(order_system_op_filter(order_system=order_system, state="needs_reverify"))


def contain(ctx, order_system: str, exc: ContractError, *, op: dict | None = None) -> dict:
    """Detect -> contain: record the failure on the active adapter, pause the
    order system's distributor parties, flag its open ops. The feed line and the
    planner wake-up happen once, from whichever worker paused it first."""
    db, now = ctx.db, utcnow()
    adapter = adapters.active(db, order_system)
    failure = {**exc.as_record(), "at": now, "op_id": (op or {}).get("_id"),
               "adapter_id": (adapter or {}).get("_id")}
    if adapter is not None:
        adapters.record_failure(db, adapter["_id"], failure)
    paused_now = []
    for party in db.parties.find({"order_system": order_system, "channel": "order_system", "order_system_paused": {"$ne": True}},
                                {"recall_id": 1, "party_id": 1}):
        res = db.parties.update_one({"_id": party["_id"], "order_system_paused": {"$ne": True}},
                                   {"$set": {"order_system_paused": True, "order_system_failure": exc.failure_text,
                                             "order_system_paused_at": now}})
        if res.modified_count:
            paused_now.append(party)
    counts = mark_needs_reverify(db, order_system)
    noun = "operation" if counts["flagged"] == 1 else "operations"
    for party in paused_now:
        feed.emit(db, "order_system_contract_failed",
                  f"contract failed: {exc.failure_text}. {counts['flagged']} {noun} flagged, "
                  f"{counts['untouched']} untouched", recall_id=party["recall_id"], party_id=party["party_id"],
                  level="bad", source="worker", system=system(order_system),
                  detail={"failure": {k: v for k, v in failure.items() if k != "at"}, **counts})
        runs.wake_planner(db, party["recall_id"], f"contract_failed:{order_system}")
    return {"failure": failure, "paused": bool(paused_now), **counts}


def retry_needs_reverify(ctx, order_system: str) -> list[dict]:
    """After a verified adapter is active: run exactly the flagged ops again,
    each reading back first under the new adapter (whose version it records).

    Each op is reopened to intent before it runs, so whatever stops this loop
    part way (a kill, an op that raises) leaves every op either still
    flagged (the repair's resume retries it: repair.request_repair) or in
    intent (claim-time recovery resolves it by read-back). An op that raises
    is recorded with its error and the loop goes on: one bad op must not
    strand the ones after it."""
    db = ctx.db
    adapter = adapters.active(db, order_system)
    out = []
    for op in db.operations.find(order_system_op_filter(order_system=order_system, state="needs_reverify")).sort("created_at", 1):
        reopened = _move(db, op["_id"], ("needs_reverify",), {"state": "intent", "adapter_version": adapter["version"],
                                                              "retried_at": utcnow()}, inc={"retries": 1})
        if reopened is None:
            continue
        try:
            out.append(execute(ctx, reopened, fresh=False))
        except Exception as exc:  # noqa: BLE001 - recorded on the op; the rest of the flagged ops still run
            error = f"{type(exc).__name__}: {exc}"
            failed = db.operations.find_one_and_update({"_id": op["_id"]},
                                                       {"$set": {"error": error, "updated_at": utcnow()}},
                                                       return_document=ReturnDocument.AFTER)
            out.append(_outcome(failed or reopened, "error", note=error))
    return out


def order_system_summary(db, order_system: str, recall_id: str | None = None) -> dict:
    """One row of the wall's systems list (V2 §12)."""
    scope = {"order_system": order_system, **({"recall_id": recall_id} if recall_id else {})}
    counts = {row["_id"]: row["n"] for row in db.operations.aggregate([
        {"$match": scope}, {"$group": {"_id": "$state", "n": {"$sum": 1}}}])}
    adapter = db.adapters.find_one({"order_system": order_system, "status": "active"}, {"version": 1, "source": 1})
    paused = db.parties.find_one({"channel": "order_system", "order_system_paused": True, **scope}, {"_id": 1}) is not None
    return {"system": system(order_system), "adapter_version": (adapter or {}).get("version"),
            "adapter_source": (adapter or {}).get("source"), "ops_verified": counts.get("verified", 0),
            "ops_flagged": counts.get("needs_reverify", 0),
            "ops_open": counts.get("intent", 0) + counts.get("sent", 0), "paused": paused}
