"""The Anthropic Messages API over httpx (V2 §6): one client for the decider,
the planner and the adapter repairer, with no SDK dependency.

Only what the harness uses is here: one non-streaming `POST /v1/messages`
with client tools, read back by content-block type. The request follows the
current API rules for the models in use (claude-haiku-5-5 for decisions,
claude-sonnet-5-5 for repairs):

  * `anthropic-version: 2026-10-21`; no temperature / top_p / top_k (they
    are refused on this version);
  * adaptive thinking is left on and controlled with `output_config.effort`
    (a decider decision is a small task: "low");
  * `tool_choice` is "auto" and the prompt says to call exactly one tool,
    because forced tool choice is refused by some current models;
  * a response may start with thinking blocks, so blocks are read by type,
    never by position; `stop_reason` "refusal" and "max_tokens" are reported
    to the caller, which treats either as "no decision".

Tests never reach the network: they pass an `httpx.Client` built on
`httpx.MockTransport`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("levela.harness.models")

API_URL = "https://api.anthropic.com"
API_VERSION = "2026-10-21"
MESSAGES_PATH = "/v1/messages"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_TOKENS = 4096   # room for adaptive thinking plus one tool call
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
RETRY_PAUSE_S = 1.0


class ModelError(Exception):
    """The model could not be asked, or answered with an API error.
    `retryable` tells the caller whether the same request may work later."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status, self.retryable = status, retryable


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class ModelReply:
    """One Messages API response, reduced to what the harness records."""
    model: str
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: int
    raw: dict = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


def tool_definition(name: str, description: str, input_schema: dict) -> dict:
    """A client tool as the Messages API takes it."""
    return {"name": name, "description": description, "input_schema": input_schema}


class AnthropicClient:
    """`messages(...)` is the only call. `http` is injectable for tests; the
    client owns (and closes) the one it builds itself."""

    def __init__(self, api_key: str, *, base_url: str = API_URL, http: httpx.Client | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_retries: int = 1):
        if not api_key:
            raise ModelError("ANTHROPIC_API_KEY is not set: the Claude decider cannot run")
        self._key, self._base = api_key, base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout_s)
        self._max_retries = max(0, int(max_retries))

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def messages(self, *, model: str, system: str, messages: list[dict], tools: list[dict] | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, effort: str | None = "low") -> ModelReply:
        """Send one request and return the parsed reply. Retries once on a
        retryable status or a dropped connection; raises ModelError otherwise."""
        body: dict = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = {"type": "auto"}
        if effort:
            body["output_config"] = {"effort": effort}
        started = time.monotonic()
        data = self._post(body)
        return parse_reply(data, latency_ms=int((time.monotonic() - started) * 1000))

    def _post(self, body: dict) -> dict:
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        attempt = 0
        while True:
            try:
                resp = self._http.post(self._base + MESSAGES_PATH, json=body, headers=headers)
            except httpx.HTTPError as exc:
                err = ModelError(f"Anthropic API unreachable: {type(exc).__name__}: {exc}", retryable=True)
            else:
                if resp.status_code == 200:
                    return _json(resp)
                err = ModelError(f"Anthropic API answered {resp.status_code}: {_error_text(resp)}",
                                 status=resp.status_code, retryable=resp.status_code in RETRYABLE_STATUS)
            if not err.retryable or attempt >= self._max_retries:
                raise err
            attempt += 1
            log.warning("%s; retrying", err)
            time.sleep(RETRY_PAUSE_S)


def parse_reply(data: dict, *, latency_ms: int = 0) -> ModelReply:
    """Text and tool_use blocks by type (thinking blocks are skipped)."""
    texts, uses = [], []
    for block in data.get("content") or []:
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(block["text"])
        elif kind == "tool_use":
            uses.append(ToolUse(id=block.get("id", ""), name=block.get("name", ""),
                                input=block.get("input") if isinstance(block.get("input"), dict) else {}))
    usage = data.get("usage") or {}
    return ModelReply(model=data.get("model", ""), text="\n".join(texts).strip(), tool_uses=uses,
                      stop_reason=data.get("stop_reason"), input_tokens=int(usage.get("input_tokens") or 0),
                      output_tokens=int(usage.get("output_tokens") or 0), latency_ms=latency_ms, raw=data)


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise ModelError("Anthropic API answered 200 with a body that is not JSON") from exc
    if not isinstance(data, dict):
        raise ModelError("Anthropic API answered 200 with a body that is not a JSON object")
    return data


def _error_text(resp: httpx.Response) -> str:
    try:
        err = (resp.json() or {}).get("error") or {}
        return f"{err.get('type', 'error')}: {err.get('message', '')}".strip()
    except ValueError:
        return resp.text[:300]
