"""The voice agent as code (agent/*) and its two scripts, for the food recall
(Maple Farm milk powder 800 g, batch B-4471): three call scripts chosen by
`party_type` (store, plant, buyer), the store's counts in tins and its three
checks, and provisioning that updates the existing agent and never makes a
duplicate.

The agent files must never carry a delivered count, every dynamic variable they
use must be one the worker sends, each tool must bind exactly the ids the API
needs and send exactly the fields its endpoint reads, and provisioning must
converge: create once, then PATCH, never duplicate. No database is needed.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from urllib.parse import parse_qs

import httpx
import pytest

from levela.config import AGENT_DIR, ROOT, SCENARIO_DIR
from levela.providers.base import PlacedCall, ProbedCall, ProviderError, ProviderTimeout

# DESIGN.md section 5: what the worker sends on every call, plus the question
# variables of V2 (spec "Questions as data"): ask_counts "yes"/"no", the open
# questions as text, and their ids.
PROMPT_VARIABLES = {"party_name", "party_type", "batch_code", "batch_spoken", "product", "hazard", "evidence",
                    "unit", "receipt", "ask_checks", "purpose", "company", "sender_name", "told_qty",
                    "ask_delivery_note", "followup_note", "ask_counts", "questions"}
WORKER_VARIABLES = PROMPT_VARIABLES | {"attempt_id", "party_id", "question_ids"}
TOOL_BINDINGS = {"attempt_id", "party_id", "system__conversation_id", "system__call_sid"}
INTEGER_FIELDS = {"sold", "on_shelf", "held", "returned", "forwarded_qty", "received_qty"}
STRING_FIELDS = {"forwarded_to_name", "delivery_note", "received_from", "batch_seen", "sold_source"}
BOOLEAN_FIELDS = {"block_test"}
ARRAY_FIELDS = {"forwards"}
# Each tool: its file, the endpoint, the fields the model fills with their
# types, and the required fields (V2 section 10 for the two new ones).
TOOLS = {
    "submit_count": ("tool_submit_count.json", "/tools/submit_count",
                     {**dict.fromkeys(INTEGER_FIELDS, "integer"), **dict.fromkeys(STRING_FIELDS, "string"),
                      **dict.fromkeys(BOOLEAN_FIELDS, "boolean"), **dict.fromkeys(ARRAY_FIELDS, "array")},
                     ["attempt_id", "party_id"]),
    "submit_answer": ("tool_submit_answer.json", "/tools/submit_answer",
                      {"question_id": "string", "value": "string"},
                      ["attempt_id", "party_id", "question_id", "value"]),
    "request_callback": ("tool_request_callback.json", "/tools/request_callback",
                         {"hour_of_day": "integer", "day_offset": "integer"},
                         ["attempt_id", "party_id", "hour_of_day"]),
}
# The property keys the vendor documents for a body schema (vendor_reference §2).
PROPERTY_KEYS = {"type", "description", "dynamic_variable", "items"}
DYNAMIC = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}")
TOOL_IDS = {"submit_count": "tool_a", "submit_answer": "tool_b", "request_callback": "tool_c"}

BASE_URL = "https://levela.example.test"
TOOL_SECRET = "tool-secret-for-tests-0123456789"
ENV = {"ELEVEN_API_KEY": "test-key", "TOOL_SECRET": TOOL_SECRET, "PUBLIC_BASE_URL": BASE_URL}
TO = "+15550100123"


def _load_script(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"levela_scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = _load_script("provision_agent")
call_once = _load_script("call_once")


def _text(name):
    return (AGENT_DIR / name).read_text(encoding="utf-8")


def _json(name):
    return json.loads(_text(name))


def _tool_schema(name):
    return _json(TOOLS[name][0])["tool_config"]["api_schema"]["request_body_schema"]


def _tool_properties(name="submit_count"):
    return _tool_schema(name)["properties"]


def _placeholders():
    return _json("agent_config.json")["conversation_config"]["agent"]["dynamic_variables"][
        "dynamic_variable_placeholders"]


def _scenario_secrets():
    """Every delivered count and delivery note in the demo scenarios."""
    counts, notes = set(), set()
    for path in SCENARIO_DIR.glob("*.json"):
        for party in json.loads(path.read_text(encoding="utf-8")).get("parties") or []:
            if "shipped_in" not in party:
                continue
            counts.add(party["shipped_in"])
            if party.get("delivery_note"):
                notes.add(party["delivery_note"])
    return counts, notes


# ---- the agent files ----------------------------------------------------------

@pytest.mark.parametrize("name", TOOLS)
def test_tool_files_have_the_vendor_shape(name):
    filename, path, _, _ = TOOLS[name]
    tool = _json(filename)["tool_config"]
    assert tool["type"] == "webhook" and tool["name"] == name
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool["name"])
    assert tool["response_timeout_secs"] == 20 and len(tool["description"]) > 100
    schema = tool["api_schema"]
    assert schema["url"] == "{{PUBLIC_BASE_URL}}" + path
    assert schema["method"] == "POST" and schema["content_type"] == "application/json"
    assert schema["request_headers"] == {"X-Tool-Secret": {"secret_id": "{{TOOL_SECRET_ID}}"}}
    assert schema["request_body_schema"]["type"] == "object"
    assert provision.TOOL_FILES[name] == filename


def test_agent_config_has_the_vendor_shape():
    agent = _json("agent_config.json")
    assert agent["name"] == "All Clear recall line"
    conv = agent["conversation_config"]
    prompt = conv["agent"]["prompt"]
    assert conv["agent"]["language"] == "en"
    assert prompt["temperature"] == 0 and prompt["llm"]
    assert prompt["tool_ids"] == [f"{{{{{provision.tool_id_placeholder(n)}}}}}" for n in TOOLS]
    assert prompt["built_in_tools"]["end_call"]["params"] == {"system_tool_type": "end_call"}
    assert "call back" in prompt["built_in_tools"]["end_call"]["description"]
    assert conv["tts"]["model_id"] in ("eleven_flash_v2", "eleven_turbo_v2")      # English agent
    assert conv["conversation"]["max_duration_seconds"] == 180
    assert conv["turn"] == {"turn_timeout": 7, "silence_end_call_timeout": 20}
    assert {"recall", "batch", "tins", "on the shelf", "held", "till", "delivery note", "Corner Mart",
            "Harbour Market", "call back", "tomorrow"} <= set(conv["asr"]["keywords"])
    hooks = agent["platform_settings"]["workspace_overrides"]["webhooks"]
    assert hooks["events"] == ["transcript", "call_initiation_failure"]


def test_prompt_and_first_message_use_only_worker_variables():
    for name in ("prompt.md", "first_message.txt"):
        used = set(DYNAMIC.findall(_text(name)))
        assert used, f"{name} uses no dynamic variables"
        assert used <= PROMPT_VARIABLES, f"{name} uses variables the worker never sends: {used - PROMPT_VARIABLES}"
    assert {"company", "party_name"} <= set(DYNAMIC.findall(_text("first_message.txt")))


def test_every_worker_variable_has_a_string_placeholder():
    placeholders = _placeholders()
    assert set(placeholders) == WORKER_VARIABLES
    assert all(isinstance(v, str) and v for v in placeholders.values())


@pytest.mark.parametrize("name", TOOLS)
def test_tool_binds_exactly_the_ids_from_dynamic_variables(name):
    _, _, model_fields, required = TOOLS[name]
    props = _tool_properties(name)
    bound = {field: p["dynamic_variable"] for field, p in props.items() if "dynamic_variable" in p}
    assert bound == {field: field for field in TOOL_BINDINGS}
    assert _tool_schema(name)["required"] == required
    # The body is exactly what the endpoint reads (DESIGN.md section 6, V2 section 10).
    assert set(props) == TOOL_BINDINGS | set(model_fields)
    assert all(set(p) <= PROPERTY_KEYS for p in props.values())


@pytest.mark.parametrize("name", TOOLS)
def test_model_filled_fields_are_typed_and_described(name):
    _, _, model_fields, _ = TOOLS[name]
    props = _tool_properties(name)
    for field, kind in model_fields.items():
        assert props[field]["type"] == kind, field
        assert len(props[field]["description"]) > 40, field
        assert "dynamic_variable" not in props[field], field


def test_callback_fields_say_how_to_fill_them():
    props = _tool_properties("request_callback")
    hour = props["hour_of_day"]["description"]
    assert "0 to 23" in hour and "twenty-four hour" in hour
    day = props["day_offset"]["description"]
    assert "0 for later today" in day and "1 for tomorrow" in day and "Leave it out" in day


def test_answer_value_matches_the_answer_types():
    """The API parses "yes, 12", "no", digits and free text (api/questions.normalize)."""
    value = _tool_properties("submit_answer")["value"]["description"]
    assert '"yes, 12"' in value and '"no"' in value and "digits" in value
    assert "q-" in _tool_properties("submit_answer")["question_id"]["description"]


def test_count_fields_say_when_to_send_zero():
    props = _tool_properties()
    for name in ("sold", "on_shelf", "held", "returned", "forwarded_qty"):
        assert "0" in props[name]["description"], f"{name} must say when to send 0"
    assert "leave it out" in props["received_qty"]["description"].lower()


def test_the_worker_writes_the_question_variables_the_way_the_agent_reads_them(db):
    """{{questions}} is "q-NN: <question> (answer: <the kind wanted>)." per open
    question, as agent_config.json's example shows and prompt.md describes
    ("starts with its id, the letter q, a dash and two digits, then the
    question and the kind of answer wanted"); {{question_ids}} lists the same
    ids. The worker builds both with levela.api.questions.call_variables from
    the documents POST /admin/questions writes, so the example agrees with
    what a real call carries."""
    from levela.api import questions

    example = _placeholders()
    [(qid, text, how)] = re.findall(r"(q-\d{2}): (.+?) \(answer: (.+?)\)\.", example["questions"])
    assert example["questions"] == f"{qid}: {text} (answer: {how})."
    answer_type = next(kind for kind, said in questions.HOW_TO_ANSWER.items() if said == how)
    rid = "RCL-TEST-Q"
    number = int(qid.removeprefix("q-"))
    for n in range(1, number + 1):
        doc = questions.insert_question(db, recall_id=rid, text=text if n == number else f"Question {n}?",
                                        answer_type=answer_type, applies_to={}, sim_now=9.0, by="admin")
    assert doc["question_id"] == qid
    party = {"recall_id": rid, "party_id": "harbour-market", "type": "store", "state": "contacting", "unresolved": 7,
             "open_questions": [qid]}
    sent = questions.call_variables(db, party)
    assert sent == {k: example[k] for k in ("questions", "question_ids", "ask_counts")}
    assert "the letter q, a dash and two digits" in _text("prompt.md")


def test_prompt_covers_questions_and_callbacks():
    """Question-only calls take no counts; answers and call backs are read back
    before their tool is called; question ids are never spoken."""
    prompt = _text("prompt.md")
    for phrase in ("{{ask_counts}}", "{{questions}}", "submit_answer", "request_callback",
                   "never ask for counts", "never say it aloud", "hour_of_day", "day_offset",
                   "Tomorrow at nine in the morning. Is that right?"):
        assert phrase in prompt, phrase
    assert prompt.index("Take counts on this call") < prompt.index("Open questions")


def test_spec_8_three_scripts_chosen_by_party_type():
    """Store: automated recall line, a simulation, product and batch named,
    counts one at a time, the three checks in the spec's words, digit-by-digit
    read-back, the two values from the tool reply, two challenges at most.
    Plant: the evidence, then its questions recorded with submit_answer.
    Buyer: the recall's instruction. Never a health answer."""
    prompt = _text("prompt.md")
    first = _text("first_message.txt")
    assert "automated recall line" in first and "simulation" in first
    assert "- store, or distributor, or self: follow \"The store script\"" in prompt
    for kind in ("plant", "buyer"):
        assert f"- {kind}: follow" in prompt, kind
    store = prompt[prompt.index("# The store script"):prompt.index("# The plant script")]
    order = [store.index(w) for w in ("how many have sold", "still on the shelf", "how many are held",
                                      "returned to", "passed to another store")]
    assert order == sorted(order)
    for check in ("Read me the batch code printed under one tin.",
                  "Open the item sales report on the till. How many have sold since the delivery?",
                  "Scan one tin at the till. Did the till refuse the sale?"):
        assert check in store, check
    assert "{{product}}" in store and "{{batch_spoken}}" in store and "store's name" in store
    assert "digit by digit" in prompt and "two values that disagree" in prompt
    assert "Never challenge their answers more than twice in one call." in prompt
    plant = prompt[prompt.index("# The plant script"):prompt.index("# The buyer script")]
    assert "{{evidence}}" in plant and "three customer complaints, from three different stores" in plant
    assert "submit_answer" in plant and "one at a time" in plant and "never call submit_count" in plant
    buyer = prompt[prompt.index("# The buyer script"):prompt.index("# Reading numbers back")]
    for phrase in ("{{product}}", "{{batch_spoken}}", "do not use it", "for a full refund"):
        assert phrase in buyer, phrase
    assert "never answer it and never give health advice" in prompt
    assert "Never answer a health question and never give health or medical advice." in prompt


def test_old_vocabulary_is_gone_from_the_agent_files():
    words = re.compile(r"panel|installer|sounder|firmware|in_stock|installed|model_seen|version_seen|"
                       r"Kestrel|DEMO-7|on_hand|portal|Harbour Fire|Queen St Alarm|Corner Properties|"
                       r"Lakeside Systems", re.IGNORECASE)
    for path in AGENT_DIR.iterdir():
        if path.name in ("planner_prompt.md", "repair_prompt.md") or path.suffix not in (".md", ".json", ".txt"):
            continue
        assert not words.search(path.read_text(encoding="utf-8")), path.name


AGENT_FACING = ("prompt.md", "first_message.txt", "agent_config.json",
                *(filename for filename, *_ in TOOLS.values()))


@pytest.mark.parametrize("name", AGENT_FACING)
def test_agent_never_sees_a_delivered_count(name):
    text = _text(name)
    assert "shipped" not in text.lower()       # also covers shipped_in and "shipped in"
    counts, notes = _scenario_secrets()
    numbers = {int(n) for n in re.findall(r"\d+", text)}
    assert not numbers & counts, f"{name} contains a scenario's delivered count"
    assert not [n for n in notes if n in text], f"{name} contains a scenario's delivery note"


def test_rendered_agent_body_never_sees_a_delivered_count():
    body = json.dumps(provision.agent_body(TOOL_IDS, "wh_1"))
    assert "shipped" not in body.lower()
    for name in ("prompt.md", "first_message.txt"):
        prose = re.sub(r"^\s*\d+\.\s", "", _text(name), flags=re.M)      # step numbers are not counts
        assert not re.search(r"\d", prose), f"{name} must carry no numbers besides step numbers"


def test_agent_body_fills_every_provisioning_placeholder():
    body = provision.agent_body(TOOL_IDS, "wh_xyz")
    agent = body["conversation_config"]["agent"]
    assert agent["prompt"]["prompt"] == _text("prompt.md").strip()
    assert agent["first_message"] == _text("first_message.txt").strip()
    assert agent["prompt"]["tool_ids"] == ["tool_a", "tool_b", "tool_c"]
    assert body["platform_settings"]["workspace_overrides"]["webhooks"]["post_call_webhook_id"] == "wh_xyz"
    assert not provision.PLACEHOLDER.search(json.dumps(body))
    assert "{{party_name}}" in agent["prompt"]["prompt"]          # runtime variables pass through
    assert "{{questions}}" in agent["prompt"]["prompt"]


def test_agent_body_needs_every_tool_id():
    with pytest.raises(provision.ProvisionError, match="REQUEST_CALLBACK_TOOL_ID"):
        provision.agent_body({"submit_count": "tool_a", "submit_answer": "tool_b"}, "wh_1")


@pytest.mark.parametrize("name", TOOLS)
def test_tool_body_fills_url_and_secret(name):
    body = provision.tool_body(name, BASE_URL, "sec_1")
    schema = body["tool_config"]["api_schema"]
    assert schema["url"] == f"{BASE_URL}{TOOLS[name][1]}"
    assert schema["request_headers"]["X-Tool-Secret"] == {"secret_id": "sec_1"}
    assert not provision.PLACEHOLDER.search(json.dumps(body))


def test_tool_body_refuses_a_file_that_names_another_tool(monkeypatch):
    monkeypatch.setitem(provision.TOOL_FILES, "submit_answer", "tool_submit_count.json")
    with pytest.raises(provision.ProvisionError, match="expected 'submit_answer'"):
        provision.tool_body("submit_answer", BASE_URL, "sec_1")


def test_render_refuses_an_unknown_placeholder():
    with pytest.raises(provision.ProvisionError, match="NOPE"):
        provision.render({"a": ["x {{NOPE}}"]}, {})


# ---- provisioning against a fake workspace --------------------------------------

class FakeWorkspace:
    """An in-memory ElevenLabs workspace behind httpx.MockTransport."""

    def __init__(self, phone_numbers=None):
        self.secrets, self.tools, self.webhooks, self.agents = {}, {}, {}, {}
        self.phone_numbers = phone_numbers if phone_numbers is not None else [
            {"provider": "twilio", "phone_number": "+15550100199", "label": "demo",
             "phone_number_id": "phnum_1", "assigned_agent": None}]
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle),
                            base_url="https://api.elevenlabs.io", headers={"xi-api-key": "test-key"})

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def _new_id(self, prefix):
        self._n += 1
        return f"{prefix}_{self._n}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "test-key"
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path))
        resource = path.strip("/").split("/")[2].replace("-", "_")     # /v1/<area>/<resource>/...
        route = getattr(self, f"_{method.lower()}_{resource}", None)
        if route is None:
            return httpx.Response(404, json={"detail": "not found"})
        return route(path, body, request)

    # /v1/convai/secrets
    def _get_secrets(self, path, body, request):
        return httpx.Response(200, json={"secrets": [
            {"type": "stored", "secret_id": k, "name": v["name"]} for k, v in self.secrets.items()]})

    def _post_secrets(self, path, body, request):
        assert body["type"] == "new"
        sid = self._new_id("sec")
        self.secrets[sid] = body
        return httpx.Response(200, json={"type": "stored", "secret_id": sid, "name": body["name"]})

    # /v1/convai/tools
    def _get_tools(self, path, body, request):
        return httpx.Response(200, json={"tools": [
            {"id": k, "tool_config": v["tool_config"]} for k, v in self.tools.items()]})

    def _post_tools(self, path, body, request):
        tid = self._new_id("tool")
        self.tools[tid] = body
        return httpx.Response(200, json={"id": tid, **body})

    def _patch_tools(self, path, body, request):
        tid = path.rsplit("/", 1)[1]
        if tid not in self.tools:
            return httpx.Response(404, json={"detail": "no such tool"})
        self.tools[tid] = body
        return httpx.Response(200, json={"id": tid, **body})

    # /v1/workspace/webhooks
    def _get_webhooks(self, path, body, request):
        return httpx.Response(200, json={"webhooks": [
            {"webhook_id": k, "name": v["name"], "webhook_url": v["webhook_url"], "auth_type": "hmac"}
            for k, v in self.webhooks.items()]})

    def _post_webhooks(self, path, body, request):
        assert body["settings"]["auth_type"] == "hmac"
        wid = self._new_id("wh")
        self.webhooks[wid] = body["settings"]
        return httpx.Response(200, json={"webhook_id": wid, "webhook_secret": f"wsec_{wid}_shown_once"})

    # /v1/convai/agents
    def _get_agents(self, path, body, request):
        search = parse_qs(request.url.query.decode()).get("search", [""])[0].lower()
        rows = [{"agent_id": k, "name": v["name"]} for k, v in self.agents.items()
                if search in v["name"].lower()]
        return httpx.Response(200, json={"agents": rows, "has_more": False, "next_cursor": None})

    def _post_agents(self, path, body, request):
        assert path == "/v1/convai/agents/create"
        aid = self._new_id("agent")
        self.agents[aid] = body
        return httpx.Response(200, json={"agent_id": aid})

    def _patch_agents(self, path, body, request):
        aid = path.rsplit("/", 1)[1]
        if aid not in self.agents:
            return httpx.Response(404, json={"detail": "no such agent"})
        self.agents[aid] = body
        return httpx.Response(200, json={"agent_id": aid, **body})

    # /v1/convai/phone-numbers
    def _get_phone_numbers(self, path, body, request):
        return httpx.Response(200, json=self.phone_numbers)


def _run(ws, env, argv=()):
    lines: list[str] = []
    with ws.client() as http:
        code = provision.main(list(argv), http=http, env=env, out=lines.append)
    return code, "\n".join(lines)


def _first_run(ws):
    """A brand-new workspace: the one case where the agent is created."""
    code, out = _run(ws, ENV, argv=["--create-agent"])
    assert code == 0, out
    return out


def _tools_by_name(ws):
    return {body["tool_config"]["name"]: (tool_id, body) for tool_id, body in ws.tools.items()}


def test_first_run_creates_everything_on_a_fresh_workspace():
    ws = FakeWorkspace()
    out = _first_run(ws)
    assert ws.writes() == [("POST", "/v1/convai/secrets"), *[("POST", "/v1/convai/tools")] * len(TOOLS),
                           ("POST", "/v1/workspace/webhooks"), ("POST", "/v1/convai/agents/create")]
    assert [m for m, p in ws.calls if p == "/v1/convai/tools"].count("GET") == 1    # listed once
    (secret_id, secret), = ws.secrets.items()
    assert secret["value"] == TOOL_SECRET and TOOL_SECRET not in secret["name"]
    tools = _tools_by_name(ws)
    assert set(tools) == set(TOOLS)
    for name, (_, body) in tools.items():
        schema = body["tool_config"]["api_schema"]
        assert schema["url"] == f"{BASE_URL}{TOOLS[name][1]}"
        assert schema["request_headers"]["X-Tool-Secret"] == {"secret_id": secret_id}
    (webhook_id, hook), = ws.webhooks.items()
    assert hook["webhook_url"] == f"{BASE_URL}/hooks/post_call"
    (agent_id, agent), = ws.agents.items()
    assert agent["conversation_config"]["agent"]["prompt"]["tool_ids"] == [tools[n][0] for n in TOOLS]
    assert agent["conversation_config"]["agent"]["prompt"]["prompt"] == _text("prompt.md").strip()
    assert agent["platform_settings"]["workspace_overrides"]["webhooks"]["post_call_webhook_id"] == webhook_id
    for line in (f"ELEVEN_AGENT_ID={agent_id}", "ELEVEN_PHONE_ID=phnum_1",
                 f"ELEVEN_WEBHOOK_ID={webhook_id}"):
        assert line in out.splitlines()
    assert out.count(f"wsec_{webhook_id}_shown_once") == 1
    assert TOOL_SECRET not in out


def test_without_an_agent_id_nothing_is_created():
    """The agent and the imported number already exist: with no
    ELEVEN_AGENT_ID and no agent of that name, the script refuses instead of
    creating a second agent."""
    ws = FakeWorkspace()
    code, out = _run(ws, ENV)
    assert code == 1
    assert ("POST", "/v1/convai/agents/create") not in ws.writes() and ws.agents == {}


def test_an_existing_agent_in_env_is_patched_never_created():
    ws = FakeWorkspace()
    ws.agents["agent_live"] = {"name": "All Clear recall line"}
    env = {**ENV, "ELEVEN_AGENT_ID": "agent_live", "ELEVEN_PHONE_ID": "phnum_1",
           "ELEVEN_WEBHOOK_SECRET": "wsec_already_in_env"}
    code, out = _run(ws, env)
    assert code == 0, out
    assert ("PATCH", "/v1/convai/agents/agent_live") in ws.writes()
    assert ("POST", "/v1/convai/agents/create") not in ws.writes() and list(ws.agents) == ["agent_live"]
    tts = ws.agents["agent_live"]["conversation_config"]["tts"]["model_id"]
    assert tts in provision.ENGLISH_TTS_MODELS
    assert "ELEVEN_PHONE_ID=phnum_1" in out.splitlines()


def test_an_english_agent_with_another_tts_model_is_refused(monkeypatch):
    real = provision.load_agent_file

    def patched(name):
        text = real(name)
        return text.replace('"eleven_flash_v2"', '"eleven_flash_v2_5"') if name == "agent_config.json" else text
    monkeypatch.setattr(provision, "load_agent_file", patched)
    with pytest.raises(provision.ProvisionError, match="eleven_flash_v2 or eleven_turbo_v2"):
        provision.agent_body(TOOL_IDS, "wh_1")


@pytest.mark.parametrize("ids_from_env", [False, True], ids=["found-by-lookup", "ids-in-env"])
def test_second_run_patches_instead_of_duplicating(ids_from_env):
    ws = FakeWorkspace()
    _first_run(ws)
    agent_id, webhook_id = next(iter(ws.agents)), next(iter(ws.webhooks))
    first_writes = len(ws.writes())
    env = {**ENV, "ELEVEN_WEBHOOK_SECRET": "wsec_already_in_env"}
    if ids_from_env:
        env.update(ELEVEN_AGENT_ID=agent_id, ELEVEN_WEBHOOK_ID=webhook_id, ELEVEN_PHONE_ID="phnum_1")
    code, out = _run(ws, env)
    assert code == 0, out
    tools = _tools_by_name(ws)
    assert ws.writes()[first_writes:] == [*[("PATCH", f"/v1/convai/tools/{tools[n][0]}") for n in TOOLS],
                                          ("PATCH", f"/v1/convai/agents/{agent_id}")]
    assert (len(ws.secrets), len(ws.tools), len(ws.webhooks), len(ws.agents)) == (1, len(TOOLS), 1, 1)
    assert f"ELEVEN_AGENT_ID={agent_id}" in out.splitlines()
    assert "ELEVEN_WEBHOOK_SECRET=" not in out


def test_rotated_tool_secret_gets_a_new_secret_and_repoints_the_tool():
    ws = FakeWorkspace()
    _first_run(ws)
    code, out = _run(ws, {**ENV, "TOOL_SECRET": "rotated-secret-abcdefghijkl",
                          "ELEVEN_WEBHOOK_SECRET": "wsec_already_in_env"})
    assert code == 0, out
    assert len(ws.secrets) == 2 and len(ws.tools) == len(TOOLS)
    new_secret = [k for k, v in ws.secrets.items() if v["value"] == "rotated-secret-abcdefghijkl"][0]
    for body in ws.tools.values():
        header = body["tool_config"]["api_schema"]["request_headers"]
        assert header["X-Tool-Secret"] == {"secret_id": new_secret}


def test_stale_webhook_url_is_reported():
    ws = FakeWorkspace()
    _first_run(ws)
    webhook_id = next(iter(ws.webhooks))
    code, _ = _run(ws, {**ENV, "ELEVEN_WEBHOOK_ID": webhook_id, "ELEVEN_WEBHOOK_SECRET": "x"},
                   argv=["--base-url", "https://new-tunnel.example.test"])
    assert code == 1
    assert len(ws.webhooks) == 1


def test_missing_webhook_secret_is_reported_when_reusing():
    ws = FakeWorkspace()
    _first_run(ws)
    code, out = _run(ws, ENV)                  # webhook reused, but its secret never reached .env
    assert code == 1 and "ELEVEN_WEBHOOK_SECRET=" not in out


@pytest.mark.parametrize("numbers, wanted, expect_id", [
    ([], None, None),
    ([{"provider": "twilio", "phone_number": "+15550100101", "phone_number_id": "p1"},
      {"provider": "twilio", "phone_number": "+15550100102", "phone_number_id": "p2"}], None, None),
    ([{"provider": "twilio", "phone_number": "+15550100101", "phone_number_id": "p1"},
      {"provider": "twilio", "phone_number": "+15550100102", "phone_number_id": "p2"}], "p2", "p2"),
    ([{"provider": "sip_trunk", "phone_number": "+15550100103", "phone_number_id": "p3"},
      {"provider": "twilio", "phone_number": "+15550100104", "phone_number_id": "p4"}], None, "p4"),
    ([{"provider": "twilio", "phone_number": "+15550100101", "phone_number_id": "p1"}], "gone", None),
])
def test_pick_phone(numbers, wanted, expect_id):
    ws = FakeWorkspace(phone_numbers=numbers)
    with ws.client() as http:
        phone_id, problem = provision.pick_phone(provision.Api(http), wanted)
    assert phone_id == expect_id
    assert (problem is None) == (expect_id is not None)


def test_dry_run_makes_no_requests_and_prints_every_body():
    ws = FakeWorkspace()
    env = {"PUBLIC_BASE_URL": "http://not-https.example.test"}       # overridden by --base-url
    code, out = _run(ws, env, argv=["--dry-run", "--base-url", BASE_URL])
    assert code == 0, out
    assert ws.calls == []
    for line in ("POST /v1/convai/secrets", "POST /v1/convai/tools", "POST /v1/workspace/webhooks",
                 f"PATCH /v1/convai/agents/{provision.DRY_RUN_AGENT_ID}", "GET /v1/convai/phone-numbers"):
        assert line in out
    assert out.count("POST /v1/convai/tools") == len(TOOLS)
    for _, path, _, _ in TOOLS.values():
        assert f"{BASE_URL}{path}" in out
    assert f"{BASE_URL}/hooks/post_call" in out
    # The agent body names each tool's (dry-run) id.
    assert all(f"<{name}_tool_id>" in out for name in TOOLS)


def test_dry_run_redacts_the_tool_secret():
    ws = FakeWorkspace()
    code, out = _run(ws, ENV, argv=["--dry-run"])
    assert code == 0 and ws.calls == []
    assert TOOL_SECRET not in out and provision.REDACTED in out


@pytest.mark.parametrize("env", [
    {**ENV, "PUBLIC_BASE_URL": "http://levela.example.test"},
    {**ENV, "PUBLIC_BASE_URL": ""},
    {k: v for k, v in ENV.items() if k != "TOOL_SECRET"},
    {k: v for k, v in ENV.items() if k != "ELEVEN_API_KEY"},
], ids=["http", "no-url", "default-tool-secret", "no-api-key"])
def test_refuses_to_run_without_https_url_and_real_secrets(env):
    ws = FakeWorkspace()
    code, _ = _run(ws, env)
    assert code == 2 and ws.calls == []


def test_request_errors_do_not_echo_the_secret():
    def handler(request):
        return httpx.Response(422, json={"detail": [{"msg": "bad", "input": TOOL_SECRET}]})
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.elevenlabs.io") as http:
        api = provision.Api(http, secrets=[TOOL_SECRET])
        with pytest.raises(provision.ProvisionError) as info:
            api.request("POST", "/v1/convai/secrets", body={"value": TOOL_SECRET})
    assert TOOL_SECRET not in str(info.value) and "422" in str(info.value)


# ---- the smoke call ----------------------------------------------------------------

@pytest.mark.parametrize("script, flag", [("call_once.py", "--attempt"),
                                          ("provision_agent.py", "--dry-run")])
def test_scripts_help_runs_from_a_plain_interpreter(script, flag):
    """Run as `python scripts/x.py`, not under pytest's sys.path, so the
    scripts' own path setup is what makes `levela` importable."""
    res = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    assert flag in res.stdout


def test_call_once_help_documents_the_unrecognised_tool_call(capsys):
    with pytest.raises(SystemExit) as info:
        call_once.main(["--help"])
    assert info.value.code == 0
    assert "call not recognised" in capsys.readouterr().out


class FakeVoice:
    name = "fake"

    def __init__(self, exc=None):
        self.exc, self.calls = exc, []

    def place_call(self, *, to_number, dynamic_variables, attempt_id):
        self.calls.append({"to_number": to_number, "dynamic_variables": dynamic_variables,
                           "attempt_id": attempt_id})
        if self.exc:
            raise self.exc
        return PlacedCall(conversation_id="conv_test", call_sid="CAtest", raw={})


class FakeProbe:
    name = "fake"

    def __init__(self, statuses, sid="CAtest"):
        self.statuses, self.sid, self.asked = list(statuses), sid, []

    def _next(self):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            return None
        return ProbedCall(call_sid=self.sid, status=status, start_time=None, to=TO,
                          raw={"duration": "41"} if status == "completed" else {})

    def call_status(self, call_sid):
        self.asked.append(("status", call_sid))
        return self._next()

    def find_calls(self, *, to_number, since):
        self.asked.append(("find", to_number))
        call = self._next()
        return [call] if call else []


def _call(argv, **kw):
    lines: list[str] = []
    kw.setdefault("env", {})
    code = call_once.main(argv, out=lines.append, sleep=lambda s: None, **kw)
    return code, "\n".join(lines)


def test_call_once_sends_exactly_the_worker_variables():
    voice = FakeVoice()
    code, out = _call(["--to", TO], voice=voice)
    assert code == 0, out
    (call,) = voice.calls
    sent = call["dynamic_variables"]
    assert call["to_number"] == TO
    assert set(sent) == WORKER_VARIABLES
    assert all(isinstance(v, str) for v in sent.values())
    assert re.fullmatch(r"att_smoke_[0-9a-f]+", call["attempt_id"]) and sent["attempt_id"] == call["attempt_id"]
    assert sent["party_type"] == "store" and sent["ask_checks"] == "yes"
    assert sent["purpose"] == "initial" and sent["ask_counts"] == "yes" and sent["receipt"] == "no"
    assert sent["sender_name"] == sent["told_qty"] == sent["followup_note"] == ""
    assert sent["questions"] == sent["question_ids"] == ""
    assert "shipped" not in json.dumps(sent).lower()
    assert "conv_test" in out and "CAtest" in out and " ms" in out
    assert "call not recognised" in out and TO not in out


def test_call_once_attempt_and_overrides():
    voice = FakeVoice()
    code, out = _call(["--attempt", "att_0123456789abcd", "--var", "party_type=plant",
                       "--var", "sender_name=Queen St Grocer"],
                      voice=voice, env={"PHONE_HARBOUR": TO})
    assert code == 0, out
    (call,) = voice.calls
    assert call["to_number"] == TO and call["attempt_id"] == "att_0123456789abcd"
    assert call["dynamic_variables"]["party_type"] == "plant"
    assert call["dynamic_variables"]["sender_name"] == "Queen St Grocer"
    assert "call not recognised" not in out


@pytest.mark.parametrize("argv", [["--var", "shipped_in=40"], ["--var", "attempt_id=att_x"],
                                  ["--var", "no-equals-sign"]])
def test_call_once_rejects_unknown_or_reserved_variables(argv):
    with pytest.raises(SystemExit) as info:
        call_once.main(["--to", TO, *argv], voice=FakeVoice())
    assert info.value.code == 2


@pytest.mark.parametrize("to", ["", "4165550123", "+0123", "+1 416 555 0123"])
def test_call_once_refuses_a_bad_number(to):
    voice = FakeVoice()
    code, _ = _call(["--to", to] if to else [], voice=voice)
    assert code == 2 and voice.calls == []


def test_call_once_needs_provisioned_ids_without_an_injected_voice():
    code, _ = _call(["--to", TO], env={"ELEVEN_API_KEY": "k"})
    assert code == 2


@pytest.mark.parametrize("exc, phrase", [(ProviderTimeout("read timed out"), "may still ring"),
                                         (ProviderError("422 bad agent"), "placed no call")])
def test_call_once_reports_provider_failures(exc, phrase):
    code, out = _call(["--to", TO], voice=FakeVoice(exc))
    assert code == 1 and phrase in out


def test_call_once_watch_prints_the_status_timeline():
    probe = FakeProbe([None, "queued", "ringing", "ringing", "in-progress", "completed"])
    code, out = _call(["--to", TO, "--watch"], voice=FakeVoice(), probe=probe)
    assert code == 0, out
    timeline = re.findall(r"^\s+\d+\.\d s\s+(\S+)\s+CAtest$", out, re.M)
    assert timeline == ["queued", "ringing", "in-progress", "completed"]
    assert "duration 41 s" in out
    assert all(kind == "status" for kind, _ in probe.asked)


def test_watch_finds_the_call_without_a_call_sid_and_gives_up_on_time():
    """No callSid from ElevenLabs: find the call by number (as recovery does),
    then follow it by its SID."""
    probe = FakeProbe([None, "ringing"])
    ticks = iter(range(0, 1000, 2))
    lines: list[str] = []
    final = call_once.watch(probe, call_sid=None, to_number=TO, since=None, timeout_s=10,
                            sleep=lambda s: None, clock=lambda: next(ticks), out=lines.append)
    assert final is None
    kinds = [kind for kind, _ in probe.asked]
    assert kinds[:2] == ["find", "find"] and set(kinds[2:]) == {"status"}
    assert "gave up" in lines[-1] and "ringing" in lines[-1]
