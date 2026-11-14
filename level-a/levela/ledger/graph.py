"""The transfer graph: $graphLookup over edges.

A sender may have several outbound edges (V2 §9), so a party's reach is a
tree, not a chain; one $graphLookup still walks it whatever its depth.
"""
from __future__ import annotations


def _reach(db, recall_id, from_party) -> dict[str, int]:
    """party_id -> hops from the parties matched by `from_party` (a party id or
    a query such as {"$in": [...]}), along transfers. One aggregation."""
    hops: dict[str, int] = {}
    for row in db.edges.aggregate([
        {"$match": {"recall_id": recall_id, "from_party": from_party}},
        {"$graphLookup": {
            "from": "edges", "startWith": "$to_party",
            "connectFromField": "to_party", "connectToField": "from_party",
            "as": "chain", "depthField": "depth",
            "restrictSearchWithMatch": {"recall_id": recall_id},
        }},
    ]):
        hops[row["to_party"]] = 1
        for hop in row["chain"]:
            d = hop["depth"] + 2
            hops[hop["to_party"]] = min(hops.get(hop["to_party"], d), d)
    return hops


def downstream(db, recall_id, party_id) -> list:
    """Every party reachable from party_id along transfers: [(party_id, hops)],
    nearest first. Recursive, so a three-tier chain is found the same way as
    one hop."""
    hops = _reach(db, recall_id, party_id)
    hops.pop(party_id, None)        # a transfer cycle back to the start is not downstream
    return sorted(hops.items(), key=lambda kv: (kv[1], kv[0]))


def tiers(db, recall_id) -> dict:
    """party_id -> hops from the shipment records (0 for a tier 1 party, None
    for a party on no transfer yet, e.g. suspected by memory). One find and
    one aggregation, however many tier 1 parties the recall has."""
    parties = list(db.parties.find({"recall_id": recall_id}, {"party_id": 1, "tier": 1}))
    out = {s["party_id"]: 0 for s in parties if s.get("tier") == 1}
    if out:
        for sid, hops in _reach(db, recall_id, {"$in": sorted(out)}).items():
            out[sid] = min(out.get(sid, hops), hops)
    for s in parties:
        out.setdefault(s["party_id"], None)
    return out
