"""Classifying a complaint (spec test 38, translated: fault -> complaint,
part -> defect), and findings correcting the memory it relied on."""
from __future__ import annotations

import pytest

from levela.memory.embed import HashEmbedder
from levela.monitoring import classify
from levela.db.schema_store import STORE_COLLECTIONS
from levela.db.setup import setup
from support import BATCH

EMB = HashEmbedder()


@pytest.fixture(autouse=True)
def _store_schema(db):
    """The store collections with their validators and indexes, even if an
    earlier test in the session dropped them."""
    setup(db, STORE_COLLECTIONS)


def _complaint(db, cid, text, code=None, store="Spadina Foods"):
    return classify.receive(db, {"complaint_id": cid, "batch_code": BATCH, "bought_at": store,
                                 "reason_code": code, "text": text})


def _no_model(*_a, **_k):
    raise AssertionError("the model (decider) was called")


def test_spec_38_known_code_maps_with_no_model_call(db, monkeypatch):
    """Spec 38: a complaint with a known reason code maps to its defect with no model call."""
    monkeypatch.setattr(classify, "nearest_complaints", _no_model)
    _complaint(db, "c-1", "the lid", code="seal")
    cls = classify.classify(db, "c-1", embedder=EMB, decider=_no_model)
    assert cls["defect"] == "seal_fault" and cls["method"] == "reason_code" and cls["model_calls"] == 0
    assert db.complaints.find_one({"_id": "c-1"})["classification"]["defect"] == "seal_fault"


def test_spec_38_free_text_cites_episodes_that_exist(db):
    """Spec 38: a free-text complaint returns a defect with evidence ids that exist."""
    classify.seed_history(db, EMB)
    _complaint(db, "c-0012", "lid was lifted, powder clumped and smelled sour")
    cls = classify.classify(db, "c-0012", embedder=EMB)
    assert cls["defect"] == "seal_fault" and cls["confidence"] >= classify.CONFIDENCE_MIN
    assert cls["evidence"] and len(cls["neighbours"]) == classify.K
    found = db.episodes.count_documents({"_id": {"$in": cls["evidence"]}, "kind": "complaint", "valid": True})
    assert found == len(cls["evidence"])
    assert db.questions.count_documents({"complaint_id": "c-0012"}) == 0


def test_spec_38_evidence_that_does_not_exist_is_refused(db):
    """The handler checks every id: a decider citing an unknown episode records no defect."""
    classify.seed_history(db, EMB)
    _complaint(db, "c-2", "seal was broken and the powder was lumpy")
    made_up = lambda text, hits: {"defect": "seal_fault", "confidence": 0.99, "evidence": ["e-9999"]}  # noqa: E731
    cls = classify.classify(db, "c-2", embedder=EMB, decider=made_up)
    assert cls["defect"] is None and cls["question_id"]


def test_spec_38_low_confidence_records_no_defect_and_asks(db):
    """Spec 38: a low-confidence complaint records no defect and adds a question."""
    classify.seed_history(db, EMB)
    _complaint(db, "c-3", "my baby did not like the taste")
    cls = classify.classify(db, "c-3", embedder=EMB)
    assert cls["defect"] is None and cls["confidence"] < classify.CONFIDENCE_MIN
    q = db.questions.find_one({"_id": cls["question_id"]})
    assert q["text"] == "What was wrong with this tin?" and q["to"] == "quality_lead" and q["status"] == "open"
    assert db.complaints.find_one({"_id": "c-3"})["classification"]["defect"] is None


def test_atlas_refuses_a_defect_with_no_grounds(db):
    """Validator: classification.defect needs a reason code or an evidence id."""
    from pymongo.errors import WriteError
    _complaint(db, "c-4", "odd")
    with pytest.raises(WriteError):
        db.complaints.update_one({"_id": "c-4"}, {"$set": {"classification": {
            "defect": "seal_fault", "confidence": 0.9, "evidence": []}}})


def test_finding_replaces_a_wrong_classification_and_invalidates_its_memory(db):
    """A person's finding replaces a wrong classification; the episodes it
    relied on are marked valid: false and are not retrieved again."""
    classify.seed_history(db, EMB)
    _complaint(db, "c-5", "seal was broken and the powder was lumpy")
    first = classify.classify(db, "c-5", embedder=EMB)
    assert first["defect"] == "seal_fault"
    relied = first["evidence"]
    cls = classify.record_finding(db, "c-5", defect="foreign_body", by="quality-lead", embedder=EMB)
    assert cls["defect"] == "foreign_body" and cls["invalidated"] == relied
    assert db.episodes.count_documents({"_id": {"$in": relied}, "valid": False}) == len(relied)
    hits, _ = classify.nearest_complaints(db, EMB, "seal was broken and the powder was lumpy", k=10)
    assert not set(relied) & {h["_id"] for h in hits}
    assert db.episodes.find_one({"_id": "e-c-5"})["defect"] == "foreign_body"


def test_finding_answers_the_quality_lead(db):
    classify.seed_history(db, EMB)
    _complaint(db, "c-6", "my baby did not like the taste")
    cls = classify.classify(db, "c-6", embedder=EMB)
    classify.record_finding(db, "c-6", defect="seal_fault", by="quality-lead", embedder=EMB)
    assert db.questions.find_one({"_id": cls["question_id"]})["status"] == "answered"
    assert db.complaints.find_one({"_id": "c-6"})["classification"]["evidence"] == ["e-c-6"]
