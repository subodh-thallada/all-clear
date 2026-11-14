"""Rebuild a recall's ledger from its claims log and compare.

The claims collection is the ledger's write-ahead log: every accepted claim
went through submit_claim(), and the party and edge documents are only ever
derived from those claims. So replaying the accepted claims, in the order they
were booked, onto the recall's seeded state must reproduce the party and edge
documents field for field. If it does not, something wrote the ledger outside
the claim path, and the diff says exactly where.

The replay runs in a scratch database on the same cluster (the ledger's
validators and indexes set up by setup()), so it goes through the same
transactions and the same Atlas validators as the live run, and the live
database is only read. A claim that forwards to several parties replays as one
claim and opens the same edges, because the raw body it was booked from is
what the claims log keeps.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .. import feed
from ..db.client import run_txn
from ..db.setup import LEDGER_COLLECTIONS, setup
from .claims import submit_claim
from .claims import CHECK_FIELDS
from .transfers import ACCOUNT_FIELDS, ledger_fields, stub_party, v2_fields

log = logging.getLogger("levela.rebuild")

BADGE_ID = "badge:rebuild"
# Marks a database as this module's scratch space; only a marked database is
# ever dropped, so a mistyped scratch name cannot destroy someone's data.
SCRATCH_MARK = "rebuild_scratch"

PARTY_FIELDS = ("shipped_in", "sold", "on_shelf", "held", "returned", "forwarded", "unresolved", *CHECK_FIELDS,
                "batch_code", "tier", "type")
EDGE_FIELDS = ("sender_qty", "recipient_qty", "corroborated_qty", "disputed_qty", "state")
# pending/contacting/reached follow attempts and calls, not claims, so a party's
# state is only compared once a claim has set it.
CLAIM_STATES = ("claimed", "reconciled")
# Recall fields a run adds after it starts; the seeded recall has none of them.
RUN_FIELDS = ("closed_at", "last_close_refused_at", "updated_at")


class RebuildError(Exception):
    """The rebuild could not run (unknown recall, unsafe scratch database)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rebuild(client, db, recall_id, *, scratch_db_name=None) -> dict:
    """Replay recall_id's accepted claims onto its seeded state in a scratch
    database and compare the result with the live parties and edges.

    Returns {"identical", "diffs", "parties", "edges", "claims_replayed"}. Each
    diff is {"collection", "id", "field", "ledger", "rebuilt"}: a field whose
    values differ, a document present on one side only (field None, the
    missing side None), or a claim the replay refused (collection "claims").
    On success the wall badge `badge:rebuild` is written; on a mismatch it is
    removed, so the wall never shows an identity the latest rebuild disproved.
    """
    started = time.monotonic()
    name = scratch_db_name or f"{db.name}_rebuild"
    scratch = _claim_scratch(client, name, db.name)
    try:
        snap = _snapshot(client, db, recall_id)
        setup(scratch, collections=LEDGER_COLLECTIONS)
        _seed(scratch, snap)
        diffs = _replay(client, scratch, recall_id, snap["claims"])
        rebuilt_parties = list(scratch.parties.find({"recall_id": recall_id}))
        rebuilt_edges = list(scratch.edges.find({"recall_id": recall_id}))
    finally:
        _drop_scratch(scratch)
    diffs += _compare_parties(snap["parties"], rebuilt_parties)
    diffs += _compare_edges(snap["edges"], rebuilt_edges)
    result = {"identical": not diffs, "diffs": diffs, "parties": len(snap["parties"]),
              "edges": len(snap["edges"]), "claims_replayed": len(snap["claims"])}
    _report(db, recall_id, result, scratch=name, seconds=round(time.monotonic() - started, 2))
    return result


def _snapshot(client, db, recall_id) -> dict:
    """Read the recall, its shipments, parties, edges and accepted claims at one
    point in time (a read-only snapshot transaction), so a claim booked while
    the rebuild runs cannot show up as a false difference."""
    def read(session):
        recall = db.recalls.find_one({"_id": recall_id}, session=session)
        if recall is None:
            raise RebuildError(f"unknown recall {recall_id}")
        scope = {"recall_id": recall_id}
        return {
            "recall": recall,
            "shipments": list(db.shipments.find(scope, session=session)),
            "parties": list(db.parties.find(scope, session=session)),
            "edges": list(db.edges.find(scope, session=session)),
            "claims": list(db.claims.find({**scope, "status": "accepted"}, session=session)
                           .sort([("created_at", 1), ("_id", 1)])),
        }
    return run_txn(client, read)


def _claim_scratch(client, name, live_name):
    """An empty database marked as this module's scratch space.

    A leftover scratch database (a rebuild that died midway) is dropped; any
    other existing database is refused. The mark is written first, so a
    scratch database left half-built by a failure is still ours to drop.
    """
    if name == live_name:
        raise RebuildError("the scratch database must not be the live database")
    scratch = client[name]
    if scratch.list_collection_names():
        if scratch.worker_state.find_one({"_id": SCRATCH_MARK}) is None:
            raise RebuildError(f"database {name} already exists and is not a rebuild scratch database; "
                               "refusing to drop it (pass another scratch_db_name)")
        _drop_scratch(scratch)
    scratch.worker_state.insert_one({"_id": SCRATCH_MARK, "source_db": live_name, "at": utcnow()})
    return scratch


def _drop_scratch(scratch) -> None:
    """Drop collection by collection: dropDatabase needs dbAdmin, which the
    Atlas readWrite user lacks, and a database with no collections is gone."""
    for coll in scratch.list_collection_names():
        scratch.drop_collection(coll)


def _seed(scratch, snap) -> None:
    """The recall as start_recall() left it, plus the stubs no claim created.

    Direct parties are rebuilt from the shipment records (the distributor's
    ground truth), not from the live party documents being checked. A party
    that memory suspected, an order system listed or a rep named is not
    claim-derived, so it is recreated with zero counts before any claim,
    exactly as it existed when the first claim named it; stubs a claim
    created are recreated by the replay itself.
    """
    recall = {k: v for k, v in snap["recall"].items() if k not in RUN_FIELDS}
    recall.update(status="open", open_units=recall["units_total"])
    scratch.recalls.insert_one(recall)
    if snap["shipments"]:
        scratch.shipments.insert_many(snap["shipments"])
    shipped = {ship["party_id"] for ship in snap["shipments"]}
    live = {s["party_id"]: s for s in snap["parties"]}
    parties = [_direct_party(ship, live.get(ship["party_id"]), recall) for ship in snap["shipments"]]
    parties += [_seeded_stub(s) for s in snap["parties"] if s["party_id"] not in shipped and not _claim_made(s)]
    if parties:
        scratch.parties.insert_many(parties)


def _claim_made(party: dict) -> bool:
    """True when a claim created the party: discovered_by "claim:<sender>",
    "order_system:<distributor>" (a distributor's order system claim) or "rep:<party>"."""
    return (party.get("discovered_by") or "").startswith(("claim:", "order_system:", "rep:"))


def _direct_party(ship: dict, live: dict | None, recall: dict) -> dict:
    """A party on the shipment list at the start of the recall: the whole
    shipment unresolved, the long-run fields as start_recall() set them.
    Identity fields come from the live document; a shipment with no party
    document is rebuilt anyway so the comparison reports the missing party."""
    ident = live or {"_id": f"{ship['recall_id']}:{ship['party_id']}", "name": ship["party_id"],
                     "type": "store", "tier": 1, "channel": "form", "discovered_by": "shipment"}
    qty = ship["qty"]
    v2 = v2_fields(tier=ident.get("tier", 1), found_via="shipment", slice_=bool(ident.get("slice")),
                   hold_for_demo=bool(ident.get("hold_for_demo")), followups=ident.get("followups", "auto"),
                   **({"order_system": ident["order_system"]} if ident.get("order_system") else {}))
    return {
        **v2,
        "_id": ident["_id"], "recall_id": ship["recall_id"], "party_id": ship["party_id"],
        "name": ident["name"], "type": ident["type"], "channel": ident["channel"],
        "phone": ident.get("phone"), "profile": ident.get("profile"),
        "discovered_by": ident["discovered_by"], "form_token": ident.get("form_token"),
        "created_at": ident.get("created_at"), **ledger_fields(qty, recall), "state": "pending",
    }


def _seeded_stub(live: dict) -> dict:
    """A party that entered the run without a claim, as it was before any."""
    entry = {k: live.get(k) for k in ("name", "type", "channel", "phone", "profile", "tier", "slice", "hold_for_demo",
                                      "followups", *ACCOUNT_FIELDS) if live.get(k) is not None}
    return {**stub_party(live["recall_id"], live["party_id"], live["name"], entry, live.get("discovered_by", ""),
                         live.get("created_at"), product=live), "_id": live["_id"]}


def _replay(client, scratch, recall_id, claims) -> list:
    """Book each accepted claim again, with its own attempt id, source, raw
    body and timestamp. The delivery-note check already passed when the claim
    was first accepted, so it is not asked again."""
    diffs = []
    for claim in claims:
        res = submit_claim(client, scratch, recall_id=recall_id, party_id=claim["party_id"],
                           attempt_id=claim["attempt_id"], source=claim["source"], raw=claim["raw"],
                           now=claim["created_at"], claim_id=claim["_id"], require_delivery_note=False)
        if not res["accepted"] or res.get("duplicate"):
            outcome = "duplicate" if res.get("duplicate") else f"rejected: {res.get('code')}"
            diffs.append({"collection": "claims", "id": str(claim["_id"]), "field": "status",
                          "ledger": "accepted", "rebuilt": outcome})
    return diffs


def _party_view(party: dict) -> dict:
    view = {k: party.get(k) for k in PARTY_FIELDS}
    view["state"] = party.get("state")
    return view


def _compare_parties(live: list, rebuilt: list) -> list:
    a = {s["party_id"]: _party_view(s) for s in live}
    b = {s["party_id"]: _party_view(s) for s in rebuilt}
    diffs = []
    for sid in sorted(a.keys() | b.keys()):
        if sid not in a or sid not in b:
            diffs.append(_missing("parties", sid, a.get(sid), b.get(sid)))
            continue
        fields = list(PARTY_FIELDS)
        if a[sid]["state"] in CLAIM_STATES or b[sid]["state"] in CLAIM_STATES:
            fields.append("state")
        diffs += _field_diffs("parties", sid, a[sid], b[sid], fields)
    return diffs


def _compare_edges(live: list, rebuilt: list) -> list:
    def key(e):
        return f"{e['from_party']}>{e['to_party']}"
    a = {key(e): {k: e.get(k) for k in EDGE_FIELDS} for e in live}
    b = {key(e): {k: e.get(k) for k in EDGE_FIELDS} for e in rebuilt}
    diffs = []
    for eid in sorted(a.keys() | b.keys()):
        if eid not in a or eid not in b:
            diffs.append(_missing("edges", eid, a.get(eid), b.get(eid)))
            continue
        diffs += _field_diffs("edges", eid, a[eid], b[eid], EDGE_FIELDS)
    return diffs


def _field_diffs(collection, doc_id, a, b, fields) -> list:
    return [{"collection": collection, "id": doc_id, "field": f, "ledger": a[f], "rebuilt": b[f]}
            for f in fields if a[f] != b[f]]


def _missing(collection, doc_id, a, b) -> dict:
    return {"collection": collection, "id": doc_id, "field": None, "ledger": a, "rebuilt": b}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _report(db, recall_id, result, *, scratch, seconds) -> None:
    """The wall badge and a feed line, so the room sees the outcome."""
    detail = {"recall_id": recall_id, "parties": result["parties"], "edges": result["edges"],
              "claims_replayed": result["claims_replayed"], "scratch_db": scratch, "seconds": seconds}
    if result["identical"]:
        text = (f"Ledger rebuilt from the claims log: identical ({_plural(result['parties'], 'party', 'parties')}, "
                f"{_plural(result['edges'], 'edge')}, "
                f"{_plural(result['claims_replayed'], 'accepted claim')} replayed)")
        db.worker_state.update_one({"_id": BADGE_ID}, {"$set": {"text": text, "at": utcnow(), "detail": detail}},
                                   upsert=True)
        feed.emit(db, "info", text, recall_id=recall_id, level="good", detail=detail, source="admin",
                  system="ledger")
        return
    db.worker_state.delete_one({"_id": BADGE_ID})
    diffs = result["diffs"]
    text = (f"Ledger rebuild found {_plural(len(diffs), 'difference')} between the live ledger and "
            f"its claims log in {recall_id}")
    log.warning("%s: %s", text, diffs)
    feed.emit(db, "error", text, recall_id=recall_id, level="bad", detail={**detail, "diffs": diffs},
              source="admin", system="ledger")
