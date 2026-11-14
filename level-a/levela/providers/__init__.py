"""Voice providers and call probes, keyed by the channel they serve.

The worker dials `voice` parties through ElevenLabs (a real phone) unless
VOICE_PROVIDER=sim, and `sim` parties always through the simulator hosted by
the API. Each provider has a matching probe that recovery asks before it may
redial: Twilio's Calls list for real calls, `sim_calls` for simulated ones.
"""
from __future__ import annotations

import logging

from .base import (FINAL_STATUS_TO_ATTEMPT, LIVE_STATUSES, CallProbe, PlacedCall, ProbedCall, ProviderError,
                   ProviderTimeout, VoiceProvider)
from .fake_voice import CallsProbe, FakeVoice
from .sim import ClientPoster, HttpPoster, SimNetwork, SimProbe, SimVoice
from .twilio_probe import TwilioProbe
from .voice import ElevenLabsVoice

log = logging.getLogger("levela.providers")

VOICE_PROVIDERS = ("elevenlabs", "sim")


def build_providers(settings, db) -> tuple[dict[str, VoiceProvider], dict[str, CallProbe]]:
    """(providers, probes), each {"voice": ..., "sim": ...} (DESIGN.md section 11).

    An unknown VOICE_PROVIDER is refused rather than silently simulated: in
    the live demo a typo would otherwise ring no phone at all.
    """
    sim_voice, sim_probe = SimVoice(settings.api_base_url, settings.tool_secret), SimProbe(db)
    if settings.voice_provider == "elevenlabs":
        voice = ElevenLabsVoice(settings)
        missing = voice.missing_config()
        if missing:
            log.warning("voice parties cannot be dialled until %s %s set", ", ".join(missing),
                        "is" if len(missing) == 1 else "are")
        providers = {"voice": voice, "sim": sim_voice}
        probes = {"voice": TwilioProbe(settings), "sim": sim_probe}
    elif settings.voice_provider == "sim":
        providers, probes = {"voice": sim_voice, "sim": sim_voice}, {"voice": sim_probe, "sim": sim_probe}
    else:
        raise ValueError(f"VOICE_PROVIDER must be one of {', '.join(VOICE_PROVIDERS)}, "
                         f"not {settings.voice_provider!r}")
    return providers, probes


__all__ = ["FINAL_STATUS_TO_ATTEMPT", "LIVE_STATUSES", "CallProbe", "CallsProbe", "ClientPoster", "ElevenLabsVoice",
           "FakeVoice", "HttpPoster", "PlacedCall", "ProbedCall", "ProviderError", "ProviderTimeout", "SimNetwork",
           "SimProbe", "SimVoice", "TwilioProbe", "VOICE_PROVIDERS", "VoiceProvider", "build_providers"]
