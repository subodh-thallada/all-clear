// Plain words for what the run is doing. The engine and the API speak in states
// and event kinds; the screen speaks in sentences a judge can read in a second.

import { ms } from '../data/format.js';

export function workerStatus(w) {
  if (!w.alive) return { text: 'Killed', tone: 'red' };
  if (!w.party) return { text: 'Idle', tone: 'grey' };
  if (w.phase === 'slot') return { text: 'Line busy', tone: 'amber' };
  if (w.phase === 'adopting') return { text: 'Taking over', tone: 'amber' };
  if (w.phase === 'result') {
    const o = w.result?.outcome;
    if (o === 'no_answer') return { text: 'No answer', tone: 'grey' };
    if (o === 'claim_rejected') return { text: 'Refused', tone: 'red' };
    return { text: o === 'adapter_activated' ? 'Repaired' : 'Saved', tone: 'green' };
  }
  if (w.op) return { text: w.op.kind === 'read_store_record' ? 'Checking stock' : 'Repairing', short: w.op.kind === 'read_store_record' ? 'Stock check' : 'Repairing', tone: 'amber' };
  if (w.attempt) {
    const a = w.attempt;
    if (a.verdict === 'refused' && !a.final) return { text: 'Refused', tone: 'red', calling: true };
    if (a.provider === 'connected' || a.recorded === 'connected') return { text: `On call ${ms(a.callS)}`, short: `Call ${ms(a.callS)}`, tone: 'green', calling: true };
    if (a.final) return { text: 'Call ended', tone: 'green' };
    return { text: 'Dialing', tone: 'amber', calling: true };
  }
  return { text: 'Starting', tone: 'amber' };
}

export function workerCaption(w, words = { party: 'store' }) {
  if (!w.alive) return w.ghost ? `Was on ${w.ghost.party}. Nobody renews its lease` : 'Restarting in 3 seconds';
  if (!w.party) return w.ghost ? 'Restarted with no memory' : 'Waiting for work';
  if (w.phase === 'lease') return 'Subagent starting';
  if (w.phase === 'brief') return 'Reading its brief';
  if (w.phase === 'decide') return 'Choosing the next step';
  if (w.phase === 'gate') return 'Passing the tool gate';
  if (w.phase === 'slot') return 'Waiting for a free phone line';
  if (w.phase === 'adopting') return 'Checking what the dead worker left';
  if (w.phase === 'result') return 'Result saved. Lease released';
  if (w.op) return w.op.kind === 'read_store_record' ? `Reading the ${words.party}'s stock record` : w.op.kind === 'canary' ? 'Testing the new mapping' : 'Retrying paused operations';
  if (w.attempt?.real && w.lastLine) return `"${w.lastLine.text}"`;
  if (w.attempt) return w.attempt.real ? (w.attempt.provider === 'connected' ? 'Talking to a person' : 'Ringing') : `Simulated ${words.party}, no voice`;
  return '';
}

// Which of the four subagent steps are done: brief, plan, gate, act.
export const workerStep = (w) => ({ lease: 0, brief: 1, decide: 2, gate: 3 }[w.phase] ?? 4);

// One sentence for the events worth saying out loud. Others return null.
export function headline(e) {
  const t = e.text || '';
  const m = (re) => re.exec(t) || [];
  switch (e.kind) {
    case 'released':
      return { tone: 'ink', text: 'Three stores released. Workers are picking them up.' };
    case 'claim_rejected': {
      const [, name, r, s] = m(/parties\. (.+?) reported (\d+), shipped_in (\d+)/);
      return { tone: 'red', text: name ? `Atlas refused the count. ${name} reported ${r}, but only ${s} were shipped.` : t };
    }
    case 'consignee_found': {
      const [, name] = m(/"([^"]+)"/);
      return { tone: 'ink', text: `New store found: ${name || 'a hidden store'}. It was on no shipment record.` };
    }
    case 'kill': {
      const [, w] = m(/worker (w\d)/);
      return { tone: 'red', text: `Worker ${(w || '').toUpperCase()} was killed${/failpoint/.test(t) ? ' mid-call' : ''}. Nobody renews its lease.` };
    }
    case 'lease_adopted': {
      const [, name] = m(/adopted (.+?):/);
      return { tone: 'green', text: `${e.system.replace('worker ', '').toUpperCase()} took over ${name || 'the store'}. 0 steps replayed, and the phone did not ring twice.` };
    }
    case 'transfer_disputed':
      return { tone: 'red', text: `${t}.` };
    case 'contract_failed':
      return { tone: 'red', text: `${e.system.replace(' orders', '')}'s order system changed. ${m(/(\d+) operations flagged/)[1] || 'Some'} operations paused.` };
    case 'adapter_activated':
      return /episode/.test(t)
        ? { tone: 'green', text: `${e.system.replace(' orders', '')} repaired from memory. 0 model calls.` }
        : { tone: 'green', text: `${e.system.replace(' orders', '')} repaired by the model. Canary check ${m(/canary (\d+ = \d+)/)[1] || 'passed'}.` };
    case 'gate_refused':
      return { tone: 'red', text: `Saboteur blocked by the tool gate. Rule: ${e.atlas?.rule || 'gate'}.` };
    case 'op_refused':
      return { tone: 'red', text: 'Saboteur blocked by Atlas. "Verified" needs a read-back.' };
    case 'attempt_refused':
      return { tone: 'red', text: 'Saboteur blocked by Atlas. One live call per store.' };
    case 'store_event':
      if (/event t-\d+/.test(t) && /moved to quarantine/.test(t)) return { tone: 'green', text: `${t.split(':')[0]}'s own stock record moved 1 tin to quarantine.` };
      if (/till refused/.test(t)) return { tone: 'ink', text: `${t.split(':')[0]}'s till refused to sell a recalled tin.` };
      return null;
    case 'close_refused':
      return { tone: 'red', text: `Atlas refused to close the recall. ${m(/(\d+) tins open/)[1] || 'Some'} tins are still open.` };
    case 'closed':
      return { tone: 'green', text: 'All clear. Every tin is explained.' };
    default:
      return null;
  }
}
