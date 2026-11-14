"""Run a child node's own work: python -m levela.node {seed,work,once}.

    seed   set up this node's database and load its records (scenarios/summit.json)
    work   loop: Summit's stores act, buyers are told, reports go to the parent
    once   one pass of `work`

The node's simulated time is hours since its sub-recall was taken, at
NODE_SIM_RATE simulated hours per real second (default 1: nine simulated
days in under four minutes). Several `work` processes may run at once:
every step is idempotent (event ids, attempt keys, the link's seq).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from ..config import Settings
from ..db.client import get_client
from ..db.setup import setup
from ..memory.embed import get_embedder
from . import buyers, child, reporter

log = logging.getLogger("levela.node")


def _voice(settings, scn):
    """The fake voice provider, with the scenario's unreachable buyers refusing.
    A real call to a buyer is never placed from a demo node."""
    from ..providers.fake_voice import FakeVoice
    return FakeVoice(faults={b["_id"]: "reject" for b in scn["buyers"] if not b.get("reachable", True)})


def once(client, db, settings, scn, http, voice, rate: float) -> list[dict]:
    out = []
    now = datetime.now(timezone.utc)
    for recall in db.recalls.find({"parent": {"$ne": None}, "match_state": "taken"}):
        sim = max((now - recall["created_at"]).total_seconds() * rate, 0.0)
        if recall["status"] == "open":
            child.tick(client, db, scn, recall["_id"], sim)
            buyers.tell_all(db, voice, recall["_id"], worker=settings.worker_name, sim=sim)
        out.append(reporter.send(db, settings, http, recall["_id"], sim=sim))
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m levela.node")
    parser.add_argument("cmd", choices=["seed", "work", "once"])
    parser.add_argument("--every", type=float, default=2.0, help="seconds between passes of `work`")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    client = get_client(settings, appname=f"levela-node-{settings.node_id}")
    db = client[settings.db_name]
    scn = child.load_scenario()
    if args.cmd == "seed":
        setup(db)
        print(child.seed_node(db, scn, get_embedder(settings), parent_url=settings.parent_url))
        return 0
    rate = float(os.environ.get("NODE_SIM_RATE") or 1.0)
    voice = _voice(settings, scn)
    with httpx.Client(timeout=10.0) as http:
        while True:
            for res in once(client, db, settings, scn, http, voice, rate):
                if res.get("sent"):
                    log.info("report %s: %s", res.get("seq"), "accepted" if res.get("accepted") else res.get("reply"))
            if args.cmd == "once":
                return 0
            time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
