# All Clear v2 — the long run and the harness

Contract for the second build wave, from the updated spec
(`../../docs/Level_A_build_spec_and_test_plan.md`, sections "Long run" and "Planner, subagents and harness").
`DESIGN.md` still holds for everything this file does not change: the ledger, attempts, failpoints,
providers, the simulator's scripted mode, the API endpoints, the wall JSON (§8, extended in §12 here).
Where the two disagree, this file wins. Section numbers in code comments refer to this file as "V2 §n".

## 0. What changes, in one paragraph

The worker stops planning from a change stream and becomes a pool of worker threads that **lease** the
next **due** party (`next_action_at <= simulated now`) from Atlas, run one **task run** on it through a
**tool gate**, and release it with its next **timer** in one transaction. Time is a **simulated clock**
computed from three numbers. A **contact ladder** decides what happens after no answer. Two distributors
are reached through a **mock order system** (separate FastAPI process) whose contract changes on day 3; the
adapter is **repaired** (model or rules), **verified by a canary** against the ledger, and **remembered**
as an episode so the second order system repairs from Vector Search with no model call. A **supervisor** kills
the worker at random; the API checks **four invariants** every simulated hour. A **planner** ranks work and
adds **questions**; it cannot touch the ledger. The four-party slice (recall_1) runs on the same engine.

## 1. Ownership for this wave (agents)

| Agent | Owns |
|---|---|
| engine | `levela/orchestrator/{claim,timers,ladder,worker,planner}.py` (+ reuse `attempts.py`, `recovery.py`, `failpoints.py`), `scripts/supervisor.py`, tests `test_claim.py`(23,24) `test_capacity.py`(30) `test_timers.py`(21) `test_worker.py` `test_random_kills.py`(22,31) `test_long_run.py`(16) `e2e_sim.py`(10) |
| harness | `levela/harness/*` (`gate, tools, brief, result, budget, verify, trace, saboteur, decider, models`), `agent/planner_prompt.md`, `scripts/replay_run.py`, tests `test_gate.py`(25–28) `test_saboteur.py`(29) `test_harness.py` |
| systems | `levela/mock/order_system.py`, `levela/systems/*` (`order_client, operations, contract, adapters, canary, repair`), `agent/repair_prompt.md`, tests `test_order_system_hold.py`(17) `test_order_system_change.py`(18,19) `test_adapter_memory.py`(20) `test_mock_order_system.py` |
| sim | `scenarios/generate.py`, `scenarios/recall_long.json`, `levela/providers/profiles.py`, `levela/providers/sim.py` (extend), tests `test_generate.py` `test_profiles.py` `test_sim.py` |
| ledger | `levela/ledger/*` (multi-destination forwards, sources, `invariants.py`), `levela/db/*` (schema v2), `levela/runs.py`, tests 1–5 + `test_invariants.py` `test_rebuild.py` |
| api | `levela/api/*` (+ `questions.py`, `scheduler.py`), tests `test_api.py` `test_form.py` `test_hooks.py` `test_questions.py`(32) `test_admin_v2.py` |
| wall | `levela/wall/*`, `tests/test_wall_assets.py` |
| ops | `agent/*` except the two prompts above, `scripts/*` except supervisor/replay_run, `deploy/*`, `Makefile`, `.github/workflows/ci.yml`, `.gitattributes`, `README.md` files, `docs/RUNBOOK.md` |

Foundation already written for this wave (read, do not rewrite): `levela/clock.py`, `levela/runs.py`,
`levela/db/setup.py` (schema v2), `seedkit/schema.py` (party channel adds `order system`).

## 2. Simulated time (`levela/clock.py`, written)

Simulated time is a float: **simulated hours since the run's day 0, 00:00**. `177.0` is day 7, 09:00
(`clock.fmt(177) == "d7 09:00"`). `runs.clock = {real_start, sim_start, seconds_per_sim_hour, paused_at,
paused_total_s}`; `sim_now = sim_start + (real_now - real_start - paused) / seconds_per_sim_hour`. Nothing
ticks. Real time is the **database's** clock (`clock.server_now(client)`: local time plus an offset taken
from `hello.localTime`), so every process agrees. Changing speed re-anchors (`real_start = now,
sim_start = sim_now`), so simulated time never jumps. Settings: 30 s/sim-hour (round one), 15 (finals),
0.05 in tests. Recalls without a long-run schedule (recall_1, recall_2) also get a run and a clock
(default 30 s/sim-hour, `sim_start` 9.0) so one engine serves both.

Helpers: `Clock.sim_now(real=None)`, `Clock.real_at(h)`, `Clock.seconds_until(h)`, `Clock.real_seconds(sim_hours)`,
`clock.fmt(h)`, `clock.day(h)`, `clock.next_at_hour(h, hour)` (the next time of day ≥ h), `clock.get_clock(db, run_id)`.

## 3. Runs (`levela/runs.py`, written)

`runs` document `_id = "run:<recall_id>"`: `recall_id, clock{...}, steps, kills, duplicates, model_calls,
tokens{last, max, total, n}, last_invariant_check{at, passed[4], details}, invariant_failed(bool),
supervisor{paused(bool), kills_paused_reason}, saboteur{on(bool), until}, parties_at_start, events{order_system_change_at{north, east} (sim h or null = by hand)},
started_at, schedule (copied from the scenario)`.
Functions: `create_run(db, recall_id, *, seconds_per_sim_hour, sim_start, parties_at_start, schedule)`,
`get_run(db, recall_id)`, `inc(db, recall_id, field, n=1)`, `record_tokens(db, recall_id, n)`, `set_clock(...)`.

## 4. Parties, v2 fields (set by `start_recall`, `stub_party`, account creation)

`level` (1 shipment list, 2 distributor account, 3 named by a claim) — the spec's integer "tier"; the
existing `tier` enum (`direct`/`downstream`) stays because the validator enforces it. `found_via`
(`shipment|order system|claim|memory|rep`), `next_action_at` (sim hours or null = no timer), `ladder_step` (1–5),
`hold_for_demo` (bool), `slice` (bool: one of the four live parties), `followups` (`auto|manual`, from the
recall options for slice parties, `auto` otherwise), `lease_owner` (str|null), `lease_until` (datetime;
`1970-01-01` when free), `lease_epoch` (int, 0), `priority` (int), `open_questions` ([qid]), `answers`
({qid: {value, at, attempt_id}}), `order system` (`north|east` on distributors), `distributor` (party_id of the
distributor on accounts), `account` (order system account id), `last_outcome` ({outcome, at}), `memory_hour`
(int hour of day learned from episodes, or null). Distributors have `channel: "order system"`.
Validator change (seedkit/schema.py): `channel` enum adds `order system`. Everything else is unchanged.

## 5. Leases, timers, ladder, slots (engine)

**Claim** (`orchestrator/claim.py`) — exactly the spec's findOneAndUpdate:
filter `recall_id` (when given), has work (`state != reconciled` or `open_questions` not empty) and not
`escalation: human`, `next_action_at <= sim_now`, `hold_for_demo != true`, `$expr: {$lt: ["$lease_until", "$$NOW"]}`;
sort `priority` desc, `next_action_at` asc; pipeline update `lease_owner = worker`,
`lease_until = {$add: ["$$NOW", lease_ms]}`, `lease_epoch = {$add: [{$ifNull: ["$lease_epoch", 0]}, 1]}`;
`ReturnDocument.AFTER`. `claim_next(db, *, worker_id, sim_now, recall_id=None, lease_s=60) -> party|None`,
`extend(db, party_id, worker_id, epoch, lease_s) -> bool`, `fence(party) -> {"_id", "lease_owner", "lease_epoch"}`.
Every later write by a worker for that party carries the fence; a lost lease writes nothing.
Recovery runs **at claim time**: attempts/operations on this party left in doubt by an earlier epoch are
resolved (DESIGN.md §5 recovery evidence order) before anything new.

**Release** (`orchestrator/timers.py`): `release(client, db, party, fence, *, next_action_at, ladder_step,
last_outcome, extra={}, run_update=None)` — one transaction: the task run's result, the party's timer,
`lease_until = 1970-01-01`. Returns False if the fence no longer matches (and writes nothing).
`due_count(db, recall_id, sim_now)`, `next_due_at(db, recall_id)` (the worker sleeps until then, capped).

**Ladder** (`orchestrator/ladder.py`) — pure functions over sim hours:

| Step | Action | On no answer / busy | Next timer |
|---|---|---|---|
| 1 | call | → step 2 | +4 h |
| 2 | call at `memory_hour` if known, else next day 09:00 | → step 3 | next day 09:00 |
| 3 | form link (the simulator delivers it; the party may submit) | no submission → step 4 | +24 h |
| 4 | third call | → step 5 | +48 h |
| 5 | escalate to the distributor's rep (accounts) / to a human (others) | — | none: units stay unresolved |

`ladder.after(party, outcome, sim_now, *, callback_at=None, memory_hour=None) -> (ladder_step, next_action_at, action)`.
`callback_requested` sets the timer to `callback_at` and keeps the step. `claim_accepted` → `None` timer
(if the party is claimed but waits on an edge, the counterpart's claim wakes it: `next_action_at = now` is set by the API
on dispute or receipt events). `claim_rejected` with the challenge cap → `escalation: human`.

**Slots** (`orchestrator/claim.py`): a voice attempt (real phone) needs a slot. In one transaction: count
live voice attempts, refuse if `>= capacity.max_live` (`capacity` document `_id: "voice"`, default 2), else
insert the attempt's intent and `$inc capacity.version` (two workers racing for the last slot conflict on
that document; one retries and finds none). Simulated calls take no slot. `take_slot_and_intent(...)`.

**Worker** (`orchestrator/worker.py`, rewritten): `python -m levela worker [--workers N] [--recall RCL]`.
N worker threads `w1..wN` (ids `"<worker_name>/w<i>"`) loop: claim → recover-at-claim → `harness.execute(...)`
→ repeat; when nothing is due, sleep until `next_due_at` (≤ 1 s real). Plus one planner thread and one
maintenance thread (stale calls, episodes for finished attempts). Startup writes the resume line to the
feed (kind `worker_resumed`, system `worker`): "restart 9: 3 due, 1 in doubt, 4 documents read, 0 steps replayed"
(`restart n` = `runs.kills`; documents read = the due + in-doubt documents actually fetched). Failpoints
(`after_intent`, `after_dial_request`, `after_mark_dialed`, plus order system `after_op_intent`, `after_op_sent`)
keep working; the demo kill still uses the failpoint.

**Planner** (`orchestrator/planner.py`): one planner at a time (a `worker_state` lease document `planner`, `$$NOW`).
Wakes when a party is created, a question is added, an order system contract fails, a close is refused or an
invariant fails (`runs.planner_wake = true`, set by whoever saw it), else every 6 simulated hours. Reads one
aggregation (parties by state and level, the ten with most unresolved units, oldest due time, free slots,
failure classes from the last simulated day). Writes only through the gate with the planner allowlist:
`set_priority, add_question, escalate, propose_adapter, draft_report`. Default decider: rules
(priority = unresolved + 2×disputed + 50 if open questions); with `PLANNER=claude` it uses `agent/planner_prompt.md`.

## 6. The harness (`levela/harness/`)

**Task run** (`task_runs`): `{_id: "tr-<hex>", worker, recall_id, party_id, lease_epoch, kind, tools[],
budget{model_calls, tool_calls, sim_hours}, used{model_calls, tool_calls, sim_hours}, status
(running|done|refused|stopped|failed), result, failure_class, started_sim, ended_sim, started_at, ended_at}`.
Kinds and tools: `contact_party` [place_call, send_form_link, schedule_callback, escalate];
`followup` [place_call, schedule_callback]; `ask_question` [place_call]; `order_system_sync` [place_hold,
confirm_hold, read_accounts, read_inventory]; `planner` [set_priority, add_question, escalate, propose_adapter, draft_report].
`close_recall` is in no list (only `/admin/close`). Default budget: 4 model calls, 6 tool calls, 2 sim hours.

**`harness.execute(ctx, party, *, worker_id, fence, decider) -> dict`** — the engine's one call:
start the run → build the brief → `decider.decide(brief)` → `gate.call(...)` for each tool call →
wait for the effect (a call: the attempt reaches a final state; extend the lease every 15 real s
(scaled down in tests) with the fence) → derive the outcome **from facts** → `result.check` →
`timers.release(...)` with `ladder.after(...)`. Returns the result. `ctx` = `HarnessContext(settings, client,
db, clock, providers, probes, order systems, memory, failpoints, recall)`.

**Gate** (`harness/gate.py`) `gate.call(ctx, run, tool, args) -> GateDecision{allowed, rule, outcome, evidence_id, duplicate}`.
Order: 1 run is `running`; 2 lease (party `lease_owner`/`lease_epoch` = run's); 3 tool in the run's
allowlist; 4 budget (`$inc used` guarded by `$lt` the limit); 5 intent (insert the attempt/operation with
its idempotency key; a duplicate key returns the stored outcome and does not act); 6 act (provider);
7 record (outcome and provider ids; the gate never writes `verified`). Checks 2, 4, 5 are one
transaction. Every refusal writes a `traces` doc with the rule and a feed line (kind `gate_refused`,
level bad): `REFUSED w5 (saboteur): place_call on queen-st-alarm. Rule: lease. Holder is w2, epoch 9`.
Idempotency keys: place_call `"<run_id>:place_call"`; order system ops `"<recall>:<party>:<kind>"`
(`read_inventory` adds `:d<day>`); form link `"<recall>:<party>:form:<ladder_step>"`.

**Brief** (`harness/brief.py`): `RULES` (the agent's rules + tool list), `PARTY` (name, level, shipped_in,
ladder step, last result with sim time), `EDGES` (in/out, sender/recipient claims), `MEMORY` (≤ 3 episodes),
`QUESTIONS`, `ASK`. Fixed size regardless of run length; `tokens` = len(text)/4 rounded (or the model's
reported input tokens). `runs.tokens` records it — the wall's "Context per decision".

**Deciders** (`harness/decider.py`): `RuleDecider` (the ladder, deterministic, default) and
`ClaudeDecider` (Anthropic Messages API, model from `PLANNER_MODEL`, default `claude-haiku-5-5`, tool use
with the run's allowlist as tools; needs `ANTHROPIC_API_KEY`). Both return `Decision{tool, args, note}`.
`harness/models.py` holds the Anthropic client (httpx, no SDK dependency) used by the decider, the planner and the repairer.

**Result** (`harness/result.py`): schema `{outcome ∈ claim_accepted|claim_rejected|no_answer|callback_requested|
form_sent|answer_recorded|escalated|failed|hold_verified|accounts_read, evidence[ids], next{action, at}, note}`.
Facts: every evidence id exists and belongs to this run; `claim_accepted` needs an accepted claim on this
run's attempt; `hold_verified` needs a verified operation of this run. Otherwise `unsupported_result`.
Failed runs get one failure class (`no_answer, claim_rejected, contract_failed, budget_exceeded,
unsupported_result, gate_refused`) saved as an episode (`kind: "failure"`); the party's next brief includes it.

**Verify** (`harness/verify.py`): call verified = post-call completed **and** an accepted claim carrying the
attempt id; hold = a fresh order system read shows it; form link = a submission with the party's token; answer =
`submit_answer` accepted; adapter = canary. **Trace** (`harness/trace.py`): one `traces` doc per model call and per
tool call: `run_id, seq, kind, brief, output, tool, args, decision, latency_ms, tokens`.
`scripts/replay_run.py RUN_ID` rebuilds a run from its traces with tools stubbed.
**Saboteur** (`harness/saboteur.py`): a decider/worker that breaks one rule on purpose
(`wrong_party, wrong_tool, double_send, false_success, loop, stale`); `/admin/saboteur {on, seconds}` runs one in the live run.

## 7. Systems: the distributor order system (`levela/mock`, `levela/systems`)

**Mock order system** `python -m levela.mock.order_system --name north --port 8101 [--scenario recall_long]`, one process per
distributor, state in memory. v1: `POST /v1/holds {batch, account_scope}` + `Idempotency-Key` → 202 `{hold_id}`;
`GET /v1/holds?batch=` → `[{hold_id, batch, account_scope, status}]`; `GET /v1/accounts?batch=` →
`[{account, name, phone, qty_shipped, qty_in_stock}]`. v2: `POST /v2/holds {batch_code, scope, reason_code}` → 202
`{hold_id, state: "pending_confirm"}`; `POST /v2/holds/{id}/confirm` → 200 `{state: "active"}`; `GET /v2/holds?batch_code=`
→ `[{hold_id, batch_code, scope, state}]`; `GET /v2/accounts?batch_code=` → `[{account, name, phone, shipped,
inventory: {available, quarantined}}]`. After the switch v1 answers 410 with a body naming v2 (the model's error text).
`GET /openapi.json` (FastAPI's, with field descriptions). `GET /_log/holds` → every hold by key (invariant 2).
Admin (`X-Admin-Token`): `POST /_admin/version {version}`, `POST /_admin/fault {fault, count}`
(`accepted_then_dropped`, `timeout_after_commit`, `stale_read` = new holds invisible to reads for 10 sim minutes),
`POST /_admin/clock {seconds_per_sim_hour}`. Idempotency: the same key returns the same hold (except after
`accepted_then_dropped`, which never stored it). URLs: `ORDER_NORTH_URL`, `ORDER_EAST_URL` (default 127.0.0.1:8101/8102).

**Operations** (`operations` collection, validator: `state: verified` requires non-null `readback_at` and
`readback_value`; unique `idempotency_key`): `{_id: "op-<hex>", recall_id, run_id, kind (place_hold|confirm_hold|
read_accounts|read_inventory|send_form_link|escalate_rep), order system, party, idempotency_key, adapter_version,
state (intent|sent|verified|needs_reverify|failed), lease_epoch, sent_at, readback_at, readback_value, request, response, error}`.
Hold flow: intent → POST (record `sent`) → read back → only a read-back that shows it → `verified`. Restart:
`intent`/`sent` ops resolved by read-back first; resent only when the read-back shows nothing, with the same key.
`read_accounts` creates level-2 parties (`found_via: order system`, `distributor`, `account`, `phone`, channel `sim`)
and an edge distributor → account (`sender_qty = qty_shipped`) and books the distributor's claim
(forwards to every account, source `order system`). `read_inventory` (per account, once per sim day until the
account reconciles) records `order_system_in_stock` on the account.

**Contract and repair**: adapters (`adapters`, partial unique index: one `status: active` per order system):
`{_id: "north:v1", order system, version, status (active|retired|candidate|rejected), replaces, source ("builtin"|"model"|"rules"|"episode ep-…"),
expected_fields{accounts:[…], holds:[…]}, mapping{canonical: order system path}, paths{…}, steps{place_hold:[…]}, canary{account, field, ledger_value, order_system_value, passed}}`.
Detect (code, not opinion): a response missing an expected field, or a 410. Contain: pause the order system
(`adapters` status of the active one stays; `order_system_paused` on the distributor party), every `intent`/`sent`
op of that order system → `needs_reverify`; verified ops untouched. Repair: `Repairer.propose(failure_text, error_body,
openapi, old_adapter) -> candidate` — `ClaudeRepairer` (`agent/repair_prompt.md`, needs `ANTHROPIC_API_KEY`),
`RulesRepairer` (deterministic schema/description matching over openapi.json; used when no key), `ScriptedRepairer` (tests).
Verify: the canary — a count the ledger holds as corroborated for one account (`edges.corroborated_qty`) must
equal the candidate's mapped `qty_shipped` read from the order system. Fail → candidate `rejected`, order system stays paused (test 19).
Activate in one transaction (old `retired`, new `active`). Retry only `needs_reverify` ops. Remember: an episode
`{kind: "adapter", system_vendor, failure_text, mapping, steps, expected_fields, embedding(failure_text)}`; the
second order system's failure searches `kind: adapter` episodes by failure text, takes the mapping, runs the same canary,
activates with 0 model calls (`source: "episode <id>"`). Vector index adds filter field `kind`; party retrieval
filters `kind: "contact"` (history episodes are tagged `kind: contact` at load).

## 8. Scenario and profiles (sim)

`scenarios/generate.py --seed 20261114` writes `scenarios/recall_long.json` (asserts conservation first).
Batch `B-4471`, 2,400 panels: slice 240 (Harbour Fire 40, Queen St Alarm 120, Lakeside Systems 80 — the
recall_1 parties with their recall_1 scripts, Corner Properties in the directory), Distributor North 1,300,
Distributor East 860 (all forwarded to their accounts). 21 accounts (level 2) in the order systems, 14 level-3
parties (Corner Properties + 13) named by forwarders' claims, one chain three hops deep. 36 simulated parties
with profiles: straight 14, no_answer 5, callback 3, over_reporter 3, reviser 2, forwarder 6, short_receiver 2,
unreachable 1 (Queen St Alarm is a forwarder and Lakeside Systems straight, by their scripts). Every simulated
party has a ground truth (`on_hand, sold, returned, destroyed, forwards[{to, qty}]`) that conserves.
`schedule`: `order_system_change_at: {north: 81.5, east: 82.0}` (day 3, by hand in demo mode), rep report day 8.
`expected.final`: only the slice's 15 disputed panels open.

`providers/profiles.py`: `respond(profile, truth, context) -> Reply` where context = purpose, attempt number,
ladder step, sim time, told_qty, open questions; Reply = `{kind: answer|no_answer|callback|busy, claims:[tool bodies],
answers:[{question_id, value}], callback_at, say}`. The SimNetwork plays slice parties from scripts (unchanged)
and the others from profiles, pacing replies by the run's clock (a call lasts ~3 sim minutes), and drives
party-initiated events in sim time: revisers resubmit through the form 48 sim h after acceptance; a form link
delivered at ladder step 3 is answered per profile; the rep reports for the unreachable party at day 8 09:00 after
escalation (source `rep`).

## 9. Ledger additions

Claims accept `forwards: [{to_name | to_party_id, qty}]` (the single `forwarded_qty`/`forwarded_to_name`
pair still works and is one entry). `forwarded` = Σ qty; one edge per destination; a later claim may change
quantities and add destinations but not drop one that has an edge (`forward_changed`). Sources: `voice, sim,
form, order system, rep`. Every accepted claim has an evidence event in `tool_events` (kind `tool_call`, `form`, `order system`, `rep`).
`ledger/invariants.py`: `check(db, recall_id, order systems) -> {at, passed:[4 bools], details}`:
1 no party has two live attempts; 2 no idempotency key has two holds in any order system's `/_log/holds`;
3 every accepted claim has a tool event; 4 mass balance = units_total. A dispute or a receipt sets the
counterpart party's `next_action_at` to now (so the ladder wakes it) — done inside the claim transaction.

## 10. API additions

`POST /tools/submit_answer {attempt_id, party_id, question_id, value}` (tool secret) → `answers.<qid>`,
`$pull open_questions`, trace + feed. `POST /admin/questions {text, answer_type, applies_to{level:[…]}}` → insert,
`$addToSet open_questions` on matching parties without an answer, `next_action_at = now`. `POST /admin/clock
{seconds_per_sim_hour?, action: start|pause|resume}`. `POST /admin/release {recall_id, party_id}` now also clears
`hold_for_demo` (and keeps releasing a held sim reply). `POST /admin/order-system {order system, action: switch_v2|fault, fault?}`
(proxies to the mock order system's admin). `POST /admin/supervisor {paused}`. `POST /admin/saboteur {on, seconds}`.
`POST /admin/recalls` accepts `scenario: "recall_long"` and `{seconds_per_sim_hour, hold_slice}`.
`levela/api/scheduler.py`: a background thread (started by `create_app` unless `settings.scheduler_enabled` is false) that,
once per simulated hour, runs the invariants and writes `runs.last_invariant_check` (failure → `invariant_failed`,
feed `invariants` level bad, `planner_wake`), and fires scheduled events (`order_system_change_at`) by calling the order system admin.

## 11. Supervisor (`scripts/supervisor.py`)

Starts `python -m levela worker --workers N`, kills it hard (`Popen.kill()`, i.e. SIGKILL / TerminateProcess)
after a random 2–5 real minutes (`--min-s/--max-s`; tests use seconds), increments `runs.kills`, feed
"supervisor: worker killed (9)", restarts it. Honours `runs.supervisor.paused`. Stops (and leaves the worker
running, killing nothing more) when `runs.invariant_failed` is true.

## 12. Wall additions (`/wall/state`)

Adds `run: {sim_now, sim_label ("d6 14:00"), day, days_total 10, real_elapsed_s, seconds_per_sim_hour, paused,
parties{total, at_start, found}, checked[a,b], steps, kills, duplicates, tokens_per_decision, invariants{at_label,
passed[4], ok}, supervisor{paused}, saboteur{on}}`, `systems: [{system: "phone", attempts, accepted_claims},
{system: "North orders", adapter_version, adapter_source, ops_verified, paused}, …]`, `workers: [{id, party_id,
since_s, kind}]` (live leases), `calls_in_flight: {real, simulated}`. Feed lines gain `system`
(`phone|North orders|East orders|ledger|worker|supervisor|invariants|gate|atlas|planner|memory|form`) and `sim_label`.

## 13. Tests 16–32 (pass conditions are the spec's; all on the fast clock, 0.05 s per sim hour, fake voice)

16 long run (40 parties, 10 sim days, < 3 min on CI's local MongoDB; ends with only the slice's 15 open; balance every sim hour) ·
17 hold took effect · 18 order system change · 19 bad mapping refused · 20 adapter memory · 21 durable timers ·
22 random kills (10, invariants hold) · 23 claim race (8 workers, one wins, epoch +1, DB clock) · 24 stale worker ·
25 wrong party · 26 wrong tool · 27 double send · 28 false success · 29 loop · 30 last slot · 31 eight workers ·
32 question fan-out. Heavy tests are `@pytest.mark.slow`; CI runs them; local runs against Atlas may skip them with `-m "not slow"`.
