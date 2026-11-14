"""Memory: episodes of past contacts, found again by Vector Search.

    embed      get_embedder(settings), check_embedder(db, embedder)
    episodes   summarize(...), answered_hour(attempt), write_episode_for_attempt(db, embedder, attempt)
    retrieve   retrieve(...), policy(hits, party), check_party(...), memory_hour(record),
               party_update(record), contacts_to_full_discovery(db, recall_id), wait_until_searchable(...)

Use the module paths (memory.retrieve.retrieve, memory.episodes.summarize):
the submodules are imported here so `from levela import memory` is enough,
and no function is re-exported under a submodule's name.
"""
from . import embed, episodes, retrieve

__all__ = ["embed", "episodes", "retrieve"]
