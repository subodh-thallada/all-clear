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
