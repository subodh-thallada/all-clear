"""Place one real outbound call through ElevenLabs and Twilio: the smoke test.

    python scripts/call_once.py [--to +1416...] [--watch] [--attempt ATT_ID] [--var key=value ...]

Rings --to (default PHONE_HARBOUR) with the provisioned agent, sending the
dynamic variables the worker sends for a direct party's first call (sample
values from agent/agent_config.json) and attempt_id "att_smoke_<hex>". Prints
the conversation id, the Twilio callSid and how long the outbound-call request
took. With --watch it then polls Twilio every 2 s until the call reaches a
final status and prints the status timeline.

What the API does with the call: during the call the agent sends the counts to
POST /tools/submit_count. The API logs that tool event (so the tool round trip
can be measured) and answers "call not recognised", because no attempt
att_smoke_... exists: nothing is booked and the agent ends the call politely.
Pass --attempt with a live attempt's id (and --var party_id=... with that
attempt's party) to have the counts booked for real.

Needs ELEVEN_API_KEY, ELEVEN_AGENT_ID and ELEVEN_PHONE_ID (run
scripts/provision_agent.py first), and TWILIO_SID / TWILIO_TOKEN for --watch.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levela.config import AGENT_DIR, Settings  # noqa: E402
from levela.providers.base import (FINAL_STATUS_TO_ATTEMPT, ProbedCall,  # noqa: E402
                                   ProviderError, ProviderTimeout)

log = logging.getLogger("call_once")

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
POLL_S = 2.0
# A tier 1 store's first call, as the worker builds it: no sender, no
# receipt, the three checks, no dispute, counts to take and no open questions.
# --var party_type=plant (or buyer) tries the other two scripts.
FIRST_CALL = {"party_type": "store", "receipt": "no", "ask_checks": "yes", "purpose": "initial", "sender_name": "",
              "told_qty": "", "followup_note": "", "ask_delivery_note": "no", "ask_counts": "yes",
              "questions": "", "question_ids": ""}


def placeholders() -> dict[str, str]:
    """The sample value of every dynamic variable the worker sends."""
    config = json.loads((AGENT_DIR / "agent_config.json").read_text(encoding="utf-8"))
    return dict(config["conversation_config"]["agent"]["dynamic_variables"]
                ["dynamic_variable_placeholders"])


def sample_variables(attempt_id: str, overrides: Mapping[str, str]) -> dict[str, str]:
    """Every variable the worker sends, as strings, exactly like a real call."""
    values = {**placeholders(), **FIRST_CALL, **overrides, "attempt_id": attempt_id}
    return {k: str(v) for k, v in values.items()}


def parse_var(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    known = placeholders()
    if not sep or key not in known:
        raise argparse.ArgumentTypeError(
            f"expected key=value with key one of: {', '.join(sorted(known))}")
    if key == "attempt_id":
        raise argparse.ArgumentTypeError("use --attempt to set attempt_id")
    return key, value


def masked(number: str) -> str:
    """Enough of a number to recognise it, not enough to leak it on a screen share."""
    return f"{number[:2]}...{number[-4:]}"


def _probe_once(probe, call_sid: str | None, to_number: str, since: datetime) -> ProbedCall | None:
    """Without a callSid from ElevenLabs, find the call the way recovery does:
    the newest call to the number since the request."""
    if call_sid:
        return probe.call_status(call_sid)
    calls = probe.find_calls(to_number=to_number, since=since)
    return calls[0] if calls else None


def watch(probe, *, call_sid: str | None, to_number: str, since: datetime, timeout_s: float,
          interval_s: float = POLL_S, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic,
          out: Callable[[str], None] = print) -> str | None:
    """Poll until the call reaches a final Twilio status, printing each status
    change with its offset. Returns the final status, or None on timeout."""
    t0, last = clock(), None
    while clock() - t0 <= timeout_s:
        try:
            call = _probe_once(probe, call_sid, to_number, since)
        except (ProviderError, OSError) as exc:     # transient: keep polling
            log.warning("status check failed: %s", exc)
            call = None
        if call is not None:
            call_sid = call.call_sid
            if call.status != last:
                out(f"  {clock() - t0:6.1f} s  {call.status:<12} {call.call_sid}")
                last = call.status
            if call.status in FINAL_STATUS_TO_ATTEMPT:
                duration = (call.raw or {}).get("duration")
                if duration not in (None, ""):
                    out(f"  Twilio duration {duration} s")
                return call.status
        sleep(interval_s)
    out(f"  gave up after {timeout_s:.0f} s; last status {last or 'not seen'}")
    return None


def parse_args(argv):
    p = argparse.ArgumentParser(prog="call_once.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--to", default=None, help="E.164 number to ring (default: PHONE_HARBOUR)")
    p.add_argument("--attempt", default=None,
                   help="existing attempt id, so the API books the counts (default: att_smoke_<hex>)")
    p.add_argument("--var", action="append", type=parse_var, default=[], metavar="KEY=VALUE",
                   help="override one dynamic variable, e.g. --var tier=downstream (repeatable)")
    p.add_argument("--watch", action="store_true",
                   help="poll Twilio every 2 s until the call ends and print the status timeline")
    p.add_argument("--watch-timeout", type=float, default=300.0, metavar="SECONDS",
                   help="stop watching after this long (default 300)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _missing(pairs) -> list[str]:
    return [name for name, value in pairs if not value]


def main(argv=None, *, voice=None, probe=None, env: Mapping[str, str] | None = None,
         sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] = print) -> int:
    """Exit status: 0 placed (and, with --watch, completed), 1 the call failed
    or did not complete, 2 refused to start. voice/probe/env are injectable."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-5s %(message)s")
    settings = Settings.from_env(env=env)
    environ = os.environ if env is None else env
    to_number = args.to or environ.get("PHONE_HARBOUR", "")
    if not E164.match(to_number):
        log.error("--to (or PHONE_HARBOUR) must be an E.164 number such as +14165550123, got %r",
                  to_number)
        return 2

    if voice is None:
        missing = _missing([("ELEVEN_API_KEY", settings.eleven_api_key),
                            ("ELEVEN_AGENT_ID", settings.eleven_agent_id),
                            ("ELEVEN_PHONE_ID", settings.eleven_phone_id)])
        if missing:
            log.error("%s not set: run scripts/provision_agent.py and copy its .env lines",
                      ", ".join(missing))
            return 2
        from levela.providers.voice import ElevenLabsVoice
        voice = ElevenLabsVoice(settings)
    if args.watch and probe is None:
        missing = _missing([("TWILIO_SID", settings.twilio_sid),
                            ("TWILIO_TOKEN", settings.twilio_token)])
        if missing:
            log.error("--watch needs %s", ", ".join(missing))
            return 2
        from levela.providers.twilio_probe import TwilioProbe
        probe = TwilioProbe(settings)

    attempt_id = args.attempt or f"att_smoke_{secrets.token_hex(5)}"
    variables = sample_variables(attempt_id, dict(args.var))
    out(f"Calling {masked(to_number)} as {variables['party_name']} ({variables['party_id']}), "
        f"attempt {attempt_id}")
    if not args.attempt:
        out("The API will answer the agent's submit_count as 'call not recognised' "
            "(no such attempt); nothing is booked.")

    requested_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    try:
        placed = voice.place_call(to_number=to_number, dynamic_variables=variables,
                                  attempt_id=attempt_id)
    except ProviderTimeout as exc:
        out(f"No answer from ElevenLabs after {time.perf_counter() - t0:.2f} s: {exc}")
        out("The call may still ring. Check Twilio's call log before trying again.")
        return 1
    except ProviderError as exc:
        out(f"ElevenLabs placed no call ({time.perf_counter() - t0:.2f} s): {exc}")
        return 1
    rtt = time.perf_counter() - t0
    out(f"conversation_id  {placed.conversation_id or '(not returned)'}")
    out(f"callSid          {placed.call_sid or '(not returned)'}")
    out(f"request took     {rtt * 1000:.0f} ms")
    if not args.watch:
        return 0

    out("Status timeline:")
    final = watch(probe, call_sid=placed.call_sid, to_number=to_number,
                  since=requested_at - timedelta(seconds=settings.probe_skew_s),
                  timeout_s=args.watch_timeout, sleep=sleep, out=out)
    return 0 if final == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
