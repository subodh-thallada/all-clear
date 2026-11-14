import { useEffect, useRef, useState } from 'react';
import { ms, num } from '../data/format.js';

// The call a worker is on: what was said, and what Atlas did about it.
// A second tab holds the full activity log, for when a judge asks "show me".

const VERDICT = {
  refused: ['bad', 'MongoDB Atlas refused the count'],
  committed: ['ok', 'Saved to the ledger'],
  adopted: ['info', 'Another worker took over this call'],
  disputed: ['bad', 'The two stores disagree'],
  recorded: ['ok', 'Batch code confirmed'],
};

function Transcript({ call, source, words }) {
  const end = useRef(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end' });
  }, [call.rows.length, call.attemptId]);

  return (
    <div className="talk">
      <div className="talk-head">
        <span>Live transcript</span>
        <span>{call.real ? (source === 'fixture' ? 'Agent and store manager, scripted' : 'Real phone') : `Simulated ${words.party}, no voice`}</span>
      </div>
      {source === 'live' && call.real && !call.rows.some((r) => r.kind === 'line' || r.kind === 'store') && (
        <p className="hint">The API does not send the words of a call yet. What Atlas decides about it appears here.</p>
      )}
      {source !== 'live' && call.rows.length === 0 && <p className="hint">{call.state === 'ringing' || call.state === 'requested' ? 'Ringing…' : 'Waiting for the first words'}</p>}
      {call.rows.map((r) => {
        if (r.kind === 'atlas') {
          const [tone, title] = VERDICT[r.verdict] || ['info', r.verdict];
          const detail =
            r.verdict === 'refused' && source === 'live' ? r.text
              : r.verdict === 'adopted' ? (source === 'live' ? r.text : '0 steps replayed. The phone did not ring twice.')
              : r.verdict === 'disputed' ? r.text
                : r.verdict === 'committed' ? r.text
                  : null;
          return (
            <div key={r.n} className={`verdict ${tone}`}>
              <b>{title}</b>
              {r.ms != null && <code>{r.ms} ms</code>}
              {detail && <small>{detail}</small>}
            </div>
          );
        }
        if (r.kind === 'sim') return <div key={r.n} className="bubble sim"><small>Store's answer</small>{r.text.replace('reply posted through the API: ', '')}</div>;
        return (
          <div key={r.n} className={`bubble ${r.kind}`}>
            <small>{r.kind === 'line' ? 'Agent' : words.person}</small>
            {r.text}
          </div>
        );
      })}
      <span ref={end} />
    </div>
  );
}

function Why({ call, words, source }) {
  const slip = call.slips.at(-1);
  if (!slip || slip.reported == null || slip.shippedIn == null) return null;
  const bad = !slip.accepted;
  return (
    <div className={`why ${bad ? 'bad' : 'ok'}`}>
      <h4>{bad ? 'Why it was refused' : 'Why it was accepted'}</h4>
      <div className="nums">
        <div><small>{words.Party} reported</small><b>{num(slip.reported)}</b></div>
        <span>{bad ? '≠' : '='}</span>
        <div><small>We shipped</small><b>{num(slip.shippedIn)}</b></div>
      </div>
      {bad ? (
        <pre>{source === 'live' ? (slip.raw || slip.serverText || '').slice(0, 260) : `WriteError 121  "Document failed validation"\ncollection: parties   rule: ${slip.rule || 'conservation'}`}</pre>
      ) : (
        <p>Every {words.unit} this {words.party} was shipped is now on the books.</p>
      )}
      <p className="rule">The rule lives in the database, not in the prompt.</p>
    </div>
  );
}

function Activity({ s }) {
  const [open, setOpen] = useState(null);
  const lines = s.events.slice(-40).reverse();
  return (
    <ol className="log">
      {lines.map((e) => {
        const bad = e.atlas?.verdict === 'refused';
        return (
          <li key={e.id} className={bad ? 'bad' : ''}>
            <button type="button" onClick={() => setOpen(open === e.id ? null : e.id)} disabled={!e.raw && !e.atlas}>
              <time>{e.stamp}</time>
              <span>{e.text}</span>
            </button>
            {open === e.id && (
              <div className="raw">
                {e.atlas && <code>{e.atlas.primitive}{e.atlas.rule ? `, ${e.atlas.rule}` : ''} on {e.atlas.collection}</code>}
                {e.raw && <pre>{e.raw}</pre>}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export default function CallPanel({ s, actions }) {
  const [tab, setTab] = useState('call');
  const call = s.call;
  const state = !call ? null
    : call.live ? (call.state === 'connected' ? ['green', `On call ${ms(call.callS)}`] : ['amber', 'Dialing'])
      : ['grey', `Ended, ${ms(call.callS)}`];

  return (
    <aside className="panel">
      <div className="panel-head">
        <span className={`avatar big ${call?.inDoubt ? 'down' : ''}`}>{call ? (call.holder || call.wasHolder || '').toUpperCase() || '–' : '–'}</span>
        <div className="who">
          <b>{call ? call.partyName : 'No call selected'}</b>
          <small>
            {!call ? (s.source === 'live' ? 'No real phone call is live' : 'Release the stores to start the first call')
              : call.inDoubt ? `No worker holds this call. ${(call.wasHolder || '').toUpperCase()} was killed`
                : call.phoneTail ? `Phone ending ${call.phoneTail}` : call.real ? 'Real phone call' : `Simulated ${s.words.party}`}
          </small>
        </div>
        {state && <span className={`pill ${state[0]}`}>{state[1]}</span>}
      </div>

      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'call'} onClick={() => setTab('call')}>Call</button>
        <button type="button" role="tab" aria-selected={tab === 'log'} onClick={() => setTab('log')}>Activity log</button>
        {s.pinned && <button type="button" className="follow" onClick={() => actions.focus(null)}>Follow the phone</button>}
      </div>

      {tab === 'log' ? (
        <Activity s={s} />
      ) : call ? (
        <>
          <Transcript call={call} source={s.source} words={s.words} />
          <Why call={call} words={s.words} source={s.source} />
        </>
      ) : (
        <div className="empty">
          <p><b>Nobody is on the phone yet.</b></p>
          {s.source === 'live'
            ? <p>When a worker dials a real phone, the call appears here with what the database did about it.</p>
            : <p>Press <b>Play demo</b>. A worker calls Harbour Market, and everything said appears here with what the database did about it.</p>}
        </div>
      )}
    </aside>
  );
}
