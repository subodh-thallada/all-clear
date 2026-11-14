# All Clear — implementation design and contracts

These are the engine's first design notes, kept for the interfaces between
modules and the places the build extends the spec (§10). The product spec is
`../../docs/All_Clear_build_spec_and_test_plan.md` and the builders' contract
is `BUILD_PLAN.md`; where this file and either of them disagree, they win.
Field names here follow the food version (six buckets: `sold`, `on_shelf`,
`held`, `returned`, `forwarded`, `unresolved`). The seed kit is not used.

## 1. Layout

```
level-a/
  pyproject.toml, requirements*.txt, Makefile, .env.example, README.md
  levela/
    config.py            Settings.from_env(); constants (LIVE/FINAL states, FAILPOINTS, MAX_CHALLENGES)
    feed.py              emit()/recent(): the wall's event feed (collection `feed`)
    db/client.py         get_client/get_db, run_txn(client, fn, counter=) (with_transaction + retry count)
    db/setup.py          setup(db): spec collections/validators/indexes (from levela.db.schema) + feed, sim_calls
    db/seed.py           start_recall(...), reset(db), runtime_recall_ids(db)
    ledger/claims.py     submit_claim(), dedupe_key(), speakable(), challenges_used(), CHALLENGE_CODES
    ledger/transfers.py  resolve_party(), edge_numbers(), stub_party(), open_edge(), settle_edge()
    ledger/reconciler.py open_units(), mass_balance(), metrics(), try_close(), atlas_error()
    ledger/graph.py      downstream() ($graphLookup), tiers()
    ledger/rebuild.py    replay accepted claims onto a fresh ledger (optional depth)
    orchestrator/attempts.py   open_attempt/mark_dialed/mark_connected/complete/adopt/abandon, AttemptRefused
    orchestrator/failpoints.py Failpoints(db, crash=).hit(name, attempt), arm()/disarm()/armed(), WorkerCrash
    orchestrator/escalation.py plan(): who to contact next; dispute follow-ups; retries; human escalation
    orchestrator/recovery.py   resolve_in_doubt(), check_stale(): evidence before any redial
    orchestrator/worker.py     Worker: change stream + periodic reconcile, dispatch, heartbeat, episodes
    providers/base.py    VoiceProvider / CallProbe protocols, PlacedCall, ProbedCall, ProviderError/Timeout
    providers/voice.py   ElevenLabsVoice (outbound-call API)
    providers/twilio_probe.py  TwilioProbe (Calls list + fetch)
    providers/sim.py     SimNetwork (plays scripted parties, posts through the API), SimVoice, SimProbe
    providers/fake_voice.py    FakeVoice: in-process test double with fault injection
    memory/embed.py      get_embedder(settings)
    memory/episodes.py   write_episode_for_attempt()
    memory/retrieve.py   retrieve() ($vectorSearch, local fallback), policy()
    api/main.py          create_app(settings, *, sim_network=None) -> FastAPI
    api/tools.py, hooks.py, form.py, admin.py, wall.py, sim.py (routers)
    wall/index.html, wall.js, wall.css     the wall display (plain HTML/JS, polls /wall/state at 1 Hz)
    __main__.py          CLI: python -m levela {api,worker,setup-db,start,reset,close,status,preflight,...}
  scenarios/recall_1.json, recall_2.json
  agent/prompt.md, agent/first_message.txt, agent/tool_submit_count.json, agent/agent_config.json
  scripts/               call_once.py, provision_agent.py, kill_demo.{sh,ps1}, crash_schedules.py, demo.py, video_shots.md
  tests/                 pytest; conftest.py gives each test a scratch DB
  deploy/                render.yaml, Dockerfile, fly.toml, tunnel notes
```

## 2. Collections (database `levela`)

Spec collections: `recalls shipments parties edges claims attempts tool_events episodes worker_state`.
Added: `feed` (wall event lines) and `sim_calls` (the simulator's call log — the
simulated equivalent of Twilio's Calls list; also the fake provider's call counter).

Document shapes beyond what the spec lists (fields marked * are new):

**recalls** `_id batch_code batch_spoken brand product hazard unit units_total open_units status opened_at closed_at`
`scenario_id synthetic mode* ("demo"|"sim") options*{all_sim, sim_parties, followups("auto"|"manual"), memory(bool)}`
`directory*[{party_id,name,aliases,tier,channel,phone,profile}]` (phones already resolved) `followups_released*(bool)`

**parties** `_id="{recall}:{party}" recall_id party_id name shipped_in sold on_shelf held returned forwarded unresolved`
`state tier channel phone profile discovered_by ("shipment"|"claim:<party>"|"memory") form_token* created_at last_claim_at`
`memory*{checked_at, mode("atlas"|"local"|"off"), hits[{episode_id, party_id, recall_id, score, tags}], actions[...]}`
`escalation*` ("human" when the party hit the challenge cap or max attempts) `memory_source*{episode_id, from_party}` (stubs created by memory)

**attempts** `_id="att_<14 hex>" recall_id party_id seq channel purpose ("initial"|"retry"|"receipt"|"dispute_followup"|"form")`
`state intent_at dialed_at connected_at ended_at conversation_id call_sid to_number* owner*{worker, boot}`
`dial_confirmed*(bool) in_doubt*(bool, provider timed out) adopted*{evidence, at, by} outcome_reason* context*{dynamic vars sent}`
`dispute_key*` (follow-ups: "{edge_id}:{sender_qty}:{recipient_qty}") `told*` (the other side's number) `episode_id*`

**claims** `_id party_id recall_id attempt_id source ("voice"|"sim"|"form") raw status reject_reason code* reported_total shipped_in`
`atlas_error*{code, codeName, errmsg, errInfo}` `dedupe_key created_at`

**tool_events** `_id attempt_id party_id recall_id conversation_id call_sid* kind ("tool_call"|"post_call") source* payload received_at`

**feed** `at kind text level ("info"|"good"|"warn"|"bad") source ("api"|"worker"|"sim"|"admin") recall_id party_id attempt_id detail`

**sim_calls** `_id=call_sid ("CAsim…") conversation_id ("conv_sim…") to attempt_id recall_id party_id purpose status (Twilio vocabulary)`
`start_time end_time script_key replies[{at, say, claim, reply}] faults{}`

**worker_state** documents: `seed_meta` (history loader), `failpoints` {armed:{name:{party_id,channel,armed_at}}},
`worker:<name>` {boot_id, pid, host, started_at, heartbeat_at, resume_token, resumed{at, missed}, stopped_at},
`badge:crash_schedules` / `badge:rebuild` {text, at, detail} (lines the wall shows), `sim_release:<recall>:<party>` {released_at}.

## 3. Rules enforced by Atlas (defined in levela/db/schema.py)

1. Party conservation validator (`$jsonSchema` non-negative ints + `$expr` equation).
2. Edge sanity validator (`corroborated_qty <= sender_qty`).
3. Close gate validator on recalls (`status: closed` only when `open_units: 0`).
4. Partial unique index `one_live_attempt_per_party` on attempts while state ∈ {intent, dialed, connected}.

## 4. Module contracts

### Ledger (built, tested by smoke run)
`submit_claim(client, db, *, recall_id, party_id, attempt_id, source, raw, now=None, directory=None, claim_id=None, require_delivery_note=False, fail_after_party_update=False) -> dict`
Result: `accepted, claim_id, message` always; on reject `code, reason`, and for `over_count` `reported_total, shipped_in`, plus
`atlas_error` when the validator refused; on accept `open_units, txn_runs, discovered? (new stub party_id), edge? {.., before}`;
`duplicate: True` on a repeated delivery. `raw` keys: `sold on_shelf held returned forwarded_qty forwarded_to_name received_qty batch_seen sold_source block_test`.
Codes: `over_count negative not_integer bad_number recipient_overdocumented delivery_note_required delivery_note_mismatch
no_inbound_edge forward_missing_name forward_self forward_changed forward_restate recall_closed unknown_party unknown_recall
receipt_sender_ambiguous` (a party with several senders must name the one it counts: `received_from`). `raw` may also carry `forwards: [{to_name | to_party_id, qty}]` (several destinations) and `received_from` (DESIGN_V2 §9).
`CHALLENGE_CODES` count toward the per-attempt cap `MAX_CHALLENGES = 2`; `challenges_used(db, attempt_id)`.

### Attempts (built)
See `orchestrator/attempts.py` docstring. `open_attempt(...)` raises `AttemptRefused(message, index, existing)`;
`message` is Atlas's raw `E11000 duplicate key error ... index: one_live_attempt_per_party ...` — show it verbatim.

### Providers (`providers/base.py`, built)
`VoiceProvider.place_call(*, to_number, dynamic_variables, attempt_id) -> PlacedCall(conversation_id, call_sid, raw)`;
raises `ProviderError` (answered, no call) or `ProviderTimeout` (outcome unknown → attempt in doubt).
`CallProbe.find_calls(*, to_number, since) -> [ProbedCall]` newest first; `CallProbe.call_status(call_sid) -> ProbedCall|None`.
Status vocabulary is Twilio's; `FINAL_STATUS_TO_ATTEMPT` maps it.

### Failpoints (built)
`Failpoints(db, crash=None, env=None, worker=None).hit(name, attempt)`; names in `config.FAILPOINTS`.
Production crash is `os._exit(137)`. Tests pass `crash=raise_crash` and catch `WorkerCrash` (a BaseException).

## 5. The worker

`Worker(settings, db, client, *, providers: dict[channel -> VoiceProvider], probes: dict[channel -> CallProbe], embedder, failpoints=None, clock=None)`
— `channel` is `"voice"` or `"sim"` (`form` parties are never dialed). `run(stop_event=None)`, `step()` (one reconcile pass; tests drive it), `startup()`.

Startup: register `worker:<name>` with a new `boot_id`; run recovery for in-doubt attempts owned by any dead boot
(this worker's previous boot is dead by definition; another worker's boot is dead after `worker_dead_after_s` without heartbeat);
open the change stream from the saved resume token, drain it, and emit
`worker_resumed`: "Resumed from saved change stream token; processed N events missed while down" (or "no saved token / token
expired: full rescan"). Then loop:

1. Wait up to ~0.5 s on the change stream (`db.watch` over parties/edges/attempts/claims/recalls). Any event marks the open recalls dirty;
   save the resume token at most once a second.
2. Every `reconcile_every_s` or when dirty: for each open recall, `escalation.plan(...)` and execute the actions.
3. `recovery.resolve_in_doubt(...)` for attempts flagged in doubt (own lost responses, or dead owners) and `recovery.check_stale(...)`.
4. Write an episode for every attempt in a final state that has none (`memory.episodes.write_episode_for_attempt`, idempotent: `_id = "ep_" + attempt_id`).
5. Heartbeat every `heartbeat_s`.

Placing a call (spec "Placing a call"): open_attempt (intent, `owner={worker, boot}`, `to_number`, `context`=dynamic vars) →
`failpoints.hit("after_intent")` → `provider.place_call` → `failpoints.hit("after_dial_request")` → `mark_dialed` →
`failpoints.hit("after_mark_dialed")`. `ProviderError` → `complete(attempt, "failed")`. `ProviderTimeout` → set `in_doubt: True`, leave in intent.

Dynamic variables (all strings): `attempt_id party_id party_name batch_code batch_spoken brand product hazard unit tier purpose
company ("Tamarack Distribution") sender_name (downstream: who forwarded, else "") told_qty (follow-up: the other side's number, else "")
ask_delivery_note ("yes"|"no") followup_note (one sentence for dispute follow-ups, else "")`. **Never `shipped_in`.**

### escalation.plan(db, recall, settings, now, embedder) -> list[Action]
Rules, per party, skipping any party with a live attempt or `escalation == "human"` or `channel == "form"`:
- Before a party's first attempt and if `recall.options.memory`: memory retrieval (`memory.retrieve`), party `parties.memory`,
  apply `memory.policy`: `suspect_party` → upsert a stub (`discovered_by: "memory"`, `memory_source`) via `resolve_party(name, recall.directory)`
  and emit `memory_suspect`; `ask_delivery_note` → that party's call gets `ask_delivery_note=yes` and the API enforces it (§6).
- Not yet claimed (pending/contacting/reached) and no attempt yet → `initial`.
- Last attempt ended without an accepted claim: `no_answer`/`failed` → `retry` after `retry_backoff_s`, up to `max_attempts` failures, then
  `escalation: "human"` + feed `needs_human`. `completed` without a claim → `retry` (same cap). `abandoned` → next attempt at once.
  A downstream party whose last rejection was `no_inbound_edge` waits until an inbound edge exists, then `receipt`.
- Party hit the challenge cap on its last attempt → `escalation: "human"`.
- Disputed edge and (`options.followups == "auto"` or `recall.followups_released`): for each party with no attempt carrying this
  `dispute_key`, a `dispute_followup` attempt with `told` = the other side's number. Each party once per dispute_key.
Execute contacts concurrently across parties (one attempt per party at a time is the index's job, not the decider's).

### recovery
`resolve_in_doubt(db, attempt, probe, settings, now, me) -> {"action": "adopted"|"abandoned"|"deferred", "evidence": ...}`:
1. `tool_events` has an event with this `attempt_id` → adopt its `conversation_id`/`call_sid`; never redial. Feed `attempt_adopted`
   "Attempt att_… adopted from tool_events: call CA… happened while the worker was down. No redial."
2. The attempt already reached a final state through the post-call webhook → adopt (evidence `post_call`).
3. Younger than `indoubt_grace_s` → deferred (the provider may still be creating the call).
4. `probe.find_calls(to_number, since=intent_at - probe_skew_s)`, compare start timestamps in code (Twilio filters by date only),
   skip SIDs already on another attempt → adopt the newest; state from its status.
5. Otherwise `abandon` ("no evidence of a call; this is the only path that dials again") and let the decider open the next attempt.
`check_stale(...)`: dialed/connected with no post_call after `stale_call_s` → `probe.call_status(call_sid)`; final → close to match.

## 6. API (FastAPI, `create_app(settings, *, sim_network=None)`)

Auth: `/tools/*` and `/sim/*` need header `X-Tool-Secret: <TOOL_SECRET>` (constant-time compare). `/admin/*` needs
`X-Admin-Token: <ADMIN_TOKEN>` (or `?key=` for the wall's buttons). `/hooks/post_call` needs a valid `ElevenLabs-Signature`
(HMAC-SHA256, see vendor_reference.md); the simulator signs its post-call payloads the same way.

| Endpoint | Body / reply |
|---|---|
| `POST /tools/submit_count` | Body: `attempt_id party_id system__conversation_id system__call_sid sold on_shelf held returned forwarded_qty forwarded_to_name received_qty batch_seen sold_source block_test` (+ optional `source`, only honoured with the tool secret: "sim"). Steps: log `tool_events` (kind tool_call) first; attempt must exist and match party; `mark_connected`; party pending/contacting → reached; if `challenges_used >= MAX_CHALLENGES` reply `{accepted:false, stop:true, message}` without booking and set `escalation:"human"`; else `submit_claim(require_delivery_note=attempt.context.ask_delivery_note == "yes")`; feed lines. Reply: `{accepted, message, reason?, reported_total?, shipped_in?, duplicate?, challenges_left, stop}`. HTTP 200 always for well-formed calls (the agent reads the JSON). |
| `POST /hooks/post_call` | Verify HMAC; log tool_events (kind post_call, payload without audio); `post_call_transcription` → `complete(attempt, "completed")`, party transcript summary on the attempt (`transcript`: list of {role, message}, `call_duration_secs`); `call_initiation_failure` → `busy`/`unknown` → `failed`, `no-answer` → `no_answer`; `post_call_audio` ignored. Attempt id from `data.conversation_initiation_client_data.dynamic_variables.attempt_id`, fallback: lookup by conversation_id. Always 200 after a valid signature. |
| `GET /form/{token}` / `POST /form/{token}` | Mobile-first HTML form. POST fields as the tool. Uses the party's live attempt if any, else opens a `form` attempt (`channel: "form"`, `dial_confirmed: True`, state connected) and completes it on acceptance. Shows the same accept/reject message, the two numbers, and the raw Atlas error. `GET /form/{token}/qr.svg` (segno). |
| `POST /admin/recalls` | `{scenario, all_sim?, sim_parties?, mode?, followups?, memory?, recall_id?}` → start_recall. |
| `POST /admin/failpoint` | `{name, party_id?, channel?}` arm; `{name: null}` disarm all. |
| `POST /admin/reset` | reset(db) (history stays). |
| `POST /admin/close` | `{recall_id}` → try_close; feed `close_refused` with the raw Atlas error, or `recall_closed`. |
| `POST /admin/release` | `{recall_id, party_id}` → release a `demo_hold` simulator reply. |
| `POST /admin/followups` | `{recall_id}` → `followups_released: true`. |
| `GET /admin/status` | recalls, worker heartbeats, armed failpoints, sim call counts. |
| `POST /sim/calls` | (tool secret) `{to_number, dynamic_variables, attempt_id, faults?}` → `SimNetwork.place_call` → `{success, conversation_id, callSid}` — the simulator's stand-in for ElevenLabs' outbound-call API. `GET /sim/calls?to=&since=` and `GET /sim/calls/{sid}` → the stand-in for Twilio's Calls list. |
| `GET /wall`, `GET /wall/state?recall_id=` | page + JSON (§8). Static assets under `/wall/static/`. |
| `GET /healthz` | `{ok, db, version}`. |

## 7. The simulator (`providers/sim.py`)

`SimNetwork(db, poster, settings)` is a fake telephony vendor. `place_call(to_number, dynamic_variables, attempt_id, faults=None)`
inserts a `sim_calls` row (status in-progress) and starts a daemon thread that plays the party:
script = `scenario.sim[party_id]["dispute_followup" if purpose == "dispute_followup" else "initial"]` (scenario from `recall.scenario_id`).
For each reply in order: wait `delay_s * settings.sim_time_scale` after the previous reply (first: after the call starts);
if `demo_hold` and recall.mode == "demo", wait for `worker_state sim_release:<recall>:<party>` (feed `sim_hold` once);
if `must_follow: X`, wait until X has an accepted claim in this recall; then POST the tool body
(`{**claim, attempt_id, party_id, system__conversation_id, system__call_sid, source: "sim", delivery_note?}` — the delivery note from
`reply.answers.delivery_note` when the call's `ask_delivery_note == "yes"`) through `poster.post_tool(body)` and record the reply.
Then post a signed `post_call_transcription` (transcript from the `say` lines) through `poster.post_hook(raw_bytes, headers)`, and mark the
sim_call completed. Faults (for tests and crash schedules): `duplicate_webhook` (post each tool body twice), `late_post_call_s`,
`no_answer` (post `call_initiation_failure` no-answer instead of replies). The network lives in the API process (so killing the worker
does not kill a simulated call, exactly like a real phone call), and in-process in tests.
`HttpPoster(base_url, tool_secret, webhook_secret)` posts over HTTP; tests use a poster around FastAPI's TestClient.
`SimVoice(api_base_url, tool_secret)` (worker side) → `POST /sim/calls`. `SimProbe(db)` reads `sim_calls`.
`FakeVoice(network, faults=...)` (tests): calls `network.place_call` in-process; `drop_response` places the call then raises
`ProviderTimeout`; `fail_before` raises `ProviderTimeout` without placing; records every call in `.calls`.

## 8. Wall state JSON (`GET /wall/state`)

```json
{
  "now": "ISO", "recall": {"_id","batch_code","product","brand","hazard","units_total","status","mode","options","opened_at","closed_at"} | null,
  "recalls": [{"_id","status","batch_code"}],
  "metrics": {"checks_completed":[a,b],"tins_located":[a,b],"held_or_returned":[a,b],"open_units":n,"disputed_units":n,"mass_balance":n,"units_total":n},
  "parties": [{"party_id","name","tier","channel","state","shipped_in","sold","on_shelf","held","returned","forwarded","unresolved",
              "discovered_by","escalation","live_attempt":{"_id","state","purpose","seq"}|null,"attempts":n,"memory":{...}|null}],
  "edges": [{"from_party","to_party","sender_qty","recipient_qty","corroborated_qty","disputed_qty","state"}],
  "feed": [{"at","kind","text","level","source","party_id","detail"}],      // newest first, ≤ 60
  "worker": {"name","alive","heartbeat_age_s","boot_id","resumed":{...}} | null,
  "calls": {"total": n, "by_party": {"party_id": n}},                        // sim_calls + attempts with a call_sid
  "failpoints": {"name": {...}},
  "badges": [{"key","text"}],                                                // crash schedules, rebuild
  "memory_ablation": {"with": n, "without": n} | null
}
```
The recall shown is `?recall_id=` or the most recently opened run-time recall. Cache the JSON for 0.5 s (free tier: 100 ops/s).
The endpoint is public, so it carries no credential: no party's form token or form URL (the token alone books counts
through `POST /form/{token}`). The operator gets the form links from `POST /admin/recalls` (`make demo` prints them).

## 9. Tests

`tests/conftest.py` gives `db`, `client`, `settings` (fast timings, `voice_provider="sim"`, `embed_provider="hash"`), `assert_balance`.
Every test uses its own scratch database. Run: `python -m pytest` from `level-a/` (pyproject sets `pythonpath=["."]`).
Spec tests 1–7, 10, 11 are automated; vector-index checks are `@pytest.mark.atlas` and skip on a plain mongod.
Assert the mass balance after every ledger write in ledger tests.

## 10. Where the build extends or fixes the spec

1. **Raw Atlas errors kept.** Rejected claims party `atlas_error` (code 121 + errInfo) and the wall shows it verbatim.
2. **Closed recalls refuse claims** inside the claim transaction (and the close gate would refuse the open_units write anyway).
3. **Challenge cap enforced server-side** (2 per attempt), not just in the prompt; the party then goes to a human instead of being redialled forever.
4. **Delivery-note check is database-side** when memory flags `over_reported`; the note is part of the dedupe key so a corrected answer is not swallowed as a duplicate.
5. **In-doubt is explicit** (`dial_confirmed`), not inferred from `state == intent`: the post-call webhook can finish an attempt while the worker is dead, and recovery still adopts it (and logs it) instead of missing it.
6. **Grace period before abandoning** (`INDOUBT_GRACE_S`): ElevenLabs may still be creating the Twilio call; abandoning too early would redial.
7. **Ownership + heartbeats**: a worker only resolves another worker's in-doubt attempts after that worker's heartbeat is stale — two workers can run at once.
8. **Level-triggered safety net**: a periodic reconcile beside the change stream, so a lost or expired resume token cannot strand a party.
9. **Simulated calls outlive the worker.** The simulator is hosted by the API process, so the kill demo's fallback (Corner Mart simulated) behaves like a real call: the claim lands while the worker is down.
10. **Demo pacing is data.** `mode: demo` honours `demo_hold`; `followups: manual` keeps the dispute open until the closing-gate beat (the spec's automatic follow-ups would otherwise resolve it in two seconds, and a follow-up to Corner Mart would ring the teammate's phone a second time during the kill beat).
11. **Failpoints can be scoped** to a party or channel, so a simulated party's dial cannot trip the demo failpoint.
12. **Admin endpoints are authenticated** (the API is on a public host).
13. **forwards_to memory applies only to the episode's own party** (an earlier finding); `over_reported` may be inherited.

## 11. Interfaces added after the vendor check (see vendor_reference.md)

- `levela/signing.py`: `sign(raw, secret, t=None) -> header`, `verify(raw, header, secret) -> bool` (ElevenLabs HMAC format). The API verifies with it; the simulator signs with it.
- `levela/providers/__init__.py`: `build_providers(settings, db) -> (providers, probes)`, dicts keyed by channel `"voice"`/`"sim"`.
  voice → `ElevenLabsVoice` + `TwilioProbe` when `settings.voice_provider == "elevenlabs"`, else `SimVoice` + `SimProbe`; sim → `SimVoice` + `SimProbe`.
- Sim poster interface: `post_tool(body: dict) -> dict` (the JSON reply) and `post_hook(raw: bytes, headers: dict) -> int` (status).
  `HttpPoster(base_url, tool_secret)` and `ClientPoster(client, tool_secret)` (wraps anything with `.post(url, content=, json=, headers=)`, e.g. FastAPI TestClient).
- Memory: `memory.embed.get_embedder(settings)`; `memory.retrieve.retrieve(db, embedder, profile, k=3, mode="auto") -> (hits, how)`;
  `memory.retrieve.policy(hits, party) -> [{"action": "suspect_party", "name", "episode_id", "from_party"} | {"action": "ask_delivery_note", "episode_id"}]`
  (`suspect_party` only when the episode's `party_id == party["party_id"]`); `memory.episodes.summarize(attempt, claims, party, edges) -> (summary, tags, entities)`
  (pure); `memory.episodes.write_episode_for_attempt(db, embedder, attempt, now=None) -> dict | None` (idempotent, `_id = "ep_" + attempt_id`);
  `memory.retrieve.contacts_to_full_discovery(db, recall_id) -> int` (attempts opened before the last party was created).
  Tags written: `answered no_answer failed abandoned counts_matched over_reported forwards_to received_transfer disputed stock_returned destroyed needs_human`;
  entities: `forwards_to` (party name), `received_from`.
- Worker: `Worker(settings, db, client, *, providers, probes, embedder=None, failpoints=None, clock=None)`; `orchestrator.worker.main(settings)` builds
  everything with `build_providers`, `memory.embed.get_embedder`, `Failpoints(db, worker=name)` and runs until SIGINT/SIGTERM.
- API: `create_app(settings=None, *, sim_network=None, client=None) -> FastAPI` with `app.state.settings/.client/.db/.sim`.
- ElevenLabs tools are created with `POST /v1/convai/tools` and referenced by `tool_ids` (inline tools are deprecated).
  `call_initiation_failure` carries **no dynamic variables**: match the attempt by `conversation_id`, then `metadata.body.CallSid`,
  then `metadata.body.To` (the party's number) and the newest live attempt to that number.
