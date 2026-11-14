import { useState } from 'react';
import * as Icon from './icons.jsx';

// The operator's buttons. Fixture: they drive the engine in this browser.
// Live: they post to the API's admin endpoints.
export default function Toolbar({ s, actions }) {
  const [more, setMore] = useState(false);
  const live = s.source === 'live';
  const locked = live && !s.admin;
  const released = !s.parties.some((p) => p.holdForDemo);
  const northDone = s.systems.north.version !== 1 || s.systems.north.status !== 'active';
  const pick = (fn) => () => {
    setMore(false);
    fn();
  };
  return (
    <div className="toolbar" title={locked ? 'Open the screen with ?key=<admin token> to use these buttons' : undefined}>
      {!live && (
        <button type="button" className={`tb primary ${s.playing ? 'on' : ''}`} onClick={actions.playAll} title="Run every beat in order (space)">
          {s.playing ? <Icon.Stop size={16} /> : <Icon.Play size={16} />}
          {s.playing ? 'Stop demo' : 'Play demo'}
        </button>
      )}
      <span className="sep" />
      <button type="button" className="tb" onClick={actions.releaseStores} disabled={released} title="Release the three slice stores (1)">
        <Icon.Send size={16} /> Release {s.words.parties}
      </button>
      <button type="button" className="tb danger" onClick={() => actions.killWorker()} title={live ? 'Arms the failpoint: the next worker to dial a real phone dies before it saves the call (3)' : 'Kill the worker on the call (3)'}>
        <Icon.Skull size={16} /> {live ? 'Kill the next caller' : 'Kill a worker'}
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
