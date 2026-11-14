"""Transfers between parties: the edge, its dispute arithmetic, and the stub
party a forwarded shipment creates.

Each party's books follow its own claim and the gap lives on the edge:

  sender.forwarded     = sum of sender_qty over its outbound edges
  recipient.shipped_in = shipment_qty + sum of corroborated_qty over its inbound edges
  corroborated_qty     = min(sender_qty, recipient_qty)
  disputed_qty         = |sender_qty - recipient_qty|

shipment_qty is the recipient's own delivery on the supplier's shipment
records: 0 for a party reached only through transfers, the shipment record
for a tier 1 party that also received tins from another party.

A store that passes tins to another store books them as forwarded (an edge);
the second store books them as on_shelf, and later as held, sold or returned,
on evidence (see the party validator in levela.db.schema).

A sender may forward to several parties (V2 §9): one edge per destination. A
party may also receive from several senders: one edge per sender, and its
shipped_in counts every one of them. Booking only the edge a claim touched
would drop the others' corroborated tins from the party's books while their
edges still counted them, and the mass balance would lose them silently.

These helpers run inside the claim transaction (they take a session).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from pymongo import UpdateOne

# Every bucket but unresolved, which is derived: shipped_in minus this.
_SUM = {"$add": ["$sold", "$on_shelf", "$held", "$returned", "$forwarded"]}
# Identity a directory entry gives an order system account's party (V2 §4).
ACCOUNT_FIELDS = ("account", "distributor")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def resolve_party(name: str, directory=None):
    """Spoken party name -> (party_id, directory entry or None), from the
    recall's directory alone (levela.ledger.names.match_party also matches the
    parties already in the run).

    Directory names and aliases win, else the slug. The id must be the same
    whether a party is first named by a claim or first suspected by memory, or
    it appears twice in the graph.
    """
    key = _norm(name)
    for entry in directory or []:
        if key in {_norm(n) for n in [entry["name"], *entry.get("aliases", [])]}:
            return entry["party_id"], entry
    return slugify(name), None


def edge_numbers(sender_qty: int, recipient_qty: int | None) -> tuple[int, int, str]:
    """(corroborated_qty, disputed_qty, state) for one transfer."""
    if recipient_qty is None:
        return 0, 0, "awaiting_recipient"
    corr = min(sender_qty, recipient_qty)
    disp = abs(sender_qty - recipient_qty)
    return corr, disp, ("corroborated" if disp == 0 else "disputed")


def edge_id(recall_id: str, from_party: str, to_party: str) -> str:
    return f"{recall_id}:{from_party}>{to_party}"


FREE_LEASE = datetime(1970, 1, 1, tzinfo=timezone.utc)


def product_fields(recall: dict | None) -> dict:
    """What a party carries from its recall so the party validator can judge
    a held claim on its own: the batch code (spec "Checks": the batch code
    is copied onto each party). `recall` may be the recall or any party of
    it; {} when neither is given (the claim path copies the recall's batch
    code onto the party at its first claim)."""
    code = (recall or {}).get("batch_code")
    return {"batch_code": code} if isinstance(code, str) else {}


def ledger_fields(shipped_in: int, product: dict | None = None) -> dict:
    """A new party's books: everything unresolved, no evidence yet."""
    return {"shipped_in": shipped_in, "sold": 0, "on_shelf": 0, "held": 0, "returned": 0, "forwarded": 0,
            "unresolved": shipped_in, **product_fields(product), "batch_seen": None, "sold_source": None,
            "block_test": None, "held_by_system": 0, "sold_by_till": 0, "system_on_shelf": 0}


def v2_fields(*, tier: int, found_via: str, next_action_at: float | None = None, slice_: bool = False,
              hold_for_demo: bool = False, followups: str = "auto", **extra) -> dict:
    """The long-run fields every party carries (V2 §4): its timer, ladder
    position and lease. A new party is due at once (`next_action_at`), so the
    worker's due query picks it up without any event having to be delivered.
    `tier` is how the party entered the run: 1 on the shipment records, 2 an
    order system's account, 3 named by a claim."""
    return {
        "tier": int(tier), "found_via": found_via, "next_action_at": next_action_at, "ladder_step": 1,
        "hold_for_demo": bool(hold_for_demo), "slice": bool(slice_), "followups": followups,
        "lease_owner": None, "lease_until": FREE_LEASE, "lease_epoch": 0, "priority": 0,
        "open_questions": [], "answers": {}, "last_outcome": None, "memory_hour": None, **extra,
    }


def found_via(discovered_by: str) -> str:
    """`discovered_by` ("shipment", "claim:<party>", "order_system:<party>", "memory",
    "rep") -> the `found_via` enum (shipment|order_system|claim|memory|rep)."""
    return (discovered_by or "").split(":", 1)[0]


def stub_party(recall_id, party_id, name, entry, discovered_by, now, extra=None, *, product: dict | None = None,
               tier: int = 3, type_: str = "store", next_action_at: float | None = None) -> dict:
    """A party known only because someone named it. shipped_in is 0 until a
    corroborated transfer books tins into it. Due at once when the caller
    passes the current simulated time as next_action_at.

    `product` is the recall (or any party of it): its batch code is copied
    on. Without it the party has no batch code until its first claim copies
    the recall's (and until then no tin can be held on a phone answer). The
    directory entry, when there is one, supplies the party's type, contact
    details and long-run fields (tier, slice, demo hold, follow-ups, and an
    order system account's `account` / `distributor`). A party named by a
    claim is a store unless the directory says otherwise."""
    entry = entry or {}
    v2 = v2_fields(tier=entry.get("tier", tier), found_via=found_via(discovered_by),
                   next_action_at=next_action_at, slice_=bool(entry.get("slice")),
                   hold_for_demo=bool(entry.get("hold_for_demo")), followups=entry.get("followups", "auto"),
                   **{k: entry[k] for k in ACCOUNT_FIELDS if entry.get(k) is not None})
    return {**v2,
        "_id": f"{recall_id}:{party_id}", "recall_id": recall_id, "party_id": party_id,
        "name": entry.get("name", name), "type": entry.get("type", type_),
        **ledger_fields(0, product),
        "state": "pending",
        # A party nobody has contact details for can still answer through the form.
        "channel": entry.get("channel", "form"), "phone": entry.get("phone"),
        "profile": entry.get("profile"),
        "discovered_by": discovered_by, "form_token": secrets.token_urlsafe(6),
        "created_at": now, **(extra or {}),
    }


def upsert_stubs(db, recall_id, stubs: list[dict], session) -> list[str]:
    """Insert the stub parties that do not exist yet, in one round trip.
    Existing parties are left exactly as they are. Returns the ids created."""
    if not stubs:
        return []
    ops = [UpdateOne({"recall_id": recall_id, "party_id": s["party_id"]}, {"$setOnInsert": s}, upsert=True)
           for s in stubs]
    res = db.parties.bulk_write(ops, ordered=True, session=session)
    return [stubs[i]["party_id"] for i in sorted(res.upserted_ids)]


def open_edges(db, *, recall_id, from_party, sender_qty: dict, now, session) -> list[dict]:
    """One awaiting edge per destination ({to_party: sender_qty}), in one round trip."""
    docs = [{
        "_id": edge_id(recall_id, from_party, to_party), "recall_id": recall_id,
        "from_party": from_party, "to_party": to_party,
        "sender_qty": qty, "recipient_qty": None,
        "corroborated_qty": 0, "disputed_qty": 0,
        "state": "awaiting_recipient", "created_at": now, "updated_at": now,
    } for to_party, qty in sender_qty.items()]
    if docs:
        db.edges.insert_many(docs, ordered=True, session=session)
    return docs


def settle_edge(db, edge, sender_qty, recipient_qty, now, session) -> dict:
    """Rewrite one edge from both claims. Returns the new numbers."""
    corr, disp, state = edge_numbers(sender_qty, recipient_qty)
    new = {"sender_qty": sender_qty, "recipient_qty": recipient_qty,
           "corroborated_qty": corr, "disputed_qty": disp, "state": state, "updated_at": now}
    db.edges.update_one({"_id": edge["_id"]}, {"$set": new}, session=session)
    return {**edge, **new}


def shipment_qty(db, recall_id, party_id, session=None) -> int:
    """Tins the supplier delivered to this party directly (0 if none).

    A recipient's shipped_in is this plus the corroborated transfer. Setting
    it to the transfer alone erased a direct party's own shipment when it
    also received tins from another party: each party still conserved, so
    no validator objected, but the global mass balance lost the shipment.
    """
    ship = db.shipments.find_one({"recall_id": recall_id, "party_id": party_id}, {"qty": 1}, session=session)
    return ship["qty"] if ship else 0


def received_total(db, recall_id, party_id, session=None) -> int:
    """The party's shipped_in as the edges say it: its own shipment plus the
    corroborated quantity of every inbound edge. Read inside the claim's
    session after the edge writes, so it sees this claim's own changes."""
    corroborated = sum(e["corroborated_qty"] for e in db.edges.find(
        {"recall_id": recall_id, "to_party": party_id}, {"corroborated_qty": 1}, session=session))
    return shipment_qty(db, recall_id, party_id, session) + corroborated


def rebook_recipient(db, *, recall_id, to_party, session):
    """The recipient's shipped_in follows its corroborated transfers (all of
    them, see received_total); its unresolved is re-derived inside the update
    so the party validator decides."""
    db.parties.update_one({"recall_id": recall_id, "party_id": to_party}, [
        {"$set": {"shipped_in": received_total(db, recall_id, to_party, session)}},
        {"$set": {"unresolved": {"$subtract": ["$shipped_in", _SUM]}}},
    ], session=session)
