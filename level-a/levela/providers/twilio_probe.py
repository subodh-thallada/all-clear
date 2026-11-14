"""Twilio's Calls API as the recovery probe (vendor_reference.md section 6).

Recovery asks two questions before it may ever redial: "did a call to this
number start at or after T?" and "what became of call CA...?". Twilio can
only filter the list by UTC date, so the list is fetched from T's date and
filtered to the exact instant here. Calls still queued or ringing have no
start_time yet; their creation time stands in, so a call Twilio is still
setting up is found rather than redialled.

A probe failure raises ProviderError/ProviderTimeout like the voice
providers; recovery treats either as "no information" and defers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .base import ProbedCall, ProviderError
from .voice import DEFAULT_TIMEOUT, json_body, mask_number, request

log = logging.getLogger("levela.providers.twilio")

API_ROOT = "https://api.twilio.com"
PAGE_SIZE = 100
MAX_PAGES = 5


def as_utc(when: datetime) -> datetime:
    """Naive datetimes are UTC (as pymongo returns them without tz_aware)."""
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    """Twilio's RFC 2822 timestamps ("Fri, 18 Oct 2019 17:02:00 +0000") as aware UTC."""
    if not value:
        return None
    try:
        return as_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError):
        log.warning("unparseable Twilio timestamp %r", value)
        return None


def to_probed(call: dict) -> ProbedCall:
    started = parse_time(call.get("start_time")) or parse_time(call.get("date_created"))
    return ProbedCall(call_sid=call["sid"], status=call.get("status") or "", start_time=started,
                      to=call.get("to") or "", raw=call)


class TwilioProbe:
    """CallProbe over Twilio's Calls list and fetch endpoints."""

    name = "twilio"

    def __init__(self, settings, http: httpx.Client | None = None):
        self.settings = settings
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def _auth(self) -> tuple[str, str]:
        s = self.settings
        missing = [env for env, value in (("TWILIO_SID", s.twilio_sid), ("TWILIO_TOKEN", s.twilio_token))
                   if not value]
        if missing:
            raise ProviderError(f"Twilio is not configured: set {', '.join(missing)}")
        return s.twilio_sid, s.twilio_token

    def _calls_url(self, suffix: str) -> str:
        return f"{API_ROOT}/2010-04-01/Accounts/{self.settings.twilio_sid}/Calls{suffix}"

    def find_calls(self, *, to_number: str, since: datetime) -> list[ProbedCall]:
        """Calls to to_number that started (or were created, if not started)
        at or after `since`, newest first."""
        auth = self._auth()
        since = as_utc(since)
        url = self._calls_url(".json")
        params: dict | None = {"To": to_number, "StartTime>": since.date().isoformat(), "PageSize": PAGE_SIZE}
        found: dict[str, ProbedCall] = {}
        for _ in range(MAX_PAGES):
            page = json_body(request(self._http, "GET", url, vendor="Twilio", params=params, auth=auth), "Twilio")
            for call in page.get("calls") or []:
                probed = to_probed(call)
                found.setdefault(probed.call_sid, probed)
            next_uri = page.get("next_page_uri")
            if not next_uri:
                break
            url, params = f"{API_ROOT}{next_uri}", None
        else:
            log.warning("more than %d pages of calls to %s since %s; only the first %d pages were read",
                        MAX_PAGES, mask_number(to_number), since.isoformat(), MAX_PAGES)
        recent = [c for c in found.values() if c.start_time is not None and c.start_time >= since]
        return sorted(recent, key=lambda c: c.start_time, reverse=True)

    def call_status(self, call_sid: str) -> ProbedCall | None:
        """The call as Twilio records it now, or None if Twilio has no such call."""
        resp = request(self._http, "GET", self._calls_url(f"/{call_sid}.json"), vendor="Twilio",
                       allow=(404,), auth=self._auth())
        if resp.status_code == 404:
            return None
        return to_probed(json_body(resp, "Twilio"))

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
