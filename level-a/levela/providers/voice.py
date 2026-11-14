"""ElevenLabs outbound calls (vendor_reference.md section 1), and the HTTP
error mapping every provider in this package shares.

The mapping is the crash-safety core. ProviderError means "no call exists":
the worker marks the attempt failed and may dial again later. ProviderTimeout
means "a call may exist": the attempt stays in doubt and only recovery, from
evidence, may decide. So a failure is an error only when the request provably
never reached the vendor (connection refused, DNS failure, connect timeout)
or the vendor answered that it placed nothing (4xx, success false). Anything
after the request could have been received (read timeout, dropped
connection, 5xx, an unreadable 2xx) is a timeout.
"""
from __future__ import annotations

import json
import logging

import httpx

from .base import PlacedCall, ProviderError, ProviderTimeout

log = logging.getLogger("levela.providers.voice")

OUTBOUND_CALL_PATH = "/v1/convai/twilio/outbound-call"
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# The agent must never hear the shipment record: it would lead the party to
# the number the database is about to check.
FORBIDDEN_VARIABLES = ("shipped_in",)

# Raised before any byte of the request left this process: no call can exist.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
             httpx.LocalProtocolError, httpx.ProxyError, httpx.InvalidURL)


def request(http: httpx.Client, method: str, url: str, *, vendor: str, allow: tuple[int, ...] = (),
            timeout=DEFAULT_TIMEOUT, **kwargs) -> httpx.Response:
    """Send one request and map every failure onto ProviderError/ProviderTimeout.

    Returns the response for 2xx and for the statuses in `allow` (a probe
    treats 404 as an answer). Never logs headers: they carry the API key.
    """
    try:
        resp = http.request(method, url, timeout=timeout, **kwargs)
    except _NOT_SENT as exc:
        log.warning("%s %s %s not sent: %r", vendor, method, url, exc)
        raise ProviderError(f"{vendor} unreachable, nothing was sent: {exc!r}") from exc
    except httpx.HTTPError as exc:
        log.warning("%s %s %s failed after sending: %r", vendor, method, url, exc)
        raise ProviderTimeout(f"{vendor} did not answer after the request was sent: {exc!r}") from exc
    code = resp.status_code
    if code in allow or 200 <= code < 300:
        return resp
    detail = error_detail(resp)
    log.warning("%s %s %s answered HTTP %s: %s", vendor, method, url, code, detail)
    if code >= 500:
        raise ProviderTimeout(f"{vendor} HTTP {code} (the request may have been acted on): {detail}")
    raise ProviderError(f"{vendor} refused the request (HTTP {code}): {detail}")


def error_detail(resp: httpx.Response) -> str:
    """The vendor's own explanation: FastAPI-style `detail` lists (ElevenLabs
    422), `detail.message`, Twilio's `message`, else the start of the body."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:300] or resp.reason_phrase
    if not isinstance(data, dict):
        return json.dumps(data)[:300]
    detail = data.get("detail", data.get("message"))
    if isinstance(detail, list):
        return "; ".join(_field_error(d) for d in detail)
    if isinstance(detail, dict):
        return str(detail.get("message") or json.dumps(detail)[:300])
    return str(detail) if detail else json.dumps(data)[:300]


def _field_error(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    where = ".".join(str(p) for p in item.get("loc") or [])
    return f"{where}: {item.get('msg')}" if where else str(item.get("msg"))


def json_body(resp: httpx.Response, vendor: str) -> dict:
    """Parse a 2xx body. A success status we cannot read may still mean a
    call was placed, so it is a timeout, not an error."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderTimeout(f"{vendor} answered HTTP {resp.status_code} with a body that is not JSON") from exc
    if not isinstance(data, dict):
        raise ProviderTimeout(f"{vendor} answered HTTP {resp.status_code} with JSON that is not an object")
    return data


def placed_call(data: dict, vendor: str) -> PlacedCall:
    """Interpret an outbound-call reply {success, message, conversation_id, callSid}.

    success true is a placed call. success false without a call SID is a
    refusal. Anything else (no success flag, or false while naming a call)
    leaves the call's existence open, so the attempt goes in doubt and
    recovery asks Twilio instead of redialling.
    """
    success, call_sid = data.get("success"), data.get("callSid")
    if success is True:
        return PlacedCall(conversation_id=data.get("conversation_id"), call_sid=call_sid, raw=data)
    message = data.get("message") or "no message"
    if success is False and not call_sid:
        raise ProviderError(f"{vendor} did not place the call: {message}")
    raise ProviderTimeout(f"{vendor} gave an ambiguous answer (success={success!r}, callSid={call_sid!r}): "
                          f"{message}")


def string_variables(dynamic_variables: dict | None, attempt_id: str) -> dict[str, str]:
    """Dynamic variables as the agent receives them: every value a string
    (a variable the prompt references but the call lacks can break the call),
    always carrying attempt_id, never the shipment record."""
    out: dict[str, str] = {}
    for key, value in (dynamic_variables or {}).items():
        if key in FORBIDDEN_VARIABLES:
            log.warning("dropping dynamic variable %s for %s: the agent must not hear it", key, attempt_id)
            continue
        out[str(key)] = "" if value is None else str(value)
    out["attempt_id"] = attempt_id
    return out


def mask_number(number: str | None) -> str:
    """A phone number fit for a log line: country code and last four digits."""
    n = number or ""
    return n if len(n) <= 6 else f"{n[:2]}{'*' * (len(n) - 6)}{n[-4:]}"


class ElevenLabsVoice:
    """VoiceProvider backed by ElevenLabs' Twilio outbound-call API."""

    name = "elevenlabs"

    def __init__(self, settings, http: httpx.Client | None = None):
        self.settings = settings
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def missing_config(self) -> list[str]:
        """Environment variables that must be set before a call can be placed."""
        s = self.settings
        return [env for env, value in (("ELEVEN_API_KEY", s.eleven_api_key),
                                       ("ELEVEN_AGENT_ID", s.eleven_agent_id),
                                       ("ELEVEN_PHONE_ID", s.eleven_phone_id)) if not value]

    def place_call(self, *, to_number: str, dynamic_variables: dict, attempt_id: str) -> PlacedCall:
        missing = self.missing_config()
        if missing:
            raise ProviderError(f"ElevenLabs is not configured: set {', '.join(missing)}")
        s = self.settings
        body = {
            "agent_id": s.eleven_agent_id,
            "agent_phone_number_id": s.eleven_phone_id,
            "to_number": to_number,
            "conversation_initiation_client_data": {
                "dynamic_variables": string_variables(dynamic_variables, attempt_id)},
            "telephony_call_config": {"ringing_timeout_secs": int(s.ringing_timeout_s)},
        }
        log.info("ElevenLabs outbound call for %s to %s", attempt_id, mask_number(to_number))
        resp = request(self._http, "POST", f"{s.eleven_base_url.rstrip('/')}{OUTBOUND_CALL_PATH}", vendor="ElevenLabs",
                       json=body, headers={"xi-api-key": s.eleven_api_key})
        placed = placed_call(json_body(resp, "ElevenLabs"), "ElevenLabs")
        log.info("ElevenLabs placed %s: conversation %s, call %s", attempt_id, placed.conversation_id,
                 placed.call_sid)
        return placed

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
