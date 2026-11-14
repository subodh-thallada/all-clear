// Live source: the screen reads Atlas through the API server.
//
//   GET /wall/state            once a second (header X-Admin-Token when a key is set)
//   POST /admin/...            the operator's buttons
//
// `normalize` is the one place that knows the API's field names. It reads what
// levela/api/wall.py returns today, and also the fuller shape the previous
// screen's types.ts described (a lease object per party, an `alive` flag per
// worker), so it keeps working as the API fills in. Field names from either the
// fire-alarm build (installed, fixed, in_stock) or the food recast (on_shelf,
// held, sold) are accepted.
//
// Nothing is invented: what the API does not send is left empty on screen. In
// particular the API does not send the words of a call yet, so in live mode the
// call panel shows what Atlas decided, not a transcript.
//
// Admin key: open the screen once with ?key=<ADMIN_TOKEN>, or paste it when a
// button asks for it. It is checked against GET /admin/status, kept in this
// browser's localStorage and taken out of the address bar.

import { WORDS } from './format.js';

const KEY_STORE = 'allclear.adminKey';
const KEY_REFUSED = 'key refused';
const LEASE_TTL_S = 8;

// The demo's parties, as scripts/demo.py names them. The judge's call releases
// Harbour and Lakeside only; the kill beat arms the failpoint for Corner Mart
// first and only then lets Corner Mart and Queen St Grocer's held reply go, so
// Corner Mart can never be dialled before the failpoint is waiting for it.
const HARBOUR = 'harbour-market';
const FOLLOW_STORE = 'allclear.follow'; // recall id whose real calls' forwards this browser releases
const KILL_PARTY = 'corner-mart';
const KILL_RELEASES = ['corner-mart', 'queen-st-grocer'];
const KILL_FAILPOINT = 'after_dial_request';

const pick = (o, ...keys) => {
  for (const k of keys) if (o?.[k] !== undefined && o[k] !== null) return o[k];
  return undefined;
};

// The API's feed kinds, mapped to the names the screen routes by.
const KIND = {
  party_discovered: 'consignee_found',
  attempt_adopted: 'lease_adopted',
  edge_disputed: 'transfer_disputed',
  order_system_contract_failed: 'contract_failed',
  followups_released: 'released',
  sim_released: 'released',
  recall_closed: 'closed',
};
const REFUSED = new Set(['claim_rejected', 'close_refused', 'attempt_refused', 'claim_duplicate', 'adapter_rejected']);
const SAY = new Set([
  'claim_rejected', 'close_refused', 'recall_closed', 'party_discovered', 'attempt_adopted', 'edge_disputed',
  'worker_heartbeat_lost', 'order_system_contract_failed', 'adapter_activated', 'adapter_rejected', 'attempt_refused',
  'failpoint_fired', 'call_in_doubt', 'needs_human',
]);
const ROUTES = {
  claim_rejected: [['sys>validator', 'validator>ask', 'ask>sys'], 'red'],
  claim_accepted: [['sys>validator', 'validator>ledger', 'ledger>tray', 'ledger>found', 'found>close'], 'green'],
  form_claim: [['sys>validator'], 'ink'],
  transfer_disputed: [['validator>ledger', 'ledger>tray'], 'red'],
  consignee_found: [['ledger>found', 'found>due'], 'ink'],
  attempt_refused: [['sys>validator'], 'red'],
  memory_hit: [['memory>dispatch'], 'ink'],
  close_refused: [['found>close', 'close>clear'], 'red'],
  closed: [['found>close', 'close>clear'], 'green'],
};

export function normalizeParty(p) {
  const food = 'on_shelf' in p || 'held' in p || 'sold' in p;
  const onShelf = pick(p, 'on_shelf', 'installed', 'on_hand') ?? 0;
  const held = food ? (p.held ?? 0) : (p.fixed ?? 0);
  const mid = food ? (p.sold ?? 0) : (p.in_stock ?? 0); // the counted, not-open bucket in the middle of the bar
  const la = p.live_attempt || null;
  const simulated = pick(p, 'simulated') ?? p.channel !== 'voice';
  const ladderStep = p.ladder_step ?? 0;
  return {
    id: pick(p, 'party_id', 'store_id', '_id'),
    name: p.name,
    type: p.type || 'store',
    tier: p.tier ?? 1,
    foundVia: p.found_via ?? (p.discovered_by ? 'claim' : null),
    channel: p.channel === 'order_system' ? 'orders' : p.channel || 'sim',
    real: !simulated,
    connected: !!(p.connected || p.connected_panels),
    phoneTail: p.phone_tail ?? null,
    memory: p.memory || null,
    shippedIn: p.shipped_in ?? 0,
    sold: mid,
    onShelf,
    held,
    returned: (p.returned ?? 0) + (p.destroyed ?? 0),
    forwarded: p.forwarded ?? 0,
    unresolved: p.unresolved ?? 0,
    state: p.state || 'pending',
    ladderStep,
    nextActionAt: p.next_action_at ?? null,
    holdForDemo: !!p.hold_for_demo,
    priority: p.priority ?? 0,
    reason: p.reason ?? (onShelf > 0 && p.state === 'claimed' ? 'removal' : ladderStep > 0 ? 'ladder' : p.state === 'pending' ? 'new' : null),
    note: p.escalation || p.note || null,
    lease: p.lease ? { owner: p.lease.owner ?? null, token: p.lease.token ?? 0, remainingS: p.lease.expired ? -1 : (p.lease.remaining_s ?? null) } : { owner: null, token: 0, remainingS: -1 },
    inFlight: p.in_flight ?? (la ? pick(la, '_id', 'attempt_id') : null) ?? null,
    liveAttempt: la ? { id: pick(la, '_id', 'attempt_id'), state: la.state, sinceS: la.since_s ?? null, turns: la.turns || null } : null,
    workState: la?.state ?? null,
    isNew: false,
  };
}

const short = (id) => {
  const n = /(\d+)\s*$/.exec(id || '');
  return n ? `W${n[1]}` : String(id || '').slice(0, 2).toUpperCase();
};

function numbersIn(line) {
  const d = line.detail && typeof line.detail === 'object' ? line.detail : {};
  const t = line.text || '';
  const reported = pick(d, 'reported_total', 'reported') ?? Number(/reported\D{0,12}(\d+)/i.exec(t)?.[1]);
  const shipped = pick(d, 'shipped_in', 'shipped') ?? Number(/shipped(?:_in)?\D{0,12}(\d+)/i.exec(t)?.[1]);
  return { reported: Number.isFinite(reported) ? reported : null, shippedIn: Number.isFinite(shipped) ? shipped : null };
}

export function createLive(api) {
  const listeners = new Set();
  const t0 = performance.now();
  const now = () => performance.now() - t0;
  let timers = [];
  let last = null;
  let link = { ok: false, host: api ? new URL(api).host : window.location.host, at: null, error: null, note: null };
  let key = loadKey();
  // Asking for the admin key: null, 'missing', 'refused', 'checking', 'wrong' or 'unreachable'.
  let keyAsk = null;
  let pending = null; // the button that was pressed without a working key; it runs once the key is in
  let follow = readStore(FOLLOW_STORE);
  const followed = new Set(); // parties this browser already released after a real call named them
  let pulseSeq = 0;
  const pulses = [];
  const seenLines = new Set();
  const seenAt = {}; // feed line id -> run clock (ms) when this browser first saw it
  const calls = {}; // attempt id -> what the screen knows about that call
  const tokens = {};
  const known = new Set();
  let primed = false;
  let pin = null;
  let focus = null;
  let snapshot = null;

  function loadKey() {
    try {
      const q = new URLSearchParams(window.location.search);
      const fromUrl = q.get('key');
      if (fromUrl !== null) {
        if (fromUrl) window.localStorage.setItem(KEY_STORE, fromUrl);
        else window.localStorage.removeItem(KEY_STORE);
        const url = new URL(window.location.href);
        url.searchParams.delete('key');
        window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
        return fromUrl || null;
      }
      return window.localStorage.getItem(KEY_STORE);
    } catch {
      return null;
    }
  }

  function freshId() {
    const d = new Date();
    const two = (n) => String(n).padStart(2, '0');
    return `RCL-${d.getFullYear()}-${two(d.getMonth() + 1)}${two(d.getDate())}-${two(d.getHours())}${two(d.getMinutes())}${two(d.getSeconds())}`;
  }

  function readStore(name) {
    try {
      return window.localStorage.getItem(name);
    } catch {
      return null;
    }
  }

  const headers = (json) => ({
    Accept: 'application/json',
    ...(json ? { 'Content-Type': 'application/json' } : {}),
    ...(key ? { 'X-Admin-Token': key } : {}),
  });

  const pulse = (wires, tone = 'ink') => {
    pulses.push({ id: ++pulseSeq, wires, tone, at: now() });
    if (pulses.length > 48) pulses.shift();
  };

  const lineId = (l) => l.id || `${l.at}|${l.kind}|${l.text}`;

  function build() {
    const d = last;
    if (!d) return empty();
    const tNow = now();
    const serverNow = Date.parse(d.now) || Date.now();
    const run = d.run || {};
    const rawParties = d.parties || d.stores || [];
    const parties = rawParties.map(normalizeParty);
    const byId = Object.fromEntries(parties.map((p) => [p.id, p]));
    // Real phone calls, newest first (only with the admin key): number tail and, after the call, the words.
    const realCalls = Array.isArray(d.real_calls) ? d.real_calls : [];
    const realById = Object.fromEntries(realCalls.map((c) => [c._id, c]));
    for (const rc of realCalls) {
      const p = byId[rc.party_id];
      if (p && !p.phoneTail && rc.phone_tail) p.phoneTail = rc.phone_tail;
      if (p && !p.lastCall) p.lastCall = rc._id;
    }
    const food = rawParties.some((p) => 'on_shelf' in p || 'held' in p || 'sold' in p) || /milk|tin/i.test(d.recall?.product || '');
    const words = !rawParties.length || food ? WORDS.tins : WORDS.panels;

    // --- feed, oldest first -------------------------------------------------
    const feed = [...(d.feed || [])].reverse();
    const down = new Set();
    const events = feed.map((l) => {
      const id = lineId(l);
      const kind = KIND[l.kind] || l.kind;
      const refused = REFUSED.has(l.kind);
      if (!(id in seenAt)) seenAt[id] = primed ? tNow : tNow - Math.max(0, serverNow - Date.parse(l.at));
      const who = l.detail?.worker || /\b(w(?:orker)?-?\d+)\b/i.exec(`${l.system || ''} ${l.text || ''}`)?.[1];
      if (who && l.kind === 'worker_heartbeat_lost') down.add(who);
      if (who && (l.kind === 'worker_started' || l.kind === 'worker_resumed')) down.delete(who);
      const raw = l.detail ? (typeof l.detail === 'string' ? l.detail : JSON.stringify(l.detail)) : null;
      return {
        id, kind, rawKind: l.kind, text: l.text, system: l.system || l.source || '', level: l.level,
        partyId: l.party_id ?? null, stamp: l.sim_label || '', ageS: Math.max(0, (serverNow - Date.parse(l.at)) / 1000),
        at: seenAt[id], raw, detail: l.detail ?? null,
        atlas: refused ? { primitive: l.kind === 'attempt_refused' || l.kind === 'claim_duplicate' ? 'unique index' : 'validator', collection: l.kind === 'close_refused' ? 'recalls' : 'parties', verdict: 'refused' } : null,
        headline: SAY.has(l.kind) || l.level === 'bad' ? { tone: l.level === 'bad' || refused ? 'red' : l.level === 'good' ? 'green' : 'ink', text: l.text } : null,
      };
    });

    // One write, one movement: a line this browser has not seen before moves a dot.
    for (const e of events) {
      if (seenLines.has(e.id)) continue;
      seenLines.add(e.id);
      const route = ROUTES[e.kind];
      if (primed && route) pulse(route[0], route[1]);
    }

    // --- leases and workers -------------------------------------------------
    const rows = (d.workers || []).filter((w) => !w.saboteur && !/saboteur/i.test(w.id || ''));
    const fuller = rows.some((w) => 'alive' in w);
    for (const w of rows) {
      known.add(w.id);
      const p = byId[w.party_id];
      if (p && !p.lease.owner) p.lease = { owner: w.id, token: pick(w, 'lease_epoch', 'lease_token') ?? 0, remainingS: null };
    }
    for (const p of parties) if (p.lease.owner) known.add(p.lease.owner);
    const ids = [...known].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).slice(0, 4);
    for (const p of parties) {
      const slot = ids.indexOf(p.lease.owner) + 1;
      // A lease this browser has not seen on this party before was just claimed.
      if (primed && slot && tokens[p.id] !== `${p.lease.owner}:${p.lease.token}`) pulse(['due>dispatch', `dispatch>w${slot}`]);
      tokens[p.id] = p.lease.owner ? `${p.lease.owner}:${p.lease.token}` : null;
    }
    const lastClaim = (pid) => [...events].reverse().find((e) => e.partyId === pid && (e.kind === 'claim_rejected' || e.kind === 'claim_accepted'));
    const holds = (p, id) => p.lease.owner === id && (p.lease.remainingS == null || p.lease.remainingS > 0);

    const workers = ids.map((id, i) => {
      const row = rows.find((w) => w.id === id);
      const alive = fuller ? row?.alive !== false : !down.has(id);
      const p = parties.find((x) => holds(x, id));
      const la = p?.liveAttempt;
      const c = la ? calls[la.id] : null;
      const verdict = la ? lastClaim(p.id) : null;
      const party = p && { id: p.id, name: p.name, real: p.real, channel: p.channel, connected: p.connected, type: p.type, tier: p.tier, shippedIn: p.shippedIn, foundVia: p.foundVia };
      return {
        id, slot: i + 1, label: short(id), alive, phase: !alive ? 'down' : p ? (la ? 'acting' : 'decide') : 'idle',
        downForS: null,
        party: alive ? party : null,
        lease: alive && p ? { remainingS: p.lease.remainingS, ttlS: LEASE_TTL_S, token: p.lease.token } : null,
        run: alive && p ? { id: p.inFlight || 'task run', kind: row?.kind || 'contact_party', tools: [], budget: { model: 4, tools: 6 }, used: { model: 0, tools: 0 } } : null,
        memory: null, decision: null, result: null, op: null, adopt: null,
        attempt: alive && la ? {
          id: la.id, recorded: la.state, provider: la.state === 'connected' ? 'connected' : 'ringing', real: p.real, final: false,
          callS: c?.connectedAt != null ? (tNow - c.connectedAt) / 1000 : 0,
          verdict: verdict && (!c || verdict.at >= c.firstSeen) ? (verdict.kind === 'claim_rejected' ? 'refused' : 'accepted') : null,
        } : null,
        lastLine: null,
        ghost: !alive && p ? { party: p.name, attemptId: p.inFlight, state: p.workState, remainingS: p.lease.remainingS, ttlS: LEASE_TTL_S } : null,
      };
    });
    const slotOf = Object.fromEntries(workers.map((w) => [w.id, w.slot]));

    // --- the calls in flight ---------------------------------------------------
    for (const p of parties) {
      const la = p.liveAttempt;
      if (!la) continue;
      const c = (calls[la.id] ||= { attemptId: la.id, partyId: p.id, partyName: p.name, real: p.real, channel: p.real ? 'voice' : p.channel, phoneTail: p.phoneTail, firstSeen: tNow - 1500, connectedAt: null, rows: [], slips: [], lineIds: new Set(), turns: 0 });
      if (la.state === 'connected' && c.connectedAt == null) c.connectedAt = tNow - (la.sinceS ?? 0) * 1000;
      c.state = la.state === 'connected' ? 'connected' : 'ringing';
      c.live = true;
      c.holder = p.lease.owner && workers.find((w) => w.id === p.lease.owner)?.alive ? short(p.lease.owner) : null;
      c.wasHolder = c.holder || c.wasHolder || (p.lease.owner ? short(p.lease.owner) : null);
      c.inDoubt = !c.holder;
      c.endedAt = null;
      // Words, if the API ever sends them: live_attempt.turns = [{ role, message }].
      const turns = la.turns || d.transcripts?.[la.id] || [];
      for (; c.turns < turns.length; c.turns += 1) {
        const tn = turns[c.turns];
        c.rows.push({ n: c.rows.length + 1, kind: tn.role === 'agent' ? 'line' : 'store', text: tn.message, at: tNow });
      }
      if (p.real) focus = la.id;
    }
    // A real call this browser did not see live (opened after it, or between polls).
    for (const rc of [...realCalls].reverse()) {
      if (calls[rc._id]) continue;
      const startedS = Math.max(0, (serverNow - Date.parse(rc.intent_at)) / 1000) || 0;
      const p = byId[rc.party_id];
      calls[rc._id] = {
        attemptId: rc._id, partyId: rc.party_id, partyName: p?.name || rc.party_id, real: true, channel: 'voice',
        phoneTail: rc.phone_tail, firstSeen: tNow - startedS * 1000 - 1500, connectedAt: null, rows: [], slips: [],
        lineIds: new Set(), turns: 0, live: false, endedAt: tNow, state: 'ended',
      };
    }
    for (const c of Object.values(calls)) {
      const rc = realById[c.attemptId];
      if (rc) {
        c.words = (rc.transcript || []).filter((t) => t.message).map((t, i) => ({ n: i + 1, kind: t.role === 'agent' ? 'line' : 'store', text: t.message }));
        c.durationS = rc.call_duration_secs ?? null;
        c.phoneTail = c.phoneTail || rc.phone_tail;
      }
    }
    for (const c of Object.values(calls)) {
      const p = byId[c.partyId];
      if (!p?.liveAttempt || p.liveAttempt.id !== c.attemptId) {
        if (c.live) c.endedAt = tNow;
        c.live = false;
        c.inDoubt = false;
        c.holder = null;
      }
      c.callS = !c.live && c.durationS != null ? c.durationS : c.connectedAt == null ? 0 : ((c.endedAt ?? tNow) - c.connectedAt) / 1000;
      if (!c.live && tNow - (c.endedAt ?? 0) > 8000) continue;
      for (const e of events) {
        if (e.partyId !== c.partyId || e.at < c.firstSeen || c.lineIds.has(e.id)) continue;
        c.lineIds.add(e.id);
        if (e.kind === 'claim_rejected' || e.kind === 'claim_accepted') {
          const n = numbersIn(e);
          const accepted = e.kind === 'claim_accepted';
          c.slips.push({ id: e.id, accepted, reported: n.reported, shippedIn: n.shippedIn, rule: accepted ? null : 'conservation', raw: e.raw, serverText: e.text });
          c.rows.push({ n: c.rows.length + 1, kind: 'atlas', verdict: accepted ? 'committed' : 'refused', text: e.text, at: e.at });
        } else if (e.kind === 'lease_adopted') c.rows.push({ n: c.rows.length + 1, kind: 'atlas', verdict: 'adopted', text: e.text, at: e.at });
        else if (e.kind === 'transfer_disputed') c.rows.push({ n: c.rows.length + 1, kind: 'atlas', verdict: 'disputed', text: e.text, at: e.at });
        else if (e.rawKind === 'sim_reply' || e.rawKind === 'form_claim') c.rows.push({ n: c.rows.length + 1, kind: 'sim', text: e.text, at: e.at });
      }
    }
    if (!focus) focus = parties.find((p) => p.liveAttempt && p.real)?.liveAttempt.id || realCalls[0]?._id || null;
    const shown = (pin && calls[pin]) || (focus && calls[focus]) || null;

    // --- stations: the mark of the newest relevant line -------------------------
    const newest = (...kinds) => [...events].reverse().find((e) => kinds.includes(e.kind));
    const name = (pid) => byId[pid]?.name || '';
    const station = { dispatch: null, memory: null, gate: null, validator: null, ask: null, ledger: null, found: null, close: null };
    const claim = newest('claim_rejected', 'claim_accepted');
    if (claim) {
      const n = numbersIn(claim);
      const refused = claim.kind === 'claim_rejected';
      station.validator = { party: name(claim.partyId), reported: n.reported, shippedIn: n.shippedIn, verdict: refused ? 'refused' : 'valid', rule: null, ms: null, at: claim.at, text: claim.text };
      if (refused) station.ask = { party: name(claim.partyId), n: 1, at: claim.at };
      else station.ledger = { party: name(claim.partyId), collections: 'claims + parties', ms: null, at: claim.at };
    }
    const found = newest('consignee_found');
    if (found) station.found = { yes: true, name: name(found.partyId) || /: (.+?) \(/.exec(found.text)?.[1] || found.text, from: '', at: found.at };
    const mem = newest('memory_hit');
    if (mem) station.memory = { party: name(mem.partyId), episodes: [], at: mem.at };
    const closeLine = newest('close_refused');
    let close = null;
    if (closeLine) {
      close = { n: closeLine.id, at: closeLine.at, open: d.metrics?.open_units ?? 0 };
      station.close = { verdict: 'refused', open: close.open, at: closeLine.at };
    }

    // --- numbers ---------------------------------------------------------------------
    const m = d.metrics || {};
    const sum = (k) => parties.reduce((n, p) => n + p[k], 0);
    const total = d.recall?.units_total ?? m.units_total ?? 0;
    const onShelf = pick(m, 'awaiting_fix', 'on_shelf') ?? sum('onShelf');
    const notLocated = (m.not_located ?? sum('unresolved')) + (m.disputed_units ?? 0);
    const systems = Array.isArray(d.systems) ? d.systems : [];
    const order = (k) => {
      const o = systems.find((x) => new RegExp(`^${k}`, 'i').test(x.system || '') || new RegExp(`^${k}`, 'i').test(x.label || ''));
      if (!o) return { name: '', version: 1, status: 'active', source: null, ops: [] };
      const failed = o.status ? o.status !== 'ok' : !!o.paused;
      return {
        name: `${k[0].toUpperCase()}${k.slice(1)} orders`, version: o.adapter_version ?? 1,
        status: !failed ? 'active' : o.status === 'repairing' ? 'repairing' : 'contract_failed',
        source: o.adapter_source === 'model' ? 'model' : o.adapter_source || null, ops: [],
      };
    };
    const phone = systems.find((x) => x.system === 'phone') || {};
    const inFlight = d.calls_in_flight;
    const liveReal = Array.isArray(inFlight) ? inFlight.filter((c) => c.real).length : inFlight?.real ?? 0;
    const inv = run.invariants || {};
    primed = true;

    return {
      source: 'live', link: { ...link, ageS: link.at == null ? 0 : (tNow - link.at) / 1000 }, admin: !!key, keyAsk, words,
      t: tNow, speed: 1, paused: !!run.paused, playing: false, failpointArmed: !!d.failpoint || Object.keys(d.failpoints || {}).length > 0,
      recall: {
        id: d.recall?._id || '', batch: d.recall?.batch_code || '', product: d.recall?.product || '', unitsTotal: total,
        status: d.recall?.status || m.status || 'open', open: m.open_units ?? onShelf + notLocated, notLocated, onShelf,
        disputed: m.disputed_units ?? 0, close,
      },
      tins: { removed: sum('held') + sum('returned'), sold: sum('sold'), onShelf, notLocated, balance: m.mass_balance ?? total },
      metrics: {
        checked: m.checks_completed || run.checked || [0, parties.length],
        located: pick(m, 'panels_located', 'tins_located') || [0, total],
        removed: pick(m, 'fixed_or_returned', 'held_or_returned') || [0, total],
      },
      run: {
        simNow: run.sim_now ?? 0, simLabel: run.sim_label || '', day: run.day ?? 0, daysTotal: run.days_total ?? 10,
        realElapsedS: run.real_elapsed_s ?? 0, secPerSimHour: run.seconds_per_sim_hour ?? 30,
        steps: run.steps ?? 0, kills: run.kills ?? 0, adoptions: run.adoptions ?? 0, duplicates: run.duplicates ?? 0,
        tokens: run.tokens_per_decision ?? 0, refusals: {}, refused: (d.refusals || []).length || events.filter((e) => e.atlas).length,
        invariants: { at: inv.at ?? 0, passed: Array.isArray(inv.passed) ? inv.passed : [true, true, true, true] },
        leaseTtlS: LEASE_TTL_S, partiesAtStart: run.parties?.at_start ?? 0,
      },
      parties: parties.map((p) => ({ ...p, lease: { ...p.lease, remainingS: p.lease.owner ? (p.lease.remainingS ?? 1) : -1 }, slot: slotOf[p.lease.owner] })),
      edges: d.edges || [], workers,
      saboteur: run.saboteur?.on ? { last: null, forS: 0 } : null,
      capacity: { maxLive: Math.max(1, liveReal), live: liveReal },
      systems: {
        phone: { attempts: phone.attempts ?? 0, accepted: phone.accepted_claims ?? 0 },
        north: order('north'), east: order('east'),
        stores: { connected: 0, events: 0, last: null }, agents: { name: '', reports: 0 }, touched: {},
      },
      station, call: shown && { ...shown, memory: byId[shown.partyId]?.memory || null }, pinned: !!(pin && calls[pin]), lastSlip: null, events, pulses,
      realParties: parties.filter((p) => p.real).map((p) => p.id),
    };
  }

  // Before the first answer the screen is empty. It never shows demo data in its place.
  function empty() {
    const none = { name: '', version: 1, status: 'active', source: null, ops: [] };
    return {
      source: 'live', link: { ...link, ageS: 0 }, admin: !!key, keyAsk, words: WORDS.tins, t: now(), speed: 1, paused: false, playing: false, failpointArmed: false,
      recall: { id: '', batch: '', product: '', unitsTotal: 0, status: 'open', open: 0, notLocated: 0, onShelf: 0, disputed: 0, close: null },
      tins: { removed: 0, sold: 0, onShelf: 0, notLocated: 0, balance: 0 },
      metrics: { checked: [0, 0], located: [0, 0], removed: [0, 0] },
      run: { simNow: 0, simLabel: '', day: 0, daysTotal: 10, realElapsedS: 0, secPerSimHour: 30, steps: 0, kills: 0, adoptions: 0, duplicates: 0, tokens: 0, refusals: {}, refused: 0, invariants: { at: 0, passed: [true, true, true, true] }, leaseTtlS: LEASE_TTL_S, partiesAtStart: 0 },
      parties: [], edges: [], workers: [], saboteur: null, capacity: { maxLive: 1, live: 0 },
      systems: { phone: { attempts: 0, accepted: 0 }, north: none, east: none, stores: { connected: 0, events: 0, last: null }, agents: { name: '', reports: 0 }, touched: {} },
      station: { dispatch: null, memory: null, gate: null, validator: null, ask: null, ledger: null, found: null, close: null },
      call: null, pinned: false, lastSlip: null, events: [], pulses,
    };
  }

  const emit = () => {
    snapshot = build();
    listeners.forEach((l) => l());
  };

  async function poll() {
    try {
      const r = await fetch(`${api}/wall/state`, { headers: headers(false), cache: 'no-store' });
      if (!r.ok) throw new Error(`/wall/state answered ${r.status}`);
      last = await r.json();
      link = { ...link, ok: true, at: now(), error: null };
      followForwards();
    } catch (err) {
      link = { ...link, ok: false, error: err.message };
    }
    emit();
  }

  // After Call Harbour: when a real call names a store that is held for the demo
  // (an edge from a real store to it), release that store so it is called next.
  function followForwards() {
    const d = last;
    const rid = d?.recall?._id;
    if (!key || !rid || follow !== rid) return;
    const parties = d.parties || [];
    const real = new Set(parties.filter((p) => p.channel === 'voice' && !p.simulated).map((p) => p.party_id));
    const held = new Set(parties.filter((p) => p.hold_for_demo).map((p) => p.party_id));
    for (const e of d.edges || []) {
      const once = `${rid}:${e.to_party}`;
      if (!real.has(e.from_party) || !held.has(e.to_party) || followed.has(once)) continue;
      followed.add(once);
      send('/admin/release', { recall_id: rid, party_id: e.to_party }).then((problem) => {
        if (problem) link = { ...link, note: problem === KEY_REFUSED ? 'Buttons locked: the admin key was refused' : problem };
      });
    }
  }

  // One admin request. Returns null when it worked, or the problem in words.
  async function send(path, body) {
    try {
      const r = await fetch(`${api}${path}`, { method: 'POST', headers: headers(true), body: JSON.stringify(body) });
      if (r.ok) return null;
      const text = await r.text();
      if (r.status === 401) return KEY_REFUSED;
      return `${path} answered ${r.status}: ${text.slice(0, 160)}`;
    } catch (err) {
      return `${path}: ${err.message}`;
    }
  }

  // An operator action: one or more requests in order. A party this recall does
  // not have (404 from /admin/release) is skipped; any other failure stops the
  // action and its words go to the top bar. Without a working admin key the
  // screen asks for one, then runs the action.
  const act = (steps, after) => {
    const run = async () => {
      if (!key) {
        keyAsk = 'missing';
        pending = run;
        emit();
        return;
      }
      const rid = last?.recall?._id;
      let note = null;
      for (const [path, body, optional] of steps(rid)) {
        const problem = await send(path, body);
        if (problem === KEY_REFUSED) {
          keyAsk = 'refused';
          pending = run;
          note = 'Buttons locked: the admin key was refused';
          break;
        }
        if (problem && !(optional && / 404:/.test(problem))) {
          note = problem;
          break;
        }
      }
      link = { ...link, note };
      if (!note && after) after();
      poll();
    };
    return run;
  };

  // Check a pasted key against the API, keep it in this browser, then run the waiting action.
  async function setKey(value) {
    const k = (value || '').trim();
    if (!k) return;
    keyAsk = 'checking';
    emit();
    let status = 0;
    try {
      const r = await fetch(`${api}/admin/status`, { headers: { Accept: 'application/json', 'X-Admin-Token': k }, cache: 'no-store' });
      status = r.status;
    } catch {
      status = 0;
    }
    if (status < 200 || status >= 300) {
      keyAsk = status === 401 ? 'wrong' : 'unreachable';
      emit();
      return;
    }
    key = k;
    try {
      window.localStorage.setItem(KEY_STORE, k);
    } catch {
      /* private window: the key lasts until reload */
    }
    keyAsk = null;
    link = { ...link, note: null };
    const next = pending;
    pending = null;
    emit();
    if (next) next();
  }

  const post = (path, body) => act((rid) => [[path, body(rid)]]);

  snapshot = empty();

  return {
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getSnapshot: () => snapshot,
    start() {
      if (timers.length) return;
      poll();
      timers = [setInterval(poll, 1000), setInterval(emit, 250)];
    },
    stop() {
      timers.forEach(clearInterval);
      timers = [];
    },
    actions: {
      // The small live demo: pause random kills and call Harbour Market only. If Harbour
      // says it passed tins to another store, followForwards releases that store next.
      releaseStores: act((recall_id) => {
        follow = recall_id;
        try {
          window.localStorage.setItem(FOLLOW_STORE, recall_id);
        } catch {
          /* the follow lasts until reload */
        }
        return [
          ['/admin/supervisor', { recall_id, paused: true, reason: 'the live call: random kills wait' }],
          ['/admin/release', { recall_id, party_id: HARBOUR }, true],
        ];
      }),
      // Start over: pause the clock of every open run (so no old timer rings a phone again)
      // and open a fresh run of the long scenario under a new recall id. Nothing is deleted:
      // the calls of earlier runs stay in memory, for Vector Search to find.
      startOver: act(() => {
        const open = (last?.recalls || []).filter((r) => r.status !== 'closed').map((r) => r._id);
        return [
          ...open.map((recall_id) => ['/admin/clock', { recall_id, action: 'pause' }, true]),
          ['/admin/recalls', { scenario: 'recall_long', mode: 'demo', recall_id: freshId() }],
        ];
      }, () => {
        follow = null;
        pin = null;
        focus = null;
      }),
      armFailpoint: () => post('/admin/failpoint', () => ({ name: snapshot.failpointArmed ? null : 'after_dial_request' }))(),
      // The kill beat (scripts/demo.py kill-arm). The API cannot kill a process: it arms the failpoint for
      // Corner Mart, then releases Corner Mart and Queen St Grocer's reply, whose claim names Corner Mart.
      // The worker that dials Corner Mart dies after asking for the call and before saving it.
      killWorker: act((recall_id) => [
        ['/admin/failpoint', { name: KILL_FAILPOINT, party_id: KILL_PARTY }],
        ...KILL_RELEASES.map((party_id) => ['/admin/release', { recall_id, party_id }, true]),
      ]),
      switchNorth: post('/admin/order-system', () => ({ order_system: 'north', action: 'switch_v2' })),
      saboteur: post('/admin/saboteur', (recall_id) => ({ recall_id, on: true, seconds: 20 })),
      closeRecall: post('/admin/close', (recall_id) => ({ recall_id })),
      togglePause: () => post('/admin/clock', (recall_id) => ({ recall_id, action: snapshot.paused ? 'resume' : 'pause' }))(),
      storeEvent: () => {},
      setKey,
      askKey: () => {
        keyAsk = keyAsk || 'missing';
        emit();
      },
      dismissKey: () => {
        keyAsk = null;
        pending = null;
        emit();
      },
      focus: (attemptId) => {
        pin = attemptId || null;
        emit();
      },
      playAll: () => {},
      setSpeed: () => {},
      reset: () => {},
    },
    // For tests: feed one /wall/state body straight in.
    _load: (d) => {
      last = d;
      link = { ...link, ok: true, at: now() };
      emit();
    },
  };
}
