"""Spec test 2 (party validator) and the other rules Atlas enforces on the ledger.

Update a 40-panel party to 60 in stock, then to valid counts, then write a
negative bucket, then claim `fixed` with version 2.4: the first, third and
fourth raise a validation error; the second succeeds. Then the rest of
"fixed needs evidence" (the connected panels, the sounder, the model), the
edge validator and the close gate.
"""
from __future__ import annotations

import pytest
from pymongo.errors import WriteError

from levela.ledger.reconciler import VALIDATION_FAILED, atlas_error
from support import party_doc, recall_doc

RID = "RCL-T2"
SID = f"{RID}:harbour-fire"
_SUM = {"$add": ["$in_stock", "$installed", "$fixed", "$returned", "$forwarded"]}


def _refused(fn):
    with pytest.raises(WriteError) as info:
        fn()
    assert info.value.code == VALIDATION_FAILED
    return atlas_error(info.value)


def _counts(db, sid=SID, **fields):
    """unresolved derived inside the update, exactly as the claim path does it."""
    return db.parties.update_one({"_id": sid}, [
        {"$set": fields},
        {"$set": {"unresolved": {"$subtract": ["$shipped_in", _SUM]}}},
    ])


def test_2_party_validator_refuses_an_over_count_a_negative_bucket_and_fixed_on_an_affected_version(db):
    """Spec test 2, step by step, each on the same 40-panel party."""
    db.parties.insert_one(party_doc(RID, "harbour-fire", 40))

    err = _refused(lambda: _counts(db, in_stock=60))
    assert err["codeName"] == "DocumentValidationFailure" and err["errInfo"]["failingDocumentId"] == SID
    assert db.parties.find_one({"_id": SID})["in_stock"] == 0

    _counts(db, in_stock=30, returned=10)
    assert (db.parties.find_one({"_id": SID})["in_stock"], db.parties.find_one({"_id": SID})["unresolved"]) == (30, 0)

    _refused(lambda: _counts(db, in_stock=-5, returned=10))

    _refused(lambda: _counts(db, in_stock=0, returned=10, fixed=30, version_seen="2.4", sounder_test=True))
    after = db.parties.find_one({"_id": SID})
    assert (after["in_stock"], after["fixed"], after["returned"], after["unresolved"]) == (30, 0, 10, 0)
    assert after["version_seen"] is None


def test_fixed_needs_a_fixed_version_read_off_the_display_and_a_sounder_that_rang(db):
    db.parties.insert_one(party_doc(RID, "owner", 25, type_="owner", tier=3))
    sid = f"{RID}:owner"
    _refused(lambda: _counts(db, sid, fixed=25))                                          # no evidence at all
    _refused(lambda: _counts(db, sid, fixed=25, version_seen="2.5"))                      # no sounder test
    _refused(lambda: _counts(db, sid, fixed=25, version_seen="2.5", sounder_test=False))  # the sounder did not ring
    _refused(lambda: _counts(db, sid, fixed=25, version_seen="2.3", sounder_test=True))   # still affected
    _counts(db, sid, installed=25, version_seen="2.4", sounder_test=False)                 # installed needs nothing
    _counts(db, sid, installed=0, fixed=25, version_seen="2.5", sounder_test=True)
    assert db.parties.find_one({"_id": sid})["fixed"] == 25


def test_the_panels_own_test_outranks_the_phone(db):
    """fixed is allowed up to fixed_by_panel with no phone evidence at all,
    and installed may never drop below the connected panels without a pass."""
    db.parties.insert_one(party_doc(RID, "owner", 25, type_="owner", tier=3, installed=25, unresolved=0,
                                    connected_unfixed=1))
    sid = f"{RID}:owner"
    _refused(lambda: _counts(db, sid, installed=0, fixed=25, version_seen="2.5", sounder_test=True))
    _counts(db, sid, installed=24, fixed=1, connected_unfixed=0, fixed_by_panel=1)        # the panel passed
    _refused(lambda: _counts(db, sid, installed=23, fixed=2))                             # one more needs evidence
    _counts(db, sid, installed=0, fixed=25, version_seen="2.5", sounder_test=True)
    assert db.parties.find_one({"_id": sid})["fixed"] == 25


def test_a_model_read_off_the_panel_must_be_the_recalled_model(db):
    db.parties.insert_one(party_doc(RID, "owner", 25, type_="owner", tier=3))
    sid = f"{RID}:owner"
    _refused(lambda: _counts(db, sid, installed=25, model_seen="DEMO-9"))
    _counts(db, sid, installed=25, model_seen="DEMO-7")
    assert db.parties.find_one({"_id": sid})["model_seen"] == "DEMO-7"


def test_party_validator_refuses_a_broken_equation_types_and_enums(db):
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, unresolved=39)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, in_stock=2.5, unresolved=37.5)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, state="lost")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, channel="fax")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, type_="retailer")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, tier=4)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-fire", 40, sounder_test="yes")))
    for field in ("tier", "type", "affected_versions", "fixed_by_panel", "connected_unfixed"):
        _refused(lambda: db.parties.insert_one({k: v for k, v in party_doc(RID, "harbour-fire", 40).items()
                                                if k != field}))
    # Distributors answer through their order system.
    db.parties.insert_one(party_doc(RID, "dist-north", 300, type_="distributor", channel="order_system"))
    assert db.parties.count_documents({"recall_id": RID}) == 1


def test_edge_validator(db):
    base = {"recall_id": RID, "from_party": "a", "to_party": "b", "sender_qty": 40, "recipient_qty": None,
            "corroborated_qty": 0, "disputed_qty": 0, "state": "awaiting_recipient"}
    db.edges.insert_one({**base, "_id": "e1"})
    _refused(lambda: db.edges.update_one({"_id": "e1"}, {"$set": {"corroborated_qty": 41, "recipient_qty": 41}}))
    _refused(lambda: db.edges.update_one({"_id": "e1"}, {"$set": {"recipient_qty": -1}}))
    _refused(lambda: db.edges.update_one({"_id": "e1"}, {"$set": {"state": "lost"}}))
    db.edges.update_one({"_id": "e1"}, {"$set": {"recipient_qty": 25, "corroborated_qty": 25, "disputed_qty": 15,
                                                 "state": "disputed"}})
    assert db.edges.find_one({"_id": "e1"})["state"] == "disputed"


def test_close_gate_validator(db):
    db.recalls.insert_one(recall_doc(RID, open_units=15))
    err = _refused(lambda: db.recalls.update_one({"_id": RID}, {"$set": {"status": "closed"}}))
    assert "Document failed validation" in err["errmsg"]
    db.recalls.update_one({"_id": RID}, {"$set": {"open_units": 0, "status": "closed"}})
    _refused(lambda: db.recalls.update_one({"_id": RID}, {"$set": {"open_units": 3}}))
    assert db.recalls.find_one({"_id": RID})["status"] == "closed"


def test_a_recall_from_a_signal_names_the_signal(db):
    _refused(lambda: db.recalls.insert_one(recall_doc("R-S", source="signal")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-X", source="phone")))
    _refused(lambda: db.recalls.insert_one({k: v for k, v in recall_doc("R-M").items() if k != "affected_versions"}))
    db.recalls.insert_one(recall_doc("R-S", source="signal", source_signal="sig-01"))
    assert db.recalls.find_one({"_id": "R-S"})["source_signal"] == "sig-01"
