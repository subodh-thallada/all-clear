import { useState } from 'react';
import * as Icon from './icons.jsx';
import KeyPrompt from './KeyPrompt.jsx';

// The operator's buttons. Fixture: they drive the engine in this browser.
// Live: they post to the API's admin endpoints.
export default function Toolbar({ s, actions }) {
  const [more, setMore] = useState(false);
  const live = s.source === 'live';
  const locked = live && !s.admin;
  const released = live
    ? !s.parties.some((p) => p.holdForDemo && /^(harbour|lakeside)/.test(p.id))
    : !s.parties.some((p) => p.holdForDemo);
  const northDone = s.systems.north.version !== 1 || s.systems.north.status !== 'active';
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
      <button type="button" className="tb" onClick={actions.releaseStores} disabled={released}
        title={live ? "The judge's call: pauses random kills and releases Harbour and Lakeside (1)" : 'Release the slice stores (1)'}>
        <Icon.Send size={16} /> {live ? 'Call Harbour' : `Release ${s.words.parties}`}
      </button>
      <button type="button" className="tb danger" onClick={() => actions.killWorker()} title={live ? 'The kill beat: arms the failpoint for Corner Mart, then releases Corner Mart and Queen St Grocer. The worker that dials Corner Mart dies before it saves the call (3)' : 'Kill a worker (3)'}>
        <Icon.Skull size={16} /> {live ? 'Arm the kill' : 'Kill a worker'}
      </button>
      <button type="button" className="tb" onClick={actions.switchNorth} disabled={northDone} title="Distributor North's order system moves to v2 (4)">
        <Icon.Swap size={16} /> Change order system
      </button>
      <div className="more">
        <button type="button" className="tb icon" onClick={() => setMore((v) => !v)} aria-expanded={more} title="More controls">
          <Icon.Dots size={18} />
        </button>
        {more && (
          <div className="menu">
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
