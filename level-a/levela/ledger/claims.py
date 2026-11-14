"""submit_claim(): the one path every claim takes (voice, simulator, form,
order system, rep).

The party update, the edges, the stub parties, the claim and the recall's
open_units commit together in one transaction or not at all. Atlas decides:
the party validator refuses any count that breaks conservation, any `fixed`
the evidence does not allow (see levela.db.schema), and a model that is not
the recalled one; the transaction aborts, and the claim is then recorded as
rejected with the two values that disagree and the raw validation error.

A claim carries the six buckets' counts (in_stock, installed, fixed,
returned; forwarded and unresolved are derived) and, from a building owner,
the three checks: `model_seen` (the model printed on the front),
`version_seen` (the software version on the display) and `sounder_test`
(did the sounder ring when the test button was pressed). A check the claim
does not carry keeps its earlier answer, so a later count cannot erase the
evidence a fixed count rests on.

A claim may forward to several parties (V2 §9): `forwards: [{to_name |
to_party_id, qty}]`, or the single `forwarded_qty` + `forwarded_to_name` pair,
which is one entry. One edge per destination and `forwarded` is their sum. A
later claim may change quantities and add parties. It may also drop a party (or
give it 0) while that party has not counted the transfer: claims are absolute,
so a sender that named the wrong party, or invented a transfer, can take it
back. The edge is deleted in the same transaction, and so is the stub party
this sender's claim created for it when nothing else refers to it. Once the
recipient has counted the transfer, its count is booked against the edge,
and dropping the party is refused (`forward_changed`, or `forward_restate`
for a claim with no transfer at all).

A receipt (`received_qty`) settles the recipient's inbound edge. A party that
several senders shipped to names the sender (`received_from`, a party name or
party id); without it the receipt is refused (`receipt_sender_ambiguous`)
rather than booked against an arbitrary edge. The recipient's shipped_in is
its own shipment plus every inbound edge's corroborated quantity.

A claim that leaves panels installed and not fixed sets the party's fix
check, `fix_check_at`, FIX_CHECK_H simulated hours later: the three checks
are asked again then (levela.harness.brief). A claim with nothing installed
clears it.

When a claim changes an edge the other party must hear about (a receipt, a
dispute, a corroboration, a new transfer into a party that already exists),
that party's `next_action_at` is set to the recall's simulated now inside the
same transaction, so the engine's due query wakes it with no event to deliver.

Every accepted claim has an evidence event in tool_events (invariant 3).
Calls (voice, sim) are logged by POST /tools/submit_count before it books;
the form, order system and rep channels are logged here, first, the same way.

A repeated delivery carries the same dedupe_key (attempt id plus a hash of
the numbers, the sorted destinations and the named sender), so it cannot post
twice. Going back on one call to numbers already booked (A, then B, then A)
is a correction, not a repeat, and is booked again (_booking_key).

A count is a whole number of panels no larger than MAX_COUNT; anything bigger
is refused as bad_number, and the raw body is made storable first (storable),
so an absurd number is a rejection, not a crash.

Names a claim speaks (the party panels went to, the sender of a receipt) are
matched before the transaction opens (levela.ledger.names.match_party):
Atlas Search cannot run inside one.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pymongo import DESCENDING, UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, WriteError

from ..clock import get_clock, server_now
from ..db.client import run_txn
from ..runs import run_id
from .names import match_party
from .reconciler import VALIDATION_FAILED, atlas_error, open_units
from .transfers import (_SUM, _norm, open_edges, product_fields, rebook_recipient, received_total, settle_edge,
                        stub_party, upsert_stubs)

# The buckets a claim states; forwarded is the sum of its transfers and
# unresolved is what is left of shipped_in.
BUCKETS = ("in_stock", "installed", "fixed", "returned")
NUMBER_FIELDS = (*BUCKETS, "forwarded_qty", "received_qty")
# A claim that leaves panels installed is checked again this much later.
FIX_CHECK_H = 48.0
# The three checks a building owner answers (spec "Checks").
CHECK_FIELDS = ("model_seen", "version_seen", "sounder_test")
_VERSION = re.compile(r"^\d+(\.\d+)*$")
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}
# The largest count a claim may carry, in panels. A batch is thousands of panels;
# a bigger number is a mishearing or a typo. It also keeps every count far
# inside BSON's 64-bit integers: a 20-digit count made the encoder raise
# OverflowError inside the claim transaction, a 500 instead of a rejection.
MAX_COUNT = 10**9
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
SOURCES = ("voice", "sim", "form", "order_system", "rep")
# The tool_events kind that is a claim's evidence, by source (V2 §9).
EVIDENCE_KIND = {"voice": "tool_call", "sim": "tool_call", "form": "form", "order_system": "order_system", "rep": "rep"}
EVIDENCE_KINDS = tuple(sorted(set(EVIDENCE_KIND.values())))
# Channels with no webhook of their own: the claim path logs their evidence.
LOGGED_HERE = ("form", "order_system", "rep")
# How a party named by a claim entered the run (parties.discovered_by prefix).
DISCOVERED_VIA = {"order_system": "order_system", "rep": "rep"}

# Rejection codes. A challenge is a rejection the agent should read back to
# the party and ask again; it counts toward the per-attempt challenge cap.
CHALLENGE_CODES = {"over_count", "negative", "not_integer", "delivery_note_mismatch",
                   "recipient_overdocumented", "model_mismatch", "fixed_needs_evidence", "version_affected",
                   "sounder_not_confirmed", "panel_not_fixed"}


class ClaimError(Exception):
    """A claim the ledger cannot book for a reason other than conservation."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


class _Duplicate(Exception):
    pass


@dataclass
class _Booking:
    """What one run of the claim transaction did (rebuilt on every retry)."""
    stage: str = "party"            # which write Atlas refused, for the rejection code
    shipped_in: int | None = None
    party: dict = field(default_factory=dict)       # the party as it was before this claim
    discovered: list = field(default_factory=list)
    removed: list = field(default_factory=list)     # stub parties deleted with a withdrawn transfer
    edges: list = field(default_factory=list)       # edges opened, changed or withdrawn, each with "before"
    touched: set = field(default_factory=set)       # parties whose state may change
    wake: set = field(default_factory=set)          # counterparts the engine must act on
    open_units: int | None = None
    claim_id: object = None

    def note_edge(self, before: dict | None, after: dict, *, counterpart: str, wake: bool) -> None:
        self.edges.append({**_edge_view(after), "before": before["state"] if before else None})
        self.touched.add(counterpart)
        if wake:
            self.wake.add(counterpart)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sim_now(client, db, recall_id) -> float:
    """The recall's simulated time by the database's clock (V2 §2): the clock
    the engine's due query compares next_action_at with. A local clock a
    second off would be twenty simulated hours off on the test clock."""
    return get_clock(db, recall_id).sim_now(server_now(client))


# ---- parsing ------------------------------------------------------------------

def storable(value):
    """`value` with every integer BSON cannot hold (outside 64 bits) turned
    into its decimal string, through dicts and lists. A raw body is logged
    and kept as evidence before it is parsed, so even an absurd number must
    be storable; parsing the string then refuses it as bad_number."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if INT64_MIN <= value <= INT64_MAX else str(value)
    if isinstance(value, dict):
        return {k: storable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [storable(v) for v in value]
    return value


def _count(n: int, given) -> int:
    if abs(n) > MAX_COUNT:
        raise ClaimError("bad_number", f"{given!r} is not a count")
    return n


def _num(v):
    """Coerce a webhook value to a number. Integral floats and numeric strings
    become ints (refused as bad_number beyond MAX_COUNT); anything
    non-integral is passed through so Atlas refuses it."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ClaimError("bad_number", f"{v!r} is not a count")
    if isinstance(v, int):
        return _count(v, v)
    if isinstance(v, float):
        return _count(int(v), v) if v.is_integer() else v
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        try:
            f = float(s)
        except ValueError:
            raise ClaimError("bad_number", f"{v!r} is not a count") from None
        return _count(int(f), v) if f.is_integer() else f
    raise ClaimError("bad_number", f"{v!r} is not a count")


def _text(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _destination(party_id, name) -> dict:
    out = {}
    if _text(party_id):
        out["to_party_id"] = _text(party_id)
    if _text(name):
        out["to_name"] = _text(name)
    return out


def forwards(raw: dict) -> list[dict]:
    """The claim's transfers, parsed but not yet resolved to parties:
    [{"qty", "to_party_id"?, "to_name"?}]. A zero quantity is not a
    destination. Negative and fractional quantities pass through, so Atlas
    refuses them like any other bucket. Raises ClaimError (bad_number) when a
    number cannot be read or `forwarded_qty` contradicts the list."""
    listed = raw.get("forwards")
    single = _num(raw.get("forwarded_qty"))
    if not listed:
        return [{"qty": single, **_destination(None, raw.get("forwarded_to_name"))}] if single else []
    if not isinstance(listed, list) or not all(isinstance(f, dict) for f in listed):
        raise ClaimError("bad_number", "forwards must be a list of {to_name or to_party_id, qty}")
    out = []
    for item in listed:
        qty = _num(item.get("qty"))
        dest = _destination(item.get("to_party_id"), item.get("to_name"))
        if qty is None:
            who = dest.get("to_name") or dest.get("to_party_id") or "a party"
            raise ClaimError("bad_number", f"no count was given for the panels sent to {who}")
        if qty != 0:
            out.append({"qty": qty, **dest})
    total = sum(f["qty"] for f in out)
    # An optional forwarded_qty filled with 0 next to the list means "not given".
    if single and single != total:
        raise ClaimError("bad_number", f"forwarded_qty {single} does not match the parties listed ({total})")
    return out


def counts(raw: dict) -> dict:
    """The claim's numbers: the four buckets, forwarded_qty (the sum of every
    transfer) and received_qty (None when the claim is not a receipt)."""
    out = {k: (_num(raw.get(k)) or 0) for k in BUCKETS}
    out["forwarded_qty"] = sum(f["qty"] for f in forwards(raw))
    out["received_qty"] = _num(raw.get("received_qty"))
    return out


def _model(v) -> str:
    if not _text(v):
        raise ClaimError("bad_answer", f"{v!r} is not a model name")
    return " ".join(v.split()).upper()


def _version(v) -> str:
    """Version digits as spoken or typed: 2.4, "2.4", "v2.4", "version 2 . 4"."""
    if isinstance(v, bool):
        raise ClaimError("bad_answer", f"{v!r} is not a software version")
    text = str(v).strip().lower()
    for prefix in ("version", "v"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = re.sub(r"\s+", "", text)
    if not _VERSION.match(text):
        raise ClaimError("bad_answer", f"{v!r} is not a software version")
    return text


def _yes_no(v) -> bool:
    if isinstance(v, bool):
        return v
    word = str(v).strip().lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    raise ClaimError("bad_answer", f"{v!r} is not yes or no")


def checks(raw: dict) -> dict:
    """The check answers the claim carries, normalised: model_seen upper
    case, version_seen as digits ("2.4"), sounder_test a bool. A check the
    claim does not carry is absent (it keeps its earlier answer). Raises
    ClaimError (bad_answer) for an answer that is not one of the closed forms."""
    out = {}
    if raw.get("model_seen") is not None:
        out["model_seen"] = _model(raw["model_seen"])
    if raw.get("version_seen") is not None:
        out["version_seen"] = _version(raw["version_seen"])
    if raw.get("sounder_test") is not None:
        out["sounder_test"] = _yes_no(raw["sounder_test"])
    return out


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _destination_key(f: dict) -> str:
    return f"id:{f['to_party_id']}" if "to_party_id" in f else f"name:{_norm(f.get('to_name', ''))}"


def dedupe_key(attempt_id: str, raw: dict) -> str:
    """Attempt id plus a hash of the numbers, the destinations (sorted, so
    the order a party list was spoken in does not matter), the named sender of
    a receipt and the delivery note when given: a repeated delivery cannot
    post twice, but a corrected answer on the same call is a new claim, and so
    is the same count received from a different sender."""
    try:
        canon = counts(raw)
        canon["forwards"] = sorted([_destination_key(f), f["qty"]] for f in forwards(raw))
        canon.update(checks(raw))
    except ClaimError:
        canon = {k: str(raw.get(k)) for k in (*NUMBER_FIELDS, *CHECK_FIELDS, "forwarded_to_name", "forwards")}
    if _text(raw.get("received_from")):
        canon["received_from"] = _norm(raw["received_from"])
    if raw.get("delivery_note"):
        canon["delivery_note"] = _digits(raw["delivery_note"])
    digest = hashlib.sha1(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return f"{attempt_id}:{digest}"


def speakable(result: dict) -> str:
    """The sentence the voice agent should base its next turn on."""
    if result.get("accepted"):
        if result.get("duplicate"):
            return "Already recorded. Do not ask again."
        return "Recorded. Thank them and move on."
    code = result.get("code")
    if code == "over_count":
        return (f"Rejected by the database: those counts add up to {result['reported_total']} panels, "
                f"but our shipment record shows {result['shipped_in']} panels were delivered to this party. "
                "Say both numbers and ask them to check again.")
    if code == "model_mismatch":
        return (f"Rejected by the database: {result.get('reason')}. Ask them to read the model printed on the "
                "front of the panel again.")
    if code in ("fixed_needs_evidence", "version_affected", "sounder_not_confirmed"):
        return (f"Rejected by the database: {result.get('reason')}. Panels count as fixed only when the display "
                "shows a version that is not affected and the sounder rang on the test. Ask them to read the "
                "version on the display and to press the test button again; if it is not fixed, the panels are "
                "installed, not fixed. Never explain how to update a panel: the installer will arrange it.")
    if code == "panel_not_fixed":
        return (f"Rejected by the database: {result.get('reason')}. A connected panel at this site has not passed "
                "its own test, so it cannot be counted as fixed. Ask them to count it as installed.")
    if code == "delivery_note_required":
        return "Before taking counts, ask for the delivery note number on their paperwork, then submit again with it."
    if code == "delivery_note_mismatch":
        return "That delivery note does not match our shipment record. Ask them to read it again, digit by digit."
    if code == "no_inbound_edge":
        return ("No sender has reported a transfer to this party yet, so the receipt cannot be booked. "
                "Thank them, say we will call back shortly, and end the call.")
    if code == "forward_missing_name":
        return ("Ask the name of the building owner or installer the panels went to, then submit again with "
                "that name.")
    if code == "bad_answer":
        return f"Not recorded: {result.get('reason')}. Read the question back and ask again."
    if code in ("forward_changed", "forward_restate"):
        # A stop code (api/tools.py): the receiving party has already counted
        # those panels, so asking again cannot settle it on this call.
        return (f"Rejected: {result.get('reason')}. The party that received them has already counted them, so "
                "the recall team will sort it out with both parties. Thank them, say a person will follow up, "
                "and end the call.")
    if code == "receipt_sender_ambiguous":
        return (f"Rejected: {result.get('reason')}. The recall team will confirm with each party that sent panels. "
                "Thank them, say a person will follow up, and end the call.")
    if code == "recall_closed":
        return "This recall is already closed. Thank them and end the call."
    return f"Rejected: {result.get('reason')}. Ask them to check the numbers again."


# ---- the claim transaction ------------------------------------------------------

def submit_claim(client, db, *, recall_id, party_id, attempt_id, source, raw, now=None,
                 directory=None, claim_id=None, require_delivery_note=False,
                 fail_after_party_update=False) -> dict:
    """Book one claim. Returns a result dict:

      accepted, claim_id, message always; duplicate? on a repeated delivery;
      on reject code, reason, reported_total?, shipped_in?, atlas_error?;
      on accept open_units, txn_runs, and when the claim changed the graph
      discovered (first new party id) + discovered_parties (all of them),
      edge (first edge opened, changed or withdrawn) + edges (all of them,
      each with "before": the state it had, None when new; a withdrawn one
      has state "withdrawn", to_name and removed), removed_parties (stubs
      deleted with a withdrawn transfer) and woken (the counterparts made due
      now; one already reconciled with nothing to ask is left alone).
    """
    if source not in SOURCES:
        raise ValueError(f"unknown claim source {source!r} (one of {', '.join(SOURCES)})")
    now = now or utcnow()
    raw = storable({k: v for k, v in dict(raw).items() if v is not None})
    base = dedupe_key(attempt_id, raw)
    prior, key = _booking_key(db, base, attempt_id)
    if prior is not None:
        return _reply(prior, duplicate=True)

    doc = {"recall_id": recall_id, "party_id": party_id, "attempt_id": attempt_id,
           "source": source, "raw": raw, "dedupe_key": key, "dedupe_base": base, "created_at": now}
    if claim_id:
        doc["_id"] = claim_id
    if source in LOGGED_HERE:
        _log_evidence(db, doc)
    try:
        c, entries = counts(raw), forwards(raw)
    except ClaimError as exc:
        return _reject(db, doc, key, recall_id, party_id, exc.code, exc.reason)
    try:
        ck = checks(raw)
    except ClaimError as exc:
        return _reject(db, doc, key, recall_id, party_id, exc.code, exc.reason)
    sender = _text(raw.get("received_from"))
    dirs = directory if directory is not None else _directory(db, recall_id)
    names = _match_names(db, recall_id, entries, sender, dirs)

    sim_now = functools.cache(lambda: _sim_now(client, db, recall_id))
    holder: dict = {}
    counter: dict = {}

    def txn(session):
        b = holder["b"] = _Booking()
        if db.claims.find_one({"dedupe_key": key}, {"_id": 1}, session=session) is not None:
            raise _Duplicate()
        _open_recall(db, recall_id, session)
        party = db.parties.find_one({"recall_id": recall_id, "party_id": party_id}, session=session)
        if party is None:
            raise ClaimError("unknown_party", f"unknown party {party_id} in {recall_id}")
        b.party = party
        if require_delivery_note and party.get("tier") == 1:
            _check_delivery_note(db, recall_id, party_id, raw.get("delivery_note"), session)
        b.touched.add(party_id)
        shipped_in = party["shipped_in"]
        if c["received_qty"] is not None:
            shipped_in = _book_receipt(db, b, recall_id, party_id, c["received_qty"], sender, names, now, session)
        b.stage, b.shipped_in = "party", shipped_in
        _book_counts(db, party, c, ck, shipped_in, now, session,
                     fix_check_at=round(sim_now() + FIX_CHECK_H, 4) if c["installed"] > 0 else None)
        if fail_after_party_update:
            raise RuntimeError("failpoint: after party update")
        _book_forwards(db, b, party, entries, names, dirs, source=source, sim_now=sim_now, now=now, session=session)
        idle = _refresh_states(db, recall_id, b.touched, session)
        b.wake -= idle | {party_id}
        if b.wake:
            _wake(db, recall_id, b.wake, sim_now(), session)
        claim = {**doc, "status": "accepted", "reject_reason": None, "code": None}
        db.claims.insert_one(claim, session=session)
        b.claim_id = claim["_id"]
        b.open_units = open_units(db, recall_id, session)
        db.recalls.update_one({"_id": recall_id}, {"$set": {"open_units": b.open_units, "updated_at": now}},
                              session=session)

    try:
        run_txn(client, txn, counter=counter)
    except _Duplicate:
        return _reply(db.claims.find_one({"dedupe_key": key}), duplicate=True)
    except DuplicateKeyError:
        # The same delivery twice at once: both passed the lookup, one
        # committed, and the unique dedupe_key stopped the other.
        found = db.claims.find_one({"dedupe_key": key})
        if found is None:
            raise
        return _reply(found, duplicate=True)
    except WriteError as exc:
        if exc.code != VALIDATION_FAILED:
            raise
        code, reason, numbers = _refusal(c, ck, entries, holder["b"])
        return _reject(db, doc, key, recall_id, party_id, code, reason, numbers, err=atlas_error(exc),
                       runs=counter.get("runs"))
    except ClaimError as exc:
        return _reject(db, doc, key, recall_id, party_id, exc.code, exc.reason, runs=counter.get("runs"))
    return _accepted(holder["b"], counter.get("runs", 1))


def _booking_key(db, base: str, attempt_id: str) -> tuple[dict | None, str]:
    """(the claim this delivery repeats, or None; the dedupe key to book under).

    A delivery whose key is new is booked under it. One whose key was refused
    is a repeat of that refusal. One whose key was accepted is a repeat only
    if that is still the attempt's latest accepted claim: claims are
    absolute, so going back on one call to numbers already given (A, then B,
    then A) is a correction, and returning the first A as a duplicate left
    the ledger on B. Such a correction is booked under the key plus the
    number of claims the attempt has accepted, which a redelivery of the same
    correction computes again (and then finds, or is stopped by the unique
    index on dedupe_key)."""
    prior = db.claims.find_one({"dedupe_key": base})
    if prior is None:
        return None, base
    if prior["status"] != "accepted":
        return prior, base
    latest = db.claims.find_one({"attempt_id": attempt_id, "status": "accepted"},
                                sort=[("created_at", DESCENDING), ("_id", DESCENDING)])
    if latest is None or latest.get("dedupe_base", latest["dedupe_key"]) == base:
        return latest or prior, base
    n = db.claims.count_documents({"attempt_id": attempt_id, "status": "accepted"})
    return None, f"{base}#{n}"


def _directory(db, recall_id: str) -> list:
    return (db.recalls.find_one({"_id": recall_id}, {"directory": 1}) or {}).get("directory") or []


def _match_names(db, recall_id: str, entries: list, sender: str | None, dirs: list) -> dict:
    """{spoken name: (party_id, directory entry or None)} for every name the
    claim speaks, matched outside the transaction (levela.ledger.names)."""
    spoken = [f["to_name"] for f in entries if "to_party_id" not in f and "to_name" in f]
    if sender:
        spoken.append(sender)
    return {name: match_party(db, recall_id, name, dirs) for name in dict.fromkeys(spoken)}


def _log_evidence(db, claim: dict) -> None:
    """The delivery as received, logged before booking exactly as a tool
    call is. Keyed by the dedupe key, so a repeated delivery logs once."""
    db.tool_events.update_one({"_id": f"ev:{claim['dedupe_key']}"}, {"$setOnInsert": {
        "kind": EVIDENCE_KIND[claim["source"]], "source": claim["source"], "attempt_id": claim["attempt_id"],
        "party_id": claim["party_id"], "recall_id": claim["recall_id"], "payload": claim["raw"],
        "received_at": claim["created_at"]}}, upsert=True)


def _open_recall(db, recall_id, session) -> dict:
    recall = db.recalls.find_one({"_id": recall_id}, session=session)
    if recall is None:
        raise ClaimError("unknown_recall", f"unknown recall {recall_id}")
    if recall["status"] != "open":
        raise ClaimError("recall_closed", "this recall is closed")
    return recall


def _check_delivery_note(db, recall_id, party_id, given, session) -> None:
    ship = db.shipments.find_one({"recall_id": recall_id, "party_id": party_id}, session=session)
    expected = (ship or {}).get("delivery_note")
    if expected and not given:
        raise ClaimError("delivery_note_required", "ask for the delivery note before taking counts")
    if expected and _digits(given) != _digits(expected):
        raise ClaimError("delivery_note_mismatch", f"delivery note {given} does not match the shipment record")


def _book_receipt(db, b: _Booking, recall_id, party_id, received_qty, sender, names, now, session) -> int:
    """The recipient's side of one inbound transfer. Returns the party's new
    shipped_in: its own shipment plus the corroborated quantity of every
    inbound transfer, read after this one is settled.

    A party with one inbound edge confirms that edge. A party that several
    senders shipped to must name the one these panels came from: settling
    whichever edge sorted first put the count on the wrong transfer (a false
    dispute, or a true count refused), and counting only that edge dropped
    the other senders' corroborated panels from the party's books, so the mass
    balance came up short and the close gate could pass."""
    inbound = list(db.edges.find({"recall_id": recall_id, "to_party": party_id}, session=session).sort("_id", 1))
    if not inbound:
        raise ClaimError("no_inbound_edge", "no sender has reported a transfer to this party yet")
    edge = inbound[0] if len(inbound) == 1 else _sender_edge(db, recall_id, inbound, sender, names, session)
    b.stage = "edge"
    settled = settle_edge(db, edge, edge["sender_qty"], received_qty, now, session)
    b.note_edge(edge, settled, counterpart=edge["from_party"], wake=_changed(edge, settled))
    return received_total(db, recall_id, party_id, session)


def _sender_edge(db, recall_id, inbound: list, sender: str | None, matched: dict, session) -> dict:
    """The inbound edge from the party a receipt names (`received_from`: a
    party id, a directory name or alias, or the sender's name; `matched` is
    the claim's names matched to parties). Refuses with
    receipt_sender_ambiguous, a rejection the party can answer and not a
    challenge, when no sender is named or the named one sent nothing here."""
    names = {s["party_id"]: s["name"] for s in db.parties.find(
        {"recall_id": recall_id, "party_id": {"$in": [e["from_party"] for e in inbound]}},
        {"party_id": 1, "name": 1}, session=session)}
    if sender:
        resolved = matched[sender][0]
        for e in inbound:
            sid = e["from_party"]
            if sender == sid or resolved == sid or _norm(sender) == _norm(names.get(sid, "")):
                return e
    listed = " and ".join(names.get(e["from_party"], e["from_party"]) for e in inbound)
    if sender:
        raise ClaimError("receipt_sender_ambiguous",
                         f"{sender} has not reported sending panels to this party; {listed} have")
    raise ClaimError("receipt_sender_ambiguous",
                     f"{listed} each sent panels to this party; the receipt must say which party sent these")


def _book_counts(db, party, c, ck, shipped_in, now, session, *, fix_check_at: float | None) -> None:
    """unresolved is derived inside the update, so a party that reports more
    than it was shipped needs a negative unresolved and Atlas refuses. The
    check answers the claim carries are set in the same update, so the party
    validator judges `fixed` against them. A model read that is the recalled
    model spelled differently ("demo 7") is stored as the recalled model."""
    answers = dict(ck)
    if "model_seen" in answers and _alnum(answers["model_seen"]) == _alnum(party["model"]):
        answers["model_seen"] = party["model"]
    db.parties.update_one({"_id": party["_id"]}, [
        {"$set": {"shipped_in": shipped_in, **{k: c[k] for k in BUCKETS}, "forwarded": c["forwarded_qty"],
                  **answers, "fix_check_at": fix_check_at}},
        {"$set": {"unresolved": {"$subtract": ["$shipped_in", _SUM]}, "state": "claimed", "last_claim_at": now}},
    ], session=session)


def _book_forwards(db, b: _Booking, party, entries, names, dirs, *, source, sim_now, now, session) -> None:
    """One edge per destination: open the new ones, re-settle the ones whose
    quantity changed, withdraw the ones the claim drops (or gives 0) while
    their recipient has not counted them, and refuse a claim that drops a
    transfer its recipient has already counted."""
    recall_id, party_id = party["recall_id"], party["party_id"]
    outbound = {e["to_party"]: e for e in db.edges.find({"recall_id": recall_id, "from_party": party_id},
                                                          session=session)}
    dests = _destinations(entries, party_id, names, dirs) if entries else {}
    dropped = sorted(set(outbound) - set(dests))
    counted = [to for to in dropped if outbound[to].get("recipient_qty") is not None]
    if counted:
        names = _names(db, recall_id, counted, session)
        if not dests:
            raise ClaimError("forward_restate", f"{names} counted the panels this party sent; a count must still "
                                                "say how many were sent there")
        raise ClaimError("forward_changed", f"an earlier claim reported panels sent to {names}, which this claim "
                                            "leaves out, and the receiving party has already counted them")
    for to_id in dropped:
        _withdraw_transfer(db, b, party, outbound[to_id], session)
    new = {to: d for to, d in dests.items() if to not in outbound}
    if new:
        _open_transfers(db, b, party, new, source=source, sim_now=sim_now, now=now, session=session)
    for to_id, edge in sorted(outbound.items()):
        if to_id not in dests or dests[to_id]["qty"] == edge["sender_qty"]:
            continue
        b.stage = "recipient"
        settled = settle_edge(db, edge, dests[to_id]["qty"], edge.get("recipient_qty"), now, session)
        was_counted = edge.get("recipient_qty") is not None
        if was_counted:
            rebook_recipient(db, recall_id=recall_id, to_party=to_id, session=session)
        b.note_edge(edge, settled, counterpart=to_id, wake=was_counted)
        b.stage = "party"
    b.touched.update(dests)


def _withdraw_transfer(db, b: _Booking, party, edge, session) -> None:
    """Delete a transfer its recipient never counted (nothing was booked
    against it, so the mass balance loses its open panels with the sender's
    forwarded), and the stub party this sender's claim created for it when
    nothing else refers to it: no other edge and no accepted claim of its
    own. The conditions read only ledger documents, so a rebuild from the
    claims log withdraws exactly the same things. A recipient that stays is
    re-checked like any other party the claim touched."""
    recall_id, sender, to_id = party["recall_id"], party["party_id"], edge["to_party"]
    db.edges.delete_one({"_id": edge["_id"]}, session=session)
    recipient = db.parties.find_one({"recall_id": recall_id, "party_id": to_id},
                                   {"name": 1, "discovered_by": 1, "shipped_in": 1}, session=session)
    removed = recipient is not None and _orphan_stub(db, recipient, recall_id, sender, to_id, session)
    if removed:
        db.parties.delete_one({"_id": recipient["_id"]}, session=session)
        b.removed.append(to_id)
    elif recipient is not None:
        b.touched.add(to_id)
    b.edges.append({**_edge_view(edge), "state": "withdrawn", "before": edge["state"],
                    "to_name": (recipient or {}).get("name") or to_id, "removed": removed})


def _orphan_stub(db, recipient: dict, recall_id, sender, to_id, session) -> bool:
    """A party that exists only because this sender's claim named it."""
    via, _, named_by = (recipient.get("discovered_by") or "").partition(":")
    if via not in ("claim", *DISCOVERED_VIA.values()) or named_by != sender or recipient.get("shipped_in"):
        return False
    other_edge = db.edges.find_one({"recall_id": recall_id, "$or": [{"from_party": to_id}, {"to_party": to_id}]},
                                   {"_id": 1}, session=session)
    own_claim = db.claims.find_one({"recall_id": recall_id, "party_id": to_id, "status": "accepted"},
                                   {"_id": 1}, session=session)
    return other_edge is None and own_claim is None


def _destinations(entries, party_id, names, dirs) -> dict:
    """{to_party_id: {"qty", "name", "entry"}}: each entry resolved to a party,
    entries naming the same party merged, parties left with no panels dropped."""
    out: dict = {}
    for f in entries:
        if "to_party_id" not in f and "to_name" not in f:
            raise ClaimError("forward_missing_name", "forwarded panels need the receiving party's name")
        if "to_party_id" in f:
            to_id = f["to_party_id"]
            entry = next((e for e in dirs if e.get("party_id") == to_id), None)
        else:
            to_id, entry = names[f["to_name"]]
        if to_id == party_id:
            raise ClaimError("forward_self", "a party cannot forward to itself")
        name = (entry or {}).get("name") or f.get("to_name") or to_id
        out.setdefault(to_id, {"qty": 0, "name": name, "entry": entry})["qty"] += f["qty"]
    return {to: d for to, d in out.items() if d["qty"] != 0}


def _names(db, recall_id, party_ids, session=None) -> str:
    names = {s["party_id"]: s["name"] for s in db.parties.find(
        {"recall_id": recall_id, "party_id": {"$in": party_ids}}, {"party_id": 1, "name": 1}, session=session)}
    return ", ".join(names.get(sid, sid) for sid in party_ids)


def _open_transfers(db, b: _Booking, party, new: dict, *, source, sim_now, now, session) -> None:
    """Stub parties for parties not yet in the run (due at once) and one awaiting
    edge to each new destination. A party that already existed is woken: a
    transfer into it is news it has to confirm."""
    recall_id, party_id = party["recall_id"], party["party_id"]
    via = f"{DISCOVERED_VIA.get(source, 'claim')}:{party_id}"
    # A distributor's order system claim names its accounts (tier 2 installers); a
    # party named by anyone else is tier 3, a building owner unless the
    # directory says otherwise. The directory entry, when there is one, wins.
    tier, type_, extra = (2, "installer", {"distributor": party_id}) if source == "order_system" \
        else (3, "owner", None)
    at = sim_now()
    stubs = [stub_party(recall_id, to, d["name"], d["entry"], via, now, extra, product=product_fields(party),
                        tier=tier, type_=type_, next_action_at=at) for to, d in new.items()]
    created = upsert_stubs(db, recall_id, stubs, session)
    if created:
        # Spec "The planner": it runs when a party is created. Set in this
        # transaction, so a new party and its wake-up commit together.
        db.runs.update_one({"_id": run_id(recall_id)},
                           {"$set": {"planner_wake": True, "planner_wake_reason": "parties_created"}}, session=session)
    b.discovered.extend(created)
    try:
        docs = open_edges(db, recall_id=recall_id, from_party=party_id,
                          sender_qty={to: d["qty"] for to, d in new.items()}, now=now, session=session)
    except BulkWriteError as exc:
        refused = _validator_refusal(exc)
        if refused is None:
            raise
        raise refused from exc
    for doc in docs:
        b.note_edge(None, doc, counterpart=doc["to_party"], wake=doc["to_party"] not in created)


def _validator_refusal(exc: BulkWriteError) -> WriteError | None:
    """A bulk write's validator refusal as the WriteError a single write
    raises, so it is classified and shown the same way (None otherwise)."""
    first = (exc.details.get("writeErrors") or [{}])[0]
    if first.get("code") != VALIDATION_FAILED:
        return None
    return WriteError(first.get("errmsg", str(exc)), VALIDATION_FAILED, first)


def _changed(before: dict, after: dict) -> bool:
    return (before["sender_qty"], before.get("recipient_qty")) != (after["sender_qty"], after.get("recipient_qty"))


def _refresh_states(db, recall_id, party_ids, session) -> set:
    """claimed -> reconciled once unresolved and installed are 0 (every panel
    is in stock, fixed, returned or passed on) and every edge touching the
    party is corroborated; back to claimed when a later claim reopens a
    dispute or reports panels installed and not fixed. Two reads and at most one write however many parties a claim
    touched: a distributor's claim touches every account.

    Returns the parties left with nothing to do (reconciled, no open
    question): the ones a wake must not disturb."""
    ids = sorted(party_ids)
    parties = list(db.parties.find({"recall_id": recall_id, "party_id": {"$in": ids},
                                  "state": {"$in": ["claimed", "reconciled"]}},
                                 {"party_id": 1, "state": 1, "unresolved": 1, "installed": 1, "open_questions": 1},
                                 session=session))
    if not parties:
        return set()
    open_ends: set = set()
    for e in db.edges.find({"recall_id": recall_id, "state": {"$ne": "corroborated"},
                            "$or": [{"from_party": {"$in": ids}}, {"to_party": {"$in": ids}}]},
                           {"from_party": 1, "to_party": 1}, session=session):
        open_ends.update((e["from_party"], e["to_party"]))
    ops, idle = [], set()
    for s in parties:
        settled = s["unresolved"] == 0 and s.get("installed", 0) == 0
        state = "reconciled" if settled and s["party_id"] not in open_ends else "claimed"
        if state != s["state"]:
            ops.append(UpdateOne({"_id": s["_id"]}, {"$set": {"state": state}}))
        if state == "reconciled" and not s.get("open_questions"):
            idle.add(s["party_id"])
    if ops:
        db.parties.bulk_write(ops, ordered=True, session=session)
    return idle


def _wake(db, recall_id, party_ids, at: float, session) -> None:
    """Make each party due by `at` (simulated hours) so the engine acts on
    it. An earlier timer stands: the party is already due."""
    db.parties.update_many(
        {"recall_id": recall_id, "party_id": {"$in": sorted(party_ids)}},
        [{"$set": {"next_action_at": {"$min": [{"$ifNull": ["$next_action_at", at]}, at]}}}],
        session=session)


def _yes_no_word(value) -> str:
    return {True: "yes", False: "no"}.get(value, "missing")


def _refusal(c: dict, ck: dict, entries: list, b: _Booking) -> tuple[str, str, dict | None]:
    """(code, reason, numbers) for a write the validators refused. The reason
    names the two values that disagree; `numbers` carries them as `values`
    (and over_count's reported_total / shipped_in)."""
    values = [*(c[k] for k in BUCKETS), c["received_qty"], *(f["qty"] for f in entries)]
    values = [v for v in values if v is not None]
    if any(v < 0 for v in values):
        return "negative", "counts cannot be negative", None
    if any(isinstance(v, float) for v in values):
        return "not_integer", "counts must be whole panels", None
    if b.stage == "recipient":
        return ("recipient_overdocumented", "the receiving party has already documented more than this transfer",
                None)
    reported = sum(c[k] for k in BUCKETS) + c["forwarded_qty"]
    if reported > b.shipped_in:
        return ("over_count", f"reported {reported}, shipped_in {b.shipped_in}",
                {"reported_total": reported, "shipped_in": b.shipped_in,
                 "values": {"reported": reported, "shipped_in": b.shipped_in}})
    party = b.party
    answers = {k: ck.get(k, party.get(k)) for k in CHECK_FIELDS}
    model = answers["model_seen"]
    if model is not None and _alnum(model) != _alnum(party["model"]):
        return ("model_mismatch", f"model_seen {model}, recalled model {party['model']}",
                {"values": {"model_seen": model, "model": party["model"]}})
    if c["fixed"] > party["fixed_by_panel"]:
        version, sounder = answers["version_seen"], answers["sounder_test"]
        if version is None:
            return ("fixed_needs_evidence", f"fixed {c['fixed']}, version_seen missing",
                    {"values": {"fixed": c["fixed"], "version_seen": None}})
        if version in party["affected_versions"]:
            return ("version_affected", f"fixed {c['fixed']}, version_seen {version} is affected",
                    {"values": {"fixed": c["fixed"], "version_seen": version}})
        if sounder is not True:
            return ("sounder_not_confirmed", f"fixed {c['fixed']}, sounder_test {_yes_no_word(sounder)}",
                    {"values": {"fixed": c["fixed"], "sounder_test": sounder}})
    if c["installed"] < party["connected_unfixed"]:
        return ("panel_not_fixed", f"installed {c['installed']}, connected panels without a passing test "
                f"{party['connected_unfixed']}",
                {"values": {"installed": c["installed"], "connected_unfixed": party["connected_unfixed"]}})
    return "refused", "Atlas refused the counts", None


def _accepted(b: _Booking, runs: int) -> dict:
    out = {"accepted": True, "claim_id": b.claim_id, "txn_runs": runs, "open_units": b.open_units}
    if b.discovered:
        out["discovered"], out["discovered_parties"] = b.discovered[0], list(b.discovered)
    if b.removed:
        out["removed_parties"] = list(b.removed)
    if b.edges:
        out["edge"], out["edges"] = b.edges[0], list(b.edges)
    if b.wake:
        out["woken"] = sorted(b.wake)
    out["message"] = speakable(out)
    return out


def _edge_view(e: dict) -> dict:
    return {k: e.get(k) for k in ("_id", "from_party", "to_party", "sender_qty", "recipient_qty",
                                   "corroborated_qty", "disputed_qty", "state")}


def _reply(claim: dict, duplicate=False) -> dict:
    out = {"accepted": claim["status"] == "accepted", "claim_id": claim["_id"]}
    if not out["accepted"]:
        out["reason"] = claim.get("reject_reason")
        out["code"] = claim.get("code")
        for k in ("reported_total", "shipped_in", "values", "atlas_error"):
            if claim.get(k) is not None:
                out[k] = claim[k]
    if duplicate:
        out["duplicate"] = True
    out["message"] = speakable(out)
    return out


def _reject(db, doc, key, recall_id, party_id, code, reason, numbers=None, err=None, runs=None) -> dict:
    rejected = {**doc, "status": "rejected", "reject_reason": reason, "code": code, **(numbers or {})}
    if err is not None:
        rejected["atlas_error"] = err
    try:
        db.claims.insert_one(rejected)
    except DuplicateKeyError:
        rejected = db.claims.find_one({"dedupe_key": key})
        return _reply(rejected, duplicate=True)
    db.parties.update_one(
        {"recall_id": recall_id, "party_id": party_id, "state": {"$in": ["pending", "contacting"]}},
        {"$set": {"state": "reached"}})
    out = _reply(rejected)
    if runs:
        out["txn_runs"] = runs
    return out


def challenges_used(db, attempt_id: str) -> int:
    """Rejections on this attempt that the agent had to read back."""
    return db.claims.count_documents({"attempt_id": attempt_id, "status": "rejected",
                                      "code": {"$in": sorted(CHALLENGE_CODES)}})
