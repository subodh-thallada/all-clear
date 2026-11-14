# All Clear — how the Level A engine becomes All Clear

The authoritative product spec is `../docs/All_Clear_build_spec_and_test_plan.md` (repo root
`docs/`). This file is the builders' contract for turning the Level A code base (a food-recall
agent, copied here without its git history and without the Nov 13 seed kit) into All Clear: a
supplier's recall tool for faulty **fire alarm panels**, with live monitoring. Where this file
and the spec disagree, the spec wins; record any deviation in this file.

The package keeps the codename `levela` and the folder `level-a/` (spec: "The repo and the
package keep the codename"). Everything a person reads — the screen, the panel page, the voice
agent, the form, feed lines, docs, README — says **All Clear**, panels, batch, parties.

## 0. No seed kit

The old code imported `seedkit` (schema constants, the embedder, history loading). It is not in
this repo and must not come back. Move what the engine needs into `levela` itself:
`levela/db/schema.py` (collections, validators, indexes, vector index definition,
`ensure_vector_index`) and `levela/memory/embed.py` (Voyage + local hash embedder). Drop the
16-recall synthetic history: memory fills from runs (recall 1 then recall 2 is the memory proof,
test 11). `load-history` becomes a no-op or is removed; `preflight` keeps its other checks.

## 1. Glossary (old → new) — apply everywhere: code, data, tests, prompts, docs

| Old (Level A) | New (All Clear) |
|---|---|
| collection `stores`, `store_id`, `from_store`/`to_store`, "store" | collection `parties`, `party_id`, `from_party`/`to_party`, "party" |
| store tiers `direct`/level 1–3 | `tier` 1, 2, 3 (ints) and `type` `distributor` / `installer` / `owner` |
| `lot_code`, lot `L-4471`, "lot" | `batch_code`, batch `B-4471`, "batch" |
| boxes | panels (model `DEMO-7`) |
| product "Maple Oat Granola Bars", brand, hazard "Undeclared peanut" | product "DEMO-7 fire alarm control panel", supplier **Kestrel Life Safety** (invented), hazard "The sounder circuit may fail to activate in an alarm on software 2.3 and 2.4" |
| buckets `on_hand`, `sold`, `returned`, `destroyed`, `forwarded`, `unresolved` | `in_stock`, `installed`, `fixed`, `returned`, `forwarded`, `unresolved` (`sold` and `destroyed` are gone) |
| "documented", "recovered" metrics | "panels located" (in stock + installed + fixed + returned), "fixed or returned" |
| "Recall closed" / "RECALL OPEN" | "All clear" / "NOT ALL CLEAR: n panels open (…)" |
| consignees | parties |
| distributor **portal** (`levela/mock/portal.py`, `systems/portal_client.py`, `portal` field, "portal North") | distributor **order system** (`levela/mock/order_system.py`, `systems/order_client.py`, `system` field, "North orders" / "East orders"); env `ORDER_NORTH_URL`, `ORDER_EAST_URL` |
| portal fields `lot`, `qty_in_stock`; v2 `lot_code`, `inventory.available` | `batch`, `qty_in_stock`; v2 `batch_code`, `inventory.available` |
| `orchestrator/boss.py` ("boss") | `orchestrator/planner.py` ("planner"). The old `harness/planner.py` (a subagent's next-action decision) becomes `harness/decide.py` |
| Harbour Market (judge, 40) | **Harbour Fire** — installer, voice (judge), 40 |
| Queen St Grocer (120: 50 on hand, 30 sold, 40 forwarded to Corner Mart) | **Queen St Alarm** — installer, simulated, 120: 50 in stock, 30 returned, 40 installed at Corner Properties |
| Lakeside Foods (80: 20 on hand, 60 destroyed) | **Lakeside Systems** — installer, simulated, 80: 20 in stock, 60 returned |
| Corner Mart (hidden; received 25) | **Corner Properties** — building owner (hidden; teammate on voice): received 25, all installed, display shows 2.4 → 15 disputed + 25 installed-not-fixed |
| Distributor North / East | Distributor North / Distributor East (they answer through their order systems) |
| other 35 food stores | installers (tier 2, from the order systems' account lists) and building owners / further installers (tier 3, named in claims). Invented Toronto-flavoured names (e.g. Bloor Alarm, Annex Fire, Dundas Lofts, Maple Court, Bayview Annex, Spadina Works) |
| phone numbers | stay in the 555-01xx block; `PHONE_HARBOUR` / `PHONE_CORNER` env (both the demo phone today) |
| wall (`levela/wall`, plain JS) | the screen: `web/` (React + three.js), built to `web/dist`, served at `/wall` (contract `web/src/api/types.ts`) |

## 2. The ledger (spec "Data model", "Rules enforced by Atlas", "Checks")

- `recalls` gains `model`, `affected_versions` (`["2.3","2.4"]`), `fixed_version` (`"2.5"`),
  `checks` (the three questions), `source` (`signal` | `manual`), `source_signal`.
- `parties` gains `type`, the new buckets, `version_seen`, `model_seen`, `sounder_test`,
  `affected_versions` (copied from the recall when the party is created), `connected_unfixed`,
  `fixed_by_panel`.
- Conservation: `shipped_in = in_stock + installed + fixed + returned + forwarded + unresolved`.
- **Fixed needs evidence** (validator on `parties`): `fixed ≤ fixed_by_panel` OR
  (`version_seen` is a string not in `affected_versions` AND `sounder_test` is `true`); and
  `installed ≥ connected_unfixed`. The phone can never claim more fixed panels than the evidence
  allows; a connected panel's own test outranks the phone.
- Close gate: `open_units = Σ(unresolved + installed) + Σ edges' open or disputed qty`; `closed`
  only at 0. The headline names the parts: `NOT ALL CLEAR: 39 panels open (15 disputed, 24 awaiting fix)`.
- Party ladder: `claimed` needs `unresolved == 0`, `installed == 0` and all edges corroborated.
- An installer installing at a building books `forwarded` to the owner (edge); the owner books
  `installed`, and later `fixed`. A claim that leaves panels `installed` sets a fix check 48
  simulated hours later (ask the three checks again). Each simulated owner follows a fix script:
  installed first, fixed 2–4 simulated days later (version 2.5, sounder yes).
- `submit_count` fields: `in_stock`, `returned`, `installed`, `fixed`, `forwarded_qty`,
  `forwarded_to_name`, `received_qty`, `model_seen`, `version_seen`, `sounder_test`. On a
  rejection the reply names the two values that disagree (e.g. `reported 60, shipped_in 40`, or
  `version_seen 2.4 is affected`).
- Spoken names are matched to existing parties before a stub is created: Atlas Search fuzzy
  matching when available (`$search` with `fuzzy`), otherwise a normalised exact match.

## 3. Monitoring (spec "Live monitoring")

Collections `panels` (24 connected panels; `_id` = serial), `panel_events` (unique
`(serial, test_id)`; validator on `kind`/`result`), `signals` (validator: `approved` needs
`approved_by`; partial unique: one open signal per batch). Signal rule: ≥3 failed tests at ≥3
sites within 30 simulated days → `proposed` signal with `shipped_to`; later failures join it.
`POST /admin/signals/approve` (a person) sets `approved_by` and creates the recall in one
transaction (`source: signal`). No agent tool can approve; the gate's allowlists never contain
`approve_signal`. A PASS from a panel on the fixed version after the recall opened moves one
panel `installed → fixed` at its party and adds 1 to `fixed_by_panel` (event + panel
`fixed_event` + ledger in one transaction; a repeated `test_id` moves nothing). Linking: when a
claim names an owner, panels whose `site_label` matches are linked (`party_id`), and the party's
`connected_unfixed` counts its linked panels without a passing test. Endpoints:
`GET /panel/{serial}` (the page, served from `web/dist/panel.html`), `GET /panel-api/{serial}`,
`POST /panel-api/{serial}/test`, `POST /panel-api/{serial}/update` (page actions, server-side,
same code path), `POST /panels/{serial}/events` (header: shared secret `PANEL_SECRET`),
`POST /admin/panels/{serial}` (set fault/version). Day 0 of the run: three connected panels at
three sites fail → signal proposed → approved by `operator-1` → recall opens with the 5 names
on the shipment records.

## 4. Dispatch (spec "Dispatch, workers and harness")

Party lease fields `lease.owner`, `lease.until`, `lease.token`, `in_flight`, `priority`,
`open_questions`. In-doubt claim first (`lease.until < $$NOW` and `in_flight != null`, sort
`lease.until`), then the due claim. Lease 8 real seconds, renewed every 2 s; fencing by
`lease.owner` + `lease.token`; recovery at claim time; release + result in one transaction.
Four identical worker processes (`WORKERS=1` fallback). The supervisor starts four, kills one
at random every 2–5 real minutes with SIGKILL (Windows: `TerminateProcess`), restarts it after
3 s; `runs.kills`, `runs.adoptions`. Change stream on `parties` wakes workers; else 500 ms poll.

## 5. Voice (spec "External interfaces", "Voice agent behaviour", "If the phone fails")

ElevenLabs Agents over Twilio. `scripts/provision_agent.py` creates/updates the agent, the
`submit_count`, `submit_answer` and `request_callback` webhook tools, the post-call webhook, and
**imports the Twilio number** (`TWILIO_NUMBER`, `TWILIO_SID`, `TWILIO_TOKEN`) into ElevenLabs when
`ELEVEN_PHONE_ID` is empty, printing the ids to put in `.env`. Agent prompt per spec (installer:
counts one at a time; owner: count + the three checks; read back digit by digit; challenge with
the two values from the tool; never explain how to repair or update a panel). Fallback level 1:
browser voice at `/talk/{token}` using the ElevenLabs widget or a signed URL; level 2/3: the form.

## 6. The screen (`web/`)

React + three.js (`@react-three/fiber`, `drei`). `GET /wall` serves `web/dist/index.html`,
`/screen/*` serves `web/dist` assets, `GET /panel/{serial}` serves `web/dist/panel.html`. The
JSON contract is `web/src/api/types.ts`; `levela/api/wall.py` must produce exactly `WallState`.
The 3D view is the recall as a city: the supplier's plant with batch B-4471, the two distributors'
warehouses with their order-system terminals, installers' depots, building owners' buildings with
their panels inside, transfers as arcs (disputed in red), the All Clear dispatch with four worker
towers holding leases on parties (a draining ring), live calls (real vs simulated), connected
panels pulsing their test results, the signal, and the headline the database allows.

## 7. Process and quality rules for builders

- Python: `level-a/.venv/Scripts/python.exe`. Tests: `pytest -q -m "not slow"` from `level-a/`
  against `LEVELA_TEST_URI` (local replica set `mongodb://127.0.0.1:27027/?replicaSet=rs0`).
- Never print or copy secrets from `level-a/.env`. Never place a real phone call. Never run git.
- Keep the spec's test numbers in test names/docstrings (tests 1–37) so the map in README holds.
