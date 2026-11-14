# All Clear: the screen

The room's screen for the recall run. One workflow down the middle, the parties
on the left, the live call on the right. Built to be read in a second.

- **Workflow.** Work comes due, the boss (one `findOneAndUpdate`) gives each
  party to one worker, the worker starts a subagent and calls, Atlas checks the
  count, and the recall can close only at the far end. An edge lights up when
  something travels it.
- **Workers.** Each card shows the subagent starting (Brief, Plan, Gate, Act),
  then Dialing and On call. A dead worker turns red until another takes over.
- **Banner.** One sentence for the event that just mattered.
- **Call panel.** What was said and what Atlas did about it, and a "60 ≠ 40"
  card when a count is refused. "Activity log" has every feed line; click a red
  one for the raw error.

## Run it

```bash
cd level-a/web
npm install
npm run build      # writes web/dist/index.html; the API serves it at GET /wall
```

Open `http://127.0.0.1:8000/wall?key=<ADMIN_TOKEN>` once. The key moves to this
browser's localStorage and leaves the address bar; the buttons then work.

For development:

```bash
npm run dev        # http://localhost:5173              demo data, scripted in the browser
                   # http://localhost:5173/?source=live  the real run, proxied from 127.0.0.1:8000
npm test           # runs the demo engine headless and checks the mass balance at every tick
```

## Two sources

| Source | When | What it shows |
| --- | --- | --- |
| Live | Served at `/wall`, or `?source=live`, or `?api=https://host` | `GET /wall/state` once a second. Never falls back to demo data: if the API stops answering, the top bar says so |
| Demo data | The dev server, `?source=fixture` or `?mock=1` | A run scripted in the browser by `src/sim`. The top bar says "Demo data". **Play demo** plays every beat in order |

`src/data/live.js` is the only file that knows the API's field names. It reads
what `levela/api/wall.py` returns today, and the fuller shape `types.ts`
described. It accepts the fire-alarm names (`installed`, `fixed`, `in_stock`)
and the food names (`on_shelf`, `held`, `sold`); the nouns on screen (tins and
stores, or panels and parties) follow the data.

**Checked against a state shaped like `wall.py`'s output, not against a running
API.** Three things the API does not send yet, and what the screen does without
them:

| Missing from `/wall/state` | On screen |
| --- | --- |
| The words of a call | The call panel shows Atlas's verdicts for that party, and says the words are not sent. To show a transcript, add `turns: [{ role, message }]` to a party's `live_attempt` |
| `lease_until` or a `lease` object per party | The lease bar is full while held; a dead worker's card says the lease is running out, without seconds |
| A heartbeat per worker | A worker is shown dead from the `worker_heartbeat_lost` feed line until it starts again |

## Buttons in live mode

| Button | Request |
| --- | --- |
| Release | `POST /admin/release { recall_id, party_id: "slice" }` |
| Kill the next caller | `POST /admin/failpoint { name: "after_dial_request", channel: "voice" }`. The API cannot kill a process, so the kill beat is the failpoint |
| Change order system | `POST /admin/order-system { order_system: "north", action: "switch_v2" }` |
| Close recall | `POST /admin/close { recall_id }` |
| ⋯ Arm failpoint, Saboteur | `POST /admin/failpoint`, `POST /admin/saboteur { recall_id, on: true, seconds: 20 }` |

Keys: space plays the demo data; 1 release, 2 failpoint, 3 kill, 4 order system,
5 saboteur, 7 close, R resets the demo data.

## Files

```text
src/
  App.jsx               layout
  ui/                   TopBar, StoreList, Flow (workflow, workers, tins bar),
                        Banner, Toolbar, CallPanel, labels, icons, hooks
  data/
    source.js           picks live or demo data
    live.js             the API adapter
    format.js
  sim/                  demo data only: engine, ledger rules, scenario
tests/engine.test.js
```

## Not carried over

This replaces the 3D city screen. The simulated panel page (`panel.html`) is not
here: the API has no `/panel` route for it at the moment. It is in git history
if it is wanted back.
