"""Lead loose ends: a node with no link secret (AGENT_LINK_SECRET unset).

Summit Foods' party (channel "agent") is due; this node has no secret for the
pairing, so nothing may be sent (an /agent request is never unsigned). The
worker says so once, as a warning, and tries again an hour later: no error
line on every retry, no step on the quiet ladder (a quiet agent would phone
the business, which would change the simulated long run, spec test 16), and
a handoff is never marked sent when nothing went out.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from levela import clock as simclock
from levela import runs
from levela.node import handoff, links
from levela.orchestrator.failpoints import Failpoints, raise_crash
from levela.orchestrator.worker import ERROR_RETRY_H, NO_LINK_SECRET_KIND, WorkerPool
from levela.providers import FakeVoice

from node_support import LINK, RECALL, SUMMIT, seed_parent, summit_party

W1 = "worker-test/w1"


@pytest.fixture
def no_secret(settings, monkeypatch):
    monkeypatch.delenv("AGENT_LINK_SECRET", raising=False)
    monkeypatch.delenv("LINK_SECRET_SUMMIT", raising=False)
    s = replace(settings, agent_link_secret="", lease_s=30)
    assert links.link_secret(s, "summit") == ""
    return s


@pytest.fixture
def pool(client, db, no_secret):
    seed_parent(db)
    runs.create_run(db, RECALL)
    simclock.forget(db)
    voice = FakeVoice()
    p = WorkerPool(no_secret, db, client, providers={"voice": voice, "sim": voice}, probes={},
                   failpoints=Failpoints(db, crash=raise_crash, env={}, worker=no_secret.worker_name), workers=1)
    p.voice = voice
    return p


def _due(db):
    db.parties.update_one({"recall_id": RECALL, "party_id": SUMMIT}, {"$set": {"next_action_at": 0.0}})


def _sim(db):
    return simclock.get_clock(db, RECALL, max_age_s=0).sim_now(simclock.server_now(db.client))


def _lines(db, kind):
    return list(db.feed.find({"kind": kind, "recall_id": RECALL}))


def _two_passes(pool, db) -> list[str]:
    out = []
    for _ in range(2):
        _due(db)                                    # an hour later: due again
        out.append(pool.work_once(W1))
        p = summit_party(db)
        assert p["lease_owner"] is None, "released"
        assert p["next_action_at"] == pytest.approx(_sim(db) + ERROR_RETRY_H, abs=0.05), "the usual retry"
    return out


def test_no_link_secret_is_one_warning_not_an_error_per_retry_and_the_ladder_stays(pool, db, no_secret):
    """Handoff verified, report overdue: quiet.health would sign a request. With
    no secret: one warning line over two passes, no error line, quiet step 0,
    the link still active, nobody phoned."""
    party = summit_party(db)
    op = handoff.intent(SimpleNamespace(db=db, settings=no_secret), party, sim=48.0)
    db.operations.insert_one({**op, "state": "verified", "readback_at": 48.0,
                              "readback_value": {"sub_recall_id": "summit-r-1", "received": 120}})
    db.parties.update_one({"_id": party["_id"]}, {"$set": {"state": "reached", "sub.state": "open",
                                                           "quiet_step": 0}})

    assert _two_passes(pool, db) == ["agent", "agent"]

    [line] = _lines(db, NO_LINK_SECRET_KIND)
    assert line["level"] == "warn" and line["system"] == "agent" and line["party_id"] == SUMMIT
    assert line["text"] == "Summit Foods: no link secret on this node: the sub-recall waits"
    assert _lines(db, "error") == [] and db.feed.count_documents({"level": "bad"}) == 0
    p = summit_party(db)
    assert int(p.get("quiet_step") or 0) == 0 and not p.get("escalated")
    assert db.links.find_one({"_id": LINK})["status"] == "active"
    assert db.attempts.count_documents({}) == 0 and pool.voice.count() == 0


def test_no_link_secret_leaves_the_handoff_unsent(pool, db):
    """No handoff yet: the intent is written, nothing is sent, and the
    operation is not marked sent (a later run with the secret reads back first)."""
    assert _two_passes(pool, db) == ["agent", "agent"]
    [op] = list(db.operations.find({"kind": "handoff"}))
    assert (op["state"], op.get("sends", 0)) == ("intent", 0)
    assert len(_lines(db, NO_LINK_SECRET_KIND)) == 1 and _lines(db, "error") == []


def test_sign_refuses_with_a_typed_error():
    with pytest.raises(links.MissingLinkSecret, match="no link secret configured"):
        links.signed_headers(b"", "")
    assert issubclass(links.MissingLinkSecret, ValueError)
