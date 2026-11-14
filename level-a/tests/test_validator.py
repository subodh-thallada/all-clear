"""Spec test 2 (party validator) and the other rules Atlas enforces on the ledger.

Update a 40-tin party to 60 on the shelf, then to valid counts, then write a
negative bucket, then claim `held` with the wrong batch read off a tin: the
first, third and fourth raise a validation error; the second succeeds. Then
the rest of "removal needs evidence" (the store's own stock record, the till
test, the till's sales report), the sub-recall rule, the edge validator and
the close gate. Every rule is checked on update; the insert checks are below.
"""
from __future__ import annotations

import pytest
from pymongo.errors import WriteError

from levela.ledger.reconciler import VALIDATION_FAILED, atlas_error
from support import HELD_CHECKS, TILL, party_doc, recall_doc

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


def test_2_party_validator_refuses_an_over_count_a_negative_bucket_and_held_with_the_wrong_batch(db):
    """Spec test 2, step by step, each on the same 40-tin party."""
    db.parties.insert_one(party_doc(RID, "harbour-market", 40))

    err = _refused(lambda: _counts(db, on_shelf=60))
    assert err["codeName"] == "DocumentValidationFailure" and err["errInfo"]["failingDocumentId"] == SID
    assert db.parties.find_one({"_id": SID})["on_shelf"] == 0

    _counts(db, sold=10, held=30, **TILL, **HELD_CHECKS)
    after = db.parties.find_one({"_id": SID})
    assert (after["sold"], after["held"], after["unresolved"]) == (10, 30, 0)

    _refused(lambda: _counts(db, sold=10, held=35, returned=-5))

    _refused(lambda: _counts(db, sold=10, held=30, batch_seen="B-4480"))
    after = db.parties.find_one({"_id": SID})
    assert (after["sold"], after["held"], after["returned"], after["unresolved"]) == (10, 30, 0, 0)
    assert after["batch_seen"] == "B-4471"


def test_held_needs_the_batch_read_off_a_tin_and_a_till_that_refused_the_sale(db):
    db.parties.insert_one(party_doc(RID, "store", 25, tier=3))
    sid = f"{RID}:store"
    _refused(lambda: _counts(db, sid, held=25))                                          # no evidence at all
    _refused(lambda: _counts(db, sid, held=25, batch_seen="B-4471"))                     # no till test
    _refused(lambda: _counts(db, sid, held=25, batch_seen="B-4471", block_test=False))   # the till sold it
    _refused(lambda: _counts(db, sid, held=25, batch_seen="B-4480", block_test=True))    # another batch
    _counts(db, sid, on_shelf=25, batch_seen="B-4480", block_test=False)                 # on the shelf needs nothing
    _counts(db, sid, on_shelf=0, held=25, **HELD_CHECKS)
    assert db.parties.find_one({"_id": sid})["held"] == 25


def test_sold_needs_a_count_read_off_the_till(db):
    db.parties.insert_one(party_doc(RID, "store", 25, tier=3, sold_by_till=4))
    sid = f"{RID}:store"
    _counts(db, sid, sold=4, on_shelf=21)                                                # the till already shows 4
    _refused(lambda: _counts(db, sid, sold=10, on_shelf=15))                             # 6 more, no till report
    _refused(lambda: _counts(db, sid, sold=10, on_shelf=15, sold_source="estimate"))
    _counts(db, sid, sold=10, on_shelf=15, **TILL)
    assert db.parties.find_one({"_id": sid})["sold"] == 10


def test_the_stores_own_stock_record_outranks_the_phone(db):
    """held is allowed up to held_by_system with no phone evidence at all, and
    on_shelf may never drop below what the stock record shows on the shelf
    (spec test 37's rule)."""
    db.parties.insert_one(party_doc(RID, "store", 25, tier=3, on_shelf=25, unresolved=0, system_on_shelf=1))
    sid = f"{RID}:store"
    _refused(lambda: _counts(db, sid, on_shelf=0, held=25, **HELD_CHECKS))              # "all pulled"
    _counts(db, sid, on_shelf=24, held=1, system_on_shelf=0, held_by_system=1)           # one quarantine move
    _refused(lambda: _counts(db, sid, on_shelf=23, held=2))                              # one more needs evidence
    _counts(db, sid, on_shelf=0, held=25, **HELD_CHECKS)
    assert db.parties.find_one({"_id": sid})["held"] == 25


def test_a_closed_sub_recall_has_nothing_on_a_shelf_or_unresolved(db):
    db.parties.insert_one(party_doc(RID, "summit-foods", 120, tier=2, channel="agent",
                                    sub={"seq": 1, "state": "open"}))
    sid = f"{RID}:summit-foods"
    _counts(db, sid, sold=31, on_shelf=9, held=58, returned=22, held_by_system=58, sold_by_till=31,
            system_on_shelf=9)
    _refused(lambda: db.parties.update_one({"_id": sid}, {"$set": {"sub.state": "closed"}}))
    _counts(db, sid, on_shelf=0, held=67, held_by_system=67, system_on_shelf=0)
    db.parties.update_one({"_id": sid}, {"$set": {"sub.state": "closed"}})
    _refused(lambda: _counts(db, sid, on_shelf=1, held=66, held_by_system=66, system_on_shelf=1))
    assert db.parties.find_one({"_id": sid})["sub"]["state"] == "closed"


def test_party_validator_refuses_a_broken_equation_types_and_enums_on_insert(db):
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, unresolved=39)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, sold=2.5, unresolved=37.5)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, state="lost")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, channel="fax")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, type_="owner")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, tier=4)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, block_test="yes")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, sold_source="memory")))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, held=40, unresolved=0)))
    _refused(lambda: db.parties.insert_one(party_doc(RID, "harbour-market", 40, on_shelf=-1, unresolved=41)))
    for field in ("tier", "type", "batch_code", "sold", "held_by_system", "sold_by_till", "system_on_shelf"):
        _refused(lambda: db.parties.insert_one({k: v for k, v in party_doc(RID, "harbour-market", 40).items()
                                                if k != field}))
    # The plant is tier 0; distributors answer through their order system; a
    # node's own depot is `self`; a store with its own copy answers as an agent.
    db.parties.insert_one(party_doc(RID, "plant", 0, type_="plant", tier=0, channel="voice"))
    db.parties.insert_one(party_doc(RID, "dist-north", 300, type_="distributor", channel="order_system"))
    db.parties.insert_one(party_doc(RID, "depot", 18, type_="self", tier=1))
    db.parties.insert_one(party_doc(RID, "summit-foods", 120, tier=2, channel="agent"))
    assert db.parties.count_documents({"recall_id": RID}) == 4


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


def test_a_recall_holds_the_product_and_names_its_signal(db):
    _refused(lambda: db.recalls.insert_one(recall_doc("R-S", source="signal")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-X", source="phone")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-A", action="recycle")))
    _refused(lambda: db.recalls.insert_one(recall_doc("R-B", barcode_verified="no")))
    for field in ("product", "barcode", "affected_batches", "action", "barcode_verified", "defect", "checks"):
        _refused(lambda: db.recalls.insert_one({k: v for k, v in recall_doc("R-M").items() if k != field}))
    db.recalls.insert_one(recall_doc("R-S", source="signal", source_signal="sig-01"))
    db.recalls.insert_one(recall_doc("R-N", action=None))     # the action waits for a person's confirmation
    assert db.recalls.find_one({"_id": "R-S"})["source_signal"] == "sig-01"
