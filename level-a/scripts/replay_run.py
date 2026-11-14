"""Replay one task run from its traces, with every tool stubbed.

    python scripts/replay_run.py RUN_ID [--decider rules|claude] [--json]

Each recorded decision's brief (text and facts, as the decider saw it) is
given to the decider again and its decision is compared with the recorded
one. Tools are not called: what they returned is already inside the next
recorded brief, so a prompt or rule change can be tried against recorded runs
without placing a call or touching an order system. Reads the database named by
ATLAS_URI / ATLAS_DB (level-a/.env). Exit status 0 when every decision
matches, 1 when any differs, 2 when the run does not exist.

`--decider claude` asks the model again (needs ANTHROPIC_API_KEY; it spends
tokens, one request per recorded decision).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levela.config import Settings  # noqa: E402
from levela.db.client import get_db  # noqa: E402
from levela.harness import trace  # noqa: E402
from levela.harness.decide import build_decider  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay a task run from its traces with tools stubbed.")
    p.add_argument("run_id", help="task run id, e.g. tr-1a2b3c4d5e6f")
    p.add_argument("--decider", choices=("rules", "claude"), default=None,
                   help="decider to replay with (default: PLANNER from the environment)")
    p.add_argument("--json", action="store_true", help="print the comparison as JSON")
    return p.parse_args(argv)


def render(report: dict) -> str:
    run = report["run"]
    lines = [f"run {run['_id']}  {run['kind']} on {run.get('party_id') or 'the recall'}  "
             f"status {run['status']}  decider {run.get('decider')}"]
    for step in report["steps"]:
        mark = "same" if step.match else "DIFFERS"
        lines.append(f"  #{step.seq:<3} recorded {_action(step.recorded):<38} replayed {_action(step.replayed):<38} {mark}")
    result = (report.get("result") or {}).get("output") or {}
    lines.append(f"{report['matched']} of {report['total']} decisions match; recorded result "
                 f"{(result.get('result') or {}).get('outcome', result.get('status', '-'))}")
    return "\n".join(lines)


def _action(decision: dict) -> str:
    if not decision.get("tool"):
        return "done"
    args = json.dumps(decision.get("args") or {}, sort_keys=True, default=str)
    return decision["tool"] + ("" if args == "{}" else f" {args}")


def main(argv=None) -> int:
    args = parse_args(argv)
    settings = Settings.from_env(**({"decider": args.decider} if args.decider else {}))
    _client, db = get_db(settings, appname="levela-replay")
    try:
        report = trace.replay(db, args.run_id, build_decider(settings))
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({**report, "steps": [vars(s) for s in report["steps"]]}, default=str, indent=2))
    else:
        print(render(report))
    return 0 if report["matched"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
