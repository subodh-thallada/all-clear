"""What a call carries: the voice agent's dynamic variables, and the key that
names one disagreement on a transfer.

The worker sends the variables with every call (ElevenLabs'
conversation_initiation_client_data.dynamic_variables), always as strings,
so the prompt never sees a missing or typed value. Never shipped_in and
never the affected or fixed software versions: the agent learns of a
mismatch only from the database's reply to submit_count.
"""
from __future__ import annotations

# Who the agent says it calls for, when the recall names no supplier.
DEFAULT_COMPANY = "Kestrel Life Safety"


def dispute_key(edge: dict) -> str:
    """Identifies one disagreement: a corrected claim that leaves a different
    gap is a new dispute and earns each party a new follow-up."""
    return f"{edge['_id']}:{edge['sender_qty']}:{edge['recipient_qty']}"


def dynamic_variables(recall: dict, party: dict, purpose: str, *, sender_name: str = "", told_qty: str = "",
                      followup_note: str = "", ask_delivery_note: bool = False, receipt: bool | None = None) -> dict:
    """The agent's dynamic variables (attempt_id and the question variables
    aside). `receipt` says whether to ask what the party received (default:
    a party below tier 1, which received its panels from another party). A
    building owner is asked the three checks."""
    if receipt is None:
        receipt = party.get("tier") != 1
    values = {
        "party_id": party["party_id"],
        "party_name": party["name"],
        "party_type": party.get("type"),
        "batch_code": recall["batch_code"],
        "batch_spoken": recall.get("batch_spoken") or recall["batch_code"],
        "model": recall.get("model"),
        "product": recall.get("product"),
        "hazard": recall.get("hazard"),
        "unit": recall.get("unit") or "panel",
        "receipt": "yes" if receipt else "no",
        "ask_checks": "yes" if party.get("type") == "owner" else "no",
        "purpose": purpose,
        "company": recall.get("supplier") or DEFAULT_COMPANY,
        "sender_name": sender_name,
        "told_qty": told_qty,
        "ask_delivery_note": "yes" if ask_delivery_note else "no",
        "followup_note": followup_note,
    }
    return {k: "" if v is None else str(v) for k, v in values.items()}
