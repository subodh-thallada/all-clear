"""Retrieval: which past episodes look like this party, and what to do about it.

Before a party's first call the worker embeds the party's profile, asks Atlas
Vector Search for the most similar episodes, and applies a fixed policy:

  forwards_to    this party passed stock on before -> suspect the named party
                 now, before anyone reports a transfer (only from the party's
                 own episodes: a similar party's partner is not this party's)
  over_reported  a first count once exceeded the shipment -> ask for the
                 delivery note before taking counts (similar parties count too)
  answered_hour  this party picked up at that hour of day before -> its
                 second call goes at that hour (parties.memory_hour, read by
                 the contact ladder; only from the party's own episodes: a
                 similar party's opening hours are not this party's)

$vectorSearch runs exact (ENN): the corpus is a few hundred vectors and the
answer must not depend on approximate candidates. A plain mongod has no
search process, so retrieval falls back to scoring the same vectors in
Python, reported as mode "local" so the wall never claims Atlas did it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pymongo.errors import OperationFailure

from ..db.schema import VECTOR_INDEX
from ..ledger.transfers import slugify
from .embed import EMBED_ERRORS, cosine

log = logging.getLogger(__name__)

MODES = ("auto", "atlas", "local")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def retrieve(db, embedder, profile: str, k: int = 3, mode: str = "auto") -> tuple[list[dict], str]:
    """Top-k valid episodes by profile similarity: (hits, "atlas" | "local").

    Each hit is the episode without its embedding, plus `score` on Atlas's
    cosine scale, (1 + cosine) / 2, whichever path scored it. Only episodes
    embedded by this embedder's model count, on either path (the vector
    index cannot filter on the model, so Atlas hits are filtered after).
    mode "atlas" raises when $vectorSearch is unavailable; "local" never
    asks Atlas; "auto" uses Atlas and scores locally when $vectorSearch is
    unavailable or returns nothing (no index yet, or it has not caught up
    with the writes).
    """
    _require_mode(mode)
    if mode != "local":
        try:
            found = _atlas(db, embedder.embed([profile])[0], k)
        except OperationFailure as exc:
            if mode == "atlas":
                raise
            log.debug("$vectorSearch unavailable (code %s): scoring locally", exc.code)
        else:
            hits = [_without_vector(h) for h in found if h.get("embedding_model") == embedder.name]
            if hits or mode == "atlas":
                return hits, "atlas"
    return _local(db, embedder, profile, k), "local"


def _atlas(db, query: list[float], k: int) -> list[dict]:
    """$vectorSearch over valid contact episodes, exact (ENN). Raises
    OperationFailure where there is no search process or no index."""
    return list(db.episodes.aggregate([
        {"$vectorSearch": {"index": VECTOR_INDEX, "path": "embedding", "queryVector": query, "exact": True,
                           "limit": k, "filter": {"valid": True, "kind": "contact"}}},
        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
    ]))


def _require_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")


def _without_vector(episode: dict) -> dict:
    return {key: value for key, value in episode.items() if key != "embedding"}


def _local(db, embedder, profile: str, k: int) -> list[dict]:
    """Exact cosine in Python over episodes embedded by this same model.

    A vector from another model (or the same model at another output size)
    lives in a different space: its score would rank nothing, so it is never
    scored at all.
    """
    query = embedder.embed([profile])[0]
    scored = []
    for ep in db.episodes.find({"valid": True, "kind": "contact", "embedding_model": embedder.name,
                                "embedding": {"$exists": True}}):
        vector = ep.pop("embedding")
        if len(vector) != len(query):
            continue
        ep["score"] = (1.0 + cosine(query, vector)) / 2.0
        scored.append(ep)
    scored.sort(key=lambda e: (-e["score"], str(e["_id"])))
    return scored[:k]


def policy(hits: list[dict], party: dict) -> list[dict]:
    """The fixed memory policy: two tags and one entity, three actions, each
    at most once (suspect_party once per named party).

      {"action": "suspect_party", "name", "episode_id", "from_party", "score"}
      {"action": "ask_delivery_note", "episode_id", "from_party"}
      {"action": "call_hour", "hour", "episode_id", "from_party"}

    suspect_party needs the party's own episode (similar
    profiles inherit tags, and an inherited forwards_to names another party's
    partner). ask_delivery_note may be inherited, but only a tier 1 party has
    a shipment record, so only a tier 1 party has a delivery note to check.
    call_hour, like suspect_party, needs the party's own episode with an
    answered call (entities.answered_hour): the most similar one wins.
    Hits come from retrieve() (`_id`) or from a stored memory check
    (`episode_id`).
    """
    own = party.get("party_id")
    has_note = party.get("tier", 1) == 1
    actions, seen = [], set()
    for hit in hits:
        tags = set(hit.get("tags") or ())
        ep_id = hit.get("_id", hit.get("episode_id"))
        source = hit.get("party_id")
        if "forwards_to" in tags and source == own:
            name = ((hit.get("entities") or {}).get("forwards_to") or "").strip()
            key = ("suspect", slugify(name))
            if name and key not in seen:
                seen.add(key)
                actions.append({"action": "suspect_party", "name": name, "episode_id": ep_id,
                                "from_party": source, "score": hit.get("score")})
        if "over_reported" in tags and has_note and "note" not in seen:
            seen.add("note")
            actions.append({"action": "ask_delivery_note", "episode_id": ep_id, "from_party": source})
        hour = _hour((hit.get("entities") or {}).get("answered_hour"))
        if hour is not None and source == own and "hour" not in seen:
            seen.add("hour")
            actions.append({"action": "call_hour", "hour": hour, "episode_id": ep_id, "from_party": source})
    return actions


def _hour(value) -> int | None:
    """An hour of day from an episode's entities, or None if it is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
        return None
    return value


def memory_hour(record: dict | None) -> int | None:
    """The hour of day a memory check says to call the party at (its
    call_hour action), or None."""
    for act in (record or {}).get("actions") or []:
        if act.get("action") == "call_hour" and _hour(act.get("hour")) is not None:
            return act["hour"]
    return None


def party_update(record: dict) -> dict:
    """The party fields one memory check writes (with $set): the record as
    parties.memory, and parties.memory_hour when the record has a call_hour.
    The worker and the harness both save a check through this, so the hour
    the contact ladder reads (ladder.after: the second call goes at it)
    comes from the same record the wall shows."""
    hour = memory_hour(record)
    return {"memory": record, **({"memory_hour": hour} if hour is not None else {})}


def check_party(db, embedder, party: dict, *, k: int = 3, mode: str = "auto", now=None) -> dict:
    """One memory check for one party: what the worker saves on parties.memory.

      {"checked_at", "mode": "atlas" | "local" | "off", "hits": [{episode_id,
       party_id, recall_id, score, tags, summary, entities}], "actions": [...],
       "reason"?}

    mode "off" (memory disabled for the recall), no embedder, or a party
    without a profile give an empty "off" record with the reason. So does an
    embedding provider outage: memory only ever adds calls, so its failure
    must not hold up the call this party is owed.
    """
    record = {"checked_at": now or utcnow(), "mode": "off", "hits": [], "actions": []}
    if mode == "off":
        return {**record, "reason": "memory is off for this recall"}
    # Checked here, not inside the guarded call below: a bad mode is a
    # programming error and must raise, not read as a provider outage.
    _require_mode(mode)
    if embedder is None:
        return {**record, "reason": "no embedder configured"}
    if not party.get("profile"):
        return {**record, "reason": "party has no profile"}
    try:
        # Memory is past experience: episodes written during this same recall
        # are left out (a slice party would otherwise inherit "ask for the
        # delivery note" from Harbour Fire's over-count minutes earlier).
        # They are dropped after the search, so ask for a few spare hits.
        hits, how = retrieve(db, embedder, party["profile"], k=k + SAME_RECALL_SPARE, mode=mode)
    except EMBED_ERRORS as exc:
        log.warning("memory check for %s skipped: embedding failed: %s", party.get("party_id"), exc)
        return {**record, "reason": f"embedding failed: {exc}"}
    current = party.get("recall_id")
    hits = [h for h in hits if current is None or h.get("recall_id") != current][:k]
    return {**record, "mode": how, "hits": [_hit_view(h) for h in hits], "actions": policy(hits, party)}


SAME_RECALL_SPARE = 8


def _hit_view(hit: dict) -> dict:
    return {"episode_id": hit["_id"], "party_id": hit.get("party_id"), "recall_id": hit.get("recall_id"),
            "score": hit.get("score"), "tags": list(hit.get("tags") or ()), "summary": hit.get("summary"),
            "entities": hit.get("entities") or {}}


def contacts_to_full_discovery(db, recall_id: str) -> int:
    """Attempts opened before the last party of the recall appeared.

    The memory ablation: without memory a hidden party appears only when a
    sender's claim names it, after several calls; with memory it is
    suspected before the first.
    """
    last = db.parties.find_one({"recall_id": recall_id}, {"created_at": 1}, sort=[("created_at", -1)])
    if last is None or last.get("created_at") is None:
        return 0
    return db.attempts.count_documents({"recall_id": recall_id, "intent_at": {"$lt": last["created_at"]}})


def wait_until_searchable(db, embedder, profile: str, episode_id: str, timeout_s: float = 60, *,
                          k: int = 10, mode: str = "auto", interval_s: float = 1.0) -> bool:
    """Poll retrieve() until `episode_id` is among the hits for `profile`.

    Atlas Vector Search indexes a write a moment after it commits (seed kit
    finding 8), so a run that reads back an episode it just wrote (test 11,
    run 2 straight after run 1) waits here first. k is wider than the
    policy's 3 so that ties with older episodes of the same profile cannot
    hide the new one. On a plain mongod the local path returns it at once.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        hits, how = retrieve(db, embedder, profile, k=k, mode=mode)
        if any(h["_id"] == episode_id for h in hits):
            log.debug("episode %s searchable (%s)", episode_id, how)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval_s, remaining))
