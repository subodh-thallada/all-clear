"""Governed memory (spec "Borrowed from Memory You Can Govern", test 42).

Every episode carries three governance fields beside `valid` and `kind`:

  scope           whose memory it is: a party id ("queen-st-grocer"), a system
                  vendor ("vendor:<name>"), or "fleet" (everyone's). A party's
                  contact habits are scoped to that party, so they are never
                  retrieved for another; complaint and adapter episodes are
                  fleet-wide.
  expires_at      when it stops being usable. Retrieval filters on it, and a
                  TTL index removes expired episodes; the TTL task runs about
                  every 60 seconds, so the query filter is what guarantees an
                  expired memory is never used.
  invalidated_by  what contradicted it (a claim, a finding, a person), set
                  together with `valid: false` by invalidate(). An invalid
                  episode is never retrieved again.

`scope`, `valid`, `expires_at` and `kind` are filter fields of the vector
index (levela.db.schema.vector_index_definition), so `$vectorSearch` applies
the same filter document (admissible()) that the local-scoring fallback
passes to find(): the two paths cannot disagree on what may be used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING

FLEET = "fleet"
VENDOR_PREFIX = "vendor:"
TTL_INDEX = "episodes_ttl_expires_at"

# How long each kind of episode may be used (a run is ten simulated days; a
# contact habit is good for a season, a repair for as long as the vendor
# keeps that API, a failure only for the run it happened in).
TTL = {
    "contact": timedelta(days=180),
    "failure": timedelta(days=14),
    "adapter": timedelta(days=365),
    "complaint": timedelta(days=365),
    "item": timedelta(days=365),
}
DEFAULT_TTL = timedelta(days=90)
# Which kinds are everyone's memory, and which belong to one party.
FLEET_KINDS = frozenset({"adapter", "complaint", "item"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def vendor_scope(vendor: str) -> str:
    return f"{VENDOR_PREFIX}{vendor}"


def default_scope(kind: str, party_id: str | None = None) -> str:
    """Fleet for complaint, adapter and item episodes; the party's own id for
    contact and failure episodes (fleet when there is no party)."""
    if kind in FLEET_KINDS or not party_id:
        return FLEET
    return str(party_id)


def governed(kind: str, *, scope: str | None = None, party_id: str | None = None, now: datetime | None = None,
             ttl: timedelta | None = None, expires_at: datetime | None = None) -> dict:
    """The governance fields a new episode is written with."""
    now = now or utcnow()
    return {"kind": kind, "scope": scope or default_scope(kind, party_id), "valid": True,
            "expires_at": expires_at or now + (ttl or TTL.get(kind, DEFAULT_TTL)), "invalidated_by": None}


def scopes_for(scope: str | list | tuple | None) -> list[str]:
    """The scopes a retrieval may read: the fleet's, plus the caller's own."""
    own = [] if scope is None else ([scope] if isinstance(scope, str) else list(scope))
    return [FLEET, *[s for s in own if s and s != FLEET]]


def admissible(kind: str, *, scope=None, now: datetime | None = None) -> dict:
    """The filter every retrieval applies, on Atlas and locally: valid, of
    this kind, in a readable scope and not expired. Valid as both a
    `$vectorSearch` pre-filter and a find() query."""
    return {"valid": True, "kind": kind, "scope": {"$in": scopes_for(scope)},
            "expires_at": {"$gt": now or utcnow()}}


def invalidate(db, episode_id: str, *, by: str, reason: str | None = None, now: datetime | None = None) -> bool:
    """Mark an episode contradicted by later evidence. True when it was valid
    until now. The episode stays (the history survives); it is never
    retrieved again."""
    res = db.episodes.update_one({"_id": episode_id, "valid": True},
                                 {"$set": {"valid": False, "invalidated_by": by, "invalidated_reason": reason,
                                           "invalidated_at": now or utcnow()}})
    return res.modified_count == 1


def ensure_ttl_index(db) -> str:
    """The TTL index on episodes.expires_at (also in levela.db.schema INDEXES)."""
    return db.episodes.create_index([("expires_at", ASCENDING)], name=TTL_INDEX, expireAfterSeconds=0)
