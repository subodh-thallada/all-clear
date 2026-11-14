"""Command line for All Clear.

    python -m levela api [--host 0.0.0.0 --port 8000]
    python -m levela worker [--name worker-1] [--workers 4] [--recall RCL-2026-017]
    python -m levela setup-db [--search-indexes]
    python -m levela start recall_1 [--all-sim] [--sim-party corner-mart] [--mode demo|sim]
                                    [--followups auto|manual] [--no-memory] [--recall-id ID]
    python -m levela reset                          (resets the mock order systems too)
    python -m levela close RECALL_ID
    python -m levela release RECALL_ID PARTY_ID
    python -m levela followups RECALL_ID
    python -m levela arm after_dial_request [--party corner-mart] [--channel voice]
    python -m levela disarm
    python -m levela count-kill [--recall RCL-2026-020] [--exit-code 137]
    python -m levela status | metrics RECALL_ID | preflight

Settings come from the environment and .env (see .env.example).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def _ctx():
    from .config import Settings
    from .db.client import get_db
    settings = Settings.from_env()
    client, db = get_db(settings, appname="levela-cli")
    return settings, client, db


def _print(obj):
    print(json.dumps(obj, indent=1, default=str))


def count_kill(db, recall_id: str | None = None, *, exit_code: int = 137) -> dict:
    """Count one worker death as a kill survived, as scripts/supervisor.py
    does when the worker it runs dies with exit 137: runs.kills + 1 and the
    supervisor's own feed line. The kill beat (scripts/kill_demo.sh, .ps1)
    runs the worker without the supervisor, so it calls this when the
    failpoint kills it; the wall's "Kills survived" and the restarted
    worker's "restart n" line then count the kill the room just watched.

    `recall_id` defaults to the newest open run-time recall (the supervisor's
    rule). Raises LookupError when there is no such recall or it has no run."""
    from pymongo import ReturnDocument

    from . import feed, runs

    if recall_id is None:
        recall = db.recalls.find_one({"status": "open"}, {"_id": 1},
                                     sort=[("opened_at", -1)])
        if recall is None:
            raise LookupError("no open run-time recall to count the kill against; pass --recall")
        recall_id = recall["_id"]
    after = db.runs.find_one_and_update({"_id": runs.run_id(recall_id)}, {"$inc": {"kills": 1}},
                                        projection={"kills": 1}, return_document=ReturnDocument.AFTER)
    if after is None:
        raise LookupError(f"recall {recall_id} has no run to count the kill on")
    kills = int(after.get("kills") or 0)
    text = f"worker died with exit {exit_code} ({kills})"
    feed.emit(db, "supervisor", text, recall_id=recall_id, level="warn", source="supervisor", system="supervisor",
              detail={"kills": kills, "exit_code": exit_code, "by": "kill_demo"})
    return {"recall_id": recall_id, "kills": kills, "text": text}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m levela", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("api")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8000)
    w = sub.add_parser("worker")
    w.add_argument("--name", default=None)
    w.add_argument("--workers", type=int, default=None, help="worker threads (default 4)")
    w.add_argument("--recall", default=None, help="serve only this recall (default: every open recall)")
    vi = sub.add_parser("setup-db")
    vi.add_argument("--search-indexes", action="store_true",
                    help="also build the episodes vector index and the party name index (Atlas only)")
    s = sub.add_parser("start")
    s.add_argument("scenario")
    s.add_argument("--all-sim", action="store_true")
    s.add_argument("--sim-party", action="append", default=[])
    s.add_argument("--mode", choices=["demo", "sim"], default=None)
    s.add_argument("--followups", choices=["auto", "manual"], default=None)
    s.add_argument("--no-memory", action="store_true")
    s.add_argument("--recall-id", default=None)
    sub.add_parser("reset")
    c = sub.add_parser("close")
    c.add_argument("recall_id")
    rl = sub.add_parser("release")
    rl.add_argument("recall_id")
    rl.add_argument("party_id")
    f = sub.add_parser("followups")
    f.add_argument("recall_id")
    ar = sub.add_parser("arm")
    ar.add_argument("name")
    ar.add_argument("--party", default=None)
    ar.add_argument("--channel", default=None)
    sub.add_parser("disarm")
    ck = sub.add_parser("count-kill", help="count a worker death as a kill survived (the kill beat)")
    ck.add_argument("--recall", default=None, help="the recall whose run counts it (default: newest open)")
    ck.add_argument("--exit-code", type=int, default=137, help="the worker's exit code, for the feed line")
    sub.add_parser("status")
    m = sub.add_parser("metrics")
    m.add_argument("recall_id")
    sub.add_parser("preflight")
    cap = sub.add_parser("calls", help="how many real phone calls may be live at once (the demo uses 1)")
    cap.add_argument("--max-live", type=int, default=None, help="set it; omit to show the current value")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")

    if args.cmd == "api":
        import uvicorn
        uvicorn.run("levela.api.main:create_app", factory=True, host=args.host, port=args.port,
                    log_level="info")
        return

    if args.cmd == "worker":
        from dataclasses import replace
        from .config import Settings
        from .orchestrator.worker import DEFAULT_WORKERS
        from .orchestrator.worker import main as worker_main
        settings = Settings.from_env()
        if args.name:
            settings = replace(settings, worker_name=args.name)
        worker_main(settings, workers=args.workers or DEFAULT_WORKERS, recall_id=args.recall)
        return

    settings, client, db = _ctx()

    if args.cmd == "setup-db":
        from .db.schema import ensure_party_name_index
        from .db.setup import ensure_vector_index, setup
        _print(setup(db))
        if args.search_indexes:
            from .memory.embed import embedder_dims, get_embedder
            _print(ensure_vector_index(db, embedder_dims(get_embedder(settings))))
            _print(ensure_party_name_index(db))
    elif args.cmd == "start":
        from .db.seed import ScenarioError, load_scenario, start_recall
        try:
            out = start_recall(client, db, load_scenario(args.scenario), all_sim=args.all_sim,
                               sim_parties=args.sim_party, mode=args.mode, followups=args.followups,
                               memory=not args.no_memory, recall_id=args.recall_id)
        except ScenarioError as exc:
            sys.exit(str(exc))
        base = settings.webhook_base
        print(f"started {out['recall_id']} ({out['mode']} mode, options {out['options']})")
        for st in out["parties"]:
            print(f"  {st['party_id']:18} {st['channel']:5} form: {base}/form/{st['form_token']}")
    elif args.cmd == "reset":
        # The mock order systems keep their version and holds in memory: reset them
        # with the database, or the next run meets a v2 order system at day 0.
        from .api import order_admin
        from .db.seed import reset
        removed = reset(db)
        http = order_admin.LazyHttp()
        try:
            order_systems = order_admin.reset_all(http, settings.order_urls, settings.admin_token)
        finally:
            http.close()
        _print({"removed": removed, "order_systems": order_systems})
        for name in order_admin.unreset(order_systems):
            print(f"order system {name} was not reset ({order_systems[name]['error']}): restart it before the next run",
                  file=sys.stderr)
    elif args.cmd == "close":
        from . import feed, runs
        from .ledger.reconciler import close_line, try_close
        res = try_close(db, args.recall_id)
        kind, text, level = close_line(res)
        feed.emit(db, kind, text, recall_id=args.recall_id, level=level, detail=res.get("atlas_error"), source="admin")
        if not res["closed"]:
            # Spec "The planner": a refused close wakes it, as the API's close does.
            runs.wake_planner(db, args.recall_id, f"close refused: {res['open_units']} open units")
        _print(res)
    elif args.cmd == "release":
        from datetime import datetime, timezone
        db.worker_state.update_one({"_id": f"sim_release:{args.recall_id}:{args.party_id}"},
                                   {"$set": {"released_at": datetime.now(timezone.utc)}}, upsert=True)
        print(f"released {args.party_id} in {args.recall_id}")
    elif args.cmd == "followups":
        db.recalls.update_one({"_id": args.recall_id}, {"$set": {"followups_released": True}})
        print(f"follow-ups released for {args.recall_id}")
    elif args.cmd == "arm":
        from .orchestrator.failpoints import arm
        _print(arm(db, args.name, party_id=args.party, channel=args.channel, by="cli"))
    elif args.cmd == "disarm":
        from .orchestrator.failpoints import disarm
        disarm(db)
        print("all failpoints disarmed")
    elif args.cmd == "count-kill":
        try:
            out = count_kill(db, args.recall, exit_code=args.exit_code)
        except LookupError as exc:
            sys.exit(str(exc))
        print(f"{out['recall_id']}: {out['text']}")
    elif args.cmd == "status":
        from .orchestrator.failpoints import armed
        _print({
            "db": settings.db_name,
            "recalls": list(db.recalls.find({},
                                            {"_id": 1, "status": 1, "open_units": 1, "mode": 1})),
            "workers": list(db.worker_state.find({"_id": {"$regex": "^worker:"}},
                                                 {"resume_token": 0})),
            "failpoints": armed(db),
            "live_attempts": list(db.attempts.find({"state": {"$in": ["intent", "dialed", "connected"]}},
                                                   {"_id": 1, "party_id": 1, "state": 1, "purpose": 1})),
        })
    elif args.cmd == "metrics":
        from .ledger import metrics
        _print(metrics(db, args.recall_id))
    elif args.cmd == "calls":
        from .orchestrator.claim import set_max_live, slots
        if args.max_live is not None:
            set_max_live(db, args.max_live)
        print(json.dumps(slots(db)))
    elif args.cmd == "preflight":
        import os

        from .memory.embed import get_embedder
        from .preflight import Report, preflight
        rep = Report(f"Preflight on {settings.db_name}")
        preflight(client, db, rep, os.environ, embedder=get_embedder(settings))
        print(rep.render())
        sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
