"""Provision the ElevenLabs side of All Clear from .env, idempotently.

    python scripts/provision_agent.py [--dry-run] [--base-url https://your-host]

Run it from level-a/ after any change to agent/* and whenever PUBLIC_BASE_URL
changes. Every run converges the workspace on the files in agent/, so a second
run updates in place and never creates duplicates:

1. the workspace secret holding TOOL_SECRET. Its name carries a fingerprint of
   the value, so rotating TOOL_SECRET creates a new secret and every tool is
   repointed, instead of silently keeping the old value;
2. the three webhook tools, submit_count, submit_answer and request_callback
   (each found by name, then PATCHed);
3. the HMAC post-call webhook, unless ELEVEN_WEBHOOK_ID is set or one already
   posts to this URL. A new webhook's signing secret is printed once, here, and
   written nowhere;
4. the agent that already exists (ELEVEN_AGENT_ID, else the one agent found
   by name), PATCHed with prompt.md, first_message.txt, the three tool ids and
   the webhook override. It is never created unless --create-agent is given,
   so a missing id cannot make a duplicate. An English agent must use TTS
   model eleven_flash_v2 or eleven_turbo_v2, and the prompt must never contain
   shipped_in: both are checked before anything is sent;
5. the phone number: ELEVEN_PHONE_ID, or the only Twilio number imported.

It ends by printing the .env lines to set. --dry-run prints every request
instead of sending it, answering lookups as a fresh workspace would.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from levela.config import AGENT_DIR, Settings, is_placeholder_secret  # noqa: E402

log = logging.getLogger("provision_agent")

WEBHOOK_NAME = "All Clear post-call"
SECRET_PREFIX = "levela_tool_secret"
# Tool name -> its file in agent/. The agent template names each tool's id as
# {{<NAME>_TOOL_ID}}, in this order.
TOOL_FILES = {
    "submit_count": "tool_submit_count.json",
    "submit_answer": "tool_submit_answer.json",
    "request_callback": "tool_request_callback.json",
}
# ElevenLabs accepts only these TTS models for an English agent.
ENGLISH_TTS_MODELS = ("eleven_flash_v2", "eleven_turbo_v2")
# Words the prompt must never contain: the agent learns of a mismatch only
# from the database's reply (spec "Handling a claim").
FORBIDDEN_PROMPT_WORDS = ("shipped_in",)
DRY_RUN_AGENT_ID = "<ELEVEN_AGENT_ID>"
# {{UPPER_CASE}} marks a value this script fills in. Lower-case {{names}} are
# ElevenLabs dynamic variables, resolved per call, and pass through untouched.
PLACEHOLDER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
REDACTED = "<redacted>"


class ProvisionError(Exception):
    """A request failed, or the workspace is in a state this script will not guess about."""


def _redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class Api:
    """The ElevenLabs management endpoints, over an injectable httpx.Client
    (tests pass one built on httpx.MockTransport)."""

    def __init__(self, http: httpx.Client, *, secrets: Iterable[str] = ()):
        self.http = http
        self.secrets = tuple(secrets)

    def request(self, method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, missing_ok: bool = False):
        """JSON reply of one request. missing_ok turns a 404 into None, for
        list endpoints a workspace may not offer."""
        try:
            resp = self.http.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise ProvisionError(f"{method} {path} failed: {exc}") from exc
        if missing_ok and resp.status_code == 404:
            return None
        if resp.is_error:
            # Validation errors can echo the request body, secret included.
            detail = _redact(resp.text[:600], self.secrets)
            raise ProvisionError(f"{method} {path} -> HTTP {resp.status_code}: {detail}")
        return resp.json() if resp.content else {}


class DryRunApi:
    """Prints each request (secrets redacted) and answers as a fresh workspace
    would, so the whole flow runs without touching the network."""

    CREATED = {
        "/v1/convai/secrets": {"secret_id": "<secret_id>"},
        "/v1/workspace/webhooks": {"webhook_id": "<webhook_id>",
                                   "webhook_secret": "<shown once on a real run>"},
        "/v1/convai/agents/create": {"agent_id": "<agent_id>"},
    }

    def __init__(self, out: Callable[[str], None] = print, *, secrets: Iterable[str] = ()):
        self.out = out
        self.secrets = tuple(secrets)

    def request(self, method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, missing_ok: bool = False):
        self.out(f"\n{method} {path}" + (f"?{urlencode(params)}" if params else ""))
        if body is not None:
            self.out(_redact(json.dumps(body, indent=2, ensure_ascii=False), self.secrets))
        if method == "GET":
            return {}
        if path == "/v1/convai/tools":
            # One id per tool, so the printed agent body shows which id goes where.
            return {"id": f"<{body['tool_config']['name']}_tool_id>"}
        return dict(self.CREATED.get(path, {}))


def _items(payload, key: str) -> list:
    """List endpoints answer {key: [...]} or a bare list."""
    if isinstance(payload, list):
        return payload
    return (payload or {}).get(key) or []


# ---- request bodies from agent/ ---------------------------------------------

def load_agent_file(name: str) -> str:
    return (AGENT_DIR / name).read_text(encoding="utf-8")


def render(template, values: Mapping[str, str]):
    """Fill {{UPPER_CASE}} placeholders in every string of a JSON template, in
    one pass, so text inserted from prompt.md is never re-scanned."""
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    if not isinstance(template, str):
        return template

    def fill(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ProvisionError(f"agent template needs a value for {m.group(0)}")
        return values[m.group(1)]
    return PLACEHOLDER.sub(fill, template)


def tool_body(name: str, base_url: str, secret_id: str) -> dict:
    """The POST /v1/convai/tools body of one tool in TOOL_FILES."""
    template = json.loads(load_agent_file(TOOL_FILES[name]))
    body = render(template, {"PUBLIC_BASE_URL": base_url, "TOOL_SECRET_ID": secret_id})
    if body["tool_config"]["name"] != name:
        raise ProvisionError(f"{TOOL_FILES[name]} defines tool {body['tool_config']['name']!r}, "
                             f"expected {name!r}")
    return body


def tool_id_placeholder(name: str) -> str:
    return f"{name.upper()}_TOOL_ID"


def agent_body(tool_ids: Mapping[str, str], webhook_id: str) -> dict:
    """The agent create/update body. `tool_ids` maps every tool in TOOL_FILES
    to its workspace id; render() refuses a template that names one not given."""
    template = json.loads(load_agent_file("agent_config.json"))
    prompt = load_agent_file("prompt.md").strip()
    for word in FORBIDDEN_PROMPT_WORDS:
        if word in prompt:
            raise ProvisionError(f"agent/prompt.md contains {word!r}; the agent must never see it")
    body = render(template, {
        "PROMPT": prompt,
        "FIRST_MESSAGE": load_agent_file("first_message.txt").strip(),
        "POST_CALL_WEBHOOK_ID": webhook_id,
        **{tool_id_placeholder(name): tool_id for name, tool_id in tool_ids.items()},
    })
    check_tts(body)
    return body


def check_tts(body: dict) -> None:
    """An English agent must name an English TTS model; ElevenLabs refuses
    the update otherwise."""
    conv = body.get("conversation_config") or {}
    language = (conv.get("agent") or {}).get("language") or "en"
    model = (conv.get("tts") or {}).get("model_id")
    if language == "en" and model not in ENGLISH_TTS_MODELS:
        raise ProvisionError(f"an English agent must use TTS model {' or '.join(ENGLISH_TTS_MODELS)}, "
                             f"not {model!r} (agent/agent_config.json)")


# ---- one function per workspace object ---------------------------------------

def secret_name(value: str) -> str:
    """The fingerprint identifies the value without revealing it (8 hex chars
    of SHA-256 of a random token)."""
    return f"{SECRET_PREFIX}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


def ensure_tool_secret(api, value: str) -> str:
    name = secret_name(value)
    for secret in _items(api.request("GET", "/v1/convai/secrets"), "secrets"):
        if secret.get("name") == name:
            log.info("reusing workspace secret %s (%s)", name, secret["secret_id"])
            return secret["secret_id"]
    created = api.request("POST", "/v1/convai/secrets",
                          body={"type": "new", "name": name, "value": value})
    log.info("created workspace secret %s (%s)", name, created["secret_id"])
    return created["secret_id"]


def ensure_tools(api, bodies: Iterable[dict]) -> dict[str, str]:
    """{tool name: id} for every body, listing the workspace's tools once."""
    existing = _items(api.request("GET", "/v1/convai/tools"), "tools")
    return {body["tool_config"]["name"]: ensure_tool(api, body, existing) for body in bodies}


def ensure_tool(api, body: dict, existing: list[dict]) -> str:
    """PATCH the one tool with this body's name, or create it."""
    name = body["tool_config"]["name"]
    matches = [t for t in existing if (t.get("tool_config") or {}).get("name") == name]
    if len(matches) > 1:
        ids = ", ".join(t["id"] for t in matches)
        raise ProvisionError(f"{len(matches)} tools are named {name} ({ids}); delete the extras "
                             "in the ElevenLabs dashboard and run again")
    if matches:
        tool_id = matches[0]["id"]
        api.request("PATCH", f"/v1/convai/tools/{tool_id}", body=body)
        log.info("updated tool %s (%s)", name, tool_id)
        return tool_id
    tool_id = api.request("POST", "/v1/convai/tools", body=body)["id"]
    log.info("created tool %s (%s)", name, tool_id)
    return tool_id


def ensure_webhook(api, url: str, webhook_id: str | None) -> tuple[str, str | None, list[str]]:
    """(webhook_id, signing secret if created now, problems). The secret only
    exists in the create reply, so a webhook is reused rather than recreated
    whenever one is known for this URL."""
    listing = api.request("GET", "/v1/workspace/webhooks", missing_ok=True)
    hooks = _items(listing, "webhooks")
    if webhook_id:
        known = next((h for h in hooks if h.get("webhook_id") == webhook_id), None)
        if listing is not None and known is None:
            return webhook_id, None, [
                f"ELEVEN_WEBHOOK_ID={webhook_id} is not a webhook in this workspace: remove it "
                "from .env and run again to create one"]
        if known and known.get("webhook_url") and known["webhook_url"] != url:
            return webhook_id, None, [
                f"webhook {webhook_id} posts to {known['webhook_url']}, not {url}: remove "
                "ELEVEN_WEBHOOK_ID from .env and run again to create one for this URL"]
        log.info("using post-call webhook %s from ELEVEN_WEBHOOK_ID", webhook_id)
        return webhook_id, None, []
    same_url = [h for h in hooks if h.get("webhook_url") == url]
    if same_url:
        log.info("reusing post-call webhook %s (already posts to %s); its signing secret was "
                 "shown when it was created", same_url[0]["webhook_id"], url)
        return same_url[0]["webhook_id"], None, []
    created = api.request("POST", "/v1/workspace/webhooks", body={
        "settings": {"auth_type": "hmac", "name": WEBHOOK_NAME, "webhook_url": url}})
    log.info("created post-call webhook %s -> %s", created["webhook_id"], url)
    return created["webhook_id"], created.get("webhook_secret"), []


def find_agent(api, name: str) -> str | None:
    found, cursor = [], None
    while True:
        params = {"search": name, "page_size": 100, **({"cursor": cursor} if cursor else {})}
        page = api.request("GET", "/v1/convai/agents", params=params)
        found += [a["agent_id"] for a in _items(page, "agents") if a.get("name") == name]
        cursor = page.get("next_cursor") if isinstance(page, dict) and page.get("has_more") else None
        if not cursor:
            break
    if len(found) > 1:
        raise ProvisionError(f"{len(found)} agents are named {name!r} ({', '.join(found)}); "
                             "set ELEVEN_AGENT_ID to the one to keep")
    return found[0] if found else None


def ensure_agent(api, body: dict, agent_id: str | None, *, allow_create: bool = False) -> str:
    """PATCH the existing agent. Without ELEVEN_AGENT_ID it is found by name;
    with neither, it is created only when allow_create says so (never by
    default: the agent and the imported number already exist)."""
    agent_id = agent_id or find_agent(api, body["name"])
    if agent_id:
        api.request("PATCH", f"/v1/convai/agents/{agent_id}", body=body)
        log.info("updated agent %r (%s)", body["name"], agent_id)
        return agent_id
    if not allow_create:
        raise ProvisionError(f"no agent to update: ELEVEN_AGENT_ID is not set and no agent is named "
                             f"{body['name']!r}. Set ELEVEN_AGENT_ID to the existing agent (this script never "
                             "creates a duplicate), or pass --create-agent for a brand-new workspace")
    agent_id = api.request("POST", "/v1/convai/agents/create", body=body)["agent_id"]
    log.info("created agent %r (%s)", body["name"], agent_id)
    return agent_id


def _masked(number: str | None) -> str:
    return f"...{number[-4:]}" if number else "?"


def pick_phone(api, wanted: str | None) -> tuple[str | None, str | None]:
    """(phone_number_id, problem). Outbound calls name the agent themselves, so
    the number is only looked up, never reassigned."""
    numbers = _items(api.request("GET", "/v1/convai/phone-numbers"), "phone_numbers")
    if wanted:
        if any(n.get("phone_number_id") == wanted for n in numbers):
            return wanted, None
        return None, f"ELEVEN_PHONE_ID={wanted} is not a phone number in this workspace"
    twilio = [n for n in numbers if n.get("provider") == "twilio"]
    if len(twilio) == 1:
        log.info("using Twilio number %s (%s)", _masked(twilio[0].get("phone_number")),
                 twilio[0]["phone_number_id"])
        return twilio[0]["phone_number_id"], None
    if not twilio:
        return None, ("no Twilio number is imported: import it in the ElevenLabs dashboard "
                      "(Phone Numbers, with its SID and token) and run again")
    listed = ", ".join(f"{n['phone_number_id']} ({_masked(n.get('phone_number'))})" for n in twilio)
    return None, f"several Twilio numbers ({listed}): set ELEVEN_PHONE_ID to one of them"


# ---- the run ------------------------------------------------------------------

def provision(api, settings: Settings, *, base_url: str, webhook_id: str | None = None,
              allow_create: bool = False, dry_run: bool = False) -> dict:
    """Converge the workspace on agent/*. Returns the ids, a webhook secret if
    one was created now, and the problems still to fix by hand."""
    secret_id = ensure_tool_secret(api, settings.tool_secret)
    tool_ids = ensure_tools(api, [tool_body(name, base_url, secret_id) for name in TOOL_FILES])
    webhook_id, webhook_secret, problems = ensure_webhook(api, f"{base_url}/hooks/post_call",
                                                          webhook_id)
    if webhook_secret is None and is_placeholder_secret(settings.webhook_secret):
        problems.append("ELEVEN_WEBHOOK_SECRET is not set, so the API will refuse every post-call "
                        "webhook: set it to the secret shown when the webhook was created, or "
                        "delete the webhook in the ElevenLabs dashboard and run again")
    agent_id = settings.eleven_agent_id or (DRY_RUN_AGENT_ID if dry_run and not allow_create else None)
    agent_id = ensure_agent(api, agent_body(tool_ids, webhook_id), agent_id, allow_create=allow_create)
    phone_id, phone_problem = pick_phone(api, settings.eleven_phone_id or None)
    if phone_problem:
        problems.append(phone_problem)
    return {"secret_id": secret_id, "tool_ids": tool_ids, "webhook_id": webhook_id,
            "webhook_secret": webhook_secret, "agent_id": agent_id, "phone_id": phone_id,
            "problems": problems}


def check_inputs(settings: Settings, base_url: str, *, dry_run: bool) -> tuple[list[str], list[str]]:
    """(errors that stop the run, warnings). A dry run needs only the URL."""
    errors, warnings = [], []
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append(f"PUBLIC_BASE_URL (or --base-url) must be the API's public https:// URL, "
                      f"got {base_url!r}")
    missing = []
    if not settings.eleven_api_key:
        missing.append("ELEVEN_API_KEY is not set")
    # Unset, or the public development value LEVELA_DEV=1 fills in.
    if is_placeholder_secret(settings.tool_secret):
        missing.append("TOOL_SECRET is not set (the development default must never reach a "
                       "public tool): python -c \"import secrets;print(secrets.token_urlsafe(24))\"")
    (warnings if dry_run else errors).extend(missing)
    return errors, warnings


def env_lines(result: dict) -> list[str]:
    lines = ["", "Set these in level-a/.env (ids, not secrets):",
             f"ELEVEN_AGENT_ID={result['agent_id']}",
             f"ELEVEN_PHONE_ID={result['phone_id'] or ''}",
             f"ELEVEN_WEBHOOK_ID={result['webhook_id']}"]
    if result["webhook_secret"]:
        lines += ["", "New post-call webhook signing secret. ElevenLabs shows it only now and this "
                  "script writes it nowhere.",
                  "Put it in level-a/.env and in the API host's environment, never in a tracked file:",
                  f"ELEVEN_WEBHOOK_SECRET={result['webhook_secret']}"]
    return lines


def parse_args(argv):
    p = argparse.ArgumentParser(prog="provision_agent.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="print every request body instead of sending it")
    p.add_argument("--base-url", default=None,
                   help="public https URL of the API (default: PUBLIC_BASE_URL)")
    p.add_argument("--create-agent", action="store_true",
                   help="create the agent when ELEVEN_AGENT_ID is unset and none has its name "
                        "(a brand-new workspace only; by default the existing agent is updated)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None, *, http: httpx.Client | None = None, env: Mapping[str, str] | None = None,
         out: Callable[[str], None] = print) -> int:
    """Exit status: 0 done, 1 a request failed or something still needs doing,
    2 refused to start. `http` and `env` are injectable for tests."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-5s %(message)s")
    settings = Settings.from_env(env=env)
    environ = os.environ if env is None else env
    base_url = (args.base_url or settings.public_base_url).rstrip("/")
    errors, warnings = check_inputs(settings, base_url, dry_run=args.dry_run)
    for w in warnings:
        log.warning("%s", w)
    if errors:
        for e in errors:
            log.error("%s", e)
        return 2

    secrets = (settings.tool_secret,)
    owned = None
    if args.dry_run:
        log.info("dry run: printing each request instead of sending it; lookups answer as a "
                 "fresh workspace, so every step below reports a create")
        api = DryRunApi(out, secrets=secrets)
    else:
        if http is None:
            owned = http = httpx.Client(base_url=settings.eleven_base_url, timeout=30.0,
                                        headers={"xi-api-key": settings.eleven_api_key})
        api = Api(http, secrets=secrets)
    try:
        result = provision(api, settings, base_url=base_url,
                           webhook_id=environ.get("ELEVEN_WEBHOOK_ID") or None,
                           allow_create=args.create_agent, dry_run=args.dry_run)
    except ProvisionError as exc:
        log.error("%s", exc)
        return 1
    finally:
        if owned is not None:
            owned.close()

    for line in env_lines(result):
        out(line)
    for problem in result["problems"]:
        log.warning("still to do: %s", problem)
    return 1 if result["problems"] and not args.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
