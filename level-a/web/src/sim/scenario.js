// Fixture scenario: batch B-4471 at simulated day 6, 14:00.
//
// Everything in this file is invented test data for running the wall without a
// backend. The quantities follow the build spec: 2,400 tins, 240 to the three
// slice stores, 1,300 to Distributor North, 860 to Distributor East. The numbers
// are checked against the mass-balance equation in tests/engine.test.js.

export const BATCH = 'B-4471';
export const PRODUCT = 'Maple Farm milk powder 800 g';
export const UNITS_TOTAL = 2400;

export const SIM_START = 158; // simulated hours since the run began: day 6, 14:00
export const SEC_PER_SIM_HOUR = 30;
export const REAL_ELAPSED_AT_START_S = 4740; // the run is 01:19:00 old when the wall opens
export const LEASE_TTL_MS = 8000;
export const LEASE_RENEW_MS = 2000;
export const RESTART_AFTER_MS = 3000;

const party = (id, name, o = {}) => ({
  id,
  name,
  type: 'store',
  tier: 2,
  foundVia: 'order_system',
  channel: 'sim', // sim | voice | orders | agent
  real: false, // true: a person answers a real phone
  connected: false, // true: the store's till and stock record report to the recall
  phoneTail: null,
  shippedIn: 0,
  sold: 0,
  onShelf: 0,
  held: 0,
  returned: 0,
  forwarded: 0,
  unresolved: 0,
  heldBySystem: 0,
  soldByTill: 0,
  systemOnShelf: 0,
  batchSeen: null,
  blockTest: null,
  soldSource: null,
  state: 'reconciled', // pending | contacting | reached | claimed | reconciled
  ladderStep: 0,
  nextActionAt: null, // simulated hours
  holdForDemo: false,
  priority: 0,
  reason: null, // new | ladder | removal | dispute | report | repair
  note: null,
  lease: { owner: null, until: -1, token: 0 },
  inFlight: null,
  behavior: null,
  ...o,
});

// name, shipped in, buckets. `via` is the party that supplied it.
const NORTH = [
  ['p-0101', 'Annex Grocer', 110, { sold: 38, held: 30, returned: 12, forwarded: 30 }],
  ['p-0102', 'Junction Market', 120, { sold: 44, held: 40, returned: 24, onShelf: 12 }, 'c'],
  ['p-0103', 'Danforth Fresh', 96, { sold: 30, held: 38, returned: 18, onShelf: 10 }],
  ['p-0127', 'Bloor Foods', 60, { unresolved: 60 }],
  ['p-0105', 'Roncesvalles Pantry', 140, { sold: 51, held: 43, returned: 12, forwarded: 34 }],
  ['p-0106', 'Parkdale Foods', 88, { sold: 29, held: 41, onShelf: 18 }, 'c'],
  ['p-0107', 'St Clair Market', 132, { sold: 40, held: 42, returned: 12, forwarded: 38 }, 'c'],
  ['p-0108', 'Ossington Grocer', 74, { sold: 22, held: 28, onShelf: 24 }],
  ['p-0109', 'College St Foods', 48, { sold: 16, held: 32 }, 'c'],
  ['p-0110', 'Dupont Fine Foods', 104, { sold: 35, held: 45, returned: 10, onShelf: 14 }, 'c'],
  ['p-0111', 'Eglinton Market', 126, { sold: 44, held: 46, returned: 12, forwarded: 24 }, 'c'],
  ['p-0112', 'Weston Road Grocer', 106, { sold: 36, held: 40, onShelf: 30 }],
];

const EAST = [
  ['p-0201', 'Summit Foods', 120, { sold: 31, onShelf: 9, held: 58, returned: 22 }, 'agent'],
  ['p-0202', 'Bayview Grocer', 92, { sold: 28, held: 24, returned: 10, forwarded: 30 }],
  ['p-0203', 'Leslieville Market', 84, { sold: 30, held: 42, onShelf: 12 }],
  ['p-0204', 'Beaches Pantry', 76, { sold: 20, held: 36, onShelf: 20 }, 'c'],
  ['p-0205', 'East York Foods', 98, { sold: 30, held: 40, forwarded: 28 }],
  ['p-0206', 'Scarborough Fresh', 110, { sold: 37, held: 40, forwarded: 33 }, 'c'],
  ['p-0207', 'Kingston Road Foods', 70, { sold: 25, held: 33, onShelf: 12 }, 'c'],
  ['p-0208', 'Victoria Park Grocer', 86, { sold: 27, held: 43, onShelf: 16 }],
  ['p-0209', 'Don Mills Market', 64, { sold: 20, held: 32, returned: 12 }, 'c'],
];

// from, id, name, sender's quantity, recipient's quantity (null: no answer yet), buckets
const TIER3 = [
  ['p-0101', 'p-0301', 'Dundas Mini Mart', 18, 18, { sold: 6, held: 12 }, 'c'],
  ['p-0101', 'p-0302', 'Harbord Variety', 12, 12, { sold: 4, held: 8 }],
  ['p-0105', 'p-0303', 'High Park Convenience', 20, 20, { sold: 8, held: 12 }, 'c'],
  ['p-0105', 'p-0304', 'Sorauren Mini Mart', 14, 14, { sold: 5, onShelf: 9 }, 'c'],
  ['p-0107', 'p-0305', 'Oakwood Variety', 16, 16, { sold: 6, held: 10 }],
  ['p-0107', 'p-0306', 'Vaughan Road Foods', 22, 22, { sold: 6, held: 8, forwarded: 8 }],
  ['p-0306', 'p-0307', 'Cedarvale Corner Store', 8, 8, { sold: 2, held: 6 }],
  ['p-0111', 'p-0308', 'Mount Pleasant Variety', 24, 24, { sold: 9, held: 9, onShelf: 6 }, 'c'],
  ['p-0206', 'p-0309', 'Woodbine Variety', 18, null, {}],
  ['p-0206', 'p-0310', 'Birchmount Mini Mart', 15, 15, { sold: 5, held: 10 }],
  ['p-0202', 'p-0311', 'Pape Variety', 20, 14, { sold: 4, held: 10 }],
  ['p-0202', 'p-0312', 'Broadview Convenience', 10, 10, { sold: 3, held: 7 }],
  ['p-0205', 'p-0313', "O'Connor Variety", 28, 22, { sold: 8, held: 14 }],
];

const REMOVAL_ORDER = [
  'p-0106', 'p-0108', 'p-0110', 'p-0112', 'p-0102', 'p-0103',
  'p-0204', 'p-0208', 'p-0207', 'p-0203', 'p-0308', 'p-0304',
];

function withEvidence(p) {
  if (p.connected) {
    p.heldBySystem = p.held;
    p.soldByTill = p.sold;
    p.systemOnShelf = p.onShelf;
  } else if (p.held > 0 || p.sold > 0) {
    p.batchSeen = BATCH;
    p.blockTest = true;
    p.soldSource = 'till';
    p.soldByTill = 0;
  }
  return p;
}

export function buildScenario() {
  const parties = [];
  const edges = [];

  // Tier 1: on the shipment records at the start. The slice is held for the demo.
  parties.push(
    party('p-harbour', 'Harbour Market', {
      tier: 1, foundVia: 'shipment', channel: 'voice', real: true, phoneTail: '0142',
      shippedIn: 40, unresolved: 40, state: 'pending', holdForDemo: true, reason: 'new',
      nextActionAt: SIM_START, behavior: { kind: 'script', id: 'harbour' },
    }),
    party('p-queen', 'Queen St Grocer', {
      tier: 1, foundVia: 'shipment', shippedIn: 120, unresolved: 120, state: 'pending',
      holdForDemo: true, reason: 'new', nextActionAt: SIM_START,
      behavior: { kind: 'script', id: 'queen' },
    }),
    party('p-lakeside', 'Lakeside Foods', {
      tier: 1, foundVia: 'shipment', shippedIn: 80, unresolved: 80, state: 'pending',
      holdForDemo: true, reason: 'new', nextActionAt: SIM_START,
      behavior: { kind: 'script', id: 'lakeside' },
    }),
    party('dist-north', 'Distributor North', {
      type: 'distributor', tier: 1, foundVia: 'shipment', channel: 'orders',
      shippedIn: 1300, held: 96, heldBySystem: 96, forwarded: 1204, state: 'reconciled',
    }),
    party('dist-east', 'Distributor East', {
      type: 'distributor', tier: 1, foundVia: 'shipment', channel: 'orders',
      shippedIn: 860, held: 60, heldBySystem: 60, forwarded: 800, state: 'reconciled',
    }),
  );

  const addAccount = (from) => ([id, name, shippedIn, buckets, flag]) => {
    const p = party(id, name, { shippedIn, ...buckets, connected: flag === 'c' });
    if (flag === 'agent') {
      p.type = 'chain';
      p.channel = 'agent';
      p.connected = true;
      p.sub = { seq: 14, buyersTold: 15, buyersIdentified: 19, partiesOpen: 3, partiesTotal: 7 };
      p.reason = 'report';
    }
    withEvidence(p);
    parties.push(p);
    edges.push({
      from, to: id, senderQty: shippedIn, recipientQty: shippedIn,
      corroboratedQty: shippedIn, disputedQty: 0, state: 'corroborated',
    });
  };
  NORTH.forEach(addAccount('dist-north'));
  EAST.forEach(addAccount('dist-east'));

  for (const [from, id, name, senderQty, recipientQty, buckets, flag] of TIER3) {
    const answered = recipientQty != null;
    const corroborated = answered ? Math.min(senderQty, recipientQty) : 0;
    const p = party(id, name, {
      tier: 3, foundVia: 'claim', shippedIn: corroborated, ...buckets, connected: flag === 'c',
    });
    withEvidence(p);
    parties.push(p);
    edges.push({
      from, to: id, senderQty, recipientQty, corroboratedQty: corroborated,
      disputedQty: answered ? Math.abs(senderQty - recipientQty) : 0,
      state: !answered ? 'awaiting_recipient' : senderQty === recipientQty ? 'corroborated' : 'disputed',
    });
  }

  const byId = Object.fromEntries(parties.map((p) => [p.id, p]));

  // Open work at day 6, 14:00.
  Object.assign(byId['p-0127'], {
    state: 'contacting', ladderStep: 2, reason: 'ladder', nextActionAt: 177,
    note: 'No answer twice', behavior: { kind: 'answers', claim: { sold: 21, held: 39 } },
  });
  Object.assign(byId['p-0309'], {
    state: 'contacting', ladderStep: 3, reason: 'ladder', nextActionAt: 172,
    note: 'No answer 3 times. Form link sent',
    behavior: { kind: 'answers', claim: { received: 18, sold: 7, held: 11 } },
  });
  for (const id of ['p-0202', 'p-0311', 'p-0205', 'p-0313']) {
    Object.assign(byId[id], { state: 'claimed', reason: 'dispute' });
  }
  byId['p-0202'].note = 'Says it sent 20 to Pape Variety. Pape counted 14';
  byId['p-0311'].note = '6 tins disputed with Bayview Grocer';
  byId['p-0205'].note = "Says it sent 28 to O'Connor Variety, not 22";
  byId['p-0313'].note = '6 tins disputed with East York Foods';
  Object.assign(byId['p-0206'], { state: 'claimed', reason: 'awaiting', note: 'Waiting for Woodbine Variety to say what it received' });
  byId['p-0201'].state = 'claimed';
  byId['p-0201'].nextActionAt = 169; // quiet-agent timer: 12 simulated hours after report 14

  REMOVAL_ORDER.forEach((id, i) => {
    const p = byId[id];
    Object.assign(p, {
      state: 'claimed', reason: 'removal', nextActionAt: SIM_START + 0.02 + i * 0.13,
      behavior: { kind: 'removal' },
    });
  });

  // Order systems. Squares on the wall are these operations.
  const ops = (prefix, verified, sent) => [
    ...Array.from({ length: verified }, (_, i) => ({ id: `${prefix}-${String(i + 1).padStart(2, '0')}`, state: 'verified', adapter: 1 })),
    ...Array.from({ length: sent }, (_, i) => ({ id: `${prefix}-${String(verified + i + 1).padStart(2, '0')}`, state: 'sent', adapter: 1 })),
  ];

  const connected = parties.filter((p) => p.connected && p.type === 'store');
  const systems = {
    phone: { attempts: 58, accepted: 29, toolEvents: 71 },
    north: {
      key: 'north', name: 'North orders', party: 'dist-north', version: 1, status: 'active',
      source: null, canary: null, ops: ops('op-n', 20, 4), note: null,
      canaryAccount: { account: 'N-009', name: 'College St Foods', value: 48 },
    },
    east: {
      key: 'east', name: 'East orders', party: 'dist-east', version: 1, status: 'active',
      source: null, canary: null, ops: ops('op-e', 16, 3), note: null,
      canaryAccount: { account: 'E-003', name: 'Leslieville Market', value: 84 },
    },
    stores: {
      connected: connected.length, events: 903, complaints: 3, signals: 1,
      last: { party: 'Maple Court Market', kind: 'quarantine', id: 't-88213' },
    },
    agents: { party: 'p-0201', name: 'Summit Foods', reports: 14 },
  };

  // What happened before the wall opened, oldest first.
  const feed = [
    { simAt: 8.08, system: 'signal', kind: 'signal_approved', text: '3 complaints from 3 stores, batch B-4471, seal fault. Approved by operator-1. Plant called' },
    { simAt: 59.08, system: 'agent', kind: 'op_verified', text: "handed 120 tins to Summit Foods' agent. Read back verified", atlas: { primitive: 'validator', rule: 'read-back required', collection: 'operations', verdict: 'accepted' } },
    { simAt: 130.33, system: 'agent', kind: 'report', text: 'Summit Foods report 11: 9 on a shelf at 1 store. Its database refused that store\'s "all pulled"', atlas: { primitive: 'transaction', collection: 'claims + parties', verdict: 'committed' } },
    { simAt: 136.5, system: 'store', kind: 'store_event', text: 'Maple Court Market: 1 tin moved to quarantine. 1 tin held, event t-88213', atlas: { primitive: 'transaction', collection: 'store_events + parties', verdict: 'committed' } },
    { simAt: 154.25, system: 'phone', kind: 'consignee_found', text: 'Annex Grocer names "Dundas Mini Mart": new party, tier 3', atlas: { primitive: 'change stream', collection: 'parties', verdict: 'seen' } },
    { simAt: 155.67, system: 'ledger', kind: 'claim_rejected', text: 'REJECTED by Atlas: Document failed validation on parties. Weston Road Grocer reported 130, shipped_in 106', atlas: { primitive: 'validator', rule: 'conservation', collection: 'parties', verdict: 'refused' } },
    { simAt: 156.08, system: 'supervisor', kind: 'kill', text: 'worker w2 killed (9)' },
    { simAt: 156.09, system: 'worker w3', kind: 'lease_adopted', text: 'adopted Bloor Foods: 1 in doubt, 4 documents read, 0 steps replayed', atlas: { primitive: 'findOneAndUpdate', rule: 'lease', collection: 'parties', verdict: 'adopted' } },
    { simAt: 157.33, system: 'phone', kind: 'no_answer', text: 'Bloor Foods: no answer, attempt 2. Next action d7 09:00' },
    { simAt: 158.0, system: 'invariants', kind: 'invariants', text: '4 of 4 hold' },
  ];

  return { parties, edges, systems, feed };
}

// --- What the people on the two real phones say -----------------------------
//
// A step fires `wait` ms after the previous one. `challenge` lines are not
// written here: the engine builds them from the two numbers in the rejection,
// as the voice agent does from the tool response.

const line = (text, wait) => ({ type: 'line', text, wait });
const store = (text, wait) => ({ type: 'store', text, wait });

export const SCRIPTS = {
  harbour: [
    { type: 'ring', wait: 0 },
    { type: 'connect', wait: 2400 },
    line('This is the automated recall line for Maple Farm milk powder, batch B-4471. This call is a simulation, and its words appear on a screen in the room.', 900),
    line('How many tins of that batch are on your shelf right now?', 3600),
    store('Sixty on the shelf.', 2600),
    line('Six, zero. Is that right?', 2000),
    store('Yes.', 1500),
    { type: 'claim', wait: 700, payload: { onShelf: 60 } },
    { type: 'challenge', wait: 1800 },
    store('Sorry. Twenty-two on the shelf.', 3200),
    line('Open the item sales report on the till. How many have sold since the delivery?', 2400),
    store('Eighteen.', 2400),
    line('Two, two on the shelf. One, eight sold. Is that right?', 2200),
    store('Yes.', 1400),
    { type: 'claim', wait: 700, payload: { onShelf: 22, sold: 18, soldSource: 'till' } },
    line('Read me the batch code printed under one tin.', 1800),
    store('B, four four seven one.', 2600),
    { type: 'answer', wait: 600, key: 'batchSeen', value: BATCH },
    line('Pull every tin of that batch and keep them apart from stock. I will check again in four hours.', 1800),
    { type: 'end', wait: 2400 },
  ],
  corner: [
    { type: 'ring', wait: 0 },
    { type: 'connect', wait: 2200 },
    line('This is the automated recall line for Maple Farm milk powder, batch B-4471. This call is a simulation.', 900),
    line('Queen St Grocer names you as a store it passed tins of that batch to. How many did you receive?', 3400),
    store('We received twenty-five. All still on the shelf.', 3000),
    line('Two, five received. Two, five on the shelf. Is that right?', 2400),
    store('Yes.', 1500),
    { type: 'claim', wait: 700, payload: { received: 25, onShelf: 25 } },
    line('Read me the batch code printed under one tin.', 2000),
    store('B, four four seven one.', 2600),
    { type: 'answer', wait: 600, key: 'batchSeen', value: BATCH },
    line('Take those tins off the shelf. Your stock record will tell me when each one is held.', 1800),
    { type: 'end', wait: 2400 },
  ],
};

// Simulated parties do not speak. Their reply is a claim posted through the API.
export const SIM_REPLIES = {
  queen: { sold: 50, held: 30, forwarded: 40, forwardedTo: 'Corner Mart', soldSource: 'till', batchSeen: BATCH, blockTest: true },
  lakeside: { sold: 20, held: 60, soldSource: 'till', batchSeen: BATCH, blockTest: true },
};

// A party the run has not met yet. It is on no shipment record.
export const HIDDEN = {
  'Corner Mart': {
    id: 'p-corner', channel: 'voice', real: true, phoneTail: '0177', connected: true,
    behavior: { kind: 'script', id: 'corner' },
  },
};

// Episodes the fixture returns for a brief. Three at most, as in the build spec.
export const EPISODES = {
  contact: [
    { id: 'e-0101', score: 0.91, text: 'Stores of this size answer after 14:00 on weekdays' },
    { id: 'e-0144', score: 0.88, text: 'A manager over-reported once and corrected on the read-back' },
    { id: 'e-0150', score: 0.84, text: 'Small grocers pass stock to corner shops nearby' },
  ],
  removal: [
    { id: 'e-0212', score: 0.9, text: 'Staff pulled the tins within six hours of the first call' },
    { id: 'e-0219', score: 0.86, text: 'The stock record lags the shelf by about an hour' },
    { id: 'e-0233', score: 0.81, text: 'A till block alone left tins on the shelf' },
  ],
  hidden: [
    { id: 'e-0150', score: 0.93, text: 'Small grocers pass stock to corner shops nearby' },
    { id: 'e-0301', score: 0.87, text: 'A shop on no list answered on the first call' },
    { id: 'e-0101', score: 0.8, text: 'Stores of this size answer after 14:00 on weekdays' },
  ],
  adapterMiss: [],
  adapterHit: [
    { id: 'e-0412', score: 0.93, text: 'v1 paths answer 410. qty_in_stock became inventory.available. Holds need a confirm call' },
  ],
};
