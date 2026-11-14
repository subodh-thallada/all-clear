"""Force one real call to a party now: the operator's button for a store a
caller just named (Ryan's Store), when no worker has dialled it yet.

    python scripts/force_call.py --recall RCL-... --party ryans-store [--dry-run]

It does what a worker's place_call does, with the same pieces: the agent's
dynamic variables (levela.harness.variables: who sent the tins, the receipt
question, the three checks, the open questions), an attempt in Atlas in state
intent (the partial unique index refuses it if the party already has a live
call, so this never doubles a worker's call), the ElevenLabs outbound call,
then `dialed` with the conversation id and Twilio call SID. From there the
API books the counts the agent sends (POST /tools/submit_count carries this
attempt id) and the post-call webhook closes the attempt, exactly as for a
worker's call. The number comes from the party document (from .env), never
from the command line.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from levela import feed  # noqa: E402
from levela.api import questions as questions_  # noqa: E402
from levela.clock import fmt, get_clock, server_now  # noqa: E402
from levela.config import Settings, load_dotenv  # noqa: E402
from levela.db.client import get_client  # noqa: E402
from levela.harness.variables import dynamic_variables  # noqa: E402
from levela.orchestrator import attempts as attempts_  # noqa: E402
from levela.orchestrator.claim import slots  # noqa: E402
from levela.providers.base import ProviderError  # noqa: E402
from levela.providers.voice import ElevenLabsVoice, mask_number  # noqa: E402


def build(db, recall: dict, party: dict, sim_now: float) -> dict:
    """The attempt document a worker would write for this party's next call."""
    pid = party["party_id"]
    edges = list(db.edges.find({"recall_id": recall["_id"], "$or": [{"from_party": pid}, {"to_party": pid}]}))
    inbound = [e for e in edges if e["to_party"] == pid]
    owed = [e for e in inbound if e.get("state") == "awaiting_recipient"]
    receipt = party.get("tier") != 1 or bool(owed)
    sender_edge = (owed or inbound)[0] if inbound and receipt else None
    sender = ""
    if sender_edge:
        s = db.parties.find_one({"recall_id": recall["_id"], "party_id": sender_edge["from_party"]}, {"name": 1})
        sender = (s or {}).get("name", "")
    history = list(db.attempts.find({"recall_id": recall["_id"], "party_id": pid}).sort("seq", -1))
    purpose = "retry" if history else "initial"
    variables = dynamic_variables(recall, party, purpose, sender_name=sender, receipt=receipt)
    aid = attempts_.new_attempt_id()
    variables.update({"attempt_id": aid, "ladder_step": str(party.get("ladder_step") or 1),
                      "attempt_number": str(len(history) + 1), "sim_time": fmt(sim_now)})
    variables.update(questions_.call_variables(db, party, take_counts=True))
    return attempts_.build_attempt(
        db, recall_id=recall["_id"], party_id=pid, channel=party["channel"], purpose=purpose,
        owner={"worker": "operator", "epoch": None}, to_number=party.get("phone"), context=variables,
        attempt_id=aid, extra={"idempotency_key": f"force:{aid}", "forced_by": "scripts/force_call.py",
                               "intent_sim": round(sim_now, 4)})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recall", required=True)
    p.add_argument("--party", required=True, help="party id, e.g. ryans-store")
    p.add_argument("--dry-run", action="store_true", help="print the variables; write and dial nothing")
    p.add_argument("--ignore-slot", action="store_true", help="dial even if every real line is busy")
    args = p.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    client = get_client(settings, appname="levela-force-call")
    db = client[settings.db_name]
    recall = db.recalls.find_one({"_id": args.recall})
    party = db.parties.find_one({"recall_id": args.recall, "party_id": args.party})
    if recall is None or party is None:
        print(f"no party {args.party} in recall {args.recall}", file=sys.stderr)
        return 2
    if party.get("channel") != "voice" or not party.get("phone"):
        print(f"{party['name']} is not a phone party (channel {party.get('channel')}); nothing to dial", file=sys.stderr)
        return 2
    sim_now = get_clock(db, args.recall).sim_now(server_now(client))
    doc = build(db, recall, party, sim_now)
    shown = {k: v for k, v in doc["context"].items() if k not in ("attempt_id",)}
    print(f"{party['name']}: {doc['purpose']} call to {mask_number(party['phone'])}")
    print(json.dumps(shown, indent=2, ensure_ascii=False, default=str))
    if args.dry_run:
        return 0
    free = slots(db)
    if free["free"] < 1 and not args.ignore_slot:
        print(f"every real line is busy ({free}); wait, or pass --ignore-slot", file=sys.stderr)
        return 3
    try:
        attempts_.insert_intent(db, doc)
    except attempts_.AttemptRefused as exc:
        print(f"refused by Atlas: {exc}", file=sys.stderr)
        return 4
    try:
        placed = ElevenLabsVoice(settings).place_call(to_number=doc["to_number"], dynamic_variables=doc["context"],
                                                      attempt_id=doc["_id"])
    except ProviderError as exc:
        attempts_.complete(db, doc["_id"], "failed", reason=f"force_call: {exc}")
        print(f"call failed: {exc}", file=sys.stderr)
        return 5
    attempts_.mark_dialed(db, doc["_id"], conversation_id=placed.conversation_id, call_sid=placed.call_sid)
    feed.emit(db, "call_placed", f"Calling {party['name']} (attempt {doc['_id']}, {doc['purpose']}, forced by the "
              f"operator): call {placed.call_sid or '?'}", recall_id=args.recall, party_id=args.party,
              source="operator", system="phone", sim=sim_now,
              detail={"attempt_id": doc["_id"], "forced": True})
    print(f"dialled: attempt {doc['_id']}, conversation {placed.conversation_id}, call {placed.call_sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
