"""Telling the buyers a store can identify (spec "Telling buyers").

For each identified buyer the node opens one attempt (levela.orchestrator.
attempts) with the idempotency key "<recall>:<buyer>:tell", then the voice
agent reads out the recall's instruction: the product, the batch, do not use
it, bring it back for a refund. It answers no health question. Each buyer
ends `told` (with that attempt's id; the buyers validator refuses `told`
without one) or `unreachable`.

A killed worker never calls a buyer twice. The attempt is written before the
call, and the buyer is pointed at it. The worker that picks the buyer up
again finds that attempt: one already ended settles the buyer; one left in
doubt (the call may or may not have been placed) is decided by the provider's
call log (`voice.probe`): a call found is adopted and the buyer is told; no
call found abandons the attempt and only then dials, under a new key.

Contacts stay in Summit's `buyers` collection. A report carries four numbers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from .. import feed
from ..orchestrator import attempts
from ..providers.base import ProviderError, ProviderTimeout

PURPOSE = "buyer_notice"
FAILPOINT_DIALED = "after_buyer_dial"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def instruction(recall: dict) -> str:
    action = {"destroy": "do not use it and throw it away", "return": "do not use it and bring it back for a refund"}
    return (f"A tin of {recall.get('product')} from batch {recall.get('batch_code')} that you bought is recalled "
            f"for a faulty lid seal. Please {action.get(recall.get('action'), action['return'])}. "
            "I can't answer health questions; please ask a doctor or pharmacist.")


def _key(recall_id: str, buyer_id: str, n: int) -> str:
    return f"{recall_id}:{buyer_id}:tell" + ("" if n == 1 else f":{n}")


def _settle(db, buyer: dict, attempt: dict, recall_id: str, sim: float | None) -> str:
    told = attempt["state"] == "completed"
    state = "told" if told else "unreachable"
    db.buyers.update_one({"_id": buyer["_id"], "state": "pending"},
                         {"$set": {"state": state, "attempt_id": attempt["_id"]}})
    feed.emit(db, "buyer_told" if told else "buyer_unreachable",
              f"buyer {buyer['_id']} {'told' if told else 'unreachable'} (contact kept at the store)",
              recall_id=recall_id, attempt_id=attempt["_id"], level="good" if told else "warn",
              source="worker", system="phone", sim=sim)
    return state


def _dial(db, voice, recall: dict, buyer: dict, attempt: dict, failpoints=None) -> dict:
    variables = {"party_id": buyer["_id"], "purpose": PURPOSE, "instruction": instruction(recall),
                 "product": recall.get("product"), "batch_code": recall.get("batch_code")}
    try:
        placed = voice.place_call(to_number=buyer["contact"], dynamic_variables=variables, attempt_id=attempt["_id"])
    except ProviderTimeout as exc:
        db.attempts.update_one({"_id": attempt["_id"]}, {"$set": {"provider_error": str(exc)}})
        return attempts.get(db, attempt["_id"])           # in doubt: the probe decides
    except ProviderError as exc:
        attempts.complete(db, attempt["_id"], "failed", reason=str(exc))
        return attempts.get(db, attempt["_id"])
    if failpoints is not None:
        failpoints(FAILPOINT_DIALED)
    attempts.mark_dialed(db, attempt["_id"], conversation_id=placed.conversation_id, call_sid=placed.call_sid)
    attempts.complete(db, attempt["_id"], "completed", reason="instruction read out")
    return attempts.get(db, attempt["_id"])


def _resolve(db, voice, attempt: dict, grace_s: float = 0.0) -> dict:
    """An attempt a dead worker left: adopt a call the provider shows, else abandon."""
    if attempt["state"] in attempts.FINAL and attempt["state"] != "abandoned":
        return attempt
    if attempt.get("dial_confirmed"):
        attempts.complete(db, attempt["_id"], "completed", reason="dial confirmed before the worker died")
        return attempts.get(db, attempt["_id"])
    since = attempt["intent_at"] - timedelta(seconds=5)
    calls = voice.probe.find_calls(to_number=attempt["to_number"], since=since)
    if calls:
        c = calls[0]
        attempts.adopt(db, attempt["_id"], evidence=f"{voice.probe.name} call log", conversation_id=c.conversation_id,
                       call_sid=c.call_sid, state="completed" if c.status == "completed" else "failed")
    else:
        attempts.abandon(db, attempt["_id"], "no call in the provider's log", by="buyers")
    return attempts.get(db, attempt["_id"])


def tell_one(db, voice, recall: dict, buyer: dict, *, worker: str = "node", sim: float | None = None,
             failpoints=None) -> str:
    rid = recall["_id"]
    buyer = db.buyers.find_one({"_id": buyer["_id"]})
    if buyer["state"] != "pending":
        return buyer["state"]
    n = 1
    if buyer.get("attempt_id"):
        prior = _resolve(db, voice, attempts.get(db, buyer["attempt_id"]))
        if prior["state"] != "abandoned":
            if prior["state"] in attempts.FINAL:
                return _settle(db, buyer, prior, rid, sim)
            return "pending"
        n = int(prior.get("try", 1)) + 1
    try:
        attempt = attempts.open_attempt(db, recall_id=rid, party_id=buyer["_id"], channel="voice", purpose=PURPOSE,
                                        owner=worker, to_number=buyer["contact"],
                                        extra={"idempotency_key": _key(rid, buyer["_id"], n), "try": n,
                                               "buyer_id": buyer["_id"]})
    except (attempts.AttemptRefused, DuplicateKeyError):
        return "pending"
    db.buyers.update_one({"_id": buyer["_id"]}, {"$set": {"attempt_id": attempt["_id"]}})
    done = _dial(db, voice, recall, buyer, attempt, failpoints)
    if done["state"] in attempts.FINAL:
        return _settle(db, buyer, done, rid, sim)
    return "pending"


def tell_all(db, voice, recall_id: str, *, worker: str = "node", sim: float | None = None, failpoints=None) -> dict:
    recall = db.recalls.find_one({"_id": recall_id})
    out = {"told": 0, "unreachable": 0, "pending": 0}
    for buyer in db.buyers.find({"state": "pending"}).sort("_id", 1):
        out[tell_one(db, voice, recall, buyer, worker=worker, sim=sim, failpoints=failpoints)] += 1
    return out
