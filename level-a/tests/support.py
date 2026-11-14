"""Documents the tests insert directly, shaped as the validators want them.

The engine builds parties with levela.ledger.transfers (ledger_fields,
v2_fields, stub_party) and recalls with levela.db.seed.start_recall; a test
that writes Atlas by hand to probe a rule starts from these instead, so the
rule under test is the only thing that differs.
"""
from __future__ import annotations

from levela.ledger.transfers import ledger_fields

# The recalled product of the demo scenarios (scenarios/recall_1.json).
PRODUCT = {"batch_code": "B-4471", "product": "Maple Farm milk powder 800 g", "barcode": "0 62345 18803 5",
           "affected_batches": ["B-4471"], "action": "return", "barcode_verified": False, "defect": "seal_fault",
           "checks": []}
# The three checks of a store that has pulled its tins: the batch code read
# under a tin and a till that refused the sale (what `held` needs), and a sold
# count read off the till (what `sold` needs).
HELD_CHECKS = {"batch_seen": "B-4471", "block_test": True}
TILL = {"sold_source": "till_report"}


def recall_doc(recall_id: str = "R", *, units_total: int = 240, **over) -> dict:
    """A recall the close-gate validator accepts."""
    return {"_id": recall_id, **PRODUCT, "source": "manual", "source_signal": None,
            "units_total": units_total, "open_units": units_total, "status": "open", **over}


def party_doc(recall_id: str, party_id: str, shipped_in: int, *, type_: str = "store", tier: int = 1,
              channel: str = "sim", state: str = "pending", **over) -> dict:
    """A party with everything unresolved, as start_recall() creates one."""
    return {"_id": f"{recall_id}:{party_id}", "recall_id": recall_id, "party_id": party_id, "name": party_id,
            "type": type_, "tier": tier, "channel": channel, "state": state, **ledger_fields(shipped_in, PRODUCT),
            **over}
