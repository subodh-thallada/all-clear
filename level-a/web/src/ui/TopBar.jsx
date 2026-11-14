import { hms, num, simClock, simParts } from '../data/format.js';
import { useTween } from './hooks.js';
import * as Icon from './icons.jsx';

export default function TopBar({ s, actions }) {
  const { recall, run } = s;
  const open = useTween(recall.open);
  const closed = recall.status === 'closed';
  const refused = recall.close && s.t - recall.close.at < 6000;
  const { day } = simParts(run.simNow);
  return (
    <header className="top">
      <div className="brand"><Icon.Logo /><b>All Clear</b></div>
      <nav className="crumbs" aria-label="Where you are">
        <span>Recalls</span><Icon.Chevron size={14} /><span>Batch {recall.batch}</span><Icon.Chevron size={14} /><b>Live run</b>
      </nav>
      <span className={`state ${closed ? 'ok' : 'bad'}`}>
        <i />
        {closed ? `Recall closed. Every ${s.words.unit} is explained` : `Recall open, ${num(open)} ${s.words.units} still open`}
      </span>
      <span className="grow" />
      <span className={`source ${s.source} ${s.source === 'live' && (!s.link?.ok || s.link?.note) ? 'down' : ''}`} title={s.source === 'fixture' ? 'Scripted in this browser. Nothing here came from Atlas.' : s.link?.error || 'Polling /wall/state once a second'}>
        {s.source === 'fixture' ? 'Demo data, scripted here' : s.link?.note ? s.link.note : s.link?.ok ? 'Live from Atlas' : 'API not answering'}
      </span>
      <span className="chip-lg"><Icon.Clock size={16} />Day {day} of 10, {simClock(run.simNow)}</span>
      <span className="chip-lg mono">{hms(run.realElapsedS)}</span>
      <span className="crew" title="The four workers">
        {s.workers.map((w) => <i key={w.id} className={!w.alive ? 'down' : w.party ? 'busy' : ''}>{w.label}</i>)}
      </span>
      <button type="button" className={`close ${refused ? 'refused' : ''}`} onClick={actions.closeRecall} title="Ask Atlas to close the recall (7)">
        <Icon.Lock size={17} />
        {refused ? 'Refused by Atlas' : 'Close recall'}
      </button>
    </header>
  );
}
