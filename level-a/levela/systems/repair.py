"""Repair: from a contract failure to a verified, active adapter (V2 §7).

    detect   code, in the order system client (a 410, a 422, a missing field)
    contain  operations.contain(): order system paused, open ops needs_reverify
    repair   memory first: an `adapter` episode whose failure text is like
             this one, from the same vendor and the same old version; else a
             Repairer proposes a mapping from the failing response, the
             order system's error text, its openapi.json and the old adapter
    verify   canary.run_canary(): the candidate must reproduce a count the
             ledger already holds; a wrong mapping is rejected, never activated
    activate adapters.activate(): old retired, new active, order system unpaused,
             in one transaction
    remember an `adapter` episode with the failure text embedded, so the next
             order system of the same vendor that fails the same way repairs with
             0 model calls (written before the retry: it depends only on the
             activated adapter, and must not be lost to a retry that dies)
    retry    operations.retry_needs_reverify(): only the flagged ops

Crash safety. Activation commits before the retry, so a kill (or an error)
in between leaves the order system unpaused under a canary-passed adapter with ops
still flagged. request_repair() on such an order system resumes: it writes the
episode if it is missing and retries what is still flagged. The planner asks
for that (harness.decide.RuleDecider.repairs), and a restarted worker
frees the repair locks its dead boot held (release_locks) instead of
waiting LOCK_TTL_S for them to lapse.

Repairers return a Proposal; all three are validated by the same code:

    ClaudeRepairer   Anthropic Messages API over httpx, JSON-only output against
                     PROPOSAL_SCHEMA, system prompt agent/repair_prompt.md,
                     model REPAIR_MODEL (default claude-sonnet-5-5), ANTHROPIC_API_KEY
    RulesRepairer    deterministic: successor endpoints by path, fields by name,
                     type and description over openapi.json (used with no key)
    ScriptedRepairer a fixed proposal (tests)

request_repair(ctx, order system) is what the planner calls.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from pymongo.errors import DuplicateKeyError, OperationFailure

from .. import feed, runs
from ..clock import EPOCH, server_now
from ..config import AGENT_DIR
from ..db.schema import VECTOR_INDEX
from ..memory.embed import EMBED_ERRORS, cosine, get_embedder
from . import adapters, canary, operations
from .contract import CANONICAL, GROUPS, INTEGER_FIELDS, ContractError
from .order_client import OrderSystemError, OrderSystemTimeout

log = logging.getLogger("levela.systems.repair")

# What each canonical field means. The model reads these sentences; the rules
# repairer scores new fields against their words.
DEFINITIONS = {
    "batch": "Batch code of the recalled product.",
    "account_scope": "Which accounts a hold covers: all, or one account.",
    "hold_id": "Identifier of a hold.",
    "status": "State of a hold: whether it is in force or still pending.",
    "account": "Identifier of the account at the distributor.",
    "name": "Name of the party.",
    "phone": "Phone number of the party.",
    "qty_shipped": "Units of the batch shipped to the account.",
    "qty_in_stock": "Units of the batch the account still has in stock and available for sale.",
}
DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RETRY_STATUSES = (429, 500, 502, 503, 504, 529)
LOCK_TTL_S = 300

_ENDPOINT = re.compile(r"^(GET|POST) /[A-Za-z0-9_\-./{}]*$")
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class RepairError(Exception):
    """No acceptable proposal. model_calls counts the calls spent finding that out."""

    def __init__(self, message: str, *, model_calls: int = 0):
        super().__init__(message)
        self.model_calls = model_calls


@dataclass
class Proposal:
    paths: dict
    mapping: dict
    status_in_force: list
    status_pending: list
    defaults: dict
    rationale: str = ""
    source: str = "rules"
    model_calls: int = 0
    usage: dict | None = None

    def spec(self) -> dict:
        """The adapter fields, as adapters.build_candidate() takes them."""
        return {"paths": self.paths, "mapping": self.mapping, "status_in_force": self.status_in_force,
                "status_pending": self.status_pending, "defaults": self.defaults}


# ------------------------------------------------------------- validation

_STR = {"type": "string"}
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "paths": {"type": "object", "additionalProperties": False,
                  "required": ["place_hold", "confirm_hold", "list_holds", "list_accounts"],
                  "properties": {"place_hold": _STR, "list_holds": _STR, "list_accounts": _STR,
                                 "confirm_hold": {"anyOf": [_STR, {"type": "null"}]}}},
        "mapping": {"type": "object", "additionalProperties": False, "required": list(CANONICAL),
                    "properties": {field: _STR for field in CANONICAL}},
        "status_in_force": {"type": "array", "items": _STR},
        "status_pending": {"type": "array", "items": _STR},
        "defaults": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "required": ["field", "value"],
                                                "properties": {"field": _STR, "value": _STR}}},
        "rationale": _STR,
    },
    "required": ["paths", "mapping", "status_in_force", "status_pending", "defaults", "rationale"],
    "additionalProperties": False,
}


def _endpoint(value, method: str, what: str) -> str:
    if not isinstance(value, str) or not _ENDPOINT.match(value) or not value.startswith(method + " "):
        raise RepairError(f"{what} must be '{method} /path', not {value!r}")
    return value


def validate_proposal(data) -> dict:
    """The same checks for every repairer: endpoints well formed, every
    canonical field mapped to a distinct dot path within its response,
    status sets non-empty and disjoint, defaults on plain request fields."""
    if not isinstance(data, dict):
        raise RepairError("the proposal is not a JSON object")
    missing = [k for k in ("paths", "mapping", "status_in_force", "status_pending", "defaults") if k not in data]
    if missing:
        raise RepairError(f"the proposal lacks {', '.join(missing)}")
    raw_paths, mapping = data["paths"] or {}, data["mapping"] or {}
    paths = {"place_hold": _endpoint(raw_paths.get("place_hold"), "POST", "paths.place_hold"),
             "list_holds": _endpoint(raw_paths.get("list_holds"), "GET", "paths.list_holds"),
             "list_accounts": _endpoint(raw_paths.get("list_accounts"), "GET", "paths.list_accounts"),
             "confirm_hold": None}
    if raw_paths.get("confirm_hold") is not None:
        paths["confirm_hold"] = _endpoint(raw_paths["confirm_hold"], "POST", "paths.confirm_hold")
    for field in CANONICAL:
        if not isinstance(mapping.get(field), str) or not _FIELD_PATH.match(mapping[field]):
            raise RepairError(f"mapping.{field} must be a dot path, not {mapping.get(field)!r}")
    for group, fields in GROUPS.items():
        used = [mapping[f] for f in fields]
        if len(set(used)) != len(used):
            raise RepairError(f"two canonical fields map to the same {group} field: {used}")
    in_force = [s for s in data["status_in_force"] or [] if isinstance(s, str) and s]
    pending = [s for s in data["status_pending"] or [] if isinstance(s, str) and s]
    if not in_force:
        raise RepairError("status_in_force is empty: nothing would ever count as a hold in force")
    if set(in_force) & set(pending):
        raise RepairError("a status cannot be both in force and pending")
    raw_defaults = data["defaults"] or []
    pairs = raw_defaults.items() if isinstance(raw_defaults, dict) else (
        (d.get("field"), d.get("value")) for d in raw_defaults if isinstance(d, dict))
    defaults = {}
    for field, value in pairs:
        if not isinstance(field, str) or not _FIELD_PATH.match(field) or "." in field:
            raise RepairError(f"default {field!r} must be a top-level request field")
        if field in (mapping["batch"], mapping["account_scope"]) or not isinstance(value, str):
            raise RepairError(f"default {field} = {value!r} is not a fixed string for an unmapped field")
        defaults[field] = value
    return {"paths": paths, "mapping": {f: mapping[f] for f in CANONICAL}, "status_in_force": in_force,
            "status_pending": pending, "defaults": defaults, "rationale": str(data.get("rationale") or "")}


# ------------------------------------------------------------ the schema

class _Spec:
    """openapi.json with $ref resolution and dot-path field listings."""

    def __init__(self, doc: dict):
        self.doc = doc or {}

    def _ref(self, ref: str) -> dict:
        node = self.doc
        for part in ref.lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node

    def resolve(self, schema) -> dict:
        for _ in range(32):
            if not isinstance(schema, dict):
                return {}
            rest = {k: v for k, v in schema.items() if k not in ("$ref", "allOf")}
            if "$ref" in schema:
                schema = {**self._ref(schema["$ref"]), **rest}
            elif len(schema.get("allOf") or []) == 1:
                schema = {**self.resolve(schema["allOf"][0]), **rest}
            else:
                return schema
        return {}

    def endpoints(self) -> list[tuple[str, str, dict]]:
        """(METHOD, path, operation) for every operation that is not deprecated."""
        out = []
        for path, item in (self.doc.get("paths") or {}).items():
            for method, op in item.items():
                if isinstance(op, dict) and method in ("get", "post") and not op.get("deprecated"):
                    out.append((method.upper(), path, op))
        return out

    def operation(self, endpoint: str) -> dict:
        method, _, path = endpoint.partition(" ")
        op = ((self.doc.get("paths") or {}).get(path) or {}).get(method.lower())
        if not isinstance(op, dict):
            raise RepairError(f"openapi.json has no {endpoint}")
        return op

    def response(self, endpoint: str) -> dict:
        """The 2xx JSON body schema (the item schema for a list)."""
        for status, resp in sorted((self.operation(endpoint).get("responses") or {}).items()):
            if str(status).startswith("2"):
                schema = self.resolve(((resp.get("content") or {}).get("application/json") or {}).get("schema"))
                return self.resolve(schema.get("items")) if schema.get("type") == "array" else schema
        raise RepairError(f"{endpoint} documents no 2xx JSON response")

    def request(self, endpoint: str) -> dict:
        body = (self.operation(endpoint).get("requestBody") or {}).get("content") or {}
        return self.resolve((body.get("application/json") or {}).get("schema"))

    def query(self, endpoint: str) -> list[str]:
        return [p["name"] for p in self.operation(endpoint).get("parameters") or []
                if p.get("in") == "query" and p.get("required")]

    def leaves(self, schema: dict, prefix: str = "") -> dict:
        """{dot path: {type, description, values}} for every scalar field."""
        out = {}
        for name, prop in (self.resolve(schema).get("properties") or {}).items():
            prop = self.resolve(prop)
            path = f"{prefix}{name}"
            if prop.get("type") == "object" or prop.get("properties"):
                out.update(self.leaves(prop, f"{path}."))
                continue
            values = prop.get("enum") or ([prop["const"]] if "const" in prop else [])
            out[path] = {"type": prop.get("type"), "description": prop.get("description") or prop.get("title") or "",
                         "values": list(values)}
        return out


_STOP = frozenset("a an and as at be but by for from has have in is it its of on or that the then this to "
                  "until use which with".split())


def _tokens(text: str) -> set[str]:
    """Lower-case words without stop words, with a plural s dropped."""
    words = re.findall(r"[a-z0-9]+", (text or "").replace("_", " ").replace(".", " ").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in words if w not in _STOP}


class RulesRepairer:
    """Deterministic repair from openapi.json alone (no model, no key).

    Endpoints: each old endpoint's successor is the one non-deprecated
    operation with the same method and the same path after the version
    segment (/v1/accounts -> /v2/accounts). A POST below the new hold path
    that takes the hold's id and is named confirm is a new step.
    Fields: a field keeping its old name keeps its meaning; otherwise the
    field of the right type whose name (weight 3) and description (weight 1)
    share the most words with the canonical field's definition, and only if
    it beats the runner-up. Request field names must agree with the list
    filters; a required request field the mapping does not cover gets the
    enum value "recall", or the repair is refused. In-force states are the
    states the confirm call returns. Ambiguity is refused, never guessed.
    """

    source = "rules"

    def propose(self, failure_text: str, error_body: str, openapi: dict, old_adapter: dict) -> Proposal:
        spec = _Spec(openapi)
        paths = {role: self._successor(spec, old_adapter["paths"][role])
                 for role in ("place_hold", "list_holds", "list_accounts")}
        paths["confirm_hold"] = self._confirm_step(spec, paths["place_hold"])
        mapping: dict = {}
        notes = []
        for group, role in (("holds", "list_holds"), ("accounts", "list_accounts")):
            leaves = spec.leaves(spec.response(paths[role]))
            mapping.update(self._map_group(group, leaves, old_adapter["mapping"]))
        notes += [f"{f} -> {mapping[f]}" for f in CANONICAL if mapping[f] != old_adapter["mapping"].get(f)]
        defaults = self._request(spec, paths, mapping)
        notes += [f"{k} = {v}" for k, v in defaults.items()]
        in_force, pending = self._statuses(spec, paths, mapping, old_adapter)
        if paths["confirm_hold"]:
            notes.append(f"a hold now needs {paths['confirm_hold']}")
        proposal = validate_proposal({"paths": paths, "mapping": mapping, "status_in_force": in_force,
                                      "status_pending": pending, "defaults": defaults,
                                      "rationale": "; ".join(notes) or "no change"})
        return Proposal(**proposal, source=self.source)

    @staticmethod
    def _successor(spec: _Spec, old: str) -> str:
        method, _, old_path = old.partition(" ")
        parts = old_path.split("/")
        tail = "/".join(parts[2:])
        found = [f"{m} {p}" for m, p, _ in spec.endpoints()
                 if m == method and p != old_path and len(p.split("/")) == len(parts)
                 and "/".join(p.split("/")[2:]) == tail]
        if len(found) != 1:
            raise RepairError(f"cannot find the one successor of {old} in openapi.json (found {found or 'none'})")
        return found[0]

    @staticmethod
    def _confirm_step(spec: _Spec, place: str) -> str | None:
        base = place.partition(" ")[2].rstrip("/")
        found = []
        for method, path, op in spec.endpoints():
            rest = path[len(base):].split("/") if path.startswith(base + "/") else []
            if method == "POST" and len(rest) == 3 and rest[1].startswith("{"):
                words = _tokens(rest[2]) | _tokens(op.get("summary", "")) | _tokens(op.get("operationId", ""))
                if "confirm" in words:
                    found.append(f"POST {path}")
        if len(found) > 1:
            raise RepairError(f"more than one confirm step below {place}: {found}")
        return found[0] if found else None

    @staticmethod
    def _map_group(group: str, leaves: dict, old_mapping: dict) -> dict:
        """Kept names first (they keep their meaning), then the best scorer."""
        def kind(field):
            return "integer" if field in INTEGER_FIELDS else "string"

        out, taken = {}, set()
        for field in GROUPS[group]:
            old = old_mapping.get(field)
            if old in leaves and leaves[old]["type"] == kind(field):
                out[field] = old
                taken.add(old)
        for field in GROUPS[group]:
            if field in out:
                continue
            want = _tokens(field) | _tokens(DEFINITIONS[field])
            scored = sorted(((3 * len(want & _tokens(path)) + len(want & _tokens(leaf["description"])), path)
                             for path, leaf in leaves.items() if path not in taken and leaf["type"] == kind(field)),
                            reverse=True)
            if not scored or scored[0][0] == 0:
                raise RepairError(f"no field in the new {group} schema matches {field}")
            if len(scored) > 1 and scored[1][0] == scored[0][0]:
                raise RepairError(f"{field}: {scored[0][1]} and {scored[1][1]} match equally well")
            out[field] = scored[0][1]
            taken.add(scored[0][1])
        return out

    @staticmethod
    def _request(spec: _Spec, paths: dict, mapping: dict) -> dict:
        body = spec.request(paths["place_hold"])
        fields = spec.leaves(body)
        for canonical in ("batch", "account_scope"):
            if mapping[canonical] not in fields:
                raise RepairError(f"{paths['place_hold']} takes no {mapping[canonical]} (for {canonical})")
        for role in ("list_holds", "list_accounts"):
            if mapping["batch"] not in spec.query(paths[role]):
                raise RepairError(f"{paths[role]} does not filter by {mapping['batch']}")
        defaults = {}
        for name in body.get("required") or []:
            if name in (mapping["batch"], mapping["account_scope"]):
                continue
            values = fields.get(name, {}).get("values") or []
            if "recall" not in values:
                raise RepairError(f"{paths['place_hold']} requires {name}, and no value of it means a recall")
            defaults[name] = "recall"
        return defaults

    @staticmethod
    def _statuses(spec: _Spec, paths: dict, mapping: dict, old_adapter: dict) -> tuple[list, list]:
        status = mapping["status"]
        placed = spec.leaves(spec.response(paths["place_hold"]))
        if mapping["hold_id"] not in placed:
            raise RepairError(f"{paths['place_hold']} does not answer {mapping['hold_id']}")
        if paths["confirm_hold"]:
            in_force = spec.leaves(spec.response(paths["confirm_hold"])).get(status, {}).get("values") or []
            if not in_force:
                raise RepairError(f"{paths['confirm_hold']} does not say which {status} is in force")
            return in_force, [v for v in placed.get(status, {}).get("values", []) if v not in in_force]
        values = spec.leaves(spec.response(paths["list_holds"])).get(status, {}).get("values") or []
        in_force = [v for v in values if v in old_adapter.get("status_in_force", [])]
        if not in_force and len(values) == 1:
            in_force = values
        if not in_force:
            raise RepairError(f"cannot tell which {status} values mean a hold is in force: {values}")
        return in_force, [v for v in values if v not in in_force]


class ClaudeRepairer:
    """One Messages API call per repair, JSON-only output validated like any
    proposal. A refusal, a truncated answer or invalid JSON is a RepairError
    (the order system stays paused), never a guess."""

    source = "model"

    def __init__(self, api_key: str | None = None, *, model: str | None = None,
                 transport: httpx.BaseTransport | None = None, base_url: str | None = None,
                 timeout: float = 120.0, env=None, max_attempts: int = 3):
        env = os.environ if env is None else env
        self.api_key = api_key or env.get("ANTHROPIC_API_KEY") or ""
        if not self.api_key:
            raise RepairError("ClaudeRepairer needs ANTHROPIC_API_KEY")
        self.model = model or env.get("REPAIR_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or env.get("ANTHROPIC_BASE_URL") or API_URL).rstrip("/")
        self.transport, self.timeout, self.max_attempts = transport, timeout, max_attempts
        self.system_prompt = (AGENT_DIR / "repair_prompt.md").read_text(encoding="utf-8")

    def request_body(self, failure_text: str, error_body: str, openapi: dict, old_adapter: dict) -> dict:
        old = {k: old_adapter.get(k) for k in ("version", "paths", "mapping", "status_in_force", "status_pending",
                                                "defaults")}
        brief = "\n\n".join([
            f"<failure>\n{failure_text}\n</failure>",
            f"<error_body>\n{error_body}\n</error_body>",
            f"<old_adapter>\n{json.dumps(old, indent=1)}\n</old_adapter>",
            f"<canonical_fields>\n{json.dumps(DEFINITIONS, indent=1)}\n</canonical_fields>",
            f"<openapi>\n{json.dumps(openapi)}\n</openapi>",
            "Propose the adapter for the order system as it is now."])
        return {"model": self.model, "max_tokens": 16000, "system": self.system_prompt,
                "messages": [{"role": "user", "content": brief}],
                "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
                "fallbacks": "default"}

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "anthropic-beta": FALLBACK_BETA,
                "content-type": "application/json"}

    def _post(self, body: dict) -> dict:
        """POST /v1/messages, retrying rate limits, overloads and lost connections."""
        last, wait = "no attempt made", 0.0
        with httpx.Client(base_url=self.base_url, transport=self.transport, timeout=self.timeout) as http:
            for attempt in range(self.max_attempts):
                if attempt:
                    time.sleep(wait)
                try:
                    resp = http.post("/v1/messages", json=body, headers=self._headers())
                except httpx.TransportError as exc:
                    last, wait = f"{type(exc).__name__}: {exc}", _retry_after(None, attempt)
                    continue
                if resp.status_code == 200:
                    return resp.json()
                last = f"Anthropic API answered {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    raise RepairError(last)
                wait = _retry_after(resp.headers.get("retry-after"), attempt)
        raise RepairError(last)

    def propose(self, failure_text: str, error_body: str, openapi: dict, old_adapter: dict) -> Proposal:
        data = self._post(self.request_body(failure_text, error_body, openapi, old_adapter))
        usage = {k: (data.get("usage") or {}).get(k) for k in ("input_tokens", "output_tokens")}
        stop = data.get("stop_reason")
        if stop == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            raise RepairError(f"the model declined to propose an adapter ({category})", model_calls=1)
        if stop == "max_tokens":
            raise RepairError("the model's proposal was cut off at max_tokens", model_calls=1)
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        try:
            proposal = validate_proposal(json.loads(text))
        except ValueError as exc:
            raise RepairError(f"the model's proposal is not JSON: {exc}", model_calls=1) from None
        except RepairError as exc:
            raise RepairError(f"the model's proposal is invalid: {exc}", model_calls=1) from None
        return Proposal(**proposal, source=self.source, model_calls=1, usage=usage)


def _retry_after(header: str | None, attempt: int) -> float:
    try:
        return min(10.0, max(0.0, float(header)))
    except (TypeError, ValueError):
        return min(10.0, 2.0 ** attempt)


class ScriptedRepairer:
    """A fixed proposal, for tests. `calls` records every propose()."""

    def __init__(self, proposal: dict, *, source: str = "scripted", model_calls: int = 0):
        self.proposal, self.source, self.model_calls = proposal, source, model_calls
        self.calls: list[str] = []

    def propose(self, failure_text: str, error_body: str, openapi: dict, old_adapter: dict) -> Proposal:
        self.calls.append(failure_text)
        return Proposal(**validate_proposal(self.proposal), source=self.source, model_calls=self.model_calls)


def default_repairer(env=None):
    """Claude when ANTHROPIC_API_KEY is set, else the rules."""
    env = os.environ if env is None else env
    return ClaudeRepairer(env=env) if env.get("ANTHROPIC_API_KEY") else RulesRepairer()


# ---------------------------------------------------------------- memory

def remember(db, embedder, adapter: dict, failure: dict, *, from_version: int, recall_id: str | None,
             now) -> dict | None:
    """Save a working repair as an `adapter` episode, embedded by its failure
    text. Returns None (and the run goes on) if the embedder is unreachable."""
    text = failure["text"]
    try:
        vector = embedder.embed([text])[0]
    except EMBED_ERRORS as exc:
        log.warning("adapter episode for %s not written: embedding failed: %s", adapter["_id"], exc)
        return None
    changes = [f"{f} -> {adapter['mapping'][f]}" for f in CANONICAL if adapter["mapping"][f] != f]
    doc = {
        "_id": f"ep-{secrets.token_hex(4)}", "kind": "adapter", "valid": True, "recall_id": recall_id,
        "order_system": adapter["order_system"], "system_vendor": adapter.get("vendor"), "failure_text": text,
        "from_version": from_version, "version": adapter["version"], "adapter_id": adapter["_id"],
        "source": adapter["source"], "paths": adapter["paths"], "steps": adapter["steps"],
        "mapping": adapter["mapping"], "status_in_force": adapter["status_in_force"],
        "status_pending": adapter["status_pending"], "defaults": adapter["defaults"],
        "expected_fields": adapter["expected_fields"], "canary": adapter.get("canary"),
        "summary": f"{text}. Repaired as adapter v{adapter['version']}: {', '.join(changes) or 'same fields'}"
                   + (f"; hold confirmed with {adapter['paths']['confirm_hold']}" if adapter["paths"].get(
                       "confirm_hold") else ""),
        "tags": ["adapter"], "reused": 0, "embedding": vector, "embedding_model": embedder.name, "created_at": now,
    }
    db.episodes.insert_one(doc)
    return doc


def find_episode(db, embedder, failure_text: str, *, vendor: str | None, from_version: int,
                 k: int = 5) -> tuple[dict | None, str]:
    """The best `adapter` episode for this failure from the same vendor and
    the same old version: (episode or None, "atlas" | "local").

    $vectorSearch with the filter {kind: "adapter", valid: true}; a cluster
    without the search index (or one that has not caught up) is scored in
    Python over the same vectors, and reported as "local"."""
    query = embedder.embed([failure_text])[0]
    hits, how = [], "atlas"
    try:
        hits = list(db.episodes.aggregate([
            {"$vectorSearch": {"index": VECTOR_INDEX, "path": "embedding", "queryVector": query, "exact": True,
                               "limit": k, "filter": {"kind": "adapter", "valid": True}}},
            {"$set": {"score": {"$meta": "vectorSearchScore"}}},
            {"$unset": "embedding"},
        ]))
    except OperationFailure as exc:
        log.debug("$vectorSearch unavailable (code %s): scoring adapter episodes locally", exc.code)
    hits = [h for h in hits if h.get("embedding_model") == embedder.name]
    if not hits:
        how = "local"
        for ep in db.episodes.find({"kind": "adapter", "valid": True, "embedding_model": embedder.name}):
            vector = ep.pop("embedding", None) or []
            if len(vector) == len(query):
                hits.append({**ep, "score": (1.0 + cosine(query, vector)) / 2.0})
        hits.sort(key=lambda h: (-h["score"], str(h["_id"])))
    for hit in hits:
        if hit.get("system_vendor") == vendor and hit.get("from_version") == from_version:
            return hit, how
    return None, how


def _from_episode(ep: dict) -> Proposal:
    proposal = validate_proposal({k: ep.get(k) for k in ("paths", "mapping", "status_in_force", "status_pending",
                                                          "defaults")})
    return Proposal(**{**proposal, "rationale": f"from episode {ep['_id']}"}, source=f"episode {ep['_id']}")


# ---------------------------------------------------------------- repair

def _recall_id(ctx, order_system: str) -> str | None:
    recall = getattr(ctx, "recall", None)
    if isinstance(recall, dict):
        return recall.get("_id")
    if isinstance(recall, str):
        return recall
    party = ctx.db.parties.find_one({"order_system": order_system, "channel": "order_system"}, {"recall_id": 1},
                                   sort=[("created_at", -1)])
    return party["recall_id"] if party else None


def _lock(ctx, order_system: str) -> str | None:
    """One repair per order system at a time (a lease in worker_state): two workers
    must not both pay for a model call. Returns the owner token, or None."""
    now = server_now(ctx.client)
    owner = f"{getattr(ctx.settings, 'worker_name', 'worker')}/{secrets.token_hex(3)}"
    try:
        ctx.db.worker_state.find_one_and_update(
            {"_id": f"repair:{order_system}", "$or": [{"until": {"$lt": now}}, {"until": None}]},
            {"$set": {"until": now + timedelta(seconds=LOCK_TTL_S), "owner": owner}}, upsert=True)
    except DuplicateKeyError:
        return None
    return owner


def _unlock(ctx, order_system: str, owner: str) -> None:
    ctx.db.worker_state.update_one({"_id": f"repair:{order_system}", "owner": owner}, {"$set": {"until": EPOCH}})


def release_locks(db, worker_name: str) -> int:
    """Free every repair lock held by a boot of `worker_name`. Called when a
    newer boot of that name starts: the old one is dead (a kill runs no
    finally), so its repair is over and need not hold the order system for the
    rest of LOCK_TTL_S. Returns how many locks were freed."""
    owners = {"$regex": f"^{re.escape(worker_name)}/"}
    res = db.worker_state.update_many({"_id": {"$regex": "^repair:"}, "owner": owners, "until": {"$gt": EPOCH}},
                                      {"$set": {"until": EPOCH}})
    return res.modified_count


def _emit(ctx, recall_id, order_system, kind, text, *, level="info", detail=None):
    feed.emit(ctx.db, kind, text, recall_id=recall_id, party_id=None, level=level, source="worker",
              system=operations.system(order_system), detail=detail)


def request_repair(ctx, order_system: str, *, repairer=None, recall_id: str | None = None) -> dict:
    """Repair the order system's adapter if its contract failed. Returns
    {"status": "healthy"|"busy"|"activated"|"resumed"|"rejected"|"failed", ...}:
    activated carries adapter_id, version, source, model_calls, canary,
    retried (the outcomes of the flagged ops) and episode_id; resumed (an
    earlier repair activated its adapter but did not finish the retry)
    carries adapter_id, version, retried and episode_id."""
    recall_id = recall_id or _recall_id(ctx, order_system)
    if recall_id is None:
        return {"status": "failed", "reason": f"no recall has a distributor on order system {order_system}"}
    owner = _lock(ctx, order_system)
    if owner is None:
        return {"status": "busy", "reason": f"another repair of order system {order_system} is running"}
    try:
        return _repair(ctx, order_system, recall_id, repairer)
    except adapters.AdapterConflict as exc:
        # A repair that outlived its lock lost the race; the winner's adapter stands.
        return {"status": "busy", "reason": str(exc)}
    finally:
        _unlock(ctx, order_system, owner)


def _repair(ctx, order_system: str, recall_id: str, repairer) -> dict:
    db = ctx.db
    client = ctx.order_systems[order_system]
    distributor = operations.distributor_party(db, recall_id, order_system)
    if distributor is None:
        return {"status": "failed", "reason": f"recall {recall_id} has no distributor on order system {order_system}"}
    batch = db.recalls.find_one({"_id": recall_id}, {"batch_code": 1})["batch_code"]
    old = adapters.active(db, order_system)
    failure = old.get("last_failure")
    if failure is None and _interrupted(db, order_system, distributor, old):
        return _resume(ctx, order_system, recall_id, old)
    if failure is None:
        try:
            client.list_accounts(batch, adapter=old)
        except ContractError as exc:
            failure = operations.contain(ctx, order_system, exc)["failure"]
        except (OrderSystemTimeout, OrderSystemError) as exc:
            return {"status": "failed", "reason": f"order system unavailable: {exc}"}
        else:
            return {"status": "healthy", "adapter_id": old["_id"]}
    try:
        openapi = client.openapi()
    except (OrderSystemTimeout, OrderSystemError) as exc:
        return {"status": "failed", "reason": f"cannot read the order system's openapi.json: {exc}"}
    vendor = (openapi.get("info") or {}).get("title")
    if getattr(ctx.settings, "memory_enabled", True):
        try:
            episode, how = find_episode(db, get_embedder(ctx.settings), failure["text"], vendor=vendor,
                                        from_version=old["version"])
        except EMBED_ERRORS as exc:
            log.warning("adapter memory skipped: embedding failed: %s", exc)
            episode, how = None, "off"
        if episode is not None:
            out = _try(ctx, order_system, recall_id, distributor, batch, old, failure, vendor, _from_episode(episode),
                       episode=episode, how=how)
            if out["status"] == "activated":
                return out
    repairer = repairer or getattr(ctx, "repairer", None) or default_repairer()
    try:
        proposal = repairer.propose(failure["text"], failure.get("error_body") or "", openapi, old)
    except RepairError as exc:
        _count_calls(db, recall_id, exc.model_calls)
        _emit(ctx, recall_id, order_system, "adapter_repair_failed", f"repair failed: {exc}. Order system stays paused",
              level="bad", detail={"model_calls": exc.model_calls})
        return {"status": "failed", "reason": str(exc), "model_calls": exc.model_calls}
    _count_calls(db, recall_id, proposal.model_calls)
    return _try(ctx, order_system, recall_id, distributor, batch, old, failure, vendor, proposal)


def _count_calls(db, recall_id: str, n: int) -> None:
    if n:
        runs.inc(db, recall_id, "model_calls", n)


def _try(ctx, order_system, recall_id, distributor, batch, old, failure, vendor, proposal: Proposal, *,
         episode: dict | None = None, how: str | None = None) -> dict:
    """Candidate -> canary -> activate, or reject with the order system still paused."""
    db = ctx.db
    sim = operations.sim_now(ctx, recall_id)
    candidate = adapters.save_candidate(db, adapters.build_candidate(
        order_system, proposal.spec(), old, source=proposal.source, vendor=vendor, model_calls=proposal.model_calls,
        usage=proposal.usage, rationale=proposal.rationale, failure=failure))
    check = canary.run_canary(db, recall_id=recall_id, distributor_id=distributor["party_id"], batch=batch,
                              client=ctx.order_systems[order_system], candidate=candidate, sim=sim)
    version = candidate["version"]
    result = {"adapter_id": candidate["_id"], "version": version, "source": proposal.source,
              "model_calls": proposal.model_calls, "canary": check,
              "episode_id": episode["_id"] if episode else None}
    if not check["passed"]:
        adapters.reject(db, candidate["_id"], check)
        why = (f"{canary.describe(check)} for account {check['account']}" if check["order_system_value"] is not None
               else check["note"])
        if episode is not None:
            text = (f"episode {episode['_id']} retrieved, but its mapping failed the canary ({why}); "
                    "asking the repairer")
        else:
            text = f"adapter v{version} rejected: {why}. Order system stays paused"
        _emit(ctx, recall_id, order_system, "adapter_rejected", text, level="bad",
              detail={"canary": check, "mapping": candidate["mapping"], "source": proposal.source})
        return {**result, "status": "rejected"}
    activated = adapters.activate(ctx.client, db, candidate, old, canary=check)
    # Remember before retrying: the episode depends only on the activated
    # adapter, and the retry makes order system calls that a kill can interrupt.
    result["episode_id"] = _remember_once(ctx, recall_id, activated, failure=failure, from_version=old["version"],
                                          episode=episode)
    flagged = operations.flagged_count(db, order_system)
    shown = canary.describe(check) + (" (shipped count from the old read: no account corroborated yet)"
                                      if check["basis"] == "sender_qty" else "")
    retrying = f"Retrying {flagged} operation{'' if flagged == 1 else 's'}"
    if episode is not None:
        text = (f"contract failed. Episode {episode['_id']} retrieved. Adapter v{version} active, 0 model calls "
                f"({shown}). {retrying}")
    else:
        text = f"adapter v{version} active: {shown}. {retrying}"
    _emit(ctx, recall_id, order_system, "adapter_activated", text, level="good",
          detail={"adapter_id": activated["_id"], "source": proposal.source, "mapping": activated["mapping"],
                  "model_calls": proposal.model_calls, "memory": how, "canary": check})
    retried = operations.retry_needs_reverify(ctx, order_system)
    return {**result, "status": "activated", "retried": retried}


def _interrupted(db, order_system: str, distributor: dict, adapter: dict) -> bool:
    """An earlier repair activated `adapter` (its canary passed, the order system
    is unpaused) but its retry did not finish: flagged ops remain."""
    return (not distributor.get("order_system_paused") and bool((adapter.get("canary") or {}).get("passed"))
            and operations.flagged_count(db, order_system) > 0)


def _resume(ctx, order_system: str, recall_id: str, adapter: dict) -> dict:
    """Finish what an interrupted repair left: the episode, then the retry of
    the ops still flagged. The adapter already passed its canary, so nothing
    is proposed or checked again and no model call is made."""
    db = ctx.db
    episode = None
    source = adapter.get("source") or ""
    if source.startswith("episode "):
        episode = db.episodes.find_one({"_id": source.split(" ", 1)[1]}, {"embedding": 0})
    replaced = db.adapters.find_one({"_id": adapter.get("replaces")}, {"version": 1}) or {}
    episode_id = _remember_once(ctx, recall_id, adapter, failure=adapter.get("failure"),
                                from_version=replaced.get("version", adapter["version"] - 1), episode=episode)
    flagged = operations.flagged_count(db, order_system)
    _emit(ctx, recall_id, order_system, "adapter_resumed",
          f"adapter v{adapter['version']} active; its repair was interrupted. Retrying the {flagged} "
          f"operation{'' if flagged == 1 else 's'} still flagged", level="good",
          detail={"adapter_id": adapter["_id"], "flagged": flagged, "episode_id": episode_id})
    retried = operations.retry_needs_reverify(ctx, order_system)
    return {"status": "resumed", "adapter_id": adapter["_id"], "version": adapter["version"],
            "retried": retried, "episode_id": episode_id}


def _remember_once(ctx, recall_id: str, adapter: dict, *, failure: dict | None, from_version: int,
                   episode: dict | None = None) -> str | None:
    """The adapter episode for an activated adapter, written at most once
    (a resumed repair calls this again). A repair taken from `episode`
    counts one reuse of it instead, also once per activation. Returns the
    episode id, or None when there is nothing to remember (no failure
    recorded) or the embedder is unreachable (the repair stands without an
    episode, as it always has).

    Adapter ids repeat across runs ("north:v2"), so this activation's episode
    is the one created at its activated_at (remember() stamps it so)."""
    db = ctx.db
    if episode is not None:
        first = db.adapters.update_one({"_id": adapter["_id"], "reuse_counted": {"$ne": True}},
                                       {"$set": {"reuse_counted": True}}).modified_count == 1
        if first:
            db.episodes.update_one({"_id": episode["_id"]}, {"$inc": {"reused": 1}})
        return episode["_id"]
    saved = db.episodes.find_one({"kind": "adapter", "adapter_id": adapter["_id"],
                                  "created_at": adapter.get("activated_at")}, {"_id": 1})
    if saved is not None:
        return saved["_id"]
    if not failure:
        return None
    ep = remember(db, get_embedder(ctx.settings), adapter, failure, from_version=from_version,
                  recall_id=recall_id, now=adapter.get("activated_at"))
    return ep["_id"] if ep else None
