"""Spec test 20, adapter memory (V2 §7 "Remember").

North's repair costs one model call and is saved as an `adapter` episode
embedded by its failure text. When Distributor East's order system (same vendor)
fails the same way, the search on the failure text returns that episode, its
mapping passes East's own canary, and adapter v2 is activated with 0 model
calls. The episode search is $vectorSearch with the filter
{kind: "adapter", valid: true} on Atlas, and the same vectors scored locally
where no search index exists (the test databases have none).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from levela.memory.embed import get_embedder
from levela.mock.order_system import VENDOR_TITLE
from levela.systems import operations as ops
from levela.systems.repair import ClaudeRepairer, find_episode, remember, request_repair
from test_mock_order_system import EAST, NORTH, V2_MAPPING, V2_PROPOSAL, make_order_system, switch, v2_adapter
from test_order_system_change import FAILURE, _reply
from test_order_system_hold import RID, book_ledger, feed_texts, make_ctx, start


def test_20_east_repairs_from_norths_episode_with_no_model_call(client, db, settings, assert_balance):
    _, north = make_order_system("north", NORTH)
    _, east = make_order_system("east", EAST)
    dists = start(client, db, order_systems=("north", "east"))
    ctx = make_ctx(settings, client, db, {"north": north, "east": east})
    north_ids = book_ledger(db, dists["north"], NORTH, corroborated=("N-009",))
    east_ids = book_ledger(db, dists["east"], EAST, corroborated=("E-007",))
    switch(north)
    switch(east)

    calls = []

    def model(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_reply(V2_PROPOSAL))

    claude = ClaudeRepairer(api_key="sk-test", transport=httpx.MockTransport(model), env={})

    bloor = db.parties.find_one({"recall_id": RID, "party_id": north_ids["N-014"]})
    assert ops.read_inventory(ctx, bloor)["action"] == "contract_failed"
    first = request_repair(ctx, "north", repairer=claude)
    assert (first["status"], first["source"], first["model_calls"]) == ("activated", "model", 1)
    assert len(calls) == 1
    episode = db.episodes.find_one({"kind": "adapter"})
    assert episode["_id"] == first["episode_id"] and episode["valid"] is True
    assert (episode["order_system"], episode["system_vendor"], episode["failure_text"]) == ("north", VENDOR_TITLE, FAILURE)
    assert episode["mapping"] == V2_MAPPING and episode["from_version"] == 1
    assert episode["embedding_model"] == get_embedder(settings).name and episode["embedding"]
    assert db.runs.find_one({"recall_id": RID})["model_calls"] == 1

    danforth = db.parties.find_one({"recall_id": RID, "party_id": east_ids["E-003"]})
    assert ops.read_inventory(ctx, danforth)["action"] == "contract_failed"
    second = request_repair(ctx, "east", repairer=claude)
    assert len(calls) == 1, "East's repair made no model call"
    assert (second["status"], second["source"], second["model_calls"]) == (
        "activated", f"episode {episode['_id']}", 0)
    assert (second["canary"]["account"], second["canary"]["ledger_value"], second["canary"]["order_system_value"]) == (
        "E-007", 110, 110)
    assert db.runs.find_one({"recall_id": RID})["model_calls"] == 1

    v2 = db.adapters.find_one({"_id": "east:v2"})
    assert (v2["status"], v2["source"], v2["model_calls"]) == ("active", f"episode {episode['_id']}", 0)
    assert v2["mapping"] == V2_MAPPING
    assert db.adapters.find_one({"_id": "east:v1"})["status"] == "retired"
    east_feed = [row["text"] for row in db.feed.find({"kind": "adapter_activated", "system": "East orders"})]
    assert east_feed == [f"contract failed. Episode {episode['_id']} retrieved. Adapter v2 active, 0 model calls "
                         "(canary 110 = 110). Retrying 1 operation"]
    (retried,) = second["retried"]
    assert (retried["action"], retried["adapter_version"]) == ("verified", 2)
    assert db.parties.find_one({"_id": danforth["_id"]})["order_system_available"] == 31
    assert db.episodes.count_documents({"kind": "adapter"}) == 1, "a reused repair is not saved twice"
    assert db.episodes.find_one({"_id": episode["_id"]})["reused"] == 1
    assert len(feed_texts(db, "order_system_contract_failed")) == 2
    assert_balance(RID)


def test_an_adapter_episode_serves_only_the_same_vendor_and_old_version(db, settings):
    embedder = get_embedder(settings)
    adapter = v2_adapter("north")
    saved = remember(db, embedder, adapter, {"text": FAILURE}, from_version=1, recall_id=RID,
                     now=datetime.now(timezone.utc))
    assert saved["kind"] == "adapter" and "qty_in_stock -> inventory.available" in saved["summary"]
    hit, how = find_episode(db, embedder, "POST /v1/holds answered 410 Gone (API v1 was retired)",
                            vendor=VENDOR_TITLE, from_version=1)
    assert hit["_id"] == saved["_id"] and how in ("atlas", "local") and "embedding" not in hit
    assert find_episode(db, embedder, FAILURE, vendor="Another vendor's order system", from_version=1)[0] is None
    assert find_episode(db, embedder, FAILURE, vendor=VENDOR_TITLE, from_version=2)[0] is None
