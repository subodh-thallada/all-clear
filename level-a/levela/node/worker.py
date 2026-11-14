"""What the parent's worker does for a party answered by another node's agent.

    act(ctx, party, *, voice, sim=None) -> dict

One call per due party with channel "agent" (the dispatch loop's hook):

  no handoff yet, or one left in intent/sent  ->  levela.node.handoff (read back first)
  handoff verified, report overdue            ->  levela.node.quiet (the ladder)
  sub-recall reported closed                  ->  nothing (next_action_at cleared)

ctx: db, client, settings, http (reaches the peer), failpoints (or None).
"""
from __future__ import annotations

from ..clock import get_clock, server_now
from ..systems import operations
from . import handoff, quiet


def act(ctx, party: dict, *, voice, sim: float | None = None) -> dict:
    db = ctx.db
    sim = get_clock(db, party["recall_id"]).sim_now(server_now(ctx.client)) if sim is None else sim
    op = db.operations.find_one({"idempotency_key": handoff.key_for(party["recall_id"], party["party_id"])})
    if op is None:
        return handoff.run(ctx, party, sim=sim)
    if op["state"] in operations.OPEN_STATES:
        return handoff.resolve(ctx, op, sim=sim)
    if (party.get("sub") or {}).get("state") == "closed":
        db.parties.update_one({"_id": party["_id"]}, {"$set": {"next_action_at": None}})
        return {"action": "settled"}
    return quiet.step(db, ctx.settings, ctx.http, voice, party, sim=sim, worker=getattr(ctx.settings, "worker_name",
                                                                                         "node"))
