# Runbook: the nine-beat demo

The operator's script for the three minutes in front of the judges (spec "Demo
script"), and everything that has to be true before they arrive. The long run
(recall `RCL-2026-020`: Maple Farm milk powder 800 g, batch B-4471, 2,400 tins,
40 parties, all invented) starts at 4:55 and is more than an hour old when
judging begins. The live part shows the rejection, the kill, the system change,
the store's own record and the refused close.

Every step below is one command. `python scripts/demo.py <beat>` does what the
operator's button does, then watches `/wall/state` and prints `ok` or `MISSING`
for each thing the room should see: rehearse with it, and run it in a side
terminal during the show as a checklist. If a button was pressed instead,
`python scripts/demo.py --check-only <beat>` only checks. `make <target>` runs
the same commands (see `Makefile`); on Windows without make, run the command
shown next to each target.

## People, screens and terminals

| Who / what | Role |
|---|---|
| Operator (C) | Runs the laptop: the terminals and the operator page (the screen opened with `?key=<ADMIN_TOKEN>`, once; the key is then remembered and removed from the address bar) |
| Narrator (A) | Says the lines; watches the screen |
| Phone P0 | Rings as the plant ("Maple Farm plant, Guelph") when the run starts, before judging; held by a teammate. Its transcript stays one click away on the operator page |
| Phone P1 | Rings as Harbour Market (`PHONE_HARBOUR`); handed to the judge |
| Phone P2 | Rings as Corner Mart (`PHONE_CORNER`); held by teammate B, who also presses the store page's buttons |
| Big screen | The screen, `https://<host>/wall` |
| Second laptop tab | The SIMULATED store page for Corner Mart, `https://<host>/store/<id>` (operator button **Open the store page**) |
| T1 | `make order-systems` (the two mock order systems; their tunnels if the API is hosted) |
| T2 | `make supervisor RECALL=RCL-2026-020` (the worker under random kills); becomes the kill-demo terminal |
| T3 | `python scripts/demo.py ...` (the beats) |
| T4 | `make summit` (Summit Foods' node: its own API, two workers and its own database) and `make store-script` (the connected stores' tills and stock records) |

## Pre-show checklist

Do these in order. Tick each one; do not skip a check because "it worked an
hour ago". The seed kit written on Nov 13 is not used (spec "First checks on
the sandbox": not without a yes), so there is no history to load: memory starts
empty and fills from the runs of the day (recall 1, then recall 2).

| When | Step | Command | Pass when |
|---|---|---|---|
| Night before | Secrets in `.env` and on the host, the same `TOOL_SECRET`, `ADMIN_TOKEN`, `ELEVEN_WEBHOOK_SECRET`, and one `AGENT_LINK_SECRET` for the pairing with Summit | see `deploy/README.md` | `python -m levela status` connects |
| 4:15 | Reset run-time state (both mock order systems are reset to v1 too) | `make reset` (`python -m levela reset`) | no run-time recall listed; both order systems answer `version 1` |
| 4:15 | Collections, validators, indexes | `make setup-db` (`python -m levela setup-db`) | every collection listed |
| 4:15 | Preflight: Atlas, schema, vector index | `make preflight` (`python -m levela preflight`) | exit 0, every check ok |
| 4:20 | Public URL answers from a phone on mobile data | open `https://<host>/healthz` | `{"ok": true, ...}` |
| 4:20 | Agent points at the current URL | `make provision` (`python scripts/provision_agent.py`; it updates the existing agent and number, never duplicates them) | "updated" for the tools and the agent, exit 0 |
| 4:25 | Order systems up (and tunnelled if the API is hosted) | T1: `make order-systems` (`bash scripts/start_order_systems.sh`, Windows `scripts\start_order_systems.ps1`) | "Both order systems are up" |
| 4:25 | API running | hosted: `/healthz` above; laptop fallback: `make api` | `/wall` loads |
| 4:30 | Summit Foods' node | T4: `make summit` (`bash scripts/start_summit.sh`) | its `/agent/health` answers |
| 4:30 | Store records | T4: `make store-script` (`python scripts/store_script.py run`: the connectors and the day-0 complaints; later `make store-script STORE_ARGS=removals`) | store events in the feed; the store page at `/store/<id>` says SIMULATED across the top |
| 4:35 | Smoke call to P1 | `python scripts/call_once.py --watch` | P1 rings; status reaches `completed` |
| 4:55 | **Start the long run**, slice and order system change held | T3: `make demo` (`python scripts/demo.py start --scenario recall_long --mode demo`); finals clock: `make demo SPEED=15` | "started RCL-2026-020 (demo mode ...)"; order system changes "by hand" |
| 4:55 | The plant is called (P0 rings) | automatic: the plant is due as soon as the recall opens | the plant's three answers in the feed; its transcript on the operator page |
| 4:55 | Worker under random kills | T2: `make supervisor RECALL=RCL-2026-020` (`python scripts/supervisor.py --workers 4 --recall RCL-2026-020`) | feed: "worker restarted" lines over time, kills counter rising |
| 5:00 | The screen is right | T3: `python scripts/demo.py run` | every line `ok` |
| Before judges | Numbers to quote | T3: `make status` (`python scripts/demo.py status`) | invariants 4 of 4, duplicates 0, the headline |
| Before judges | Phones | | charged, ringer on, the Twilio number saved as a contact on all three (call screening) |
| Optional, 20 s before the judge's call | Saboteur | T3: `python scripts/demo.py saboteur --seconds 20` | feed: `REFUSED ... Rule: ...` |

Random kills stay on until the judge's call. The `judge` beat pauses them; the
kill beat uses the failpoint, so its timing is exact.

## Operator buttons

The six buttons for the live part (spec "Demo controls"), each with the
command that does the same from T3:

| Button | Does | Same from T3 |
|---|---|---|
| **Release Harbour Market** | Pauses random kills; releases Harbour Market (the judge's call) and Lakeside Foods to the timers | `python scripts/demo.py judge` |
| **Arm failpoint** | Arms `after_dial_request` on Corner Mart, then releases Corner Mart and Queen St Grocer's held reply | `make kill-demo RECALL=RCL-2026-020` (arms, then runs the worker in the foreground) |
| **Switch North to v2** | Switches Distributor North's order system to v2 (East follows from memory) | `python scripts/demo.py order-system`, then `python scripts/demo.py memory` |
| **Saboteur** | A worker breaks one rule on purpose for 20 s; the gate refuses it | `python scripts/demo.py saboteur` |
| **Open the store page** | Opens Corner Mart's SIMULATED store page (`/store/<id>`) | open the URL in the second tab; `python scripts/demo.py store` watches |
| **Close recall** | Asks Atlas to close the recall | `python scripts/demo.py close` |

## The show

| Time | Beat | Operator | The room sees | Say |
|---|---|---|---|---|
| 0:00 | Run in progress | T3: `python scripts/demo.py run` (check only) | Started at 4:55, the simulated day, 40 parties from 5, steps, kills survived, 0 duplicate effects. Oldest feed lines: three complaints classified as a seal fault on B-4471, the recall approved by a person, the plant called | "Lie to it, kill it, break the system under it: our agent still won't say 'all clear' until the database shows every recalled tin off every shelf." |
| 0:15 | Judge call | Button **Release Harbour Market**, or T3: `python scripts/demo.py judge` | P1 rings. Hand it to the judge: "You manage Harbour Market. Feel free to lie about your stock." | |
| 0:35 | Caught | T3: `python scripts/demo.py caught` (watches) | Feed: "Rejected by Atlas: 60 reported, 40 shipped", the raw validation error. The agent says both numbers; the judge corrects; accepted | "The model did not catch that. The database did." |
| 0:55 | Hidden party | T2: Ctrl+C the supervisor (it stops its worker), then `make kill-demo RECALL=RCL-2026-020` (button **Arm failpoint** does the same arming, for when the supervisor restarts the worker instead) | The simulated stores answered in the background. Queen St Grocer's claim has created Corner Mart on screen | "Queen St Grocer passed 40 tins to a store no shipment record had." |
| 1:00 | Kill | (kill_demo runs the worker) T3: `python scripts/demo.py kill` | The worker dials Corner Mart on P2 and dies: the `WORKER KILLED (exit 137)` banner. P2 rings once; B says "we received 25, all still on the shelf". The edge turns disputed: 15 tins. Operator presses Enter in T2: the lease drains, the resume line (documents read, 0 steps replayed), the attempt adopted, no second ring | "The call was requested and the worker died before saving it. A naive agent redials. Ours finds the evidence first." |
| 1:40 | A system changes | Button **Switch North to v2**, or T3: `python scripts/demo.py order-system` | The contract failure, the flagged operations, the repaired adapter, the canary matching, and only the flagged operations retried | "There is no orchestrator. The boss is one findOneAndUpdate." |
| 2:00 | East repairs from memory | T3: `python scripts/demo.py memory` | East's order system fails the same way; the saved mapping is retrieved and activated with 0 model calls | "The second order system repaired itself from memory: Vector Search on the failure text, the same canary, no model call." |
| 2:15 | The store's own record | Button **Open the store page**; B presses **Move one tin to quarantine**, then **Scan a tin at the till**. T3: `python scripts/demo.py store` (watches) | Feed: one tin held at Corner Mart, with the store event's id as evidence; the till refuses the sale (`blocked_sale`, "recalled batch, not for sale") | "A phone answer is a claim. This is the store's own stock record, and it outranks the phone." |
| 2:30 | All clear refused | Button **Close recall**, or T3: `python scripts/demo.py close` | Atlas refuses: "NOT ALL CLEAR: 39 tins open (24 still on a shelf, 15 disputed)". Checks completed, tins located, and held or returned sit side by side | "Thirty-nine tins are still open, so Atlas will not let us say all clear." |
| 2:45 | Wrap | T3: `python scripts/demo.py wrap` (resumes random kills) | | "Real: the calls, the kills and every refusal by the database. Simulated: the clock, the other parties, the order systems and the store's till. A different product is a different recall document on the same engine." |

If time is left, hand the operator page to the judges: lie, kill, break a
system, start the saboteur, press Close. If a beat is not working at judging
time, drop its verb from the opening sentence.

After the judges leave: in T2, Ctrl+C the kill-demo worker and run
`make supervisor RECALL=RCL-2026-020` again, so the run keeps surviving kills.

### Beat notes

- **Judge call.** Releasing the whole slice at once would let Queen St
  Grocer's held reply go too, and Corner Mart would be dialled before the
  failpoint is armed. Release Harbour Market (and Lakeside Foods) only; the
  kill beat releases the rest.
- **Hidden party and kill.** `kill_demo` refuses while another worker is
  alive: stop the supervisor first (Ctrl+C in T2). It arms
  `after_dial_request` for `corner-mart`, releases Corner Mart's hold and
  Queen St Grocer's held reply (`--no-release` if Queen St Grocer was already
  released), then runs `python -m levela worker` in the foreground. Exit code
  137 means the failpoint fired: the call exists, the worker never saved it.
  Wait for B to answer and the claim to land (the edge turns disputed while
  the worker is down), then press Enter. Windows:
  `powershell -ExecutionPolicy Bypass -File scripts\kill_demo.ps1 -RecallId RCL-2026-020`.
- **A system changes.** The switch is held in demo mode (the scheduler would
  otherwise fire it on its simulated day). The canary compares a count the
  ledger already holds as corroborated for one account with the candidate
  mapping's reading of the order system.
- **The store's own record.** Corner Mart is a connected store: its stock
  record is the only thing that can mark a tin held there. The quarantine
  event and the ledger change are one transaction; posting the same event id
  again moves nothing (test 36). The scan after the block is a `blocked_sale`:
  it moves nothing and shows the block works.
- **All clear refused.** The refusal is Atlas's close-gate validator on
  `recalls`, shown raw in the feed, with the headline the database allows.

## Fallbacks

| If | Then |
|---|---|
| Voice fails in the room | Go down the levels under "If the phone fails" in the spec: browser voice, then voice out and the form in, then the form by QR code (Harbour Market's form link from `make demo`'s output, or `/form/<token>/qr.svg` on a phone). The same rejection appears on the screen |
| The second call (Corner Mart) is flaky | Run the kill with Corner Mart simulated: `VOICE_PROVIDER=sim make kill-demo RECALL=RCL-2026-020`. The kill uses the fake provider and shows its call counter of 1; the failpoint and the adoption are the same |
| Venue Wi-Fi drops | Laptop to the phone hotspot. The API is already on the public host and keeps taking webhooks; the worker reconnects (it is killed and restarted all evening anyway) |
| The judge will not play along | A teammate plays Harbour Market's manager |
| The repair stalls in front of judges | Show the last passing run of test 18 (`python -m pytest tests/test_order_system_change.py -q`) and say so |
| The run goes long | Skip the East repair and say it in one sentence |
| The store page does not load, or its buttons do nothing | Post the same two events from T4 with the store script (`make store-script`), or show the last passing run of test 36. Say so |
| Memory beat cannot run on the long run | Start recall 2, as in the four-party demo: `python scripts/demo.py start --scenario recall_2 --all-sim`; Corner Mart is reached before Queen St Grocer answers |
| `kill_demo` says the worker exited with a code other than 137 | The failpoint did not fire (Corner Mart was dialled before it was armed, or another worker took it). `python -m levela status` shows what is armed; restart with `make supervisor` and show a supervisor kill instead: "restart n" and the resume line |
| Summit Foods' node is down | That is test 47's case: the party walks its quiet-agent ladder and its tins stay open, so close is still refused. Restart it with `make summit`; the next report is accepted |
| The invariant counter turns red | The supervisor stops itself and the failure stays on the screen. Say what it is: the check doing its job. `make status` names the check that failed |
| The hosted API is asleep or down | Wake it with `/healthz` (about a minute). If it is down: the API on the laptop behind a tunnel (`deploy/README.md`, "Backup"), then `make provision` |

## Likely questions

| Question | Answer |
|---|---|
| Is the check just application code? | The rule is an Atlas validator. Show the raw validation error in the feed and the validator in `levela/db/schema.py`. |
| Does someone still have to go to each store? | Yes, the staff already in that store pull the tins. We replace the weeks of phone calls, and we do not close until the store's records or both sides of every transfer agree. |
| A phone answer could be a lie. | Two parties must agree on every transfer; `held` needs the batch code read from a tin and a till test, or the store's own stock record; a connected store's records outrank the phone. A consistent liar with a plausible batch code is not caught at a store that is not connected. |
| Why does a person approve the recall? | Three complaints must not recall a batch on their own, and a safety recall is the company's decision. The system proposes it with the evidence; no agent has an approve tool (test 35). |
| Why does it call the plant? | The plant knows which other batches ran with the same fault and what stores must do. Its answers are recorded, its shipped count is checked against our receiving records, and the barcode is proven on one till before blocks go out. |
| Does the AI decide what is wrong with a tin? | Only when the reason code does not. It must cite past complaints that exist; a low-confidence answer goes to the quality lead; a wrong finding is corrected in memory. |
| Killing the worker does not kill the call. What does that prove? | The risk is the gap between requesting a call and saving it. A restart that trusts its own records redials. Ours checks `tool_events` and Twilio's call list first. |
| What if two workers take the same party? | Atlas returns it to one of them (test 25). If both still tried to act, the partial unique index refuses the second attempt (test 6). |
| What is simulated? | The clock, 36 parties, the distributors' order systems, the shipment records and the stores' tills and stock records. Atlas, the calls, the webhooks, the kills and the refusals are real. |
| What is Summit Foods? | A six-store chain running its own copy of the agent: a second copy of the same code with its own database. It takes a sub-recall, reads its own tills and reports signed counts. Its buyer list never leaves its database (test 48). |
| What stops the model inventing a mapping? | The canary. A mapping that does not reproduce a known count is never activated (test 19). |
| Why Vector Search for the mapping? | The key is the failure text, which is worded differently from one system to the next. A plain lookup needs an exact match. |
| How do you know nothing ran twice? | Four invariants are checked every simulated hour, one of them against the order system's own log. |
| Can you stop it? | One button pauses the run. No worker takes new work, calls in flight finish, and resume continues from Atlas (test 40). |
| Who allowed this action? | Run the audit query for any party (`GET /admin/audit/{party_id}`): worker, task run, gate decision, approval and Atlas outcome, in order (test 41). |
| Does this exist already? | Recall software, recall voice agents for car dealers and recall-agent prototypes exist. We found none combining these: calls down a supply chain, two parties checked against each other in the database, the store's own records as proof, survival of kills and repair of a changed system. |
| Was anyone hurt in the real recalls? | The product in the demo is invented. The notices we read report no incidents, and we do not claim otherwise. |
| Who decides when a recall ends? | The regulator. All Clear produces the company's evidence, not the decision. |
