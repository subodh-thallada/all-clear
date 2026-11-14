"""The canary: a count the ledger already holds, read again through a
candidate adapter (V2 §7 "Verify").

The basis is one account whose receipt both sides agree on: an edge from the
distributor in state `corroborated`, whose corroborated_qty the sender's v1
read and the account's own claim both produced. A candidate that maps
qty_shipped to the wrong field reads a different number for that account
and is never activated. Only when no account has corroborated yet does the
canary fall back to the edge's sender_qty (the old adapter's read), and the
record says so. With no edge at all there is nothing to check against, and a
candidate is not activated on trust.

    {account, party_id, field, ledger_value, order_system_value, passed, basis
     ("corroborated_qty"|"sender_qty"|None), note, inconclusive, at (sim hours)}
"""
from __future__ import annotations

from .contract import ContractError
from .order_client import OrderSystemError, OrderSystemTimeout

FIELD = "qty_shipped"
SENDER_NOTE = ("no account has corroborated its receipt yet: compared with the shipped count "
               "read through the old adapter")


def ledger_basis(db, recall_id: str, distributor_id: str) -> dict | None:
    """The account the canary reads, and the ledger's count for it."""
    edges = list(db.edges.find({"recall_id": recall_id, "from_party": distributor_id},
                               {"to_party": 1, "state": 1, "sender_qty": 1, "corroborated_qty": 1}))
    if not edges:
        return None
    accounts = {s["party_id"]: s["account"] for s in db.parties.find(
        {"recall_id": recall_id, "party_id": {"$in": [e["to_party"] for e in edges]}, "account": {"$type": "string"}},
        {"party_id": 1, "account": 1})}
    edges = [e for e in edges if e["to_party"] in accounts]
    agreed = sorted((e for e in edges if e["state"] == "corroborated" and e["corroborated_qty"] > 0),
                    key=lambda e: (-e["corroborated_qty"], e["to_party"]))
    if agreed:
        e = agreed[0]
        return {"party_id": e["to_party"], "account": accounts[e["to_party"]], "ledger_value": e["corroborated_qty"],
                "basis": "corroborated_qty", "note": None}
    shipped = sorted((e for e in edges if e["sender_qty"] > 0), key=lambda e: (-e["sender_qty"], e["to_party"]))
    if not shipped:
        return None
    e = shipped[0]
    return {"party_id": e["to_party"], "account": accounts[e["to_party"]], "ledger_value": e["sender_qty"],
            "basis": "sender_qty", "note": SENDER_NOTE}


def run_canary(db, *, recall_id: str, distributor_id: str, batch: str, client, candidate: dict, sim: float) -> dict:
    """Read the basis account through `candidate` and compare. Never raises
    for order system trouble: an unreachable order system gives inconclusive=True (try
    again), a candidate that cannot even read the accounts fails."""
    record = {"account": None, "party_id": None, "field": FIELD, "ledger_value": None, "order_system_value": None,
              "passed": False, "basis": None, "note": None, "inconclusive": False, "at": sim}
    basis = ledger_basis(db, recall_id, distributor_id)
    if basis is None:
        return {**record, "note": "no account of this distributor is in the ledger yet: nothing to check "
                                  "a new adapter against"}
    record.update(basis)
    try:
        rows = client.list_accounts(batch, adapter=candidate)
    except ContractError as exc:
        return {**record, "note": f"the candidate cannot read the accounts: {exc.failure_text}"}
    except (OrderSystemTimeout, OrderSystemError) as exc:
        return {**record, "inconclusive": True, "note": f"order system unavailable: {exc}"}
    row = next((r for r in rows if r["account"] == basis["account"]), None)
    if row is None:
        return {**record, "note": f"account {basis['account']} is not in the candidate's read"}
    value = row[FIELD]
    return {**record, "order_system_value": value, "passed": value == basis["ledger_value"]}


def describe(canary: dict) -> str:
    """"canary 48 = 48" / "canary 48 != 22", for the feed."""
    sign = "=" if canary.get("passed") else "!="
    return f"canary {canary.get('ledger_value')} {sign} {canary.get('order_system_value')}"
