# All Clear

An agent that runs a tin recall to completion. One batch of 2,400 tins went to
about 40 parties and distributors; All Clear contacts every party by phone,
through a party form and through two distributor order systems, books every count in
MongoDB Atlas behind validators and transactions, finds the parties no shipment
list named, survives being killed at random, adapts when an order system changes its
contract, and refuses to close the recall until every tin is explained. The
database, not the model, decides what counts.

Hackathon track: **Finish the Job**. The long run plays 10 simulated days in
two real hours, more than 300 steps, 35 parties found at run time, ten or
more kills survived, and zero duplicate effects.

## What was built today

Everything in this directory except the seed kit (below) was written during
the event:

- **Ledger in Atlas** (`levela/ledger`, `levela/db`): one transaction per
  claim, transfer edges with two claims each, disputes, multi-destination
  forwards, the closing gate, the mass balance, four run invariants, a rebuild
  of the ledger from the append-only claims log.
- **Engine** (`levela/orchestrator`): a pool of workers that lease the next due
  party from Atlas on the database's clock, durable timers, the five-step
  contact ladder, call slots for real phones, crash recovery from evidence
  (tool events, Twilio's call list) before any redial, failpoints, and a planner
  that ranks work and adds questions but cannot touch the ledger.
- **Harness** (`levela/harness`): every action of every worker goes through one
  tool gate (lease, allowlist, budget, idempotency key, then the provider),
  fixed-size briefs, results checked against facts, traces and replay, and a
  saboteur that breaks one rule at a time.
- **Systems** (`levela/mock`, `levela/systems`): a mock distributor order system per
  distributor (v1, then v2 with renamed fields and a confirm step, three
  faults), order system operations with `sent` and `verified` kept apart, contract
  checks in code, repair by model or rules, a canary against the ledger, and
  adapter memory in Vector Search.
- **Simulator** (`levela/providers`, `scenarios/generate.py`): the 36 profiled
  parties of the long run, the scripted four-party slice, the simulated clock.
- **API and voice** (`levela/api`, `agent/`): the ElevenLabs agent as code
  (prompt, three webhook tools, provisioning), the post-call webhook, the party
  form, admin controls, the scheduler, questions as data.
- **Wall** (`levela/wall`): the display the room watches, polling Atlas-backed
  state once a second.
- **Ops**: the scripts, the Makefile, the deploy files, CI and these docs.

## Real and simulated

| Real | Simulated |
|---|---|
| MongoDB Atlas (sandbox cluster) and every validator, index and transaction refusal | The clock: 10 days played in 120 minutes (`levela/clock.py`) |
| Two outbound phone calls, ElevenLabs Agents over Twilio | 36 of the 40 parties, each with a behaviour profile |
| The tool and post-call webhooks, on a public HTTPS URL | The two distributor order systems: mock processes written today |
| Every kill of the worker (SIGKILL or the failpoint's `os._exit(137)`) | The shipment records and the 16 past recalls (the seed kit) |

**Seeded for the demo:** the shipment records, the 16 past recalls in
`data/history/` (2,325 documents), the scenarios in `scenarios/` and the
simulated parties' behaviour are synthetic. Every party, brand, batch and person
is invented, and every phone number in the data is in the 555-0100 to 555-0199
block, which cannot ring a real line.

## Pre-existing tools used

| Tool | Used for |
|---|---|
| The All Clear seed kit (`seedkit/`, `data/history/`), written on Nov 13 before the event and disclosed as such | Synthetic history, the spec's Atlas rules, dry run and preflight |
| MongoDB Atlas, PyMongo | System of record: validators, transactions, change streams, `$graphLookup`, `$$NOW` leases, Vector Search |
| Voyage AI embeddings through Atlas (local hash embedder as fallback) | Episode vectors for memory |
| ElevenLabs Agents | The voice agent and its webhook tools |
| Twilio | The phone number; the Calls list for in-doubt calls |
| Anthropic Claude (optional, `PLANNER=claude`, `ANTHROPIC_API_KEY`) | The decider, the planner and the order system repairer; deterministic rules without a key |
| FastAPI, Uvicorn, Pydantic, httpx, segno | The API, its clients, the form's QR codes |
| pytest, GitHub Actions | Tests and CI |
| Render, Fly.io or Railway; Cloudflare quick tunnels, ngrok | Hosting the API; tunnels as backup |

## Run it

Python 3.11 or newer, from `level-a/`. Copy `.env.example` to `.env` and fill
it in (Atlas URI, shared secrets, ElevenLabs and Twilio for real calls). An
empty shared secret refuses every request that needs it; for a run that stays
on the laptop, `LEVELA_DEV=1` in `.env` fills empty ones with public
development values instead (never on a host).

```sh
pip install -r requirements-dev.txt
make setup-db            # python -m levela setup-db: collections, validators, indexes
make seed                # python -m levela load-history: the synthetic history and the vector index (once)
make preflight           # python -m levela preflight
```

Each process in its own terminal (the `Makefile` targets are one CLI call each):

| Process | Command |
|---|---|
| API (tools, webhooks, form, admin, wall, scheduler) | `make api` (`python -m levela api --host 127.0.0.1 --port 8000`) |
| Mock order systems North and East | `make order systems` (`bash scripts/start_order_systems.sh`; Windows `scripts\start_order_systems.ps1`) |
| Worker pool | `make worker` (`python -m levela worker --workers 4`) |
| Worker under random kills | `make supervisor` (`python scripts/supervisor.py --workers 4`) |
| Voice agent on ElevenLabs | `make provision` (`python scripts/provision_agent.py`), then `python scripts/call_once.py --watch` |
| Start the long run, demo mode | `make demo` (`python scripts/demo.py start --scenario recall_long --mode demo`) |
| Start the four-party slice alone | `make demo-4` (`python scripts/demo.py start --scenario recall_1 --mode demo`) |
| The whole run unattended | `make long-run` (sim mode under random kills) |
| The kill beat | `make kill-demo` (`bash scripts/kill_demo.sh`; Windows `scripts\kill_demo.ps1`) |
| One beat of the demo, checked against the wall | `python scripts/demo.py <beat>`; `python scripts/demo.py beats` lists them |
| Wall | `http://127.0.0.1:8000/wall` (operator buttons: `/wall?key=<ADMIN_TOKEN>`) |
| Tests | `make test` (fast), `make test-slow` (long run, random kills, eight workers) |

Deploying the API: `deploy/README.md`. The demo itself: `docs/RUNBOOK.md`.
The one-minute video: `scripts/video_shots.md`.

## Architecture

```text
   judge's phone   teammate's phone
         \             /
          Twilio  <->  ElevenLabs Agents (agent/: prompt, submit_count,
                         |                 submit_answer, request_callback)
                         | tool calls, post-call webhook (HTTPS, signed)
                         v
  wall  <--- 1 Hz --- API server (FastAPI, public host) ---- admin proxy ----+
  (browser)           tools | hooks | form | admin | wall                    |
                      simulator: 36 profiled parties, posts through the API   |
                      scheduler: 4 invariants every simulated hour           |
                         |                                                   v
                         | one transaction per claim         mock North orders :8101
                         v                                    mock East orders  :8102
   +--------------------- MongoDB Atlas ----------------------+       ^
   | ledger: recalls parties edges claims (validators)         |       |
   | attempts operations adapters capacity (unique indexes)   |       | holds, accounts
   | runs task_runs traces questions feed                     |       | (idempotency keys,
   | episodes + Vector Search (contacts, order system repairs)       |       |  read-back verify)
   +-----------------------------^----------------------------+       |
                                 | lease due party ($$NOW, epoch)    |
                                 | release with its next timer       |
              worker pool w1..wN + planner  --- every action through ---+
                       ^                     the tool gate
                       | SIGKILL at random, restart
                   supervisor
```

Work flows one way: events wake the planner, the planner ranks the work, workers
lease it, the gate checks every action, and only a read-back or an accepted
claim decides what counts.

## The rules Atlas enforces

1. **Party conservation** (validator on `parties`): every bucket is a
   non-negative integer and `shipped_in = on_hand + returned + destroyed + sold
   + forwarded + unresolved`. Over-reporting would need a negative
   `unresolved`, so Atlas rejects the write; the raw error goes on the wall.
2. **Edge sanity** (validator on `edges`): `corroborated_qty <= sender_qty`.
3. **Close gate** (validator on `recalls`): `status: closed` only when
   `open_units` is 0. The close button cannot override it.
4. **One live attempt per party** (partial unique index on `attempts` while
   `intent`, `dialed` or `connected`).
5. **Verified means read back** (validator on `operations`): `state: verified`
   needs a non-null `readback_at` and `readback_value`.
6. **One effect per idempotency key** (unique index on
   `operations.idempotency_key`, and on attempts' keys).
7. **One active adapter per order system** (partial unique index on `adapters`).
8. **The due query reads only what is due** (index on `parties.next_action_at`);
   leases compare against the server's `$$NOW`, never a worker's clock.

## Test map

Tests 1 to 7, 10, 11 and 16 to 32 run in pytest on the fake voice provider
(`make test`, `make test-slow`; CI runs both on a MongoDB 8.0 replica set).
Tests 8, 9 and 14 need a real phone; 12, 13 and 15 are also checked by hand.

| # | Test | Passes when | Proven by |
|---|---|---|---|
| 1 | Atlas basics | Transactions commit and abort, a validator rejects an insert and an update, a change stream sees an insert, the vector index is queryable | `tests/test_atlas_basics.py`; `make dryrun`; `make preflight` (vector index, on Atlas) |
| 2 | Party validator | 60 on hand for 40 shipped and a negative bucket are refused; valid counts are accepted | `tests/test_validator.py` |
| 3 | Claim posting | One claim and one party update; nothing on a mid-transaction failure; a duplicate posts once | `tests/test_claims.py` |
| 4 | Transfers | 40 forwarded, 25 received: 25 corroborated, 15 disputed, balance 240 after every write | `tests/test_transfers.py` |
| 5 | Close gate | Refused by Atlas with a dispute open; closes after the correction; metrics exact | `tests/test_reconciler.py` |
| 6 | One live attempt | The second attempt raises a duplicate-key error | `tests/test_attempts.py` |
| 7 | Recovery | Each failpoint, kill, restart: one call per party in every case | `tests/test_recovery.py` |
| 8 | Voice loop | Rejection spoken within about three seconds; numbers read back; four of five runs clean | By hand: `scripts/call_once.py`, `agent/README.md` ("Measurements"); the agent files by `tests/test_agent_config.py` |
| 9 | Post-call webhook | The attempt completes and the transcript is stored | By hand with a real call; the handler by `tests/test_hooks.py` |
| 10 | Simulated end to end | Recall 1 all simulated: metrics exact, one dispute of 15, not closable | `tests/e2e_sim.py` |
| 11 | Memory | With memory, Corner Mart' attempt starts before Queen St Grocer's claim; without, after | `tests/test_memory.py` |
| 12 | Wall display | New party within two seconds, the rejection in the feed, metrics match | By hand during test 10; assets by `tests/test_wall_assets.py` |
| 13 | Fallback form | The same rejection and acceptance as the voice path | `tests/test_form.py`; by hand on a phone |
| 14 | Kill demo with a real call | The phone rings once; the claim lands while the worker is down; adopted on restart | By hand: `scripts/kill_demo.sh`, checked by `scripts/demo.py kill` |
| 15 | Full rehearsal | Three runs under three minutes, every beat landing, one on a hotspot | By hand: `docs/RUNBOOK.md`, `scripts/demo.py` |
| 16 | Long run | 40 parties, 10 simulated days, under 3 minutes; only the slice's 15 disputed tins open; balance every simulated hour | `tests/test_long_run.py` |
| 17 | Hold took effect | Accepted then dropped: stays `sent`, one resend with the same key verifies, one hold in the order system's log | `tests/test_order_system_hold.py` |
| 18 | Order system change | Only `intent` and `sent` operations flagged; the repair passes the canary; only those retry | `tests/test_order_system_change.py` |
| 19 | Bad mapping refused | A wrong mapping fails the canary; nothing activated; the order system stays paused | `tests/test_order_system_change.py` |
| 20 | Adapter memory | East's failure retrieves the episode; adapter v2 active with 0 model calls | `tests/test_adapter_memory.py` |
| 21 | Durable timers | Killed with 5 timers pending: each fires once; the restart reads only due and in-doubt documents | `tests/test_timers.py` |
| 22 | Random kills | 10 kills in a fast long run; all four invariants hold; the run ends as in test 16 | `tests/test_random_kills.py` |
| 23 | Claim race | 8 workers, one due party: one claim wins, the epoch rises by 1, expiry on the database clock | `tests/test_claim.py` |
| 24 | Stale worker | A worker paused past its lease writes nothing; the second worker's result stands | `tests/test_claim.py` |
| 25 | Wrong party | The saboteur calls a party it did not lease: refused on `lease` | `tests/test_gate.py` |
| 26 | Wrong tool | A contact subagent calls `close_recall`: refused on `allowlist` | `tests/test_gate.py` |
| 27 | Double send | One attempt exists; the second call returns the stored outcome; one hold in the order system's log | `tests/test_gate.py` |
| 28 | False success | `verified` without a read-back rejected by Atlas; `claim_accepted` without a claim rejected as `unsupported_result` | `tests/test_gate.py` |
| 29 | Loop | Stopped at its budget; lease released; `budget_exceeded` episode; the party due again | `tests/test_saboteur.py` |
| 30 | Last slot | Two workers race for the last call slot: one dials, the other finds none | `tests/test_capacity.py` |
| 31 | Eight workers | Test 22 with 8 workers: invariants hold, duplicates 0 | `tests/test_random_kills.py` |
| 32 | Question fan-out | Only parties without an answer become due; reconciled parties asked the question only; no count asked again | `tests/test_questions.py` |

Every other file under `tests/` supports these: the schema and the clock, the
ladder and the worker, the harness, the order system, the simulator and its
profiles, the API, the agent files, the scripts and these docs
(`tests/test_docs.py` checks this table names a file for every test).

## More

- `docs/DESIGN.md` and `docs/DESIGN_V2.md`: the build contracts.
- `../docs/Level_A_build_spec_and_test_plan.md`: the product spec and test plan.
- `docs/RUNBOOK.md`: the operator's runbook for the demo.
- `deploy/README.md`: hosting the API, tunnels, re-provisioning.
- `agent/README.md`: the voice agent as code.
- `seedkit/README.md`: the seed kit and the synthetic history.
