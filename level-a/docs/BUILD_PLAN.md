# All Clear — build plan (backend)

The product spec is `docs/All_Clear_build_spec_and_test_plan.md` at the repo root (Nov 14, food
version: an 800 g tin of milk powder, batch B-4471). It is authoritative. This file is the
builders' contract for getting the existing engine there. Record any deviation here.

**Scope of this build: the backend only, end to end.** Do not modify `level-a/web/` (the React +
three.js screen); it is out of scope for now. The API must still serve `GET /wall/state` with
data that matches the new domain, and a minimal server-rendered simulated store page.

## Where the code is

The engine came from Level A (a food recall: stores, boxes, lot, portal). It was then half
converted toward a fire-alarm-panel version (that conversion is now abandoned). What is done and
stays: collection `parties` (+ `party_id`, `from_party`/`to_party`), `batch_code` / batch
`B-4471`, the distributor **order system** (`levela/mock/order_system.py`,
`levela/systems/order_client.py`, "North orders" / "East orders", env `ORDER_*_URL`), the planner
(`levela/orchestrator/planner.py`; `levela/harness/decide.py` is a subagent's next-action
decision), name matching (`levela/ledger/names.py`, Atlas Search fuzzy with a normalised exact
match fallback), seed-kit removal (`levela/db/schema.py`, `levela/memory/embed.py`). What must
change: every fire-alarm word, bucket and field below goes to the food version.

## Glossary (fire-alarm version in the code → food version in the spec)

| In the code now | Spec (food) |
|---|---|
| panels, DEMO-7, "fire alarm control panel", supplier Kestrel Life Safety, software versions | tins: **Maple Farm milk powder 800 g** (invented), barcode (invented, e.g. `0 62345 18803 5`), batch **B-4471**, defect `seal_fault` ("faulty lid seal"); the company doing the recall: invent one (e.g. Maple Farm Foods); the plant: e.g. "Maple Farm plant, Guelph" (invented) |
| party types `distributor` / `installer` / `owner` (and `part_maker`) | `plant` (tier 0), `distributor`, `store`, `self` (a node's own depot) |
| Harbour Fire, Queen St Alarm, Lakeside Systems, Corner Properties | **Harbour Market**, **Queen St Grocer**, **Lakeside Foods**, **Corner Mart** (the original Level A names) |
| buckets `in_stock`, `installed`, `fixed`, `returned`, `forwarded`, `unresolved` | `sold`, `on_shelf`, `held`, `returned`, `forwarded`, `unresolved` — `installed→on_shelf` (the open bucket), `fixed→held` (needs evidence), `in_stock→held` (merged: stock kept off sale is held), `sold` is new (tins that reached shoppers, bounded by the till) |
| `fixed_by_panel`, `connected_unfixed` | `held_by_system`, `system_on_shelf`; new `sold_by_till` |
| checks `model_seen`, `version_seen`, `sounder_test` | `batch_seen`, `sold_source`, `block_test` |
| recall `model`, `affected_versions`, `fixed_version`, `fix_verified` | `product`, `barcode`, `affected_batches`, `action` (return or destroy), `barcode_verified`, `defect`, `checks` |
| `panels`, `panel_events` | `store_systems`, `store_events`; `complaints` is new |
| `/panel/{serial}`, `/panels/{serial}/events` | `/store/{id}`, `/stores/{id}/events`; `/complaints` is new |
| metrics "panels located", "fixed or returned" | "tins located" (sold + on shelf + held + returned), "held or returned" |
| headline `NOT ALL CLEAR: n panels open (…)` | `NOT ALL CLEAR: n tins open (a not located, b still on a shelf, c disputed)` (non-zero parts only) or `ALL CLEAR` |

## Ledger (spec "Data model", "Rules enforced by Atlas", "Checks", "Proof that a tin left the shelf")

- Conservation: `shipped_in = sold + on_shelf + held + returned + forwarded + unresolved`.
- **Removal needs evidence** (validator on `parties`, insert AND update): `held ≤ held_by_system`
  OR (`batch_seen` equals the party's `batch_code` AND `block_test` is true); `sold ≤ sold_by_till`
  OR `sold_source == "till_report"`; `on_shelf ≥ system_on_shelf`; plus (sub-recall) `sub.state:
  "closed"` needs `on_shelf == 0` and `unresolved == 0`. The batch code is copied onto each party.
- Close gate: `open_units = Σ(unresolved + on_shelf) + Σ edges' open/disputed qty`; `closed` only at 0.
- Party ladder: `claimed` needs `unresolved == 0`, `on_shelf == 0` and all edges corroborated.
- A claim that leaves tins on the shelf sets a removal check 4 simulated hours later; each
  simulated store follows a removal script (on the shelf first, held 4–12 simulated hours later).
- `submit_count` (store): `sold`, `on_shelf`, `held`, `returned`, `forwarded_qty`,
  `forwarded_to_name`, `received_qty`, and the checks `batch_seen`, `sold_source`, `block_test`;
  rejections name the two values that disagree.
- Scenario (spec "Scenario for the demo"): Harbour Market 40 (voice, judge, free); Queen St Grocer
  120 (simulated: 50 sold, 30 held, 40 passed to Corner Mart); Lakeside Foods 80 (simulated: 20
  sold, 60 held); Corner Mart (hidden, connected store, voice teammate): received 25, all still on
  the shelf → slice ends with 15 disputed + 25 on the shelf. Long run: spec "Scenario at scale"
  (2,400 tins: 240 slice, 1,300 North, 860 East; tiers; profiles incl. Summit Foods "own copy").

## Features (see the spec sections)

- Store records: complaints + classification (reason code → `$vectorSearch` on complaint episodes
  → model with cited evidence ids → low confidence asks the quality lead; findings correct memory),
  store systems + events (`baseline`, `sale`, `blocked_sale`, `quarantine`, `customer_return`,
  `delivery`), signals + human approval gate, the plant call (three questions as data, shipped count
  checked against receiving records, person confirms batches/action, `barcode_verified` before
  blocks roll out), the simulated store page. Tests 34–39.
- Dispatch: leases `lease.owner/until/token`, `in_flight`, 8 s, renewal 2 s, in-doubt claim first,
  4 worker processes, supervisor kills/restarts, adoption, planner, gate, saboteur, questions,
  pause/resume (`runs.state`), `GET /admin/audit/{party_id}`. Tests 6, 7, 21–33, 40, 41.
- Sub-recall, agent to agent: nodes (`NODE_ID`, `MONGODB_DB`, `PORT`, `PARENT_URL`), `links`,
  `buyers`, `/agent/recalls`, `/agent/reports`, `/agent/health`, HMAC `X-AllClear-Signature`,
  Summit Foods node (`scenarios/summit.json`), reports as claims (`source: agent`), buyers told,
  quiet-agent ladder, `scripts/scan_names.py`. Tests 43–49.
- Voice + governed memory: prompts for store / plant / buyer (reads the recall's instruction, never
  answers a health question), tools, provisioning (the ElevenLabs agent and the imported Twilio
  number already exist: `ELEVEN_AGENT_ID`, `ELEVEN_PHONE_ID` in `.env` — update, never duplicate;
  English agents must use TTS model `eleven_flash_v2` or `eleven_turbo_v2`), episodes `scope`,
  `expires_at`, `invalidated_by`, TTL index, filter fields. Tests 11, 42.

## Rules for builders

- Python: `level-a/.venv/Scripts/python.exe`; tests from `level-a/`: `pytest -q -m "not slow"`
  against `LEVELA_TEST_URI` (local replica set `mongodb://127.0.0.1:27027/?replicaSet=rs0`).
- Never print or copy secrets from `level-a/.env`. Never place a real phone call. Never run git.
- Keep the spec's test numbers in test names/docstrings (tests 1–49).

## Deviations and status, by workstream

Each workstream appends here under its own heading only. One line per deviation from the spec.

### WS1 ledger core
- schema + claim path landed (levela/db/schema.py, levela/ledger/claims.py `submit_claim` = `post_claim`, transfers, reconciler; scenarios recall_1/recall_2 use party ids harbour-market, queen-st-grocer, lakeside-foods, corner-mart).
- Claim refusal codes: over_count, still_on_shelf, held_needs_evidence, batch_mismatch, block_not_confirmed, sold_needs_till, sub_closed (+ the old transfer codes); results carry `values` {field: v} and `pair` [a, b].
- Removal check: a claim leaving tins on_shelf sets `removal_check_at` and `next_action_at` = sim now + 4 h (`REMOVAL_CHECK_H`); sim script key and purpose is `removal_check` (was fix_check). Ladder/brief should read `removal_check_at` and `on_shelf`.
- metrics() keys: checks_completed, tins_located, held_or_returned, open_units, not_located, on_shelf, disputed_units, mass_balance, status (panels_located/fixed_or_returned/awaiting_fix gone).
- Party `batch_code` is not a required field: a stub built without `product=` has none until its first claim copies the recall's (held on a phone answer is refused while it is missing). Evidence fields default to 0 when absent (`$ifNull`).
- Recall validator requires only batch_code, product, affected_batches, source, units_total, open_units, status; barcode/action/barcode_verified/defect/checks/parent/matched_item are typed when present.
- Added a `claims` validator (source enum incl. the long run's `order_system`, `rep`; status; dedupe_key) — not in the spec's rule list.
- `store_system` and `agent` claims log their own evidence in tool_events (kinds `store_system`, `agent_report`) so invariant 3 holds; for `agent` pass `attempt_id = <link_id>:<seq>` so the dedupe key is the link plus the sequence number.
- Scenario recalls carry `plant` {party_id "maple-farm-plant", name "Maple Farm plant, Guelph"}, `company`, `instruction`, `defect_label`; recall 2 is batch B-5120 (180 tins). Recall 1 numbers: Harbour Market valid answer 10 sold + 30 held; Queen St dispute follow-up 50 sold, 45 held, 25 to Corner Mart.
### WS2 simulator and long run
- Status: `scenarios/generate.py` (food), `recall_long.json`, `tests/fixtures/recall_long_mini.json`, `levela/providers/{profiles,sim}.py` landed; tests 10 (2 s), 16 (14 s) and 22 (72 s, 10 kills, 4 workers) pass.
- recall_long's recall, slice parties, Corner Mart and slice scripts are copied from recall_1.json (WS1's), minus Corner Mart's `removal_check` script, so its 25 tins stay on the shelf; generate.py's check refuses any drift.
- Removal script: `truth.removal.after_h` (4-12 h) counts from when the store first heard of the recall (`heard_at`: its first answered/callback call, or a form link it opened), not from the start of the run. Before the pull a claim reports the final `held` tins as `on_shelf` with `block_test: false`; `sold_source` is always `till_report`.
- A no_answer store that is already in touch (it filled in its form) picks up its removal-check calls; the rep's day-8 report is the store after the pull (`held` with the checks).
- Summit's reports (16, every 12 sim h from d2 11:00; 9 on a shelf from d5; closed at d9 11:00 and again at d9 23:00) are generated in `sim_truth.summit-foods.params.reports`, not read from summit.json; generate.py's main() writes summit.json through WS5's `write_summit(path, seed)`.
- Single-node runs: the simulator (PartyEvents kind `agent_report`) plays Summit's node. On its first due report it creates the parent link `ln-summit` (peer `summit`) if none exists, attaches the party, books the handoff with WS5's `handoff.book_handoff` under the worker's own idempotency key (it stands in for the read-back), then books reports through `node.report.book_report`. Only the newest due report is booked, so a late start skips the backlog. Fallback without WS5: `submit_claim(source="agent")` plus `sub.*` set directly.
- Until the day-2 report, Summit is a phone party (no link at account-read time). A call to it hears "our own agent is handling this" and books no counts.
- Invariant 3 needed claims.EVIDENCE_KINDS to accept WS5's tool_events kind `agent` (fixed by the lead).
- `connected_stores` (14: Corner Mart's page, the 3 complaint stores Maple Court Market, Bayview Corner Store and Spadina Fresh, plus 10 more) is data for WS4. The phone ledger does not read it.
- Test 22 runs four workers, as the spec says (it was 1).
### WS3 API, voice agent, form, wall
- `submit_count` reply keeps `values` (WS1's {field: v}) and adds `pair` and `two_values` ([{field, value}] x 2) beside `reason`; the agent prompt reads "the two values". Rejection feed line: `<name> - REJECTED by Atlas: <errmsg> on parties. <reason>`, raw Atlas error verbatim in `detail`; `claim_rejected`/`close_refused` are tagged system `ledger` (spec screen), no longer `atlas`.
- Voice agent: one prompt, three scripts by `party_type` (store/distributor/self, plant, buyer); new variable `evidence` (WS6 sends it; blank falls back to "three customer complaints, from three different stores"); removal re-check purpose is `removal_check`. Buyer instruction is fixed prompt text (product, batch, do not use it, bring it back for a full refund), not a variable. TTS `eleven_flash_v2`.
- `scripts/provision_agent.py` never creates the agent unless `--create-agent` (no ELEVEN_AGENT_ID and no agent by name exits 1); refuses an English agent with another TTS model and a prompt containing `shipped_in`; a dry run PATCHes `<ELEVEN_AGENT_ID>`.
- Form: checks shown for type `store`/`self`; `sold_source` is a choice "till"/"estimate" stored as `till_report`/`estimate`; `block_test` yes/no to bool. Closed-recall notice says "This recall is closed".
- Questions: tier 0 (the plant) is a valid `applies_to.tier`; a `plant` party never takes counts (`ask_counts` "no" even when the caller asks).
- `/wall/state`: metrics are computed on the wall from party/edge rows (`tins_located`, `held_or_returned`, `on_shelf`, ...) and overwritten by WS1's `metrics()` where keys match; headline from the ledger when it speaks tins. Added (no renames): `parties[].simulated/tag`, `feed[].simulated/tag`, `systems[].line` plus `stores` and `agents` entries, `monitoring` {connected, on_shelf, moved_to_held, last_event, stores, line} (replaces the old monitoring shape the React types still describe), `run.day_label/real_label/clock_label`, `screen` (the spec's text block). New `GET /wall/text`. History recalls (`seed_batch`) never shown.
- SIMULATED = channel `sim` or `order_system`, or `voice` while VOICE_PROVIDER is not elevenlabs. Agent parties (Summit) are not marked simulated.
- `POST /admin/recalls` calls WS4's `ensure_plant_party` (ImportError tolerated) and lists every party with a form token, the plant included. Fixed a broken `$order system` group path in the wall's operations count.
### WS4 store records
- Status: `levela/db/schema_store.py`, `levela/monitoring/{store_events,link,classify,signals,plant}.py`, `levela/api/store.py`, `scripts/store_script.py` landed; tests 34–39 pass (`tests/test_signals.py`, `test_store_records.py`, `test_classify.py`, `test_plant.py`).
- Store events move the party's buckets directly inside the event's transaction (spec: one transaction); they are logged in `store_events`, not as `claims`, so `levela.ledger.rebuild` (claims replay) does not reproduce a connected store's moves.
- A `quarantine` or `sale` also lowers `system_on_shelf` by the tins moved (the stock record shows one fewer on the shelf), else the validator's `on_shelf >= system_on_shelf` would refuse the move.
- `baseline` takes `sold_by_till`/`held_by_system` as max(current, record) and locates unresolved tins the record explains (on_shelf first, then sold, held); `system_on_shelf` is capped at the party's `on_shelf` when the party has fewer tins booked than the record (e.g. a 0-tin stub). Events posted before a connector is linked update the store's own stock record only; the link-time baseline carries it onto the ledger.
- A breach `sale` (after `block_readback_at`) takes the tin from `on_shelf`, else `unresolved`, else `held`; the party goes to `reached`, gets `breach` {event_id, at} and `next_action_at` = sim now; the feed line is level `error`.
- `POST /stores/{id}/events` and `POST /complaints` use the existing `X-Tool-Secret` (no new secret). The page's buttons post to `POST /store/{id}/press?key=<admin token>` (form), never carrying the store secret; without `?key=` the page is read-only.
- Barcode: the first `blocked_sale` (scanned barcode absent or equal to the recall's) sets `barcode_verified` and blocks every other linked connector in the same transaction; before that only the first store may be blocked (`link.block_allowed`). Simulated tills read a block back at once (`block_readback_at` = request time).
- Linking: at approval, before each event (`try_link`), and once per simulated hour from the scheduler (`link.tick`); a claim naming a store links on the next of these, not inside the claim.
- Signal window: complaints carry `day` (simulated day, default 0); the rule keeps `day` within 30 of the newest classified complaint. `shipped_to` comes from `shipments` with a `batch_code` (pre-recall records); when empty, approval fills it from the new recall's tier-1 parties.
- One open signal per batch = partial unique index on `batch_code` while status is proposed or approved; a complaint after approval joins the approved signal.
- Quality-lead question: a `questions` doc `{_id: "ql-<complaint>", to: "quality_lead", recall_id: null}` (no recall yet, no fan-out); a finding answers it. Finding evidence is the new episode `e-<complaint>`. Decider: deterministic similarity vote (confidence = winner share x nearest cosine / 0.42, threshold 0.65); `ModelDecider` only with PLANNER=claude and a key. Retrieval calls `levela.memory.retrieve.retrieve(kind="complaint")` when that parameter exists, else scores locally.
- Plant: created only when the recall names `plant` or came from a signal; channel `sim` in sim runs, `voice` with `plant.phone`/`PLANT_PHONE`, else `form`. Its three questions are `applies_to.tier [0]`. The shipped-count dispute is an edge `plant -> maple-farm-foods` (`kind: "plant_count"`); a plant count above the receiving records also raises the mass balance by the difference (try_close refuses on the balance as well). `apply_answers` runs after each plant answer (`plant.on_answer` hooked in `api/tools.handle_answer`). Person confirmation: `POST /admin/plant/{recall_id}/confirm` {by, batch?, action?}.
- Extra endpoints: `GET /admin/signals`, `POST /admin/stores/{id}/block`, `POST /admin/complaints/{id}/finding`, `POST /admin/complaints/history` (seeds the confirmed past complaints), `POST /admin/plant/{recall_id}/check`.
- Hooks in other files (import + call): `levela/api/scheduler.py` `_tick_run` calls `monitoring.link.tick`; `levela/api/tools.py` `handle_answer` calls `monitoring.plant.on_answer`. The plant hook on `POST /admin/recalls` is WS3's `_ensure_plant`; signal approval calls `ensure_plant_party` itself.
### WS5 sub-recall between nodes
- Status: links/buyers schema, `levela/node/*`, `/agent/*` endpoints, `scenarios/summit.py`, `scripts/scan_names.py`, `scripts/start_summit.sh` landed; tests 43–49 pass (`tests/test_subrecall.py`, `tests/test_scan_names.py`).
- `links.role` is this node's role in the pairing (our link to Summit is `parent`, Summit's to us is `child`); the same `_id` (`ln-summit`) on both sides.
- Reports and the handoff are booked by `levela/node/book.py` (one transaction: party `$set`, claim, tool_events row, open_units), not through `ledger/claims.submit_claim`: a report sets absolute buckets and `sub`, which submit_claim does not carry. The parties validator decides; a Python mirror names the rule (`under_count`/`over_count`, `still_on_shelf`, `held_needs_evidence`, `sold_needs_till`, `sub_closed`) and refuses with `enforced_by: "node"` if a database ever lets one through.
- A `seq` at or below the last accepted is answered `{"accepted": false, "rule": "stale_seq", "ignored": true}` and writes nothing; the same `seq` again returns the first claim (`duplicate: true`).
- Summit's staff counts are booked by the same `node/book.py` (source `sim`), not by the full orchestrator ladder; Summit's store managers are scripted in `child.tick()` from `summit.json` (pull hours, the Over-reporter's day-5 "all pulled", the day-9 last pull).
- WS4's `monitoring.store_events.post_event` did not exist at build time: Summit's store events (`baseline`, `quarantine`, `sale`) are written by `node/child.store_event()` in WS4's `store_events` shape with the ledger change in one transaction.
- Matching: barcode first; else candidates from word overlap over the item file (the normalised-match fallback; no Atlas Search index is created for items) plus `memory.retrieve(kind="item")` when WS6 adds `kind=`, else the same exact cosine locally. Items are `episodes` of kind `item`. A low-confidence match stores the child recall with `match_state: "needs_person"`, 0 units and no parties, and a feed line; the handoff op stays `sent` until a read-back shows `taken` (no person-confirm endpoint yet).
- Child recall: `_id` `<node>-<parent recall>`, `parent` also carries `key` (unique partial index `uniq_parent_key` on `recalls`, so one sub-recall per key) and `node`.
- Quiet ladder: step 1 is `GET /agent/health?ask=report` (the child's next pass sends a report); an unanswered health check moves straight to step 2 (phone the business, number from East's account list) in the same firing; step 3 sets `escalated` and a feed line, no `questions` doc.
- `MONGODB_DB` is mapped to `ATLAS_DB` by `scripts/start_summit.sh` (config reads `ATLAS_DB`); `NODE_ID`, `PARENT_URL`, `AGENT_LINK_SECRET` (or `LINK_SECRET_<PEER>`) and `PORT` added to Settings. Summit's node runs with `SCHEDULER_ENABLED=0` and the fake voice provider for buyers.
- Summit's business line is `555-0400` (not in the staff `555-01xx` range): our ledger holds it from East's account list, so scan_names does not look for it.
- Hooks left to other owners: the order system's account upsert should call `levela.node.links.agent_party_fields(db, system, account)` to make a matched account `channel: "agent"`; the dispatch loop should call `levela.node.worker.act(ctx, party, voice=...)` for due `channel: "agent"` parties.
### WS6 dispatch, harness, governed memory
- Lease fields keep the code's names `lease_owner`, `lease_until`, `lease_epoch` (spec: `lease.owner/until/token`); `lease_epoch` is the fencing token.
- Pause (test 40): a party filter cannot read `runs`, so `claim_next` reads `runs.state` just before its findOneAndUpdate (paused recalls are left out) and again just after; a claim that raced a pause hands its lease back under its fence. A run document without `state` counts as running.
- `POST /admin/pause` / `/admin/resume` take an optional `recall_id` (none: every open recall's run) and report in-flight calls and operations; the run clock (`POST /admin/clock`) is separate and untouched.
- Audit (test 41): one aggregation on `traces` with `$unionWith` over `task_runs`, `attempts`, `operations`, `claims`, plus `feed` lines of kind `atlas_rejected` (a write Atlas refused leaves no other document); approval is the recall's `approved_by` / `source` / `source_signal`. Optional `?recall_id=`.
- Governed memory (test 42): contact and failure episodes are scoped to their party id, so a never-contacted party no longer inherits similar parties' contact episodes (spec Memory "inherits"); complaint, adapter and item episodes are `fleet`. Vendor scope is `vendor:<name>` (helper only, nothing writes it yet).
- Every episode must carry `scope` and `expires_at` to be retrievable: the same filter document (`levela.memory.govern.admissible`) is the `$vectorSearch` pre-filter and the local find(). Writers outside WS6 use `levela.memory.episodes.write_episode(...)` or `govern.governed(...)`.
- Test 42's "classification changes" is tested at the retrieval level (nearest admissible complaint episode), since no WS4 classifier existed when this was written.
- Test 40's "after a resume the run ends as in test 16" is shown on a four-store pool run (every store worked once, one call each); test 16 itself is WS2's long run.
- Party field `order_system_in_stock` is now `order_system_available` (the order system's available stock for an account).
### WS7 ops, scripts, docs
- Seed kit not used (spec "First checks": no yes given): no `make seed`/`dryrun`, no CI dry run, the image copies no `seedkit/` or `data/`; `tests/test_scripts.py::test_the_seed_kit_is_not_used` checks it. `pyproject.toml` still lists `seedkit*` in package discovery (harmless; not WS7's line).
- The spec's Technology list says "52 tests"; its tables number 49, and the README maps 1 to 49.
- Spec test 39's name ("Part maker call and fix canary") is pre-translation; the README calls it "Plant call and barcode check" (`TRANSLATED_NAMES` in `tests/test_docs.py`).
- Dispatch tests 23 to 33 still carry the older plan's numbers in their files (e.g. `test_claim.py` 23/24 are the spec's 25/26); the README says "numbered k there" per row until WS6 renumbers.
- Demo beats: the spec's 1:40 system change is two `scripts/demo.py` commands (`order-system` at 1:40, `memory` at 2:00), so there are ten commands for the nine beats; the store beat (2:15) only watches: the teammate presses the store page's buttons.
- Makefile target `order systems` (two targets by accident) is now `order-systems`; added guarded `summit`, `store-script`, `scan-names` (print which script is missing).
- CI drops the old `node --check levela/wall/wall.js` step (no `levela/wall/` any more; `web/` is out of scope) and adds `bash -n scripts/*.sh`. The Dockerfile now copies `agent/` (the planner and repairer read their prompts).
- Added a one-screen root `README.md` (unowned; the GitHub landing page) pointing at `level-a/README.md`.
- `make store-script` defaults to `STORE_ARGS=run` (WS4's script needs a command); `deploy/render.yaml` gains `AGENT_LINK_SECRET` (sync: false) for `/agent` on the host.
### Lead integration (after all seven workstreams)
- Superseded above: `pyproject.toml` no longer lists `seedkit*`; dispatch tests are renumbered to the spec (test_claim 25/26, test_gate 27–30, test_saboteur 31, test_capacity 32, test_questions 33; the eight-worker kill run is an extension of 22; the four-worker run also carries spec 23). The README rows no longer say "numbered k there".
- `levela/ledger/claims.py` EVIDENCE_KINDS also accepts kind `agent` (WS5's node/book.py and handoff.py), so invariant 3 holds for Summit's handoff and reports (test 16).
- Complaint episodes (`monitoring/classify.py`) and item episodes (`node/match.py`) were written with `expires_at: null` (items also with scope `node:<id>`), which WS6's governed filter never admits: on Atlas the `$vectorSearch` path would return nothing and every complaint would go to the quality lead. Both now carry the governed TTL (365 days) and scope `fleet`; `seed_history` repairs null expiries. Regression: `tests/test_memory_integration.py`.
- Demo config: set `AGENT_LINK_SECRET` in `.env`. Without it the node worker raises "no link secret configured" for Summit (an error feed line) in single-node runs; a missing secret is not treated as a quiet agent, so the simulated long run is unchanged.
- Not done: `level-a/web/` (React screen) still types the fire-alarm shape (out of scope; `/wall/state` added new fields beside the old). Spec tests 8, 9, 14 need a real phone and 12, 15 are checked by hand.
- Result on a local MongoDB 8.0 replica set: fast suite 884 passed, 7 skipped (Atlas-only or no PowerShell); slow suite 10 passed, 1 skipped.
