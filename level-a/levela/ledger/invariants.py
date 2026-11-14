"""The four run invariants (V2 §9; spec "Kills and invariants").

The API's scheduler checks them once per simulated hour and writes the result
to the run; a failure turns the wall's counter red and stops the supervisor.

  1  no party has two live attempts
  2  no idempotency key has two holds in any order system's own log (/_log/holds)
  3  every accepted claim has an evidence event in tool_events
  4  the mass balance equals units_total

Checks 1, 3 and 4 are one aggregation each, read together in one snapshot
transaction: a claim commits its party, edge and claim writes together, so a
check that read them at different moments could report a balance that never
existed and stop the run for nothing. Check 2 reads the order system logs the
caller fetched (`order_system_logs = {order system: holds}`), so the ledger never does HTTP.

Each entry of `passed` is True (held), False (failed) or None (unknown).
Check 2 is unknown when an order system's log could not be read and the logs that
were read show no duplicate: nothing was checked against the unread order system,
so "held" would be a claim the check never made (the wall would show 4 of 4
with the order systems down). Only False is a failure: an unknown check never sets
the sticky invariant_failed, so an unreachable order system does not stop the
supervisor.

A duplicate effect is an extra live attempt for a party or an extra hold under
one key. Each is counted into `runs.duplicates` once, the first time a check
sees it, however many hourly checks see it again.
"""
from __future__ import annotations

from pymongo import ReturnDocument

from ..clock import get_clock, server_now
from ..config import LIVE_ATTEMPT_STATES
from ..db.client import run_txn
from ..runs import run_id
from .claims import EVIDENCE_KINDS
from .reconciler import balance_pipeline

NAMES = ("no party has two live attempts", "no idempotency key has two holds",
         "every accepted claim has a tool event", "mass balance equals units_total")
# Rows kept per failing check in the details written to the run.
DETAIL_ROWS = 20


def _live_attempts(recall_id) -> list:
    """Parties with more than one attempt in intent, dialed or connected."""
    return [
        {"$match": {"recall_id": recall_id, "state": {"$in": list(LIVE_ATTEMPT_STATES)}}},
        {"$sort": {"seq": 1, "_id": 1}},
        {"$group": {"_id": "$party_id", "attempts": {"$push": "$_id"}}},
        {"$match": {"attempts.1": {"$exists": True}}},
        {"$sort": {"_id": 1}},
    ]


def _claims_without_evidence(recall_id) -> list:
    """Accepted claims whose attempt has no evidence event of a claim kind."""
    return [
        {"$match": {"recall_id": recall_id, "status": "accepted"}},
        {"$lookup": {"from": "tool_events", "localField": "attempt_id", "foreignField": "attempt_id", "as": "evidence",
                     "pipeline": [{"$match": {"kind": {"$in": list(EVIDENCE_KINDS)}}}, {"$limit": 1},
                                  {"$project": {"_id": 1}}]}},
        {"$match": {"evidence": {"$size": 0}}},
        {"$project": {"_id": 0, "claim_id": {"$toString": "$_id"}, "party_id": 1, "attempt_id": 1, "source": 1}},
    ]


def _holds_by_key(log) -> dict:
    """{key: [hold, ...]} from one order system's /_log/holds, given either as a list
    of holds that each carry `idempotency_key` (or `key`) or already grouped
    by key. Holds without a key are under None."""
    if isinstance(log, dict) and isinstance(log.get("holds"), list):
        # The mock order system's own shape: {"order_system": name, "holds": [rows]}.
        log = log["holds"]
    elif isinstance(log, dict):
        return {k: (v if isinstance(v, list) else [v]) for k, v in log.items()}
    out: dict = {}
    for hold in log:
        out.setdefault(hold.get("idempotency_key") or hold.get("key"), []).append(hold)
    return out


def _duplicate_holds(order_system_logs: dict) -> tuple[list, list, int]:
    """(keys with more than one hold, order systems whose log could not be read,
    holds that carry no key). A log passed as None was not readable: it is
    named in the details and cannot fail the check, since nothing was seen,
    but it cannot pass it either (check() reports it as unknown)."""
    dups, unreadable, keyless = [], [], 0
    for order_system in sorted(order_system_logs):
        log = order_system_logs[order_system]
        if log is None:
            unreadable.append(order_system)
            continue
        grouped = _holds_by_key(log)
        keyless += len(grouped.pop(None, []))
        for key in sorted(grouped):
            holds = grouped[key]
            if len(holds) > 1:
                ids = sorted(str(h.get("hold_id") or f"#{i}") for i, h in enumerate(holds))
                dups.append({"order_system": order_system, "key": key, "holds": len(holds), "hold_ids": ids})
    return dups, unreadable, keyless


def _effects(live: list, holds: list) -> list[str]:
    """Stable ids for each duplicate effect: every live attempt after a
    party's first, every hold after a key's first."""
    out = [f"attempt:{a}" for row in live for a in row["attempts"][1:]]
    out += [f"hold:{h['order_system']}:{h['key']}:{hid}" for h in holds for hid in h["hold_ids"][1:]]
    return out


def sim_at(db, recall_id) -> float:
    """The recall's simulated hour by the database's clock."""
    return round(get_clock(db, recall_id).sim_now(server_now(db.client)), 4)


def check(db, recall_id, order_system_logs: dict | None = None, *, at: float | None = None) -> dict:
    """Run the four checks. Returns {"at" (simulated hour), "passed" [4 of
    True / False / None for unknown], "details"}. `order_system_logs` maps each
    order system to the holds its /_log/holds returned (None when it could not be
    read, which makes check 2 unknown unless another log shows a duplicate);
    pass {} for a recall with no order systems. Raises ValueError for an unknown
    recall."""
    def read(session):
        return (list(db.attempts.aggregate(_live_attempts(recall_id), session=session)),
                list(db.claims.aggregate(_claims_without_evidence(recall_id), session=session)),
                next(db.recalls.aggregate(balance_pipeline(recall_id), session=session), None))

    live, unbacked, totals = run_txn(db.client, read)
    if totals is None:
        raise ValueError(f"unknown recall {recall_id}")
    holds, unreadable, keyless = _duplicate_holds(order_system_logs or {})
    balance = totals["parties"] + totals["edges"]
    effects = _effects(live, holds)
    details = {
        "live_attempts": [{"party_id": r["_id"], "attempts": r["attempts"]} for r in live[:DETAIL_ROWS]],
        "duplicate_holds": holds[:DETAIL_ROWS],
        "order_systems_checked": sorted(p for p in (order_system_logs or {}) if p not in unreadable),
        "order_systems_unreadable": unreadable, "holds_without_key": keyless,
        "claims_without_evidence": unbacked[:DETAIL_ROWS], "claims_without_evidence_n": len(unbacked),
        "mass_balance": {"balance": balance, "units_total": totals["units_total"],
                         "parties": totals["parties"], "edges": totals["edges"]},
        "duplicate_effects": effects, "duplicates": len(effects),
    }
    # A duplicate seen in any readable log fails check 2 outright; otherwise
    # an unread log leaves it unknown rather than vacuously held.
    holds_check = False if holds else (None if unreadable else True)
    passed = [not live, holds_check, not unbacked, balance == totals["units_total"]]
    return {"at": sim_at(db, recall_id) if at is None else at, "passed": passed, "details": details}


def failed(result: dict) -> bool:
    """Whether any check failed. An unknown check (None) is not a failure."""
    return any(p is False for p in result["passed"])


def describe(result: dict) -> str:
    """The feed line: "4 of 4 hold", "3 of 4 hold, 1 unknown (East orders
    unreadable)", or which checks failed and by how much."""
    d, passed = result["details"], result["passed"]
    text = f"{sum(p is True for p in passed)} of {len(passed)} hold"
    unknown = sum(p is None for p in passed)
    if unknown:
        unread = ", ".join(f"{p.capitalize()} orders" for p in d.get("order_systems_unreadable") or [])
        text += f", {unknown} unknown" + (f" ({unread} unreadable)" if unread else "")
    failures = []
    if passed[0] is False:
        parties = ", ".join(r["party_id"] for r in d["live_attempts"])
        failures.append(f"two live attempts at {parties}")
    if passed[1] is False:
        keys = ", ".join(f"{h['order_system']} {h['key']} ({h['holds']} holds)" for h in d["duplicate_holds"])
        failures.append(f"duplicate holds: {keys}")
    if passed[2] is False:
        failures.append(f"{d['claims_without_evidence_n']} accepted claim(s) without a tool event")
    if passed[3] is False:
        mb = d["mass_balance"]
        failures.append(f"mass balance {mb['balance']} != units_total {mb['units_total']}")
    return text if not failures else f"{text}. FAILED: {'; '.join(failures)}"


def record(db, recall_id, result: dict) -> dict | None:
    """Write a check to the run (one atomic update): last_invariant_check;
    on failure (a check that is False, not one that is unknown) a sticky
    invariant_failed, invariant_failed_at (the first failing hour) and
    planner_wake; and runs.duplicates incremented by the duplicate effects no
    earlier check counted. Returns the run's {duplicates, invariant_failed},
    or None when the recall has no run."""
    effects = sorted(set(result["details"]["duplicate_effects"]))
    last = {"at": result["at"], "passed": list(result["passed"]), "details": result["details"]}
    stages = [
        {"$set": {"_new": {"$setDifference": [{"$literal": effects}, {"$ifNull": ["$duplicate_effects", []]}]}}},
        {"$set": {"last_invariant_check": {"$literal": last},
                  "duplicates": {"$add": [{"$ifNull": ["$duplicates", 0]}, {"$size": "$_new"}]},
                  "duplicate_effects": {"$setUnion": [{"$ifNull": ["$duplicate_effects", []]}, "$_new"]}}},
        {"$unset": "_new"},
    ]
    if failed(result):
        stages.append({"$set": {"invariant_failed": True, "planner_wake": True, "planner_wake_reason": "invariant_failed",
                                "invariant_failed_at": {"$ifNull": ["$invariant_failed_at", result["at"]]}}})
    return db.runs.find_one_and_update({"_id": run_id(recall_id)}, stages,
                                       projection={"_id": 0, "duplicates": 1, "invariant_failed": 1},
                                       return_document=ReturnDocument.AFTER)
