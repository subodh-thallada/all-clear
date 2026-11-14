"""Documents the tests insert directly, shaped as the validators want them.

The engine builds parties with levela.ledger.transfers (ledger_fields,
v2_fields, stub_party) and recalls with levela.db.seed.start_recall; a test
that writes Atlas by hand to probe a rule starts from these instead, so the
rule under test is the only thing that differs.
"""
from __future__ import annotations

from levela.ledger.transfers import ledger_fields

# The recalled product of the demo scenarios (scenarios/recall_1.json).
PRODUCT = {"model": "DEMO-7", "affected_versions": ["2.3", "2.4"], "fixed_version": "2.5"}


def recall_doc(recall_id: str = "R", *, units_total: int = 240, **over) -> dict:
    """A recall the close-gate validator accepts."""
    return {"_id": recall_id, "batch_code": "B-4471", **PRODUCT, "source": "manual", "source_signal": None,
            "units_total": units_total, "open_units": units_total, "status": "open", **over}


def party_doc(recall_id: str, party_id: str, shipped_in: int, *, type_: str = "installer", tier: int = 1,
              channel: str = "sim", state: str = "pending", **over) -> dict:
    """A party with everything unresolved, as start_recall() creates one."""
    return {"_id": f"{recall_id}:{party_id}", "recall_id": recall_id, "party_id": party_id, "name": party_id,
            "type": type_, "tier": tier, "channel": channel, "state": state, **ledger_fields(shipped_in, PRODUCT),
            **over}
