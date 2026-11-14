"""Documents the tests insert directly, shaped as the validators want them.

The engine builds parties with levela.ledger.transfers (ledger_fields,
v2_fields, stub_party) and recalls with levela.db.seed.start_recall; a test
that writes Atlas by hand to probe a rule starts from these instead, so the
rule under test is the only thing that differs.
"""
from __future__ import annotations

from levela.ledger.transfers import ledger_fields
from levela.ledger.transfers import stub_party as _stub_party

# The recalled product of the demo scenarios (scenarios/recall_1.json).
BATCH = "B-4471"
PRODUCT = {"product": "Maple Farm milk powder 800 g", "barcode": "0 62345 18803 5", "batch_code": BATCH,
           "affected_batches": [BATCH], "action": "return", "barcode_verified": False, "defect": "seal_fault"}
# The three check answers that let a phone claim mark tins held (batch read
# off a tin, the till refused the scan) and count sold from the till report.
HELD_EVIDENCE = {"batch_seen": BATCH, "block_test": True}
SOLD_EVIDENCE = {"sold_source": "till_report"}


def recall_doc(recall_id: str = "R", *, units_total: int = 240, **over) -> dict:
    """A recall the close-gate validator accepts."""
    return {"_id": recall_id, **PRODUCT, "source": "manual", "source_signal": None,
            "units_total": units_total, "open_units": units_total, "status": "open", **over}


def party_doc(recall_id: str, party_id: str, shipped_in: int, *, type_: str = "store", tier: int = 1,
              channel: str = "sim", state: str = "pending", **over) -> dict:
    """A party with everything unresolved, as start_recall() creates one.
    `over` replaces any field, e.g. on_shelf=25, unresolved=0, held_by_system=3."""
    return {"_id": f"{recall_id}:{party_id}", "recall_id": recall_id, "party_id": party_id, "name": party_id,
            "type": type_, "tier": tier, "channel": channel, "state": state, **ledger_fields(shipped_in, PRODUCT),
            **over}


def stub_party(recall_id: str, party_id: str, name: str | None = None, entry: dict | None = None,
               discovered_by: str = "memory", now=None, extra: dict | None = None, *, product: dict | None = None,
               **kw) -> dict:
    """levela.ledger.transfers.stub_party with the demo product by default
    (its batch code copied on). Same positional order and keywords."""
    return _stub_party(recall_id, party_id, name or party_id, entry, discovered_by, now, extra,
                       product=PRODUCT if product is None else product, **kw)
