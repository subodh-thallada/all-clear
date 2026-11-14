"""Spec test 42: governed memory (spec "Borrowed from Memory You Can Govern").

An episode scoped to one party is not retrieved for another. An expired
episode is not retrieved. An invalidated complaint episode is not retrieved,
and the classification that relied on it changes.

No complaint classifier exists in levela.monitoring yet (WS4), so the last
part is tested at the retrieval level: `nearest_defect` below is the
smallest classifier that "relies on" memory (the defect of the nearest
admissible complaint episode), exactly the evidence WS4's classifier cites.
Every check runs on the local-scoring path and, where Vector Search exists,
on `$vectorSearch` too: both take the same filter document.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from levela.db.schema import INDEXES, vector_index_definition
from levela.memory import govern
from levela.memory import retrieve as mem
from levela.memory.embed import HashEmbedder
from levela.memory.episodes import write_episode

NOW = datetime(2026, 11, 14, 17, 0, tzinfo=timezone.utc)
EMB = HashEmbedder()
HABIT = "Answers the phone after 14:00 on weekdays; the manager is in the stock room every morning."
LID = "Tin lid lifts at the seam, powder clumped and smells sour; batch B-4471 printed under the tin."


def _modes(on_atlas):
    return ("local", "auto") if on_atlas else ("local",)


def _contact(db, party_id, *, summary=HABIT, now=NOW, **kw):
    return write_episode(db, EMB, kind="contact", text=summary, summary=summary, party_id=party_id,
                         recall_id="R-OLD", now=now, **kw)


def nearest_defect(db, text, *, mode="local", now=NOW):
    """The defect the nearest admissible complaint episode names, with its id as evidence."""
    hits, _ = mem.retrieve(db, EMB, text, k=1, mode=mode, kind="complaint", now=now)
    return (hits[0]["defect"], hits[0]["_id"]) if hits else (None, None)


def test_spec_42_the_index_filters_and_the_ttl_index_are_declared():
    paths = {f["path"] for f in vector_index_definition(256)["fields"] if f["type"] == "filter"}
    assert {"scope", "valid", "expires_at", "kind"} <= paths
    ttl = [o for coll, keys, o in INDEXES if coll == "episodes" and keys == [("expires_at", 1)]]
    assert ttl and ttl[0]["expireAfterSeconds"] == 0


def test_spec_42_setup_installs_the_ttl_index(db):
    info = db.episodes.index_information()
    assert any(ix.get("expireAfterSeconds") == 0 and ix["key"] == [("expires_at", 1)] for ix in info.values())


def test_spec_42_contact_episodes_are_scoped_to_their_party_and_complaints_to_the_fleet(db):
    ep = _contact(db, "queen-st-grocer")
    assert ep["scope"] == "queen-st-grocer" and ep["valid"] is True and ep["invalidated_by"] is None
    assert ep["expires_at"] == NOW + govern.TTL["contact"]
    complaint = write_episode(db, EMB, kind="complaint", text=LID, summary="faulty lid seal", now=NOW,
                              fields={"defect": "seal_fault"})
    assert complaint["scope"] == govern.FLEET


def test_spec_42_a_party_scoped_episode_is_not_retrieved_for_another_party(db, on_atlas):
    ep = _contact(db, "queen-st-grocer")
    for mode in _modes(on_atlas):
        own, _ = mem.retrieve(db, EMB, HABIT, k=3, mode=mode, scope="queen-st-grocer", now=NOW)
        other, _ = mem.retrieve(db, EMB, HABIT, k=3, mode=mode, scope="lakeside-foods", now=NOW)
        anyone, _ = mem.retrieve(db, EMB, HABIT, k=3, mode=mode, now=NOW)
        assert [h["_id"] for h in own] == [ep["_id"]]
        assert other == [] and anyone == [], "the same text, another party: nothing"


def test_spec_42_check_party_reads_only_the_partys_own_and_the_fleets_episodes(db):
    _contact(db, "queen-st-grocer", summary=HABIT)
    lakeside = {"recall_id": "R-NEW", "party_id": "lakeside-foods", "tier": 1, "profile": HABIT}
    queen = {**lakeside, "party_id": "queen-st-grocer"}
    assert mem.check_party(db, EMB, lakeside, mode="local", now=NOW)["hits"] == []
    [hit] = mem.check_party(db, EMB, queen, mode="local", now=NOW)["hits"]
    assert hit["party_id"] == "queen-st-grocer"


def test_spec_42_an_expired_episode_is_not_retrieved(db, on_atlas):
    fresh = _contact(db, "corner-mart", now=NOW)
    stale = _contact(db, "corner-mart", summary=HABIT + " Closed on Sundays.", now=NOW - timedelta(days=200),
                     ttl=timedelta(days=1))
    assert stale["expires_at"] < NOW
    for mode in _modes(on_atlas):
        hits, _ = mem.retrieve(db, EMB, HABIT, k=5, mode=mode, scope="corner-mart", now=NOW)
        assert [h["_id"] for h in hits] == [fresh["_id"]]
    # The same episode, asked about before it expired, was usable then.
    before, _ = mem.retrieve(db, EMB, HABIT, k=5, mode="local", scope="corner-mart",
                             now=NOW - timedelta(days=199, hours=12))
    assert stale["_id"] in {h["_id"] for h in before}


def test_spec_42_an_invalidated_complaint_is_not_retrieved_and_the_classification_changes(db, on_atlas):
    wrong = write_episode(db, EMB, kind="complaint", text=LID, summary="classified as a foreign object",
                          now=NOW, fields={"defect": "foreign_object", "complaint_id": "c-0007"})
    right = write_episode(db, EMB, kind="complaint", text=LID + " The seal ring is loose.",
                          summary="faulty lid seal", now=NOW, fields={"defect": "seal_fault", "complaint_id": "c-0011"})
    for mode in _modes(on_atlas):
        assert nearest_defect(db, LID, mode=mode) == ("foreign_object", wrong["_id"])
    # A quality lead's finding contradicts c-0007's classification.
    assert govern.invalidate(db, wrong["_id"], by="finding:f-0003", reason="the lead found a lid seal fault", now=NOW)
    assert not govern.invalidate(db, wrong["_id"], by="finding:f-0003"), "already invalid"
    stored = db.episodes.find_one({"_id": wrong["_id"]})
    assert stored["valid"] is False and stored["invalidated_by"] == "finding:f-0003", "the history survives"
    for mode in _modes(on_atlas):
        hits, _ = mem.retrieve(db, EMB, LID, k=5, mode=mode, kind="complaint", now=NOW)
        assert wrong["_id"] not in {h["_id"] for h in hits}
        assert nearest_defect(db, LID, mode=mode) == ("seal_fault", right["_id"])


def test_spec_42_retrieval_is_by_kind(db):
    write_episode(db, EMB, kind="complaint", text=LID, summary="faulty lid seal", now=NOW, fields={"defect": "seal_fault"})
    item = write_episode(db, EMB, kind="item", text="Maple Farm milk powder 800 g, barcode 0 62345 18803 5",
                         summary="item candidate", now=NOW)
    assert mem.retrieve(db, EMB, LID, k=5, mode="local", kind="contact", now=NOW)[0] == []
    hits, _ = mem.retrieve(db, EMB, "Maple Farm milk powder 800 g", k=5, mode="local", kind="item", now=NOW)
    assert [h["_id"] for h in hits] == [item["_id"]]


@pytest.mark.atlas
def test_spec_42_vector_search_applies_the_same_filter(db, on_atlas):
    if not on_atlas:
        pytest.skip("needs Atlas Vector Search")
    ep = _contact(db, "queen-st-grocer")
    assert mem.wait_until_searchable(db, EMB, HABIT, ep["_id"], timeout_s=60, mode="atlas", scope="queen-st-grocer")
    hits, how = mem.retrieve(db, EMB, HABIT, k=3, mode="atlas", scope="lakeside-foods", now=NOW)
    assert how == "atlas" and hits == []
