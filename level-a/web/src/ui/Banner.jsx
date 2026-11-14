import { headline } from './labels.js';

// One sentence at a time, for the event that just mattered.
// It stays for a few seconds of run time, then leaves.
const STAY = 6500;

export default function Banner({ s }) {
  let found = null;
  for (let i = s.events.length - 1; i >= 0 && i >= s.events.length - 12; i -= 1) {
    const e = s.events[i];
    if (e.seeded) break;
    const h = e.headline || headline(e);
    if (h) {
      found = { ...h, e };
      break;
    }
  }
  const ageMs = found ? found.e.ageS * 1000 : Infinity;
  return (
    <div className="banner-slot">
      {found && ageMs <= STAY ? (
        <div className={`banner ${found.tone}`} key={found.e.id} role="status"><i />{found.text}</div>
      ) : (
        <span className="banner-idle">{s.parties.some((p) => p.holdForDemo) ? (s.source === 'live' ? 'The run is going. Press Call Harbour to start the live part.' : 'The run is already going. Press Play demo to start the live part.') : ''}</span>
      )}
    </div>
  );
}
