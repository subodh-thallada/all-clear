# All Clear

"Lie to it, kill it, break the system under it: our agent still won't say 'all clear' until the database shows every recalled tin off every shelf."

All Clear (codename Level A in the repo) is a recall agent for a food company
that sells through distributors and stores. It finds every tin of a recalled
batch, has it pulled from sale, and keeps the recall open until the store's own
records show it is gone, with the evidence in MongoDB Atlas. The demo recalls
an invented product: Maple Farm milk powder 800 g, batch B-4471 (faulty lid
seal), 2,400 tins, about 40 parties, 10 simulated days. Track: **Finish the Job**.

## Built today

Everything in this directory was written during the event. The seed kit
written on Nov 13 is not used and not in the repository (spec "First checks on
the sandbox": not without a yes).

- **Ledger in Atlas** (`levela/ledger`, `levela/db`): six buckets per party
  (`sold`, `on_shelf`, `held`, `returned`, `forwarded`, `unresolved`) that must
  add up to what was shipped in; `held` and `sold` need evidence (the store's
  own records, or the batch code read from a tin plus a till test); one
  transaction per claim; transfers checked against both sides; the close gate;
  the mass balance; four run invariants; a rebuild from the append-only claims.
- **Engine** (`levela/orchestrator`): workers that lease the next due party on
  the database's clock, durable timers, the contact ladder, call slots for
  real phones, recovery from evidence (tool events, Twilio's call list) before
  any redial, failpoints, and a planner that ranks work and adds questions but
  cannot touch the ledger.
- **Harness** (`levela/harness`, `levela/api/audit.py`): every action goes
  through one tool gate (lease, allowlist, budget, idempotency key, then the
  provider); typed results checked against facts; traces; a saboteur that
  breaks one rule at a time; pause and resume; the audit query for one party.
- **Distributors' order systems** (`levela/mock`, `levela/systems`): a mock
  order system per distributor (v1, then v2 with renamed fields and a confirm
  step), holds with `sent` and `verified` kept apart, contract checks in code,
  repair by model or rules, a canary against the ledger, adapter memory.
- **Store records** (`levela/monitoring`, `levela/db/schema_store.py`,
  `levela/api/store.py`): complaints and their classification, store events
  (`baseline`, `sale`, `blocked_sale`, `quarantine`, `customer_return`,
  `delivery`) booked with the ledger change in one transaction, signals and
  the human approval gate, the plant call (three questions as data; no block
  until one till has verified the barcode), and the SIMULATED store page
  (`/store/{id}`: Scan a tin at the till, Move one tin to quarantine,
  Customer return), and the store script that posts the other connected
  stores' records (`scripts/store_script.py`).
- **Sub-recall, agent to agent** (`levela/node`, `levela/db/schema_node.py`,
  `levela/api/node.py`, `scenarios/summit.py`): Summit Foods' node takes a
  sub-recall once per key, matches its own item, and reports signed counts
  that our database checks like any claim (HMAC `X-AllClear-Signature`); the
  quiet-agent ladder; buyers told once each; its buyer list stays home
  (`scripts/scan_names.py` checks our database for it). `make summit`
  (`scripts/start_summit.sh`) starts the second node.
- **Simulator** (`levela/providers`, `scenarios/generate.py`): 36 profiled
  parties, the scripted four-party slice, the simulated clock.
- **API and voice** (`levela/api`, `agent/`): the ElevenLabs agent as code
  (prompts for store, plant and buyer, the webhook tools, provisioning), the
  post-call webhook, the party form, admin controls, the scheduler, questions
  as data.
- **Governed memory** (`levela/memory`): episodes scoped to a party, expiring
  and invalidated episodes never retrieved, retrieval by kind.
- **Ops** (this workstream): the scripts, the Makefile, the deploy files, CI
  and these docs.

## Real and simulated

| | What |
|---|---|
| Real | MongoDB Atlas and every refusal it makes (validators, unique indexes, the close gate) |
| Real | The phone calls: ElevenLabs Agents over Twilio (the plant, the judge as Harbour Market, a teammate as Corner Mart) |
| Real | The webhook server on a public URL (tool calls, the post-call webhook, the form, the store page) |
| Real | Every kill of a worker (the supervisor's SIGKILL, or the failpoint's `os._exit(137)`) |
| Simulated | The clock: 10 simulated days played in about two real hours (`levela/clock.py`) |
| Simulated | 36 parties, each with a behaviour profile |
| Simulated | The distributors' order systems: mock processes written today, one per distributor |
| Simulated | The shipment records |
| Simulated | The stores' tills and stock records: the store page says SIMULATED across the top, and a script posts the other stores' events |
| Simulated | Summit Foods' node is a second copy of the same code, with its own database on the same cluster |

The product, the barcode, the batch, the companies and every person are
invented, and every phone number in the data is in the 555-0100 to 555-0199
block, which cannot ring a real line. Because the product is invented,
nothing here says or implies that anyone was hurt by it.

## Run it

Python 3.11 or newer, from `level-a/`. Copy `.env.example` to `.env` and fill
it in (Atlas URI, shared secrets, ElevenLabs and Twilio for real calls). An
empty shared secret refuses every request that needs it; for a run that stays
on the laptop, `LEVELA_DEV=1` in `.env` fills empty ones with public
development values instead (never on a host). No API key is needed for the
tests: the decider, the planner, the repairer and the embedder have
deterministic fallbacks.

```sh
pip install -r requirements-dev.txt
make setup-db            # python -m levela setup-db: collections, validators, indexes
make preflight           # python -m levela preflight
```

Each process in its own terminal (each `Makefile` target is one CLI call):

| Process | Command |
|---|---|
| API (tools, webhooks, form, store page, admin, screen, scheduler) | `make api` (`python -m levela api --host 127.0.0.1 --port 8000`) |
| Workers | `make worker` (`python -m levela worker --workers 4`) |
| Workers under random kills | `make supervisor` (`python scripts/supervisor.py --workers 4`) |
| Mock order systems, North and East | `make order-systems` (`bash scripts/start_order_systems.sh`; Windows `scripts\start_order_systems.ps1`) |
| Summit Foods' node: a second API, two workers, its own database | `make summit` (`bash scripts/start_summit.sh`) |
| The store page (SIMULATED till and stock record) | `http://127.0.0.1:8000/store/<id>` |
| The other connected stores' records | `make store-script` (`python scripts/store_script.py run`: the connectors and the day-0 complaints; `STORE_ARGS=removals` later, `STORE_ARGS=approve` is a person's approval) |
| Voice agent on ElevenLabs | `make provision` (`python scripts/provision_agent.py`), then `python scripts/call_once.py --watch` |
| Start the long run, demo mode | `make demo` (`python scripts/demo.py start --scenario recall_long --mode demo`) |
| Start the four-party slice alone | `make demo-4` (`python scripts/demo.py start --scenario recall_1 --mode demo`) |
| The whole run unattended | `make long-run` (sim mode under random kills) |
| The kill beat | `make kill-demo` (`bash scripts/kill_demo.sh`; Windows `scripts\kill_demo.ps1`) |
| One beat of the demo, checked against the screen | `python scripts/demo.py <beat>`; `python scripts/demo.py beats` lists them |
| The screen | `http://127.0.0.1:8000/wall` (operator buttons: `/wall?key=<ADMIN_TOKEN>`) |
| Tests | `make test` (fast), `make test-slow` (long run, random kills, eight workers) |

Deploying the API: `deploy/README.md`. The demo itself:
`docs/RUNBOOK.md`. The one-minute video: `scripts/video_shots.md`.

## Spec tests

The spec (`../docs/All_Clear_build_spec_and_test_plan.md`) numbers 49 tests.
The automated ones run in pytest on the fake voice provider (`make test`,
`make test-slow`; CI runs both on a MongoDB 8.0 replica set; tests marked
`atlas` need Vector Search and skip on a plain mongod). Where an older plan's
number is still in a file, the row says so.

| # | Test | Where it lives |
|---|---|---|
| 1 | Atlas basics | `tests/test_atlas_basics.py`; `make preflight` on the sandbox |
| 2 | Party validator | `tests/test_validator.py` |
| 3 | Claim posting | `tests/test_claims.py` |
| 4 | Transfers | `tests/test_transfers.py` |
| 5 | Close gate | `tests/test_reconciler.py` |
| 6 | One live attempt | `tests/test_attempts.py` |
| 7 | Recovery | `tests/test_worker.py`, `tests/test_recovery.py` |
| 8 | Voice loop | Needs a phone: `scripts/call_once.py`, `agent/README.md`; the agent files by `tests/test_agent_config.py` |
| 9 | Post-call webhook | Needs a phone for the real call; the handler by `tests/test_hooks.py` |
| 10 | Simulated end to end | `tests/e2e_sim.py` |
| 11 | Memory | `tests/test_memory.py` |
| 12 | Screen | By hand during test 10: `docs/RUNBOOK.md` |
| 13 | Fallback form | `tests/test_form.py`; by hand on a phone |
| 14 | Kill demo with a real call | Needs a phone: `scripts/kill_demo.sh`, checked by `scripts/demo.py` (`kill`) |
| 15 | Full rehearsal | By hand: `docs/RUNBOOK.md`, `scripts/demo.py` |
| 16 | Long run | `tests/test_long_run.py` |
| 17 | Hold took effect | `tests/test_order_system_hold.py` |
| 18 | Order-system change | `tests/test_order_system_change.py` |
| 19 | Bad mapping refused | `tests/test_order_system_change.py` |
| 20 | Adapter memory | `tests/test_adapter_memory.py` |
| 21 | Durable timers | `tests/test_timers.py` |
| 22 | Random kills | `tests/test_random_kills.py` |
| 23 | Leases | `tests/test_random_kills.py` (`test_22_*`: four workers on the fast long run, no two live attempts) |
| 24 | Adoption | `tests/test_worker.py`, `tests/test_recovery.py` (numbered 7 there) |
| 25 | Race | `tests/test_claim.py` |
| 26 | Stale worker | `tests/test_claim.py` |
| 27 | Wrong party | `tests/test_gate.py` |
| 28 | Wrong tool | `tests/test_gate.py` |
| 29 | Double send | `tests/test_gate.py` |
| 30 | False success | `tests/test_gate.py` |
| 31 | Loop | `tests/test_saboteur.py` |
| 32 | Last slot | `tests/test_capacity.py` |
| 33 | Question fan-out | `tests/test_questions.py` |
| 34 | Signal rule | `tests/test_signals.py` |
| 35 | Approval gate | `tests/test_signals.py` |
| 36 | Removal by the store's own record | `tests/test_store_records.py` |
| 37 | The till outranks the phone | `tests/test_store_records.py` |
| 38 | Diagnosis | `tests/test_classify.py` (complaint classification) |
| 39 | Plant call and barcode check | `tests/test_plant.py`, `tests/test_questions.py` (the plant's three answers), `tests/test_store_records.py` (no block before the barcode is verified) |
| 40 | Stop mid-run | `tests/test_pause_audit.py` |
| 41 | Audit reconstruction | `tests/test_pause_audit.py` |
| 42 | Governed memory | `tests/test_governed_memory.py` |
| 43 | Handoff once | `tests/test_subrecall.py` |
| 44 | Match and book | `tests/test_subrecall.py` |
| 45 | The till outranks the staff | `tests/test_subrecall.py` |
| 46 | Conservation, evidence, replay and signature | `tests/test_subrecall.py` |
| 47 | Quiet agent | `tests/test_subrecall.py` |
| 48 | The buyer list stays home | `tests/test_scan_names.py`; `scripts/scan_names.py` after a full run (`make scan-names`) |
| 49 | Buyers told | `tests/test_subrecall.py` |

Test 39's name in the spec is still the earlier version's; translated with
the spec's "Changed from" table, it is the plant call and the barcode check
before blocks go out.
Every other file under `tests/` supports these: the schema and the clock, the
ladder and the worker, the harness, the order system, the simulator and its
profiles, the API, the agent files, the scripts and these docs
(`tests/test_docs.py` checks this table against the spec).

## Architecture

```text
 plant's, judge's, teammate's phones
            |
         Twilio  <->  ElevenLabs Agents (agent/: prompts, submit_count, submit_answer)
                        | tool calls, post-call webhook (HTTPS, signed)
                        v
 screen <-- 1 Hz -- API server (FastAPI, public host) ------ admin proxy -------+
 store page (SIMULATED)  tools | hooks | form | store | complaints | admin      |
   + store script  --->  POST /stores/{id}/events (event + ledger: one txn)     |
                         simulator: 36 profiled parties, posts through the API  v
                         scheduler: 4 invariants every simulated hour   mock North orders :8101
                         planner: priorities and questions only         mock East orders  :8102
                        | one transaction per claim                          ^
                        v                                                    | holds, accounts
  +------------------------ MongoDB Atlas -----------------------------+      | (idempotency keys,
  | ledger: recalls parties edges claims (validators)                  |      |  read-back verify)
  | store records: complaints store_systems store_events signals       |      |
  | attempts operations adapters capacity (unique indexes)             |      |
  | runs task_runs traces questions feed; links buyers                 |      |
  | episodes + Vector Search (contacts, complaints, order system fixes)|      |
  +-------------------------------^------------------------------------+      |
                                  | lease the due party ($$NOW, epoch)        |
               four workers  -----+--- every action through the tool gate ----+
                  ^      \
   supervisor: SIGKILL    \ signed HTTP (X-AllClear-Signature), never the other database
   at random, restart      v
                    Summit Foods' node: its own API, two workers, its own database
```

Work flows one way: events wake the planner, the planner ranks the work,
workers lease it, the gate checks every action, and only a read-back, an
accepted claim or the store's own record decides what counts. No agent
framework: the orchestrator is a plain state machine, because the correctness
rules live in the database.

## The rules Atlas enforces

1. **Party conservation** (validator on `parties`): every bucket is a
   non-negative integer and `shipped_in = sold + on_shelf + held + returned +
   forwarded + unresolved`. Over-reporting would need a negative
   `unresolved`, so Atlas rejects the write; the raw error goes on the screen.
2. **Removal needs evidence** (validator on `parties`, insert and update):
   `held <= held_by_system`, or the batch code read from a tin equals the
   recall's and the till test refused the sale; `sold <= sold_by_till`, or the
   count was read from the till; `on_shelf >= system_on_shelf`, so a connected
   store's stock record outranks the phone.
3. **Edge sanity** (validator on `edges`): `corroborated_qty <= sender_qty`.
4. **Close gate** (validator on `recalls`): `status: closed` only when
   `open_units` is 0. The close button cannot override it, and the refusal
   carries the headline: `NOT ALL CLEAR: n tins open (...)`.
5. **One live attempt per party** (partial unique index on `attempts`).
6. **Verified means read back** (validator on `operations`): `verified` needs
   a read-back.
7. **One effect per idempotency key**, **one active adapter per order system**,
   **one store event per event id** and **one open signal per batch** (unique
   and partial unique indexes).
8. **A person approves a recall** (validator on `signals`): `approved` needs
   `approved_by`; no agent's tool list contains approve.

## Pre-existing tools used

| Tool | Used for |
|---|---|
| MongoDB Atlas (the provided sandbox), PyMongo | System of record: validators, transactions, change streams, `$graphLookup`, `$$NOW` leases, Vector Search |
| ElevenLabs Agents | The voice agent and its webhook tools |
| Twilio | The phone number; the Calls list for in-doubt calls |
| Voyage AI embeddings through Atlas (local hash embedder as fallback) | Episode vectors for memory |
| Anthropic Claude (optional, `ANTHROPIC_API_KEY`) | The decider, the planner, the order system repairer and complaint classification; deterministic rules without a key |
| FastAPI, Uvicorn, Pydantic, httpx, segno | The API, its clients, the form's QR codes |
| pytest, GitHub Actions | Tests and CI |
| Render, Fly.io or Railway; Cloudflare quick tunnels, ngrok | Hosting the API; tunnels as backup |

Not used: the seed kit written on Nov 13 (no yes was given to open it), any
agent framework, image analysis.

## Prior art

Recall software, recall voice agents for car dealers and recall-agent
prototypes exist. We found none combining these: calls down a supply chain,
two parties checked against each other in the database, a store's own records
as the proof of removal, survival of kills, and repair of a changed system. A
different product is a different recall document on the same engine.

## More

- `../docs/All_Clear_build_spec_and_test_plan.md`: the product spec and test plan.
- `docs/BUILD_PLAN.md`: the builders' contract and the deviations from the spec.
- `docs/DESIGN.md` and `docs/DESIGN_V2.md`: the engine's earlier design notes.
- `docs/RUNBOOK.md`: the operator's runbook for the demo.
- `deploy/README.md`: hosting the API, tunnels, re-provisioning.
- `agent/README.md`: the voice agent as code.
