# Runbook: the nine-beat demo

The operator's script for the three minutes in front of the judges (spec "Demo
script"), and everything that has to be true before they arrive. The long run
(recall `RCL-2026-020`, batch B-4471, 2,400 tins, 40 parties) starts at 4:55
and is more than an hour old when judging begins. The live part shows the
rejection, the kill, the order system change and the refused close.

Every step below is one command. `python scripts/demo.py <beat>` does what the
wall's operator button does, then watches `/wall/state` and prints `ok` or
`MISSING` for each thing the room should see: rehearse with it, and run it in a
side terminal during the show as a checklist. If a button on the wall was
pressed instead, `python scripts/demo.py --check-only <beat>` only checks.
`make <target>` runs the same commands (see `Makefile`); on Windows without
make, run the command shown next to each target.

## People, screens and terminals

| Who / what | Role |
|---|---|
| Operator (C) | Runs the laptop: the terminals and the wall's operator strip (`/wall?key=<ADMIN_TOKEN>`, opened once; the key is then remembered and removed from the address bar) |
| Narrator (A) | Says the lines; watches the wall |
| Phone P1 | Rings as Harbour Market (`PHONE_HARBOUR`); handed to the judge |
| Phone P2 | Rings as Corner Mart (`PHONE_CORNER`); held by teammate B |
| Big screen | The wall, `https://<host>/wall` |
| T1 | `make order systems` (the two mock order systems; their tunnels if the API is hosted) |
| T2 | `make supervisor RECALL=RCL-2026-020` (the worker under random kills); becomes the kill-demo terminal |
| T3 | `python scripts/demo.py ...` (the beats) |

## Pre-show checklist

Do these in order. Tick each one; do not skip a check because "it worked an
hour ago".

| When | Step | Command | Pass when |
|---|---|---|---|
| Night before | Secrets in `.env` and on the host, the same `TOOL_SECRET`, `ADMIN_TOKEN`, `ELEVEN_WEBHOOK_SECRET` | see `deploy/README.md` | `python -m levela status` connects |
| 4:15 | Reset run-time state (history stays; both mock order systems are reset to v1 too) | `make reset` (`python -m levela reset`) | no run-time recall listed; both order systems answer `version 1` |
| 4:15 | Preflight: Atlas, schema, history, vector index | `make preflight` (`python -m levela preflight`) | exit 0, every check ok |
| Once only | Load the synthetic history, if preflight says it is missing | `make seed` (`python -m levela load-history`) | "loaded ... history documents" and the vector index queryable |
| 4:20 | Public URL answers from a phone on mobile data | open `https://<host>/healthz` | `{"ok": true, ...}` |
| 4:20 | Agent points at the current URL | `make provision` (`python scripts/provision_agent.py`) | "updated" for the three tools and the agent, exit 0 |
| 4:25 | Order systems up (and tunnelled if the API is hosted) | T1: `make order systems` (`bash scripts/start_order_systems.sh`, Windows `scripts\start_order_systems.ps1`) | "Both order systems are up" |
| 4:25 | API running | hosted: `/healthz` above; laptop fallback: `make api` | `/wall` loads |
| 4:30 | Smoke call to P1 | `python scripts/call_once.py --watch` | P1 rings; status reaches `completed` |
| 4:55 | **Start the long run**, slice and order system change held | T3: `make demo` (`python scripts/demo.py start --scenario recall_long --mode demo`); finals clock: `make demo SPEED=15` | "started RCL-2026-020 (demo mode ...)"; order system changes "by hand" |
| 4:55 | Worker under random kills | T2: `make supervisor RECALL=RCL-2026-020` (`python scripts/supervisor.py --workers 4 --recall RCL-2026-020`) | feed: "worker restarted" lines over time, kills counter rising |
| 5:00 | The wall is right | T3: `python scripts/demo.py run` | every line `ok` |
| Before judges | Numbers to quote | T3: `make status` (`python scripts/demo.py status`) | invariants 4 of 4, duplicates 0 |
| Before judges | Phones | | charged, ringer on, the Twilio number saved as a contact on both (call screening) |
| Optional, 20 s before the judge's call | Saboteur | T3: `python scripts/demo.py saboteur --seconds 20` | feed: `REFUSED ... Rule: ...` |

Random kills stay on until the judge's call. The `judge` beat pauses them; the
kill beat uses the failpoint, so its timing is exact.

## The show

| Time | Beat | Operator | The room sees | Say |
|---|---|---|---|---|
| 0:00 | Run in progress | T3: `python scripts/demo.py run` (check only) | Started at 4:55, the simulated day, 40 parties from 5, steps, kills survived, 0 duplicate effects | "Nothing counts in a recall until every tin is explained. This agent will not close one until the mass balance holds." |
| 0:15 | Judge call | Button **Release Harbour Market** (it also pauses random kills and releases Lakeside Foods), or T3: `python scripts/demo.py judge` | P1 rings. Hand it to the judge: "You manage Harbour Market. Feel free to lie about your stock." | |
| 0:35 | Caught | T3: `python scripts/demo.py caught` (watches) | Feed: rejected by Atlas, 60 reported, 40 shipped. The agent says both numbers; the judge corrects; accepted | "The model did not catch that. The database did." |
| 1:00 | Hidden party | T2: Ctrl+C the supervisor (it stops its worker), then `make kill-demo RECALL=RCL-2026-020` (the wall's **Arm failpoint** button does the same arming and releases Corner Mart and Queen St Grocer, for when the supervisor restarts the worker instead) | `kill_demo` arms the failpoint and releases Queen St Grocer's held reply: her claim creates Corner Mart on the wall | "Queen St Grocer forwarded part of the batch to a party no list had." |
| 1:05 | Kill | (kill_demo runs the worker) T3: `python scripts/demo.py kill` | The worker dials Corner Mart and dies: the `WORKER KILLED (exit 137)` banner. P2 rings once; B says "received twenty-five". The edge turns disputed: 15 tins. Operator presses Enter in T2: the resume line (documents read, nothing replayed), the attempt adopted, no second ring | "The call was requested and the worker died before saving it. A naive agent redials. Ours finds the evidence first." |
| 1:45 | Order system changes | Button **Switch order system to v2** (North), or T3: `python scripts/demo.py order system` | Contract failure (a field missing), operations flagged, the repaired adapter, the canary matching, only the flagged operations retried | "The order system changed under us. A mapping that does not reproduce a count the ledger already trusts is never activated." |
| 2:20 | Memory | Button **Switch East to v2** (secondary row), or T3: `python scripts/demo.py memory` | East orders fails the same way; the saved mapping is retrieved and activated with 0 model calls | "The second order system repaired itself from memory: Vector Search on the failure text, the same canary, no model call." |
| 2:35 | Closing gate | Button **Close recall**, or T3: `python scripts/demo.py close` | Close refused by Atlas; the open tins; documented, recovered and open side by side | "Fifteen tins are unexplained, so Atlas will not let the recall close." |
| 2:50 | Wrap | T3: `python scripts/demo.py wrap` (resumes random kills) | | "What is real here: the calls, the kills and every refusal by the database. What is simulated: the clock, the other parties and the order system." Then: "Today this is food recalls. Next it is any recalled product. Eventually it is any obligation that must reach everyone in a chain and be proven complete." |

After the judges leave: in T2, Ctrl+C the kill-demo worker and run
`make supervisor RECALL=RCL-2026-020` again, so the run keeps surviving kills.

### Beat notes

- **Judge call.** Releasing the whole slice at once would let Queen St
  Grocer's held reply go too, and Corner Mart would be dialled before the
  failpoint is armed. Release Harbour Market (and Lakeside Foods) only; the
  kill beat releases the rest.
- **Hidden party and kill.** `kill_demo` refuses while another worker is
  alive: stop the supervisor first (Ctrl+C in T2). It arms
  `after_dial_request` for `corner-mart`, releases Corner Mart' hold and
  Queen St Grocer's held reply (`--no-release` if Queen St Grocer was already
  released), then runs `python -m levela worker` in the foreground. Exit code
  137 means the failpoint fired: the call exists, the worker never saved it.
  Wait for B to answer and the claim to land (the edge turns disputed while
  the worker is down), then press Enter. Windows:
  `powershell -ExecutionPolicy Bypass -File scripts\kill_demo.ps1 -RecallId RCL-2026-020`.
- **Order system changes.** The switch is held in demo mode (the scheduler would
  otherwise fire it on simulated day 3). The canary compares a count the
  ledger already holds as corroborated for one account with the candidate
  mapping's reading of the order system.
- **Closing gate.** The refusal is Atlas's close-gate validator on `recalls`,
  shown raw in the feed.

## Fallbacks

| If | Then |
|---|---|
| Voice fails in the room | The judge uses the web form on the laptop (Harbour Market's form link from `make demo`'s output, or its QR code at `/form/<token>/qr.svg` on a phone): the same rejection appears on the wall |
| The second phone (Corner Mart) is flaky | Run the kill with Corner Mart simulated: `VOICE_PROVIDER=sim make kill-demo RECALL=RCL-2026-020` (both worker runs dial voice parties through the simulator). The failpoint, the adoption and the call counter of 1 are the same |
| Venue Wi-Fi drops | Laptop to the phone hotspot. The API is on the public host and keeps taking webhooks; the worker reconnects (it is killed and restarted all evening anyway) |
| The judge will not play along | A teammate plays the manager |
| The run goes long | Skip the memory beat and say it in one sentence |
| The order system repair stalls in front of judges | Show the last passing run of test 18 (`python -m pytest tests/test_order_system_change.py -q`) and say so |
| Memory beat cannot run on the long run | Start recall 2 as in the four-party demo: `python scripts/demo.py start --scenario recall_2 --all-sim`; Corner Mart is reached before Queen St Grocer answers |
| `kill_demo` says the worker exited with a code other than 137 | The failpoint did not fire (Corner Mart was dialled before it was armed, or another worker took it). `python -m levela status` shows what is armed; restart with `make supervisor` and show a supervisor kill instead: "restart n" and the resume line |
| The invariant counter turns red | The supervisor stops itself and the failure stays on the wall. Say what it is: the check doing its job. `make status` names the check that failed |
| The hosted API is asleep or down | Wake it with `/healthz` (about a minute). If it is down: the API on the laptop behind a tunnel (`deploy/README.md`, "Backup"), then `make provision` |

## Likely questions

| Question | Answer |
|---|---|
| Is the check just application code? | The rule is an Atlas validator. Show the raw validation error in the feed and the validator in `seedkit/schema.py`. |
| Killing the worker does not kill the call. What does that prove? | The risk is the gap between requesting a call and saving it. A restart that trusts its own records redials. Ours checks `tool_events` and Twilio's call list first. |
| What is simulated? | The clock, 36 of the 40 parties (behaviour profiles), the two distributor order systems and the shipment records. Atlas, the two calls, the webhooks and every kill are real. |
| Is the clock real? | No. Ten days are played in 120 minutes. The calls, the kills and the database refusals are real. |
| Is the order system real? | No. It is a mock written today, run as its own process with its own faults. The repair is checked against a count the ledger already trusts. |
| Why Vector Search for the mapping? | The key is the failure text, which is worded differently from one order system to the next. A plain lookup needs an exact match. |
| What stops the model inventing a mapping? | The canary. A mapping that does not reproduce a known count is never activated (test 19). |
| How do you know nothing ran twice? | Four invariants are checked every simulated hour, one of them against the order system's own log: one live attempt per party, one hold per idempotency key, a tool event behind every accepted claim, and the mass balance. |
| What stops two workers working on the same party? | A lease taken with one atomic update on the database's clock (`$$NOW`), with an epoch that fences every later write. Test 23 races eight workers for one party; test 24 resumes a stale worker whose writes match nothing. |
| What can the model actually do? | Only the tools on its run's list, through one gate: lease, allowlist, budget, idempotency key, then the provider. Closing the recall is on no list. The saboteur breaks one rule at a time and is refused (tests 25 to 29). |
| Does the context grow with the run? | No. Each decision is built from the party's document, its edges, its open questions and at most three episodes. The wall shows the tokens per decision. |
| Where is Vector Search? | Episodes of past contacts and of order system repairs. The second order system's adapter comes from one. |
| Does this exist already? | Recall software and recall-agent prototypes exist. We found none combining live database rejection, mid-run discovery and crash-safe calling. |
| A party could hold stock from another supplier. | Then the over-count is a dispute to investigate, which is what the challenge starts. |
| Who decides when a recall ends? | The regulator. All Clear produces the firm's evidence, not the decision. |
