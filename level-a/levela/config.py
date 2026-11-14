"""Environment loading and constants.

Every tunable lives here so the API, the worker, the simulator and the tests
read the same values. `Settings.from_env()` is the only entry point; tests
build a Settings with overrides instead of mutating os.environ.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # level-a/
SCENARIO_DIR = ROOT / "scenarios"
AGENT_DIR = ROOT / "agent"
# The screen (web/, React + three.js), built to web/dist by `npm run build`.
SCREEN_DIR = ROOT / "web" / "dist"

DB_NAME = "levela"
DEMO_RECALL_IDS = ["RCL-2026-017", "RCL-2026-018"]

# Attempt lifecycle. LIVE states are covered by the partial unique index.
LIVE_ATTEMPT_STATES = ("intent", "dialed", "connected")
FINAL_ATTEMPT_STATES = ("completed", "no_answer", "failed", "abandoned")
ATTEMPT_STATES = LIVE_ATTEMPT_STATES + FINAL_ATTEMPT_STATES

PARTY_STATES = ("pending", "contacting", "reached", "claimed", "reconciled")
CALL_FAILPOINTS = ("after_intent", "after_dial_request", "after_mark_dialed")
# Order system operations (V2 §5): intent written / request sent, its answer not recorded.
ORDER_SYSTEM_FAILPOINTS = ("after_op_intent", "after_op_sent")
FAILPOINTS = CALL_FAILPOINTS + ORDER_SYSTEM_FAILPOINTS

# A party may be challenged on its numbers this many times per attempt.
MAX_CHALLENGES = 2

# Public development values for the three shared secrets. They are in the
# repository, so they authenticate nothing: Settings.from_env() uses one only
# when its variable is unset AND LEVELA_DEV=1 (a laptop-only run, where every
# process reads the same .env). Without that opt-in an unset secret stays
# empty, and deps.secret_matches / signing.verify refuse everyone, so a host
# that forgot a secret fails closed instead of accepting the public value.
DEV_SECRETS = {"tool_secret": "dev-tool-secret", "webhook_secret": "dev-webhook-secret",
               "admin_token": "dev-admin-token"}


def is_placeholder_secret(value: str | None) -> bool:
    """True for a secret that protects nothing: unset, or a public development
    value. Anything that hands a secret to an outside service (provisioning
    the voice agent's tools) must refuse these."""
    return not value or value in DEV_SECRETS.values()


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Read KEY=VALUE lines from .env (cwd, then level-a/). Never overrides
    variables that are already set."""
    candidates = [Path(path)] if path else [Path.cwd() / ".env", ROOT / ".env"]
    for p in candidates:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            return


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(v, default):
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Atlas
    atlas_uri: str = "mongodb://127.0.0.1:27017/?replicaSet=rs0"
    db_name: str = DB_NAME

    # URLs. api_base_url is how the worker and the simulator reach the API;
    # public_base_url is what ElevenLabs is configured to call.
    api_base_url: str = "http://127.0.0.1:8000"
    public_base_url: str = ""

    # Shared secrets. Empty means unset: every request that needs one is
    # refused (see DEV_SECRETS for the laptop-only opt-in).
    tool_secret: str = ""           # header X-Tool-Secret on /tools/* and /sim/*
    webhook_secret: str = ""        # ElevenLabs post-call HMAC secret
    admin_token: str = ""           # header X-Admin-Token on /admin/*

    # ElevenLabs + Twilio
    eleven_api_key: str = ""
    eleven_agent_id: str = ""
    eleven_phone_id: str = ""
    eleven_base_url: str = "https://api.elevenlabs.io"
    twilio_sid: str = ""
    twilio_token: str = ""
    ringing_timeout_s: int = 20

    # Which provider dials channel=voice parties: "elevenlabs" (real phone) or
    # "sim" (the simulator plays the party; used for the kill-demo fallback).
    voice_provider: str = "elevenlabs"

    # Embeddings (see levela.memory.embed): provider chosen from these.
    embed_provider: str = ""
    embed_api_key: str = ""
    embed_model: str = "voyage-4"
    embed_base_url: str = "https://ai.mongodb.com/v1"
    embed_dims: str = ""
    memory_enabled: bool = True

    # Worker
    worker_name: str = field(default_factory=lambda: f"worker-{socket.gethostname()}".lower())
    heartbeat_s: float = 2.0
    worker_dead_after_s: float = 8.0      # another worker's attempts are in doubt after this silence
    reconcile_every_s: float = 2.0        # level-triggered safety net beside the change stream
    indoubt_grace_s: float = 15.0         # never abandon an attempt younger than this
    probe_skew_s: float = 5.0             # clock skew allowed when matching Twilio start times
    stale_call_s: float = 120.0           # dialed/connected with no post-call after this: ask Twilio
    max_attempts: int = 3                 # no_answer/failed attempts before a party goes to a human
    retry_backoff_s: float = 30.0

    # Leases (V2 section 5): a claimed party is held for lease_s real seconds
    # unless the holder extends it, which it does every lease_extend_s while
    # it waits for a call. Tests shrink both.
    lease_s: float = 60.0
    lease_extend_s: float = 15.0

    # Simulator
    sim_time_scale: float = 1.0           # multiply every scripted delay; 0 = as fast as possible

    # Harness (V2 §6). "rules" is deterministic and needs no key; "claude"
    # asks the Anthropic Messages API and needs ANTHROPIC_API_KEY.
    run_min_real_s: float = 20.0          # a task run's time budget never ends before this much real time
    planner: str = "rules"
    planner_model: str = "claude-haiku-5-5"
    repair_model: str = "claude-sonnet-5-5"
    anthropic_api_key: str = ""

    # Mock distributor order systems (V2 §7): one process per distributor. The API
    # proxies the operator's switch/fault buttons to their admin endpoints and
    # reads their hold logs for invariant 2.
    order_north_url: str = "http://127.0.0.1:8101"
    order_east_url: str = "http://127.0.0.1:8102"

    # The API's background scheduler (V2 §10): invariants once per simulated
    # hour and scheduled order system changes. Tests switch it off and call tick().
    scheduler_enabled: bool = True

    # Sub-recall between nodes (spec "Sub-recall, agent to agent"). node_id
    # names this copy; parent_url is set only on a child node (Summit Foods).
    # The link secret is shared per pairing; LINK_SECRET_<PEER> overrides it
    # for one peer (levela.node.links.link_secret). PORT is the API's port.
    node_id: str = "maple-farm"
    parent_url: str = ""
    agent_link_secret: str = ""
    port: int = 8000

    @classmethod
    def from_env(cls, env=None, **overrides) -> "Settings":
        if env is None:
            load_dotenv()
            env = os.environ
        g = env.get
        # An unset secret stays empty (fail closed) unless this is an explicit
        # laptop-only development run; a value that is set always wins.
        dev = DEV_SECRETS if _bool(g("LEVELA_DEV"), False) else {}
        s = cls(
            atlas_uri=g("ATLAS_URI") or cls.atlas_uri,
            db_name=g("ATLAS_DB") or DB_NAME,
            api_base_url=(g("API_BASE_URL") or cls.api_base_url).rstrip("/"),
            public_base_url=(g("PUBLIC_BASE_URL") or "").rstrip("/"),
            tool_secret=g("TOOL_SECRET") or dev.get("tool_secret", ""),
            webhook_secret=g("ELEVEN_WEBHOOK_SECRET") or g("WEBHOOK_SECRET") or dev.get("webhook_secret", ""),
            admin_token=g("ADMIN_TOKEN") or dev.get("admin_token", ""),
            eleven_api_key=g("ELEVEN_API_KEY") or "",
            eleven_agent_id=g("ELEVEN_AGENT_ID") or "",
            eleven_phone_id=g("ELEVEN_PHONE_ID") or "",
            eleven_base_url=(g("ELEVEN_BASE_URL") or cls.eleven_base_url).rstrip("/"),
            twilio_sid=g("TWILIO_SID") or "",
            twilio_token=g("TWILIO_TOKEN") or "",
            ringing_timeout_s=int(_float(g("RINGING_TIMEOUT_S"), 20)),
            voice_provider=(g("VOICE_PROVIDER") or "elevenlabs").lower(),
            embed_provider=g("EMBED_PROVIDER") or "",
            embed_api_key=g("EMBED_API_KEY") or g("VOYAGE_API_KEY") or "",
            embed_model=g("EMBED_MODEL") or cls.embed_model,
            embed_base_url=g("EMBED_BASE_URL") or cls.embed_base_url,
            embed_dims=g("EMBED_DIMS") or "",
            memory_enabled=_bool(g("MEMORY_ENABLED"), True),
            worker_name=g("WORKER_NAME") or f"worker-{socket.gethostname()}".lower(),
            heartbeat_s=_float(g("HEARTBEAT_S"), 2.0),
            worker_dead_after_s=_float(g("WORKER_DEAD_AFTER_S"), 8.0),
            reconcile_every_s=_float(g("RECONCILE_EVERY_S"), 2.0),
            indoubt_grace_s=_float(g("INDOUBT_GRACE_S"), 15.0),
            probe_skew_s=_float(g("PROBE_SKEW_S"), 5.0),
            stale_call_s=_float(g("STALE_CALL_S"), 120.0),
            max_attempts=int(_float(g("MAX_ATTEMPTS"), 3)),
            retry_backoff_s=_float(g("RETRY_BACKOFF_S"), 30.0),
            lease_s=_float(g("LEASE_S"), 60.0),
            lease_extend_s=_float(g("LEASE_EXTEND_S"), 15.0),
            sim_time_scale=_float(g("SIM_TIME_SCALE"), 1.0),
            run_min_real_s=_float(g("RUN_MIN_REAL_S"), 20.0),
            planner=(g("PLANNER") or cls.planner).lower(),
            planner_model=g("PLANNER_MODEL") or cls.planner_model,
            repair_model=g("REPAIR_MODEL") or cls.repair_model,
            anthropic_api_key=g("ANTHROPIC_API_KEY") or "",
            order_north_url=(g("ORDER_NORTH_URL") or cls.order_north_url).rstrip("/"),
            order_east_url=(g("ORDER_EAST_URL") or cls.order_east_url).rstrip("/"),
            scheduler_enabled=_bool(g("SCHEDULER_ENABLED"), True),
            node_id=g("NODE_ID") or cls.node_id,
            parent_url=(g("PARENT_URL") or "").rstrip("/"),
            agent_link_secret=g("AGENT_LINK_SECRET") or "",
            port=int(_float(g("PORT"), 8000)),
        )
        return replace(s, **overrides) if overrides else s

    def embed_env(self) -> dict:
        """The environment levela.memory.embed.make_embedder() reads."""
        out = {"EMBED_MODEL": self.embed_model, "EMBED_BASE_URL": self.embed_base_url}
        if self.embed_provider:
            out["EMBED_PROVIDER"] = self.embed_provider
        if self.embed_api_key:
            out["EMBED_API_KEY"] = self.embed_api_key
        if self.embed_dims:
            out["EMBED_DIMS"] = self.embed_dims
        return out

    @property
    def webhook_base(self) -> str:
        return self.public_base_url or self.api_base_url

    @property
    def order_urls(self) -> dict[str, str]:
        """Order system name (as on a distributor party's `order system` field) -> base URL."""
        return {"north": self.order_north_url, "east": self.order_east_url}
