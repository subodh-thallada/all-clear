"""Providers without a database: the ElevenLabs request and its error
mapping, the Twilio probe, SimVoice, the posters, FakeVoice and
build_providers. Every HTTP exchange goes through httpx.MockTransport."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from levela.providers import (CallsProbe, ClientPoster, ElevenLabsVoice, FakeVoice, HttpPoster, PlacedCall,
                              ProviderError, ProviderTimeout, SimProbe, SimVoice, TwilioProbe, build_providers)
from levela.providers.twilio_probe import API_ROOT, MAX_PAGES, parse_time

API_KEY = "xi-test-key-0123456789"
ELEVEN_URL = "https://eleven.test/v1/convai/twilio/outbound-call"
TO = "+14165550141"


def mock_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_reply(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


def raises_exactly(exc_type, fn):
    """ProviderTimeout subclasses ProviderError, so pytest.raises(ProviderError)
    would also pass for a timeout; the distinction is the point here."""
    with pytest.raises(ProviderError) as info:
        fn()
    assert type(info.value) is exc_type, f"expected {exc_type.__name__}, got {type(info.value).__name__}"
    return info.value


@pytest.fixture
def eleven_settings(settings):
    return replace(settings, eleven_api_key=API_KEY, eleven_agent_id="agent_test", eleven_phone_id="phnum_test",
                   eleven_base_url="https://eleven.test", ringing_timeout_s=20)


@pytest.fixture
def twilio_settings(settings):
    return replace(settings, twilio_sid="ACtest", twilio_token="tw-token")


def place(voice, **variables):
    return voice.place_call(to_number=TO, attempt_id="att_0001",
                            dynamic_variables={"party_id": "harbour-fire", **variables})


# ---------------------------------------------------------------- ElevenLabs

def test_elevenlabs_request_body_and_headers(eleven_settings):
    seen = []

    def handler(request):
        seen.append(request)
        return json_reply(200, {"success": True, "message": "ok", "conversation_id": "conv_1", "callSid": "CA1"})

    voice = ElevenLabsVoice(eleven_settings, http=mock_http(handler))
    placed = place(voice, party_name="Harbour Fire", told_qty=25, sender_name=None, shipped_in=40)

    assert placed == PlacedCall("conv_1", "CA1", {"success": True, "message": "ok", "conversation_id": "conv_1",
                                                  "callSid": "CA1"})
    (req,) = seen
    assert req.method == "POST" and str(req.url) == ELEVEN_URL
    assert req.headers["xi-api-key"] == API_KEY
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {
        "agent_id": "agent_test", "agent_phone_number_id": "phnum_test", "to_number": TO,
        "conversation_initiation_client_data": {"dynamic_variables": {
            "party_id": "harbour-fire", "party_name": "Harbour Fire", "told_qty": "25", "sender_name": "",
            "attempt_id": "att_0001"}},
        "telephony_call_config": {"ringing_timeout_secs": 20},
    }


def _raise(exc_type, message="boom"):
    def handler(request):
        raise exc_type(message, request=request)
    return handler


@pytest.mark.parametrize("handler, expected", [
    pytest.param(_raise(httpx.ConnectError, "connection refused"), ProviderError, id="connect-refused"),
    pytest.param(_raise(httpx.ConnectError, "getaddrinfo failed"), ProviderError, id="dns-failure"),
    pytest.param(_raise(httpx.ConnectTimeout), ProviderError, id="connect-timeout"),
    pytest.param(_raise(httpx.ReadTimeout), ProviderTimeout, id="read-timeout"),
    pytest.param(_raise(httpx.WriteTimeout), ProviderTimeout, id="write-timeout"),
    pytest.param(_raise(httpx.RemoteProtocolError, "Server disconnected"), ProviderTimeout, id="dropped"),
    pytest.param(_raise(httpx.ReadError), ProviderTimeout, id="read-error"),
    pytest.param(lambda r: json_reply(500, {"detail": "internal"}), ProviderTimeout, id="500"),
    pytest.param(lambda r: httpx.Response(503, text="unavailable"), ProviderTimeout, id="503"),
    pytest.param(lambda r: json_reply(401, {"detail": {"status": "invalid_api_key", "message": "bad key"}}),
                 ProviderError, id="401"),
    pytest.param(lambda r: json_reply(429, {"detail": "rate limited"}), ProviderError, id="429"),
    pytest.param(lambda r: httpx.Response(200, text="<html>gateway</html>"), ProviderTimeout, id="200-not-json"),
    pytest.param(lambda r: json_reply(200, {"success": False, "message": "busy", "callSid": "CA9"}),
                 ProviderTimeout, id="200-false-with-call"),
    pytest.param(lambda r: json_reply(200, {"message": "accepted"}), ProviderTimeout, id="200-no-success-flag"),
])
def test_elevenlabs_error_mapping(eleven_settings, handler, expected):
    voice = ElevenLabsVoice(eleven_settings, http=mock_http(handler))
    raises_exactly(expected, lambda: place(voice))


def test_elevenlabs_422_carries_the_detail(eleven_settings):
    detail = [{"loc": ["body", "to_number"], "msg": "value is not a valid phone number", "type": "value_error"}]
    voice = ElevenLabsVoice(eleven_settings, http=mock_http(lambda r: json_reply(422, {"detail": detail})))
    err = raises_exactly(ProviderError, lambda: place(voice))
    assert "422" in str(err) and "body.to_number: value is not a valid phone number" in str(err)


def test_elevenlabs_success_false_is_an_error_with_the_message(eleven_settings):
    reply = {"success": False, "message": "Twilio rejected the number", "conversation_id": None, "callSid": None}
    voice = ElevenLabsVoice(eleven_settings, http=mock_http(lambda r: json_reply(200, reply)))
    err = raises_exactly(ProviderError, lambda: place(voice))
    assert "Twilio rejected the number" in str(err)


def test_elevenlabs_missing_config_names_the_env_vars(settings):
    def handler(request):
        raise AssertionError("nothing may be sent without credentials")

    voice = ElevenLabsVoice(replace(settings, eleven_agent_id="agent_test"), http=mock_http(handler))
    assert voice.missing_config() == ["ELEVEN_API_KEY", "ELEVEN_PHONE_ID"]
    err = raises_exactly(ProviderError, lambda: place(voice))
    assert "ELEVEN_API_KEY" in str(err) and "ELEVEN_PHONE_ID" in str(err) and "ELEVEN_AGENT_ID" not in str(err)


def test_elevenlabs_never_logs_the_api_key(eleven_settings, caplog):
    caplog.set_level(logging.DEBUG)
    ok = ElevenLabsVoice(eleven_settings, http=mock_http(
        lambda r: json_reply(200, {"success": True, "conversation_id": "c", "callSid": "CA2"})))
    place(ok)
    bad = ElevenLabsVoice(eleven_settings, http=mock_http(lambda r: json_reply(400, {"detail": "nope"})))
    raises_exactly(ProviderError, lambda: place(bad))
    assert caplog.records and API_KEY not in caplog.text
    assert TO not in caplog.text, "phone numbers are masked in logs"


# ---------------------------------------------------------------- Twilio

def rfc2822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def twilio_call(sid, *, start=None, created=None, status="completed"):
    return {"sid": sid, "to": TO, "from": "+14165550100", "status": status, "direction": "outbound-api",
            "start_time": rfc2822(start) if start else None, "date_created": rfc2822(created or start)}


def test_parse_time_reads_rfc2822_as_utc():
    assert parse_time("Fri, 18 Oct 2019 17:02:00 +0000") == datetime(2019, 10, 18, 17, 2, tzinfo=timezone.utc)
    assert parse_time("Fri, 18 Oct 2019 13:02:00 -0400") == datetime(2019, 10, 18, 17, 2, tzinfo=timezone.utc)
    assert parse_time("") is None and parse_time(None) is None and parse_time("yesterday") is None


def test_twilio_find_calls_pages_and_filters_precisely(twilio_settings):
    since = datetime(2026, 11, 14, 15, 30, 0, tzinfo=timezone.utc)
    page_1 = {"calls": [
        twilio_call("CAqueued", created=since + timedelta(seconds=40), status="queued"),
        twilio_call("CAafter", start=since + timedelta(seconds=20), status="in-progress"),
        twilio_call("CAexact", start=since),
    ], "next_page_uri": "/2010-04-01/Accounts/ACtest/Calls.json?To=%2B14165550141&PageSize=100&Page=1&PageToken=PA1"}
    page_2 = {"calls": [
        twilio_call("CAearlier", start=since - timedelta(seconds=1)),             # same date, before since
        twilio_call("CAold_queued", created=since - timedelta(minutes=5), status="queued"),
    ], "next_page_uri": None}
    seen = []

    def handler(request):
        seen.append(request)
        return json_reply(200, page_1 if len(seen) == 1 else page_2)

    calls = TwilioProbe(twilio_settings, http=mock_http(handler)).find_calls(to_number=TO, since=since)

    assert [c.call_sid for c in calls] == ["CAqueued", "CAafter", "CAexact"]
    assert calls[0].start_time == since + timedelta(seconds=40) and calls[0].status == "queued"
    assert calls[1].to == TO and calls[1].raw["direction"] == "outbound-api"
    first, second = seen
    assert f"{first.url.scheme}://{first.url.host}{first.url.path}" == \
        f"{API_ROOT}/2010-04-01/Accounts/ACtest/Calls.json"
    assert dict(first.url.params) == {"To": TO, "StartTime>": "2026-11-14", "PageSize": "100"}
    assert first.headers["authorization"] == "Basic " + base64.b64encode(b"ACtest:tw-token").decode()
    assert str(second.url) == API_ROOT + page_1["next_page_uri"]
    assert second.headers["authorization"] == first.headers["authorization"]


def test_twilio_uses_the_utc_date_of_since(twilio_settings):
    seen = []

    def handler(request):
        seen.append(request)
        return json_reply(200, {"calls": [], "next_page_uri": None})

    probe = TwilioProbe(twilio_settings, http=mock_http(handler))
    toronto_evening = datetime(2026, 11, 14, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert probe.find_calls(to_number=TO, since=toronto_evening) == []
    probe.find_calls(to_number=TO, since=datetime(2026, 11, 14, 23, 59))          # naive means UTC
    assert [r.url.params["StartTime>"] for r in seen] == ["2026-11-15", "2026-11-14"]


def test_twilio_reads_at_most_five_pages(twilio_settings):
    since = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    seen = []

    def handler(request):
        seen.append(request)
        n = len(seen)
        return json_reply(200, {"calls": [twilio_call(f"CA{n}", start=since + timedelta(minutes=n))],
                                "next_page_uri": f"/2010-04-01/Accounts/ACtest/Calls.json?Page={n}"})

    calls = TwilioProbe(twilio_settings, http=mock_http(handler)).find_calls(to_number=TO, since=since)
    assert len(seen) == MAX_PAGES
    assert [c.call_sid for c in calls] == [f"CA{n}" for n in range(MAX_PAGES, 0, -1)]


def test_twilio_call_status(twilio_settings):
    start = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)

    def handler(request):
        if request.url.path.endswith("/CAgone.json"):
            return json_reply(404, {"code": 20404, "message": "not found", "status": 404})
        assert request.url.path == "/2010-04-01/Accounts/ACtest/Calls/CAlive.json"
        return json_reply(200, twilio_call("CAlive", start=start, status="no-answer"))

    probe = TwilioProbe(twilio_settings, http=mock_http(handler))
    live = probe.call_status("CAlive")
    assert (live.call_sid, live.status, live.start_time, live.to) == ("CAlive", "no-answer", start, TO)
    assert probe.call_status("CAgone") is None


@pytest.mark.parametrize("handler, expected", [
    pytest.param(lambda r: json_reply(500, {"message": "oops"}), ProviderTimeout, id="500"),
    pytest.param(lambda r: json_reply(401, {"code": 20003, "message": "Authenticate"}), ProviderError, id="401"),
    pytest.param(_raise(httpx.ConnectError), ProviderError, id="connect"),
    pytest.param(_raise(httpx.ReadTimeout), ProviderTimeout, id="read-timeout"),
])
def test_twilio_errors_map_like_the_voice_provider(twilio_settings, handler, expected):
    probe = TwilioProbe(twilio_settings, http=mock_http(handler))
    raises_exactly(expected, lambda: probe.find_calls(to_number=TO, since=datetime.now(timezone.utc)))
    raises_exactly(expected, lambda: probe.call_status("CA1"))


def test_twilio_missing_config(settings):
    probe = TwilioProbe(replace(settings, twilio_sid="ACtest"), http=mock_http(lambda r: json_reply(200, {})))
    err = raises_exactly(ProviderError, lambda: probe.call_status("CA1"))
    assert "TWILIO_TOKEN" in str(err) and "TWILIO_SID" not in str(err)


# ---------------------------------------------------------------- SimVoice

def test_sim_voice_posts_to_the_simulator():
    seen = []

    def handler(request):
        seen.append(request)
        return json_reply(200, {"success": True, "conversation_id": "conv_sim_1", "callSid": "CA" + "0" * 32})

    voice = SimVoice("http://api.test/", "tool-secret", http=mock_http(handler))
    placed = place(voice, ask_delivery_note="yes", told_qty=None)
    assert (placed.conversation_id, placed.call_sid) == ("conv_sim_1", "CA" + "0" * 32)
    (req,) = seen
    assert req.method == "POST" and str(req.url) == "http://api.test/sim/calls"
    assert req.headers["x-tool-secret"] == "tool-secret"
    assert json.loads(req.content) == {"to_number": TO, "attempt_id": "att_0001", "dynamic_variables": {
        "party_id": "harbour-fire", "ask_delivery_note": "yes", "told_qty": "", "attempt_id": "att_0001"}}


@pytest.mark.parametrize("handler, expected", [
    pytest.param(_raise(httpx.ConnectError), ProviderError, id="api-down"),
    pytest.param(_raise(httpx.ReadTimeout), ProviderTimeout, id="read-timeout"),
    pytest.param(lambda r: json_reply(500, {"detail": "boom"}), ProviderTimeout, id="500"),
    pytest.param(lambda r: json_reply(401, {"detail": "bad tool secret"}), ProviderError, id="401"),
    pytest.param(lambda r: json_reply(422, {"detail": [{"loc": ["body"], "msg": "x"}]}), ProviderError, id="422"),
    pytest.param(lambda r: json_reply(200, {"success": False, "message": "stopped"}), ProviderError, id="false"),
])
def test_sim_voice_error_mapping(handler, expected):
    voice = SimVoice("http://api.test", "s", http=mock_http(handler))
    raises_exactly(expected, lambda: place(voice))


# ---------------------------------------------------------------- posters

def test_http_poster_posts_tool_bodies_and_raw_webhooks():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/tools/submit_count":
            return json_reply(200, {"accepted": True, "message": "Recorded."})
        return httpx.Response(200)

    poster = HttpPoster("http://api.test/", "tool-secret", http=mock_http(handler))
    assert poster.post_tool({"attempt_id": "att_1", "in_stock": 5}) == {"accepted": True, "message": "Recorded."}
    raw = b'{"type": "post_call_transcription", "data": {}}'
    assert poster.post_hook(raw, {"ElevenLabs-Signature": "t=1,v0=ab", "Content-Type": "application/json"}) == 200
    tool, hook = seen
    assert str(tool.url) == "http://api.test/tools/submit_count" and tool.headers["x-tool-secret"] == "tool-secret"
    assert json.loads(tool.content) == {"attempt_id": "att_1", "in_stock": 5}
    assert str(hook.url) == "http://api.test/hooks/post_call" and hook.content == raw
    assert hook.headers["elevenlabs-signature"] == "t=1,v0=ab"


def test_http_poster_never_raises():
    def handler(request):
        if request.url.path == "/tools/submit_count":
            return httpx.Response(500, text="Internal Server Error")
        raise httpx.ConnectError("refused", request=request)

    poster = HttpPoster("http://api.test", "s", http=mock_http(handler))
    reply = poster.post_tool({"attempt_id": "att_1"})
    assert reply["status"] == 500 and reply["error"].startswith("HTTP 500")
    assert poster.post_hook(b"{}", {}) == 0
    down = HttpPoster("http://api.test", "s", http=mock_http(_raise(httpx.ConnectError)))
    assert "ConnectError" in down.post_tool({})["error"]
    not_json = HttpPoster("http://api.test", "s", http=mock_http(lambda r: httpx.Response(200, text="ok")))
    assert "not JSON" in not_json.post_tool({})["error"]


def test_client_poster_through_fastapi_testclient():
    app, got = FastAPI(), {}

    @app.post("/tools/submit_count")
    async def tool(request: Request):
        got["tool"] = (request.headers.get("x-tool-secret"), await request.json())
        return {"accepted": False, "message": "Rejected"}

    @app.post("/hooks/post_call")
    async def hook(request: Request):
        got["hook"] = (request.headers.get("elevenlabs-signature"), await request.body())
        return {"ok": True}

    poster = ClientPoster(TestClient(app), "tool-secret")
    assert poster.post_tool({"attempt_id": "att_1"}) == {"accepted": False, "message": "Rejected"}
    assert poster.post_hook(b'{"a": 1}', {"ElevenLabs-Signature": "t=1,v0=cd"}) == 200
    assert got == {"tool": ("tool-secret", {"attempt_id": "att_1"}), "hook": ("t=1,v0=cd", b'{"a": 1}')}


# ---------------------------------------------------------------- FakeVoice

def fake_call(voice, attempt_id="att_1", party_id="harbour-fire", to=TO):
    return voice.place_call(to_number=to, attempt_id=attempt_id, dynamic_variables={"party_id": party_id})


def test_fake_voice_records_and_fabricates_ids():
    voice = FakeVoice()
    placed = fake_call(voice)
    assert placed.call_sid.startswith("CA") and len(placed.call_sid) == 34
    assert placed.conversation_id.startswith("conv_fake_")
    (entry,) = voice.calls
    assert entry["placed"] and entry["fault"] is None and entry["attempt_id"] == "att_1"
    assert entry["to"] == TO and entry["party_id"] == "harbour-fire" and entry["call_sid"] == placed.call_sid
    assert voice.count() == 1 and voice.count("harbour-fire") == 1 and voice.count("corner-properties") == 0
    assert isinstance(voice.probe, CallsProbe)


def test_fake_voice_faults():
    voice = FakeVoice(faults={"att_drop": "drop_response", "att_before": "fail_before", "lakeside-systems": "reject"})
    raises_exactly(ProviderTimeout, lambda: fake_call(voice, "att_drop"))
    raises_exactly(ProviderTimeout, lambda: fake_call(voice, "att_before"))
    raises_exactly(ProviderError, lambda: fake_call(voice, "att_rej", party_id="lakeside-systems"))
    fake_call(voice, "att_ok")
    assert [(c["attempt_id"], c["placed"]) for c in voice.calls] == [
        ("att_drop", True), ("att_before", False), ("att_rej", False), ("att_ok", True)]
    assert [c["attempt_id"] for c in voice.placed_calls()] == ["att_drop", "att_ok"]
    assert voice.calls[0]["call_sid"], "a dropped response still names the call that exists"


def test_fake_voice_fault_lookup_order_and_callable():
    voice = FakeVoice(faults={"att_1": "reject", "harbour-fire": "fail_before", "*": "drop_response"})
    raises_exactly(ProviderError, lambda: fake_call(voice, "att_1"))
    raises_exactly(ProviderTimeout, lambda: fake_call(voice, "att_2"))
    raises_exactly(ProviderTimeout, lambda: fake_call(voice, "att_3", party_id="corner-properties"))
    assert [c["fault"] for c in voice.calls] == ["reject", "fail_before", "drop_response"]

    seen = []

    def every_second_call(attempt_id, party_id, n):
        seen.append((attempt_id, party_id, n, voice2.count(party_id)))
        return "drop_response" if n % 2 else None

    voice2 = FakeVoice(faults=every_second_call)
    fake_call(voice2, "a")
    raises_exactly(ProviderTimeout, lambda: fake_call(voice2, "b"))
    fake_call(voice2, "c")
    assert seen == [("a", "harbour-fire", 0, 0), ("b", "harbour-fire", 1, 1), ("c", "harbour-fire", 2, 2)]
    assert voice2.count() == 3


def test_fake_voice_rejects_bad_faults():
    with pytest.raises(ValueError, match="need a SimNetwork"):
        fake_call(FakeVoice(faults={"*": "duplicate_webhook"}))
    with pytest.raises(ValueError, match="unknown simulator fault"):
        fake_call(FakeVoice(faults={"*": "explode"}))
    with pytest.raises(ValueError, match="more than one"):
        fake_call(FakeVoice(faults={"*": {"reject": True, "fail_before": True}}))


def test_fake_probe_over_its_own_calls():
    voice = FakeVoice(faults={"att_lost": "fail_before"})
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    first = fake_call(voice, "att_a")
    second = fake_call(voice, "att_b")
    raises_exactly(ProviderTimeout, lambda: fake_call(voice, "att_lost"))
    fake_call(voice, "att_other", to="+14165550199")
    found = voice.probe.find_calls(to_number=TO, since=before)
    assert [c.call_sid for c in found] == [second.call_sid, first.call_sid]
    assert all(c.status == "completed" for c in found)
    assert voice.probe.find_calls(to_number=TO, since=datetime.now(timezone.utc) + timedelta(seconds=5)) == []
    assert voice.probe.call_status(first.call_sid).conversation_id == first.conversation_id
    assert voice.probe.call_status("CAunknown") is None


# ---------------------------------------------------------------- build_providers

def test_build_providers_elevenlabs(settings):
    sentinel_db = object()
    providers, probes = build_providers(replace(settings, voice_provider="elevenlabs"), sentinel_db)
    assert isinstance(providers["voice"], ElevenLabsVoice) and isinstance(probes["voice"], TwilioProbe)
    assert isinstance(providers["sim"], SimVoice) and isinstance(probes["sim"], SimProbe)
    assert probes["sim"].db is sentinel_db
    assert {p.name for p in providers.values()} == {"elevenlabs", "sim"}


def test_build_providers_sim_and_unknown(settings):
    providers, probes = build_providers(replace(settings, voice_provider="sim"), object())
    assert isinstance(providers["voice"], SimVoice) and providers["voice"] is providers["sim"]
    assert isinstance(probes["voice"], SimProbe) and probes["voice"] is probes["sim"]
    with pytest.raises(ValueError, match="VOICE_PROVIDER"):
        build_providers(replace(settings, voice_provider="eleven"), object())
