# All Clear: build spec and test plan

Nov 14, 2026 · @Caesar

## What it is

All Clear (codename Level A in the repo and the other project docs) is a supplier's recall tool. When a batch of fire alarm panels turns out to be faulty, it finds every panel in the batch and keeps the recall open until each one is fixed or returned, with evidence in Atlas. Track: Finish the Job.

- **One sentence:** "Lie to it, kill it, break the system under it: our agent still won't say 'all clear' until the database shows every faulty fire alarm panel found and fixed."
- **User:** the recall desk of a supplier of fire alarm panels.
- **General claim, said once at the end:** a different product is a different recall document on the same engine. It fits products that move through businesses you can contact, not products sold to anonymous consumers.

### The loop

1. **Signal.** Connected panels report their monthly tests. Three failures at three sites from one batch raise a signal.
2. **Trace.** One aggregation ties the failing serial numbers to a batch and to everyone who was shipped that batch.
3. **Approve.** The system proposes a recall and a person at the supplier approves it. No model can open one.
4. **Locate.** Workers phone installers and building owners, and place holds in distributors' order systems, until every panel has a holder.
5. **Fix.** Installers update the installed panels. The agent tracks each one and never gives repair instructions.
6. **Verify.** A panel counts as fixed only on evidence: its own passing test, or a version read off its display plus a sounder test.
7. **All clear.** Atlas refuses to close the recall while any panel is unlocated, disputed, or installed and not fixed.

Steps 1 to 3 and the panel's own test in step 6 are the live monitoring part. Monitoring is the trigger and the proof. The recall is the job.

### Scope

- **Scenario:** one batch (B-4471), 2,400 panels of an invented model (DEMO-7), about 40 parties over 10 simulated days. The run starts with 5 names on the shipment records and finds the other 35 as it goes.
- **Live slice:** 240 panels, three installers and one hidden building owner. It is built first, and it is the live part of the demo.
- **Real:** Atlas, the phone calls (ElevenLabs over Twilio), the webhook server on a public URL, every kill of a worker and every refusal by the database.
- **Simulated:** the other parties (behaviour profiles), the distributors' order systems (mocks built today), the clock, the shipment records, and the fire alarm panel (a web page labelled "simulated"). No real fire alarm equipment is touched.
- **Rules:** build in the provided Atlas Sandbox, public repo, new work only. The screen supports the demo and is not the product. The agent verifies and never advises anyone on repairing a fire system.

### Words used in this doc

| Word | Meaning |
| --- | --- |
| Batch | The recalled production batch: 2,400 panels |
| Party | Anyone who held panels from the batch: a distributor, an installer or a building owner. One document in `parties` |
| Distributor | Sells to installers. Answers through its order system |
| Installer | A fire alarm contractor. Holds stock and installs panels at buildings |
| Building owner | The owner or manager of buildings with installed panels |
| Connected panel | A panel that reports its tests to the supplier's service. 24 of the 2,400 |
| In stock, installed, fixed, returned, forwarded, unresolved | The six buckets a party's panels fall into. Installed means installed and not yet confirmed fixed |
| All clear | The recall is closed |

`parties` is the collection that the dispatch addendum and the wall spec call `stores`.

### Checks, stored on the recall

| Check | Question on the call | Rule |
| --- | --- | --- |
| Affected unit | "What model is printed on the front of the panel?" | Must be the recalled model |
| Still at risk | "What software version does the display show?" | A version in the affected list means not fixed |
| Fix took effect | "Press the test button. Did the sounder ring?" | `fixed` needs yes |

- Installers answer with counts and the names of the buildings they installed at. Building owners answer the three checks.
- Answers are closed: a model, version digits, yes or no.
- The recall document holds the model, the affected versions (2.3 and 2.4) and the fixed version (2.5). They are copied onto each party, so one validator can refuse `fixed` with an affected version.
- For a connected panel, only its own passing test can mark it fixed. A phone answer cannot override the panel.
- Demo data uses an invented model and versions.

### Real precedent

| Recall | Units | How affected units were identified | Source |
| --- | --- | --- | --- |
| Notifier control panels, 2008 | About 2,000 | Firmware 11.2.2 or 3.14.1 | [Fire Engineering](https://www.fireengineering.com/fire-safety/fire-alarm-control-panels-recalled-by-notifier-because-of-alert-failure/) |
| Fire-Lite MS-9600LS panels, 2010 | About 530 | Model used with an expander module | [CPSC](https://www.cpsc.gov/Recalls/2011/Fire-Alarm-Control-Panels-Recalled-by-Fire-Lite-Alarms-Due-to-Alert-Failure) |
| Simplex 4100U-NXP panels, 2011 | Not stated on the page read | Software revision 14.01, read from the panel's menu | [Justia recall list](https://recalls.justia.com/infants-and-children/fire-or-smoke-alarms/21-30) |
| Bosch control panels, 2012 | About 330 | Software 1.10 to 1.12, determined by installers | [CPSC](https://www.cpsc.gov/Recalls/2012/fire-control-panels-recalled-by-bosch-security-systems-corp-due-to-alarm-failure-posing) |
| Bull and Cygnus fire alarm systems (UK), August 2026 | Not stated | One production batch, reference 2410. Every unit in it is being replaced, including units that have not failed | [Fire Industry Association](https://www.fia.uk.com/news/product-recall-bull-and-cygnus-fire-alarm-detection-system-fads-2607-0120.html) |

- Each hazard was a system that could fail to alarm or report in a fire. The four notices that mention incidents report none, so never say anyone was hurt.
- Regular tests are already required. Ontario adopted the national inspection and testing standard on 1 January 2026, with set formats for annual reports and monthly logs ([Inspect Point summary](https://www.inspectpoint.com/ontario-fire-alarm-inspection-requirements-2026/)).

## Live monitoring

Monitoring starts the recall and proves the fix. It is three small pieces: a simulated panel that reports its tests, a rule that turns failed tests into a proposed recall, and a rule that lets only a panel's own passing test mark that panel fixed.

### The simulated panel

A web page served by the API at `/panel/{serial}`, with "SIMULATED" across the top. It stands in for a connected panel, because a real fire alarm system must not be touched.

| Control | What it does |
| --- | --- |
| Run test | Posts a test event for this serial. The result is FAIL while the panel's version is affected and its fault flag is set, otherwise PASS |
| Apply update | Sets the panel's software version to 2.5 |
| Display | Model, serial, software version and last test result. A teammate reads the version from here during a call |

```text
SIMULATED PANEL                      not a real fire alarm system
Model DEMO-7      Serial D7-4471-0193      Software 2.4
Site: Corner Properties, Tower 2
Last test: FAIL   sounder circuit   day 6 15:40
[ Run test ]   [ Apply update ]
```

### Collections

| Collection | Purpose | Key fields | Rules in Atlas |
| --- | --- | --- | --- |
| `panels` | One document per connected panel | `_id` (the serial), `batch_code`, `model`, `sw_version`, `site_label`, `party_id`, `fault`, `fixed_event` | The serial is the `_id`, so a serial exists once |
| `panel_events` | Append-only test results | `serial`, `test_id`, `kind` (monthly\_test, fault), `result` (pass, fail), `sw_version`, `at` | Validator on `kind` and `result`. Unique (`serial`, `test_id`), so a repeated post counts once |
| `signals` | One per batch under suspicion | `batch_code`, `events`, `sites`, `shipped_to`, `status` (proposed, approved, dismissed), `approved_by`, `recall_id` | Validator: `approved` needs `approved_by`. Partial unique index: one open signal per batch |

`panel_events` is a regular collection, not a time series collection. Time series collections cannot be written in a transaction and do not support change streams ([MongoDB limitations](https://www.mongodb.com/docs/manual/core/timeseries/timeseries-limitations/)). Here the event and the ledger change share one transaction.

### From failed tests to a recall

1. The panel posts to `POST /panels/{serial}/events`. The API inserts the event.
2. On a FAIL, the API runs the trace below.
3. Three or more failures at three or more sites create a `signals` document in `proposed`. A later failure for the same batch is added to it.
4. The operator page shows the signal with the list of who was shipped the batch.
5. A person presses Approve. The API sets `approved_by` and creates the recall, in one transaction.
6. No model is on this path. No agent has an approve tool, and a recall whose source is a signal must point to an approved one.

```text
panel_events   match    result is fail, within 30 simulated days
               lookup   panels on serial            gives batch_code and site_label
               group    by batch_code: the events and the distinct sites
               match    3 or more events and 3 or more sites
               lookup   shipments on batch_code     gives who was shipped the batch
```

```json
{
  "_id": "sig-01",
  "batch_code": "B-4471",
  "rule": "3 failed tests at 3 sites within 30 days",
  "events": ["pe-0007", "pe-0011", "pe-0012"],
  "sites": ["Maple Court", "Bayview Annex", "Spadina Works"],
  "shipped_to": ["Distributor North", "Distributor East", "Harbour Fire", "Queen St Alarm", "Lakeside Systems"],
  "status": "approved",
  "approved_by": "operator-1",
  "recall_id": "r-1"
}
```

### Proof that a fix took effect

- **A passing test moves one panel.** A PASS from a panel on version 2.5, after the recall opened, moves one panel from `installed` to `fixed` at its party and adds 1 to the party's `fixed_by_panel`. The event, the panel's `fixed_event` and the ledger change are one transaction.
- **The panel outranks the phone.** A party with connected panels carries `connected_unfixed`, the number of them with no passing test. The validator on `parties` requires `installed` to be at least that number. A phone claim of "all fixed" is refused while a connected panel still fails.
- **Linking a panel to a party.** A connected panel knows its `site_label`, not a contact. When a claim names a building owner, the API links the panels whose label matches. Use Atlas Search fuzzy matching if the sandbox has it, otherwise a normalised exact match.
- **Panels that are not connected** are verified by the three checks on a call. That is weaker evidence, and Open risks says so.

### In the run

| When | What happens |
| --- | --- |
| Simulated day 0 | Three of the 24 connected panels fail their monthly test. The signal is proposed and approved, and the recall opens with the 5 names on the shipment records |
| Days 1 to 9 | As building owners are found, their connected panels are linked. Passing tests after an installer's visit move panels to fixed |
| Live in the demo | A teammate presses Apply update and Run test on the simulated panel. One panel at Corner Properties moves to fixed, with the event id as evidence |

On screen this is one strip, not a dashboard: connected panels, how many are failing, how many have passed after a fix, and the last panel event in the feed.

### Endpoints added

| Endpoint | Purpose |
| --- | --- |
| `GET /panel/{serial}` | The simulated panel page |
| `POST /panels/{serial}/events` | Receive a test event. Header: a shared secret |
| `POST /admin/signals/{id}/approve` | A person approves a proposed signal and the recall opens |
| `POST /admin/panels/{serial}` | Set a panel's fault flag or version for a test |

### Tests

| # | Test | Passes when |
| --- | --- | --- |
| 34 | Signal rule | Two failures raise nothing. A third at a third site creates one `proposed` signal. A fourth is added to the same signal |
| 35 | Approval gate | A recall with a signal source cannot be created while the signal is `proposed`. Atlas rejects `approved` without `approved_by`. No agent tool list contains approve |
| 36 | Fix by the panel's own test | A PASS on version 2.5 moves exactly one panel from `installed` to `fixed`. Posting the same `test_id` again moves nothing. A FAIL, or a PASS on version 2.4, moves nothing |
| 37 | The panel outranks the phone | A claim of all fixed is rejected by Atlas while a linked connected panel has no passing test. After its PASS, the same claim is accepted |

## Long run

The run is long in four ways that show on screen: steps taken, simulated days elapsed, parties found at run time, and kills survived. This section covers the 40-party run. The four-party live slice sits inside it.

### Track fit

| Clause in the participant guide | Mechanism | Where a judge sees it |
| --- | --- | --- |
| A long multi-step task | One recall of about 40 parties and 300 or more steps | Step counter and party count |
| Work that takes hours | 10 simulated days of calls, callbacks, fix checks and retries on durable timers | Simulated day on screen; the run started before judging |
| Across several systems | Phone (ElevenLabs over Twilio), the web form, two distributors' order systems, the panel service | Feed lines tagged by system |
| Atlas persists verified task state | Ledger, attempts, operations, timers, adapters and panel events all live in Atlas | Close refused by Atlas |
| Vector Search retrieves past experience | Episodes hold party behaviour and order-system mappings | The second distributor repairs from a retrieved mapping, with no model call |
| Took effect, not only sent | A call counts when a claim is accepted. A hold counts when the read-back shows it. A fix counts on a passing test or the three checks | `sent` and `verified` are separate states in the feed |
| Resume without replaying history | A restarted worker asks Atlas what is due and what is in doubt | Resume line: documents read, not steps replayed |
| A system's fields or workflow change | Distributor North's order system moves to v2 on simulated day 3 | Flagged operations, repaired mapping, targeted retry |

### Targets

| Measure | Target | Proven by |
| --- | --- | --- |
| Parties | 40: 5 at the start, 35 found during the run | Test 16 |
| Steps | 300 or more | Run stats |
| Simulated time | 10 days | Test 16 |
| Real time for the judged run | 120 minutes, at 30 seconds per simulated hour | Clock setting |
| Random kills survived | 10 or more | Test 22 |
| Duplicate effects | 0 | Invariant check, once per simulated hour |
| Systems | 4, one of which changes | Tests 17 to 19 |
| Model context per decision | Same size at step 300 as at step 1 | Token count on screen |

### What the run looks like

&#91;embedded content: run timeline · six phases over ten simulated days\]

The party count grows for two days, an order system changes on day 3, and the rest of the run is chasing, fix checks and settling. All clear is the only step the agent cannot force.

The screen during round one, at simulated day 6. Values and names are examples.

```text
ALL CLEAR   Recall of batch B-4471   2,400 panels
NOT ALL CLEAR: 640 panels open (285 not located, 355 awaiting fix)
Simulated day 6 of 10, 14:00     Real time 01:19     Clock: 30 s per simulated hour

Parties   40 (5 at the start, 35 found)      Checked 31 of 40      Steps 412
Kills survived 9      Duplicate effects 0      Context per decision 1,850 tokens
Located 2,115 of 2,400 (88%)      Fixed or returned 1,340 of 2,400 (56%)
Connected panels 24      failing 3      passed after fix 9

Systems
  phone         58 attempts, 29 accepted claims
  North orders  adapter v2 (repaired by model)    24 operations verified
  East orders   adapter v2 (from memory)          19 operations verified
  panels        41 test events, 1 signal approved

Feed, newest first
  d6 14:00  invariants    4 of 4 hold
  d6 13:20  phone         Bloor Alarm: no answer, attempt 2. Next action d7 09:00
  d6 12:05  worker w3     adopted Bloor Alarm: 1 in doubt, 4 documents read, 0 steps replayed
  d6 12:05  supervisor    worker w2 killed (9)
  d6 11:40  ledger        REJECTED by Atlas: Document failed validation on parties. Reported 90, shipped_in 60
  d6 10:15  phone         Annex Fire names "Dundas Lofts": new party, tier 3
  d5 16:30  panel         D7-4471-0117 PASS on 2.5. Maple Court: 1 panel fixed
  d3 09:42  East orders   contract failed. Episode e-0412 retrieved. Adapter v2 active, 0 model calls
  d3 09:31  North orders  adapter v2 active: canary 48 = 48. Retrying 4 operations
  d3 09:30  North orders  contract failed: field qty_in_stock missing. 4 operations flagged, 20 untouched
  d0 08:05  signal        3 failed tests at 3 sites, batch B-4471. Approved by operator-1. Recall opened
```

### Scenario at scale

`scenarios/generate.py` builds `recall_long.json` from a fixed seed. It asserts that every panel is conserved before it writes the file. The batch is 2,400 panels: 240 to the slice, 1,300 to Distributor North and 860 to Distributor East.

| Tier | Who | Count | Known at the start | Found through |
| --- | --- | --- | --- | --- |
| 1 | Slice installers: Harbour Fire, Queen St Alarm, Lakeside Systems | 3 | Yes | Shipment records |
| 1 | Distributors | 2 | Yes | Shipment records |
| 2 | Distributor accounts, all installers | 21 | No | The order system's account list for the batch |
| 3 | Building owners and further installers, including Corner Properties | 14 | No | A claim that names them |

Two parties answer a real phone: Harbour Fire (a judge) and Corner Properties (a teammate). The two distributors answer through their order systems. The other 36 are simulated, each with one behaviour profile.

| Profile | Parties | Behaviour | What it exercises |
| --- | --- | --- | --- |
| Straight | 14 | Answers the first contact with correct counts | Volume |
| No answer | 5 | Answers only on the third attempt, or only in stated hours | Timers, the ladder, memory of good hours |
| Callback | 3 | Asks to be called back the next morning | A timer set from a reply |
| Over-reporter | 3 | Reports more than it was shipped, then corrects | Validator rejection at scale |
| Reviser | 2 | Corrects an accepted count two days later | Claim versions and re-reconciliation |
| Forwarder | 6 | Installed or passed on part of its stock to parties on no list; one chain is three hops deep | Sub-recalls |
| Short receiver | 2 | Received fewer panels than the sender claims | Disputes and follow-ups |
| Unreachable | 1 | Never answers; the distributor's rep reports for it on day 8 | The whole ladder, then escalation |

Every building owner also follows a fix script. Its panels are reported installed at first, and fixed two to four simulated days later, after the installer's visit. A fix check is a durable timer like any other.

### Added collections and fields

| Where | Added | Purpose |
| --- | --- | --- |
| `parties` | `tier`, `found_via` | How the party entered the run |
| `parties` | `next_action_at`, `ladder_step` | The durable timer and the position on the contact ladder |
| `parties` | `hold_for_demo` | Live-slice parties skip the timers until released by hand |
| `operations` (new) | `op_id`, `kind`, `party_id`, `idempotency_key`, `adapter_version`, `state`, `readback_at`, `readback_value` | One document per order-system call. States: `intent`, `sent`, `verified`, `needs_reverify`, `failed` |
| `adapters` (new) | `system`, `version`, `expected_fields`, `mapping`, `status`, `canary` | The contract every order-system response is checked against |
| `runs` (new) | `clock`, `steps`, `kills`, `duplicates`, `tokens_per_decision`, `last_invariant_check` | The numbers on screen |
| `episodes` | `kind: "adapter"`, `system_vendor`, `mapping` | Lets a second order system reuse a repair |

Four rules are added in Atlas:

1. `operations`: a validator refuses `state: "verified"` without `readback_at` and `readback_value`.
2. `operations`: a unique index on `idempotency_key`.
3. `adapters`: a partial unique index allows one `active` version per system.
4. `parties`: an index on `next_action_at`, so the due query reads only what is due.

### Clock and timers

1. Simulated time is computed from three settings in `runs.clock`: real start, simulated start and seconds per simulated hour. Nothing ticks.
2. Every open party has `next_action_at` in simulated time. A worker's loop is the claim in the Dispatch section: parties that are due, have work and are not held for the demo.
3. Acting on a party and setting its next timer happen in one transaction with the attempt's `intent`.
4. A worker that takes over a party resolves its in-doubt attempt or operation first. It reads no history.
5. The model decides from a fixed-size context: the party's document, its open edges, up to three retrieved episodes and the tool list.

Clock settings: 30 seconds per simulated hour for round one (120 minutes), 15 seconds for the finals (60 minutes), 0.05 seconds in tests.

A timer as it sits on a party. Times are simulated hours since the run began, so 177 is day 7 at 09:00.

```json
{
  "_id": "p-0127",
  "name": "Bloor Alarm",
  "type": "installer",
  "tier": 2,
  "found_via": "order_system",
  "state": "contacting",
  "shipped_in": 60,
  "ladder_step": 2,
  "next_action_at": 177,
  "hold_for_demo": false
}
```

What the model is given for one decision. Its size does not depend on how long the run has been going.

```text
RULES    the agent's rules and its tool list
PARTY    Bloor Alarm, installer, tier 2, shipped_in 60, ladder step 2, last result: no answer at d6 13:20
EDGES    in: Distributor North 60 (sender claim). out: none
MEMORY   up to 3 episodes, for example "answers after 14:00 on weekdays"
ASK      choose the next action and its time
```

### Contact ladder

| Step | Action | Moves on when | Next timer |
| --- | --- | --- | --- |
| 1 | Call | No answer or busy | 4 simulated hours |
| 2 | Call at an hour retrieved from memory, else the next morning | No answer | Next simulated day, 9:00 |
| 3 | Form link, delivered by the simulator | No submission | 24 simulated hours |
| 4 | Third call | No answer | 48 simulated hours |
| 5 | Escalate to the distributor's rep | The rep has no data | None: units stay unresolved and the recall cannot close |

A reply can set the timer. "Call back tomorrow at 9" becomes `next_action_at` for that hour. A claim that leaves panels installed sets a fix check 48 simulated hours later.

### Distributor holds

For each distributor the agent places a hold on the batch, so that no more panels ship, and reads the account list.

1. Write an `operations` document in state `intent`. The idempotency key is built from recall, party and kind.
2. Send `POST /holds` with the key. Record `sent`.
3. Read back with `GET /holds`. Only a read-back that shows the hold moves the operation to `verified`.
4. Read `GET /accounts`. Each account becomes a tier 2 installer with `found_via: "order_system"` and an edge from the distributor.
5. An operation left in `intent` or `sent` by a dead worker is resolved by read-back first. It is sent again only when the read-back shows nothing, and with the same key.

The mock injects three faults: accepted then dropped, timeout after commit, and a stale read for 10 simulated minutes.

One order-system call as it is stored: first when sent, then when the read-back confirms it.

```json
{
  "_id": "op-0031",
  "kind": "place_hold",
  "system": "north",
  "party_id": "dist-north",
  "idempotency_key": "r-1:dist-north:place_hold",
  "adapter_version": 1,
  "state": "sent",
  "sent_at": 2.4,
  "readback_at": null,
  "readback_value": null
}
```

```json
{
  "_id": "op-0031",
  "state": "verified",
  "readback_at": 2.5,
  "readback_value": { "hold_id": "H-7781", "status": "on_hold" }
}
```

A worker killed between the two leaves the first document. The worker that adopts the distributor reads the order system, finds hold H-7781 under the same key, and writes the second state without sending again.

### When an order system changes

On simulated day 3, or by hand in the demo, Distributor North's order system moves to v2. `batch` becomes `batch_code`, `qty_in_stock` becomes `inventory.available`, and a hold needs a second call to confirm it.

1. **Detect.** A response fails the active adapter's `expected_fields`, or a read-back cannot find a field. This is a check in code, not the model's opinion.
2. **Contain.** Work on that system pauses. Its operations in `intent` or `sent` become `needs_reverify`. Verified operations are not touched.
3. **Repair.** The model gets the failing response, the system's error text, its openapi.json and the old mapping. It proposes a new mapping and any new step.
4. **Verify.** The proposed adapter must reproduce a canary: a count the ledger already holds as corroborated for one account. A wrong mapping fails here and is never activated.
5. **Activate.** The new adapter becomes `active` in one transaction that retires the old one. Each operation records the adapter version it used.
6. **Retry.** Only the `needs_reverify` operations run again.
7. **Remember.** The failure text and the mapping are saved as an `adapter` episode. When Distributor East's system fails the same way, Vector Search on the failure text returns the episode. The mapping passes the same canary check and is activated with no model call.

The same account before and after the change:

```json
{ "account": "N-014", "name": "Bloor Alarm", "phone": "+1-416-555-0127", "qty_shipped": 60, "qty_in_stock": 22 }
```

```json
{ "account": "N-014", "name": "Bloor Alarm", "phone": "+1-416-555-0127", "shipped": 60, "inventory": { "available": 22, "quarantined": 0 } }
```

The adapter the repair produces:

```json
{
  "_id": "north:v2",
  "system": "north",
  "version": 2,
  "status": "active",
  "replaces": "north:v1",
  "source": "model",
  "expected_fields": ["account", "name", "phone", "shipped", "inventory.available"],
  "mapping": {
    "batch": "batch_code",
    "qty_shipped": "shipped",
    "qty_in_stock": "inventory.available",
    "status": "state"
  },
  "steps": { "place_hold": ["POST /v2/holds", "POST /v2/holds/{id}/confirm"] },
  "canary": { "account": "N-009", "field": "qty_shipped", "ledger_value": 48, "system_value": 48, "passed": true }
}
```

Distributor East's adapter is the same document with `"source": "episode e-0412"`. A mapping that sends `qty_shipped` to the wrong field returns a different number for account N-009, fails the canary and is discarded.

### Kills and invariants

`scripts/supervisor.py` starts four workers. Every 2 to 5 real minutes it kills one at random with SIGKILL and restarts it after 3 seconds. Each kill increments `runs.kills`.

Once per simulated hour the API runs four checks and writes the result to `runs`:

1. No party has two live attempts.
2. No idempotency key has two holds in the order system's own log.
3. Every accepted claim has a tool event or a panel event behind it.
4. Mass balance equals `units_total`.

A failed check turns the counter red and stops the supervisor.

The run document behind the counters. The last check was at simulated hour 158, which is day 6 at 14:00.

```json
{
  "_id": "run-1",
  "recall_id": "r-1",
  "clock": { "real_start": "2026-11-14T16:55:00-05:00", "sim_start": 0, "seconds_per_sim_hour": 30 },
  "workers": 4,
  "steps": 412,
  "kills": 9,
  "adoptions": 9,
  "duplicates": 0,
  "tokens_per_decision": 1850,
  "last_invariant_check": { "at": 158, "passed": [true, true, true, true] }
}
```

### Mock order system

The order system is its own FastAPI app in `levela/mock/order_system.py`. It runs as a separate process, one instance per distributor, so its faults and its version are independent of the API server.

| Version | Endpoint | Request | Response |
| --- | --- | --- | --- |
| v1 | `POST /v1/holds` | `batch`, `account_scope`, header `Idempotency-Key` | `202` with `hold_id` |
| v1 | `GET /v1/holds?batch=` |  | List of `hold_id`, `batch`, `status` |
| v1 | `GET /v1/accounts?batch=` |  | List of `account`, `name`, `phone`, `qty_shipped`, `qty_in_stock` |
| v2 | `POST /v2/holds` | `batch_code`, `scope`, `reason_code`, header `Idempotency-Key` | `202` with `hold_id` and `state: "pending_confirm"` |
| v2 | `POST /v2/holds/{id}/confirm` |  | `200` with `state: "active"` |
| v2 | `GET /v2/holds?batch_code=` |  | List of `hold_id`, `batch_code`, `state` |
| v2 | `GET /v2/accounts?batch_code=` |  | List of `account`, `name`, `phone`, `shipped`, `inventory.available`, `inventory.quarantined` |

After the switch, v1 paths answer `410` with a body that names v2. That body is the error text the model reads. `GET /_log/holds` returns the system's own record of holds by key, for invariant check 2.

### Admin endpoints added

| Endpoint | Purpose |
| --- | --- |
| `POST /admin/clock` | Set seconds per simulated hour; start or pause the run clock |
| `POST /admin/release` | Release a held live-slice party to the timers |
| `POST /admin/order-system` | Switch an order system to v2, or arm one of its faults |
| `POST /admin/supervisor` | Pause or resume random kills |
| `GET /wall/state` | Now also returns `runs`: simulated day, steps, kills, duplicates, tokens per decision |

### Demo controls

- The long run starts at 4:55 with the slice parties and the order-system change held.
- Random kills are paused from the judge's call until the wrap. The kill beat uses the failpoint, so its timing is exact.
- The operator page has six buttons for the live part: Release Harbour Fire, Arm failpoint, Switch North to v2, Saboteur, Open the panel page, Close recall.
- If the repair stalls in front of judges, show the last passing run of test 18 and say so.
- Memory fallback: start recall 2, as in the four-party demo.

### Tests 16 to 22

Seven tests, all in `pytest` on the fast clock. None needs a phone: the slice parties use the fake voice provider.

| # | Test | Passes when |
| --- | --- | --- |
| 16 | Long run | 40 parties and 10 simulated days finish in under 3 minutes. The run ends with only the slice's panels open: 15 disputed and 25 installed at Corner Properties. Mass balance holds at every simulated hour |
| 17 | Hold took effect | With "accepted then dropped" armed, the operation stays `sent` and the read-back finds nothing. One resend with the same key reaches `verified`. The order system's log shows one hold |
| 18 | Order-system change | After the switch, only `intent` and `sent` operations become `needs_reverify`. The repaired adapter passes the canary. Only those operations retry, and verified ones keep their adapter version |
| 19 | Bad mapping refused | A wrong mapping fails the canary. No adapter is activated and work on that system stays paused |
| 20 | Adapter memory | Distributor East's failure retrieves the episode. Adapter v2 is activated with 0 model calls |
| 21 | Durable timers | Workers are killed with 5 timers pending and restarted after they are due. Each timer fires once, and no history is read |
| 22 | Random kills | 10 kills during a fast long run with four workers. All four invariants hold and the run ends as in test 16 |

## Dispatch, workers and harness

Four identical workers claim work from Atlas under a lease, and one gate decides what any of them may do. No model sits in the dispatch path, and no model can mark its own work done: only a read-back, an accepted claim or a panel's own test can.

&#91;embedded content: planner, workers and the tool gate · nine parts, one direction of flow\]

Work flows one way. Events make work due, workers claim it, the gate checks every action, and evidence decides what counts.

This section merges the dispatch addendum with the harness. The lease mechanics are the addendum's. The gate, the typed results and the saboteur tests sit on top. The addendum's optional Ask is replaced by Questions as data, below.

### Safety and liveness are separate

| Property | Enforced by |
| --- | --- |
| Never two live attempts for a party | Partial unique index on `attempts` |
| Never two holds for one key | Unique index on `operations.idempotency_key`, and a read-back before any resend |
| Who works on what | The lease on the party |
| What happens when a worker dies | Lease expiry, then adoption by another worker |

A wrong or lost lease costs seconds. It cannot cause a duplicate call or hold, because the indexes refuse the second write whatever the lease says.

### What comes from published practice

- **State lives outside the model.** Anthropic's long-running harness carries state between sessions in files, and defines done as a feature list with pass flags ([source](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)). Here the state is Atlas documents and done is the close gate.
- **Subagents get a contract.** Anthropic's research system gives each subagent an objective, an output format, tool guidance and task boundaries. Its subagents store work externally and pass back references ([source](https://www.anthropic.com/engineering/multi-agent-research-system)). Here the contract is a task-run document and the references are evidence ids.

What this design adds is for effects on outside systems: an idempotency key on every action, verification by read-back, and refusal by the database when evidence is missing. Leases with fencing tokens are a standard pattern too. Say "we found none combining these", never "first".

### Claiming work

Each party carries `lease.owner`, `lease.until`, `lease.token`, `in_flight`, `priority` and `open_questions`. A worker claims a party with one atomic `findOneAndUpdate`. It tries the in-doubt claim first, then the due claim.

```text
In-doubt claim   filter   lease expired:  $expr  lease.until < $$NOW
                          in_flight is not null
                 sort     lease.until ascending

Due claim        filter   has work:   state is not reconciled, or open_questions is not empty
                          is due:     next_action_at <= simulated now
                          not held:   hold_for_demo is false
                          is free:    $expr  lease.until < $$NOW
                 sort     priority descending, then next_action_at ascending

Both             update   (pipeline)  lease.owner = worker id
                                      lease.until = $$NOW + 8 seconds
                                      lease.token = lease.token + 1
                 return   the document after the update
```

1. **The database clock decides.** `$$NOW` is evaluated by the server, in the filter through `$expr` and in the pipeline update ([MongoDB docs](https://www.mongodb.com/docs/manual/tutorial/update-documents-with-aggregation-pipeline/)). Workers' clocks are never compared. Test 1 confirms `$$NOW` in a `findOneAndUpdate` filter on the sandbox. The fallback is the worker's own clock, which is acceptable because all workers run on one machine.
2. **The lease lasts 8 real seconds.** A thread renews it every 2 seconds while the worker holds the party. A killed worker stops renewing, so another worker adopts the party within about 8 seconds. Both numbers are settings: shorter on the test clock, 5 seconds for the finals.
3. **The token is a fencing token.** Every later write under the lease carries `lease.owner` and `lease.token` in its filter. A worker that lost its lease matches nothing and drops the party.
4. **Recovery runs at claim time.** If `in_flight` names an attempt or an operation, the new holder resolves it before doing anything new.
5. **Release and result are one transaction.** It writes the result, sets `next_action_at`, clears `in_flight` and moves `lease.until` to now.
6. **A new party starts free.** It is created with `lease.until` at the epoch, `lease.token` 0 and `next_action_at` now.

An `$expr` filter does not use an index well. At 40 parties that does not matter. Distributors are parties too, so a distributor's lease makes adapter repair single-flight per order system.

### One unit of work

1. Claim.
2. If `in_flight` is set, resolve it. For an attempt: check `tool_events`, then Twilio, then mark it `abandoned`. For an operation: read back before any resend.
3. Choose the next action. This is the model, working from a fixed-size brief.
4. One transaction: the attempt or operation in `intent`, the party's next timer, and `in_flight` set to its id.
5. Dial, or call the order system. Save the returned identifiers.
6. Keep renewing the lease until the attempt or operation reaches a final state. Write the episode.
7. Release.

- **Wake-up:** each worker keeps a change stream on `parties`. An insert, or a change to `next_action_at`, triggers an immediate claim. The stream never decides who acts. Otherwise a worker polls every 500 ms on the demo clock.
- **Operations budget:** on a cluster capped at 100 operations per second, four idle workers use about 16, renewals about 2 and the screen about 9.
- **Fallback:** `WORKERS=1` runs the same code as a single worker.

### Slots for real calls

A voice attempt needs a call slot.

- Slots in use are counted from live voice attempts, not kept in a counter. A killed worker cannot leak a slot.
- The count and the attempt's `intent` are written in one transaction that also increments a `capacity` document. Two workers racing for the last slot conflict on that document, and one retries.
- `capacity.max_live` is set from the accounts on the day.

| Vendor | Limit | Source |
| --- | --- | --- |
| Twilio | 1 new outbound call per second by default; extra API calls are queued | [Twilio support](https://support.twilio.com/hc/en-us/articles/223180028-How-Fast-Can-I-Place-or-Receive-Phone-Calls-with-Twilio) |
| Twilio | Live calls at once: 5 on a trial, 2 with no approved customer profile, 3 with an individual profile, no cap with a business profile | [Twilio support](https://support.twilio.com/hc/en-us/articles/223180028-How-Fast-Can-I-Place-or-Receive-Phone-Calls-with-Twilio) |
| ElevenLabs | Live calls at once depend on the plan. Burst pricing allows up to 3 times that limit at twice the rate. Calls beyond it are rejected unless queueing is on | [ElevenLabs docs](https://elevenlabs.io/docs/agents-platform/guides/burst-pricing) |

Expect two real phones at once, perhaps three. Simulated parties are not limited. In the demo the two real calls are made one after the other.

### The planner

The planner is the "boss" of the original idea, kept deliberately weak. It ranks and adds work. It does not assign work, and it cannot change the ledger.

| Aspect | Rule |
| --- | --- |
| Runs when | A party is created, a question is added, an order-system contract fails, a signal is proposed, a close is refused or an invariant fails. Otherwise every 6 simulated hours |
| Reads | One aggregation: parties by state and tier, the ten parties with the most open panels, the oldest due time, free call slots, and failure classes from the last simulated day |
| May write | `parties.priority`, a new question, an escalation to ladder step 5, a candidate adapter, a draft status report |
| May not | Write claims, edges, attempts or operations. Set `verified` or `reconciled`. Approve a signal. Close the recall |
| Enforced by | Its own allowlist in the tool gate. A restricted Atlas user is optional, and works per collection, not per field |

Its plan is data. If the planner is killed, the priorities and questions it wrote are still in Atlas, and its next run reads the same aggregation. The claim works without it: every `priority` is then 0 and the sort falls back to due time.

### Questions as data

A new question for every party is one document and one update. The three checks are the first questions on a recall, asked of building owners.

```json
{
  "_id": "q-04",
  "text": "Did you also receive batch B-4480?",
  "answer_type": "yes_no_count",
  "applies_to": { "tier": [1, 2, 3] },
  "asked_at": 96
}
```

1. Adding it pushes `q-04` onto `open_questions` for every matching party that has no answer, and sets `next_action_at` to now. Parties that already answered are not touched.
2. Free workers claim those parties at once, within the call slots.
3. The brief lists the open questions. The voice agent receives them as dynamic variables and records each answer with a `submit_answer` tool: question id and value.
4. The agent reads the answer back before the tool call. The API stores it under `answers.q-04` and removes the id from `open_questions`.
5. A reconciled party is asked the question only. Its counts are not asked again.

### The subagent contract

A subagent is one model working on one party, with no memory of its own. Each run is a `task_runs` document.

```json
{
  "_id": "tr-0412",
  "worker": "w3",
  "party_id": "p-0127",
  "lease_token": 7,
  "kind": "contact_party",
  "tools": ["place_call", "send_form_link", "schedule_callback", "escalate"],
  "budget": { "model_calls": 4, "tool_calls": 6, "sim_hours": 2 },
  "used": { "model_calls": 1, "tool_calls": 1, "sim_hours": 0.2 },
  "status": "running",
  "result": null
}
```

| Part | Rule |
| --- | --- |
| Brief | Built by code from the party document, its edges, its open questions and up to three episodes. Fixed size. The subagent never sees the run's history or other parties |
| Tools | Only those listed on the run. Each kind of run has its own list |
| Budget | Model calls, tool calls and simulated time. The gate checks it on every call |
| Result | One JSON object, validated against a schema. Free text is not accepted |

The result a subagent returns:

```json
{
  "outcome": "no_answer",
  "evidence": ["at-0931"],
  "next": { "action": "call", "at": 177 },
  "note": "Rang out twice. Memory says this installer answers after 14:00."
}
```

`outcome` is one of `claim_accepted`, `claim_rejected`, `no_answer`, `callback_requested`, `form_sent`, `answer_recorded`, `escalated` and `failed`. The result handler checks it against the facts. Every evidence id must exist and belong to this run. A result that says `claim_accepted` with no accepted claim for this run's attempt is rejected as `unsupported_result`.

### The tool gate

Every tool call from every subagent, and from the planner, passes through one function. Its checks run in this order:

1. **Run.** Load the task run. Refuse if it is not `running`.
2. **Lease.** Compare the party's `lease.owner` and `lease.token` with the run's.
3. **Allowlist.** The tool must be in the run's `tools`.
4. **Budget.** Increment `used` under a guard. Refuse at the limit.
5. **Intent.** Insert the attempt or operation with its idempotency key. A duplicate key returns the stored outcome and does not act again.
6. **Act.** Call the provider.
7. **Record.** Store the outcome and the provider's ids. The gate never writes `verified`.

Checks 2, 4 and 5 are one transaction. A refusal is written to the trace with the rule that caused it.

### Verification

The model that acted never decides whether the action worked.

| Effect | Counts as verified when | Written by |
| --- | --- | --- |
| Phone call | The provider reports the call completed, and a claim carrying this attempt id was accepted by Atlas | API server, on the webhooks |
| Distributor hold | A fresh read of the order system shows the hold | The verify function, in code |
| Form link | A submission arrives with the party's token | API server |
| Answer to a question | The party confirmed the read-back and `submit_answer` was accepted | API server |
| Adapter | It reproduces the canary count | The canary check, in code |
| Fix of a connected panel | The panel posts a PASS on the fixed version | API server, on the panel event |
| Fix of any other panel | A claim carries a version outside the affected list and a sounder test of yes. This is weaker evidence than the panel's own test | The validator on `parties` |

### Traces and replay

Every model call and every tool call writes one `traces` document: run id, sequence number, the brief as sent, the model's output, the tool arguments, the gate's decision, latency and tokens. If LangSmith credits are provided, the same run id is its tag.

`scripts/replay_run.py` rebuilds one run from its trace. Tools are stubbed with the recorded outputs, so a prompt change can be tested against recorded runs without placing a call.

A failed run gets one failure class and is saved as an episode: `no_answer`, `claim_rejected`, `contract_failed`, `budget_exceeded`, `unsupported_result` or `gate_refused`. The next brief for that party includes it.

### Tests 23 to 33

`harness/saboteur.py` is a worker that breaks one rule on purpose. All eleven tests run in `pytest` on the fast clock. In the demo, saboteur mode runs one such worker inside the live run.

| # | Test | Passes when |
| --- | --- | --- |
| 23 | Leases | Four workers on a fast long run. No party ever has two live attempts. The run ends as in test 16 |
| 24 | Adoption | The lease holder is killed at each failpoint and not restarted. Another worker adopts within the lease time plus one second. The provider records exactly one call per party. `in_flight` ends null |
| 25 | Race | Two workers claim one due party at the same instant, 100 rounds. Exactly one gets it every round, and `lease.token` rises by 1. Expiry follows the database clock |
| 26 | Stale worker | A worker paused past its lease resumes. Its writes match nothing, and the second worker's result stands |
| 27 | Wrong party | The saboteur acts on a party it does not hold. The gate refuses on `lease` |
| 28 | Wrong tool | A contact subagent calls `close_recall` or `approve_signal`. The gate refuses on `allowlist` |
| 29 | Double send | The same effect is sent twice. One attempt exists, the second call returns the stored outcome, and the order system's log shows one hold |
| 30 | False success | A write of `verified` with no read-back is rejected by Atlas. A result of `claim_accepted` with no accepted claim is rejected as `unsupported_result` |
| 31 | Loop | A subagent that never returns is stopped at its budget. The lease is released, a `budget_exceeded` episode is saved, and the party is due again |
| 32 | Last slot | Two workers race for the last call slot. One dials; the other's transaction retries and finds none |
| 33 | Question fan-out | A question is added mid-run. Only parties without an answer become due, reconciled parties are asked the question only, and no count is asked again |

Test 7 runs with one worker, where the restarted worker adopts its own expired lease. Test 22 runs with four.

### What this section adds

| Kind | Added |
| --- | --- |
| Collections | `task_runs`, `traces`, `questions`, `capacity` |
| Fields on `parties` | `lease.owner`, `lease.until`, `lease.token`, `in_flight`, `priority`, `open_questions`, `answers` |
| Fields on `runs` | `workers`, `adoptions` |
| Tools | `submit_answer` for the voice agent. `set_priority`, `add_question`, `escalate`, `propose_adapter` and `draft_report` for the planner |
| Endpoints | `POST /tools/submit_answer`, `POST /admin/questions`, `POST /admin/saboteur` |
| Screen | Workers and calls in flight, with real and simulated shown apart. The lease draining on a dead worker's party. Gate refusals in the feed, with the rule named |

A refusal on screen, with example values:

```text
d6 15:10  gate        REFUSED  w5 (saboteur): place_call on p-0131. Rule: lease. Holder is w2, token 9
d6 15:10  atlas       REJECTED w5 (saboteur): operations.state = verified without readback_value
d6 15:11  gate        STOPPED  w5 (saboteur): budget_exceeded after 6 tool calls. Party p-0140 is due again
d6 15:12  invariants  4 of 4 hold
```

In the demo, switch saboteur mode on for 20 seconds before the judge's call, or keep it for questions. The recall stays in front; the harness is the reason it stays correct.

## Architecture

Three kinds of process share one Atlas cluster: an API server that receives every claim and panel event, four workers that act on what is due, and a mock order system per distributor. A supervisor kills workers at random, and the API server stays up.

&#91;embedded content: architecture · API server, four workers, mock order systems, one Atlas cluster\]

The voice agent talks to the party and sends each spoken count to the API as a tool call. The API writes the claim and the ledger in one transaction, and Atlas accepts or rejects it. A worker never touches a claim: it claims a party, places calls and order-system operations, and resolves what a dead worker left in doubt.

- **API server:** FastAPI on a public host. It handles the tool webhooks, the post-call webhook, the web form, panel events, admin actions, the invariant check and the screen's data.
- **Workers:** four identical Python processes. Each claims a party under a lease, opens attempts and operations, places calls, and reads and writes memory.
- **Planner:** runs inside the API process, on events. It writes priorities and questions only.
- **Supervisor:** a small script that kills a worker at random and restarts it. It is paused during the live part of the demo.
- **Mock order system:** a separate FastAPI process per distributor, with its own faults and its own version switch.
- **Simulators:** a module the workers invoke for the 36 profiled parties, and the simulated panel page. Both post through the API, so simulated and real input share one code path.
- **Screen:** a page served by the API, polling once a second.
- **Not drawn:** the supervisor, the planner, and a worker's query of Twilio's Calls list for in-doubt attempts.

## Technology list

Thirteen tools, each with one job. No agent framework is used: the orchestrator is a plain state machine, because the correctness rules live in the database.

| Technology | Role in the build |
| --- | --- |
| MongoDB Atlas (sandbox cluster) | System of record: ledger, claims, attempts, operations, panel events and memory. Uses validators, transactions, unique and partial indexes, change streams, pipeline updates with `$$NOW`, `$lookup`, `$graphLookup` and Vector Search. Atlas Search for name matching if the sandbox has it |
| PyMongo (sync client) | Driver for the API and the workers; transactions through sessions |
| Python 3.11+ with FastAPI and Uvicorn | The webhook, panel and admin server, and the mock order systems |
| Pydantic | Request and response models for the tool endpoints |
| httpx | Calls to ElevenLabs, Twilio and the order systems |
| ElevenLabs Agents | Voice agent: talks to the party, calls the `submit_count` and `submit_answer` webhook tools, sends a post-call webhook |
| Twilio (paid account) | One purchased phone number imported into ElevenLabs; Calls list API for in-doubt checks |
| A language model API | The subagent's choice of next action, adapter repair and the planner. Choose from the partner resources |
| Embedding model | Vectors for memory episodes. Use Voyage if the sandbox provides it, otherwise any embedding API |
| Public host for the API (Render, Fly.io or Railway; a tunnel as backup) | Stable HTTPS URL for webhooks, independent of venue Wi-Fi |
| Plain HTML and JavaScript, or React if its owner is faster in it | The screen and the simulated panel page |
| pytest | The 37 tests |
| Git and a public GitHub repo | Required for submission |

Optional: LangSmith for traces. Left out on purpose: SMS, email, image evidence and any agent framework.

## Data model

Nineteen collections in one database. The nine below are the ledger and its logs. The long run adds three, the harness four and monitoring three, each described in its own section.

| Collection | Purpose | Key fields | Indexes |
| --- | --- | --- | --- |
| `recalls` | One document per recall | `batch_code`, `model`, `affected_versions`, `fixed_version`, `checks`, `source` (signal, manual), `source_signal`, `units_total`, `open_units`, `status` (open, closed) | None beyond `_id` |
| `shipments` | Seeded record of what the supplier shipped | `recall_id`, `party_id`, `qty` | Unique (`recall_id`, `party_id`) |
| `parties` | One node per party per recall; this is the ledger | `type` (distributor, installer, owner), `shipped_in`, `in_stock`, `installed`, `fixed`, `returned`, `forwarded`, `unresolved`, `version_seen`, `sounder_test`, `affected_versions`, `connected_unfixed`, fixed\_by\_panel, `state`, `tier`, `channel`, `phone`, `found_via` | Unique (`recall_id`, `party_id`) |
| `edges` | One transfer between two parties | `from_party`, `to_party`, `sender_qty`, `recipient_qty`, `corroborated_qty`, `disputed_qty`, `state` (awaiting\_recipient, corroborated, disputed) | Unique (`recall_id`, `from_party`, `to_party`) |
| `claims` | Append-only record of everything a party or a panel said | `party_id`, `attempt_id`, `source` (voice, sim, form, panel), `raw`, `status` (accepted, rejected), `reject_reason`, `dedupe_key` | Unique `dedupe_key` |
| `attempts` | One contact attempt | `party_id`, `seq`, `channel`, `state` (intent, dialed, connected, completed, no\_answer, failed, abandoned), `conversation_id`, `call_sid`, `intent_at` | Unique (`recall_id`, `party_id`, `seq`); partial unique (`recall_id`, `party_id`) while state is intent, dialed or connected |
| `tool_events` | Append-only log of every webhook received | `attempt_id`, `conversation_id`, `kind` (tool\_call, post\_call), `payload`, `received_at` | `attempt_id` |
| `episodes` | Memory of past contacts and repairs | `party_profile`, `summary`, `tags`, `embedding`, `valid` | Vector Search index on `embedding`, filter on `valid` |
| `worker_state` | One document per worker | Change stream resume token, heartbeat | None beyond `_id` |

### Rules enforced by Atlas

1. **Party conservation** (validator on `parties`). Every quantity is a non-negative integer and the equation below holds. A party that reports more than it was shipped would need a negative `unresolved`, so Atlas rejects the write.
2. **Fixed needs evidence** (validator on `parties`). `fixed_by_panel` counts the connected panels that passed their own test. Any `fixed` above that number needs `version_seen` outside `affected_versions` and `sounder_test` true. `installed` is never below `connected_unfixed`.
3. **Edge sanity** (validator on `edges`). Quantities are non-negative integers and `corroborated_qty` is never larger than `sender_qty`.
4. **Close gate** (validator on `recalls`). `status` can be `closed` only when `open_units` is 0.
5. **One live attempt per party** (partial unique index on `attempts`). A second attempt cannot be opened while one is in intent, dialed or connected.

```latex
shipped\_in = in\_stock + installed + fixed + returned + forwarded + unresolved
```

### How a transfer is booked

Each party's books follow its own claim, and the gap lives on the edge.

- The sender's `forwarded` equals its own `sender_qty`.
- The recipient's `shipped_in` equals `corroborated_qty`, the smaller of the two claims.
- `disputed_qty` is the difference between the two claims.
- An installer that installs panels at a building books them as forwarded to the building's owner. The owner books them as installed, and later as fixed.

The global check below is asserted in tests after every write. It is the mass balance for the whole recall.

```latex
units\_total = \sum_{parties} (in\_stock + installed + fixed + returned + unresolved) + \sum_{edges} (sender\_qty - corroborated\_qty)
```

### Scenario for the demo

Names are fictional. Direct shipments add up to 240 panels.

| Party | Type | Channel | Shipped in | Scripted answer |
| --- | --- | --- | --- | --- |
| Harbour Fire | Installer | Voice, played by a judge | 40 | Free; the first answer is expected to be a lie |
| Queen St Alarm | Installer | Simulated | 120 | 50 in stock, 30 returned, 40 installed at Corner Properties |
| Lakeside Systems | Installer | Simulated | 80 | 20 in stock, 60 returned |
| Corner Properties (hidden at the start) | Building owner | Voice, played by a teammate | Discovered mid-run | Received 25, all installed, version 2.4 on the display |

Corner Properties manages several buildings, because one building rarely has that many panels. One of its 25 panels is connected: the simulated panel page. The slice ends with 15 panels disputed and 25 installed and not fixed, so the recall cannot close.

Optional: two database users, one for the API with ledger write access and one for the workers with read-only ledger access.

## Core flows

Eight flows carry the whole system. In each one, a step advances only on evidence written to Atlas.

### Party ladder

| State | Meaning | Evidence that moves it forward |
| --- | --- | --- |
| pending | Known, not yet contacted | A worker opens an attempt |
| contacting | An attempt is live | A tool event or simulated reply arrives |
| reached | The party answered | An accepted claim is posted |
| claimed | Its counts are on the books | Its `unresolved` and `installed` are 0 and all its edges are corroborated |
| reconciled | Every panel at this party is explained and safe | Final state |

### Placing a call

1. The worker inserts an attempt in state `intent`, through the tool gate. The partial unique index refuses a second live attempt for the same party.
2. The worker asks ElevenLabs to place the call, passing `attempt_id` and `party_id` as dynamic variables.
3. The worker saves the returned identifiers and sets the attempt to `dialed`.
4. During the call, every tool webhook carries `attempt_id`, the conversation ID and the Twilio call SID. The API logs each one in `tool_events`.
5. The post-call webhook sets the attempt to `completed`, `no_answer` or `failed`.

Failpoints sit after step 1, between steps 2 and 3, and after step 3. The demo kill uses the middle one: the call exists but the worker never saved it.

### Recovery when a party is adopted

For an attempt named by `in_flight` and still in `intent`:

1. If `tool_events` holds an event with this `attempt_id`, the call happened. Copy its identifiers into the attempt and do not redial.
2. Otherwise, ask Twilio for calls to this party's number that started at or after `intent_at`. If one exists, adopt its SID and do not redial.
3. Otherwise, mark the attempt `abandoned` and open the next one. This is the only path that dials again.

For an attempt in `dialed` or `connected` with no post-call event after two minutes, ask Twilio for the call's status and close the attempt to match. Twilio's list filter works on dates, so compare start timestamps in code.

### Handling a claim

1. The voice agent calls `submit_count` with the spoken numbers and the check answers.
2. The API logs the event, then opens a transaction.
3. Inside it, the API sets the party's buckets and check answers, sets `unresolved` to `shipped_in` minus their sum, creates an edge and a stub party if panels went elsewhere, and inserts the claim as accepted.
4. If Atlas rejects the party update, the transaction aborts. The API then inserts the claim as rejected, with the reason.
5. The API answers the tool with `accepted` and, on rejection, the two values that disagree.
6. The agent speaks the challenge using those two values and asks again. Two challenges at most.

The agent's prompt never contains `shipped_in` or the affected versions. It learns of a mismatch only from the tool response, so every challenge originates in the database. A repeated webhook delivery carries the same `dedupe_key` (attempt ID plus a hash of the values), so it cannot post twice.

### Transfers and disputes

1. A party reports installing panels at, or passing panels to, a named party. In one transaction, the sender's `forwarded` is set, an edge is created in `awaiting_recipient`, and a stub party is created with `shipped_in` 0 and tier 3.
2. A worker sees the new party on the change stream and claims it.
3. The recipient reports what it received. In one transaction, the edge gets `recipient_qty`, `corroborated_qty` and `disputed_qty`, and the recipient's `shipped_in` and buckets are set together.
4. If the two claims differ, the edge is `disputed`. A follow-up attempt goes to each party, telling each the other side's number.
5. A corrected claim from either side re-runs step 3.

A spoken name is matched to existing parties before a stub is created: Atlas Search fuzzy matching if the sandbox has it, otherwise a normalised exact match. Connected panels whose `site_label` matches are linked to the party in the same step.

### Confirming a fix

1. A claim that leaves panels `installed` sets a fix check on the party's timer.
2. At the fix check the party is asked the three checks again.
3. A connected panel reports for itself: its PASS on the fixed version moves one panel to `fixed`.
4. A claim of `fixed` passes the validator only with a version outside the affected list and a sounder test of yes, and never for more panels than the connected ones allow.

### Closing gate

`try_close` recomputes `open_units`, writes it, then tries to set `status` to `closed`. Atlas refuses while `open_units` is above 0. `open_units` is the sum of every party's `unresolved` and `installed`, plus every edge's open or disputed quantity.

Three numbers are reported, never merged into one:

- **Checks completed:** parties in claimed or reconciled, out of all parties.
- **Panels located:** panels in stock, installed, fixed or returned, out of the batch.
- **Panels fixed or returned:** out of the batch.

### Memory

1. When an attempt ends, the worker writes an episode: the party's profile, one or two sentences on what happened, tags, and an embedding.
2. Before contacting a party, the worker runs `$vectorSearch` with that party's profile and keeps the top three valid episodes.
3. A fixed policy turns hits into actions. Tag `forwards_to` creates the named party as a suspected party and contacts it at once. Tag `over_reported` tells the agent to ask for the delivery note number before accepting counts.
4. Run 2 sends a second batch through the same network. It must reach Corner Properties before Queen St Alarm answers. That ordering is the measured proof that memory changed behaviour.

Retrieval is by similarity of profile, so a party never contacted before inherits episodes from similar parties.

## External interfaces

The API serves seven groups of endpoints, and a worker makes two kinds of outbound call: to ElevenLabs and to an order system. Vendor field names below were read from the vendor docs listed under Sources.

### Endpoints served by the API

| Endpoint | Called by | Purpose | Reply |
| --- | --- | --- | --- |
| `POST /tools/submit_count` | ElevenLabs webhook tool | Receive spoken counts and check answers during a call | `accepted`; on rejection also the two values that disagree and a one-line `reason` |
| `POST /tools/submit_answer` | ElevenLabs webhook tool | Record the answer to one open question | `accepted` |
| `POST /hooks/post_call` | ElevenLabs post-call webhook | Close the attempt and store the transcript | HTTP 200 |
| `GET` and `POST /form/{token}` | A judge or teammate in a browser | Fallback form; same claim path as voice | Same accept or reject message |
| `GET /panel/{serial}` and `POST /panels/{serial}/events` | The simulated panel | The panel page and its test events | HTML; `accepted` |
| `POST /admin/recalls`, `/admin/failpoint`, `/admin/reset`, and the admin endpoints in earlier sections | Team laptop | Start a recall, arm a failpoint, reseed | JSON status |
| `GET /wall` and `GET /wall/state` | The screen | The page and its data | HTML and JSON |

### The `submit_count` tool

- **Filled by the model:** `in_stock`, `returned`, `installed`, `fixed`, `forwarded_qty`, `forwarded_to_name`, `received_qty`, and the check answers `model_seen`, `version_seen` and `sounder_test`.
- **Filled from dynamic variables, not by the model:** `attempt_id`, `party_id`, `system__conversation_id` and `system__call_sid`.
- **Header:** a shared secret that the API checks.
- **Timeout:** the vendor page states none. Measure the round trip on the first real call and keep the handler to one transaction.

### Outbound call

- `POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call`
- **Required:** `agent_id`, `agent_phone_number_id`, `to_number`.
- **Used here:** `conversation_initiation_client_data.dynamic_variables` with `attempt_id`, `party_id`, `party_name`, `party_type`, `batch_code` and the open questions.
- **Response:** `success`, `message`, and optionally `conversation_id` and `callSid`. The worker saves both when present. Recovery does not depend on them.
- **Ringing:** the default timeout is 60 seconds. Set `telephony_call_config.ringing_timeout_secs` to 20 for the demo.

### Post-call webhook

- Handle `post_call_transcription` and `call_initiation_failure`. Ignore `post_call_audio`.
- Requests are signed with HMAC in the `ElevenLabs-Signature` header.
- `data.conversation_initiation_client_data.dynamic_variables` carries `attempt_id` back.
- `call_initiation_failure` reports `busy`, `no-answer` or `unknown`. Map these to attempt states `failed` or `no_answer`.
- The endpoint must return HTTP 200.

### Voice agent behaviour

Written today in `agent/prompt.md`.

1. Say it is an automated recall line and that this is a simulation.
2. Name the batch and the model.
3. Ask an installer for counts one at a time: in stock, returned, installed at buildings, passed to another installer.
4. Ask a building owner how many panels it has, then the three checks: model on the front, version on the display, sounder test.
5. Read each answer back digit by digit and wait for a yes.
6. Call `submit_count`.
7. If rejected, state the two values from the tool response and ask again. Two challenges at most.
8. If panels went elsewhere, ask for the name of the building owner or installer.
9. Never explain how to repair or update a panel. Say the installer will arrange it.
10. Thank the person and end. Target length is 45 seconds.

### Phone setup

- A paid Twilio account with one purchased local number, imported into ElevenLabs with its SID and token.
- A purchased number supports inbound and outbound calls. A verified caller ID supports outbound only.
- Twilio's Calls list accepts `to`, `status` and `startTimeAfter` filters, with dates as YYYY-MM-DD in UTC.

### If the phone fails

Each level keeps the same claim path, so the refusal by Atlas is real at every level.

| Level | What the judge does | How |
| --- | --- | --- |
| 1. Browser voice | Talks to the same agent from a laptop | `Conversation.startSession({ agentId, connectionType: "webrtc" })`, or the `<elevenlabs-convai agent-id>` widget |
| 2. Voice out, form in | Hears the agent's question and types the counts | The web form on a laptop |
| 3. Form only | Opens the form from a QR code; up to three judges at once | The web form on their phones |

With no phone, the kill beat moves to the distributor hold: the worker dies after sending, and the worker that adopts it reads back and does not send a second hold.

## Folder structure

One Python package, three kinds of process (`api`, `worker` and `mock`), one screen. Every file below is created today. The repo and the package keep the codename.

```text
level-a/
  README.md  pyproject.toml  .env.example  Makefile
  levela/
    config.py
    clock.py
    db/      client.py  setup.py  seed.py
    ledger/  claims.py  transfers.py  reconciler.py  graph.py  invariants.py
    orchestrator/  worker.py  claim.py  lease.py  planner.py  timers.py  ladder.py  attempts.py  recovery.py  failpoints.py  escalation.py
    harness/  gate.py  brief.py  result.py  budget.py  verify.py  trace.py  saboteur.py
    monitoring/  panels.py  events.py  signals.py  trace_batch.py  link.py
    providers/  voice.py  twilio_probe.py  sim.py  profiles.py  fake_voice.py
    systems/  order_client.py  operations.py  contract.py  adapters.py  canary.py
    memory/  episodes.py  embed.py  retrieve.py
    api/     main.py  tools.py  hooks.py  form.py  panel.py  admin.py  questions.py  wall.py
    mock/    order_system.py
    wall/    index.html  wall.js  wall.css  panel.html
  scenarios/  recall_1.json  recall_2.json  generate.py  recall_long.json
  agent/      prompt.md  tool_submit_count.json  tool_submit_answer.json  repair_prompt.md  planner_prompt.md
  tests/      test_atlas_basics.py test_validator.py test_claims.py test_transfers.py test_reconciler.py
              test_attempts.py test_recovery.py test_memory.py test_api.py e2e_sim.py
              test_long_run.py test_hold.py test_system_change.py test_adapter_memory.py
              test_timers.py test_random_kills.py
              test_claim.py test_adoption.py test_gate.py test_saboteur.py test_capacity.py test_questions.py
              test_signals.py test_panel_fix.py
  scripts/    call_once.py  kill_demo.sh  supervisor.py  replay_run.py  video_shots.md
```

## Test plan

Thirty-seven tests, each with a pass condition someone can check in under five minutes. Tests 1 to 15 are below. Tests 16 to 22 are under Long run, 23 to 33 under Dispatch, and 34 to 37 under Live monitoring.

Tests 1 to 7, 10 and 11 run in `pytest` against the sandbox with the fake voice provider. Tests 8, 9 and 14 need a real phone. Tests 12, 13 and 15 are checked by hand.

| # | What | How to test | Pass when |
| --- | --- | --- | --- |
| 1 | Atlas basics | A two-collection transaction commits and aborts. A validator rejects an insert and an update. A change stream receives an insert. The vector index becomes queryable. `$$NOW` works inside `$expr` in a `findOneAndUpdate` filter. Note whether Atlas Search is available | All pass on the sandbox cluster |
| 2 | Party validator | Update a 40-panel party to 60 in stock, then to valid counts, then write a negative bucket, then claim `fixed` with version 2.4 | The first, third and fourth raise a validation error; the second succeeds |
| 3 | Claim posting | Submit a valid claim. Force an exception mid-transaction. Send the same claim twice | One claim and one party update; nothing written on failure; the duplicate posts once |
| 4 | Transfers | The sender reports 40 installed at a building owner, the recipient reports 25 | The edge shows 25 corroborated and 15 disputed; the recipient's `shipped_in` is 25; the global equation equals 240 after every write |
| 5 | Close gate | Call `try_close` with a dispute open, again with panels installed and not fixed, then after both are settled | The first and second are refused by Atlas; the third closes; the three metrics equal hand-computed values |
| 6 | One live attempt | Open two attempts for one party | The second raises a duplicate-key error |
| 7 | Recovery | With the fake provider, arm each failpoint in turn, kill, restart. Include the case where the provider never received the call | The provider records exactly one call per party in every case |
| 8 | Voice loop | Call a team phone. Say 60 for the 40-panel party, then correct it. Repeat five times, twice with background noise playing | Tool event logged; rejection spoken within about three seconds; numbers read back correctly; four of five runs clean |
| 9 | Post-call webhook | End a real call | The attempt becomes completed; the transcript is stored |
| 10 | Simulated end to end | Run recall 1 with every party simulated | Finishes in under 30 seconds; metrics exact; 15 disputed and 25 installed; the recall is not closable |
| 11 | Memory | Run recall 1, then recall 2. Repeat recall 2 with memory disabled | With memory, Corner Properties' attempt starts before Queen St Alarm's claim; without it, after |
| 12 | Screen | Watch the screen during test 10 | A new party appears within two seconds; the rejection shows in the feed; metrics match the reconciler |
| 13 | Fallback form | Submit 60, then valid counts, through the web form | Same rejection and acceptance as the voice path |
| 14 | Kill demo with a real call | Arm the middle failpoint, start a real call, and let another worker adopt the party | The phone rings once; the claim lands while the worker is down; the attempt is adopted with no redial |
| 15 | Full rehearsal | Run the demo script three times, once on a phone hotspot | Each run under three minutes with every beat landing |

## Build order

The order follows dependencies. It names no owners and no times.

1. Sandbox checks (test 1), the public repo, a hello API on the public host, the agent shell.
2. Ledger: collections, validators, claim posting, transfers, close gate (tests 2 to 5).
3. Attempts with the partial index, the lease claim and renewal, failpoints, the fake voice provider (tests 6 and 25).
4. Voice: tool webhook, read-back, first real call, post-call webhook, web form (tests 8, 9 and 13).
5. Recovery by adoption, the simulator and its profiles, the four-party slice end to end (tests 7, 10 and 24).
6. Clock, timers, ladder, the scenario generator, the 40-party run (tests 16, 21 and 23).
7. Mock order system, operations, holds, adapters, the canary, the change and its repair (tests 17 to 20).
8. Supervisor, invariants, random kills (test 22).
9. Tool gate, task runs, typed results, traces, saboteur, questions, the planner (tests 26 to 33).
10. Monitoring: panel page, events, signal rule, approval, fix by test (tests 34 to 37).
11. Memory: episodes, embedding, vector index, policy, recall 2 (test 11).
12. The screen, then the kill demo with a real call and three full rehearsals (tests 12, 14 and 15).

## Demo script

Three minutes, nine beats, two real calls. The run is already more than an hour old when judges arrive. The live part shows the rejection, the kill, the system change, the panel's own test and the refused close.

| Time | Beat | What the room sees |
| --- | --- | --- |
| 0:00 | Run in progress | The opening sentence. Screen: started at 4:55, the simulated day, 40 parties from 5, steps, kills survived, 0 duplicate effects. Oldest line in the feed: three failed panel tests, one batch, recall approved |
| 0:15 | Judge call | Release the slice parties. A team phone rings. A judge plays Harbour Fire and is invited to lie |
| 0:35 | Caught | Feed: "Rejected by Atlas: 60 reported, 40 shipped". The agent challenges, the judge corrects, the claim is accepted |
| 0:55 | Hidden party | The simulated installers answered in the background. Queen St Alarm's claim has created Corner Properties on screen |
| 1:00 | Kill | Arm the failpoint. A worker dials Corner Properties on a teammate's phone and dies. The phone rings once. The teammate says "we received 25, all installed, the display says 2.4". The edge turns disputed: 15 panels. The lease drains, another worker adopts the party, and there is no second ring |
| 1:40 | A system changes | Switch Distributor North's order system to v2. Feed: the contract failure, the flagged operations, the repaired adapter, the canary matching, and only the flagged operations retried. East then repairs from memory with no model call |
| 2:15 | The panel speaks | On the simulated panel page the teammate presses Apply update, then Run test. Feed: PASS on 2.5, one panel fixed at Corner Properties, with the event id |
| 2:30 | All clear refused | Press Close. Atlas refuses: "NOT ALL CLEAR: 39 panels open (15 disputed, 24 awaiting fix)". The three metrics sit side by side |
| 2:45 | Wrap | One sentence each: what is real, what is simulated, what else it fits |

If time is left, hand the operator page to the judges: lie, kill, break a system, start the saboteur, press Close.

### Lines to say

- **Opening:** "Lie to it, kill it, break the system under it: our agent still won't say 'all clear' until the database shows every faulty fire alarm panel found and fixed."
- **On the rejection:** "The model did not catch that. The database did."
- **On the kill:** "The call was requested and the worker died before saving it. A naive agent redials. Ours finds the evidence first."
- **On the dispatch:** "There is no orchestrator. The boss is one findOneAndUpdate."
- **On the panel:** "A phone answer is a claim. This is the panel's own test, and it outranks the phone."
- **On the gate:** "Thirty-nine panels are still open, so Atlas will not let us say all clear."
- **Wrap:** "Real: the calls, the kills and every refusal by the database. Simulated: the clock, the other parties, the order systems and the panel. A different product is a different recall document on the same engine."

If a beat is not working at judging time, drop its verb from the opening sentence.

### What must be visible on screen

- The raw Atlas validation error in the feed, not a friendly rewrite.
- The duplicate-key error when a second attempt is refused.
- On adoption, one line: documents read, and 0 steps replayed.
- `sent` and `verified` as separate states.
- The word SIMULATED on the panel page and beside every simulated party.
- The memory ablation as a number: contacts to full discovery, with memory and without.

### Fallbacks

| If | Then |
| --- | --- |
| Voice fails in the room | Go down the levels under "If the phone fails": browser voice, then voice out and form in, then the form by QR code |
| The second call is flaky | Corner Properties is simulated; the kill uses the fake provider and shows its call counter |
| Venue Wi-Fi drops | Switch to the phone hotspot; the API is already on a public host |
| The judge will not play along | A teammate plays the installer |
| The repair stalls | Show the last passing run of test 18 and say so |
| The run goes long | Skip the East repair and say it in one sentence |

### Likely questions

- **"Is the check just application code?"** The rule is an Atlas validator. Show the validation error in the feed and the validator in the repo.
- **"Does someone still have to go to each panel?"** Yes, the person already in that building. We do not replace the technician. We replace the weeks of phone calls, and we do not close until both sides agree.
- **"A phone answer could be a lie."** Two parties must agree on every transfer, a fixed claim needs a version and a sounder test, and a connected panel's own test outranks the phone. A consistent liar with a plausible version is not caught on a panel that is not connected.
- **"Monitoring fire panels already exists."** Yes, vendors sell connected monitoring. This is not a monitoring product. It uses test results to start a recall and to prove a fix.
- **"Why does a person approve the recall?"** One failed test must not recall a batch, and a safety recall is the supplier's decision. The system proposes it with evidence.
- **"Killing the worker does not kill the call. What does that prove?"** The risk is the gap between requesting a call and saving it. A restart that trusts its own records redials. Ours checks `tool_events` and Twilio first.
- **"What if two workers take the same party?"** Atlas returns it to one of them (test 25). If both still tried to act, the partial unique index refuses the second attempt (test 6).
- **"What is simulated?"** The clock, 36 parties, the order systems, the shipment records and the panel. Atlas, the calls, the webhooks, the kills and the refusals are real.
- **"What stops the model inventing a mapping?"** The canary. A mapping that does not reproduce a known count is never activated (test 19).
- **"Why Vector Search for the mapping?"** The key is the failure text, which is worded differently from one system to the next. A plain lookup needs an exact match.
- **"How do you know nothing ran twice?"** Four invariants are checked every simulated hour, one of them against the order system's own log.
- **"Does this exist already?"** Recall software, recall voice agents for car dealers and recall-agent prototypes exist. We found none combining calls down a supply chain, two parties checked against each other in the database, survival of kills and repair of a changed system.
- **"Was anyone hurt in the real recalls?"** The notices we read report no incidents. We do not claim otherwise.

## Beyond fire alarm panels

A fire alarm panel recall is one instance of a general problem: an obligation that must reach every party in a chain and be proven complete, when each party holds only part of the truth. A different product is a different recall document: its batch, who was shipped it, its checks, and what counts as fixed.

| Fact | Figure | Source |
| --- | --- | --- |
| US recalls in 2025, five industries | 3,295, up from 3,232 in 2024 | [Sedgwick, Feb 2026](https://www.sedgwick.com/?p=39197) |
| Units recalled, first half of 2026 | 941.2 million across 1,634 recalls | [Sedgwick, Aug 2026](https://www.sedgwick.com/?p=56748) |
| Vehicle recalls completed, recalls begun 2012 to 2022 | 65.8% weighted average | [NHTSA report to Congress](https://www.nhtsa.gov/document/report-congress-improving-vehicle-safety-recall-completion-rates) |
| Consumer product units corrected, whole chain | 65% (865 closed cases, FY 2014 to 2016) | [CPSC workshop report](https://www.cpsc.gov/s3fs-public/Recall_Effectiveness_Workshop_Report-2018.pdf) |

- **Where it fits:** products that move through businesses you can contact, such as installed equipment, medical devices and industrial parts.
- **Where it does not:** products sold to anonymous consumers. That is a different contact problem: millions of individuals instead of 40 businesses.
- **What compounds:** each run leaves a map of who installs for whom, which no shipment record contains. The next recall through the same distributor starts with more names.
- **Limits to state if asked:** the regulator decides when a recall ends, and this produces the supplier's evidence, not the decision. Recall service firms already do this work with call centres. It needs batch-level shipment records to start from.

## First checks on the sandbox

Run these before building on them.

- [ ] Create the sandbox project from the emailed link and note the cluster tier.
- [ ] Run test 1: transaction, validator on insert and update, change stream, vector index.
- [ ] Check `$$NOW` inside `$expr` in a `findOneAndUpdate` filter. If it fails, use the worker's clock for leases.
- [ ] Check whether Atlas Search is available. If not, match names by normalised exact match.
- [ ] Check whether you can create database users.
- [ ] Create the public repo and push the skeleton.
- [ ] Deploy the hello API and open its URL from a phone on mobile data.
- [ ] Choose the model and embedding providers from the partner resources page, and check it for ElevenLabs credits.
- [ ] Ask in Discord whether the seed kit written on Nov 13 may be used. Without a yes, do not open it.

## Open risks and what is unverified

Seventeen items are unverified or risky. Each has a check or a fallback.

| Item | Status | Check or fallback |
| --- | --- | --- |
| Transactions on the sandbox tier | Unverified. Free clusters are three-node replica sets on MongoDB 8.0, and the limits page lists no transaction restriction | Test 1 |
| `$$NOW` in the claim filter | Unverified on the sandbox | Test 1; fall back to the worker's clock |
| Validators on updates | The page read shows inserts only | Test 2 covers updates |
| Free-cluster cap of 100 operations per second | Applies if the sandbox is a free cluster | The budget under One unit of work; the screen polls once a second |
| Atlas Search on the sandbox | Unverified | Normalised exact match for names |
| Tool webhook timeout | Not stated on the vendor page | Measure on the first real call; one transaction per call |
| Speech recognition in a loud room | Untested | Digit read-back; the screen shows the rejection; the levels under "If the phone fails" |
| A phone answer is a claim, not proof | True for every panel that is not connected | Two parties must agree, the three checks, and the panel's own test where connected. State the limit when asked |
| Monitoring is the fully simulated half | Judges may discount it, and connected monitoring is an existing product ([Honeywell CLSS](https://buildings.honeywell.com/dk/en/solutions/fire/clss)) | One strip on screen and two beats. The panel page says SIMULATED |
| Dashboards as the main feature are banned | The screen and the monitoring strip could read as one | Lead with the refusals. The screen supports the demo |
| Simulated clock and mock order systems | Judges may discount simulated systems | Name what is real in the wrap. Each order system is a separate process with its own faults |
| The system change is the guide's own example | Other teams will show a similar repair | Lead with the close gate and the mass balance. Show the canary, which a plain retry does not have |
| Novelty | Recall workflow software, recall voice agents for car dealers and recall-agent projects exist (see Sources) | Say "we found none combining these", never "first" |
| Seed kit written on Nov 13 | It holds code and data made before the event | Ask in Discord before opening it |
| Dispatch addendum and wall spec | They use `stores` and the food names. This doc is the current design | Read `parties` for `stores` |
| The name All Clear | Searches found no recall product or hackathon project with that name. Not checked as a trademark | Enough for a team name |
| Model and embedding providers | Undecided | Choose from the partner resources |

## Sources

Pages opened for this spec:

- [ElevenLabs: outbound call via Twilio](https://elevenlabs.io/docs/eleven-agents/api-reference/integrations/twilio/outbound-call.md)
- [ElevenLabs: server tools](https://elevenlabs.io/docs/eleven-agents/customization/tools/server-tools.md)
- [ElevenLabs: dynamic variables](https://elevenlabs.io/docs/eleven-agents/customization/personalization/dynamic-variables)
- [ElevenLabs: post-call webhooks](https://elevenlabs.io/docs/eleven-agents/workflows/post-call-webhooks)
- [ElevenLabs: Twilio native integration](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration.md)
- [ElevenLabs: burst pricing](https://elevenlabs.io/docs/agents-platform/guides/burst-pricing)
- [Twilio: Call resource](https://www.twilio.com/docs/voice/api/call-resource)
- [Twilio: how fast can I place or receive phone calls](https://support.twilio.com/hc/en-us/articles/223180028-How-Fast-Can-I-Place-or-Receive-Phone-Calls-with-Twilio)
- [MongoDB: validation with query operators](https://www.mongodb.com/docs/manual/core/schema-validation/specify-query-expression-rules/)
- [MongoDB: updates with aggregation pipeline](https://www.mongodb.com/docs/manual/tutorial/update-documents-with-aggregation-pipeline/)
- [MongoDB Atlas: free cluster limits](https://www.mongodb.com/docs/atlas/reference/free-shared-limitations.md)
- [Anthropic: effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: how we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
- [Fire Engineering: Notifier control panel recall](https://www.fireengineering.com/fire-safety/fire-alarm-control-panels-recalled-by-notifier-because-of-alert-failure/)
- [CPSC: Fire-Lite control panel recall](https://www.cpsc.gov/Recalls/2011/Fire-Alarm-Control-Panels-Recalled-by-Fire-Lite-Alarms-Due-to-Alert-Failure)
- [CPSC: Bosch control panel recall](https://www.cpsc.gov/Recalls/2012/fire-control-panels-recalled-by-bosch-security-systems-corp-due-to-alarm-failure-posing)
- [Justia: fire and smoke alarm recalls, including Simplex](https://recalls.justia.com/infants-and-children/fire-or-smoke-alarms/21-30)
- [Fire Industry Association: Bull and Cygnus recall, August 2026](https://www.fia.uk.com/news/product-recall-bull-and-cygnus-fire-alarm-detection-system-fads-2607-0120.html)
- [Inspect Point: Ontario fire alarm inspection requirements 2026](https://www.inspectpoint.com/ontario-fire-alarm-inspection-requirements-2026/)
- [Honeywell: Connected Life Safety Services](https://buildings.honeywell.com/dk/en/solutions/fire/clss)
- [Sedgwick: 2026 State of the Nation recall index](https://www.sedgwick.com/?p=39197)
- [Sedgwick: H1 2026 recall index](https://www.sedgwick.com/?p=56748)
- [NHTSA: report to Congress on recall completion rates](https://www.nhtsa.gov/document/report-congress-improving-vehicle-safety-recall-completion-rates)
- [CPSC: Recall Effectiveness Workshop Report, 2018](https://www.cpsc.gov/s3fs-public/Recall_Effectiveness_Workshop_Report-2018.pdf)
- [Closeout, the closest public recall prototype](https://github.com/ANMOLGUPTA-24/closeout)

Seen in search results only, not opened in full:

- [MongoDB: time series collection limitations](https://www.mongodb.com/docs/manual/core/timeseries/timeseries-limitations/)
- [Spyne: recall AI software for car dealerships](https://www.spyne.ai/blogs/best-recall-software-for-dealerships)
- [Oracle: Product Recall Management](https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/25c/fampr/overview-of-product-recall-management.html)
- [Tavant Recalls](https://platform.softwareone.com/product/recalls/PCP-6862-0457)
- [AWS Builder Center: RecallZero](https://builder.aws.com/content/3AccSg72uPF7h1TswVWutUMZXq1/agents-for-humans-building-recallzero-an-agent-that-finishes-product-recalls)
- [lablab: RecallOps](https://lablab.ai/ai-hackathons/band-of-agents-hackathon/gudman/recallops)
- [Siemens: Cerberus cloud apps](https://sid.siemens.com/api/khub/documents/KPwe9l2REQOFUI~5jttlqA/content)
