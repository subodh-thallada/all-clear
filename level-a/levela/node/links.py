"""Links between nodes and the signature on every /agent request.

A link is one pairing of two nodes, stored on both (levela.db.schema_node):
`role` is this node's role in the pairing. Our node's link to Summit Foods is
role "parent"; Summit's link to us is role "child".

Every /agent request carries

    X-AllClear-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, f"{t}." + body)>

with the pairing's secret, which is an environment variable and never in
Atlas: LINK_SECRET_<PEER> (peer node id, upper case, `-` as `_`) or else
AGENT_LINK_SECRET (Settings.agent_link_secret). A timestamp more than five
minutes from now is refused, so a captured request cannot be replayed later.
A GET signs the empty body. Parsing reuses the webhook's ASCII-only rules
(levela.signing): a malformed header is "not signed", never an exception.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from ..signing import _HEX, _TIMESTAMP

HEADER = "X-AllClear-Signature"
WINDOW_S = 5 * 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- signing

class MissingLinkSecret(ValueError):
    """This node has no secret for the pairing (neither LINK_SECRET_<PEER> nor
    AGENT_LINK_SECRET), so nothing is sent: an /agent request is never unsigned.
    A configuration gap, not an unreachable peer: the parent's worker waits and
    says so once (orchestrator.worker), it does not walk the quiet ladder."""


def _digest(secret: str, t: str, raw: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), f"{t}.".encode("utf-8") + raw, hashlib.sha256).hexdigest()


def sign(raw: bytes, secret: str, t: int | None = None) -> str:
    if not secret:
        raise MissingLinkSecret("no link secret configured: refusing to send an unsigned /agent request")
    t = str(int(time.time()) if t is None else int(t))
    return f"t={t},v1={_digest(secret, t, raw)}"


def parse(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    parts = {}
    for piece in header.split(","):
        k, _, v = piece.strip().partition("=")
        if k:
            parts[k] = v
    t, sig = parts.get("t") or "", parts.get("v1") or ""
    if not _TIMESTAMP.fullmatch(t) or not _HEX.fullmatch(sig):
        return None
    return t, sig


def check(raw: bytes, header: str | None, secret: str, *, now: float | None = None) -> str | None:
    """None when the signature is good, else why it is not."""
    parsed = parse(header)
    if parsed is None:
        return "missing or malformed signature"
    if not secret:
        return "no link secret configured on this node"
    t, sig = parsed
    now = time.time() if now is None else now
    if abs(int(t) - now) > WINDOW_S:
        return "signature timestamp outside the 5-minute window"
    if not hmac.compare_digest(sig.lower().encode("ascii"), _digest(secret, t, raw).encode("ascii")):
        return "signature does not match"
    return None


def body_bytes(payload: dict | None) -> bytes:
    """The exact bytes that are signed and sent."""
    if payload is None:
        return b""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def signed_headers(raw: bytes, secret: str, *, t: int | None = None) -> dict:
    return {HEADER: sign(raw, secret, t), "Content-Type": "application/json"}


def link_secret(settings, peer_node: str | None, env=None) -> str:
    """The pairing's secret: LINK_SECRET_<PEER>, else AGENT_LINK_SECRET."""
    env = os.environ if env is None else env
    if peer_node:
        key = "LINK_SECRET_" + peer_node.upper().replace("-", "_")
        if env.get(key):
            return env[key]
    return getattr(settings, "agent_link_secret", "") or ""


# ---------------------------------------------------------------- links

def link_doc(*, link_id: str, role: str, peer_node: str, peer_url: str, match: dict | None = None,
             party_id: str | None = None, status: str = "pending", now: datetime | None = None) -> dict:
    now = now or utcnow()
    return {"_id": link_id, "role": role, "peer_node": peer_node, "peer_url": (peer_url or "").rstrip("/"),
            "match": {"system": (match or {}).get("system"), "account": (match or {}).get("account")},
            "party_id": party_id, "last_seq_sent": 0, "last_seq_accepted": 0, "last_seen_at": None,
            "status": status, "created_at": now}


def ensure_link(db, doc: dict) -> dict:
    """Insert the link once; an existing pairing keeps its sequence numbers."""
    db.links.update_one({"role": doc["role"], "peer_node": doc["peer_node"]},
                        {"$setOnInsert": doc}, upsert=True)
    return db.links.find_one({"role": doc["role"], "peer_node": doc["peer_node"]})


def get(db, link_id: str) -> dict | None:
    return db.links.find_one({"_id": link_id})


def by_peer(db, role: str, peer_node: str) -> dict | None:
    return db.links.find_one({"role": role, "peer_node": peer_node})


def for_account(db, system: str, account: str) -> dict | None:
    """The parent-side link that matches an order system account, if any."""
    return db.links.find_one({"role": "parent", "match.system": system, "match.account": account})


def empty_sub() -> dict:
    return {"seq": 0, "parties_total": 0, "parties_open": 0, "state": "pending",
            "buyers": {"identified": 0, "told": 0, "unreachable": 0, "anonymous_sales": 0}}


def agent_party_fields(db, system: str, account: str) -> dict | None:
    """What an order system account's party gets when a link matches it: it is
    answered by the other node's agent, not by phone. None when no link
    matches. The order system code calls this when it creates account
    parties (spec "The handoff", step 1)."""
    link = for_account(db, system, account)
    if link is None:
        return None
    return {"channel": "agent", "link_id": link["_id"], "sub": empty_sub()}


def attach_party(db, link: dict, recall_id: str, party_id: str) -> None:
    """Point the link at its party, and mark the party as answered by the agent."""
    db.links.update_one({"_id": link["_id"]}, {"$set": {"party_id": party_id}})
    db.parties.update_one({"recall_id": recall_id, "party_id": party_id, "sub": {"$exists": False}},
                          {"$set": {"sub": empty_sub()}})
    db.parties.update_one({"recall_id": recall_id, "party_id": party_id},
                          {"$set": {"channel": "agent", "link_id": link["_id"]}})
