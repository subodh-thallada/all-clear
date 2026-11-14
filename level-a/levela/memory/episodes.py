"""Episodes: what happened on one contact attempt, kept for the next recall.

When an attempt reaches a final state the worker calls
write_episode_for_attempt(). The episode keeps the party's profile, a one-
or two-sentence summary, tags and entities, and an embedding of the profile.
Retrieval is by profile similarity, so before the first call of the next
recall the worker finds what happened at this party, and at parties like it.

Only two tags change behaviour (memory.retrieve.policy): `forwards_to` (this
party passes stock on to a named party) and `over_reported` (its first count
exceeded what was shipped). The others describe the call for people reading
the wall or the episode.

One entity changes behaviour too: `answered_hour`, the simulated hour of day
(0-23) a call the party picked up was placed at. The next recall's memory
check turns the party's own answered hour into parties.memory_hour, and the
contact ladder puts that party's second call at that hour (spec "Contact
ladder" step 2; the no-answer profile's "memory of good hours").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

from .. import feed
from ..clock import hour_of_day
from ..config import FINAL_ATTEMPT_STATES
from ..ledger.claims import ClaimError, counts
from ..ledger.transfers import resolve_party
from .embed import EMBED_ERRORS

log = logging.getLogger(__name__)

# Every tag an episode can carry, in the order they are written.
TAGS = ("answered", "no_answer", "failed", "abandoned", "counts_matched", "over_reported", "forwards_to",
        "received_transfer", "disputed", "panels_returned", "awaiting_fix", "fixed", "needs_human")
# The only two tags the memory policy acts on (memory.retrieve.policy).
ACTIONABLE_TAGS = frozenset({"forwards_to", "over_reported"})
_STATE_TAG = {"completed": "answered", "no_answer": "no_answer", "failed": "failed", "abandoned": "abandoned"}
_BUCKET_WORDS = (("in_stock", "in stock"), ("installed", "installed"), ("fixed", "fixed"), ("returned", "returned"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def episode_id(attempt_id: str) -> str:
    """One episode per attempt: the id is derived, so a second write collides."""
    return f"ep_{attempt_id}"


# ------------------------------------------------------------------ summary

def summarize(attempt: dict, claims: list, party: dict, edges: list, *, directory=None,
              party_names: dict | None = None) -> tuple[str, list[str], dict]:
    """(summary, tags, entities) for one finished attempt. Pure.

    claims: this attempt's claims in the order they were made.
    edges: the recall's transfers; only those touching this party are used.
    directory: the recall's directory, so a party spoken as "the corner properties"
        is remembered by its directory name, the name a later recall resolves.
    party_names: party_id -> name for the parties on those edges.
    """
    state = attempt.get("state")
    if state not in _STATE_TAG:
        raise ValueError(f"attempt {attempt.get('_id')} is {state!r}, not in a final state")
    sid = party["party_id"]
    names = _Names(directory, party_names)
    inbound = [e for e in edges if e.get("to_party") == sid]
    outbound = [e for e in edges if e.get("from_party") == sid]
    accepted = [c for c in claims if c.get("status") == "accepted"]
    rejected = [c for c in claims if c.get("status") == "rejected"]
    final = _quantities(accepted[-1]["raw"]) if accepted else None
    sender = names.of(inbound[0]["from_party"]) if inbound else None

    tags, entities = {_STATE_TAG[state]}, {}
    if accepted and not rejected:
        tags.add("counts_matched")
    if any(c.get("code") == "over_count" for c in rejected):
        tags.add("over_reported")
    if final is not None:
        partner = names.canonical(accepted[-1]["raw"].get("forwarded_to_name"))
        if final["forwarded_qty"] > 0 and partner:
            tags.add("forwards_to")
            entities["forwards_to"] = partner
        if (final["received_qty"] or 0) > 0:
            tags.add("received_transfer")
            if sender:
                entities["received_from"] = sender
        if final["returned"] > 0:
            tags.add("panels_returned")
        if final["installed"] > 0:
            tags.add("awaiting_fix")
        if final["fixed"] > 0:
            tags.add("fixed")
    if any(e.get("state") == "disputed" for e in inbound + outbound):
        tags.add("disputed")
    if party.get("escalation") == "human":
        tags.add("needs_human")
    hour = answered_hour(attempt)
    if hour is not None:
        entities["answered_hour"] = hour

    first = (_counts_sentence(accepted, rejected, names, sender) if claims
             else _outcome_sentence(state, attempt))
    notes = _dispute_notes(inbound, outbound, names)
    if "needs_human" in tags:
        notes.append("handed to a person")
    summary = first if not notes else f"{first} {_capitalise('; '.join(notes))}."
    return summary, [t for t in TAGS if t in tags], entities


def answered_hour(attempt: dict) -> int | None:
    """The simulated hour of day (0-23) a call the party picked up was placed
    at, or None: the party did not pick up (only a `completed` call was
    answered), it was a form submission (not a phone hour), or the attempt
    carries no simulated time (`intent_sim`, which the harness writes on
    every call it places; older attempts and the seeded history have none)."""
    if attempt.get("state") != "completed" or attempt.get("channel") == "form":
        return None
    at = attempt.get("intent_sim")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return int(hour_of_day(float(at))) % 24


class _Names:
    """Party names for sentences: the recall's own parties first, then the directory."""

    def __init__(self, directory, party_names):
        self.directory = directory or []
        self.party_names = party_names or {}

    def of(self, party_id: str) -> str:
        if party_id in self.party_names:
            return self.party_names[party_id]
        entry = next((d for d in self.directory if d.get("party_id") == party_id), None)
        return entry["name"] if entry else party_id

    def canonical(self, spoken) -> str:
        """A spoken party name as the directory (or the stub it created) names it."""
        spoken = (spoken or "").strip()
        if not spoken:
            return ""
        party_id, entry = resolve_party(spoken, self.directory)
        return entry["name"] if entry else self.party_names.get(party_id, spoken)


def _quantities(raw: dict) -> dict | None:
    """The claim's numbers as the ledger read them; None if they never parsed."""
    try:
        return counts(raw or {})
    except ClaimError:
        return None


def _describe(raw: dict, names: _Names, sender: str | None) -> str:
    """'25 received from Queen St Alarm and 25 on hand', '40 forwarded to Corner Properties'."""
    q = _quantities(raw)
    if q is None:
        return "counts that could not be read"
    parts = []
    if q["received_qty"] is not None:
        parts.append(f"{q['received_qty']} received" + (f" from {sender}" if sender else ""))
    parts += [f"{q[key]} {words}" for key, words in _BUCKET_WORDS if q[key]]
    if q["forwarded_qty"]:
        partner = names.canonical(raw.get("forwarded_to_name"))
        parts.append(f"{q['forwarded_qty']} forwarded" + (f" to {partner}" if partner else ""))
    return _join(parts) if parts else "no panels held"


def _counts_sentence(accepted, rejected, names: _Names, sender) -> str:
    first_bad = rejected[0] if rejected else None
    if accepted:
        booked = _describe(accepted[-1]["raw"], names, sender)
        if first_bad is None:
            return f"Reported {booked}."
        if first_bad.get("code") == "over_count":
            return (f"First answer was {_describe(first_bad['raw'], names, sender)}{_against(first_bad)}; "
                    f"corrected to {booked}.")
        return f"First answer was refused ({_reason(first_bad)}); then reported {booked}."
    if first_bad.get("code") == "over_count":
        return (f"Reported {_describe(first_bad['raw'], names, sender)}{_against(first_bad)} "
                "and never corrected it; nothing was booked.")
    return f"Counts were refused ({_reason(first_bad)}); nothing was booked."


def _outcome_sentence(state: str, attempt: dict) -> str:
    """What happened on an attempt that produced no claim at all."""
    if state == "no_answer":
        seconds = _ringing_seconds(attempt)
        return f"No answer after {seconds} s of ringing." if seconds else "No answer."
    if state == "failed":
        reason = attempt.get("outcome_reason")
        return f"The call failed ({reason})." if reason else "The call failed."
    if state == "abandoned":
        return "Abandoned: no evidence the call was ever placed."
    return "Answered, but no counts were given."


def _dispute_notes(inbound, outbound, names: _Names) -> list[str]:
    notes = [f"{names.of(e['from_party'])} reported sending {e['sender_qty']}, "
             f"so {e['disputed_qty']} {_are(e['disputed_qty'])} disputed"
             for e in inbound if e.get("state") == "disputed"]
    notes += [f"{names.of(e['to_party'])} reported receiving {e['recipient_qty']}, "
              f"so {e['disputed_qty']} {_are(e['disputed_qty'])} disputed"
              for e in outbound if e.get("state") == "disputed"]
    return notes


def _ringing_seconds(attempt: dict) -> int | None:
    """Seconds from the dial to the no-answer report; None when not measurable."""
    start, end = attempt.get("dialed_at") or attempt.get("intent_at"), attempt.get("ended_at")
    if start is None or end is None:
        return None
    seconds = round((end - start).total_seconds())
    return seconds if seconds > 0 else None


def _against(claim: dict) -> str:
    shipped = claim.get("shipped_in")
    return f" against {shipped} shipped" if shipped is not None else ""


def _reason(claim: dict) -> str:
    return claim.get("reject_reason") or claim.get("code") or "rejected"


def _are(n) -> str:
    return "is" if n == 1 else "are"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


# -------------------------------------------------------------------- write

def write_episode_for_attempt(db, embedder, attempt: dict, now=None) -> dict | None:
    """Write the episode for a finished attempt. Returns the new document, or
    None when nothing was written: the attempt is gone or still live, the
    episode exists already (the worker may call this twice, from two passes
    or two workers), the party has no profile to embed, or the embedder is
    unreachable (the worker's next pass tries again).

    Sets attempts.episode_id to the episode's id, or to None when the party
    has no profile, so a worker selecting final attempts without an
    `episode_id` field sees each attempt once.
    """
    att = db.attempts.find_one({"_id": attempt["_id"]})
    if att is None or att.get("state") not in FINAL_ATTEMPT_STATES:
        return None
    ep_id = episode_id(att["_id"])
    if db.episodes.find_one({"_id": ep_id}, {"_id": 1}) is not None:
        _link(db, att["_id"], ep_id)
        return None
    rid, sid = att["recall_id"], att["party_id"]
    party = db.parties.find_one({"recall_id": rid, "party_id": sid})
    if party is None:
        log.warning("episode for %s skipped: party %s is not in %s", att["_id"], sid, rid)
        return None
    if not party.get("profile"):
        db.attempts.update_one({"_id": att["_id"], "episode_id": {"$exists": False}},
                               {"$set": {"episode_id": None}})
        return None

    claims = list(db.claims.find({"attempt_id": att["_id"]}).sort([("created_at", 1), ("_id", 1)]))
    edges = list(db.edges.find({"recall_id": rid, "$or": [{"from_party": sid}, {"to_party": sid}]}))
    recall = db.recalls.find_one({"_id": rid}, {"directory": 1}) or {}
    others = sorted({e["from_party"] for e in edges} | {e["to_party"] for e in edges})
    names = {s["party_id"]: s["name"]
             for s in db.parties.find({"recall_id": rid, "party_id": {"$in": others}}, {"party_id": 1, "name": 1})}
    summary, tags, entities = summarize(att, claims, party, edges, directory=recall.get("directory"),
                                        party_names=names)
    try:
        vector = embedder.embed([party["profile"]])[0]
    except EMBED_ERRORS as exc:
        log.warning("episode for %s not written yet: embedding failed: %s", att["_id"], exc)
        return None

    doc = {
        "_id": ep_id, "recall_id": rid, "party_id": sid, "attempt_id": att["_id"],
        "party_profile": party["profile"], "summary": summary, "tags": tags, "entities": entities,
        "valid": True, "kind": "contact", "embedding": vector, "embedding_model": embedder.name, "created_at": now or utcnow(),
    }
    try:
        db.episodes.insert_one(doc)
    except DuplicateKeyError:
        _link(db, att["_id"], ep_id)
        return None
    _link(db, att["_id"], ep_id)
    _announce(db, party, doc)
    return doc


def _link(db, attempt_id: str, ep_id: str) -> None:
    db.attempts.update_one({"_id": attempt_id}, {"$set": {"episode_id": ep_id}})


def _announce(db, party: dict, doc: dict) -> None:
    """Tell the room when an episode will change what the next recall does."""
    phrases = []
    if "forwards_to" in doc["tags"]:
        phrases.append(f"passes stock on to {doc['entities']['forwards_to']}")
    if "over_reported" in doc["tags"]:
        phrases.append("over-reported its first count")
    if not phrases:
        return
    feed.emit(db, "memory_note", f"Remembered for next time: {party['name']} {' and '.join(phrases)}",
              recall_id=doc["recall_id"], party_id=doc["party_id"], attempt_id=doc["attempt_id"],
              source="worker", detail={"episode_id": doc["_id"], "summary": doc["summary"],
                                       "tags": [t for t in doc["tags"] if t in ACTIONABLE_TAGS]})
