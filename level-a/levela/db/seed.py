"""Load a scenario, start a recall from it, reset run-time state.

start_recall() is what POST /admin/recalls does: one transaction inserts the
recall (its model, affected and fixed versions and the three checks come from
the scenario and are copied onto every party), the shipment records and the
parties on them (tier 1). Hidden parties are not
inserted; they live in the recall's `directory` (copied from the scenario,
with real phone numbers resolved from the environment) and appear when a
claim or a memory names them.

Recall options (stored on the recall document, read by the worker and the
simulator):

  mode       "demo"  demo_hold replies wait for /admin/release; voice parties
                     ring real phones.
             "sim"   every scripted reply runs on its timer.
  followups  "auto"   the worker opens dispute follow-ups as soon as an edge
                      is disputed.
             "manual" follow-ups wait for /admin/followups (the demo needs the
                      dispute to stay open until the closing-gate beat).
  memory     true/false: run Vector Search before contacting a party.

The long run (recall_long, V2 §8) adds a `schedule` (sim_start, speed,
hold_slice_in_demo, order_system_change_at) copied onto the run, distributors on
the shipment list with channel "order_system", and directory entries carrying
`slice` and `tier`. Slice parties (and slice directory entries, such as
Corner Properties) wait for the operator in demo mode when the schedule says so;
`hold_slice` overrides the schedule either way.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import feed
from ..clock import DEFAULT_SECONDS_PER_SIM_HOUR, DEFAULT_SIM_START, server_now
from ..clock import forget as forget_clocks
from ..config import SCENARIO_DIR
from ..ledger.transfers import ledger_fields, v2_fields
from ..runs import create_run
from .client import run_txn
from .schema import PARTY_TYPES

# A real phone number, as the voice provider dials it.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
RUNTIME_COLLECTIONS = ["shipments", "parties", "edges", "claims", "attempts", "tool_events",
                       "operations", "task_runs", "traces", "questions"]


class ScenarioError(Exception):
    """A scenario that cannot start a recall as asked."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def scenario_path(name: str):
    return SCENARIO_DIR / f"{name}.json"


def load_scenario(name_or_path) -> dict:
    """A scenario by name (scenarios/<name>.json) or by path."""
    path = Path(str(name_or_path))
    if not path.exists():
        path = scenario_path(str(name_or_path))
    return json.loads(path.read_text(encoding="utf-8"))


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIO_DIR.glob("*.json"))


def _contact(entry: dict, sim: bool, env) -> tuple[str, str | None]:
    """(channel, phone). Real numbers come from the environment, never the repo.
    Distributors answer through their order system and have no phone."""
    channel = entry.get("channel", "sim")
    if channel == "order_system":
        return "order_system", None
    if channel == "voice" and sim:
        return "sim", entry.get("sim_phone") or entry.get("phone")
    if channel == "voice":
        phone = entry.get("phone") or env.get(entry.get("phone_env", ""), "")
        if not E164.match(phone or ""):
            raise ScenarioError(
                f"{entry['name']} is a voice party: set {entry.get('phone_env')} to an E.164 "
                f"number such as +14165550100, or simulate it (all_sim / sim_parties)")
        return "voice", phone
    return channel, entry.get("phone")


def _identity(entry: dict, default_tier: int) -> dict:
    """A scenario entry's type (distributor, installer or owner) and tier
    (1 on the shipment records, 2 an order system's account, 3 named by a
    claim). Every entry names its type: the checks a call asks depend on it."""
    if entry.get("type") not in PARTY_TYPES:
        raise ScenarioError(f"{entry.get('name')} needs a type: one of {', '.join(PARTY_TYPES)}")
    return {"type": entry["type"], "tier": int(entry.get("tier", default_tier))}


def _hold(mode: str, schedule: dict, hold_slice: bool | None) -> bool:
    """Whether slice parties wait for /admin/release: the caller's choice when
    given, else the scenario's `hold_slice_in_demo` in demo mode."""
    if hold_slice is not None:
        return bool(hold_slice)
    return mode == "demo" and bool(schedule.get("hold_slice_in_demo"))


def start_recall(client, db, scenario: dict, *, all_sim=False, sim_parties=(), mode=None, followups=None,
                 memory=True, recall_id=None, env=None, now=None, emit=True,
                 seconds_per_sim_hour: float | None = None, hold_slice: bool | None = None,
                 source: str = "manual", source_signal: str | None = None, session=None) -> dict:
    """Insert the recall, its shipments, the parties on the shipment list and
    its run (with the clock) in one transaction. `now` defaults to the
    database's clock: the run's clock is anchored on it, and on the test
    clock a second of laptop skew would be twenty simulated hours.

    `source` is "manual" (the operator started it) or "signal" with the
    approved signal's id in `source_signal`. With `session` the writes join
    the caller's transaction (a signal's approval and its recall commit
    together) instead of opening one."""
    env = os.environ if env is None else env
    now = now or server_now(client)
    mode = mode or ("sim" if all_sim else "demo")
    if mode not in ("demo", "sim"):
        raise ScenarioError("mode must be demo or sim")
    followups = followups or ("manual" if mode == "demo" else "auto")
    if followups not in ("auto", "manual"):
        raise ScenarioError("followups must be auto or manual")
    sim_set = set(sim_parties or ())
    r = scenario["recall"]
    rid = recall_id or r["_id"]
    schedule = dict(scenario.get("schedule") or {})
    sim_start = float(schedule.get("sim_start", DEFAULT_SIM_START))
    speed = float(seconds_per_sim_hour or schedule.get("seconds_per_sim_hour") or DEFAULT_SECONDS_PER_SIM_HOUR)
    hold = _hold(mode, schedule, hold_slice)

    def slice_fields(entry: dict) -> dict:
        is_slice = bool(entry.get("slice"))
        return {"slice": is_slice, "hold_for_demo": is_slice and hold,
                "followups": followups if is_slice else "auto"}

    directory = []
    for entry in scenario.get("directory", []):
        channel, phone = _contact(entry, all_sim or entry["party_id"] in sim_set, env)
        directory.append({k: v for k, v in {**entry, "channel": channel, "phone": phone, **_identity(entry, 3),
                                            **slice_fields(entry)}.items() if k != "phone_env"})

    shipments, parties = [], []
    for s in scenario["parties"]:
        channel, phone = _contact(s, all_sim or s["party_id"] in sim_set, env)
        sid, qty = s["party_id"], int(s["shipped_in"])
        identity = _identity(s, 1)
        shipments.append({
            "_id": f"shp:{rid}:{sid}", "recall_id": rid, "party_id": sid, "qty": qty,
            "delivery_note": s.get("delivery_note"), "shipped_at": now - timedelta(days=9),
            "synthetic": True,
        })
        extra = slice_fields(s)
        parties.append({
            **v2_fields(tier=identity["tier"], found_via="shipment", next_action_at=sim_start,
                        slice_=extra["slice"], hold_for_demo=extra["hold_for_demo"], followups=extra["followups"]),
            **({"order_system": s["order_system"]} if s.get("order_system") else {}),
            "_id": f"{rid}:{sid}", "recall_id": rid, "party_id": sid, "name": s["name"], "type": identity["type"],
            **ledger_fields(qty, r),
            "state": "pending", "channel": channel, "phone": phone,
            "profile": s.get("profile"), "discovered_by": "shipment",
            "form_token": secrets.token_urlsafe(6), "created_at": now,
        })
    if sum(s["shipped_in"] for s in parties) != r["units_total"]:
        raise ScenarioError("the shipment records do not add up to units_total")
    if source not in ("manual", "signal") or (source == "signal") != bool(source_signal):
        raise ScenarioError("source is manual, or signal with the approved signal's id")

    recall = {**r, "_id": rid, "open_units": r["units_total"], "status": "open", "opened_at": now,
              "source": source, "source_signal": source_signal,
              "scenario_id": scenario["scenario_id"], "synthetic": True, "mode": mode,
              "options": {"all_sim": bool(all_sim), "sim_parties": sorted(sim_set), "followups": followups,
                          "memory": bool(memory)},
              "directory": directory, "followups_released": False}

    def txn(session):
        db.recalls.insert_one(recall, session=session)
        db.shipments.insert_many(shipments, session=session)
        db.parties.insert_many(parties, session=session)
        create_run(db, rid, real_start=now, seconds_per_sim_hour=speed, sim_start=sim_start,
                   parties_at_start=len(parties), schedule=schedule, session=session)

    if session is None:
        run_txn(client, txn)
    else:
        txn(session)
    forget_clocks(db)
    if emit:
        feed.emit(db, "recall_started",
                  f"Recall {rid} opened: batch {r['batch_code']}, {r['units_total']} {r['model']} panels "
                  f"shipped to {len(parties)} parties ({mode} mode, memory {'on' if memory else 'off'})",
                  recall_id=rid, detail={"options": recall["options"], "mode": mode, "source": source,
                                         "source_signal": source_signal})
    return {"recall_id": rid, "mode": mode, "options": recall["options"],
            "parties": [{"party_id": s["party_id"], "channel": s["channel"], "form_token": s["form_token"]}
                       for s in parties]}


def reset(db) -> dict:
    """Remove every recall and everything recorded under it: the ledger, the
    attempts, the operations, the runs, the episodes, the adapters, the feed,
    the simulator's call log and the worker's bookkeeping.

    Run between rehearsals: an episode left over from a rehearsal makes run 1
    find Corner Properties through memory at second zero.

    This is the database's half. The mock order systems keep their version and
    holds in their own processes, so its callers (POST /admin/reset and
    `python -m levela reset`) also reset every configured order system
    (levela.api.order_admin.reset_all): the adapters deleted here start
    again at the builtin v1, which an order system left on v2 would refuse at day 0.
    """
    removed = {name: db[name].delete_many({}).deleted_count for name in RUNTIME_COLLECTIONS}
    for name in ("recalls", "episodes", "runs", "adapters", "capacity", "feed", "sim_calls", "worker_state"):
        removed[name] = db[name].delete_many({}).deleted_count
    return removed
