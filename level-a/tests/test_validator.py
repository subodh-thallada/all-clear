"""Spec test 2 (party validator) and the other rules Atlas enforces on the ledger.

Update a 40-tin party to 60 on the shelf, then to valid counts, then write a
negative bucket, then claim `held` above `held_by_system` with the wrong
`batch_seen`: the first, third and fourth raise a validation error; the
second succeeds. Then the rest of "removal needs evidence" (the till, the
store's own stock record, the block test, the sub-recall's closed state),
spec test 37's validator half, the edge validator and the close gate.
"""
from __future__ import annotations

import pytest
from pymongo.errors import WriteError

from levela.ledger.reconciler import VALIDATION_FAILED, atlas_error
from support import BATCH, party_doc, recall_doc

RID = "RCL-T2"
SID = f"{RID}:harbour-market"
_SUM = {"$add": ["$sold", "$on_shelf", "$held", "$returned", "$forwarded"]}


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


def test_spec_2_party_validator_refuses_an_over_count_a_negative_bucket_and_held_on_the_wrong_batch(db):
    """Spec test 2, step by step, each on the same 40-tin party."""
    db.parties.insert_one(party_doc(RID, "harbour-market", 40))

    err = _refused(lambda: _counts(db, on_shelf=60))
    assert err["codeName"] == "DocumentValidationFailure" and err["errInfo"]["failingDocumentId"] == SID
    assert db.parties.find_one({"_id": SID})["on_shelf"] == 0

    _counts(db, on_shelf=30, returned=10)
    after = db.parties.find_one({"_id": SID})
    assert (after["on_shelf"], after["returned"], after["unresolved"]) == (30, 10, 0)

    _refused(lambda: _counts(db, on_shelf=-5, returned=10))

    _refused(lambda: _counts(db, on_shelf=0, returned=10, held=30, batch_seen="B-4472", block_test=True))
    after = db.parties.find_one({"_id": SID})
    assert (after["on_shelf"], after["held"], after["returned"], after["unresolved"]) == (30, 0, 10, 0)
    assert after["batch_seen"] is None and after["batch_code"] == BATCH


def test_held_needs_the_recalled_batch_read_off_a_tin_and_a_till_that_refused_the_sale(db):
    db.parties.insert_one(party_doc(RID, "corner-mart", 25, tier=3))
    sid = f"{RID}:corner-mart"
    _refused(lambda: _counts(db, sid, held=25))                                           # no evidence at all
    _refused(lambda: _counts(db, sid, held=25, batch_seen=BATCH))                         # no block test
    _refused(lambda: _counts(db, sid, held=25, batch_seen=BATCH, block_test=False))       # the till sold it
    _refused(lambda: _counts(db, sid, held=25, batch_seen="B-1000", block_test=True))     # another batch
    _counts(db, sid, on_shelf=25, batch_seen=BATCH, block_test=False)                      # on the shelf needs nothing
    _counts(db, sid, on_shelf=0, held=25, batch_seen=BATCH, block_test=True)
    assert db.parties.find_one({"_id": sid})["held"] == 25


def test_held_up_to_the_stores_own_record_needs_no_phone_evidence(db):
    """held is allowed up to held_by_system (the store's quarantine moves)
    with no check answers at all; one more needs the batch and the block."""
    db.parties.insert_one(party_doc(RID, "corner-mart", 25, tier=3, on_shelf=25, unresolved=0))
    sid = f"{RID}:corner-mart"
    _counts(db, sid, on_shelf=24, held=1, held_by_system=1)
    _refused(lambda: _counts(db, sid, on_shelf=23, held=2))
    _counts(db, sid, on_shelf=0, held=25, batch_seen=BATCH, block_test=True)
    assert db.parties.find_one({"_id": sid})["held"] == 25


def test_sold_needs_the_till(db):
    db.parties.insert_one(party_doc(RID, "lakeside-foods", 80))
    sid = f"{RID}:lakeside-foods"
    _refused(lambda: _counts(db, sid, sold=20, on_shelf=60))                               # a guess
    _refused(lambda: _counts(db, sid, sold=20, on_shelf=60, sold_source="estimate"))
    _counts(db, sid, sold=20, on_shelf=60, sold_by_till=20)                                # the till's own count
    _refused(lambda: _counts(db, sid, sold=21, on_shelf=59))
    _counts(db, sid, sold=21, on_shelf=59, sold_source="till_report")                      # read from the report
    assert db.parties.find_one({"_id": sid})["sold"] == 21


def test_spec_37_validator_half_all_pulled_is_refused_while_the_stock_record_shows_a_tin_on_the_shelf(db):
    """The till outranks the phone: on_shelf may never drop below
    system_on_shelf, whatever the check answers say."""
    db.parties.insert_one(party_doc(RID, "corner-mart", 25, tier=3, on_shelf=25, unresolved=0, system_on_shelf=1))
    sid = f"{RID}:corner-mart"
    _refused(lambda: _counts(db, sid, on_shelf=0, held=25, batch_seen=BATCH, block_test=True))
    _counts(db, sid, on_shelf=1, held=24, batch_seen=BATCH, block_test=True)
    # The quarantine event moves the last tin: the record no longer shows it on the shelf.
    _counts(db, sid, on_shelf=0, held=25, system_on_shelf=0, held_by_system=1)
    assert db.parties.find_one({"_id": sid})["on_shelf"] == 0


def test_a_closed_sub_recall_has_nothing_on_a_shelf_or_unresolved(db):
    db.parties.insert_one(party_doc(RID, "summit-foods", 120, type_="store", channel="agent", tier=2,
                                    link_id="link-summit", sub={"seq": 0, "parties_total": 0, "parties_open": 0,
                                                                "state": "open", "buyers": {}}))
    sid = f"{RID}:summit-foods"
    _refused(lambda: db.parties.update_one({"_id": sid}, {"$set": {"sub.state": "closed"}}))     # 120 unresolved
    _counts(db, sid, on_shelf=10, sold=110, sold_source="till_report")
    _refused(lambda: db.parties.update_one({"_id": sid}, {"$set": {"sub.state": "closed"}}))     # 10 on a shelf
    _counts(db, sid, on_shelf=0, sold=110, returned=10)
    db.parties.update_one({"_id": sid}, {"$set": {"sub.state": "closed"}})
    assert db.parties.find_one({"_id": sid})["sub"]["state"] == "closed"


def test_party_validator_refuses_a_broken_equation_types_and_enums(db):
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, unresolved=39)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, on_shelf=2.5, unresolved=37.5)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, state="lost")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, channel="fax")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, type_="retailer")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, tier=4)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, block_test="yes")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, held_by_system=-1)))
    for field in ("tier", "type", "on_shelf", "sold"):
        _refused(lambda: db.parties.insert_one({k: v for k, v in party_doc(RID, "harbour-market", 40).items()
                                                if k != field}))
    # Distributors answer through their order system, another node through its agent, the plant is tier 0.
    db.parties.insert_one(party_doc(RID, "dist-north", 300, type_="distributor", channel="order_system"))
    db.parties.insert_one(party_doc(RID, "summit-foods", 120, channel="agent", tier=2))
    db.parties.insert_one(party_doc(RID, "maple-farm-plant", 0, type_="plant", channel="voice", tier=0))
    db.parties.insert_one(party_doc(RID, "own-depot", 0, type_="self", tier=1))
    assert db.parties.count_documents({"recall_id": RID}) == 4


def test_a_party_with_no_batch_code_cannot_hold_on_a_phone_answer(db):
    doc = {k: v for k, v in party_doc(RID, "corner-mart", 25, tier=3).items() if k != "batch_code"}
    db.parties.insert_one(doc)
    sid = f"{RID}:corner-mart"
    _refused(lambda: _counts(db, sid, held=25, batch_seen=BATCH, block_test=True))


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


def test_recall_fields_and_a_recall_from_a_signal_names_the_signal(db):
    _refused(lambda: db.recalls.insert_one(recall_doc("R-S", source="signal")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-X", source="phone")))
    _refused(lambda: db.recalls.insert_one({k: v for k, v in recall_doc("R-M").items() if k != "affected_batches"}))
    _refused(lambda: db.recalls.insert_one({k: v for k, v in recall_doc("R-M").items() if k != "product"}))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-A", action="repair")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-B", barcode_verified="yes")))
    db.recalls.insert_one(recall_doc("R-S", source="signal", source_signal="sig-01"))
    db.recalls.insert_one(recall_doc("R-C", action="destroy", parent={"link_id": "link-1", "recall_id": "RCL-P"},
                                     matched_item={"sku": "MF-800"}))
    db.recalls.insert_one(recall_doc("R-N", action=None))
    assert db.recalls.find_one({"_id": "R-S"})["source_signal"] == "sig-01"


def test_claims_carry_a_known_source(db):
    base = {"recall_id": RID, "party_id": "p", "status": "accepted"}
    for i, source in enumerate(("voice", "sim", "form", "store_system", "agent")):
        db.claims.insert_one({**base, "source": source, "dedupe_key": f"k{i}"})
    _refused(lambda: db.claims.insert_one({**base, "source": "fax", "dedupe_key": "k9"}))
