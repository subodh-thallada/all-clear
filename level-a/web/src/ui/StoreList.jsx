import { num } from '../data/format.js';
import { workerStatus } from './labels.js';
import * as Icon from './icons.jsx';

// Who is being called, who is waiting, who is done. Nothing else.

export default function StoreList({ s, actions }) {
  const now = s.run.simNow;
  const W = s.words;
  const byWorker = Object.fromEntries(s.workers.map((w) => [w.id, w]));
  const calling = [];
  const waiting = [];
  const stuck = [];
  const done = [];
  let timers = 0;
  for (const p of s.parties) {
    const w = p.lease.owner && p.lease.remainingS > 0 ? byWorker[p.lease.owner] : null;
    if (w) calling.push({ p, w });
    else if (p.inFlight) waiting.push({ p, pill: ['In doubt', 'red'], why: 'Its worker died mid-call' });
    else if (p.holdForDemo) waiting.push({ p, pill: ['Ready', 'grey'], why: `${num(p.shippedIn)} ${W.units}, not called yet` });
    else if (p.nextActionAt != null && p.nextActionAt <= now) waiting.push({ p, pill: ['Due', 'amber'], why: p.reason === 'removal' ? `${num(p.onShelf)} ${W.units} ${W.shelf}` : p.note || 'First call' });
    else if (p.state === 'reconciled') done.push(p);
    else if (p.nextActionAt != null) timers += 1;
    else stuck.push(p);
  }
  calling.sort((a, b) => a.w.id.localeCompare(b.w.id));
  const [checked, total] = s.metrics.checked;

  return (
    <aside className="stores">
      <div className="lot">
        <h2>Batch {s.recall.batch}</h2>
        <p>{num(s.recall.unitsTotal)} {W.units} shipped to {s.parties.length} {W.parties}</p>
        <div className="progress">
          <div><span>{W.Party === 'Store' ? 'Stores' : 'Parties'} checked</span><b>{checked} of {total}</b></div>
          <span className="track"><i style={{ width: `${total ? (checked / total) * 100 : 0}%` }} /></span>
        </div>
      </div>

      <div className="scroll">
        <h4>On a call now <span>{calling.length}</span></h4>
        {calling.map(({ p, w }) => {
          const st = workerStatus(w.alive && w.party?.id === p.id ? w : { ...w, alive: false });
          const focused = s.call && s.call.partyId === p.id;
          return (
            <button type="button" key={p.id} className={`store ${focused ? 'focused' : ''}`} onClick={() => (p.inFlight || p.lastCall) && actions.focus(p.inFlight || p.lastCall)}>
              <span className="avatar">{w.label}</span>
              <span className="who"><b>{p.name}</b><small>{p.shippedIn ? `${num(p.shippedIn)} ${W.units}` : 'On no shipment record'}</small></span>
              <span className={`pill ${st.tone}`}>{st.text}</span>
            </button>
          );
        })}
        {calling.length === 0 && <p className="none">No worker is busy</p>}

        <h4>Waiting for a worker <span>{waiting.length}</span></h4>
        {waiting.slice(0, 4).map(({ p, pill, why }) => (
          <div className="store" key={p.id}>
            <span className="avatar ghost"><Icon.Phone size={16} /></span>
            <span className="who"><b>{p.name}</b><small>{why}</small></span>
            <span className={`pill ${pill[1]}`}>{pill[0]}</span>
          </div>
        ))}
        {waiting.length > 4 && <p className="none">and {waiting.length - 4} more</p>}
        {waiting.length === 0 && <p className="none">{timers ? `Nobody right now. ${timers} on a timer` : 'Nobody is waiting'}</p>}

        {stuck.length > 0 && (
          <>
            <h4>Not settled yet <span>{stuck.length}</span></h4>
            {stuck.slice(0, 3).map((p) => (
              <div className="store" key={p.id}>
                <span className="avatar ghost bad">!</span>
                <span className="who"><b>{p.name}</b><small>{p.note || (p.onShelf ? `${num(p.onShelf)} ${W.units} ${W.shelf}` : `Waiting on another ${W.party}`)}</small></span>
              </div>
            ))}
            {stuck.length > 3 && <p className="none">and {stuck.length - 3} more</p>}
          </>
        )}

        <h4>Every {W.unit} explained <span>{done.length}</span></h4>
        {done.slice(-3).reverse().map((p) => (
          <div className="store" key={p.id}>
            <span className="avatar ok"><Icon.Check size={16} /></span>
            <span className="who"><b>{p.name}</b><small>{num(p.shippedIn)} {W.units}</small></span>
          </div>
        ))}
        {done.length > 3 && <p className="none">and {done.length - 3} more</p>}
      </div>

      <dl className="stats">
        <div><dd>{num(s.run.steps)}</dd><dt>steps</dt></div>
        <div><dd>{s.run.kills}</dd><dt>kills survived</dt></div>
        <div><dd>{s.run.duplicates}</dd><dt>duplicates</dt></div>
      </dl>
    </aside>
  );
}
