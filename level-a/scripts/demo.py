"""Rehearse the nine-beat demo against a running API (spec "Demo script").

    python scripts/demo.py [--api URL] [--recall-id RCL] [--timeout S] [--check-only] COMMAND

Each beat is one command. It does what the operator's button would do (skip
that with --check-only when the button on the wall was pressed instead),
prints what the room should see and the line to say, then polls
GET /wall/state once a second until each piece of evidence is on the wall,
printing "ok" or "MISSING" for each. Exit status: 0 everything seen,
1 something missing, 2 the API refused a request or could not be reached.

    beats      the running order: time, command, what the room sees, the line to say
    start      start a recall (default recall_long in demo mode: slice and order system change held)
    status     the wall's numbers on one screen
    run        0:00  run in progress: clock, parties found, 0 duplicates, invariants
    judge      0:15  pause random kills; release Harbour Fire and Lakeside Systems
    caught     0:35  Atlas rejects Harbour Fire's count; the correction is accepted
    kill-arm   1:00  arm after_dial_request on Corner Properties; release Corner Properties and
                     Queen St Alarm (scripts/kill_demo.sh runs this, then the worker)
    kill       1:05  failpoint fired, Corner Properties found, 15 disputed, resume line,
                     attempt adopted, Corner Properties called once
    order system     1:45  switch North orders to v2: contract failure, repaired adapter, retry
    memory     2:20  switch East orders to v2: adapter from memory, no model call
    close      2:35  press Close: Atlas refuses while panels are unexplained
    wrap       2:50  resume random kills; the wrap lines

Extras for the run-up and for questions:

    saboteur   switch saboteur mode on for N seconds (default 20): the gate refuses it
    question   add a question for every party: one document, one update, parties due

API_BASE_URL and ADMIN_TOKEN come from level-a/.env like every other process;
--api overrides the URL.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levela.config import Settings  # noqa: E402

POLL_S = 1.0
DEFAULT_TIMEOUT_S = 60.0
# Feed lines are stamped by whichever process wrote them (API, worker,
# supervisor); allow this much clock difference between them.
SKEW = timedelta(seconds=5)
KILL_FAILPOINT = "after_dial_request"
HARBOUR, QUEEN, LAKESIDE, CORNER = "harbour-fire", "queen-st-alarm", "lakeside-systems", "corner-properties"
DISPUTED_PANELS = 15


class DemoError(Exception):
    """The API refused a request or could not be reached (exit status 2)."""


# ---- the API -------------------------------------------------------------------

class Api:
    """The few endpoints a rehearsal touches, over an injectable httpx.Client
    (tests pass one built on httpx.MockTransport)."""

    def __init__(self, http: httpx.Client, admin_token: str):
        self.http = http
        self.headers = {"X-Admin-Token": admin_token}

    def _send(self, method: str, path: str, **kw) -> dict:
        try:
            resp = self.http.request(method, path, headers=self.headers, **kw)
        except httpx.HTTPError as exc:
            raise DemoError(f"{method} {path}: the API did not answer ({exc.__class__.__name__}: {exc}). "
                            "Is `make api` running, and is API_BASE_URL (or --api) right?") from exc
        if resp.is_error:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise DemoError(f"{method} {path} -> HTTP {resp.status_code}: {str(detail)[:400]}")
        return resp.json() if resp.content else {}

    def state(self, recall_id: str | None) -> dict:
        return self._send("GET", "/wall/state", params={"recall_id": recall_id} if recall_id else None)

    def status(self) -> dict:
        return self._send("GET", "/admin/status")

    def post(self, path: str, body: dict) -> dict:
        return self._send("POST", path, json=body)


# ---- what the wall shows -------------------------------------------------------

def _parse_at(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class View:
    """One /wall/state reply plus every feed line seen since the beat began
    (the wall keeps only the newest sixty, and a busy run scrolls)."""

    state: dict
    feed: list[dict] = field(default_factory=list)

    @property
    def run(self) -> dict:
        return self.state.get("run") or {}

    @property
    def metrics(self) -> dict:
        return self.state.get("metrics") or {}

    def party(self, party_id: str) -> dict | None:
        return next((s for s in self.state.get("parties") or [] if s.get("party_id") == party_id), None)

    def edge(self, from_party: str, to_party: str) -> dict | None:
        return next((e for e in self.state.get("edges") or []
                     if e.get("from_party") == from_party and e.get("to_party") == to_party), None)

    def system(self, name: str) -> dict:
        return next((s for s in self.state.get("systems") or [] if s.get("system") == name), {})

    def lines(self, kind: str, *, party_id: str | None = None, system: str | None = None) -> list[dict]:
        return [ln for ln in self.feed if ln.get("kind") == kind
                and (party_id is None or ln.get("party_id") == party_id)
                and (system is None or ln.get("system") == system)]


class FeedLog:
    """Accumulates feed lines across polls, oldest first, without repeats.
    Lines older than `since` belong to an earlier beat and are ignored."""

    def __init__(self, since: datetime | None):
        self.since = since
        self.lines: list[dict] = []
        self._seen: set[tuple] = set()

    def add(self, state: dict) -> None:
        for ln in reversed(state.get("feed") or []):
            key = (ln.get("at"), ln.get("kind"), ln.get("text"), ln.get("party_id"))
            if key in self._seen:
                continue
            at = _parse_at(ln.get("at"))
            if self.since is not None and at is not None and at < self.since:
                continue
            self._seen.add(key)
            self.lines.append(ln)


@dataclass(frozen=True)
class Expect:
    """One thing the room should see. `met(view, before)` reads the current
    view and the view from just before the beat acted."""

    label: str
    met: Callable[[View, View], bool]


def has_line(kind: str, *, party_id: str | None = None, system: str | None = None):
    return lambda v, _b: bool(v.lines(kind, party_id=party_id, system=system))


def after_fire(kind: str, *, party_id: str | None = None):
    """A `kind` line written after the kill failpoint fired on Corner Properties:
    the run's earlier resume lines prove nothing about this kill."""
    def met(v: View, _b: View) -> bool:
        fired = [_parse_at(ln.get("at")) for ln in v.lines("failpoint_fired", party_id=CORNER)]
        fired = [at for at in fired if at is not None]
        if not fired:
            return False
        return any((at := _parse_at(ln.get("at"))) is not None and at > max(fired)
                   for ln in v.lines(kind, party_id=party_id))
    return met


def watch(api: Api, recall_id: str | None, expectations: tuple[Expect, ...], before: View, *,
          since: datetime | None, timeout_s: float, out: Callable[[str], None],
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
          poll_s: float = POLL_S) -> bool:
    """Poll the wall until every expectation is met or the time is up. Each
    one prints "ok" the first time it is seen; the rest print "MISSING"."""
    log = FeedLog(since)
    met: set[str] = set()
    deadline = clock() + timeout_s
    last_error = None
    while True:
        try:
            state = api.state(recall_id)
        except DemoError as exc:
            # A missed poll is not a missed beat: say so once and keep watching.
            if str(exc) != last_error:
                out(f"  (the wall did not answer, still watching: {exc})")
                last_error = str(exc)
        else:
            log.add(state)
            view = View(state, log.lines)
            for e in expectations:
                if e.label not in met and e.met(view, before):
                    met.add(e.label)
                    out(f"  ok       {e.label}")
        if len(met) == len(expectations) or clock() >= deadline:
            break
        sleep(poll_s)
    for e in expectations:
        if e.label not in met:
            out(f"  MISSING  {e.label}")
    return len(met) == len(expectations)


# ---- the beats -----------------------------------------------------------------

@dataclass(frozen=True)
class Context:
    api: Api
    recall_id: str
    args: argparse.Namespace
    out: Callable[[str], None]


def _release(ctx: Context, party_id: str) -> str:
    reply = ctx.api.post("/admin/release", {"recall_id": ctx.recall_id, "party_id": party_id})
    unheld = reply.get("unheld") or []
    return f"released {party_id}" + (" (was held: due now)" if unheld else "")


def act_judge(ctx: Context) -> list[str]:
    ctx.api.post("/admin/supervisor", {"recall_id": ctx.recall_id, "paused": True,
                                       "reason": "the judge's call: the kill beat uses the failpoint"})
    return ["random kills paused", _release(ctx, HARBOUR), _release(ctx, LAKESIDE)]


def live_workers(api: Api) -> list[dict]:
    return [w for w in api.status().get("workers") or [] if w.get("alive")]


def act_kill_arm(ctx: Context) -> list[str]:
    """Arm first, so Corner Properties cannot be dialled before the failpoint is
    waiting for it; then let Corner Properties (and, unless --no-release, Queen St
    Alarm's held reply, which names Corner Properties) go."""
    running = live_workers(ctx.api)
    if running and not ctx.args.force:
        names = ", ".join(f"{w.get('name')} (heartbeat {w.get('heartbeat_age_s')} s ago)" for w in running)
        raise DemoError(f"a worker is still running: {names}. Stop the supervisor first (Ctrl+C in its "
                        "terminal): the kill beat runs the worker in the foreground so the room sees it die "
                        "and come back. --force arms anyway.")
    ctx.api.post("/admin/failpoint", {"name": KILL_FAILPOINT, "party_id": CORNER})
    done = [f"failpoint {KILL_FAILPOINT} armed for {CORNER}", _release(ctx, CORNER)]
    if not ctx.args.no_release:
        done.append(_release(ctx, QUEEN))
    return done


def _switch(order_system: str) -> Callable[[Context], list[str]]:
    def act(ctx: Context) -> list[str]:
        ctx.api.post("/admin/order-system", {"order_system": order_system, "action": "switch_v2"})
        return [f"order system {order_system} switched to v2"]
    return act


def act_close(ctx: Context) -> list[str]:
    res = ctx.api.post("/admin/close", {"recall_id": ctx.recall_id})
    if res.get("closed"):
        return ["the recall CLOSED: every panel is explained (this is not the demo's ending)"]
    return [f"close refused: {res.get('open_units')} open units"]


def act_wrap(ctx: Context) -> list[str]:
    ctx.api.post("/admin/supervisor", {"recall_id": ctx.recall_id, "paused": False})
    return ["random kills resumed (restart the supervisor if kill_demo's worker is the one running)"]


def _invariants_hold(v: View, _b: View) -> bool:
    return (v.run.get("invariants") or {}).get("ok") is True


def _adapter(order_system: str, *, from_memory: bool) -> Callable[[View, View], bool]:
    def met(v: View, _b: View) -> bool:
        s = v.system(f"{order_system.capitalize()} orders")
        source = str(s.get("adapter_source") or "")
        return (s.get("adapter_version") or 0) >= 2 and (source.startswith("episode") if from_memory else True)
    return met


def _retried(order_system: str) -> Callable[[View, View], bool]:
    def met(v: View, _b: View) -> bool:
        s = v.system(f"{order_system.capitalize()} orders")
        return (s.get("adapter_version") or 0) >= 2 and s.get("ops_flagged") == 0 and not s.get("paused")
    return met


def _party_called_once(party_id: str) -> Callable[[View, View], bool]:
    return lambda v, _b: ((v.state.get("calls") or {}).get("by_party") or {}).get(party_id) == 1


def _disputed(v: View, _b: View) -> bool:
    e = v.edge(QUEEN, CORNER)
    return bool(e) and e.get("disputed_qty") == DISPUTED_PANELS


def _held(party_id: str, held: bool) -> Callable[[View, View], bool]:
    def met(v: View, _b: View) -> bool:
        s = v.party(party_id)
        return s is not None and bool(s.get("hold_for_demo")) is held
    return met


def _call_live_or_placed(party_id: str) -> Callable[[View, View], bool]:
    def met(v: View, _b: View) -> bool:
        s = v.party(party_id) or {}
        return s.get("live_attempt") is not None or bool(v.lines("call_placed", party_id=party_id))
    return met


def _model_calls_unchanged(v: View, before: View) -> bool:
    return v.run.get("model_calls", 0) == before.run.get("model_calls", 0)


@dataclass(frozen=True)
class Beat:
    command: str
    at: str
    title: str
    room: str
    say: str
    act: Callable[[Context], list[str]] | None
    expect: tuple[Expect, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S


BEATS = (
    Beat("run", "0:00", "Run in progress",
         "The wall: started at 4:55, the simulated day, 40 parties from 5, steps, kills survived, "
         "0 duplicate effects.",
         "Nothing counts in a recall until every panel is explained. This agent will not close one until "
         "the mass balance holds.",
         None,
         (Expect("a run is in progress on an open recall",
                 lambda v, _b: bool(v.run) and (v.state.get("recall") or {}).get("status") == "open"),
          Expect("the simulated clock is running", lambda v, _b: v.run.get("paused") is False),
          Expect("parties found during the run", lambda v, _b: (v.run.get("parties") or {}).get("found", 0) > 0),
          Expect("0 duplicate effects", lambda v, _b: v.run.get("duplicates") == 0),
          Expect("4 of 4 invariants hold at the last check", _invariants_hold),
          Expect("Harbour Fire is held for the live part", _held(HARBOUR, True))),
         timeout_s=5.0),
    Beat("judge", "0:15", "Judge call",
         "A team phone rings. A judge plays Harbour Fire's manager and is invited to lie.",
         "", act_judge,
         (Expect("random kills paused", lambda v, _b: (v.run.get("supervisor") or {}).get("paused") is True),
          Expect("Harbour Fire released to the timers", _held(HARBOUR, False)),
          Expect("a call to Harbour Fire is placed", _call_live_or_placed(HARBOUR)),
          Expect("Lakeside Systems answered in the background", has_line("claim_accepted", party_id=LAKESIDE)))),
    Beat("caught", "0:35", "Caught",
         "Wall feed: rejected by Atlas, 60 reported, 40 shipped. The agent challenges, the judge corrects, "
         "the claim is accepted.",
         "The model did not catch that. The database did.",
         None,
         (Expect("Atlas rejected Harbour Fire's count", has_line("claim_rejected", party_id=HARBOUR)),
          Expect("the corrected count was accepted", has_line("claim_accepted", party_id=HARBOUR))),
         timeout_s=180.0),
    Beat("kill-arm", "1:00", "Hidden party (the kill is armed)",
         "Queen St Alarm's claim creates Corner Properties on the wall.",
         "", act_kill_arm,
         (Expect(f"failpoint {KILL_FAILPOINT} armed for {CORNER}",
                 lambda v, _b: ((v.state.get("failpoints") or {}).get(KILL_FAILPOINT) or {}).get("party_id")
                 == CORNER),),
         timeout_s=5.0),
    Beat("kill", "1:05", "Kill",
         "The worker dials Corner Properties on a teammate's phone and dies. The phone rings once. The teammate "
         "says received 25. The edge turns disputed: 15 panels. Restart the worker: documents read, the "
         "attempt adopted, no second ring.",
         "The call was requested and the worker died before saving it. A naive agent redials. Ours finds "
         "the evidence first.",
         None,
         (Expect("Corner Properties found from Queen St Alarm's claim", lambda v, _b: v.party(CORNER) is not None),
          Expect("the failpoint fired on the call to Corner Properties", has_line("failpoint_fired", party_id=CORNER)),
          Expect(f"the claim landed: {DISPUTED_PANELS} panels disputed", _disputed),
          Expect("the worker resumed: documents read, nothing replayed", after_fire("worker_resumed")),
          Expect("the in-doubt attempt was adopted, not redialled",
                 after_fire("attempt_adopted", party_id=CORNER)),
          Expect("Corner Properties was called exactly once", _party_called_once(CORNER))),
         timeout_s=300.0),
    Beat("order_system", "1:45", "Order system changes",
         "The contract failure, the flagged operations, the repaired adapter, the canary matching, and only "
         "the flagged operations retried.",
         "The order system changed under us. A mapping that does not reproduce a count the ledger already trusts "
         "is never activated.",
         _switch("north"),
         (Expect("North orders switched to v2", has_line("order_system_switched", system="North orders")),
          Expect("contract failure detected in code", has_line("order_system_contract_failed", system="North orders")),
          Expect("repaired adapter v2 active after the canary", _adapter("north", from_memory=False)),
          Expect("flagged operations retried; none left flagged", _retried("north"))),
         timeout_s=120.0),
    Beat("memory", "2:20", "Memory",
         "East orders fails the same way. The saved mapping is retrieved and activated with no model call.",
         "The second order system repaired itself from memory: Vector Search on the failure text, the same canary, "
         "no model call.",
         _switch("east"),
         (Expect("East orders switched to v2", has_line("order_system_switched", system="East orders")),
          Expect("contract failure detected in code", has_line("order_system_contract_failed", system="East orders")),
          Expect("adapter v2 active from a remembered episode", _adapter("east", from_memory=True)),
          Expect("no model call for the second repair", _model_calls_unchanged),
          Expect("flagged operations retried; none left flagged", _retried("east"))),
         timeout_s=90.0),
    Beat("close", "2:35", "Closing gate",
         "Press Close. Atlas refuses and the wall shows the open panels. The three metrics sit side by side.",
         "Fifteen panels are unexplained, so Atlas will not let the recall close.",
         act_close,
         (Expect("Atlas refused the close", has_line("close_refused")),
          Expect("the recall is still open with panels unexplained",
                 lambda v, _b: (v.state.get("recall") or {}).get("status") == "open"
                 and (v.metrics.get("open_units") or 0) > 0)),
         timeout_s=15.0),
    Beat("wrap", "2:50", "Wrap",
         "One sentence each: what is real, what is simulated, why it matters.",
         "What is real here: the calls, the kills and every refusal by the database. What is simulated: the "
         "clock, the other parties and the order system. Today this is fire alarm panels. A different product is a different recall document on the same engine.",
         act_wrap,
         (Expect("random kills resumed", lambda v, _b: (v.run.get("supervisor") or {}).get("paused") is False),),
         timeout_s=10.0),
)
def act_saboteur(ctx: Context) -> list[str]:
    seconds = ctx.args.seconds
    ctx.api.post("/admin/saboteur", {"recall_id": ctx.recall_id, "on": True, "seconds": seconds})
    return [f"saboteur on for {seconds:g} s"]


def act_question(ctx: Context) -> list[str]:
    doc = ctx.api.post("/admin/questions", {"recall_id": ctx.recall_id, "text": ctx.args.text,
                                            "answer_type": ctx.args.answer_type})
    return [f"question {doc.get('question_id')} added: {doc.get('due')} parties due now"]


# Not beats of the nine: the saboteur runs before the judge's call or for
# questions (spec "Demo controls"), the question shows questions as data.
EXTRAS = (
    Beat("saboteur", "--", "Saboteur",
         "A worker breaks one rule on purpose. The feed shows REFUSED with the rule the gate applied.",
         "The model can propose anything. The gate decides, and Atlas refuses what has no evidence.",
         act_saboteur,
         (Expect("saboteur mode on", lambda v, _b: (v.run.get("saboteur") or {}).get("on") is True),
          Expect("the gate refused the saboteur", has_line("gate_refused"))),
         timeout_s=60.0),
    Beat("question", "--", "Question for every party",
         "One question document and one update: the parties without an answer become due and are asked "
         "the question only.",
         "A new question for every party is one document and one update.",
         act_question,
         (Expect("the question was added and fanned out", has_line("question_added")),
          Expect("an answer was recorded", has_line("answer_recorded"))),
         timeout_s=120.0),
)
BEAT_BY_COMMAND = {b.command: b for b in BEATS + EXTRAS}


def run_beat(ctx: Context, beat: Beat, *, sleep=time.sleep, clock=time.monotonic) -> int:
    out = ctx.out
    out(f"{beat.at}  {beat.title}")
    out(f"  The room sees: {beat.room}")
    before = View(ctx.api.state(ctx.recall_id))
    acting = beat.act is not None and not ctx.args.check_only
    if acting:
        for line in beat.act(ctx):
            out(f"  done     {line}")
    # A beat that acts counts only what follows its action; a watching beat
    # counts everything still on the wall.
    started = _parse_at(before.state.get("now")) if acting else None
    since = started - SKEW if started else None
    timeout = ctx.args.timeout if ctx.args.timeout is not None else beat.timeout_s
    ok = watch(ctx.api, ctx.recall_id, beat.expect, before, since=since, timeout_s=timeout, out=out,
               sleep=sleep, clock=clock)
    if beat.say:
        out(f"  Say: \"{beat.say}\"")
    return 0 if ok else 1


# ---- the other commands ----------------------------------------------------------

def print_beats(out: Callable[[str], None]) -> None:
    for b in BEATS:
        out(f"{b.at}  {b.title:<34} python scripts/demo.py {b.command}")
        out(f"       {b.room}")
        if b.say:
            out(f"       Say: \"{b.say}\"")


def _pair(value) -> str:
    a, b = (list(value or []) + [0, 0])[:2]
    return f"{a or 0:,} of {b or 0:,}"


def status_lines(state: dict) -> list[str]:
    """The wall's numbers as plain lines (pre-show checks, and between beats)."""
    recall, run, m = state.get("recall") or {}, state.get("run") or {}, state.get("metrics") or {}
    if not recall:
        return ["No run-time recall yet: start one (make demo, or python scripts/demo.py start)."]
    lines = [f"{recall.get('_id')}  {recall.get('status')}  batch {recall.get('batch_code')}  "
             f"{recall.get('units_total') or 0:,} {recall.get('product') or ''}".rstrip()]
    if run:
        c, inv = run.get("parties") or {}, run.get("invariants") or {}
        held = "paused" if run.get("paused") else "running"
        passed = inv.get("passed") or []
        lines += [
            f"Simulated {run.get('sim_label')} (day {run.get('day')} of {run.get('days_total')}), "
            f"{run.get('seconds_per_sim_hour')} s per simulated hour, clock {held}",
            f"Parties {c.get('total')} ({c.get('at_start')} at the start, {c.get('found')} found)   "
            f"Checked {_pair(run.get('checked'))}   Steps {run.get('steps')}",
            f"Kills survived {run.get('kills')}   Duplicate effects {run.get('duplicates')}   "
            f"Context per decision {run.get('tokens_per_decision') or '-'} tokens   "
            f"Model calls {run.get('model_calls', 0)}",
            f"Invariants: {sum(1 for p in passed if p)} of {len(passed)} hold at {inv.get('at_label') or '-'}"
            if passed else "Invariants: not checked yet",
            f"Random kills {'paused' if (run.get('supervisor') or {}).get('paused') else 'on'}   "
            f"Saboteur {'on' if (run.get('saboteur') or {}).get('on') else 'off'}",
        ]
    lines.append(f"Located {_pair(m.get('panels_located'))}   Fixed or returned {_pair(m.get('fixed_or_returned'))}   "
                 f"Open {m.get('open_units')} (disputed {m.get('disputed_units')})   "
                 f"Mass balance {m.get('mass_balance')} of {m.get('units_total')}")
    for s in state.get("systems") or []:
        if s.get("system", "").endswith(" orders"):
            lines.append(f"  {s['system']:<13} adapter v{s.get('adapter_version')} ({s.get('adapter_source')})   "
                         f"{s.get('ops_verified')} verified, {s.get('ops_flagged')} flagged"
                         + ("   PAUSED" if s.get("paused") else ""))
        else:
            lines.append(f"  {s.get('system'):<13} {s.get('attempts')} attempts, "
                         f"{s.get('accepted_claims')} accepted claims")
    flight = state.get("calls_in_flight") or {}
    lines.append(f"Leases live {len(state.get('workers') or [])}   Calls in flight: {flight.get('real', 0)} real, "
                 f"{flight.get('simulated', 0)} simulated   Failpoints armed: "
                 f"{', '.join(state.get('failpoints') or {}) or 'none'}")
    return lines


def start(api: Api, args, out: Callable[[str], None]) -> int:
    body = {"scenario": args.scenario, "mode": args.mode, "all_sim": args.all_sim}
    if args.speed is not None:
        body["seconds_per_sim_hour"] = args.speed
    if args.sim_party:
        body["sim_parties"] = args.sim_party
    if args.recall_id:
        body["recall_id"] = args.recall_id
    res = api.post("/admin/recalls", body)
    run = res.get("run") or {}
    out(f"started {res.get('recall_id')} ({res.get('mode')} mode, options {res.get('options')})")
    if run:
        out(f"  clock from {run.get('sim_start_label')} at {run.get('seconds_per_sim_hour')} s per simulated hour; "
            f"order system changes {run.get('order_system_change_at') or 'by hand'}")
    for s in res.get("parties") or []:
        out(f"  {s.get('party_id'):18} {s.get('channel'):6} form: {s.get('form_url')}")
    return 0


# ---- entry point ---------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(prog="python scripts/demo.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default=None, help="the API's base URL (default: API_BASE_URL)")
    p.add_argument("--recall-id", default=None, help="default: the most recently opened run-time recall")
    p.add_argument("--timeout", type=float, default=None,
                   help="seconds to wait for a beat's evidence (default: per beat)")
    p.add_argument("--check-only", action="store_true",
                   help="do not act (the button was pressed on the wall); only check the evidence")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("beats", help="print the running order")
    s = sub.add_parser("start", help="start a recall through the API")
    s.add_argument("--scenario", default="recall_long")
    s.add_argument("--mode", choices=["demo", "sim"], default="demo")
    s.add_argument("--speed", type=float, default=None, metavar="SECONDS",
                   help="seconds per simulated hour (default: the scenario's schedule)")
    s.add_argument("--sim-party", action="append", default=[],
                   help="simulate this voice party too, e.g. corner-properties when the second phone is flaky")
    s.add_argument("--all-sim", action="store_true", help="simulate every party (recall_2, the memory fallback)")
    sub.add_parser("status", help="the wall's numbers on one screen")
    for beat in BEATS + EXTRAS:
        b = sub.add_parser(beat.command, help=f"{beat.at} {beat.title}")
        if beat.command == "kill-arm":
            b.add_argument("--no-release", action="store_true",
                           help="Queen St Alarm was already released: only arm and release Corner Properties")
            b.add_argument("--force", action="store_true", help="arm even though a worker is running")
        elif beat.command == "saboteur":
            b.add_argument("--seconds", type=float, default=20.0)
        elif beat.command == "question":
            b.add_argument("text", help='for example "Did you also receive batch B-4480?"')
            b.add_argument("--answer-type", default="yes_no_count",
                           choices=["yes_no_count", "yes_no", "count", "text"])
    return p.parse_args(argv)


def resolve_recall(api: Api, recall_id: str | None) -> str:
    if recall_id:
        return recall_id
    recall = api.state(None).get("recall")
    if not recall:
        raise DemoError("no run-time recall yet: start one with `make demo` (or python scripts/demo.py start)")
    return recall["_id"]


def main(argv=None, *, http: httpx.Client | None = None, env: Mapping[str, str] | None = None,
         out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> int:
    """`http`, `env`, `sleep` and `clock` are injectable for tests."""
    args = parse_args(argv)
    if args.command == "beats":
        print_beats(out)
        return 0
    settings = Settings.from_env(env=env)
    base = (args.api or settings.api_base_url).rstrip("/")
    owned = None
    if http is None:
        owned = http = httpx.Client(base_url=base, timeout=15.0)
    try:
        api = Api(http, settings.admin_token)
        if args.command == "start":
            return start(api, args, out)
        recall_id = resolve_recall(api, args.recall_id)
        if args.command == "status":
            for line in status_lines(api.state(recall_id)):
                out(line)
            return 0
        ctx = Context(api=api, recall_id=recall_id, args=args, out=out)
        return run_beat(ctx, BEAT_BY_COMMAND[args.command], sleep=sleep, clock=clock)
    except DemoError as exc:
        out(f"ERROR: {exc}")
        return 2
    finally:
        if owned is not None:
            owned.close()


if __name__ == "__main__":
    sys.exit(main())
