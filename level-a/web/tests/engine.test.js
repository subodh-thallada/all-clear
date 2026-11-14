// Runs the fixture engine headless through every beat of the demo and checks the
// rules the wall depends on. `npm test`.
import assert from 'node:assert/strict';
import { createEngine } from '../src/sim/engine.js';
import { totals } from '../src/sim/ledger.js';
import { UNITS_TOTAL } from '../src/sim/scenario.js';

const e = createEngine();
const S = () => e._state();
let ticks = 0;

function check() {
  const t = totals(S().parties, S().edges);
  assert.equal(t.balance, UNITS_TOTAL, `mass balance broke at t=${S().t}: ${t.balance}`);
  const live = {};
  for (const a of S().attempts) if (!a.final) live[a.partyId] = (live[a.partyId] || 0) + 1;
  for (const [id, n] of Object.entries(live)) assert.equal(n, 1, `two live attempts for ${id}`);
  for (const p of S().parties) {
    const held = S().workers.filter((w) => w.alive && w.partyId === p.id && w.phase !== 'result');
    assert.ok(held.length <= 1, `two workers hold ${p.id}`);
  }
}

function run(ms) {
  for (let i = 0; i < ms / 100; i += 1) {
    e.advance(100);
    ticks += 1;
    check();
  }
}

// Opening state.
let s = e.getSnapshot();
assert.equal(s.parties.length, 39);
assert.deepEqual(s.metrics.checked, [34, 39]);
console.log('start      ', s.recall.open, 'open:', s.recall.notLocated, 'not located,', s.recall.onShelf, 'on a shelf');

// The whole demo, on autoplay.
e.actions.playAll();
run(20000);
s = e.getSnapshot();
assert.equal(s.call?.partyName, 'Harbour Market');
assert.ok(s.call.rows.some((r) => r.kind === 'atlas' && r.verdict === 'refused'), 'the lie is refused');
const rows = s.call.rows.map((r) => `${r.kind}:${(r.verdict || r.text).slice(0, 24)}`);
const iStore = s.call.rows.findIndex((r) => r.kind === 'store');
const iAtlas = s.call.rows.findIndex((r) => r.kind === 'atlas');
assert.ok(iStore < iAtlas, 'the refusal sits after the count');

run(60000);
s = e.getSnapshot();
assert.ok(s.parties.find((p) => p.id === 'p-corner'), 'Corner Mart was found');
assert.equal(s.parties.length, 40);

run(60000);
s = e.getSnapshot();
const corner = s.parties.find((p) => p.id === 'p-corner');
const cornerAttempts = S().attempts.filter((a) => a.partyId === 'p-corner');
assert.equal(cornerAttempts.length, 1, 'Corner Mart was dialled once, kill or no kill');
assert.equal(corner.shippedIn, 25);
const edge = s.edges.find((x) => x.to === 'p-corner');
assert.equal(edge.disputedQty, 15);
assert.ok(s.events.some((x) => x.kind === 'lease_adopted' && /Corner Mart/.test(x.text)), 'Corner Mart was adopted');
assert.ok(s.run.kills >= 10);

run(120000);
s = e.getSnapshot();
assert.equal(s.systems.north.version, 2);
assert.equal(s.systems.north.source, 'model');
assert.equal(s.systems.east.version, 2);
assert.equal(s.systems.east.source, 'episode e-0412');
assert.ok(s.systems.north.ops.every((o) => o.state === 'verified'));
assert.equal(s.systems.north.ops.filter((o) => o.adapter === 2).length, 4, 'only the flagged operations were retried');
assert.ok(s.events.some((x) => x.kind === 'attempt_refused'), 'the duplicate-key refusal is in the feed');
assert.ok(s.events.some((x) => x.kind === 'close_refused'), 'close was refused');
assert.equal(s.recall.status, 'open');
assert.equal(s.run.duplicates, 0);
assert.equal(s.parties.find((p) => p.id === 'p-corner').held, 1, 'one tin held on the store record');

console.log('after beats', s.recall.open, 'open:', s.recall.notLocated, 'not located,', s.recall.onShelf, 'on a shelf');
console.log('refusals   ', JSON.stringify(s.run.refusals));

// Let the background run settle. Only real disputes and the stores still pulling stay open.
run(20 * 60000);
s = e.getSnapshot();
console.log('settled    ', s.recall.open, 'open:', s.recall.notLocated, 'not located,', s.recall.onShelf, 'on a shelf;', s.metrics.checked.join(' of '), 'checked');
console.log(`ok: ${ticks} ticks, mass balance ${UNITS_TOTAL} at every one`);
