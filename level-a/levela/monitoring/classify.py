"""Classifying a complaint (spec "Store records" > "Classifying a complaint").

  1  reason code first     a code in REASON_CODES names the defect; no model, no search
  2  similar complaints    the five nearest `kind: "complaint"` episodes (Atlas
                           $vectorSearch through levela.memory.retrieve, or local
                           scoring on a plain mongod), each with a confirmed defect
  3  the decider           returns {defect, confidence, evidence}; the handler checks
                           that every evidence id is a valid complaint episode
  4  low confidence        no defect is recorded; the quality lead is asked
                           "What was wrong with this tin?" (a question document)
  5  findings              a person's finding confirms or replaces the classification;
                           a wrong one marks the episodes it relied on valid: false

The decider is deterministic by default (a similarity-weighted vote). With an
Anthropic key ModelDecider asks the model for the same JSON object; any model
failure falls back to the rules, so the path works with no key.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from .. import feed
from ..ledger.names import normalise
from ..memory.embed import EMBED_ERRORS, cosine

log = logging.getLogger(__name__)

DEFECTS = {
    "seal_fault": "faulty lid seal",
    "foreign_body": "foreign body in the powder",
    "label_error": "wrong or missing label",
    "dented_tin": "dented or damaged tin",
}
# The complaint form's reason codes. Codes are compared upper case, spaces as dashes.
REASON_CODES = {
    "SEAL": "seal_fault", "SEAL-FAULT": "seal_fault", "LID-SEAL": "seal_fault", "R-SEAL": "seal_fault",
    "FOREIGN": "foreign_body", "FOREIGN-BODY": "foreign_body",
    "LABEL": "label_error", "LABEL-ERROR": "label_error",
    "DENT": "dented_tin", "DAMAGED-TIN": "dented_tin",
}
K = 5                       # nearest complaint episodes
try:
    from ..memory.govern import TTL as _TTL
    COMPLAINT_TTL = _TTL["complaint"]
except (ImportError, KeyError):  # pragma: no cover
    COMPLAINT_TTL = timedelta(days=365)
CONFIDENCE_MIN = 0.65       # below this no defect is recorded and a person is asked
STRONG_COSINE = 0.42        # a nearest neighbour this close counts as full strength
MAX_EVIDENCE = 3
QUALITY_LEAD_QUESTION = "What was wrong with this tin?"
QUALITY_LEAD = "quality_lead"
FEED_SYSTEM = "store records"

# Past complaints a person confirmed (memory for step 2). Seeded once per database.
HISTORY = [
    ("e-0101", "seal_fault", "lid seal was broken when I opened the tin and the powder was clumped"),
    ("e-0144", "seal_fault", "the foil seal under the lid was lifted and the powder smelled sour"),
    ("e-0150", "seal_fault", "seal on the tin was not stuck down, the powder was hard and lumpy"),
    ("e-0162", "seal_fault", "tin lid did not seal properly, powder had moisture and clumps"),
    ("e-0171", "seal_fault", "the inner seal was loose and the powder smelled sour"),
    ("e-0203", "foreign_body", "found a small piece of plastic in the powder"),
    ("e-0211", "foreign_body", "there was a black speck like metal in the milk powder"),
    ("e-0305", "label_error", "the label shows the wrong best before date"),
    ("e-0402", "dented_tin", "tin arrived dented and the side was crushed"),
]


class ComplaintError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def code_defect(reason_code) -> str | None:
    if not isinstance(reason_code, str) or not reason_code.strip():
        return None
    key = re.sub(r"[\s_]+", "-", reason_code.strip().upper())
    return REASON_CODES.get(key)


# ---- memory: complaint episodes ---------------------------------------------------

def complaint_episode(embedder, *, episode_id: str, text: str, defect: str, confirmed_by: str,
                      complaint_id: str | None = None, now: datetime | None = None) -> dict:
    return {"_id": episode_id, "kind": "complaint", "text": text, "summary": text, "defect": defect,
            "confirmed_by": confirmed_by, "complaint_id": complaint_id, "valid": True, "scope": "fleet",
            # Governed memory (levela.memory.govern): retrieval admits only an
            # episode whose expires_at is in the future, on Atlas and locally.
            "expires_at": (now or utcnow()) + COMPLAINT_TTL, "invalidated_by": None,
            "tags": [defect], "embedding": embedder.embed([text])[0],
            "embedding_model": embedder.name, "created_at": now or utcnow()}


def seed_history(db, embedder) -> list[str]:
    """Insert the confirmed past complaints that are missing. Idempotent."""
    made = []
    for ep_id, defect, text in HISTORY:
        doc = complaint_episode(embedder, episode_id=ep_id, text=text, defect=defect, confirmed_by="quality-lead")
        res = db.episodes.update_one({"_id": ep_id}, {"$setOnInsert": doc}, upsert=True)
        if res.upserted_id is not None:
            made.append(ep_id)
    # Episodes seeded before expiry was governed carry expires_at null, which
    # governed retrieval never admits: give them the complaint TTL.
    db.episodes.update_many({"kind": "complaint", "expires_at": None},
                            {"$set": {"expires_at": utcnow() + COMPLAINT_TTL, "invalidated_by": None}})
    return made


def _local_nearest(db, embedder, text: str, k: int) -> list[dict]:
    query = embedder.embed([text])[0]
    now = utcnow()
    scored = []
    for ep in db.episodes.find({"kind": "complaint", "valid": True, "embedding_model": embedder.name,
                                "embedding": {"$exists": True}}):
        if _expired(ep, now):
            continue
        vector = ep.pop("embedding")
        if len(vector) != len(query):
            continue
        ep["score"] = (1.0 + cosine(query, vector)) / 2.0
        scored.append(ep)
    scored.sort(key=lambda e: (-e["score"], str(e["_id"])))
    return scored[:k]


def _expired(ep: dict, now: datetime) -> bool:
    exp = ep.get("expires_at")
    if not isinstance(exp, datetime):
        return False
    return (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) <= now


def nearest_complaints(db, embedder, text: str, *, k: int = K, mode: str = "auto") -> tuple[list[dict], str]:
    """The k nearest valid complaint episodes: (hits, "atlas" | "local").
    Uses levela.memory.retrieve with kind="complaint" when it takes that
    filter (WS6); otherwise scores locally over the same episodes."""
    from ..memory import retrieve as retrieve_
    try:
        params = inspect.signature(retrieve_.retrieve).parameters
    except (TypeError, ValueError):
        params = {}
    if mode != "local" and "kind" in params:
        hits, how = retrieve_.retrieve(db, embedder, text, k=k, mode=mode, kind="complaint")
        now = utcnow()
        hits = [h for h in hits if h.get("kind") == "complaint" and h.get("valid", True) and not _expired(h, now)]
        if hits or how == "atlas":
            return hits, how
    return _local_nearest(db, embedder, text, k), "local"


# ---- the decider --------------------------------------------------------------------

def rules_decider(text: str, hits: list[dict]) -> dict:
    """Deterministic: a vote of the neighbours weighted by cosine similarity.
    confidence = (the winner's share of the positive similarity) x (how close
    its nearest neighbour is, capped at STRONG_COSINE)."""
    votes: dict[str, float] = {}
    best: dict[str, float] = {}
    ids: dict[str, list[str]] = {}
    for h in hits:
        d = h.get("defect")
        sim = 2.0 * float(h.get("score", 0.5)) - 1.0
        if not d or sim <= 0:
            continue
        votes[d] = votes.get(d, 0.0) + sim
        best[d] = max(best.get(d, 0.0), sim)
        ids.setdefault(d, []).append(str(h["_id"]))
    if not votes:
        return {"defect": None, "confidence": 0.0, "evidence": []}
    defect = max(votes, key=lambda d: (votes[d], d))
    share = votes[defect] / sum(votes.values())
    confidence = share * min(1.0, best[defect] / STRONG_COSINE)
    return {"defect": defect, "confidence": round(confidence, 3), "evidence": ids[defect][:MAX_EVIDENCE]}


class ModelDecider:
    """The same contract through the Anthropic Messages API. Never used in
    tests; any failure falls back to rules_decider."""

    def __init__(self, api_key: str, model: str):
        from ..harness.models import AnthropicClient
        self.client, self.model = AnthropicClient(api_key), model

    def __call__(self, text: str, hits: list[dict]) -> dict:
        from ..harness.models import tool_definition
        lines = "\n".join(f"- {h['_id']}: {h.get('defect')}: {h.get('text')}" for h in hits)
        tool = tool_definition("classify", "Record the defect in this complaint.", {
            "type": "object", "properties": {
                "defect": {"type": ["string", "null"], "enum": [*DEFECTS, None]},
                "confidence": {"type": "number"}, "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["defect", "confidence", "evidence"]})
        try:
            reply = self.client.messages(
                model=self.model, tools=[tool],
                system=("You classify a food complaint into one defect. Use only the past complaints given as "
                        "evidence and cite their ids. Call the classify tool exactly once."),
                messages=[{"role": "user", "content": f"Complaint: {text}\n\nPast complaints:\n{lines}"}])
            use = next(u for u in reply.tool_uses if u.name == "classify")
            out = dict(use.input)
            return {"defect": out.get("defect"), "confidence": float(out.get("confidence") or 0),
                    "evidence": [str(e) for e in out.get("evidence") or []], "by": self.model}
        except Exception as exc:  # noqa: BLE001 - the rules decide when the model cannot
            log.warning("model decider failed, using rules: %s", exc)
            return rules_decider(text, hits)


def make_decider(settings=None):
    if settings is not None and getattr(settings, "anthropic_api_key", "") and \
            getattr(settings, "planner", "rules") == "claude":
        try:
            return ModelDecider(settings.anthropic_api_key, settings.planner_model)
        except Exception as exc:  # noqa: BLE001
            log.warning("model decider unavailable: %s", exc)
    return rules_decider


# ---- the handler ----------------------------------------------------------------------

def _new_complaint_id(db) -> str:
    n = db.complaints.estimated_document_count() + 1
    while True:
        cid = f"c-{n:04d}"
        if db.complaints.find_one({"_id": cid}, {"_id": 1}) is None:
            return cid
        n += 1


def receive(db, payload: dict, *, now: datetime | None = None) -> dict:
    """Insert one complaint as the complaint form sent it (not yet classified)."""
    now = now or utcnow()
    text = " ".join(str(payload.get("text") or "").split())[:1000]
    batch = str(payload.get("batch_code") or "").strip()
    bought_at = " ".join(str(payload.get("bought_at") or "").split())
    if not batch or not bought_at or not text:
        raise ComplaintError("bad_complaint", "a complaint needs batch_code, bought_at and text")
    day = payload.get("day", 0)
    if isinstance(day, bool) or not isinstance(day, (int, float)):
        raise ComplaintError("bad_complaint", "day must be a number (the simulated day)")
    cid = payload.get("complaint_id") or _new_complaint_id(db)
    doc = {"_id": str(cid), "batch_code": batch, "bought_at": bought_at, "store_key": normalise(bought_at),
           "reason_code": payload.get("reason_code") or None, "text": text, "day": float(day), "at": now,
           "classification": None, "signal_id": None}
    res = db.complaints.update_one({"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True)
    stored = db.complaints.find_one({"_id": doc["_id"]})
    return {**stored, "duplicate": res.upserted_id is None}


def _evidence_exists(db, ids: list[str]) -> bool:
    if not ids:
        return False
    found = db.episodes.count_documents({"_id": {"$in": list(ids)}, "kind": "complaint", "valid": True})
    return found == len(set(ids))


def classify(db, complaint_id: str, *, embedder, decider=None, mode: str = "auto",
             trace_id: str | None = None, now: datetime | None = None) -> dict:
    """Classify one complaint and store the classification. Returns it."""
    now = now or utcnow()
    c = db.complaints.find_one({"_id": complaint_id})
    if c is None:
        raise ComplaintError("unknown_complaint", f"no complaint {complaint_id}")
    if (c.get("classification") or {}).get("defect"):
        return c["classification"]
    by = trace_id or f"tr-{secrets.token_hex(3)}"
    defect = code_defect(c.get("reason_code"))
    if defect:
        cls = {"defect": defect, "confidence": 1.0, "evidence": [], "by": "reason_code",
               "method": "reason_code", "model_calls": 0}
    else:
        try:
            hits, how = nearest_complaints(db, embedder, c["text"], mode=mode)
        except EMBED_ERRORS as exc:
            log.warning("complaint %s: embedding failed: %s", complaint_id, exc)
            hits, how = [], "off"
        decide = decider or rules_decider
        raw = decide(c["text"], hits) or {}
        evidence = [str(e) for e in raw.get("evidence") or []]
        confident = (raw.get("defect") in DEFECTS and float(raw.get("confidence") or 0) >= CONFIDENCE_MIN
                     and _evidence_exists(db, evidence))
        cls = {"defect": raw.get("defect") if confident else None,
               "confidence": round(float(raw.get("confidence") or 0), 3), "evidence": evidence,
               "by": raw.get("by") or by, "method": f"similar_complaints:{how}",
               "neighbours": [str(h["_id"]) for h in hits],
               "model_calls": 1 if isinstance(decide, ModelDecider) else 0}
        if not confident:
            cls["proposed_defect"] = raw.get("defect")
            cls["question_id"] = ask_quality_lead(db, c, now=now)
            cls["status"] = "needs_person"
    db.complaints.update_one({"_id": complaint_id}, {"$set": {"classification": cls, "classified_at": now}})
    if cls["defect"]:
        feed.emit(db, "complaint_classified",
                  f"Complaint {complaint_id} ({c['bought_at']}): {DEFECTS[cls['defect']]} on batch "
                  f"{c['batch_code']} ({cls['method'].split(':')[0].replace('_', ' ')}, "
                  f"confidence {cls['confidence']:.2f})", system=FEED_SYSTEM,
                  detail={"complaint_id": complaint_id, **{k: cls[k] for k in ("defect", "confidence", "evidence")}})
    else:
        feed.emit(db, "needs_human", f"Complaint {complaint_id}: not sure of the defect (confidence "
                  f"{cls['confidence']:.2f}); the quality lead is asked", level="warn", system=FEED_SYSTEM,
                  detail={"complaint_id": complaint_id, "question_id": cls.get("question_id")})
    return cls


def ask_quality_lead(db, complaint: dict, *, now: datetime | None = None) -> str:
    """One open question to the company's quality lead about this complaint,
    stored as data in `questions` (no recall yet, so no party fan-out)."""
    from ..api.questions import normalize_text
    qid = f"ql-{complaint['_id']}"
    db.questions.update_one({"_id": qid}, {"$setOnInsert": {
        "_id": qid, "question_id": qid, "recall_id": None, "to": QUALITY_LEAD,
        "text": normalize_text(QUALITY_LEAD_QUESTION), "answer_type": "text", "applies_to": {"tier": []},
        "complaint_id": complaint["_id"], "status": "open", "asked_at": complaint.get("day", 0),
        "created_at": now or utcnow(), "by": "classifier"}}, upsert=True)
    return qid


def record_finding(db, complaint_id: str, *, defect: str, by: str, embedder,
                   now: datetime | None = None) -> dict:
    """A person's finding on a complaint. It confirms the classification (or
    replaces it); a classification it contradicts marks the episodes that
    classification relied on valid: false. The finding becomes a confirmed
    complaint episode, so the next similar complaint retrieves it."""
    now = now or utcnow()
    if defect not in DEFECTS:
        raise ComplaintError("bad_defect", f"defect must be one of {', '.join(DEFECTS)}")
    if not isinstance(by, str) or not by.strip():
        raise ComplaintError("bad_finding", "a finding names the person (by)")
    c = db.complaints.find_one({"_id": complaint_id})
    if c is None:
        raise ComplaintError("unknown_complaint", f"no complaint {complaint_id}")
    prior = c.get("classification") or {}
    relied = list(prior.get("evidence") or [])
    wrong = bool(prior.get("defect") and prior["defect"] != defect)
    invalidated = []
    if wrong and relied:
        db.episodes.update_many({"_id": {"$in": relied}, "kind": "complaint"},
                                {"$set": {"valid": False, "invalidated_by": f"finding:{complaint_id}",
                                          "invalidated_at": now}})
        invalidated = relied
    ep_id = f"e-{complaint_id}"
    episode = complaint_episode(embedder, episode_id=ep_id, text=c["text"], defect=defect, confirmed_by=by,
                                complaint_id=complaint_id, now=now)
    db.episodes.replace_one({"_id": ep_id}, episode, upsert=True)
    cls = {"defect": defect, "confidence": 1.0, "evidence": [ep_id], "by": by, "method": "finding",
           "confirmed": not wrong and prior.get("defect") == defect, "replaced": prior or None,
           "invalidated": invalidated, "model_calls": 0}
    after = db.complaints.find_one_and_update({"_id": complaint_id},
                                              {"$set": {"classification": cls, "classified_at": now}},
                                              return_document=ReturnDocument.AFTER)
    db.questions.update_one({"_id": f"ql-{complaint_id}"},
                            {"$set": {"status": "answered", "answer": {"value": defect, "by": by, "at": now}}})
    feed.emit(db, "complaint_finding", f"{by}: complaint {complaint_id} is a {DEFECTS[defect]}"
              + (f"; {len(invalidated)} memories it relied on are marked invalid" if invalidated else ""),
              system=FEED_SYSTEM, level="warn" if wrong else "info",
              detail={"complaint_id": complaint_id, "defect": defect, "invalidated": invalidated})
    return after["classification"]


def complaint_view(doc: dict) -> dict:
    return json.loads(json.dumps({k: v for k, v in doc.items() if k != "at"}, default=str))
