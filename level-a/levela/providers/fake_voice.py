"""FakeVoice: the in-process VoiceProvider tests drive, with fault injection.

Crash safety is decided by what happens around the provider call, so the
fake can produce each outcome on demand:

  drop_response  the call is placed, then the answer is lost (ProviderTimeout):
                 the lost-response case recovery must adopt, never redial
  fail_before    the request never reached the provider (ProviderTimeout):
                 recovery must find no call and abandon
  reject         the provider refused (ProviderError): no call exists
  any SimNetwork fault (duplicate_webhook, late_post_call_s, drop_post_call,
                 no_answer) is passed through to the network

`faults` is a dict {attempt_id | party_id | "*": fault} (looked up in that
order) or a callable (attempt_id, party_id, n_calls_so_far) -> fault | None,
where n_calls_so_far counts every place_call this fake has seen. A fault is
a name, or a dict of names to values ({"late_post_call_s": 2}) that may also
name one of the fake's own faults.

With a network, a placed call is really played by SimNetwork and `.probe` is
SimProbe over sim_calls. Without one, ids are fabricated, nothing is stored,
and `.probe` reads this fake's own call log (every placed call "completed").
"""
from __future__ import annotations

import secrets
import threading
from datetime import datetime, timezone

from .base import PlacedCall, ProbedCall, ProviderError, ProviderTimeout
from .sim import FAULTS as SIM_FAULTS
from .sim import SimProbe, check_faults
from .twilio_probe import as_utc

OWN_FAULTS = ("drop_response", "fail_before", "reject")


def split_fault(fault) -> tuple[str | None, dict]:
    """(the fake's own fault or None, the faults dict for the network)."""
    if not fault:
        return None, {}
    spec = {fault: True} if isinstance(fault, str) else dict(fault)
    own = [name for name in OWN_FAULTS if spec.pop(name, None)]
    if len(own) > 1:
        raise ValueError(f"one call cannot have more than one of {', '.join(own)}")
    return (own[0] if own else None), check_faults(spec)


class FakeVoice:
    """VoiceProvider test double. Records every request in `.calls`."""

    def __init__(self, network=None, *, faults=None, name: str = "fake"):
        self.network, self.faults, self.name = network, faults, name
        self.calls: list[dict] = []
        self._lock = threading.RLock()
        self.probe = SimProbe(network.db) if network is not None else CallsProbe(self)

    def place_call(self, *, to_number: str, dynamic_variables: dict, attempt_id: str) -> PlacedCall:
        party_id = (dynamic_variables or {}).get("party_id")
        with self._lock:
            fault = self._fault_for(attempt_id, party_id)
            entry = {"to": to_number, "attempt_id": attempt_id, "party_id": party_id,
                     "dynamic_variables": dict(dynamic_variables or {}), "at": datetime.now(timezone.utc),
                     "placed": False, "fault": fault, "conversation_id": None, "call_sid": None}
            self.calls.append(entry)
        own, sim_faults = split_fault(fault)
        if own == "fail_before":
            raise ProviderTimeout(f"fake: the request for {attempt_id} never reached the provider")
        if own == "reject":
            raise ProviderError(f"fake: the provider refused the call for {attempt_id}")
        placed = self._dial(to_number, dynamic_variables, attempt_id, sim_faults)
        with self._lock:
            entry.update(placed=True, conversation_id=placed.conversation_id, call_sid=placed.call_sid)
        if own == "drop_response":
            raise ProviderTimeout(f"fake: call {placed.call_sid} placed for {attempt_id}, the answer was lost")
        return placed

    def placed_calls(self, party_id: str | None = None) -> list[dict]:
        """Calls that really rang a (simulated) phone, oldest first."""
        with self._lock:
            return [dict(c) for c in self.calls if c["placed"] and (party_id is None or c["party_id"] == party_id)]

    def count(self, party_id: str | None = None) -> int:
        """How many times a phone rang: the number a no-double-dial test asserts on."""
        return len(self.placed_calls(party_id))

    def _fault_for(self, attempt_id: str, party_id: str | None):
        if self.faults is None:
            return None
        if callable(self.faults):
            return self.faults(attempt_id, party_id, len(self.calls))
        for key in (attempt_id, party_id, "*"):
            if key in self.faults:
                return self.faults[key]
        return None

    def _dial(self, to_number, dynamic_variables, attempt_id, sim_faults) -> PlacedCall:
        if self.network is not None:
            return self.network.place_call(to_number=to_number, dynamic_variables=dynamic_variables,
                                           attempt_id=attempt_id, faults=sim_faults or None)
        if sim_faults:
            raise ValueError(f"simulator faults {', '.join(sim_faults)} need a SimNetwork "
                             f"(choose from {', '.join(SIM_FAULTS)} only with FakeVoice(network))")
        sid, conversation_id = "CA" + secrets.token_hex(16), "conv_fake_" + secrets.token_hex(12)
        return PlacedCall(conversation_id=conversation_id, call_sid=sid,
                          raw={"success": True, "message": "fake call placed", "conversation_id": conversation_id,
                               "callSid": sid})


class CallsProbe:
    """CallProbe over a FakeVoice's own log, for tests without a SimNetwork."""

    name = "fake"

    def __init__(self, voice: FakeVoice):
        self._voice = voice

    def find_calls(self, *, to_number: str, since: datetime) -> list[ProbedCall]:
        since = as_utc(since)
        hits = [_probed(c) for c in self._voice.placed_calls() if c["to"] == to_number and c["at"] >= since]
        return sorted(hits, key=lambda c: c.start_time, reverse=True)

    def call_status(self, call_sid: str) -> ProbedCall | None:
        return next((_probed(c) for c in self._voice.placed_calls() if c["call_sid"] == call_sid), None)


def _probed(entry: dict) -> ProbedCall:
    return ProbedCall(call_sid=entry["call_sid"], status="completed", start_time=entry["at"], to=entry["to"],
                      conversation_id=entry["conversation_id"], raw=entry)
