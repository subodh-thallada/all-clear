"""The brief (V2 §6): everything a decider may know for one decision.

    RULES      the rules and this run's tools
    PARTY      name, type, tier, state, shipped_in, ladder step, last result with its time
    EDGES      in / out transfers, each side's claim
    MEMORY     at most 3 lines: this party's failures, then retrieved episodes
    QUESTIONS  at most 3 open questions
    ASK        what to decide, and what this run has already done

It is built by code from the party document, its edges, its open questions
and at most three episodes, never from the run's history or other parties, so
its size does not grow with the run: every list is capped and every line is
cut to a fixed width. `tokens` is len(text) / 4 (a decider that talks to a
model records the model's own input count instead).

`gather()` reads the facts once (a handful of bounded queries) and picks the
run kind; `render()` turns facts into text. The facts travel with the brief
into the trace, so a recorded decision can be replayed by a rules decider
without the database.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import feed
from ..api import questions as questions_
from ..clock import day, fmt
from ..ledger.transfers import resolve_party, stub_party
from ..orchestrator import ladder
from .variables import dispute_key

log = logging.getLogger("levela.harness.brief")

MAX_EDGES = 3          # per direction
MAX_MEMORY = 3
MAX_QUESTIONS = questions_.PER_CALL     # the brief lists the questions the call will ask
LINE_MAX = 220
LIVE = ("intent", "dialed", "connected")
NOT_YET_CLAIMED = ("pending", "contacting", "reached")

RULES = ("You decide the next action for one party of a tin recall. Counts count only when the party "
         "itself gives them and the database accepts them, so never report an outcome yourself. Use only the "
         "tools listed. Make exactly one tool call, or none when nothing is owed now.")
PLANNER_RULES = ("You rank the recall's work. You may set priorities, add a question, escalate a party, propose a "
              "candidate adapter or draft a status report. You may not touch counts, claims, attempts or "
              "operations, and you may not close the recall. Make exactly one tool call, or none when the "
              "ranking already holds.")
ASK = {
    "contact_party": "Contact this party. The ladder asks for {action} at step {step}.",
    "followup": "Follow up the disputed transfer: ask this party to re-check its number ({note}).",
    "ask_question": "Counts are on the books. Ask only the open questions; do not ask for counts again.",
    "order_system_sync": "Bring this distributor's order system in line: next owed operation is {next_op}.",
    "planner": "Rank the work: priority = unresolved + 2 x disputed + 50 when questions are open.",
}


@dataclass
class Brief:
    text: str
    tokens: int
    facts: dict = field(default_factory=dict)

    def to_doc(self) -> dict:
        return {"text": self.text, "tokens": self.tokens, "facts": self.facts}

    @classmethod
    def from_doc(cls, doc: dict) -> "Brief":
        return cls(text=doc.get("text", ""), tokens=int(doc.get("tokens") or 0), facts=dict(doc.get("facts") or {}))


def estimate_tokens(text: str) -> int:
    return int(round(len(text) / 4.0))


def _cut(text: str, width: int = LINE_MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


# ------------------------------------------------------------------ facts

def gather(ctx, party: dict, sim_now: float) -> dict:
    """The facts for one party, and the kind of run it needs now (or None
    with the reason nothing is owed)."""
    rid, sid = party["recall_id"], party["party_id"]
    attempts = list(ctx.db.attempts.find({"recall_id": rid, "party_id": sid},
                                         {"state": 1, "seq": 1, "dispute_key": 1, "purpose": 1}).sort("seq", 1))
    edges = list(ctx.db.edges.find({"recall_id": rid, "$or": [{"from_party": sid}, {"to_party": sid}]}))
    names = _names(ctx.db, rid, {e["from_party"] for e in edges} | {e["to_party"] for e in edges} - {sid})
    facts = {
        "sim_now": round(float(sim_now), 4), "sim_label": fmt(sim_now), "day": day(sim_now),
        "party": _party_view(party),
        "edges": _edge_views(edges, sid, names),
        "memory": _memory_lines(ctx.db, party),
        "questions": open_questions(ctx.db, party),
        "attempts": len(attempts),
        "live_attempt": next((a["_id"] for a in attempts if a["state"] in LIVE), None),
        "dispute": None, "order_system": None, "removal_check": False, "removal_check_at": None,
    }
    kind, reason = _kind(ctx, party, facts, attempts, edges, names)
    facts.update(kind=kind, reason=reason)
    return facts


def _names(db, recall_id: str, party_ids: set) -> dict:
    if not party_ids:
        return {}
    return {s["party_id"]: s["name"] for s in db.parties.find({"recall_id": recall_id, "party_id": {"$in": sorted(party_ids)}},
                                                            {"party_id": 1, "name": 1})}


def _party_view(party: dict) -> dict:
    last = party.get("last_outcome") or {}
    keys = ("party_id", "name", "type", "tier", "found_via", "state", "channel", "shipped_in", "unresolved", "escalation",
            "order_system", "distributor", "account", "priority", "followups")
    view = {k: party.get(k) for k in keys}
    view.update(ladder_step=ladder.clamp(party.get("ladder_step")), dialable=_dialable(party),
                last_outcome=last.get("outcome"), last_at=last.get("at"), open_questions=list(party.get("open_questions") or []))
    return view


def _dialable(party: dict) -> bool:
    return party.get("channel") in ("voice", "sim") and bool(party.get("phone"))


def _edge_views(edges: list, sid: str, names: dict) -> dict:
    def view(e, other):
        return {"edge_id": e["_id"], "other": other, "other_name": names.get(other, other), "sender_qty": e["sender_qty"],
                "recipient_qty": e.get("recipient_qty"), "state": e["state"], "disputed_qty": e.get("disputed_qty", 0)}
    inbound = [view(e, e["from_party"]) for e in edges if e["to_party"] == sid]
    outbound = [view(e, e["to_party"]) for e in edges if e["from_party"] == sid]
    return {"in": inbound[:MAX_EDGES], "out": outbound[:MAX_EDGES],
            "more": max(0, len(inbound) - MAX_EDGES) + max(0, len(outbound) - MAX_EDGES)}


def _memory_lines(db, party: dict) -> list[dict]:
    """This party's own failures first (the spec: "the next brief for that
    party includes it"), then what retrieval found before its first contact."""
    lines = [{"source": "failure", "text": f"{ep.get('failure_class')}: {ep.get('summary', '')}"}
             for ep in db.episodes.find({"kind": "failure", "recall_id": party["recall_id"], "party_id": party["party_id"]},
                                        {"failure_class": 1, "summary": 1}).sort("created_at", -1).limit(MAX_MEMORY)]
    for hit in ((party.get("memory") or {}).get("hits") or []):
        if len(lines) >= MAX_MEMORY:
            break
        text = hit.get("summary") or ", ".join(hit.get("tags") or [])
        if text:
            lines.append({"source": "memory", "text": text})
    return lines[:MAX_MEMORY]


def open_questions(db, party: dict, session=None) -> list[dict]:
    """The open questions the party's next call asks ({id, text, answer_type}),
    at most MAX_QUESTIONS. Read through levela.api.questions, the owner of the
    question schema, so the brief lists exactly what the call's dynamic
    variables carry and what /tools/submit_answer accepts."""
    return [{"id": q["question_id"], "text": q.get("text") or "", "answer_type": q.get("answer_type") or "text"}
            for q in questions_.asked_on_next_call(db, party, session=session)]


def _kind(ctx, party: dict, facts: dict, attempts: list, edges: list, names: dict) -> tuple[str | None, str]:
    """Which kind of run this party needs now (V2 §6 kinds)."""
    if party.get("channel") == "order_system":
        facts["order_system"] = order_system_facts(ctx, party, facts["day"])
        nxt = facts["order_system"]["next_op"]
        return ("order_system_sync", f"order system owes {nxt}") if nxt else (None, facts["order_system"]["why_idle"])
    if facts["live_attempt"]:
        return None, f"attempt {facts['live_attempt']} is still live"
    if party.get("escalation") == "human":
        return None, "handed to a person"
    state, questions = party.get("state"), bool(party.get("open_questions"))
    if state in NOT_YET_CLAIMED:
        if party.get("escalation") == "rep":
            return None, "waiting for the distributor's rep"
        return "contact_party", f"counts not on the books (ladder step {ladder.clamp(party.get('ladder_step'))})"
    owed = [e for e in edges if e["to_party"] == party["party_id"] and e["state"] == "awaiting_recipient"]
    if owed:
        # Its own counts are booked, but a party (another party) says it
        # forwarded stock here: the receipt is still owed (a direct party can
        # receive a transfer too).
        return "contact_party", f"a transfer from {names.get(owed[0]['from_party'], owed[0]['from_party'])} awaits its receipt"
    dispute = open_dispute(party, attempts, edges, names)
    if dispute is not None and _followups_allowed(ctx, party):
        facts["dispute"] = dispute
        return "followup", f"transfer {dispute['edge_id']} is disputed"
    if questions:
        return "ask_question", "open questions"
    if dispute is not None:
        return None, "disputed, follow-ups wait for the operator"
    check_at = party.get("removal_check_at")
    if party.get("on_shelf") and check_at is not None:
        if facts["sim_now"] >= check_at:
            facts["removal_check"] = True
            return "contact_party", f"removal check: {party['on_shelf']} tins still on the shelf"
        facts["removal_check_at"] = check_at
        return None, f"removal check due {fmt(check_at)}"
    return None, "counts on the books; waiting on a counterpart" if state == "claimed" else "reconciled"


def open_dispute(party: dict, attempts: list, edges: list, names: dict) -> dict | None:
    """The first disputed transfer this party has not yet been followed up on
    (once per dispute_key: a corrected claim that leaves a new gap is a new dispute)."""
    sid = party["party_id"]
    for e in edges:
        if e["state"] != "disputed":
            continue
        key = dispute_key(e)
        if any(a.get("dispute_key") == key and a["state"] == "completed" for a in attempts):
            continue
        is_sender = e["from_party"] == sid
        other = e["to_party"] if is_sender else e["from_party"]
        told = e["recipient_qty"] if is_sender else e["sender_qty"]
        note = f"{names.get(other, other)} reported {'receiving' if is_sender else 'sending'} {told}"
        return {"edge_id": e["_id"], "key": key, "other": other, "told": told, "note": note}
    return None


def _followups_allowed(ctx, party: dict) -> bool:
    """auto unless the party's follow-ups are manual and the operator has not
    released them (read fresh: the release happens while the run is going)."""
    mode = party.get("followups") or ((ctx.recall or {}).get("options") or {}).get("followups") or "auto"
    if mode != "manual":
        return True
    recall = ctx.db.recalls.find_one({"_id": party["recall_id"]}, {"followups_released": 1}) or {}
    return bool(recall.get("followups_released"))


PROBE_SETTLED = ("verified", "failed", "needs_reverify")


def order_system_facts(ctx, party: dict, today: int) -> dict:
    """What a distributor's order system still owes, in the fixed order
    place_hold -> confirm_hold (when the active adapter has the step and the
    hold read back pending) -> read_accounts -> the contract probe (after a
    order system switch) -> read_inventory (once per simulated day while any of its
    accounts is unexplained).

    The probe exists because a switch is detected only by an order system call
    (V2 §7 "Detect"): a distributor that is reconciled, or has read its
    inventory today, would otherwise make none for a day or ever. It is
    owed before the daily read and before "reconciled", and it counts as
    that day's inventory read.

    With nothing owed, `idle` says why, which decides the next timer:
    paused (the repair wakes it), unsettled (an operation awaits its
    read-back: look again soon), read_today (tomorrow morning) or
    reconciled (no timer)."""
    rid, sid, order_system = party["recall_id"], party["party_id"], party.get("order_system")
    ops = {}
    for op in ctx.db.operations.find({"recall_id": rid, "party_id": sid},
                                     {"kind": 1, "state": 1, "idempotency_key": 1, "probe": 1,
                                      "readback_value.status": 1}):
        ops.setdefault(op["kind"], []).append({"key": op["idempotency_key"], "state": op["state"],
                                               "probe": op.get("probe"),
                                               "status": (op.get("readback_value") or {}).get("status")})
    adapter = ctx.db.adapters.find_one({"order_system": order_system, "status": "active"},
                                       {"steps": 1, "version": 1, "status_pending": 1}) or {}
    needs_confirm = len(((adapter.get("steps") or {}).get("place_hold")) or []) > 1
    accounts_open = ctx.db.parties.count_documents({"recall_id": rid, "distributor": sid, "state": {"$ne": "reconciled"}})
    inventory_key = f"{rid}:{sid}:read_inventory:d{today}"
    probe = _probe(ctx.db, party, ops.get("read_inventory", []))
    facts = {"order_system": order_system, "adapter_version": adapter.get("version"), "paused": bool(party.get("order_system_paused")),
             "ops": {k: [o["state"] for o in v] for k, v in ops.items()}, "needs_confirm": needs_confirm,
             "accounts_open": accounts_open, "inventory_key": inventory_key, "probe": probe, "next_op": None,
             "idle": None, "why_idle": ""}
    hold, read = ops.get("place_hold"), ops.get("read_accounts")
    confirm = None if not hold or ops.get("confirm_hold") else _confirm_owed(hold[0], adapter, needs_confirm)
    idle = (None, "")
    if facts["paused"]:
        idle = ("paused", "order system work is paused until the adapter is repaired")
    elif not hold:
        facts["next_op"] = "place_hold"
    elif confirm == "owed":
        facts["next_op"] = "confirm_hold"
    elif confirm == "unsettled":
        idle = ("unsettled", f"place_hold is {hold[0]['state']}, waiting for its read-back before the confirm step")
    elif not read:
        facts["next_op"] = "read_accounts"
    elif read[0]["state"] != "verified":
        idle = ("unsettled", f"read_accounts is {read[0]['state']}, waiting for its read-back")
    elif probe and probe["state"] is None:
        facts["next_op"] = "read_inventory"
    elif probe:
        idle = ("unsettled", f"contract probe {probe['n']} is {probe['state']}, waiting for its read-back")
    elif accounts_open and not any(_reads_day(o["key"], inventory_key) for o in ops.get("read_inventory", [])):
        facts["next_op"] = "read_inventory"
    elif accounts_open:
        idle = ("read_today", "inventory already read today")
    else:
        idle = ("reconciled", "every account is reconciled")
    if facts["next_op"] is None:
        facts["idle"], facts["why_idle"] = idle
    return facts


def _confirm_owed(hold: dict, adapter: dict, needs_confirm: bool) -> str | None:
    """Whether the hold needs the active adapter's confirm step: "owed" only
    for a place_hold verified by a read-back that showed it pending (a hold
    already in force, under v1 too, needs no second write), "unsettled"
    while the place_hold still awaits its read-back (there is no verified
    hold to confirm yet), else None."""
    if not needs_confirm or hold["state"] == "failed":
        return None
    if hold["state"] != "verified":
        return "unsettled"
    return "owed" if hold.get("status") in (adapter.get("status_pending") or []) else None


def _reads_day(key: str, inventory_key: str) -> bool:
    """A read_inventory key for this day: the daily read, or a contract
    probe made that day (operations.idempotency_key puts the day first)."""
    return key == inventory_key or key.startswith(f"{inventory_key}:probe")


def _probe(db, party: dict, reads: list[dict]) -> dict | None:
    """The contract probe this distributor still owes after an order system switch:
    {"n", "state"} with state None while no operation exists for it, or the
    state of the one that does. None when nothing is owed.

    The debt is settled once its operation has reached the order system: verified,
    failed, or flagged needs_reverify by contain() (the switch was noticed,
    and the repair's retry finishes the read). Settling clears
    order_system_probe_due, which also takes a reconciled distributor out of the
    due query again. The clear is conditional on the probe number, so a
    switch made meanwhile (n + 1) is never lost, and on the operation's
    state, a fact in Atlas, so any worker may make it."""
    n = int(party.get("order_system_probes") or 0)
    if not party.get("order_system_probe_due") or n < 1:
        return None
    op = next((o for o in reads if o.get("probe") == n), None)
    if op is None:
        return {"n": n, "state": None}
    if op["state"] not in PROBE_SETTLED:
        return {"n": n, "state": op["state"]}
    db.parties.update_one({"_id": party["_id"], "order_system_probes": n, "order_system_probe_due": True},
                         {"$set": {"order_system_probe_due": False}})
    return None


# ------------------------------------------------------------------- planner

def gather_planner(ctx, sim_now: float) -> dict:
    """The planner's one view (V2 §5): parties by state and tier, the ten with
    the most unexplained tins (with the priority the rule wants), the oldest
    due time and the failure classes of the last simulated day."""
    rid = ctx.recall["_id"]
    disputed = {row["_id"]: row["n"] for row in ctx.db.edges.aggregate([
        {"$match": {"recall_id": rid, "state": "disputed"}},
        {"$project": {"ends": ["$from_party", "$to_party"], "q": "$disputed_qty"}},
        {"$unwind": "$ends"}, {"$group": {"_id": "$ends", "n": {"$sum": "$q"}}}])}
    facet = next(ctx.db.parties.aggregate([
        {"$match": {"recall_id": rid}},
        {"$facet": {
            "by_state": [{"$group": {"_id": {"state": "$state", "tier": "$tier"}, "n": {"$sum": 1}}}],
            "top": [{"$match": {"state": {"$ne": "reconciled"}}}, {"$sort": {"unresolved": -1, "party_id": 1}},
                    {"$limit": 10}, {"$project": {"party_id": 1, "name": 1, "unresolved": 1, "priority": 1,
                                                  "questions": {"$size": {"$ifNull": ["$open_questions", []]}}}}],
            "oldest_due": [{"$match": {"next_action_at": {"$ne": None}}}, {"$sort": {"next_action_at": 1}},
                           {"$limit": 1}, {"$project": {"next_action_at": 1}}],
        }}]), {})
    failures = {row["_id"]: row["n"] for row in ctx.db.task_runs.aggregate([
        {"$match": {"recall_id": rid, "failure_class": {"$ne": None}, "ended_sim": {"$gte": sim_now - 24.0}}},
        {"$group": {"_id": "$failure_class", "n": {"$sum": 1}}}])}
    top = []
    for s in facet.get("top", []):
        wanted = int(s.get("unresolved") or 0) + 2 * int(disputed.get(s["party_id"], 0)) + (50 if s["questions"] else 0)
        top.append({"party_id": s["party_id"], "name": s["name"], "unresolved": s.get("unresolved", 0),
                    "disputed": disputed.get(s["party_id"], 0), "priority": int(s.get("priority") or 0), "wanted": wanted})
    oldest = (facet.get("oldest_due") or [{}])[0].get("next_action_at")
    return {"kind": "planner", "reason": "scheduled review", "sim_now": round(float(sim_now), 4), "sim_label": fmt(sim_now),
            "day": day(sim_now), "by_state": sorted([[r["_id"].get("state"), r["_id"].get("tier"), r["n"]]
                                                     for r in facet.get("by_state", [])], key=str),
            "top": top, "oldest_due": oldest, "failures": dict(sorted(failures.items()))}


# ----------------------------------------------------------------- render

def render(facts: dict, *, tools: list, done: list | tuple = ()) -> Brief:
    """The brief text for one decision. `done` is this run's tool results so
    far (at most the run's tool budget, so the text stays bounded)."""
    facts = {**facts, "done": [dict(d) for d in done][:6], "tools": list(tools)}
    sections = _planner_sections(facts) if facts.get("kind") == "planner" else _party_sections(facts)
    text = "\n".join(f"{name:<10} {body}" for name, body in sections)
    return Brief(text=text, tokens=estimate_tokens(text), facts=facts)


def ladder_tool(facts: dict) -> str:
    """The tool the ladder asks for now. A party with no phone can only be
    reached through its form, so its call steps become form links."""
    tool = ladder.action_for(facts["party"]["ladder_step"])
    if tool == ladder.CALL and not facts["party"]["dialable"]:
        return ladder.FORM_LINK
    return tool


def _party_sections(facts: dict) -> list[tuple[str, str]]:
    s = facts["party"]
    last = (f"{s['last_outcome']} at {fmt(s['last_at'])}" if s.get("last_outcome") else "none yet")
    party_line = (f"{s['name']} ({s['party_id']}), {s.get('type')}, tier {s.get('tier')}, via {s.get('found_via')}, state {s.get('state')}, "
                  f"shipped_in {s.get('shipped_in')}, unresolved {s.get('unresolved')}, ladder step {s['ladder_step']}, "
                  f"last result: {last}" + ("" if s["dialable"] else ", no phone (form only)"))
    memory = "; ".join(f"[{m['source']}] {_cut(m['text'], 70)}" for m in facts["memory"]) or "none"
    questions = "; ".join(f"{q['id']} ({q['answer_type']}): {_cut(q['text'], 60)}" for q in facts["questions"]) or "none"
    return [("RULES", _cut(f"{RULES} Tools: {', '.join(facts['tools'])}.", 2 * LINE_MAX)),
            ("PARTY", _cut(party_line)),
            ("EDGES", _cut(_edges_text(facts["edges"]))),
            ("MEMORY", _cut(memory)),
            ("QUESTIONS", _cut(questions)),
            ("ASK", _cut(_ask(facts)))]


def _edges_text(edges: dict) -> str:
    def side(e):
        recip = "none" if e["recipient_qty"] is None else e["recipient_qty"]
        return f"{_cut(e['other_name'], 30)} sender {e['sender_qty']} / recipient {recip} ({e['state']})"
    inbound = ", ".join(side(e) for e in edges["in"]) or "none"
    outbound = ", ".join(side(e) for e in edges["out"]) or "none"
    more = f" (+{edges['more']} more)" if edges.get("more") else ""
    return f"in: {inbound}. out: {outbound}{more}"


def _ask(facts: dict) -> str:
    kind = facts.get("kind")
    step = facts["party"]["ladder_step"]
    template = ASK.get(kind, "Nothing is owed now: make no tool call.")
    text = template.format(action=ladder_tool(facts), step=step,
                           note=(facts.get("dispute") or {}).get("note", ""),
                           next_op=(facts.get("order_system") or {}).get("next_op") or "nothing")
    done = "; ".join(f"{d['tool']} -> {d.get('state') or d.get('outcome')}" for d in facts["done"])
    return f"{text} Done in this run: {done or 'nothing'}."


def _planner_sections(facts: dict) -> list[tuple[str, str]]:
    top = "; ".join(f"{t['party_id']} unresolved {t['unresolved']} disputed {t['disputed']} "
                    f"priority {t['priority']} -> {t['wanted']}" for t in facts["top"][:10])
    states = ", ".join(f"{st}/L{lv}: {n}" for st, lv, n in facts["by_state"][:12])
    fails = ", ".join(f"{k} {v}" for k, v in facts["failures"].items()) or "none"
    done = "; ".join(f"{d['tool']} {d.get('target', '')} -> {d.get('state') or d.get('outcome')}"
                     for d in facts["done"]) or "nothing"
    return [("RULES", _cut(f"{PLANNER_RULES} Tools: {', '.join(facts['tools'])}.", 2 * LINE_MAX)),
            ("PARTIES", _cut(states)),
            ("TOP", _cut(top, 4 * LINE_MAX)),
            ("DUE", _cut(f"oldest due {fmt(facts['oldest_due'])}; failures last day: {fails}")),
            ("ASK", _cut(f"{ASK['planner']} Done in this run: {done}."))]


# ----------------------------------------------------------------- memory

def remember_before_first_contact(ctx, party: dict, sim_now: float) -> dict:
    """Retrieval before a party's first contact, once (parties.memory): the
    episodes found go into the MEMORY section, a `forwards_to` hit on the
    party's own episode suspects the named party now (a stub, due at once),
    and `over_reported` makes the call ask for the delivery note. Returns
    the party with its memory record. Memory never blocks a contact: any
    failure is recorded and the contact goes ahead without it."""
    recall = ctx.recall or {}
    if (ctx.memory is None or not getattr(ctx.settings, "memory_enabled", True)
            or not (recall.get("options") or {}).get("memory") or (party.get("memory") or {}).get("checked_at")
            or party.get("channel") == "order_system"):
        return party
    now = feed.utcnow()
    record = {"checked_at": now, "mode": "off", "hits": [], "actions": []}
    try:
        found = ctx.memory.check_party(recall, party) or {}
        record.update(mode=found.get("mode", "off"), hits=list(found.get("hits") or []),
                      actions=list(found.get("actions") or []))
    except Exception as exc:  # noqa: BLE001 - memory only ever adds work; its outage must not stop a call
        log.warning("memory check for %s failed: %s", party["party_id"], exc)
        record["error"] = str(exc)
    from ..memory.retrieve import party_update      # memory/answered hour on the party, as the worker writes them

    fields = party_update(record)
    won = ctx.db.parties.update_one({"_id": party["_id"], "memory.checked_at": {"$exists": False}},
                                   {"$set": fields}).modified_count == 1
    if not won:
        saved = ctx.db.parties.find_one({"_id": party["_id"]}, {"memory": 1, "memory_hour": 1}) or {}
        return {**party, "memory": saved.get("memory"), "memory_hour": saved.get("memory_hour")}
    if record["hits"]:
        feed.emit(ctx.db, "memory_hit", f"Memory ({record['mode']}): {len(record['hits'])} similar past episodes "
                                        f"for {party['name']}", recall_id=party["recall_id"], party_id=party["party_id"],
                  source="worker", system="memory", sim=sim_now)
    for act in record["actions"]:
        if act.get("action") == "suspect_party":
            _suspect(ctx, party, act, now, sim_now)
    return {**party, **fields}


def _suspect(ctx, party: dict, act: dict, now, sim_now: float) -> None:
    """Upsert the party a party's own forwards_to episode names, under the id a
    claim naming it would use, due at once."""
    name = (act.get("name") or "").strip()
    rid = party["recall_id"]
    to_id, entry = resolve_party(name, (ctx.recall or {}).get("directory"))
    if not name or to_id == party["party_id"]:
        return
    source = {"episode_id": act.get("episode_id"), "from_party": act.get("from_party") or party["party_id"]}
    doc = stub_party(rid, to_id, name, entry, "memory", now, extra={"memory_source": source}, next_action_at=sim_now)
    res = ctx.db.parties.update_one({"recall_id": rid, "party_id": to_id}, {"$setOnInsert": doc}, upsert=True)
    if res.upserted_id is not None:
        feed.emit(ctx.db, "memory_suspect", f"Memory: {party['name']} forwarded to {doc['name']} last time - "
                                            f"{doc['name']} added and due now", recall_id=rid, party_id=to_id,
                  level="good", source="worker", system="memory", sim=sim_now, detail=source)
