"""The two provider interfaces the worker depends on.

VoiceProvider places a call. CallProbe answers "did a call to this number
start after T?" and "what is this call's status?" for recovery. The real
pair is ElevenLabs (voice.py) + Twilio (twilio_probe.py); the simulator
(sim.py) and the test double (fake_voice.py) implement the same two.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """The provider answered and no call was placed. Safe to treat as failed."""


class ProviderTimeout(ProviderError):
    """The request may or may not have reached the provider (timeout, dropped
    connection, 5xx after sending). The call may exist: the attempt is in doubt."""


@dataclass
class PlacedCall:
    conversation_id: str | None
    call_sid: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class ProbedCall:
    call_sid: str
    status: str                       # Twilio vocabulary: queued ringing in-progress completed busy failed no-answer canceled
    start_time: datetime | None
    to: str
    conversation_id: str | None = None
    raw: dict = field(default_factory=dict)


# Twilio call status -> attempt state. Statuses not listed are still live.
FINAL_STATUS_TO_ATTEMPT = {
    "completed": "completed",
    "busy": "failed",
    "failed": "failed",
    "no-answer": "no_answer",
    "canceled": "failed",
}
LIVE_STATUSES = {"queued", "initiated", "ringing", "in-progress"}


@runtime_checkable
class VoiceProvider(Protocol):
    name: str

    def place_call(self, *, to_number: str, dynamic_variables: dict, attempt_id: str) -> PlacedCall:
        """Ask the provider to dial. Raises ProviderError (no call) or
        ProviderTimeout (outcome unknown)."""
        ...


@runtime_checkable
class CallProbe(Protocol):
    name: str

    def find_calls(self, *, to_number: str, since: datetime) -> list[ProbedCall]:
        """Calls to to_number whose start time is at or after `since`, newest
        first. The caller filters by exact timestamp; providers may only be
        able to filter by date."""
        ...

    def call_status(self, call_sid: str) -> ProbedCall | None:
        ...
