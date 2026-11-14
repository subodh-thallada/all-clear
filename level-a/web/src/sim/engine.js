// Fixture engine: a small, in-browser stand-in for the API server, the four
// workers and Atlas, so the wall can be built and rehearsed before /wall/state
// exists. It follows the build spec's mechanics instead of replaying a recording:
//
//   - a worker claims a party under a lease with a fencing token, and renews it
//   - a subagent is one task run: a brief, a tool list, a budget, a typed result
//   - every tool call passes the gate, every claim passes the validators
//   - a call is run by the provider, not the worker, so it outlives a killed worker
//   - an expired lease with something in flight is adopted, and nothing is redialled
//
// Nothing here is evidence. In a judged run the wall reads Atlas through the API
// (src/data/live.js) and this file is not used.

import {
  buildScenario, SCRIPTS, SIM_REPLIES, HIDDEN, EPISODES, BATCH, PRODUCT, UNITS_TOTAL,
  SIM_START, SEC_PER_SIM_HOUR, REAL_ELAPSED_AT_START_S, LEASE_TTL_MS, LEASE_RENEW_MS, RESTART_AFTER_MS,
} from './scenario.js';
import { postClaim, totals, tryClose } from './ledger.js';
import { simStamp, pad, WORDS } from '../data/format.js';

const TOOLS = {
  contact_party: ['place_call', 'send_form_link', 'schedule_callback', 'escalate'],
  removal_check: ['place_call', 'read_store_record', 'schedule_callback'],
  repair_adapter: ['propose_adapter', 'run_canary', 'retry_operation'],
  resolve_in_doubt: ['read_tool_events', 'query_call_list', 'mark_abandoned'],
};

const PRELUDE = { lease: 500, brief: 800, decide: 700, gate: 450 };

export function createEngine() {
  let S;
  let snapshot = null;
  let timer = null;
  let last = 0;
  const listeners = new Set();

  let seed = 11;
  const rnd = () => {
    seed = (seed * 1664525 + 1013904223) % 4294967296;
    return seed / 4294967296;
  };
  const int = (a, b) => a + Math.floor(rnd() * (b - a + 1));

  const simNow = () => SIM_START + S.t / 1000 / SEC_PER_SIM_HOUR;
  const P = (id) => S.parties.find((p) => p.id === id);
  const W = (id) => S.workers.find((w) => w.id === id);
  const A = (id) => S.attempts.find((a) => a.id === id);

  function init() {
    seed = 11;
    const sc = buildScenario();
    S = {
      t: 0,
      speed: 1,
      paused: false,
      n: { attempt: 931, run: 412, claim: 207, ev: 0, ep: 440, storeEv: 88213, pulse: 0, gate: 0 },
      recall: { id: 'r-1', status: 'open' },
      run: {
        steps: 412, kills: 9, adoptions: 9, duplicates: 0, tokens: 1850,
        refusals: { conservation: 9, evidence: 2, live_attempt: 2, dedupe: 1, idempotency: 1, readback: 0, close_gate: 0, gate: 0 },
        invariants: { at: SIM_START, passed: [true, true, true, true] },
      },
      parties: sc.parties,
      edges: sc.edges,
      systems: sc.systems,
      workers: ['w1', 'w2', 'w3', 'w4'].map((id, i) => ({
        id, alive: true, phase: 'idle', until: 0, nextPoll: 300 + i * 130, partyId: null, token: null,
        lastRenew: 0, run: null, attemptId: null, memory: null, decision: null, result: null,
        op: null, adopt: null, ghost: null, downAt: null, restartAt: null,
      })),
      attempts: [],
      calls: {},
      slips: [],
      events: [],
      pulses: [],
      timers: [],
      touched: {},
      capacity: { maxLive: 1 },
      station: { dispatch: null, memory: null, gate: null, validator: null, ask: null, ledger: null, found: null, close: null },
      failpoint: false,
      sab: null,
      play: null,
      close: null,
      pin: null,
      lastReal: null,
      lastInvHour: SIM_START,
      nextSummit: 21000,
    };
    for (const e of sc.feed) S.events.push({ id: ++S.n.ev, seeded: true, ...e });
  }

  // --- writes the wall can see ------------------------------------------------

  function feed(ev) {
    const e = { id: ++S.n.ev, simAt: simNow(), ...ev };
    S.events.push(e);
    if (S.events.length > 160) S.events.splice(0, S.events.length - 160);
    return e;
  }

  const refuse = (rule) => {
    S.run.refusals[rule] = (S.run.refusals[rule] || 0) + 1;
  };

  // One write, one movement: a pulse names the wires a write travelled.
  function pulse(wires, tone = 'ink') {
    S.pulses.push({ id: ++S.n.pulse, wires, tone, at: S.t });
    if (S.pulses.length > 48) S.pulses.splice(0, S.pulses.length - 48);
  }

  const touch = (system) => {
    S.touched[system] = S.t;
  };

  function row(call, kind, data) {
    const r = typeof data === 'string' ? { text: data } : data;
    call.rows.push({ n: call.rows.length + 1, kind, at: S.t, ...r });
  }

  // --- dispatch: the boss is one findOneAndUpdate ------------------------------

  function claim(w) {
    const now = simNow();
    const others = S.workers.some((x) => x !== w && x.alive && x.phase === 'idle');

    // In-doubt claim: the lease expired and something was left in flight.
    const doubt = S.parties
      .filter((p) => p.inFlight && p.lease.until < S.t && !(p.lease.owner === w.id && others))
      .sort((a, b) => a.lease.until - b.lease.until)[0];
    if (doubt) return adopt(w, doubt);

    // Due claim: has work, is due, is not held for the demo, is free.
    const due = S.parties
      .filter((p) => p.nextActionAt != null && p.nextActionAt <= now && !p.holdForDemo && p.lease.until < S.t && !p.inFlight)
      .sort((a, b) => b.priority - a.priority || a.nextActionAt - b.nextActionAt)[0];
    if (due) lease(w, due);
  }

  function lease(w, p) {
    p.lease = { owner: w.id, until: S.t + LEASE_TTL_MS, token: p.lease.token + 1 };
    if (p.state === 'pending') p.state = 'contacting';
    Object.assign(w, {
      partyId: p.id, token: p.lease.token, lastRenew: S.t, phase: 'lease', until: S.t + PRELUDE.lease,
      run: null, attemptId: null, memory: null, decision: null, result: null, op: null, adopt: null,
    });
    S.station.dispatch = { party: p.name, worker: w.id, token: p.lease.token, kind: 'due', at: S.t };
    pulse(['queue>due', 'due>dispatch', `dispatch>${w.id}`]);
  }

  function adopt(w, p) {
    const prev = p.lease.owner;
    p.lease = { owner: w.id, until: S.t + LEASE_TTL_MS, token: p.lease.token + 1 };
    Object.assign(w, {
      partyId: p.id, token: p.lease.token, lastRenew: S.t, phase: 'adopting', until: S.t + 1100,
      attemptId: p.inFlight, adopt: { prev, attemptId: p.inFlight }, memory: null, result: null, op: null,
      decision: { tool: 'read_tool_events' },
      run: {
        id: `tr-${pad(++S.n.run, 4)}`, kind: 'resolve_in_doubt', tools: TOOLS.resolve_in_doubt,
        budget: { model: 4, tools: 6 }, used: { model: 0, tools: 1 }, status: 'running', at: S.t,
      },
    });
    S.station.dispatch = { party: p.name, worker: w.id, token: p.lease.token, kind: 'in_doubt', prev, at: S.t };
    pulse(['queue>due', 'due>dispatch', `dispatch>${w.id}`]);
  }

  // --- one unit of work ---------------------------------------------------------

  const kindFor = (p) => (p.reason === 'repair' ? 'repair_adapter' : p.reason === 'removal' ? 'removal_check' : 'contact_party');

  const PHASES = {
    idle(w) {
      if (S.t < w.nextPoll) return;
      w.nextPoll = S.t + 500;
      claim(w);
    },

    // The lease is written. Spawn the subagent: one task run with a fixed-size brief.
    lease(w) {
      const p = P(w.partyId);
      const kind = kindFor(p);
      w.run = {
        id: `tr-${pad(++S.n.run, 4)}`, kind, tools: TOOLS[kind],
        budget: { model: 4, tools: 6 }, used: { model: 0, tools: 0 }, status: 'running', at: S.t,
      };
      if (kind === 'repair_adapter') {
        const hit = p.id === 'dist-east' && S.systems.north.version === 2;
        w.memory = hit ? EPISODES.adapterHit : EPISODES.adapterMiss;
        if (hit) {
          feed({
            system: 'East orders', kind: 'episode_retrieved', text: 'Episode e-0412 retrieved for the failure text, score 0.93',
            atlas: { primitive: '$vectorSearch', collection: 'episodes', verdict: 'retrieved' },
          });
        }
      } else if (p.foundVia === 'claim' && p.state !== 'claimed') {
        w.memory = EPISODES.hidden;
      } else {
        w.memory = kind === 'removal_check' ? EPISODES.removal : EPISODES.contact;
      }
      S.station.memory = { party: p.name, episodes: w.memory, at: S.t };
      S.run.steps += 1;
      pulse(['memory>dispatch']);
      w.phase = 'brief';
      w.until = S.t + PRELUDE.brief;
    },

    // The model chooses the next action from the brief. Its context is the same size every time.
    brief(w) {
      const p = P(w.partyId);
      const kind = w.run.kind;
      if (kind === 'repair_adapter' && w.memory.length) {
        w.decision = { tool: 'run_canary', note: 'mapping from episode e-0412' };
      } else {
        w.run.used.model += 1;
        S.run.steps += 1;
        S.run.tokens = int(1790, 1910);
        if (kind === 'repair_adapter') w.decision = { tool: 'propose_adapter' };
        else if (kind === 'removal_check' && p.connected) w.decision = { tool: 'read_store_record' };
        else w.decision = { tool: 'place_call' };
      }
      w.phase = 'decide';
      w.until = S.t + PRELUDE.decide;
    },

    // Every tool call passes the gate: run, lease, allowlist, budget, intent.
    decide(w) {
      w.run.used.tools += 1;
      S.run.steps += 1;
      S.station.gate = { id: ++S.n.gate, worker: w.id, tool: w.decision.tool, verdict: 'passed', rule: null, at: S.t };
      pulse([`${w.id}>gate`, 'gate>sys']);
      w.phase = 'gate';
      w.until = S.t + PRELUDE.gate;
    },

    gate(w) {
      const p = P(w.partyId);
      if (w.decision.tool === 'place_call') return placeCall(w, p);
      if (w.decision.tool === 'read_store_record') {
        touch('stores');
        w.op = { kind: 'read_store_record', until: S.t + 1300 };
      } else {
        const key = p.id === 'dist-north' ? 'north' : 'east';
        S.systems[key].status = 'repairing';
        touch(key);
        w.op = { kind: 'canary', sys: key, until: S.t + 1500 };
      }
      w.phase = 'acting';
    },

    // A real call needs a slot. Slots in use are counted from live attempts.
    slot(w) {
      placeCall(w, P(w.partyId));
    },

    acting(w) {
      if (w.op) {
        if (S.t >= w.op.until) finishOp(w);
        return;
      }
      const a = A(w.attemptId);
      if (a.final) conclude(w, a);
    },

    // A dead worker left something in flight. Resolve it before doing anything new.
    adopting(w) {
      const p = P(w.partyId);
      const a = A(w.attemptId);
      a.holder = w.id;
      if (!a.final) a.recorded = a.provider === 'connected' ? 'connected' : 'dialed';
      S.run.adoptions += 1;
      S.run.steps += 1;
      feed({
        system: `worker ${w.id}`, kind: 'lease_adopted',
        text: `adopted ${p.name}: 1 in doubt, 4 documents read, 0 steps replayed. Call found in tool_events, no redial`,
        atlas: { primitive: 'findOneAndUpdate', rule: 'lease', collection: 'parties', verdict: 'adopted' },
        ref: { partyId: p.id, attemptId: a.id, worker: w.id },
      });
      row(S.calls[a.id], 'atlas', {
        verdict: 'adopted', tag: 'findOneAndUpdate on parties.lease',
        text: `${w.id.toUpperCase()} adopted ${p.name}. 4 documents read, 0 steps replayed. Call found, no redial`,
      });
      w.phase = 'acting';
    },

    // Release and result are one transaction. Then the subagent is gone.
    result(w) {
      Object.assign(w, {
        partyId: null, token: null, run: null, attemptId: null, memory: null, decision: null,
        result: null, op: null, adopt: null, phase: 'idle', nextPoll: S.t + 250,
      });
    },
  };

  function placeCall(w, p) {
    const real = p.real && p.behavior?.kind === 'script';
    if (real) {
      const live = S.attempts.filter((a) => a.real && !a.final).length;
      if (live >= S.capacity.maxLive) {
        w.phase = 'slot';
        w.until = S.t + 300;
        return;
      }
    }
    const a = {
      id: `at-${pad(++S.n.attempt, 4)}`, partyId: p.id, holder: w.id, real,
      recorded: 'intent', provider: 'requested', final: false, outcome: null,
      steps: stepsFor(p), i: 0, nextAt: S.t, saveAt: S.t + 450, challenges: 0, lastReject: null,
      startedAt: S.t, connectedAt: null, endedAt: null,
    };
    a.nextAt = S.t + a.steps[0].wait;
    S.attempts.push(a);
    S.calls[a.id] = {
      attemptId: a.id, partyId: p.id, partyName: p.name, real, phoneTail: p.phoneTail,
      channel: real ? 'voice' : p.real ? 'form' : 'sim', rows: [], slips: [], connectedAt: null, endedAt: null,
    };
    if (real) S.lastReal = a.id;
    p.inFlight = a.id;
    w.attemptId = a.id;
    w.phase = 'acting';
    S.systems.phone.attempts += 1;
    touch('phone');
    // Failpoint: the call was requested and the worker dies before saving its identifiers.
    if (S.failpoint && real) {
      S.failpoint = false;
      a.saveAt = null;
      kill(w, 'failpoint');
    }
  }

  function stepsFor(p) {
    const b = p.behavior || {};
    if (b.kind === 'script' && SCRIPTS[b.id]) return SCRIPTS[b.id];
    if (b.kind === 'script') {
      const slow = b.id === 'queen';
      return [
        { type: 'ring', wait: 0 },
        { type: 'connect', wait: 1500 },
        { type: 'claim', wait: slow ? 30000 : 4200, payload: SIM_REPLIES[b.id] },
        { type: 'end', wait: 900 },
      ];
    }
    if (b.kind === 'removal') {
      return [
        { type: 'ring', wait: 0 },
        { type: 'connect', wait: int(1200, 1900) },
        { type: 'claim', wait: int(1900, 3000), build: 'removal' },
        { type: 'end', wait: 800 },
      ];
    }
    if (b.kind === 'answers') {
      return [
        { type: 'ring', wait: 0 },
        { type: 'connect', wait: 1700 },
        { type: 'claim', wait: 2600, payload: { soldSource: 'till', batchSeen: BATCH, blockTest: true, ...b.claim } },
        { type: 'end', wait: 800 },
      ];
    }
    return [{ type: 'ring', wait: 0 }, { type: 'noanswer', wait: 3200 }];
  }

  // What the store's staff pulled since the last check.
  function removalPayload(p) {
    const pulled = p.onShelf <= 6 ? p.onShelf : Math.ceil(p.onShelf * (0.45 + rnd() * 0.25));
    const payload = {
      sold: p.sold, onShelf: p.onShelf - pulled, held: p.held + pulled, returned: p.returned, forwarded: p.forwarded,
      soldSource: p.soldSource || 'till',
    };
    if (p.connected) {
      payload.heldBySystem = p.heldBySystem + pulled;
      payload.systemOnShelf = Math.max(0, p.systemOnShelf - pulled);
    } else {
      payload.batchSeen = BATCH;
      payload.blockTest = true;
    }
    return { payload, pulled };
  }

  function finishOp(w) {
    const p = P(w.partyId);
    const op = w.op;

    if (op.kind === 'read_store_record') {
      w.op = null;
      if (p.behavior?.kind === 'removal' && p.onShelf > 0) {
        const { payload, pulled } = removalPayload(p);
        const first = S.n.storeEv + 1;
        S.n.storeEv += pulled;
        S.systems.stores.events += pulled;
        S.systems.stores.last = { party: p.name, kind: 'quarantine', id: `t-${S.n.storeEv}` };
        const slip = submit(p, payload, { source: 'store_system', tool: 'store_events', evidence: `t-${first}${pulled > 1 ? ` to t-${S.n.storeEv}` : ''}` });
        return finish(w, slip.accepted ? 'claim_accepted' : 'claim_rejected', [slip.evidence]);
      }
      S.systems.stores.last = { party: p.name, kind: 'stock record read', id: null };
      p.nextActionAt = simNow() + 4;
      return finish(w, 'answer_recorded', []);
    }

    const sys = S.systems[op.sys];
    if (op.kind === 'canary') {
      const fromMemory = w.memory.length > 0;
      const c = sys.canaryAccount;
      sys.canary = { account: c.account, ledger: c.value, system: c.value, passed: true };
      sys.version = 2;
      sys.status = 'active';
      sys.source = fromMemory ? 'episode e-0412' : 'model';
      sys.note = null;
      const n = sys.ops.filter((o) => o.state === 'needs_reverify').length;
      feed({
        system: sys.name, kind: 'adapter_activated',
        text: fromMemory
          ? `adapter v2 active from episode e-0412: canary ${c.value} = ${c.value}. 0 model calls. Retrying ${n} operations`
          : `adapter v2 active: canary ${c.value} = ${c.value}. Retrying ${n} operations`,
        atlas: { primitive: 'transaction', rule: 'one active adapter', collection: 'adapters', verdict: 'committed' },
      });
      touch(op.sys);
      w.op = { kind: 'retry', sys: op.sys, n, until: S.t + 520 };
      return;
    }

    if (op.kind === 'retry') {
      const next = sys.ops.find((o) => o.state === 'needs_reverify');
      if (next) {
        next.state = 'verified';
        next.adapter = 2;
        w.run.used.tools = Math.min(w.run.budget.tools, w.run.used.tools + 1);
        S.run.steps += 1;
        touch(op.sys);
        pulse(['sys>validator'], 'green');
        op.until = S.t + 520;
        return;
      }
      feed({
        system: sys.name, kind: 'op_verified', text: `${op.n} operations verified by read-back. The other ${sys.ops.length - op.n} keep adapter v1`,
        atlas: { primitive: 'validator', rule: 'read-back required', collection: 'operations', verdict: 'accepted' },
      });
      w.op = null;
      Object.assign(p, { reason: null, priority: 0, nextActionAt: null });
      if (op.sys === 'north') S.timers.push({ at: S.t + 2600, fn: () => failContract('east') });
      finish(w, 'adapter_activated', [`${op.sys}:v2`]);
    }
  }

  // The attempt reached a final state: write the typed result and release.
  function conclude(w, a) {
    const p = P(w.partyId);
    if (a.outcome === 'no_answer') {
      p.ladderStep += 1;
      p.nextActionAt = simNow() + 4;
      feed({ system: 'phone', kind: 'no_answer', text: `${p.name}: no answer, attempt ${p.ladderStep}. Next action ${simStamp(p.nextActionAt)}` });
      return finish(w, 'no_answer', [a.id]);
    }
    const slip = S.calls[a.id].slips.at(-1);
    finish(w, slip?.accepted ? 'claim_accepted' : 'claim_rejected', [a.id, slip?.id].filter(Boolean));
  }

  function finish(w, outcome, evidence) {
    const p = P(w.partyId);
    w.result = {
      outcome, evidence,
      next: p.nextActionAt != null ? { action: p.reason === 'removal' ? 'removal check' : 'call', at: p.nextActionAt } : null,
      episode: `e-${pad(++S.n.ep, 4)}`,
    };
    w.run.status = 'done';
    S.run.steps += 1;
    p.inFlight = null;
    p.lease.until = S.t;
    w.phase = 'result';
    w.until = S.t + 1300;
  }

  // --- the provider and the API: these keep running when a worker dies ---------------

  function tickAttempts() {
    for (const a of S.attempts) {
      if (a.final) continue;
      if (a.saveAt != null && S.t >= a.saveAt) {
        const holder = W(a.holder);
        if (holder?.alive && holder.attemptId === a.id && a.recorded === 'intent') a.recorded = 'dialed';
        a.saveAt = null;
      }
      while (!a.final && S.t >= a.nextAt) step(a);
    }
  }

  function step(a) {
    const st = a.steps[a.i];
    const p = P(a.partyId);
    const call = S.calls[a.id];
    const holder = W(a.holder);
    const held = holder?.alive && holder.attemptId === a.id;

    if (st.type === 'ring') a.provider = 'ringing';
    else if (st.type === 'connect') {
      a.provider = 'connected';
      a.connectedAt = call.connectedAt = S.t;
      if (held) a.recorded = 'connected';
      if (p.state === 'contacting') p.state = 'reached';
    } else if (st.type === 'line' || st.type === 'store') row(call, st.type, st.text);
    else if (st.type === 'challenge') {
      // Built from the two values in the tool response, never from the prompt.
      row(call, 'line', `Our records show ${a.lastReject.shippedIn} tins shipped to you. You reported ${a.lastReject.reported}. Please count again.`);
    } else if (st.type === 'claim') {
      const payload = st.build === 'removal' ? removalPayload(p).payload : st.payload;
      if (!a.real) row(call, 'sim', `reply posted through the API: ${describe(payload)}`);
      const slip = submit(p, payload, { attempt: a, source: a.real ? 'voice' : call.channel, tool: 'submit_count' });
      if (!slip.accepted) {
        a.challenges += 1;
        a.lastReject = { reported: slip.reported, shippedIn: slip.shippedIn };
        S.station.ask = { party: p.name, n: a.challenges, at: S.t };
      }
    } else if (st.type === 'answer') {
      p[st.key] = st.value;
      row(call, 'atlas', { verdict: 'recorded', tag: 'submit_answer', text: `batch_seen ${st.value}. It is the recalled batch` });
    } else if (st.type === 'noanswer' || st.type === 'end') {
      a.final = true;
      a.outcome = st.type === 'end' ? 'completed' : 'no_answer';
      a.provider = 'ended';
      a.recorded = a.outcome; // the post-call webhook closes the attempt, whoever holds the lease
      a.endedAt = call.endedAt = S.t;
      return;
    }
    a.i += 1;
    a.nextAt += a.steps[a.i].wait; // from the scheduled time, so a fast clock does not stretch the call
  }

  const describe = (c) =>
    [
      c.received != null && `received ${c.received}`,
      c.sold ? `sold ${c.sold}` : null,
      c.onShelf ? `on shelf ${c.onShelf}` : null,
      c.held ? `held ${c.held}` : null,
      c.returned ? `returned ${c.returned}` : null,
      c.forwarded ? `passed ${c.forwarded} to "${c.forwardedTo || 'known stores'}"` : null,
    ].filter(Boolean).join(', ') || 'nothing on hand';

  function makeHidden(name, from) {
    const h = HIDDEN[name] || {};
    return {
      id: h.id || `p-${pad(400 + S.parties.length, 4)}`, name, type: 'store', tier: Math.max(3, from.tier + 1), foundVia: 'claim',
      channel: h.channel || 'sim', real: !!h.real, connected: !!h.connected, phoneTail: h.phoneTail || null,
      shippedIn: 0, sold: 0, onShelf: 0, held: 0, returned: 0, forwarded: 0, unresolved: 0,
      heldBySystem: 0, soldByTill: 0, systemOnShelf: 0, batchSeen: null, blockTest: null, soldSource: null,
      state: 'pending', ladderStep: 0, nextActionAt: simNow(), holdForDemo: false, priority: 3, reason: 'new',
      note: `Named by ${from.name}. On no shipment record`, lease: { owner: null, until: -1, token: 0 },
      inFlight: null, behavior: h.behavior || { kind: 'answers', claim: {} }, foundAt: S.t,
    };
  }

  // A claim arrives at the API. One transaction; Atlas accepts it or refuses it.
  function submit(p, payload, { attempt = null, source, tool, evidence = null }) {
    const call = attempt ? S.calls[attempt.id] : null;
    if (attempt) S.systems.phone.toolEvents += 1;
    const before = { onShelf: p.onShelf, held: p.held };
    const res = postClaim(S.parties, S.edges, p, payload, makeHidden);
    const txn = int(18, 27);
    const validator = txn + int(31, 44);
    const slip = {
      id: `cl-${pad(++S.n.claim, 4)}`, attemptId: attempt?.id || null, partyId: p.id, partyName: p.name, tool, source,
      fields: {
        onShelf: payload.onShelf ?? 0, sold: payload.sold ?? 0, held: payload.held ?? 0, returned: payload.returned ?? 0,
        forwarded: payload.forwarded ?? 0, forwardedTo: payload.forwardedTo || null, received: payload.received ?? null,
      },
      reported: res.reported, shippedIn: res.shippedIn, accepted: res.accepted, rule: res.rule || null,
      timing: { tool: 0, txn, validator, ledger: res.accepted ? validator + int(16, 28) : null },
      raw: res.raw || null, evidence: evidence || attempt?.id || null, at: S.t,
    };
    slip.ms = slip.timing.ledger ?? slip.timing.validator;
    S.slips.push(slip);
    if (S.slips.length > 40) S.slips.shift();
    if (call) call.slips.push(slip);
    const ref = { partyId: p.id, attemptId: attempt?.id || null };

    if (!res.accepted) {
      refuse(res.rule === 'conservation' ? 'conservation' : 'evidence');
      if (call) {
        row(call, 'atlas', {
          verdict: 'refused', tag: 'validator on parties', ms: slip.ms,
          text: `Document failed validation. reported ${res.reported}, shipped_in ${res.shippedIn}`,
        });
      }
      feed({
        system: 'ledger', kind: 'claim_rejected', ref, raw: res.raw,
        text: `REJECTED by Atlas: Document failed validation on parties. ${p.name} reported ${res.reported}, shipped_in ${res.shippedIn}`,
        atlas: { primitive: 'validator', rule: res.rule, collection: 'parties', verdict: 'refused' },
      });
      S.station.validator = { party: p.name, reported: res.reported, shippedIn: res.shippedIn, verdict: 'refused', rule: res.rule, ms: slip.ms, at: S.t };
      pulse(['sys>validator', 'validator>ask', 'ask>sys'], 'red');
      return slip;
    }

    // Committed.
    if (source !== 'store_system' && source !== 'agent') S.systems.phone.accepted += 1;
    const cols = source === 'store_system' ? 'store_events + parties' : res.created || res.disputed ? 'claims + parties + edges' : 'claims + parties';
    const pulled = p.held - before.held;
    const summary =
      source === 'store_system'
        ? `${pulled} ${pulled === 1 ? 'tin' : 'tins'} moved to quarantine per its stock record. ${p.onShelf} on a shelf`
        : describe(payload);
    if (call) row(call, 'atlas', { verdict: 'committed', tag: `transaction on ${cols}`, ms: slip.ms, text: summary });
    feed({
      system: source === 'store_system' ? 'store' : source === 'agent' ? 'agent' : 'ledger',
      kind: source === 'store_system' ? 'store_event' : 'claim_accepted', ref,
      text: source === 'agent' ? summaryAgent(p) : `${p.name}: ${source === 'store_system' ? summary : `claim accepted. ${summary}`}`,
      atlas: { primitive: 'transaction', collection: cols, verdict: 'committed' },
    });
    S.station.validator = { party: p.name, reported: res.reported, shippedIn: res.shippedIn, verdict: 'valid', rule: null, ms: slip.timing.validator, at: S.t };
    S.station.ledger = { party: p.name, collections: cols, ms: slip.ms, at: S.t };
    const wires = ['sys>validator', 'validator>ledger', 'ledger>tray', 'ledger>found'];

    if (p.connected && !p.baseline && attempt) {
      // A connector starts with a baseline, per the store's own records.
      p.baseline = true;
      p.systemOnShelf = p.onShelf;
      p.soldByTill = p.sold;
      S.systems.stores.events += 1;
      S.systems.stores.last = { party: p.name, kind: 'baseline', id: `t-${++S.n.storeEv}` };
    }

    if (res.disputed) {
      const from = P(res.disputed.from);
      const text = `${from.name} sent ${res.disputed.senderQty}, ${p.name} received ${res.disputed.recipientQty}. ${res.disputed.qty} tins disputed`;
      if (call) row(call, 'atlas', { verdict: 'disputed', tag: 'transaction on edges + parties', text });
      feed({ system: 'ledger', kind: 'transfer_disputed', ref, text, atlas: { primitive: 'transaction', collection: 'edges + parties', verdict: 'committed' } });
      from.note = `Says it sent ${res.disputed.senderQty} to ${p.name}. ${p.name} counted ${res.disputed.recipientQty}`;
      p.dispute = `${res.disputed.qty} tins disputed with ${from.name}`;
    }

    if (res.created) {
      const c = res.created;
      feed({
        system: 'phone', kind: 'consignee_found', ref: { partyId: c.id },
        text: `${p.name} names "${c.name}": new party, tier ${c.tier}`,
        atlas: { primitive: 'change stream', collection: 'parties', verdict: 'seen' },
      });
      S.station.found = { yes: true, name: c.name, from: p.name, at: S.t };
      wires.push('found>due');
    } else {
      wires.push('found>close');
    }
    pulse(wires, 'green');

    // Set the party's next timer in the same transaction.
    if (p.onShelf > 0) {
      const wasRemoval = p.behavior?.kind === 'removal';
      p.reason = 'removal';
      p.nextActionAt = simNow() + (wasRemoval ? 1 : 4);
      p.behavior = p.id === 'p-corner' ? { kind: 'records' } : { kind: 'removal' };
      p.note = null;
    } else {
      p.nextActionAt = null;
    }
    p.ladderStep = 0;
    tidy();
    return slip;
  }

  // Why a party with no timer is still open: it waits on the other side of a transfer.
  function tidy() {
    for (const p of S.parties) {
      if (p.nextActionAt != null || p.holdForDemo) continue;
      if (p.state === 'reconciled') {
        if (p.reason !== 'repair') p.reason = null;
        p.note = null;
        continue;
      }
      if (p.state !== 'claimed') continue;
      const mine = S.edges.filter((e) => e.from === p.id || e.to === p.id);
      const waiting = mine.find((e) => e.from === p.id && e.state === 'awaiting_recipient');
      if (waiting) {
        p.reason = 'awaiting';
        p.note = `Waiting for ${P(waiting.to).name} to say what it received`;
      } else if (mine.some((e) => e.state === 'disputed')) {
        p.reason = 'dispute';
        p.note = p.note || p.dispute || null;
      }
    }
  }

  const summaryAgent = (p) =>
    `${p.name} report ${p.sub.seq} accepted. ${p.shippedIn} tins, ${p.onShelf} on a shelf. Buyers told ${p.sub.buyersTold} of ${p.sub.buyersIdentified}`;

  // --- kills and restarts ---------------------------------------------------------------

  function kill(w, cause) {
    if (!w || !w.alive) return;
    const p = w.partyId ? P(w.partyId) : null;
    const a = w.attemptId ? A(w.attemptId) : null;
    w.ghost = p && w.phase !== 'result' ? { partyId: p.id, attemptId: a?.id || null, state: a ? a.recorded : w.phase } : null;
    Object.assign(w, {
      alive: false, phase: 'down', downAt: S.t, restartAt: S.t + RESTART_AFTER_MS, partyId: null, token: null,
      attemptId: null, run: null, memory: null, decision: null, result: null, op: null, adopt: null,
    });
    S.run.kills += 1;
    feed({
      system: 'supervisor', kind: 'kill', ref: { worker: w.id },
      text: `worker ${w.id} killed (${S.run.kills})${cause === 'failpoint' ? ' at the failpoint: call requested, identifiers not saved' : ''}`,
    });
  }

  function tickWorker(w) {
    if (w.ghost) {
      const gp = P(w.ghost.partyId);
      if (!gp || gp.lease.owner !== w.id || gp.lease.until < S.t) w.ghost = null;
    }
    if (!w.alive) {
      if (S.t >= w.restartAt) Object.assign(w, { alive: true, phase: 'idle', until: S.t, nextPoll: S.t + 600 });
      return;
    }
    if (w.partyId) {
      const p = P(w.partyId);
      if (p.lease.owner !== w.id || p.lease.token !== w.token) {
        // Fencing: this worker lost its lease. Its writes match nothing; drop the party.
        if (w.phase !== 'result') {
          Object.assign(w, { partyId: null, token: null, run: null, attemptId: null, memory: null, decision: null, result: null, op: null, adopt: null, phase: 'idle' });
          return;
        }
      } else if (w.phase !== 'result' && S.t - w.lastRenew >= LEASE_RENEW_MS) {
        p.lease.until = S.t + LEASE_TTL_MS;
        w.lastRenew = S.t;
      }
    }
    if (S.t < w.until) return;
    PHASES[w.phase](w);
  }

  // --- systems that change under the run ---------------------------------------------------

  function failContract(key) {
    const sys = S.systems[key];
    if (sys.version !== 1 || sys.status !== 'active') return;
    let flagged = 0;
    for (const op of sys.ops) {
      if (op.state === 'sent' || op.state === 'intent') {
        op.state = 'needs_reverify';
        flagged += 1;
      }
    }
    sys.status = 'contract_failed';
    sys.note = 'field qty_in_stock missing';
    feed({
      system: sys.name, kind: 'contract_failed',
      text: `contract failed: field qty_in_stock missing. ${flagged} operations flagged, ${sys.ops.length - flagged} untouched`,
    });
    Object.assign(P(sys.party), { reason: 'repair', priority: 10, nextActionAt: simNow() });
    touch(key);
  }

  const SABOTEUR = [
    [900, () => {
      const v = S.workers.find((w) => w.alive && w.partyId) || null;
      const p = v ? P(v.partyId) : S.parties[6];
      sabGate('refused', 'place_call', 'lease', `place_call on ${p.id}. Rule: lease. Holder is ${v ? v.id : 'nobody'}, token ${p.lease.token}`);
    }],
    [3300, () => sabGate('refused', 'close_recall', 'allowlist', 'close_recall. Rule: allowlist. No subagent has a close tool')],
    [5700, () => {
      refuse('readback');
      S.sab.last = { verdict: 'rejected', rule: 'read-back required', at: S.t };
      feed({
        system: 'atlas', kind: 'op_refused', text: 'REJECTED w5 (saboteur): operations.state = verified without readback_value',
        atlas: { primitive: 'validator', rule: 'read-back required', collection: 'operations', verdict: 'refused' },
      });
      pulse(['sab>gate', 'gate>sys', 'sys>validator'], 'red');
    }],
    [8100, () => {
      const v = S.parties.find((p) => p.inFlight) || S.parties[7];
      refuse('live_attempt');
      S.sab.last = { verdict: 'rejected', rule: 'one live attempt', at: S.t };
      feed({
        system: 'atlas', kind: 'attempt_refused',
        text: `REJECTED w5 (saboteur): E11000 duplicate key error collection: allclear.attempts index: one_live_attempt dup key: { recall_id: "r-1", party_id: "${v.id}" }`,
        atlas: { primitive: 'partial unique index', rule: 'one live attempt', collection: 'attempts', verdict: 'refused' },
      });
      pulse(['sab>gate', 'gate>sys', 'sys>validator'], 'red');
    }],
    [10500, () => sabGate('stopped', 'place_call', 'budget', 'budget_exceeded after 6 tool calls. The party is due again')],
    [12300, () => {
      feed({ system: 'invariants', kind: 'invariants', text: `${checkInvariants().filter(Boolean).length} of 4 hold` });
      S.sab = null;
    }],
  ];

  function sabGate(verdict, tool, rule, text) {
    refuse('gate');
    S.sab.last = { verdict, rule, at: S.t };
    S.station.gate = { id: ++S.n.gate, worker: 'w5', saboteur: true, tool, verdict, rule, at: S.t };
    feed({
      system: 'gate', kind: 'gate_refused', text: `${verdict.toUpperCase()} w5 (saboteur): ${text}`,
      atlas: { primitive: 'tool gate', rule, collection: 'task_runs', verdict: 'refused' },
    });
    pulse(['sab>gate'], 'red');
  }

  function checkInvariants() {
    const live = {};
    for (const a of S.attempts) if (!a.final) live[a.partyId] = (live[a.partyId] || 0) + 1;
    const t = totals(S.parties, S.edges);
    return [Object.values(live).every((n) => n === 1), true, true, t.balance === UNITS_TOTAL];
  }

  function tickBackground() {
    const hour = Math.floor(simNow());
    if (hour > S.lastInvHour) {
      S.lastInvHour = hour;
      const passed = checkInvariants();
      S.run.invariants = { at: hour, passed };
      feed({ system: 'invariants', kind: 'invariants', text: `${passed.filter(Boolean).length} of 4 hold` });
    }
    if (S.t >= S.nextSummit) {
      S.nextSummit = S.t + 52000;
      const p = P('p-0201');
      const moved = Math.min(3, p.onShelf);
      p.sub.seq += 1;
      p.sub.buyersTold = Math.min(p.sub.buyersIdentified, p.sub.buyersTold + (moved ? 1 : 0));
      S.systems.agents.reports = p.sub.seq;
      touch('agents');
      submit(p, {
        sold: p.sold, onShelf: p.onShelf - moved, held: p.held + moved, returned: p.returned, forwarded: p.forwarded,
        heldBySystem: p.heldBySystem + moved, systemOnShelf: Math.max(0, p.systemOnShelf - moved), soldSource: 'till',
      }, { source: 'agent', tool: 'agent_report', evidence: `ln-summit:${p.sub.seq}` });
      p.reason = 'report';
      p.behavior = null;
      p.nextActionAt = simNow() + 12; // the quiet-agent timer
    }
    if (S.sab) {
      while (S.sab && S.sab.i < SABOTEUR.length && S.t - S.sab.at >= SABOTEUR[S.sab.i][0]) {
        const fn = SABOTEUR[S.sab.i][1];
        S.sab.i += 1;
        fn();
      }
    }
    for (const tm of S.timers.filter((x) => S.t >= x.at)) tm.fn();
    S.timers = S.timers.filter((x) => S.t < x.at);
  }

  // --- the operator's buttons ------------------------------------------------------------------

  const actions = {
    releaseStores() {
      const now = simNow();
      let i = 0;
      for (const p of S.parties) {
        if (!p.holdForDemo) continue;
        p.holdForDemo = false;
        p.nextActionAt = now;
        p.priority = 5 - i;
        i += 1;
      }
      if (i) feed({ system: 'operator', kind: 'released', text: `${i} slice stores released to the timers` });
    },
    armFailpoint() {
      S.failpoint = !S.failpoint;
    },
    killWorker(id) {
      const busy = (w) => w.alive && w.partyId;
      const w =
        (id && W(id)) ||
        S.workers.find((x) => busy(x) && x.attemptId && A(x.attemptId).real && !A(x.attemptId).final) ||
        S.workers.find((x) => busy(x) && x.attemptId) ||
        S.workers.find(busy) ||
        S.workers.find((x) => x.alive);
      kill(w, 'operator');
    },
    switchNorth() {
      failContract('north');
    },
    saboteur() {
      if (!S.sab) S.sab = { at: S.t, i: 0, last: null };
    },
    storeEvent() {
      const corner = P('p-corner');
      const p = corner && corner.onShelf > 0 && corner.baseline
        ? corner
        : S.parties.find((x) => x.connected && x.type === 'store' && x.onShelf > 0 && !x.inFlight && x.lease.until < S.t);
      if (!p) return;
      const id = `t-${++S.n.storeEv}`;
      S.systems.stores.events += 1;
      S.systems.stores.last = { party: p.name, kind: 'quarantine', id };
      touch('stores');
      const slip = submit(p, {
        sold: p.sold, onShelf: p.onShelf - 1, held: p.held + 1, returned: p.returned, forwarded: p.forwarded,
        heldBySystem: p.heldBySystem + 1, systemOnShelf: Math.max(0, p.systemOnShelf - 1), soldSource: p.soldSource || 'till',
      }, { source: 'store_system', tool: 'store_events', evidence: id });
      if (!slip.accepted) return;
      S.events.at(-1).text = `${p.name}: 1 tin moved to quarantine. ${p.heldBySystem} ${p.heldBySystem === 1 ? 'tin' : 'tins'} held, event ${id}`;
      S.timers.push({
        at: S.t + 1900,
        fn: () => {
          const id2 = `t-${++S.n.storeEv}`;
          S.systems.stores.events += 1;
          S.systems.stores.last = { party: p.name, kind: 'blocked_sale', id: id2 };
          touch('stores');
          feed({ system: 'store', kind: 'store_event', text: `${p.name}: the till refused a sale. blocked_sale "recalled batch, not for sale", event ${id2}. Nothing moves` });
        },
      });
    },
    closeRecall() {
      const t = totals(S.parties, S.edges);
      const res = tryClose(t.open);
      if (res.closed) {
        S.recall.status = 'closed';
        feed({ system: 'recall', kind: 'closed', text: 'ALL CLEAR: 0 tins open', atlas: { primitive: 'validator', rule: 'close gate', collection: 'recalls', verdict: 'accepted' } });
        pulse(['found>close', 'close>clear'], 'green');
        return;
      }
      refuse('close_gate');
      S.close = { n: (S.close?.n || 0) + 1, at: S.t, open: t.open, notLocated: t.notLocated, onShelf: t.onShelf, disputed: t.disputed };
      S.station.close = { verdict: 'refused', open: t.open, at: S.t };
      feed({
        system: 'recall', kind: 'close_refused', raw: res.raw,
        text: `NOT ALL CLEAR: ${t.open} tins open (${t.notLocated} not located, ${t.onShelf} still on a shelf)`,
        atlas: { primitive: 'validator', rule: 'close gate', collection: 'recalls', verdict: 'refused' },
      });
      pulse(['found>close', 'close>clear'], 'red');
    },
    playAll() {
      S.play = S.play ? null : { i: 0, readyAt: null };
    },
    focus(attemptId) {
      S.pin = attemptId || null;
    },
    setSpeed(x) {
      S.speed = x;
    },
    togglePause() {
      S.paused = !S.paused;
    },
    reset() {
      init();
    },
  };

  // The beats of the demo script, in order. Each waits for the run to reach it.
  const PLAY = [
    { do: () => actions.releaseStores() },
    { when: () => !!P('p-corner'), do: () => { S.failpoint = true; } },
    {
      when: () => {
        const a = S.attempts.find((x) => x.partyId === 'p-corner');
        return a?.final && !P('p-corner').inFlight;
      },
      delay: 2500,
      do: () => actions.switchNorth(),
    },
    { when: () => S.systems.east.version === 2 && S.systems.east.ops.every((o) => o.state === 'verified'), delay: 2500, do: () => actions.saboteur() },
    { when: () => !S.sab, delay: 1500, do: () => actions.storeEvent() },
    { delay: 5000, do: () => actions.closeRecall() },
  ];

  function tickPlay() {
    if (!S.play) return;
    const st = PLAY[S.play.i];
    if (!st) {
      S.play = null;
      return;
    }
    if (st.when && !st.when()) return;
    if (S.play.readyAt == null) S.play.readyAt = S.t + (st.delay || 0);
    if (S.t < S.play.readyAt) return;
    st.do();
    S.play.i += 1;
    S.play.readyAt = null;
  }

  // --- snapshot: the same shape the live adapter produces ----------------------------------------

  function viewWorker(w) {
    const p = w.partyId ? P(w.partyId) : null;
    const a = w.attemptId ? A(w.attemptId) : null;
    const call = a ? S.calls[a.id] : null;
    const spoken = call?.rows.filter((r) => r.kind === 'line' || r.kind === 'store').at(-1) || null;
    const gp = w.ghost ? P(w.ghost.partyId) : null;
    return {
      id: w.id, slot: S.workers.indexOf(w) + 1, label: w.id.toUpperCase(), alive: w.alive, phase: w.phase,
      downForS: w.alive ? null : (S.t - w.downAt) / 1000,
      party: p && {
        id: p.id, name: p.name, real: a ? a.real : p.real && p.behavior?.kind === 'script',
        channel: call ? call.channel : p.channel === 'voice' ? (p.behavior?.kind === 'script' ? 'voice' : 'form') : p.channel,
        connected: p.connected, type: p.type, tier: p.tier, shippedIn: p.shippedIn, foundVia: p.foundVia,
      },
      lease: p && w.phase !== 'result' ? { remainingS: (p.lease.until - S.t) / 1000, ttlS: LEASE_TTL_MS / 1000, token: p.lease.token } : null,
      run: w.run, memory: w.memory, decision: w.decision, result: w.result, op: w.op && { kind: w.op.kind, sys: w.op.sys },
      adopt: w.adopt,
      attempt: a && {
        id: a.id, recorded: a.recorded, provider: a.provider, real: a.real, final: a.final, challenges: a.challenges,
        callS: a.connectedAt == null ? 0 : ((a.endedAt ?? S.t) - a.connectedAt) / 1000,
        verdict: call?.slips.length ? (call.slips.at(-1).accepted ? 'accepted' : 'refused') : null,
      },
      lastLine: spoken && { speaker: spoken.kind, text: spoken.text },
      ghost: gp && { party: gp.name, attemptId: w.ghost.attemptId, state: w.ghost.state, remainingS: (gp.lease.until - S.t) / 1000, ttlS: LEASE_TTL_MS / 1000 },
    };
  }

  function viewCall(id) {
    const c = S.calls[id];
    if (!c) return null;
    const a = A(id);
    const p = P(c.partyId);
    const holder = W(p.lease.owner);
    const held = p.inFlight === id && p.lease.until >= S.t && holder?.alive;
    return {
      ...c,
      state: a.provider,
      recorded: a.recorded,
      live: !a.final,
      holder: held ? p.lease.owner : null,
      wasHolder: held ? null : a.holder,
      inDoubt: p.inFlight === id && !held,
      callS: c.connectedAt == null ? 0 : ((c.endedAt ?? S.t) - c.connectedAt) / 1000,
    };
  }

  function snap() {
    const t = totals(S.parties, S.edges);
    const liveReal = S.attempts.filter((a) => a.real && !a.final).length;
    const focusId = S.pin && S.calls[S.pin] ? S.pin : S.lastReal;
    const connected = S.parties.filter((p) => p.connected && p.type === 'store' && (p.id !== 'p-corner' || p.baseline));
    return {
      source: 'fixture',
      words: WORDS.tins,
      t: S.t,
      speed: S.speed,
      paused: S.paused,
      playing: !!S.play,
      failpointArmed: S.failpoint,
      recall: {
        id: 'r-1', batch: BATCH, product: PRODUCT, unitsTotal: UNITS_TOTAL, status: S.recall.status,
        open: t.open, notLocated: t.notLocated, onShelf: t.onShelf, disputed: t.disputed, close: S.close,
      },
      tins: { removed: t.removed, sold: t.sold, onShelf: t.onShelf, notLocated: t.notLocated, balance: t.balance },
      metrics: { checked: [t.checked, S.parties.length], located: [t.located, UNITS_TOTAL], removed: [t.removed, UNITS_TOTAL] },
      run: {
        simNow: simNow(), realElapsedS: REAL_ELAPSED_AT_START_S + S.t / 1000, secPerSimHour: SEC_PER_SIM_HOUR,
        steps: S.run.steps, kills: S.run.kills, adoptions: S.run.adoptions, duplicates: S.run.duplicates, tokens: S.run.tokens,
        refusals: S.run.refusals, refused: Object.values(S.run.refusals).reduce((a, b) => a + b, 0),
        invariants: S.run.invariants, leaseTtlS: LEASE_TTL_MS / 1000, partiesAtStart: 5,
      },
      parties: S.parties.map((p) => ({
        ...p,
        lease: { owner: p.lease.owner, token: p.lease.token, remainingS: (p.lease.until - S.t) / 1000 },
        workState: p.inFlight ? A(p.inFlight)?.recorded : null,
        isNew: p.foundAt != null && S.t - p.foundAt < 6000,
      })),
      edges: S.edges,
      workers: S.workers.map(viewWorker),
      saboteur: S.sab && { last: S.sab.last, forS: (S.t - S.sab.at) / 1000 },
      capacity: { maxLive: S.capacity.maxLive, live: liveReal },
      systems: {
        phone: { ...S.systems.phone, live: S.attempts.filter((a) => !a.final).length },
        north: S.systems.north,
        east: S.systems.east,
        stores: {
          ...S.systems.stores,
          connected: connected.length,
          onShelfThere: connected.reduce((n, p) => n + p.onShelf, 0),
          movedToHeld: connected.reduce((n, p) => n + p.heldBySystem, 0),
        },
        agents: { ...S.systems.agents, ...P('p-0201').sub, tins: P('p-0201').shippedIn, onShelf: P('p-0201').onShelf },
        touched: S.touched,
      },
      station: S.station,
      call: focusId ? viewCall(focusId) : null,
      pinned: !!(S.pin && S.calls[S.pin]),
      lastSlip: S.slips.at(-1) || null,
      events: S.events.map((e) => ({ ...e, stamp: simStamp(e.simAt), ageS: (simNow() - e.simAt) * SEC_PER_SIM_HOUR })),
      pulses: S.pulses,
    };
  }

  function emit() {
    snapshot = snap();
    listeners.forEach((l) => l());
  }

  function advance(dt) {
    if (!S.paused) {
      S.t += dt * S.speed;
      tickPlay();
      tickAttempts();
      tickBackground();
      const k = Math.floor(S.t / 100) % S.workers.length;
      for (let i = 0; i < S.workers.length; i += 1) tickWorker(S.workers[(i + k) % S.workers.length]);
    }
    emit();
  }

  init();
  emit();

  const wrapped = {};
  for (const [name, fn] of Object.entries(actions)) {
    wrapped[name] = (...args) => {
      fn(...args);
      emit();
    };
  }

  return {
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getSnapshot: () => snapshot,
    actions: wrapped,
    advance,
    start() {
      if (timer) return;
      last = performance.now();
      timer = setInterval(() => {
        const now = performance.now();
        const dt = Math.min(250, now - last);
        last = now;
        advance(dt);
      }, 100);
    },
    stop() {
      clearInterval(timer);
      timer = null;
    },
    _state: () => S,
  };
}
