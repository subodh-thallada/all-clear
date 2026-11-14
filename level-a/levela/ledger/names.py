"""Matching a spoken name to a party already in the run, before a stub is created.

A party named on a call ("the Corner Mart people", "Korner Mart") must
resolve to the party that already exists, or the graph gets the same store
twice. match_party() is the one place that decides, so the claim path and
the store-records link (a connector's store label to its party) agree:

  1  the recall's directory: a name or alias, normalised      (exact)
  2  the recall's parties: Atlas Search fuzzy on `name`      (when the cluster has it)
  3  the recall's parties: the normalised name               (everywhere)
  4  otherwise the slug of the spoken name: a new party

Atlas Search needs a search process and the PARTY_NAME_INDEX search index
(levela.db.schema.ensure_search_indexes). Where either is missing $search
raises or finds nothing, and the normalised exact match decides. $search is
refused inside a transaction, so names are matched before the claim's
transaction opens; the ids it returns are deterministic, so a retry of the
transaction books against the same parties.
"""
from __future__ import annotations

import logging
import re

from pymongo.errors import OperationFailure

from ..db.schema import PARTY_NAME_INDEX
from .transfers import resolve_party

log = logging.getLogger(__name__)

_FILLER = {"the", "a", "an"}
FUZZY_MAX_EDITS = 1
# A fuzzy hit must score at least this (Atlas's relevance score) to count.
FUZZY_MIN_SCORE = 1.0


def normalise(name: str) -> str:
    """Lower case, punctuation as spaces, a leading article dropped:
    "The Corner-Mart" -> "corner mart"."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    while words and words[0] in _FILLER:
        words = words[1:]
    return " ".join(words)


def _fuzzy(db, recall_id: str, name: str) -> str | None:
    """The party id Atlas Search ranks first for `name` in this recall, or
    None (no search process, no index, or no hit good enough)."""
    try:
        hits = list(db.parties.aggregate([
            {"$search": {"index": PARTY_NAME_INDEX, "compound": {
                "must": [{"text": {"query": name, "path": "name",
                                   "fuzzy": {"maxEdits": FUZZY_MAX_EDITS}}}],
                "filter": [{"equals": {"path": "recall_id", "value": recall_id}}]}}},
            {"$limit": 1},
            {"$project": {"party_id": 1, "score": {"$meta": "searchScore"}}},
        ]))
    except OperationFailure as exc:
        log.debug("$search unavailable (code %s): matching names exactly", exc.code)
        return None
    if hits and hits[0].get("score", 0) >= FUZZY_MIN_SCORE:
        return hits[0]["party_id"]
    return None


def _exact(db, recall_id: str, name: str) -> str | None:
    key = normalise(name)
    if not key:
        return None
    for row in db.parties.find({"recall_id": recall_id}, {"party_id": 1, "name": 1}):
        if normalise(row.get("name", "")) == key:
            return row["party_id"]
    return None


def match_party(db, recall_id: str, name: str, directory=None) -> tuple[str, dict | None]:
    """Spoken name -> (party_id, directory entry or None). Never inside a
    transaction (see the module docstring)."""
    for entry in directory or []:
        if normalise(name) in {normalise(n) for n in [entry["name"], *entry.get("aliases", [])]}:
            return entry["party_id"], entry
    found = _fuzzy(db, recall_id, name) or _exact(db, recall_id, name)
    if found is not None:
        entry = next((e for e in directory or [] if e.get("party_id") == found), None)
        return found, entry
    return resolve_party(name, directory)
