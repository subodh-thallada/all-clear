"""Cross-workstream: every writer of episodes must write governed episodes.

levela.memory.retrieve admits only episodes in a readable scope whose
`expires_at` is in the future (spec "Borrowed from Memory You Can Govern").
The complaint classifier (spec 38) and the sub-recall item matcher (spec 44)
write their own episodes; on Atlas the `$vectorSearch` path has no local
fallback, so an ungoverned episode is silently never retrieved. These tests
use the same governed filter locally.
"""
from __future__ import annotations

from levela.memory import retrieve
from levela.memory.embed import HashEmbedder
from levela.monitoring import classify
from levela.node import match


def test_spec_38_complaint_episodes_pass_the_governed_filter(db):
    emb = HashEmbedder()
    made = classify.seed_history(db, emb)
    assert made, "the classifier seeds confirmed past complaints"
    hits, how = retrieve.retrieve(db, emb, "lid was lifted, powder clumped and smelled sour",
                                  k=5, mode="local", kind="complaint")
    assert how == "local" and len(hits) == 5
    assert all(h["kind"] == "complaint" and h["scope"] == "fleet" for h in hits)


def test_spec_38_null_expiry_episodes_are_repaired_on_seed(db):
    emb = HashEmbedder()
    classify.seed_history(db, emb)
    db.episodes.update_many({"kind": "complaint"}, {"$set": {"expires_at": None}})
    classify.seed_history(db, emb)
    assert db.episodes.count_documents({"kind": "complaint", "expires_at": None}) == 0


def test_spec_44_item_episodes_pass_the_governed_filter(db):
    emb = HashEmbedder()
    items = [{"item_id": "SKU-1", "description": "Maple Farm milk powder 800 g tin", "barcode": "0 62345 18803 5"},
             {"item_id": "SKU-2", "description": "Maple Farm oat drink 1 L", "barcode": "0 62345 11111 1"}]
    match.load_items(db, emb, items, node_id="summit")
    hits, _ = retrieve.retrieve(db, emb, "milk powder 800 g", k=2, mode="local", kind="item")
    assert {h["item_id"] for h in hits} == {"SKU-1", "SKU-2"}
