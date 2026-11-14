"""Match a recall notice to the node's own item file (spec "What a node is": Match).

The barcode decides when the notice carries one. Otherwise two searches
return candidates from the item file, which is stored as `episodes` of kind
"item" (one per item, embedded like any episode):

  search   the description's words against each item's words (normalised
           exact tokens; the Atlas Search leg where a search index exists)
  vector   levela.memory.retrieve with kind="item" ($vectorSearch on Atlas,
           exact cosine in Python elsewhere)

A deterministic decider ranks the union by word overlap (Jaccard), breaks
ties with the vector score, and cites the ids it relied on (the pick and
the runner-up). A confident pick needs overlap >= MIN_SCORE and a margin of
MIN_MARGIN over the runner-up; anything less is low confidence: the node
books nothing and asks a person (levela.node.child).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
import re

from ..memory import retrieve as retrieve_mod
try:
    from ..memory.govern import TTL as _TTL
    ITEM_TTL = _TTL["item"]
except (ImportError, KeyError):  # pragma: no cover
    ITEM_TTL = timedelta(days=365)
from ..memory.embed import cosine

ITEM_KIND = "item"
MIN_SCORE = 0.8
MIN_MARGIN = 0.1


def digits(code) -> str:
    return re.sub(r"\D", "", str(code or ""))


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def item_episode(item: dict, embedder, *, node_id: str) -> dict:
    text = item["description"]
    # Governed memory: the item file lives in this node's own database, so it
    # is fleet memory there; expires_at must be set or retrieval never admits it.
    return {"_id": f"item:{item['item_id']}", "kind": ITEM_KIND, "valid": True, "scope": "fleet",
            "node_id": node_id, "expires_at": datetime.now(timezone.utc) + ITEM_TTL, "invalidated_by": None,
            "item_id": item["item_id"], "description": text, "barcode": item.get("barcode"),
            "barcode_digits": digits(item.get("barcode")), "text": text,
            "embedding": embedder.embed([text])[0], "embedding_model": embedder.name}


def load_items(db, embedder, items: list[dict], *, node_id: str) -> int:
    for item in items:
        doc = item_episode(item, embedder, node_id=node_id)
        db.episodes.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return len(items)


def _vector(db, embedder, text: str, k: int) -> tuple[list[dict], str]:
    """Item candidates by vector: WS6's retrieve(kind=...) when it has the
    filter, else the same exact cosine over item episodes here."""
    try:
        params = inspect.signature(retrieve_mod.retrieve).parameters
    except (TypeError, ValueError):
        params = {}
    if "kind" in params:
        hits, mode = retrieve_mod.retrieve(db, embedder, text, k=k, kind=ITEM_KIND)
        return [h for h in hits if h.get("kind") == ITEM_KIND], mode
    query = embedder.embed([text])[0]
    scored = []
    for ep in db.episodes.find({"kind": ITEM_KIND, "valid": True, "embedding_model": embedder.name}):
        vec = ep.pop("embedding", None)
        if vec and len(vec) == len(query):
            ep["score"] = (1.0 + cosine(query, vec)) / 2.0
            scored.append(ep)
    scored.sort(key=lambda e: (-e["score"], str(e["_id"])))
    return scored[:k], "local"


def match(db, embedder, notice: dict, *, k: int = 5) -> dict:
    """{"decided_by": "barcode"|"description"|None, "item_id", "confidence",
    "cited": [episode ids], "candidates": [...], "needs_person": bool, "mode"}."""
    code = digits(notice.get("barcode"))
    if code:
        hit = db.episodes.find_one({"kind": ITEM_KIND, "valid": True, "barcode_digits": code})
        if hit is not None:
            return {"decided_by": "barcode", "item_id": hit["item_id"], "description": hit["description"],
                    "confidence": 1.0, "cited": [hit["_id"]], "candidates": [], "needs_person": False,
                    "mode": "barcode"}
    text = notice.get("product") or notice.get("description") or ""
    q = words(text)
    vec_hits, mode = _vector(db, embedder, text, k)
    vscore = {h["_id"]: h.get("score", 0.0) for h in vec_hits}
    pool = {h["_id"]: h for h in vec_hits}
    for ep in db.episodes.find({"kind": ITEM_KIND, "valid": True}, {"embedding": 0}):
        if q & words(ep["description"]):
            pool.setdefault(ep["_id"], ep)
    ranked = sorted(({"id": i, "item_id": ep["item_id"], "description": ep["description"],
                      "search": round(jaccard(q, words(ep["description"])), 4),
                      "vector": round(vscore.get(i, 0.0), 4)} for i, ep in pool.items()),
                    key=lambda c: (-c["search"], -c["vector"], c["id"]))
    if not ranked:
        return {"decided_by": None, "item_id": None, "confidence": 0.0, "cited": [], "candidates": [],
                "needs_person": True, "mode": mode, "reason": "no candidate in the item file"}
    top = ranked[0]
    margin = top["search"] - (ranked[1]["search"] if len(ranked) > 1 else 0.0)
    cited = [c["id"] for c in ranked[:2]]
    confident = top["search"] >= MIN_SCORE and margin >= MIN_MARGIN
    out = {"decided_by": "description" if confident else None, "item_id": top["item_id"] if confident else None,
           "description": top["description"] if confident else None, "confidence": top["search"],
           "margin": round(margin, 4), "cited": cited, "candidates": ranked[:k], "needs_person": not confident,
           "mode": mode}
    if not confident:
        out["reason"] = (f"best candidate {top['item_id']} scored {top['search']:.2f} with a margin of "
                         f"{margin:.2f}: a person must confirm the item")
    return out
