import { useState } from 'react';
import * as Icon from './icons.jsx';
import KeyPrompt from './KeyPrompt.jsx';

// The operator's buttons. Fixture: they drive the engine in this browser.
// Live: they post to the API's admin endpoints.
export default function Toolbar({ s, actions }) {
  const [more, setMore] = useState(false);
  const live = s.source === 'live';
  const locked = live && !s.admin;
  const [starting, setStarting] = useState(false);
  const harbour = s.parties.find((p) => p.id === 'harbour-market');
  const released = live ? !!harbour && !harbour.holdForDemo : !s.parties.some((p) => p.holdForDemo);
  // Live: Call Harbour, then the call's own status until Harbour's call has ended, then Start over.
  const harbourStep = !live || !harbour ? null
    : harbour.holdForDemo ? 'call'
      : harbour.liveAttempt ? (harbour.liveAttempt.state === 'connected' ? 'talking' : 'ringing')
        : harbour.attempts > 0 ? 'done' : 'queued';
  const northDone = s.systems.north.version !== 1 || s.systems.north.status !== 'active';
  const startOver = async () => {
    if (starting || !window.confirm('Start a new run? This run\'s clock is paused and Harbour Market can be called again.')) return;
    setStarting(true);
    try {
      await actions.startOver();
    } finally {
      setStarting(false);
    }
  };
  const pick = (fn) => () => {
    setMore(false);
    fn();
  };
  return (
    <div className="toolbar">
      {live && s.keyAsk && <KeyPrompt s={s} actions={actions} />}
      {locked && !s.keyAsk && (
        <button type="button" className="tb unlock" onClick={actions.askKey} title="Paste the admin key to use these buttons">
          <Icon.Lock size={16} /> Unlock buttons
        </button>
      )}
      {!live && (
        <button type="button" className={`tb primary ${s.playing ? 'on' : ''}`} onClick={actions.playAll} title="Run every beat in order (space)">
          {s.playing ? <Icon.Stop size={16} /> : <Icon.Play size={16} />}
          {s.playing ? 'Stop demo' : 'Play demo'}
        </button>
      )}
      <span className="sep" />
      {live && starting ? (
        <button type="button" className="tb" disabled><Icon.Play size={16} /> Starting a new run…</button>
      ) : harbourStep === 'queued' || harbourStep === 'ringing' || harbourStep === 'talking' ? (
        <button type="button" className="tb status" disabled
          title="Harbour Market is released. The phone rings when a worker and a phone line are free">
          <Icon.Phone size={16} />
          {harbourStep === 'talking' ? 'On the phone with Harbour' : harbourStep === 'ringing' ? 'Ringing Harbour…' : 'Dialling Harbour…'}
        </button>
      ) : live && released ? (
        <button type="button" className="tb" onClick={startOver}
          title="Pause this run's clock and start a new run, so Harbour can be called again">
          <Icon.Play size={16} /> Start over
        </button>
      ) : (
        <button type="button" className="tb" onClick={actions.releaseStores} disabled={released}
          title={live ? 'Pauses random kills and calls Harbour Market. If Harbour names another store, that store is called next (1)' : 'Release the slice stores (1)'}>
          <Icon.Send size={16} /> {live ? 'Call Harbour' : `Release ${s.words.parties}`}
        </button>
      )}
      <button type="button" className="tb danger" onClick={() => actions.killWorker()} title={live ? 'The kill beat: arms the failpoint for Corner Mart, then releases Corner Mart and Queen St Grocer. The worker that dials Corner Mart dies before it saves the call (3)' : 'Kill a worker (3)'}>
        <Icon.Skull size={16} /> {live ? 'Arm the kill' : 'Kill a worker'}
      </button>
      {(!live || s.systems.north.name) && (
        <button type="button" className="tb" onClick={actions.switchNorth} disabled={northDone} title="Distributor North's order system moves to v2 (4)">
          <Icon.Swap size={16} /> Change order system
        </button>
      )}
      <div className="more">
        <button type="button" className="tb icon" onClick={() => setMore((v) => !v)} aria-expanded={more} title="More controls">
          <Icon.Dots size={18} />
        </button>
        {more && (
          <div className="menu">
            {live && <button type="button" onClick={pick(startOver)} disabled={starting}>Start a new run</button>}
            <button type="button" onClick={pick(actions.armFailpoint)}>{s.failpointArmed ? 'Disarm failpoint' : 'Arm failpoint'} <kbd>2</kbd></button>
            <button type="button" onClick={pick(actions.saboteur)} disabled={!!s.saboteur}>Start the saboteur <kbd>5</kbd></button>
            {!live && <button type="button" onClick={pick(actions.storeEvent)}>Quarantine a tin at a store <kbd>6</kbd></button>}
            {!live && (
              <>
                <hr />
                <button type="button" onClick={pick(() => actions.setSpeed(s.speed === 1 ? 2 : s.speed === 2 ? 4 : 1))}>Speed: {s.speed}x</button>
                <button type="button" onClick={pick(actions.reset)}>Reset the run <kbd>R</kbd></button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
