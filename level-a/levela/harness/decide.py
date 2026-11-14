"""Deciders (V2 §6): given a brief, choose the next tool call (or none).

RuleDecider  deterministic, the default: the contact ladder for parties, one
             follow-up per dispute, the question only for reconciled parties,
             the order system's fixed operation order, and the planner's priority rule
             (unresolved + 2 x disputed + 50 when questions are open).
ClaudeDecider  the Anthropic Messages API (PLANNER=claude, ANTHROPIC_API_KEY):
             the brief is the user turn and the run's allowlist is the tool
             list. When the model cannot be reached, or declines, the rules
             decide instead, so a model outage never stalls the recall.

A decider only proposes. Every call still goes through the gate, and the
outcome is read from the database afterwards; a decider's own claim of
success (`Decision.result`) is checked against the facts like any other.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import AGENT_DIR
from .brief import Brief, ladder_tool
from .models import AnthropicClient, ModelError, tool_definition

log = logging.getLogger("levela.harness.decide")

PLANNER_PRIORITY_CALLS = 5     # priorities per planner run; the last tool call is the report
DECIDER_SYSTEM = ("You are the decider inside a panel recall harness. The user turn is a brief about one piece "
                  "of work. Follow its RULES and ASK. Call exactly one of the tools you are given, or answer "
                  "with one short sentence and no tool call when nothing is owed now.")
PLANNER_PROMPT_FILE = AGENT_DIR / "planner_prompt.md"


@dataclass
class Decision:
    """One decider decision. `tool` None means the run is done. `result` is
    a decider's claimed result (checked against the facts, never trusted)."""
    tool: str | None
    args: dict = field(default_factory=dict)
    note: str = ""
    result: dict | None = None
    tokens: int | None = None
    model_calls: int = 0

    def to_doc(self) -> dict:
        return {"tool": self.tool, "args": dict(self.args), "note": self.note, "result": self.result,
                "tokens": self.tokens, "model_calls": self.model_calls}

    @classmethod
    def from_doc(cls, doc: dict) -> "Decision":
        return cls(tool=doc.get("tool"), args=dict(doc.get("args") or {}), note=doc.get("note") or "",
                   result=doc.get("result"), tokens=doc.get("tokens"), model_calls=int(doc.get("model_calls") or 0))

    def same_action(self, other: "Decision") -> bool:
        """Same tool with the same arguments (notes may differ)."""
        return self.tool == other.tool and dict(self.args) == dict(other.args)


class RuleDecider:
    name = "rules"
    is_model = False
    continues_after_effect = False

    def decide(self, brief: Brief) -> Decision:
        facts = brief.facts
        kind = facts.get("kind")
        done = facts.get("done") or []
        if kind == "planner":
            return self._planner(facts, done)
        if kind == "order_system_sync":
            nxt = (facts.get("order_system") or {}).get("next_op")
            if not nxt or any(d["tool"] == nxt for d in done):
                return Decision(None, note="order system is up to date for now")
            return Decision(nxt, note=f"order system order: {nxt} is next")
        if done:
            return Decision(None, note="this run's contact is made")
        if kind == "contact_party":
            tool = ladder_tool(facts)
            return Decision(tool, note=f"ladder step {facts['party']['ladder_step']}: {tool}")
        if kind == "followup":
            return Decision("place_call", note=f"follow-up: {(facts.get('dispute') or {}).get('note', '')}")
        if kind == "ask_question":
            return Decision("place_call", note="ask the open questions only")
        return Decision(None, note=facts.get("reason") or "nothing owed")

    def _planner(self, facts: dict, done: list) -> Decision:
        touched = {d.get("target") for d in done}
        if sum(d["tool"] == "set_priority" for d in done) < PLANNER_PRIORITY_CALLS:
            for t in sorted(facts.get("top") or [], key=lambda t: (-abs(t["wanted"] - t["priority"]), t["party_id"])):
                if t["wanted"] != t["priority"] and t["party_id"] not in touched:
                    return Decision("set_priority", {"party_id": t["party_id"], "priority": t["wanted"]},
                                    note=f"{t['party_id']}: {t['priority']} -> {t['wanted']}")
        if not any(d["tool"] == "draft_report" for d in done):
            return Decision("draft_report", {"text": status_report(facts)}, note="status report")
        return Decision(None, note="ranking holds")


def status_report(facts: dict) -> str:
    """A one-paragraph draft status report from the planner's view."""
    open_parties = sum(n for state, _level, n in facts.get("by_state") or [] if state != "reconciled")
    unresolved = sum(int(t.get("unresolved") or 0) for t in facts.get("top") or [])
    worst = ", ".join(t["name"] for t in (facts.get("top") or [])[:3]) or "none"
    fails = ", ".join(f"{k} {v}" for k, v in (facts.get("failures") or {}).items()) or "none"
    return (f"{facts.get('sim_label')}: {open_parties} parties still open; the ten largest hold {unresolved} "
            f"unexplained panels (largest: {worst}). Failures in the last simulated day: {fails}.")


class ClaudeDecider:
    """Decides with Claude; the rules decide whenever the model gives no usable answer."""

    is_model = True
    continues_after_effect = False

    def __init__(self, client: AnthropicClient, model: str, *, fallback: RuleDecider | None = None,
                 planner_prompt: str | None = None):
        self.client, self.model = client, model
        self.name = f"claude:{model}"
        self.fallback = fallback or RuleDecider()
        self._planner_prompt = planner_prompt

    def decide(self, brief: Brief) -> Decision:
        from .tools import REGISTRY

        tools = [tool_definition(n, REGISTRY[n].description, REGISTRY[n].input_schema)
                 for n in brief.facts.get("tools") or [] if n in REGISTRY]
        system = self._system(brief.facts.get("kind"))
        try:
            reply = self.client.messages(model=self.model, system=system, tools=tools,
                                         messages=[{"role": "user", "content": brief.text}])
        except ModelError as exc:
            log.warning("decider model unavailable (%s): the rules decide", exc)
            return self._fallback(brief, f"model unavailable ({exc}); rules decided", tokens=None)
        if reply.refused or reply.stop_reason == "max_tokens":
            return self._fallback(brief, f"model stopped ({reply.stop_reason}); rules decided",
                                  tokens=reply.input_tokens)
        if not reply.tool_uses:
            return Decision(None, note=reply.text[:300] or "model made no tool call", tokens=reply.input_tokens,
                            model_calls=1)
        use = reply.tool_uses[0]
        return Decision(use.name, dict(use.input), note=reply.text[:300], tokens=reply.input_tokens, model_calls=1)

    def _fallback(self, brief: Brief, note: str, *, tokens) -> Decision:
        decision = self.fallback.decide(brief)
        return Decision(decision.tool, decision.args, note=f"{note}: {decision.note}", tokens=tokens, model_calls=1)

    def _system(self, kind: str | None) -> str:
        if kind != "planner":
            return DECIDER_SYSTEM
        if self._planner_prompt is None:
            self._planner_prompt = PLANNER_PROMPT_FILE.read_text(encoding="utf-8")
        return self._planner_prompt


def build_decider(settings, *, http=None):
    """The decider the settings ask for. PLANNER=claude without a key falls
    back to the rules, loudly: a missing key must not stop the run."""
    if settings.planner == "claude":
        if settings.anthropic_api_key:
            return ClaudeDecider(AnthropicClient(settings.anthropic_api_key, http=http), settings.planner_model)
        log.warning("PLANNER=claude but ANTHROPIC_API_KEY is not set: using the rules decider")
    elif settings.planner != "rules":
        raise ValueError(f"PLANNER must be rules or claude, not {settings.planner!r}")
    return RuleDecider()
