"""The simulator: a fake telephony vendor that plays the simulated parties.

SimNetwork stands in for ElevenLabs + Twilio together. place_call() logs a
row in `sim_calls` (the simulated Twilio Calls list) and starts a daemon
thread that plays the party: each reply goes to the API's tool endpoint
exactly as the voice agent's tool call would, and when the call ends a
signed post-call webhook goes to the API exactly as ElevenLabs' would.
Nothing on the API side knows which kind of call it is serving.

The network lives in the API process, so killing the worker does not kill a
simulated call, just as it would not hang up a real phone (DESIGN.md 10.9).

Two ways to play a party:

  scripted  `scenario.sim[party_id][key]` (DESIGN.md section 7, the four-party
            slice). A reply waits `delay_s * sim_time_scale` after the
            previous one (the first after the call starts); in demo mode a
            `demo_hold` reply waits for `worker_state sim_release:<recall>:<party>`;
            a `must_follow: X` reply waits until X has an accepted claim. No
            call outlasts `max_call_s` (ElevenLabs' default maximum
            conversation length): a party still waiting then hangs up and the
            post-call goes out with what was said.
  profile   (V2 section 8, the long run) when the party has no script for
            the call's purpose but has `scenario.sim_truth[party_id]`:
            providers.profiles.respond() decides the reply from the party's
            behaviour profile and ground truth. Turns are paced by the run's
            simulated clock (about a simulated minute each, so a call lasts
            ~3 simulated minutes). No answer or busy becomes a
            call_initiation_failure; a callback request goes to
            POST /tools/request_callback; question answers to
            POST /tools/submit_answer. A scripted party also answers the
            questions the call asked after its script (from its sim_truth;
            a party without one had none of any other batch). A question-only
            call (purpose ask_question) has no script: the party gives no
            counts and only answers.

Party-initiated events (PartyEvents) run in simulated time on one scheduler
thread, kept in Atlas so they survive a restart and fire once: the
submission a party makes after a form link reaches it (POST /sim/form_link),
a reviser's correction through its form, and the distributor rep's report
for the unreachable party once it has been escalated (POST /tools/report).

Faults (tests and crash schedules), passed to place_call as a dict:
  duplicate_webhook  post every tool body twice, back to back
  late_post_call_s   seconds to hold the post-call webhook back
  drop_post_call     never send the post-call; the row stays unfinished
  no_answer          nobody picks up: no replies, a call_initiation_failure

Also here: the posters (how the network reaches the API), SimVoice (the
worker's side: POST /sim/calls, POST /sim/form_link) and SimProbe (sim_calls
as the call probe).
"""
from __future__ import annotations

import functools
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import httpx
from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

from .. import feed, signing
from ..api.questions import question_docs
from ..clock import fmt, get_clock, server_now
from ..db.seed import load_scenario
from . import profiles
from .base import PlacedCall, ProbedCall, ProviderError
from .voice import DEFAULT_TIMEOUT, json_body, placed_call, request, string_variables

log = logging.getLogger("levela.providers.sim")

TOOL_PATH = "/tools/submit_count"
ANSWER_PATH = "/tools/submit_answer"
CALLBACK_PATH = "/tools/request_callback"
REPORT_PATH = "/tools/report"
HOOK_PATH = "/hooks/post_call"
FORM_PATH = "/form/"
CALLS_PATH = "/sim/calls"
FORM_LINK_PATH = "/sim/form_link"
TOOL_SECRET_HEADER = "X-Tool-Secret"

FAULTS = ("duplicate_webhook", "late_post_call_s", "drop_post_call", "no_answer")
POLL_S = 0.15
MAX_CALL_S = 600.0
TURN_SIM_MIN = 1.0               # one turn of a profile call, in simulated minutes

_READY, _STOPPED, _TIMED_OUT = "ready", "stopped", "timed_out"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def check_faults(faults: dict | None) -> dict:
    """Validate a faults dict; a typo must fail the test that made it, not be ignored."""
    faults = dict(faults or {})
    unknown = sorted(set(faults) - set(FAULTS))
    if unknown:
        raise ValueError(f"unknown simulator fault(s) {', '.join(unknown)}; choose from {', '.join(FAULTS)}")
    late = faults.get("late_post_call_s")
    if late is not None and (isinstance(late, bool) or not isinstance(late, (int, float)) or late < 0):
        raise ValueError(f"late_post_call_s must be a number of seconds, not {late!r}")
    return faults


# ---------------------------------------------------------------- posters

class _Poster:
    """The API's endpoints, over any `post(url, json=|content=, headers=)` callable.

    Never raises into a player: a party whose tool call fails just hears an
    error reply ({"error": ...}); a webhook or form post that fails reports
    status 0.
    """

    def __init__(self, post, base_url: str, tool_secret: str):
        self._post, self._base, self._secret = post, base_url, tool_secret

    def post_tool(self, body: dict) -> dict:
        """POST /tools/submit_count: one claim."""
        return self._post_json(TOOL_PATH, body)

    def post_answer(self, body: dict) -> dict:
        """POST /tools/submit_answer {attempt_id, party_id, question_id, value}."""
        return self._post_json(ANSWER_PATH, body)

    def post_callback(self, body: dict) -> dict:
        """POST /tools/request_callback {attempt_id, party_id, callback_at (sim hour)}."""
        return self._post_json(CALLBACK_PATH, body)

    def post_report(self, body: dict) -> dict:
        """POST /tools/report {recall_id, party_id, source: "rep", evidence_id, counts...}."""
        return self._post_json(REPORT_PATH, body)

    def post_hook(self, raw: bytes, headers: dict) -> int:
        try:
            resp = self._post(self._base + HOOK_PATH, content=raw, headers=dict(headers))
        except Exception as exc:  # noqa: BLE001 - reported to the player as status 0
            log.warning("simulator post-call webhook failed: %r", exc)
            return 0
        return resp.status_code

    def post_form(self, token: str, fields: dict) -> int:
        """POST /form/{token}, form-encoded, as a phone's browser would send it."""
        content = urlencode({k: str(v) for k, v in fields.items()}).encode("utf-8")
        try:
            resp = self._post(self._base + FORM_PATH + quote(token, safe=""), content=content,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        except Exception as exc:  # noqa: BLE001 - reported to the scheduler as status 0
            log.warning("simulator form submission failed: %r", exc)
            return 0
        return resp.status_code

    def _post_json(self, path: str, body: dict) -> dict:
        try:
            resp = self._post(self._base + path, json=body, headers={TOOL_SECRET_HEADER: self._secret})
        except Exception as exc:  # noqa: BLE001 - a transport failure is a reply the player records
            log.warning("simulator post to %s for %s failed: %r", path, body.get("attempt_id"), exc)
            return {"error": f"{type(exc).__name__}: {exc}"}
        if resp.status_code >= 400:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}", "status": resp.status_code}
        try:
            reply = resp.json()
        except ValueError:
            return {"error": f"HTTP {resp.status_code}: the reply is not JSON", "status": resp.status_code}
        if not isinstance(reply, dict):
            return {"error": f"HTTP {resp.status_code}: the reply is not a JSON object", "status": resp.status_code}
        return reply


class HttpPoster(_Poster):
    """Reaches the API over HTTP (the network hosted next to a separate API, or scripts)."""

    def __init__(self, base_url: str, tool_secret: str, timeout: float = 10, *, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=timeout)
        super().__init__(functools.partial(self._http.post, timeout=timeout), base_url.rstrip("/"), tool_secret)


class ClientPoster(_Poster):
    """Reaches the API through an in-process client such as FastAPI's TestClient."""

    def __init__(self, client, tool_secret: str):
        super().__init__(client.post, "", tool_secret)


# ---------------------------------------------------------------- the network

@dataclass
class _Call:
    """One simulated call. Owned by its player thread; never shared."""
    sid: str
    conversation_id: str
    to: str
    attempt_id: str
    recall_id: str
    party_id: str
    purpose: str
    variables: dict
    faults: dict
    started: datetime
    t0: float
    recall: dict = field(default_factory=dict)
    script: list = field(default_factory=list)
    entry: dict | None = None        # sim_truth[party_id] when the profile plays the call
    name: str = ""

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


class SimNetwork:
    """A fake telephony vendor that plays the simulated parties in daemon threads."""

    def __init__(self, db, poster, settings, *, max_call_s: float = MAX_CALL_S,
                 event_wait_s: float = 1.0):
        self.db, self.poster, self.settings = db, poster, settings
        self.max_call_s = max_call_s
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self._players: set[threading.Thread] = set()
        self._scenarios: dict[str, dict | None] = {}
        self.events = PartyEvents(db, poster, scenario=self._scenario, stop=self._stop, max_wait_s=event_wait_s)

    # -- public

    def place_call(self, *, to_number: str, dynamic_variables: dict, attempt_id: str,
                   faults: dict | None = None) -> PlacedCall:
        """Log the call in sim_calls and start playing the party.

        `raw` on the result is the outbound-call API's reply shape
        ({success, message, conversation_id, callSid}), so POST /sim/calls can
        return it as is.
        """
        if self._stop.is_set():
            raise ProviderError("the simulator is stopped; no call placed")
        faults = check_faults(faults)
        variables = string_variables(dynamic_variables, attempt_id)
        attempt = self.db.attempts.find_one({"_id": attempt_id}, {"recall_id": 1, "party_id": 1, "purpose": 1}) or {}
        recall_id = variables.get("recall_id") or attempt.get("recall_id")
        party_id = variables.get("party_id") or attempt.get("party_id")
        if not recall_id or not party_id:
            raise ProviderError(f"the simulator has no attempt {attempt_id} to play; no call placed")
        purpose = variables.get("purpose") or attempt.get("purpose") or "initial"
        recall = self.db.recalls.find_one({"_id": recall_id}, {"scenario_id": 1, "mode": 1}) or {}
        scenario = self._scenario(recall.get("scenario_id")) or {}
        scripts = (scenario.get("sim") or {}).get(party_id) or {}
        key = _script_key(purpose, scripts)
        script = scripts.get(key) or []
        entry = (scenario.get("sim_truth") or {}).get(party_id)
        by_profile = entry is not None and not script
        call = _Call(sid="CA" + secrets.token_hex(16), conversation_id="conv_sim_" + secrets.token_hex(12),
                     to=to_number, attempt_id=attempt_id, recall_id=recall_id, party_id=party_id, purpose=purpose,
                     variables=variables, faults=faults, started=utcnow(), t0=time.monotonic(), recall=recall,
                     script=script, entry=entry)
        self.db.sim_calls.insert_one({
            "_id": call.sid, "conversation_id": call.conversation_id, "to": to_number, "attempt_id": attempt_id,
            "recall_id": recall_id, "party_id": party_id, "purpose": purpose,
            "status": "ringing" if faults.get("no_answer") else "in-progress",
            "start_time": call.started, "end_time": None, "script_key": None if by_profile else key,
            "profile": entry["profile"] if by_profile else None,
            "replies": [], "faults": faults, "dynamic_variables": variables, "sim": True,
        })
        if not script and not by_profile:
            log.info("simulated call %s: no %s script for %s; the party will not reply", call.sid, key, party_id)
        if scenario.get("sim_truth"):
            self.start()
        player = threading.Thread(target=self._run, args=(call,), daemon=True,
                                  name=f"sim-{party_id}-{call.sid[-6:]}")
        with self._cv:
            self._players.add(player)
        player.start()
        log.info("simulated call %s to %s for %s (%s)", call.sid, party_id, attempt_id, purpose)
        return PlacedCall(conversation_id=call.conversation_id, call_sid=call.sid,
                          raw={"success": True, "message": "simulated call placed",
                               "conversation_id": call.conversation_id, "callSid": call.sid})

    def form_link_delivered(self, *, recall_id: str, party_id: str, token: str) -> dict:
        """A form link reached the party (POST /sim/form_link). Its profile
        decides whether and when it submits; the submission is an event in
        simulated time. Delivering the same link again changes nothing.

        Raises LookupError for an unknown party, ValueError when the token
        is not that party's form token.
        """
        party = self.db.parties.find_one({"recall_id": recall_id, "party_id": party_id}, {"name": 1, "form_token": 1})
        if party is None:
            raise LookupError(f"no party {party_id} in recall {recall_id}")
        if token != party.get("form_token"):
            raise ValueError(f"that link's token is not {party_id}'s form token")
        recall = self.db.recalls.find_one({"_id": recall_id}, {"scenario_id": 1}) or {}
        entry = ((self._scenario(recall.get("scenario_id")) or {}).get("sim_truth") or {}).get(party_id)
        if entry is None:
            return {"accepted": True, "submit_at": None, "submit_label": None, "duplicate": False,
                    "note": "no behaviour profile: a person answers this party's link"}
        self.start()
        now = _sim_now(self.db, recall_id)
        plan = profiles.form_plan(entry["profile"], entry["truth"], entry.get("params"), now)
        event, created = self.events.schedule(
            recall_id, party_id, "form", plan.submit_at if plan else None, plan.fields if plan else None,
            token=token, state=PENDING if plan else IGNORED)
        due = event.get("due_at")
        if created:
            text = (f"{party['name']} (simulated) got the form link and will fill it in at {fmt(due)}" if plan
                    else f"{party['name']} (simulated) got the form link and has not opened it")
            feed.emit(self.db, "sim_reply", text, recall_id=recall_id, party_id=party_id, source="sim",
                      system="form", detail={"event": event["_id"], "submit_at": due})
        return {"accepted": True, "submit_at": due, "submit_label": fmt(due) if due is not None else None,
                "duplicate": not created}

    def start(self) -> None:
        """Start the party-event scheduler (idempotent). The API should call
        this at startup so events pending in Atlas resume after a restart."""
        self.events.start()

    def wait_idle(self, timeout: float | None = 30.0) -> bool:
        """Block until every player has finished. False if `timeout` passed first."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._players, timeout)

    def stop(self) -> None:
        """Refuse new calls; every player exits at its next wait without
        posting more, and the event scheduler stops."""
        self._stop.set()
        self.events.join(timeout=5.0)

    # -- the player

    def _run(self, call: _Call) -> None:
        try:
            self._play(call)
        except Exception as exc:  # noqa: BLE001 - one broken script must not take down the API's other calls
            log.exception("simulated call %s crashed", call.sid)
            self._update_row(call, {"status": "failed", "end_time": utcnow(), "error": repr(exc)})
            feed.emit(self.db, "error", f"Simulated call to {call.name or call.party_id} crashed: {exc}",
                      recall_id=call.recall_id, party_id=call.party_id, attempt_id=call.attempt_id,
                      level="bad", source="sim")
        finally:
            with self._cv:
                self._players.discard(threading.current_thread())
                self._cv.notify_all()

    def _play(self, call: _Call) -> None:
        call.name = call.variables.get("party_name") or self._party_name(call)
        by_profile = call.entry is not None and not call.script
        if call.faults.get("no_answer"):
            self._ring_out(call, self._ring_s(call) if by_profile
                           else self.settings.ringing_timeout_s * self.settings.sim_time_scale)
            return
        if by_profile:
            self._play_profile(call)
        else:
            self._play_script(call)

    def _play_script(self, call: _Call) -> None:
        transcript = [_turn("agent", _opening_line(call.variables), 0)]
        since = call.t0
        for reply in call.script:
            turn = self._await_turn(call, call.recall, reply, since)
            if turn == _STOPPED:
                return
            if turn == _TIMED_OUT:
                break
            result = self._say(call, reply, transcript)
            since = time.monotonic()
            if result.get("stop"):
                break
        else:
            if self._answer_open_questions(call, transcript) == _STOPPED:
                return
            transcript.append(_turn("agent", "Thank you for your help. Goodbye.", call.elapsed()))
        ended, duration = utcnow(), call.elapsed()
        self._finish(call, self._transcription(call, transcript, duration), "completed", ended)

    def _play_profile(self, call: _Call) -> None:
        """One call to a profile party: decide the reply, then play it at the
        pace of the run's clock."""
        entry = call.entry
        clock = get_clock(self.db, call.recall_id)
        ctx = self._context(call, clock)
        reply = profiles.respond(entry["profile"], entry["truth"], entry.get("params"), ctx)
        rang_out = reply.kind in (profiles.NO_ANSWER, profiles.BUSY)
        self._update_row(call, {"reply_kind": reply.kind, "attempt_no": ctx.attempt, "sim_at": round(ctx.sim_now, 4),
                                **({"status": "ringing"} if rang_out else {})})
        if rang_out:
            self._ring_out(call, self._ring_s(call), reason="busy" if reply.kind == profiles.BUSY else "no-answer")
            return
        transcript = [_turn("agent", _opening_line(call.variables), 0)]
        if self._converse(call, clock, reply, transcript) == _STOPPED:
            return
        transcript.append(_turn("agent", "Thank you for your help. Goodbye.", call.elapsed()))
        ended, duration = utcnow(), call.elapsed()
        self._finish(call, self._transcription(call, transcript, duration), "completed", ended)

    def _converse(self, call: _Call, clock, reply: profiles.Reply, transcript: list) -> str:
        """Claims until the database accepts one, then the answers, then a
        callback request. A turn takes about a simulated minute. A reply
        that says stop (the challenge cap) ends the call there."""
        ended = False
        for body, say in reply.claim_turns():
            if self._pause(clock):
                return _STOPPED
            result = self._say(call, {"say": say, "claim": body}, transcript)
            if result.get("accepted"):
                self._after_acceptance(call, clock)
                break
            if result.get("stop"):
                ended = True
                break
        if not ended:
            for answer, say in reply.answer_turns():
                if self._pause(clock):
                    return _STOPPED
                self._answer(call, answer, say, transcript)
            if reply.kind == profiles.CALLBACK:
                if self._pause(clock):
                    return _STOPPED
                self._request_callback(call, reply.callback_at, reply.callback_line(), transcript)
        return _STOPPED if self._pause(clock) else _READY

    def _answer_open_questions(self, call: _Call, transcript: list) -> str:
        """After its script, a party answers the questions the call asked
        from its ground truth, as a profile party would. A scripted party
        without one (a real phone in the demo, simulated in tests) had none
        of any other batch, so a question fan-out still hears from every party."""
        questions = self._questions(call)
        if not questions:
            return _READY
        clock = get_clock(self.db, call.recall_id)
        answers, says = profiles.answers_for(questions, (call.entry or {}).get("truth") or {})
        for answer, say in zip(answers, says):
            if self._pause(clock):
                return _STOPPED
            self._answer(call, answer, say, transcript)
        return _READY

    def _context(self, call: _Call, clock) -> profiles.Context:
        """What the party knows when its phone rings: how often it has rung
        (this call included), whether it picked up before, and the questions
        open on it."""
        rows = list(self.db.sim_calls.find({"recall_id": call.recall_id, "party_id": call.party_id},
                                           {"reply_kind": 1}))
        earlier = [r.get("reply_kind") for r in rows if r["_id"] != call.sid]
        party = self.db.parties.find_one({"recall_id": call.recall_id, "party_id": call.party_id},
                                        {"ladder_step": 1}) or {}
        return profiles.Context(
            purpose=call.purpose if call.purpose in profiles.PURPOSES else "initial",
            attempt=max(1, len(rows)), ladder_step=int(party.get("ladder_step") or 1),
            sim_now=clock.sim_now(server_now(self.db.client)), told_qty=_count(call.variables.get("told_qty")),
            open_questions=tuple(self._questions(call)),
            sender_name=call.variables.get("sender_name") or "",
            reached_before=any(k in (profiles.ANSWER, profiles.CALLBACK) for k in earlier),
            callbacks_before=sum(k == profiles.CALLBACK for k in earlier))

    def _questions(self, call: _Call) -> list[dict]:
        """The questions the agent asks on this call ({id, text, answer_type}).

        The agent asks what its dynamic variables carry (`question_ids`, set
        by the harness from levela.api.questions), so the party answers those
        and nothing else. A call placed without the variable (a test dialling
        the network directly) asks the questions open on the party. Either
        way the ids are looked up the way /tools/submit_answer looks them up,
        by recall and question id."""
        if "question_ids" in call.variables:
            ids = [qid.strip() for qid in call.variables["question_ids"].split(",")]
        else:
            party = self.db.parties.find_one({"recall_id": call.recall_id, "party_id": call.party_id},
                                            {"open_questions": 1}) or {}
            ids = list(party.get("open_questions") or [])
        return [{"id": q["question_id"], "text": q.get("text") or "", "answer_type": q.get("answer_type")}
                for q in question_docs(self.db, call.recall_id, ids)]

    def _after_acceptance(self, call: _Call, clock) -> None:
        """A reviser schedules its correction once its count is accepted."""
        entry = call.entry
        plan = profiles.revision_plan(entry["profile"], entry["truth"], entry.get("params"),
                                      clock.sim_now(server_now(self.db.client)))
        if plan is not None:
            self.events.schedule(call.recall_id, call.party_id, "revise", plan.submit_at, plan.fields)

    def _pause(self, clock, sim_minutes: float = TURN_SIM_MIN) -> bool:
        """Wait one turn of simulated time. True if the network stopped."""
        return self._stop.wait(clock.real_seconds(sim_minutes / 60.0))

    def _ring_s(self, call: _Call) -> float:
        """Real seconds a profile party's phone rings: the ringing timeout in simulated time."""
        return get_clock(self.db, call.recall_id).real_seconds(self.settings.ringing_timeout_s / 3600.0)

    def _await_turn(self, call: _Call, recall: dict, reply: dict, since: float) -> str:
        """Wait out the scripted delay, the demo hold and the must_follow order."""
        delay = float(reply.get("delay_s") or 0) * self.settings.sim_time_scale
        if self._stop.wait(max(0.0, since + delay - time.monotonic())):
            return _STOPPED
        deadline = call.t0 + self.max_call_s
        if reply.get("demo_hold") and recall.get("mode") == "demo" and not self._released(call):
            feed.emit(self.db, "sim_hold", f"{call.name}'s reply is held for the demo - release it when ready",
                      recall_id=call.recall_id, party_id=call.party_id, attempt_id=call.attempt_id,
                      source="sim", detail={"say": reply.get("say"), "call_sid": call.sid})
            turn = self._wait_for(lambda: self._released(call), deadline)
            if turn != _READY:
                return self._gave_up(call, turn, "the demo release")
        follow = reply.get("must_follow")
        if follow:
            turn = self._wait_for(lambda: self._claimed(call.recall_id, follow), deadline)
            if turn != _READY:
                return self._gave_up(call, turn, f"{follow}'s claim")
        return _READY

    def _wait_for(self, check, deadline: float) -> str:
        while not self._stop.is_set():
            if check():
                return _READY
            if time.monotonic() >= deadline:
                return _TIMED_OUT
            self._stop.wait(POLL_S)
        return _STOPPED

    def _gave_up(self, call: _Call, turn: str, what: str) -> str:
        if turn == _TIMED_OUT:
            feed.emit(self.db, "info", f"{call.name} (simulated) hung up after {self.max_call_s:.0f} s "
                                       f"waiting for {what}",
                      recall_id=call.recall_id, party_id=call.party_id, attempt_id=call.attempt_id,
                      level="warn", source="sim", detail={"call_sid": call.sid})
        return turn

    def _say(self, call: _Call, reply: dict, transcript: list) -> dict:
        """Speak one claim: the wall line, then the tool call, then the record."""
        body = _tool_body(call, reply)
        claim = {k: v for k, v in body.items() if k not in _BODY_IDS}
        return self._speak(call, reply.get("say") or "", "submit_count", body, self.poster.post_tool, transcript,
                           record={"claim": claim}, repeat=bool(call.faults.get("duplicate_webhook")))

    def _answer(self, call: _Call, answer: dict, say: str, transcript: list) -> dict:
        body = {"attempt_id": call.attempt_id, "party_id": call.party_id, "question_id": answer["question_id"],
                "value": answer["value"]}
        return self._speak(call, say, "submit_answer", body, self.poster.post_answer, transcript,
                           record={"answer": dict(answer)})

    def _request_callback(self, call: _Call, callback_at: float, say: str, transcript: list) -> dict:
        body = {"attempt_id": call.attempt_id, "party_id": call.party_id, "callback_at": callback_at}
        return self._speak(call, say, "request_callback", body, self.poster.post_callback, transcript,
                           record={"callback_at": callback_at})

    def _speak(self, call: _Call, say: str, tool: str, body: dict, post, transcript: list, *, record: dict,
               repeat: bool = False) -> dict:
        """One party turn: the wall line, the tool call (twice with the
        duplicate_webhook fault), the sim_calls record and the transcript."""
        feed.emit(self.db, "sim_reply", f'{call.name} (simulated): "{say}"', recall_id=call.recall_id,
                  party_id=call.party_id, attempt_id=call.attempt_id, source="sim",
                  detail={**record, "call_sid": call.sid})
        transcript.append(_turn("user", say, call.elapsed()))
        entry = {"at": utcnow(), "say": say, **record, "reply": post(body)}
        if repeat:
            entry["duplicate_reply"] = post(body)
        self._update_row(call, None, push={"replies": entry})
        result = entry["reply"]
        answer = _agent_answer(result) if tool == "submit_count" else _agent_noted(result)
        transcript.append(_turn("agent", answer, call.elapsed(), tool=tool, body=body, result=result))
        return result

    def _ring_out(self, call: _Call, ring_s: float, *, reason: str = "no-answer") -> None:
        """Nobody picks up (or the line is engaged): ring, then report the
        failure the way ElevenLabs does."""
        if self._stop.wait(ring_s):
            return
        payload = {
            "type": "call_initiation_failure", "event_timestamp": int(time.time()),
            "data": {"agent_id": "sim", "conversation_id": call.conversation_id, "failure_reason": reason,
                     "metadata": {"type": "twilio",
                                  "body": {"CallSid": call.sid, "CallStatus": reason, "To": call.to}}},
        }
        self._finish(call, payload, reason, utcnow())

    def _finish(self, call: _Call, payload: dict, status: str, ended: datetime) -> None:
        """Deliver the post-call webhook (unless a fault says otherwise), then close the row."""
        if call.faults.get("drop_post_call"):
            log.info("simulated call %s: post-call dropped (fault)", call.sid)
            return
        late = call.faults.get("late_post_call_s")
        if late and self._stop.wait(late):
            return
        raw = json.dumps(payload, default=str).encode("utf-8")
        headers = {signing.HEADER: signing.sign(raw, self.settings.webhook_secret),
                   "Content-Type": "application/json"}
        code = self.poster.post_hook(raw, headers)
        if code != 200:
            log.warning("simulated call %s: post-call webhook answered %s", call.sid, code)
        self._update_row(call, {"status": status, "end_time": ended, "post_call_http": code})

    def _transcription(self, call: _Call, transcript: list, duration: float) -> dict:
        """A post_call_transcription payload shaped like ElevenLabs' (vendor_reference.md section 4)."""
        said = [t["message"] for t in transcript if t["role"] == "user"]
        variables = {**call.variables, "system__conversation_id": call.conversation_id,
                     "system__call_sid": call.sid}
        return {
            "type": "post_call_transcription", "event_timestamp": int(time.time()),
            "data": {
                "agent_id": "sim", "conversation_id": call.conversation_id, "status": "done",
                "transcript": transcript,
                "metadata": {
                    "start_time_unix_secs": int(call.started.timestamp()),
                    "call_duration_secs": int(round(duration)),
                    "termination_reason": "simulated call ended",
                    "phone_call": {"type": "twilio", "direction": "outbound", "external_number": call.to,
                                   "call_sid": call.sid},
                },
                "analysis": {"transcript_summary": f"{call.name} (simulated): " + (" ".join(said) or "no reply")},
                "conversation_initiation_client_data": {"dynamic_variables": variables},
            },
        }

    # -- reads and the row

    def _scenario(self, scenario_id: str | None) -> dict | None:
        if not scenario_id:
            return None
        with self._cv:
            if scenario_id in self._scenarios:
                return self._scenarios[scenario_id]
        try:
            scenario = load_scenario(scenario_id)
        except (OSError, ValueError) as exc:
            log.warning("simulator cannot load scenario %s: %s", scenario_id, exc)
            scenario = None
        with self._cv:
            return self._scenarios.setdefault(scenario_id, scenario)

    def _party_name(self, call: _Call) -> str:
        party = self.db.parties.find_one({"recall_id": call.recall_id, "party_id": call.party_id}, {"name": 1})
        return (party or {}).get("name") or call.party_id

    def _released(self, call: _Call) -> bool:
        key = f"sim_release:{call.recall_id}:{call.party_id}"
        return self.db.worker_state.find_one({"_id": key}, {"_id": 1}) is not None

    def _claimed(self, recall_id: str, party_id: str) -> bool:
        return self.db.claims.find_one({"recall_id": recall_id, "party_id": party_id, "status": "accepted"},
                                       {"_id": 1}) is not None

    def _update_row(self, call: _Call, fields: dict | None, *, push: dict | None = None) -> None:
        """The row is bookkeeping: losing one update must not end the call."""
        update = {}
        if fields:
            update["$set"] = fields
        if push:
            update["$push"] = push
        try:
            self.db.sim_calls.update_one({"_id": call.sid}, update)
        except PyMongoError as exc:
            log.warning("simulated call %s: sim_calls update failed: %s", call.sid, exc)


_BODY_IDS = ("attempt_id", "party_id", "system__conversation_id", "system__call_sid", "source")


def _script_key(purpose: str, scripts: dict) -> str:
    """The script a call plays: the purpose's own script when the party has
    one (a fix check scripted as the installer's visit), else the initial
    script, replayed (a fix check before the visit hears the same answer).
    A question call has no script, so a party with a ground truth answers it
    from its profile."""
    if purpose in ("dispute_followup", "ask_question") or purpose in scripts:
        return purpose
    return "initial"


def _count(value) -> int | None:
    """A dynamic variable that carries a number ("25"), or None."""
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _sim_now(db, recall_id: str) -> float:
    return get_clock(db, recall_id).sim_now(server_now(db.client))


def _tool_body(call: _Call, reply: dict) -> dict:
    """The submit_count body the voice agent would send for this reply. The
    delivery note is only said when the agent asked for it."""
    body = {**(reply.get("claim") or {}), "attempt_id": call.attempt_id, "party_id": call.party_id,
            "system__conversation_id": call.conversation_id, "system__call_sid": call.sid, "source": "sim"}
    note = (reply.get("answers") or {}).get("delivery_note")
    if call.variables.get("ask_delivery_note") == "yes" and note is not None:
        body["delivery_note"] = note
    return body


def _turn(role: str, message: str, at: float, *, tool: str = "submit_count", body: dict | None = None,
          result: dict | None = None) -> dict:
    turn = {"role": role, "message": message, "time_in_call_secs": int(at), "tool_calls": [], "tool_results": []}
    if body is not None:
        turn["tool_calls"] = [{"tool_name": tool, "params_as_json": json.dumps(body, default=str)}]
        turn["tool_results"] = [{"tool_name": tool, "result_value": json.dumps(result, default=str),
                                 "is_error": "error" in (result or {})}]
    return turn


def _opening_line(v: dict) -> str:
    line = (f"Hello, this is {v.get('company') or 'the distributor'} calling about the recall of "
            f"{v.get('product') or 'a product'}, batch {v.get('batch_spoken') or v.get('batch_code') or ''}.").strip()
    return f"{line} {v['followup_note']}" if v.get("followup_note") else line


def _agent_answer(result: dict) -> str:
    if "error" in result:
        return "Sorry, I could not record that just now."
    if result.get("stop"):
        return "Thank you. Someone from our team will follow up with you. Goodbye."
    if result.get("accepted"):
        return "Thank you, that is recorded."
    return "That does not match our records. Could you check the numbers again?"


def _agent_noted(result: dict) -> str:
    return "Sorry, I could not record that just now." if "error" in result else "Thank you, I have noted that."


# ---------------------------------------------------------------- party-initiated events

EVENT_KIND = "sim_event"
PENDING, FIRING, DONE, FAILED, IGNORED = "pending", "firing", "done", "failed", "ignored"
REP_RECHECK_H = 1.0              # the rep checks for an escalation once a simulated hour
RETRY_H = 0.5                    # a submission the API did not take is tried again after this
MAX_TRIES = 5
STALE_FIRING_S = 60.0            # a firing event older than this was left by a dead process
RECALL_REFRESH_S = 5.0           # how often the scheduler looks for newly opened long-run recalls
MIN_WAIT_S = 0.01
BUCKETS = ("in_stock", "installed", "fixed", "returned")


def event_id(recall_id: str, party_id: str, kind: str) -> str:
    return f"{EVENT_KIND}:{recall_id}:{party_id}:{kind}"


class PartyEvents:
    """Party-initiated events in simulated time (V2 section 8).

    Kinds: `form` (a party submits through its form link), `revise` (a
    reviser's correction through its form), `rep` (the distributor's rep
    reports for the unreachable party once an escalate_rep operation exists).

    Each event is one worker_state document `sim_event:<recall>:<party>:<kind>`
    inserted with $setOnInsert, so a re-delivered trigger never schedules it
    twice. The scheduler thread claims a due event (pending -> firing), reads
    the ledger first (a party that already shows these counts is not sent
    them again), posts the submission and closes the event (done). The
    events live in Atlas, so a restarted API process resumes them; an event
    left firing by a dead process is retried after STALE_FIRING_S behind the
    same read-back. reset() clears worker_state, and with it every event.
    """

    def __init__(self, db, poster, *, scenario, stop: threading.Event, max_wait_s: float = 1.0):
        self.db, self.poster = db, poster
        self._scenario, self._stop, self.max_wait_s = scenario, stop, max_wait_s
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._armed: set = set()
        self._armed_at: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-events")
            self._thread.start()

    def join(self, timeout: float) -> None:
        """Wake the scheduler (it exits once the network's stop is set) and wait for it."""
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def schedule(self, recall_id: str, party_id: str, kind: str, due_at: float | None, fields: dict | None, *,
                 token: str | None = None, by: dict | None = None, state: str = PENDING) -> tuple[dict, bool]:
        """Insert the event unless it exists. Returns (the event, created)."""
        key = event_id(recall_id, party_id, kind)
        doc = {"kind": EVENT_KIND, "type": kind, "recall_id": recall_id, "party_id": party_id,
               "due_at": None if due_at is None else round(float(due_at), 4), "fields": fields, "token": token,
               "by": by, "state": state, "tries": 0, "created_at": utcnow()}
        res = self.db.worker_state.update_one({"_id": key}, {"$setOnInsert": doc}, upsert=True)
        if res.upserted_id is not None:
            self._wake.set()
            return {"_id": key, **doc}, True
        return self.db.worker_state.find_one({"_id": key}), False

    # -- the scheduler

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self._tick()
            except PyMongoError as exc:
                log.warning("simulator events: database error, retrying: %s", exc)
                wait = self.max_wait_s
            except Exception:  # noqa: BLE001 - the scheduler must outlive one bad tick
                log.exception("simulator events: tick failed")
                wait = self.max_wait_s
            self._wake.wait(wait)
            self._wake.clear()

    def _tick(self) -> float:
        """Fire every due event; return how long to sleep until the next one."""
        self._arm_rep_reports()
        wait = self.max_wait_s
        real_now = server_now(self.db.client)
        for event in list(self.db.worker_state.find({"kind": EVENT_KIND, "state": {"$in": [PENDING, FIRING]}})):
            if self._stop.is_set():
                break
            if event["state"] == FIRING and not _stale(event):
                continue
            clock = get_clock(self.db, event["recall_id"])
            if clock.sim_now(real_now) + 1e-9 >= event["due_at"]:
                self._fire(event, clock)
                wait = MIN_WAIT_S
            else:
                wait = min(wait, max(MIN_WAIT_S, clock.seconds_until(event["due_at"], real_now)))
        return wait

    def _arm_rep_reports(self) -> None:
        """One `rep` event per unreachable party of every open long-run recall,
        due at its rep_reports_at (the report itself waits for the escalation)."""
        if self._armed_at is not None and time.monotonic() - self._armed_at < RECALL_REFRESH_S:
            return
        self._armed_at = time.monotonic()
        for recall in self.db.recalls.find({"status": "open"},
                                           {"scenario_id": 1, "opened_at": 1}):
            key = (recall["_id"], recall.get("opened_at"))
            if key in self._armed:
                continue
            scenario = self._scenario(recall.get("scenario_id")) or {}
            for party_id, entry in (scenario.get("sim_truth") or {}).items():
                at = profiles.rep_report_at(entry["profile"], entry.get("params"))
                if at is None:
                    continue
                dist = next((s for s in scenario.get("parties", []) if s["party_id"] == entry.get("upstream")), {})
                self.schedule(recall["_id"], party_id, "rep", at, profiles.rep_report(entry["truth"], at),
                              by={"party_id": dist.get("party_id"), "name": dist.get("name"),
                                  "order_system": dist.get("order_system")})
            self._armed.add(key)

    def _fire(self, event: dict, clock) -> None:
        claimed = self.db.worker_state.find_one_and_update(
            {"_id": event["_id"], "state": event["state"]},
            {"$set": {"state": FIRING, "fired_at": utcnow()}}, return_document=ReturnDocument.AFTER)
        if claimed is None:
            return
        handler = self._report if claimed["type"] == "rep" else self._submit_form
        try:
            outcome = handler(claimed, clock.sim_now(server_now(self.db.client)))
        except PyMongoError:
            raise                    # left firing: retried once stale, behind the read-back
        except Exception as exc:  # noqa: BLE001 - a broken event must not stop every other event
            log.exception("simulator event %s failed", claimed["_id"])
            feed.emit(self.db, "error", f"Simulated party event {claimed['_id']} failed: {exc}", level="bad",
                      recall_id=claimed["recall_id"], party_id=claimed["party_id"], source="sim")
            outcome = _closed(FAILED, error=repr(exc))
        self.db.worker_state.update_one({"_id": claimed["_id"], "state": FIRING}, {"$set": outcome})

    def _submit_form(self, event: dict, now: float) -> dict:
        """Post the party's form submission unless the ledger already shows it."""
        party = self._party(event)
        if party is None:
            return _closed(DONE, skipped="the party is not in the ledger")
        fields = event["fields"]
        if _shows(party, fields):
            return _closed(DONE, skipped="the ledger already shows these counts")
        what = "sent a corrected count" if event["type"] == "revise" else "submitted counts"
        feed.emit(self.db, "sim_reply", f"{party['name']} (simulated) {what} through the form: "
                                        f"{profiles.spoken(fields)}",
                  recall_id=event["recall_id"], party_id=event["party_id"], source="sim", system="form",
                  detail={"event": event["_id"], "fields": fields})
        token = event.get("token") or party.get("form_token")
        if not token:
            return _closed(FAILED, error="the party has no form token")
        status = self.poster.post_form(token, fields)
        if status != 200:
            return _retry(event, now, f"the form answered HTTP {status}")
        return _closed(DONE, http=status, booked=_shows(self._party(event) or {}, fields))

    def _report(self, event: dict, now: float) -> dict:
        """The rep reports once the party has been escalated to them."""
        op = self.db.operations.find_one({"recall_id": event["recall_id"], "kind": "escalate_rep",
                                          "party_id": event["party_id"], "state": {"$ne": "failed"}}, {"_id": 1})
        if op is None:
            return {"state": PENDING, "due_at": round(now + REP_RECHECK_H, 4)}
        party = self._party(event)
        fields = event["fields"]
        if party is not None and _shows(party, fields):
            return _closed(DONE, skipped="the ledger already shows these counts", op_id=op["_id"])
        by = event.get("by") or {}
        name = (party or {}).get("name") or event["party_id"]
        feed.emit(self.db, "sim_reply", f"{by.get('name') or 'The distributor'}'s rep reports for {name}: "
                                        f"{profiles.spoken(fields)}",
                  recall_id=event["recall_id"], party_id=event["party_id"], source="sim",
                  system=feed.orders_label(by["order_system"]) if by.get("order_system") else "phone",
                  detail={"event": event["_id"], "op_id": op["_id"], "fields": fields})
        body = {"recall_id": event["recall_id"], "party_id": event["party_id"], "source": "rep",
                "evidence_id": op["_id"], **fields}
        reply = self.poster.post_report(body)
        if "error" in reply:
            return _retry(event, now, reply["error"])
        return _closed(DONE, op_id=op["_id"], reply=reply)

    def _party(self, event: dict) -> dict | None:
        return self.db.parties.find_one({"recall_id": event["recall_id"], "party_id": event["party_id"]},
                                       {"name": 1, "form_token": 1, "state": 1, "forwarded": 1,
                                        **{b: 1 for b in BUCKETS}})


def _stale(event: dict) -> bool:
    fired = event.get("fired_at")
    if fired is None:
        return True
    fired = fired if fired.tzinfo else fired.replace(tzinfo=timezone.utc)
    return (utcnow() - fired).total_seconds() > STALE_FIRING_S


def _closed(state: str, **result) -> dict:
    return {"state": state, "done_at": utcnow(), "result": result}


def _retry(event: dict, now: float, why: str) -> dict:
    tries = int(event.get("tries") or 0) + 1
    if tries >= MAX_TRIES:
        return {**_closed(FAILED, error=why), "tries": tries}
    return {"state": PENDING, "due_at": round(now + RETRY_H, 4), "tries": tries, "last_error": why}


def _shows(party: dict, fields: dict) -> bool:
    """The ledger already holds these counts for the party: the read-back
    that makes a re-fired event harmless."""
    if party.get("state") not in ("claimed", "reconciled"):
        return False
    forwarded = int(fields.get("forwarded_qty") or 0) + sum(f["qty"] for f in fields.get("forwards") or [])
    return (all(int(party.get(b, 0)) == int(fields.get(b, 0)) for b in BUCKETS)
            and int(party.get("forwarded", 0)) == forwarded)


# ---------------------------------------------------------------- worker side

class SimVoice:
    """VoiceProvider that asks the API's simulator to place the call (POST /sim/calls)."""

    name = "sim"

    def __init__(self, api_base_url: str, tool_secret: str, http: httpx.Client | None = None):
        self._base, self._secret = api_base_url.rstrip("/"), tool_secret
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def place_call(self, *, to_number: str, dynamic_variables: dict, attempt_id: str) -> PlacedCall:
        body = {"to_number": to_number, "dynamic_variables": string_variables(dynamic_variables, attempt_id),
                "attempt_id": attempt_id}
        resp = request(self._http, "POST", self._base + CALLS_PATH, vendor="simulator", json=body,
                       headers={TOOL_SECRET_HEADER: self._secret})
        return placed_call(json_body(resp, "simulator"), "simulator")

    def send_form_link(self, *, recall_id: str, party_id: str, token: str) -> dict:
        """Deliver a simulated party's form link (POST /sim/form_link): the
        simulated counterpart of texting the link. Re-sending is harmless."""
        resp = request(self._http, "POST", self._base + FORM_LINK_PATH, vendor="simulator",
                       json={"recall_id": recall_id, "party_id": party_id, "token": token},
                       headers={TOOL_SECRET_HEADER: self._secret})
        return json_body(resp, "simulator")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()


class SimProbe:
    """CallProbe over sim_calls, the simulated Twilio Calls list."""

    name = "sim"

    def __init__(self, db):
        self.db = db

    def find_calls(self, *, to_number: str, since: datetime) -> list[ProbedCall]:
        rows = self.db.sim_calls.find({"to": to_number, "start_time": {"$gte": since}}).sort("start_time", DESCENDING)
        return [_probed(r) for r in rows]

    def call_status(self, call_sid: str) -> ProbedCall | None:
        row = self.db.sim_calls.find_one({"_id": call_sid})
        return _probed(row) if row else None


def _probed(row: dict) -> ProbedCall:
    start = row.get("start_time")
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return ProbedCall(call_sid=row["_id"], status=row.get("status") or "", start_time=start, to=row.get("to") or "",
                      conversation_id=row.get("conversation_id"), raw=row)
