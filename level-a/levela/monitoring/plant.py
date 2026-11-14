"""Calling the plant (spec "Store records" > "Calling the plant").

When a recall opens, ensure_plant_party() creates the plant that made the
batch as a party like any other: type plant, tier 0, due at once, holding no
tins on the ledger. It asks three questions, stored as data in `questions`
(levela.api.questions; applies_to tier [0], so only the plant gets them):

  other_batches   Which other batches ran with the same fault?
  shipped_count   How many tins of this batch did you ship to us?
  action          What must stores do with the tins: return them or destroy them?

The voice agent records each answer with submit_answer (answers.<qid> =
{value, at, attempt_id}: stored against the plant's attempt). apply_answers()
then reads them:

  * the shipped count is checked against the company's own receiving records
    (the recall's `received_from_plant`, else the sum of its shipments); a
    difference is a disputed edge plant -> company, which keeps open_units
    above 0, so it blocks all clear like any other dispute;
  * a batch the plant names is only proposed (`proposed_batches`); it joins
    `affected_batches` when a person confirms it (confirm()); the same for
    `action` (`proposed_action`).

The barcode is proven before blocks roll out: see levela.monitoring.link
(block_allowed) and store_events (_verify_barcode).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .. import feed
from ..api import questions as questions_

log = logging.getLogger(__name__)

PLANT_PARTY_ID = "maple-farm-plant"
PLANT_NAME = "Maple Farm plant, Guelph"
COMPANY_ID = "maple-farm-foods"          # the company doing the recall: the receiving side of the plant edge
PLANT_TIER = 0
PLANT_QUESTIONS = (
    ("other_batches", "Which other batches ran with the same fault?", "text"),
    ("shipped_count", "How many tins of this batch did you ship to us?", "count"),
    ("action", "What must stores do with the tins: return them or destroy them?", "text"),
)
ACTIONS = ("return", "destroy")
FEED_SYSTEM = "store records"
_BATCH = re.compile(r"\b([A-Z])\s*-?\s*(\d{4})\b")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def wants_plant(recall: dict) -> bool:
    """A recall that names its plant, or one opened from a signal."""
    return bool(recall.get("plant")) or recall.get("source") == "signal"


def _run_sim_start(db, recall_id: str, session=None) -> float:
    run = db.runs.find_one({"recall_id": recall_id}, {"clock": 1}, session=session) or {}
    try:
        return float((run.get("clock") or {}).get("sim_start", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _channel(recall: dict, plant: dict) -> tuple[str, str | None]:
    import os
    if (recall.get("options") or {}).get("all_sim") or recall.get("mode") == "sim":
        return "sim", plant.get("sim_phone")
    phone = plant.get("phone") or os.environ.get(plant.get("phone_env") or "PLANT_PHONE", "")
    if phone:
        return "voice", phone
    return "form", None


def plant_party_doc(recall: dict, *, next_action_at: float, now: datetime) -> dict:
    """The plant as a party: no tins on its books (shipped_in 0), due at once."""
    import secrets
    from ..ledger.transfers import ledger_fields, v2_fields
    plant = recall.get("plant") or {}
    pid = plant.get("party_id") or PLANT_PARTY_ID
    channel, phone = _channel(recall, plant)
    return {
        **v2_fields(tier=PLANT_TIER, found_via="recall", next_action_at=next_action_at),
        "_id": f"{recall['_id']}:{pid}", "recall_id": recall["_id"], "party_id": pid,
        "name": plant.get("name") or PLANT_NAME, "type": "plant", **ledger_fields(0, recall),
        "state": "pending", "channel": channel, "phone": phone,
        "profile": plant.get("profile") or "plant that made the batch; answers about batches, counts and the action",
        "discovered_by": "recall", "form_token": secrets.token_urlsafe(6), "created_at": now,
    }


def ensure_plant_party(db, recall: dict, *, session=None, now: datetime | None = None) -> dict | None:
    """Create the plant party and its three questions when the recall opens.
    Idempotent: an existing plant party and existing questions are kept.
    Returns {party_id, name, created, questions: {key: question_id}}, or None
    for a recall that names no plant and did not come from a signal."""
    if not recall or not wants_plant(recall):
        return None
    now = now or utcnow()
    rid = recall["_id"]
    sim_start = _run_sim_start(db, rid, session)
    doc = plant_party_doc(recall, next_action_at=sim_start, now=now)
    res = db.parties.update_one({"recall_id": rid, "party_id": doc["party_id"]}, {"$setOnInsert": doc},
                                upsert=True, session=session)
    asked = {}
    for key, text, answer_type in PLANT_QUESTIONS:
        q = questions_.find_same(db, rid, text, session=session)
        if q is None:
            q = questions_._insert_next(db, {
                "recall_id": rid, "text": questions_.normalize_text(text), "answer_type": answer_type,
                "applies_to": {"tier": [PLANT_TIER]}, "asked_at": round(sim_start, 4), "created_at": now,
                "by": "plant_call", "plant_key": key}, session=session)
        asked[key] = q["question_id"]
        questions_.fan_out(db, q, sim_start, session=session)
    db.recalls.update_one({"_id": rid}, {"$set": {"plant_party_id": doc["party_id"], "plant_questions": asked}},
                          session=session)
    if res.upserted_id is not None and session is None:
        feed.emit(db, "party_discovered", f"{doc['name']} added: the plant is called first, with the evidence",
                  recall_id=rid, party_id=doc["party_id"], system=FEED_SYSTEM, detail={"questions": asked})
    return {"party_id": doc["party_id"], "name": doc["name"], "created": res.upserted_id is not None,
            "questions": asked}


def receiving_count(db, recall: dict) -> int:
    """The company's own receiving records for the batch."""
    if isinstance(recall.get("received_from_plant"), int):
        return int(recall["received_from_plant"])
    rows = list(db.shipments.aggregate([{"$match": {"recall_id": recall["_id"]}},
                                        {"$group": {"_id": None, "n": {"$sum": "$qty"}}}]))
    return int(rows[0]["n"]) if rows else int(recall.get("units_total") or 0)


def batches_named(text, own: str) -> list[str]:
    out = []
    for letter, digits in _BATCH.findall(str(text or "").upper()):
        code = f"{letter}-{digits}"
        if code != own and code not in out:
            out.append(code)
    return out


def action_named(text) -> str | None:
    words = str(text or "").lower()
    found = [a for a in ACTIONS if a in words]
    return found[0] if len(found) == 1 else None


def _count_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return _count_value(value.get("count"))
    if isinstance(value, str) and value.strip().replace(",", "").isdigit():
        return int(value.strip().replace(",", ""))
    return None


def apply_answers(db, recall_id: str, *, now: datetime | None = None) -> dict:
    """Read the plant's answers and act on them (see the module docstring).
    Safe to run again: the edge is upserted, proposals are sets."""
    from ..db.client import run_txn
    from ..ledger.reconciler import open_units
    from ..ledger.transfers import edge_id, edge_numbers
    now = now or utcnow()
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None:
        return {"applied": False, "reason": f"unknown recall {recall_id}"}
    pid = recall.get("plant_party_id") or PLANT_PARTY_ID
    plant = db.parties.find_one({"recall_id": recall_id, "party_id": pid})
    keys = recall.get("plant_questions") or {}
    if plant is None or not keys:
        return {"applied": False, "reason": "the recall has no plant call"}
    answers = plant.get("answers") or {}
    got = {k: answers.get(qid) for k, qid in keys.items() if answers.get(qid) is not None}
    out: dict = {"applied": True, "answers": {k: {"value": v.get("value"), "attempt_id": v.get("attempt_id")}
                                              for k, v in got.items()}}
    sets: dict = {f"plant_answers.{k}": v for k, v in out["answers"].items()}
    adds: dict = {}

    if "other_batches" in got:
        named = batches_named(got["other_batches"]["value"], recall["batch_code"])
        if named:
            adds["proposed_batches"] = {"$each": named}
        out["proposed_batches"] = named
    if "action" in got:
        action = action_named(got["action"]["value"])
        if action:
            sets["proposed_action"] = action
        out["proposed_action"] = action

    if "shipped_count" in got:
        said = _count_value(got["shipped_count"]["value"])
        if said is not None:
            received = receiving_count(db, recall)
            corr, disp, state = edge_numbers(said, received)
            eid = edge_id(recall_id, pid, COMPANY_ID)
            edge = {"recall_id": recall_id, "from_party": pid, "to_party": COMPANY_ID, "kind": "plant_count",
                    "sender_qty": said, "recipient_qty": received, "corroborated_qty": corr, "disputed_qty": disp,
                    "state": state, "updated_at": now,
                    "note": "the plant's shipped count against the company's receiving records"}

            def txn(session):
                db.edges.update_one({"_id": eid}, {"$set": edge, "$setOnInsert": {"created_at": now}}, upsert=True,
                                    session=session)
                n = open_units(db, recall_id, session)
                db.recalls.update_one({"_id": recall_id}, {"$set": {"open_units": n}}, session=session)

            run_txn(db.client, txn)
            out["shipped_count"] = {"plant": said, "receiving": received, "disputed_qty": disp, "state": state}
            if disp:
                feed.emit(db, "edge_disputed", f"{plant['name']} says it shipped {said} tins; the receiving records "
                          f"show {received}: {disp} disputed, all clear is blocked", recall_id=recall_id,
                          party_id=pid, level="warn", system=FEED_SYSTEM, detail=out["shipped_count"])
    update = {"$set": {**sets, "plant_checked_at": now}}
    if adds:
        update["$addToSet"] = adds
    db.recalls.update_one({"_id": recall_id}, update)
    return out


def confirm(db, recall_id: str, *, by: str, batch: str | None = None, action: str | None = None,
            now: datetime | None = None) -> dict:
    """A person confirms what the plant said: a named batch joins
    affected_batches, the action is set. Nothing the plant says changes the
    recall before this."""
    now = now or utcnow()
    if not isinstance(by, str) or not by.strip():
        raise ValueError("a confirmation names the person (by)")
    recall = db.recalls.find_one({"_id": recall_id})
    if recall is None:
        raise ValueError(f"unknown recall {recall_id}")
    update: dict = {"$set": {"updated_at": now}}
    if batch:
        if batch not in (recall.get("proposed_batches") or []):
            raise ValueError(f"{batch} was not named by the plant")
        update["$addToSet"] = {"affected_batches": batch}
        update["$set"][f"confirmed.batches.{batch}"] = {"by": by, "at": now}
    if action:
        if action not in ACTIONS:
            raise ValueError("action is return or destroy")
        update["$set"].update(action=action, action_confirmed_by=by, action_confirmed_at=now)
    db.recalls.update_one({"_id": recall_id}, update)
    feed.emit(db, "plant_confirmed", f"{by} confirmed: " + ", ".join(
        p for p in ((f"batch {batch} added to the recall" if batch else ""),
                    (f"stores must {action} the tins" if action else "")) if p),
        recall_id=recall_id, system=FEED_SYSTEM, detail={"batch": batch, "action": action, "by": by})
    return db.recalls.find_one({"_id": recall_id}, {"affected_batches": 1, "action": 1, "proposed_batches": 1,
                                                    "proposed_action": 1})


def on_answer(db, party: dict | None) -> dict | None:
    """Called after submit_answer records an answer: a plant's answers are
    read at once. Never raises (the answer is already stored)."""
    if not party or party.get("type") != "plant":
        return None
    try:
        return apply_answers(db, party["recall_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("plant answers for %s not applied: %s", party.get("recall_id"), exc)
        return None
