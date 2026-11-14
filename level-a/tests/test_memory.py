"""Memory: episode summaries and tags, idempotent writes, retrieval, the
policy, the inert history, run 1 teaching run 2, the remembered answering
hour that times ladder step 2, the ablation number, and spec test 11 end to
end (worker pool, harness, memory check and simulator in this process).

Every test uses the local hash embedder and its own scratch database. The
Vector Search test needs Atlas and skips on a plain mongod.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure

from levela import harness
from levela.api.hooks import handle_post_call
from levela.api.main import create_app
from levela.config import LIVE_ATTEMPT_STATES, Settings
from levela.db.schema import ensure_vector_index
from levela.db.seed import load_scenario, start_recall
from levela.harness.decide import RuleDecider
from levela.ledger.claims import submit_claim
from levela.ledger.reconciler import metrics
from levela.ledger.transfers import resolve_party, stub_party
from levela.memory import embed as mem_embed
from levela.memory import episodes as mem_episodes
from levela.memory import retrieve as mem
from levela.memory.embed import HashEmbedder
from levela.memory.episodes import episode_id, summarize, write_episode_for_attempt
from levela.orchestrator.attempts import complete, mark_dialed, open_attempt
from levela.orchestrator.failpoints import Failpoints
from levela.orchestrator.worker import MemoryHooks, WorkerPool, build_decider
from levela.providers import ClientPoster, FakeVoice, SimNetwork, SimProbe
from levela.providers.base import PlacedCall

R1, R2 = "RCL-2026-017", "RCL-2026-018"
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)


class Run:
    """Plays a scenario through the real ledger the way the worker and the
    API would: open an attempt, book each claim, complete it, write its
    episode. The mass balance is asserted after every claim."""

    def __init__(self, client, db, assert_balance, scenario, start=T0):
        self.client, self.db, self.assert_balance = client, db, assert_balance
        self.scn = load_scenario(scenario)
        self.rid = self.scn["recall"]["_id"]
        self.clock = start
        start_recall(client, db, self.scn, all_sim=True, mode="sim", now=self.tick())
        self.attempts: dict[tuple[str, str], dict] = {}

    def tick(self) -> datetime:
        self.clock += timedelta(seconds=1)
        return self.clock

    def claims(self, party_id: str, phase: str = "initial") -> list[dict]:
        return [reply["claim"] for reply in self.scn["sim"][party_id][phase]]

    def call(self, party_id: str, raws: list[dict], *, purpose: str = "initial", state: str = "completed",
             episode: bool = True) -> dict:
        att = open_attempt(self.db, recall_id=self.rid, party_id=party_id, channel="sim", purpose=purpose,
                           now=self.tick())
        mark_dialed(self.db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CA{att['_id']}",
                    now=self.tick())
        for raw in raws:
            submit_claim(self.client, self.db, recall_id=self.rid, party_id=party_id, attempt_id=att["_id"],
                         source="sim", raw=raw, now=self.tick())
            self.assert_balance(self.rid)
        complete(self.db, att["_id"], state, now=self.tick())
        att = self.db.attempts.find_one({"_id": att["_id"]})
        self.attempts[(party_id, purpose)] = att
        if episode:
            write_episode_for_attempt(self.db, HashEmbedder(), att, now=self.tick())
        return att

    def run_1(self) -> "Run":
        """Recall 1 up to the closing gate: 15 tins disputed on Queen St -> Corner Mart."""
        for sid in ("harbour-market", "lakeside-foods", "queen-st-grocer", "corner-mart"):
            self.call(sid, self.claims(sid))
        return self

    def party(self, party_id: str) -> dict:
        return self.db.parties.find_one({"recall_id": self.rid, "party_id": party_id})


def _inputs(db, attempt: dict):
    """What write_episode_for_attempt reads, for calling summarize directly."""
    rid, sid = attempt["recall_id"], attempt["party_id"]
    claims = list(db.claims.find({"attempt_id": attempt["_id"]}).sort([("created_at", 1), ("_id", 1)]))
    party = db.parties.find_one({"recall_id": rid, "party_id": sid})
    edges = list(db.edges.find({"recall_id": rid}))
    names = {s["party_id"]: s["name"] for s in db.parties.find({"recall_id": rid})}
    directory = db.recalls.find_one({"_id": rid})["directory"]
    return claims, party, edges, directory, names


@pytest.fixture
def run1(client, db, assert_balance):
    return Run(client, db, assert_balance, "recall_1").run_1()


# ------------------------------------------------------------- summarize

def test_summarize_harbour_market_over_reported_then_corrected(db, run1):
    att = run1.attempts[("harbour-market", "initial")]
    claims, party, edges, directory, names = _inputs(db, att)
    summary, tags, entities = summarize(att, claims, party, edges, directory=directory, party_names=names)
    assert summary == ("First answer was 60 on the shelf against 40 shipped; "
                       "corrected to 10 sold and 30 held.")
    assert tags == ["answered", "over_reported", "tins_sold", "held"]
    assert "counts_matched" not in tags
    assert entities == {}


def test_summarize_queen_st_forwards_to_corner_mart(db, run1):
    att = run1.attempts[("queen-st-grocer", "initial")]
    ep = db.episodes.find_one({"_id": episode_id(att["_id"])})
    # Written when the call ended, before Corner Mart answered: no dispute yet.
    assert ep["summary"] == "Reported 50 sold, 30 held and 40 forwarded to Corner Mart."
    assert ep["tags"] == ["answered", "counts_matched", "forwards_to", "tins_sold", "held"]
    assert ep["scope"] == "queen-st-grocer", "governed: a contact episode is the party's own"
    assert ep["entities"] == {"forwards_to": "Corner Mart"}


def test_summarize_canonicalises_the_spoken_party_name():
    attempt = {"_id": "att_q", "state": "completed"}
    party = {"party_id": "queen-st-grocer", "name": "Queen St Grocer"}
    claim = {"status": "accepted",
             "raw": {"sold": 50, "held": 30, "forwarded_qty": 40, "forwarded_to_name": " the corner mart "}}
    directory = load_scenario("recall_1")["directory"]
    summary, tags, entities = summarize(attempt, [claim], party, [], directory=directory)
    assert entities == {"forwards_to": "Corner Mart"}
    assert summary == "Reported 50 sold, 30 held and 40 forwarded to Corner Mart."
    # Not in the directory: the stub's name (the first spelling heard) wins, else the words as spoken.
    claim["raw"]["forwarded_to_name"] = "bobs deli"
    assert summarize(attempt, [claim], party, [], party_names={"bobs-deli": "Bob's Deli"})[2] == \
        {"forwards_to": "Bob's Deli"}
    assert summarize(attempt, [claim], party, [])[2] == {"forwards_to": "bobs deli"}


def test_summarize_corner_mart_received_and_disputed(db, run1):
    att = run1.attempts[("corner-mart", "initial")]
    claims, party, edges, directory, names = _inputs(db, att)
    summary, tags, entities = summarize(att, claims, party, edges, directory=directory, party_names=names)
    assert summary == ("Reported 25 received from Queen St Grocer and 25 on the shelf. "
                       "Queen St Grocer reported sending 40, so 15 are disputed.")
    assert tags == ["answered", "counts_matched", "received_transfer", "disputed", "still_on_shelf"]
    assert entities == {"received_from": "Queen St Grocer"}
    # The sender's side of the same dispute, summarised now.
    q_att = run1.attempts[("queen-st-grocer", "initial")]
    q_summary, q_tags, _ = summarize(q_att, *_inputs(db, q_att)[:3], directory=directory, party_names=names)
    assert q_summary.endswith("Corner Mart reported receiving 25, so 15 are disputed.")
    assert "disputed" in q_tags and "forwards_to" in q_tags


def test_summarize_lakeside_sold_and_held(db, run1):
    ep = db.episodes.find_one({"party_id": "lakeside-foods", "recall_id": R1})
    assert ep["summary"] == "Reported 20 sold and 60 held."
    assert ep["tags"] == ["answered", "counts_matched", "tins_sold", "held"]


def test_summarize_outcomes_without_claims():
    party = {"party_id": "lakeside-foods", "name": "Lakeside Foods"}
    no_answer = {"_id": "att_n", "state": "no_answer", "intent_at": T0, "dialed_at": T0 + timedelta(seconds=1),
                 "ended_at": T0 + timedelta(seconds=21)}
    assert summarize(no_answer, [], party, []) == ("No answer after 20 s of ringing.", ["no_answer"], {})
    instant = {**no_answer, "ended_at": no_answer["dialed_at"]}
    assert summarize(instant, [], party, [])[0] == "No answer."
    failed = {"_id": "att_f", "state": "failed", "outcome_reason": "busy"}
    assert summarize(failed, [], party, []) == ("The call failed (busy).", ["failed"], {})
    abandoned = {"_id": "att_a", "state": "abandoned"}
    assert summarize(abandoned, [], party, [])[1] == ["abandoned"]
    silent = {"_id": "att_c", "state": "completed"}
    assert summarize(silent, [], party, []) == ("Answered, but no counts were given.", ["answered"], {})


def test_summarize_never_corrected_goes_to_a_human():
    attempt = {"_id": "att_h", "state": "completed"}
    party = {"party_id": "harbour-market", "name": "Harbour Market", "escalation": "human"}
    over = {"status": "rejected", "code": "over_count", "raw": {"on_shelf": 60}, "reported_total": 60,
            "shipped_in": 40, "reject_reason": "60 reported, 40 shipped"}
    summary, tags, _ = summarize(attempt, [over, dict(over)], party, [])
    assert summary == ("Reported 60 on the shelf against 40 shipped and never corrected it; nothing was booked. "
                       "Handed to a person.")
    assert tags == ["answered", "over_reported", "needs_human"]


def test_summarize_other_rejection_then_accepted():
    attempt = {"_id": "att_d", "state": "completed"}
    party = {"party_id": "harbour-market", "name": "Harbour Market"}
    refused = {"status": "rejected", "code": "delivery_note_required", "raw": {"held": 30},
               "reject_reason": "ask for the delivery note before taking counts"}
    booked = {"status": "accepted", "raw": {"sold": 18, "held": 12, "delivery_note": "DN-51877"}}
    summary, tags, _ = summarize(attempt, [refused, booked], party, [])
    assert summary == ("First answer was refused (ask for the delivery note before taking counts); "
                       "then reported 18 sold and 12 held.")
    assert tags == ["answered", "tins_sold", "held"]  # a rejection on the call, and not an over-count


def test_summarize_refuses_a_live_attempt():
    with pytest.raises(ValueError):
        summarize({"_id": "att_l", "state": "connected"}, [], {"party_id": "x"}, [])


def test_summarize_keeps_the_hour_an_answered_call_was_placed_at():
    """The entity behind ladder step 2's "hour retrieved from memory"."""
    party = {"party_id": "morrow-avenue-alarms", "name": "Morrow Avenue Alarms"}
    answered = {"_id": "att_a", "state": "completed", "channel": "sim", "intent_sim": 38.25}     # d1 14:15
    summary, tags, entities = summarize(answered, [], party, [])
    assert (summary, tags, entities) == ("Answered, but no counts were given.", ["answered"], {"answered_hour": 14})
    not_answered = [{**answered, "state": "no_answer"}, {**answered, "state": "failed"},
                    {**answered, "channel": "form"}, {"_id": "att_old", "state": "completed"},
                    {**answered, "intent_sim": True}]
    for attempt in not_answered:
        assert "answered_hour" not in summarize(attempt, [], party, [])[2], attempt


# ------------------------------------------------------------------ write

def test_write_episode_shape_and_link(db, run1):
    att = run1.attempts[("harbour-market", "initial")]
    ep = db.episodes.find_one({"_id": episode_id(att["_id"])})
    profile = run1.party("harbour-market")["profile"]
    assert ep["_id"] == f"ep_{att['_id']}"
    assert (ep["recall_id"], ep["party_id"], ep["attempt_id"]) == (R1, "harbour-market", att["_id"])
    assert ep["party_profile"] == profile and ep["valid"] is True
    assert ep["embedding_model"] == "hash-256" and len(ep["embedding"]) == 256
    assert ep["embedding"] == pytest.approx(HashEmbedder().embed([profile])[0])
    assert ep["created_at"] is not None
    assert db.attempts.find_one({"_id": att["_id"]})["episode_id"] == ep["_id"]


def test_write_episode_is_idempotent(db, run1):
    att = run1.attempts[("lakeside-foods", "initial")]
    assert write_episode_for_attempt(db, HashEmbedder(), att) is None     # written by the run already
    db.episodes.delete_one({"_id": episode_id(att["_id"])})

    results, gate = [], threading.Barrier(4)

    def write():
        gate.wait()
        results.append(write_episode_for_attempt(db, HashEmbedder(), att))

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert db.episodes.count_documents({"attempt_id": att["_id"]}) == 1


def test_write_episode_skips_live_attempts_and_profileless_parties(client, db, assert_balance):
    run = Run(client, db, assert_balance, "recall_1")
    live = open_attempt(db, recall_id=R1, party_id="harbour-market", channel="sim", now=run.tick())
    assert write_episode_for_attempt(db, HashEmbedder(), live) is None
    complete(db, live["_id"], "no_answer", now=run.tick())

    db.parties.update_one({"recall_id": R1, "party_id": "harbour-market"}, {"$unset": {"profile": ""}})
    assert write_episode_for_attempt(db, HashEmbedder(), live) is None
    assert db.episodes.count_documents({}) == 0
    # Marked as considered, so a worker selecting attempts without episode_id sees it once.
    assert db.attempts.find_one({"_id": live["_id"]})["episode_id"] is None


def test_write_episode_waits_out_an_embedding_outage(db, run1):
    class Down:
        name, dims = "hash-256", 256

        def embed(self, texts):
            raise OSError("connection refused")

    att = run1.attempts[("lakeside-foods", "initial")]
    db.episodes.delete_one({"_id": episode_id(att["_id"])})
    db.attempts.update_one({"_id": att["_id"]}, {"$unset": {"episode_id": ""}})
    assert write_episode_for_attempt(db, Down(), att) is None
    assert "episode_id" not in db.attempts.find_one({"_id": att["_id"]})
    assert write_episode_for_attempt(db, HashEmbedder(), att) is not None


def test_actionable_episodes_are_announced_on_the_wall(db, run1):
    lines = {row["party_id"]: row for row in db.feed.find({"kind": "memory_note"})}
    assert set(lines) == {"harbour-market", "queen-st-grocer"}
    assert lines["queen-st-grocer"]["text"] == \
        "Remembered for next time: Queen St Grocer passes stock on to Corner Mart"
    assert lines["harbour-market"]["detail"]["tags"] == ["over_reported"]


# --------------------------------------------------------------- retrieve

def test_retrieve_local_ranks_the_parties_own_episode_first(db, run1, on_atlas):
    """Governed (spec test 42): a contact episode is retrieved only for its own party."""
    profile = run1.party("harbour-market")["profile"]
    assert mem.retrieve(db, HashEmbedder(), profile, k=3, mode="local")[0] == [], "no fleet contact episodes"
    hits, how = mem.retrieve(db, HashEmbedder(), profile, k=3, mode="local", scope="harbour-market")
    assert how == "local" and len(hits) == 1
    assert hits[0]["party_id"] == "harbour-market" and hits[0]["recall_id"] == R1
    assert hits[0]["score"] == pytest.approx(1.0)
    assert all("embedding" not in h for h in hits)
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    if not on_atlas:
        auto, how = mem.retrieve(db, HashEmbedder(), profile, k=3, scope="harbour-market")
        assert how == "local" and [h["_id"] for h in auto] == [h["_id"] for h in hits]
        with pytest.raises(OperationFailure):
            mem.retrieve(db, HashEmbedder(), profile, mode="atlas")
    with pytest.raises(ValueError):
        mem.retrieve(db, HashEmbedder(), profile, mode="nearest")


def test_local_fallback_scores_only_this_models_vectors(db, run1):
    profile = run1.party("harbour-market")["profile"]
    same_vector = HashEmbedder().embed([profile])[0]
    base = {"recall_id": "RCL-X", "party_id": "harbour-market", "party_profile": profile, "summary": "",
            "tags": ["over_reported"], "entities": {}, "created_at": T0, "kind": "contact",
            "scope": "harbour-market", "expires_at": T0 + timedelta(days=3650), "invalidated_by": None}
    db.episodes.insert_many([
        {**base, "_id": "ep_other_model", "valid": True, "embedding": same_vector, "embedding_model": "voyage-4"},
        {**base, "_id": "ep_other_dims", "valid": True, "embedding": same_vector[:128],
         "embedding_model": "hash-256"},
        {**base, "_id": "ep_invalid", "valid": False, "embedding": same_vector, "embedding_model": "hash-256"},
    ])
    hits, _ = mem.retrieve(db, HashEmbedder(), profile, k=10, mode="local", scope="harbour-market")
    assert not {"ep_other_model", "ep_other_dims", "ep_invalid"} & {h["_id"] for h in hits}
    assert len(hits) == db.episodes.count_documents({"embedding_model": "hash-256", "valid": True,
                                                     "scope": "harbour-market"}) - 1


def test_wait_until_searchable_locally(db, run1, on_atlas):
    if on_atlas:
        pytest.skip("the Atlas variant is test_vector_search_on_atlas")
    att = run1.attempts[("queen-st-grocer", "initial")]
    profile = run1.party("queen-st-grocer")["profile"]
    assert mem.wait_until_searchable(db, HashEmbedder(), profile, episode_id(att["_id"]), timeout_s=5,
                                     scope="queen-st-grocer")
    assert not mem.wait_until_searchable(db, HashEmbedder(), profile, "ep_missing", timeout_s=0,
                                         scope="queen-st-grocer")


# ----------------------------------------------------------------- policy

def _hit(party_id, tags, *, entities=None, score=0.9, ep="ep_1"):
    return {"_id": ep, "party_id": party_id, "recall_id": R1, "tags": tags, "entities": entities or {},
            "score": score}


QUEEN = {"party_id": "queen-st-grocer", "tier": 1}


def test_policy_suspects_the_partner_from_the_parties_own_episode():
    hits = [_hit("queen-st-grocer", ["answered", "forwards_to"], entities={"forwards_to": "Corner Mart"},
                 score=0.99, ep="ep_q")]
    assert mem.policy(hits, QUEEN) == [{"action": "suspect_party", "name": "Corner Mart", "episode_id": "ep_q",
                                        "from_party": "queen-st-grocer", "score": 0.99}]


def test_policy_ignores_an_inherited_forwards_to():
    hits = [_hit("parkdale-grocer", ["forwards_to"], entities={"forwards_to": "Corner Mart"})]
    assert mem.policy(hits, QUEEN) == []


def test_policy_applies_an_inherited_over_reported():
    hits = [_hit("queen-st-grocer", ["counts_matched"], ep="ep_own"),
            _hit("harbour-market", ["over_reported"], ep="ep_h")]
    assert mem.policy(hits, QUEEN) == [{"action": "ask_delivery_note", "episode_id": "ep_h",
                                        "from_party": "harbour-market"}]
    # A downstream party has no shipment record, so there is no delivery note to ask for.
    assert mem.policy(hits, {"party_id": "corner-mart", "tier": 3}) == []


def test_policy_deduplicates_and_needs_a_name():
    hits = [_hit("queen-st-grocer", ["forwards_to", "over_reported"], entities={"forwards_to": "Corner Mart"},
                 ep="ep_a"),
            _hit("queen-st-grocer", ["forwards_to", "over_reported"], entities={"forwards_to": "corner mart"},
                 ep="ep_b"),
            _hit("queen-st-grocer", ["forwards_to"], ep="ep_c")]
    assert [(a["action"], a["episode_id"]) for a in mem.policy(hits, QUEEN)] == \
        [("suspect_party", "ep_a"), ("ask_delivery_note", "ep_a")]


def test_policy_reads_stored_memory_checks_too():
    stored = [{"episode_id": "ep_q", "party_id": "queen-st-grocer", "tags": ["forwards_to"],
               "entities": {"forwards_to": "Corner Mart"}, "score": 1.0}]
    assert mem.policy(stored, QUEEN)[0]["episode_id"] == "ep_q"


def test_policy_remembers_the_parties_own_answering_hour_only():
    """A similar party's opening hours are not this party's: only the party's
    own answered call times its second call, and the most similar one wins."""
    hits = [_hit("harbour-market", ["answered"], entities={"answered_hour": 9}, ep="ep_h"),
            _hit("queen-st-grocer", ["no_answer"], ep="ep_q_miss"),
            _hit("queen-st-grocer", ["answered"], entities={"answered_hour": 14}, ep="ep_q14"),
            _hit("queen-st-grocer", ["answered"], entities={"answered_hour": 16}, ep="ep_q16"),
            _hit("queen-st-grocer", ["answered"], entities={"answered_hour": 25}, ep="ep_bad")]
    actions = mem.policy(hits, QUEEN)
    assert actions == [{"action": "call_hour", "hour": 14, "episode_id": "ep_q14", "from_party": "queen-st-grocer"}]
    assert mem.memory_hour({"actions": actions}) == 14
    assert mem.party_update({"mode": "local", "actions": actions}) == {
        "memory": {"mode": "local", "actions": actions}, "memory_hour": 14}
    # No remembered hour: the check writes only the record, and the ladder keeps its +4 h.
    assert mem.policy(hits[:2], QUEEN) == [] and mem.memory_hour({"actions": []}) is None
    assert mem.party_update({"actions": []}) == {"memory": {"actions": []}}


# ------------------------------------------------------------- check_party

def test_an_empty_memory_triggers_no_action_for_any_demo_party(db):
    scn = load_scenario("recall_1")
    for entry in scn["parties"] + scn["directory"]:
        party = {"party_id": entry["party_id"], "profile": entry["profile"], "tier": entry.get("tier", 1)}
        checked = mem.check_party(db, HashEmbedder(), party)
        assert (checked["hits"], checked["actions"]) == ([], []), entry["party_id"]


def test_run_1_teaches_run_2(client, db, assert_balance):
    Run(client, db, assert_balance, "recall_1").run_1()
    run2 = Run(client, db, assert_balance, "recall_2", start=T0 + timedelta(hours=1))
    directory = db.recalls.find_one({"_id": R2})["directory"]
    q_ep = db.episodes.find_one({"recall_id": R1, "party_id": "queen-st-grocer"})
    h_ep = db.episodes.find_one({"recall_id": R1, "party_id": "harbour-market"})

    queen = mem.check_party(db, HashEmbedder(), run2.party("queen-st-grocer"))
    assert queen["mode"] in ("atlas", "local")
    top = queen["hits"][0]
    assert (top["party_id"], top["recall_id"], top["episode_id"]) == ("queen-st-grocer", R1, q_ep["_id"])
    assert top["score"] == pytest.approx(1.0)
    assert set(top) == {"episode_id", "party_id", "recall_id", "score", "tags", "summary", "entities"}
    suspects = [a for a in queen["actions"] if a["action"] == "suspect_party"]
    assert suspects == [{"action": "suspect_party", "name": "Corner Mart", "episode_id": q_ep["_id"],
                         "from_party": "queen-st-grocer", "score": top["score"]}]
    assert resolve_party(suspects[0]["name"], directory)[0] == "corner-mart"

    harbour = mem.check_party(db, HashEmbedder(), run2.party("harbour-market"))
    assert {"action": "ask_delivery_note", "episode_id": h_ep["_id"], "from_party": "harbour-market"} \
        in harbour["actions"]
    assert not [a for a in harbour["actions"] if a["action"] == "suspect_party"]

    lakeside = mem.check_party(db, HashEmbedder(), run2.party("lakeside-foods"))
    assert not [a for a in lakeside["actions"] if a["action"] == "suspect_party"]
    corner = {**next(d for d in directory if d["party_id"] == "corner-mart"), "tier": 3}
    assert mem.check_party(db, HashEmbedder(), corner)["actions"] == []


class NoAnswerVoice:
    """A phone nobody picks up: the post-call webhook reports no answer
    (through the API's own handler) before place_call returns."""

    name = "no-answer"

    def __init__(self, db):
        self.db, self.calls = db, []

    def place_call(self, *, to_number, dynamic_variables, attempt_id):
        self.calls.append(dynamic_variables["party_id"])
        conv = f"conv_{attempt_id}"
        handle_post_call(self.db, {"type": "call_initiation_failure", "data": {
            "conversation_id": conv, "failure_reason": "no-answer",
            "conversation_initiation_client_data": {"dynamic_variables": {"attempt_id": attempt_id}}}})
        return PlacedCall(conversation_id=conv, call_sid=f"CA{attempt_id}")


def test_the_hour_a_party_answered_in_one_recall_times_its_second_call_in_the_next(client, db, settings,
                                                                                   assert_balance):
    """Spec "Contact ladder" step 2, "call at an hour retrieved from memory":
    Lakeside Foods picked up at 14:20 in recall 1. In recall 2 nobody picks up
    its first call at 09:00, and the second goes at 14:00 that day, not +4 h,
    because the memory check before its first contact put the remembered
    hour on parties.memory_hour (worker pool, real harness, real ladder)."""
    run1 = Run(client, db, assert_balance, "recall_1")
    att = open_attempt(db, recall_id=R1, party_id="lakeside-foods", channel="sim", now=run1.tick())
    db.attempts.update_one({"_id": att["_id"]}, {"$set": {"intent_sim": 14 + 20 / 60}})      # d0 14:20
    mark_dialed(db, att["_id"], conversation_id=f"conv_{att['_id']}", call_sid=f"CA{att['_id']}", now=run1.tick())
    submit_claim(client, db, recall_id=R1, party_id="lakeside-foods", attempt_id=att["_id"], source="sim",
                 raw=run1.claims("lakeside-foods")[0], now=run1.tick())
    complete(db, att["_id"], "completed", now=run1.tick())
    ep = write_episode_for_attempt(db, HashEmbedder(), db.attempts.find_one({"_id": att["_id"]}))
    assert ep["entities"] == {"answered_hour": 14}

    rid = start_recall(client, db, load_scenario("recall_2"), all_sim=True, env={}, emit=False)["recall_id"]
    db.parties.update_many({"recall_id": rid, "party_id": {"$ne": "lakeside-foods"}}, {"$set": {"next_action_at": None}})
    voice = NoAnswerVoice(db)
    pool = WorkerPool(settings, db, client, providers={"sim": voice, "voice": voice},
                      probes={"sim": SimProbe(db), "voice": SimProbe(db)},
                      memory=MemoryHooks(db, HashEmbedder(), mode="local"), harness=harness, decider=RuleDecider(),
                      failpoints=Failpoints(db, env={}, worker=settings.worker_name), workers=1, recall_id=rid)
    assert pool.work_once(f"{settings.worker_name}/w1") == "executed"

    party = db.parties.find_one({"recall_id": rid, "party_id": "lakeside-foods"})
    assert voice.calls == ["lakeside-foods"]
    assert party["memory"]["actions"] == [{"action": "call_hour", "hour": 14, "episode_id": ep["_id"],
                                           "from_party": "lakeside-foods"}]
    assert party["memory_hour"] == 14
    assert party["last_outcome"]["outcome"] == "no_answer"
    assert (party["ladder_step"], party["next_action_at"]) == (2, 14.0), "the second call goes at the remembered hour"


def test_check_party_off_records(db, run1):
    party = run1.party("harbour-market")
    now = T0 + timedelta(days=1)

    class Down:
        name, dims = "hash-256", 256

        def embed(self, texts):
            raise OSError("connection refused")

    cases = [(mem.check_party(db, HashEmbedder(), party, mode="off", now=now), "memory is off for this recall"),
             (mem.check_party(db, None, party, now=now), "no embedder configured"),
             (mem.check_party(db, HashEmbedder(), {**party, "profile": None}, now=now), "party has no profile"),
             (mem.check_party(db, Down(), party, now=now), "embedding failed: connection refused")]
    for record, reason in cases:
        assert record == {"checked_at": now, "mode": "off", "hits": [], "actions": [], "reason": reason}
    with pytest.raises(ValueError):     # a bad mode is a bug, not an outage
        mem.check_party(db, HashEmbedder(), party, mode="nearest")


# -------------------------------------------------------------- ablation

def test_contacts_to_full_discovery(client, db):
    scn = load_scenario("recall_1")
    corner = next(d for d in scn["directory"] if d["party_id"] == "corner-mart")

    def recall(rid, stub_after_s, attempts_at_s):
        start_recall(client, db, scn, all_sim=True, mode="sim", recall_id=rid, now=T0, emit=False)
        for sid, at in zip(["harbour-market", "lakeside-foods", "queen-st-grocer"], attempts_at_s):
            open_attempt(db, recall_id=rid, party_id=sid, channel="sim", now=T0 + timedelta(seconds=at))
        db.parties.insert_one(stub_party(rid, "corner-mart", "Corner Mart", corner, "claim:queen-st-grocer",
                                        T0 + timedelta(seconds=stub_after_s)))
        open_attempt(db, recall_id=rid, party_id="corner-mart", channel="sim",
                     now=T0 + timedelta(seconds=stub_after_s + 1))

    recall("RCL-T-NOMEM", stub_after_s=40, attempts_at_s=[1, 2, 3])   # found by Queen St's claim
    recall("RCL-T-MEM", stub_after_s=0.5, attempts_at_s=[1, 2, 3])    # suspected by memory first
    assert mem.contacts_to_full_discovery(db, "RCL-T-NOMEM") == 3
    assert mem.contacts_to_full_discovery(db, "RCL-T-MEM") == 0
    assert mem.contacts_to_full_discovery(db, "RCL-T-NONE") == 0


# ------------------------------------------------------------- spec test 11

class _System:
    """The API (TestClient, hosting the SimNetwork) and a worker pool with the
    real harness and MemoryHooks over the hash embedder, all in this process,
    as test 10 (tests/e2e_sim.py) runs them. One pool per recall; the
    network outlives the pools, as it outlives a killed worker."""

    def __init__(self, client, db, settings):
        self.client, self.db, self.settings = client, db, settings
        self.api = TestClient(create_app(settings, client=client))
        self.net = SimNetwork(db, ClientPoster(self.api, settings.tool_secret), settings)
        self.api.app.state.sim = self.net
        self.voice = FakeVoice(self.net)
        self.memory = MemoryHooks(db, HashEmbedder())

    def start(self, scenario: str, *, memory: bool, recall_id: str | None = None) -> str:
        body = {"scenario": scenario, "all_sim": True, "mode": "sim", "followups": "manual", "memory": memory,
                **({"recall_id": recall_id} if recall_id else {})}
        res = self.api.post("/admin/recalls", headers={"X-Admin-Token": self.settings.admin_token}, json=body)
        assert res.status_code == 200, res.text
        return res.json()["recall_id"]

    def run(self, recall_id: str, until, what: str, timeout_s: float = 120.0) -> None:
        """Four worker threads on one recall until `until()` holds and nothing is in flight."""
        s = self.settings
        pool = WorkerPool(s, self.db, self.client, providers={"sim": self.voice, "voice": self.voice},
                          probes={"sim": self.voice.probe, "voice": self.voice.probe}, memory=self.memory,
                          failpoints=Failpoints(self.db, env={}, worker=s.worker_name), decider=build_decider(s),
                          harness=harness, workers=4, recall_id=recall_id)
        stop = threading.Event()
        thread = threading.Thread(target=pool.run, args=(stop,), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + timeout_s
            while not (until() and self.quiet(recall_id)):
                assert time.monotonic() < deadline, f"{what}: {self.lines(recall_id)}"
                time.sleep(0.25)
        finally:
            stop.set()
            thread.join(30)

    def quiet(self, recall_id: str) -> bool:
        return (self.db.attempts.count_documents({"recall_id": recall_id,
                                                  "state": {"$in": list(LIVE_ATTEMPT_STATES)}}) == 0
                and self.db.parties.count_documents({"recall_id": recall_id,
                                                    "$expr": {"$gt": ["$lease_until", "$$NOW"]}}) == 0)

    def lines(self, recall_id: str) -> list[str]:
        return [f"{f.get('kind')}: {f['text']}" for f in self.db.feed.find({"recall_id": recall_id}).sort("at", -1)
                .limit(15)]

    def close(self) -> None:
        self.net.stop()
        self.net.wait_idle(10)


def _checked(db, recall_id: str) -> bool:
    """Every store's counts are on the books (the plant, when the recall has
    one, is a voice role and is not part of this test)."""
    stores = {"recall_id": recall_id, "type": "store"}
    done = db.parties.count_documents({**stores, "state": {"$in": ["claimed", "reconciled"]}})
    return done == db.parties.count_documents(stores) == 4


def _corner_mart_vs_queen_st(db, recall_id: str):
    """(Corner Mart's first attempt opened, Queen St Grocer's accepted claim booked)."""
    first = db.attempts.find_one({"recall_id": recall_id, "party_id": "corner-mart"}, sort=[("intent_at", 1)])
    claim = db.claims.find_one({"recall_id": recall_id, "party_id": "queen-st-grocer", "status": "accepted"},
                               sort=[("created_at", 1)])
    assert first is not None and claim is not None, (first, claim)
    return first["intent_at"], claim["created_at"]


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_11_memory_reaches_corner_mart_before_queen_st_grocer_answers(client, db, settings, assert_balance):
    """Spec test 11: run recall 1, then recall 2; repeat recall 2 with memory
    disabled. With memory, Corner Mart's attempt starts before Queen St
    Grocer's claim; without it, after. Every party is simulated and every
    piece is real: the worker pool, the harness, the memory check
    (MemoryHooks), the episodes the maintenance thread writes, and the
    simulator, where Corner Mart's recall-2 reply waits for Queen St
    Grocer's claim (must_follow) exactly as the real party could not know of
    the second batch before Queen St Grocer did."""
    system = _System(client, db, settings)
    try:
        r1 = system.start("recall_1", memory=False)
        system.run(r1, lambda: _checked(db, r1) and db.attempts.count_documents(
            {"recall_id": r1, "episode_id": {"$exists": False}}) == 0, "recall 1 did not settle with its episodes")
        assert_balance(r1)
        queen_ep = db.episodes.find_one({"recall_id": r1, "party_id": "queen-st-grocer", "tags": "forwards_to"})
        assert queen_ep is not None and queen_ep["entities"]["forwards_to"] == "Corner Mart"
        profile = db.parties.find_one({"recall_id": r1, "party_id": "queen-st-grocer"})["profile"]
        assert mem.wait_until_searchable(db, HashEmbedder(), profile, queen_ep["_id"], timeout_s=120,
                                         scope="queen-st-grocer")

        with_memory = system.start("recall_2", memory=True)
        system.run(with_memory, lambda: _checked(db, with_memory), "recall 2 with memory did not settle")
        without = system.start("recall_2", memory=False, recall_id=f"{R2}-NOMEM")
        system.run(without, lambda: _checked(db, without), "recall 2 without memory did not settle")
    finally:
        system.close()

    for rid in (with_memory, without):
        assert_balance(rid)
    corner = db.parties.find_one({"recall_id": with_memory, "party_id": "corner-mart"})
    assert (corner["found_via"], corner["memory_source"]["episode_id"]) == ("memory", queen_ep["_id"])
    attempt_at, claim_at = _corner_mart_vs_queen_st(db, with_memory)
    assert attempt_at < claim_at, "with memory, Corner Mart is called before Queen St Grocer's claim"
    assert db.parties.find_one({"recall_id": without, "party_id": "corner-mart"})["found_via"] == "claim"
    attempt_at, claim_at = _corner_mart_vs_queen_st(db, without)
    assert attempt_at > claim_at, "without memory, Corner Mart is found only by Queen St Grocer's claim"
    assert mem.contacts_to_full_discovery(db, with_memory) < mem.contacts_to_full_discovery(db, without)


# ---------------------------------------------------------------- embedder

def test_get_embedder_is_cached_per_provider_and_model(settings):
    first = mem_embed.get_embedder(settings)
    assert first is mem_embed.get_embedder(settings)
    assert (first.name, first.dims) == ("hash-256", 256)
    assert first.embed(["a party"]) == HashEmbedder().embed(["a party"])
    voyage = mem_embed.get_embedder(Settings.from_env(env={}, embed_provider="voyage", embed_api_key="test-key"))
    assert voyage is not first and voyage.name == "voyage-4" and voyage.dims is None


def test_cached_embedder_memoises_by_text():
    class Counting:
        name, dims = "count-2", 2

        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [[float(len(t)), 1.0] for t in texts]

    inner = Counting()
    cached = mem_embed.CachedEmbedder(inner, maxsize=2)
    assert cached.embed(["ab", "c", "ab"]) == [[2.0, 1.0], [1.0, 1.0], [2.0, 1.0]]
    assert inner.calls == [["ab", "c"]]
    vec = cached.embed(["ab"])[0]
    vec[0] = 99.0                                   # a caller editing its copy
    assert cached.embed(["ab"]) == [[2.0, 1.0]] and len(inner.calls) == 1
    cached.embed(["dddd"])                          # evicts the least recently used: "c"
    cached.embed(["c"])
    assert inner.calls[-1] == ["c"]


def test_check_embedder(db):
    hash_ = HashEmbedder()
    ok, detail = mem_embed.check_embedder(db, hash_)
    assert ok and "no episodes stored yet" in detail

    db.episodes.insert_one({"_id": "e1", "valid": True, "kind": "contact", "embedding_model": "hash-256",
                            "embedding": hash_.embed(["a party"])[0], "created_at": T0})
    assert mem_embed.check_embedder(db, hash_)[0] is True

    db.episodes.insert_one({"_id": "e2", "valid": True, "kind": "contact", "embedding_model": "voyage-4",
                            "embedding": [0.0] * 1024, "created_at": T0 + timedelta(minutes=1)})
    ok, detail = mem_embed.check_embedder(db, hash_)
    assert not ok and "voyage-4" in detail and "hash-256" in detail

    class Voyage:
        """Learns its size from the first response, like memory.embed.VoyageEmbedder."""
        name, dims = "voyage-4", None

        def __init__(self, size=None, error=None):
            self.size, self.error = size, error

        def embed(self, texts):
            if self.error:
                raise self.error
            self.dims = self.size
            return [[0.0] * self.size for _ in texts]

    assert mem_embed.check_embedder(db, Voyage(size=1024))[0] is True
    ok, detail = mem_embed.check_embedder(db, Voyage(size=512))
    assert not ok and "1024" in detail and "512" in detail
    ok, detail = mem_embed.check_embedder(db, Voyage(error=OSError("no route to host")))
    assert not ok and "unreachable" in detail


# ------------------------------------------------------------------ Atlas

@pytest.mark.atlas
def test_vector_search_on_atlas(client, db, assert_balance, on_atlas):
    if not on_atlas:
        pytest.skip("needs Atlas Vector Search (plain mongod has no search process)")
    emb = HashEmbedder()
    run = Run(client, db, assert_balance, "recall_1").run_1()
    built = ensure_vector_index(db, emb.dims)
    assert built["ok"], built
    att = run.attempts[("harbour-market", "initial")]
    profile = run.party("harbour-market")["profile"]
    assert mem.wait_until_searchable(db, emb, profile, episode_id(att["_id"]), timeout_s=120, mode="atlas")
    hits, how = mem.retrieve(db, emb, profile, k=3, mode="atlas")
    assert how == "atlas"
    assert hits[0]["party_id"] == "harbour-market" and hits[0]["score"] == pytest.approx(1.0, abs=1e-3)
    assert all("embedding" not in h for h in hits)
    # Memory is past recalls: the next recall's Queen St Grocer suspects Corner
    # Mart; the same party asked again inside recall 1 learns nothing from it.
    next_recall = {**run.party("queen-st-grocer"), "recall_id": "RCL-2026-018"}
    checked = mem.check_party(db, emb, next_recall, mode="atlas")
    assert checked["mode"] == "atlas"
    assert any(a["action"] == "suspect_party" and a["name"] == "Corner Mart" for a in checked["actions"])
    same = mem.check_party(db, emb, run.party("queen-st-grocer"), mode="atlas")
    assert all(h["recall_id"] != run.party("queen-st-grocer")["recall_id"] for h in same["hits"])


def test_tags_are_the_documented_vocabulary():
    assert mem_episodes.TAGS == ("answered", "no_answer", "failed", "abandoned", "counts_matched",
                                 "over_reported", "forwards_to", "received_transfer", "disputed",
                                 "tins_sold", "tins_returned", "still_on_shelf", "held", "needs_human")
