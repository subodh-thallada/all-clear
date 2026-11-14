import { useEffect, useRef } from 'react';
import { num, simStamp } from '../data/format.js';
import { reduced, useFitBox, useTween } from './hooks.js';
import { workerCaption, workerStatus, workerStep } from './labels.js';
import * as Icon from './icons.jsx';

// The whole job as one picture, in the order the work travels:
// work comes due, the boss (Atlas) gives each store to one worker, the worker
// phones the store, Atlas checks the count, and the recall can close only at
// the far end. Everything drawn here is read from the run's state.

const W = 1000;
const H = 800;
const colX = (i) => 30 + i * 245;
const mid = (i) => colX(i) + 102.5;

const BOX = {
  trigger: { x: 45, y: 28, w: 250, h: 100 },
  boss: { x: 375, y: 28, w: 250, h: 100 },
  memory: { x: 705, y: 28, w: 250, h: 100 },
  phone: { x: colX(0), y: 346, w: 205, h: 120 },
  check: { x: colX(1), y: 346, w: 205, h: 120 },
  save: { x: colX(2), y: 346, w: 205, h: 120 },
  sent: { x: colX(3), y: 346, w: 205, h: 120 },
  ask: { x: colX(1), y: 518, w: 205, h: 84 },
  gate: { x: colX(3), y: 518, w: 205, h: 120 },
  locked: { x: colX(3), y: 686, w: 205, h: 98 },
  tins: { x: 30, y: 644, w: 695, h: 140 },
};
const workerBox = (i) => ({ x: colX(i), y: 172, w: 205, h: 118 });
const at = (b) => ({ left: b.x, top: b.y, width: b.w, height: b.h });

// A path through points with rounded corners.
function route(pts, r = 12) {
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i += 1) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const a = Math.min(r, Math.hypot(x1 - x0, y1 - y0) / 2);
    const b = Math.min(r, Math.hypot(x2 - x1, y2 - y1) / 2);
    const ax = x1 - Math.sign(x1 - x0) * a;
    const ay = y1 - Math.sign(y1 - y0) * a;
    const bx = x1 + Math.sign(x2 - x1) * b;
    const by = y1 + Math.sign(y2 - y1) * b;
    d += ` L${ax},${ay} Q${x1},${y1} ${bx},${by}`;
  }
  const last = pts.at(-1);
  return `${d} L${last[0]},${last[1]}`;
}

// The ids are the wires a write travels, as the engine and the API name them.
const EDGES = [
  { id: 'due>dispatch', pts: [[295, 78], [375, 78]] },
  { id: 'memory>dispatch', pts: [[705, 78], [625, 78]], dashed: true },
  ...[0, 1, 2, 3].map((i) => ({ id: `dispatch>w${i + 1}`, pts: [[500, 128], [500, 150], [mid(i), 150], [mid(i), 172]] })),
  ...[0, 1, 2, 3].map((i) => ({ id: `w${i + 1}>gate`, pts: i === 0 ? [[mid(0), 290], [mid(0), 346]] : [[mid(i), 290], [mid(i), 318], [mid(0), 318], [mid(0), 346]] })),
  { id: 'sys>validator', pts: [[235, 406], [275, 406]] },
  { id: 'validator>ledger', pts: [[480, 406], [520, 406]] },
  { id: 'ledger>found', pts: [[725, 406], [765, 406]] },
  { id: 'validator>ask', pts: [[mid(1), 466], [mid(1), 518]] },
  { id: 'ask>sys', pts: [[275, 560], [mid(0), 560], [mid(0), 466]] },
  { id: 'found>close', pts: [[mid(3), 466], [mid(3), 518]] },
  { id: 'close>clear', pts: [[mid(3), 638], [mid(3), 686]], dashed: true },
  { id: 'ledger>tray', pts: [[mid(2), 466], [mid(2), 644]] },
  { id: 'found>due', pts: [[970, 406], [988, 406], [988, 8], [170, 8], [170, 28]] },
].map((e) => ({ ...e, d: route(e.pts), end: e.pts.at(-1), prev: e.pts.at(-2) }));

const ARROW = 7;
function arrow(e) {
  const [x, y] = e.end;
  const [px, py] = e.prev;
  if (x > px) return `M${x},${y} l-${ARROW},-${ARROW * 0.62} v${ARROW * 1.24} z`;
  if (x < px) return `M${x},${y} l${ARROW},-${ARROW * 0.62} v${ARROW * 1.24} z`;
  if (y > py) return `M${x},${y} l-${ARROW * 0.62},-${ARROW} h${ARROW * 1.24} z`;
  return `M${x},${y} l-${ARROW * 0.62},${ARROW} h${ARROW * 1.24} z`;
}

// One write, one movement: a dot travels the edges a write took, and each edge
// stays lit for a moment after it passes.
function Edges({ pulses, state }) {
  const svg = useRef(null);
  const layer = useRef(null);
  const seen = useRef(null);
  const live = useRef({ dots: [], raf: 0 });

  useEffect(() => {
    const last = pulses.at(-1)?.id ?? 0;
    if (seen.current == null || last < seen.current) {
      seen.current = last;
      return;
    }
    const fresh = pulses.filter((p) => p.id > seen.current);
    seen.current = last;
    for (const p of fresh) {
      const segs = p.wires
        .map((id) => svg.current.querySelector(`[data-edge="${id}"]`))
        .filter(Boolean)
        .map((g) => {
          const path = g.querySelector('.line');
          const len = path.getTotalLength();
          return { g, path, len, dur: Math.max(160, Math.min(700, len / 0.8)) };
        });
      if (!segs.length) continue;
      if (reduced()) {
        segs.forEach((sg) => light(sg.g, p.tone));
        continue;
      }
      const el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      el.setAttribute('r', 5.5);
      el.setAttribute('class', `dot ${p.tone}`);
      layer.current.appendChild(el);
      live.current.dots.push({ el, segs, i: -1, t0: performance.now(), tone: p.tone });
    }
    if (live.current.dots.length && !live.current.raf) live.current.raf = requestAnimationFrame(frame);
  }, [pulses]);

  useEffect(() => () => cancelAnimationFrame(live.current.raf), []);

  function light(g, tone) {
    g.dataset.hot = tone;
    clearTimeout(g._off);
    g._off = setTimeout(() => delete g.dataset.hot, 1100);
  }

  function frame(now) {
    const L = live.current;
    L.dots = L.dots.filter((dt) => {
      let t = now - dt.t0;
      let i = 0;
      while (i < dt.segs.length && t > dt.segs[i].dur) {
        t -= dt.segs[i].dur;
        i += 1;
      }
      if (i >= dt.segs.length) {
        dt.el.remove();
        return false;
      }
      if (i !== dt.i) {
        dt.i = i;
        light(dt.segs[i].g, dt.tone);
      }
      const sg = dt.segs[i];
      const pt = sg.path.getPointAtLength((t / sg.dur) * sg.len);
      dt.el.setAttribute('cx', pt.x);
      dt.el.setAttribute('cy', pt.y);
      return true;
    });
    L.raf = L.dots.length ? requestAnimationFrame(frame) : 0;
  }

  return (
    <svg ref={svg} className="edges" viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
      {EDGES.map((e) => (
        <g key={e.id} data-edge={e.id} className={`edge ${e.dashed ? 'dashed' : ''} ${state[e.id] || ''}`}>
          <path className="line" d={e.d} />
          <path className="tip" d={arrow(e)} />
        </g>
      ))}
      <g ref={layer} />
    </svg>
  );
}

function Node({ id, icon, kind, title, children, chip, chipTop = false, tone, extra = '' }) {
  return (
    <section className={`node ${extra}`} style={at(BOX[id])} data-tone={tone || undefined}>
      <header>
        <span className="tile">{icon}</span>
        <span className="kind">{kind}</span>
        {chip && chipTop && <code className="chip chip-up">{chip}</code>}
      </header>
      <h3>{title}</h3>
      <div className="sub">{children}</div>
      {chip && !chipTop && <code className="chip">{chip}</code>}
    </section>
  );
}

function Orders({ s }) {
  const orders = [s.systems.north, s.systems.east].filter((o) => o.name);
  if (!orders.length) return null;
  return (
    <div className="orders">
      {orders.map((o) => {
        const bad = o.status !== 'active';
        const text = bad ? (o.status === 'repairing' ? 'being repaired' : 'changed, paused') : o.version === 2 ? (o.source === 'model' ? 'repaired by the model' : 'repaired from memory') : 'orders working';
        return <span key={o.name} className={`pill ${bad ? 'red' : o.version === 2 ? 'green' : 'grey'}`}>{o.name.split(' ')[0]}: {text}</span>;
      })}
    </div>
  );
}

function Worker({ w, i, words, focused, onFocus }) {
  const st = workerStatus(w);
  const step = workerStep(w);
  const busy = w.alive && w.party;
  const lease = w.lease || w.ghost;
  const k = lease ? (lease.remainingS == null ? 1 : Math.max(0, Math.min(1, lease.remainingS / lease.ttlS))) : 0;
  const cls = ['worker', !w.alive ? 'down' : busy ? 'busy' : 'idle', focused ? 'focused' : '', st.calling ? 'calling' : ''].join(' ');
  const Tag = busy && w.attempt ? 'button' : 'div';
  const name = busy ? w.party.name : w.ghost ? w.ghost.party : null;
  return (
    <Tag className={cls} style={at(workerBox(i))} {...(Tag === 'button' ? { type: 'button', onClick: onFocus, title: 'Show this call' } : {})}>
      <header>
        <span className="wid">{w.label}</span>
        <span className="kind">{busy && w.run ? 'Subagent' : 'Worker'}</span>
        <span className={`pill ${st.tone}`}>
          {st.calling && <i className="bars"><b /><b /><b /></i>}
          {st.short || st.text}
        </span>
      </header>
      <h3 key={name || 'none'} className={busy ? 'pop' : ''}>{name || `No ${words.party}`}</h3>
      <p className="caption">{workerCaption(w, words)}</p>
      {busy && step < 4 ? (
        <ol className="steps" aria-label="Subagent start-up">
          {['Brief', 'Plan', 'Gate', 'Act'].map((s, n) => <li key={s} className={n < step ? 'done' : n === step ? 'now' : ''}>{s}</li>)}
        </ol>
      ) : (
        <div className={`lease ${!w.alive || (!busy && w.ghost) ? 'draining' : ''}`}>
          <span className="track"><i style={{ transform: `scaleX(${k})` }} /></span>
          <span className="cap">
            {lease ? (w.alive && busy ? 'Lease held, renews every 2 s' : lease.remainingS == null ? 'Lease running out. Nobody renews it' : `Lease runs out in ${Math.max(0, lease.remainingS).toFixed(1)} s`) : 'No lease'}
          </span>
        </div>
      )}
    </Tag>
  );
}

function Tins({ s }) {
  const { removed, sold, onShelf, notLocated } = s.tins;
  const total = s.recall.unitsTotal || 1;
  const open = onShelf + notLocated;
  const a = useTween(removed);
  const b = useTween(sold);
  const c = useTween(open);
  const pct = (n) => `${(n / total) * 100}%`;
  return (
    <section className="tins" style={at(BOX.tins)}>
      <header>
        <h3>Where the {num(total)} {s.words.units} are</h3>
        <span>The recall closes only when the red part is 0</span>
      </header>
      <div className="bar" role="img" aria-label={`${removed} held or returned, ${sold} sold, ${open} open`}>
        <i className="g1" style={{ width: pct(removed) }} />
        <i className="g2" style={{ width: pct(sold) }} />
        <i className="r" style={{ width: pct(open) }} />
      </div>
      <dl>
        <div><dt><i className="g1" />{num(a)}</dt><dd>{s.words.removed}</dd></div>
        <div><dt><i className="g2" />{num(b)}</dt><dd>{s.words.mid}</dd></div>
        <div><dt><i className="r" />{num(c)}</dt><dd>open: {num(onShelf)} {s.words.shelf}, {num(notLocated)} missing</dd></div>
        <Orders s={s} />
      </dl>
    </section>
  );
}

const FLASH = 3200;
const fresh = (s, st) => st && s.t - st.at < FLASH;

export default function Flow({ s, actions }) {
  const [ref, scale] = useFitBox(W, H);
  const now = s.run.simNow;
  const st = s.station;
  const held = s.parties.filter((p) => p.holdForDemo).length;
  const due = s.parties.filter((p) => p.nextActionAt != null && p.nextActionAt <= now && !p.holdForDemo && !(p.lease.remainingS > 0)).length;
  const busy = s.workers.filter((w) => w.alive && w.party).length;
  const onCall = s.workers.filter((w) => w.alive && w.attempt && !w.attempt.final).length;
  const next = s.parties.filter((p) => p.nextActionAt != null && p.nextActionAt > now).sort((x, y) => x.nextActionAt - y.nextActionAt)[0];

  const edge = {};
  const lanes = [1, 2, 3, 4].map((n) => s.workers.find((w) => w.slot === n) || { id: `empty-${n}`, slot: n, label: s.source === 'live' ? '–' : `W${n}`, alive: true, phase: 'idle', party: null, lease: null, ghost: null, attempt: null });
  lanes.forEach((w) => {
    if (w.alive && w.party) edge[`dispatch>w${w.slot}`] = 'on';
    else if (w.ghost) edge[`dispatch>w${w.slot}`] = 'lost';
    if (w.alive && w.party && (w.attempt || w.op)) edge[`w${w.slot}>gate`] = 'on';
  });
  const wd = s.words;

  const v = st.validator;
  const vNow = fresh(s, v);
  const refused = vNow && v.verdict === 'refused';
  const closed = s.recall.status === 'closed';
  const closeRefused = fresh(s, st.close);
  return (
    <div className="flow" ref={ref}>
      <div className="canvas" style={{ width: W, height: H, transform: `translate(-50%, -50%) scale(${scale})` }}>
        <Edges pulses={s.pulses} state={edge} />

        <span className="tag-edge" style={{ left: 580, top: 8 }}>yes: a new {wd.party} joins the queue</span>
        <span className="tag-edge ok" style={{ left: 500, top: 384 }}>valid</span>
        <span className={`tag-edge ${refused ? 'bad' : ''}`} style={{ left: mid(1), top: 492 }}>refused</span>
        <span className="tag-edge" style={{ left: 204, top: 560 }}>ask again, 2 times at most</span>
        <span className="tag-edge" style={{ left: mid(3), top: 492 }}>no</span>
        <span className="tag-edge" style={{ left: mid(3), top: 662 }}>0 {wd.units} open</span>

        <Node id="trigger" icon={<Icon.Bolt />} kind="Trigger" title="New work is due" chip="next_action_at" chipTop tone={fresh(s, st.dispatch) ? 'on' : null}>
          {held ? `${held} ${wd.parties} waiting for release` : due ? `${due} ${due === 1 ? `${wd.party} is` : `${wd.parties} are`} due now` : next ? `Next: ${next.name}, ${simStamp(next.nextActionAt)}` : 'Nothing is waiting'}
        </Node>

        <Node id="boss" icon={<Icon.Database />} kind="The boss" title={`One worker per ${wd.party}`} chip="findOneAndUpdate" chipTop tone={fresh(s, st.dispatch) ? 'on' : null}>
          {busy} busy, {onCall} on a call
        </Node>

        <Node id="memory" icon={<Icon.History />} kind="Memory" title={`Recall similar ${wd.parties}`} chip="$vectorSearch" chipTop tone={fresh(s, st.memory) ? 'on' : null}>
          {st.memory?.episodes?.length ? `${st.memory.episodes.length} past calls for ${st.memory.party}` : 'Up to 3 past calls per decision'}
        </Node>

        {lanes.map((w, i) => (
          <Worker
            key={w.id}
            w={w}
            i={i}
            words={wd}
            focused={!!(s.call && w.attempt && s.call.attemptId === w.attempt.id)}
            onFocus={() => w.attempt && actions.focus(w.attempt.id)}
          />
        ))}

        <Node id="phone" icon={<Icon.Phone />} kind="On the phone" title={`${wd.Party} says its counts`} chip="tool_events" tone={vNow ? 'on' : null}>
          {v ? (v.reported != null ? `${v.party}: ${num(v.reported)} ${wd.units}` : v.party) : 'A count, a batch code, yes or no'}
        </Node>

        <Node
          id="check"
          icon={refused ? <Icon.ShieldX /> : <Icon.Shield />}
          kind={refused ? 'Refused by Atlas' : 'Atlas checks'}
          title={`Do the ${wd.units} add up?`}
          chip="validator on parties"
          tone={refused ? 'bad' : vNow ? 'ok' : null}
        >
          {v && v.reported != null && v.shippedIn != null ? (
            <span className="versus">
              <b>{num(v.reported)}</b> said <em>{v.verdict === 'refused' ? 'vs' : '='}</em> <b>{num(v.shippedIn)}</b> shipped
            </span>
          ) : v ? (
            v.verdict === 'refused' ? 'Atlas refused the write' : 'Counts add up'
          ) : (
            'The rule lives in the database'
          )}
        </Node>

        <Node id="save" icon={<Icon.Save />} kind="Write" title="Save to the ledger" chip="transaction" tone={fresh(s, st.ledger) ? 'ok' : null}>
          {st.ledger && fresh(s, st.ledger) ? (st.ledger.ms != null ? `${st.ledger.party}, in ${st.ledger.ms} ms` : st.ledger.party) : `Claim and ${wd.party}, together`}
        </Node>

        <Node id="sent" icon={<Icon.Branch />} kind="Check" title={`Sent ${wd.units} elsewhere?`} chip="change stream" tone={fresh(s, st.found) ? 'on' : null}>
          {st.found?.yes ? `Found ${st.found.name}` : wd.shop}
        </Node>

        <Node id="ask" icon={<Icon.Ask />} kind="On the phone" title={`Ask the ${wd.party} again`} tone={fresh(s, st.ask) ? 'bad' : null}>
          Agent says both numbers
        </Node>

        <Node id="gate" icon={<Icon.Flag />} kind="Gate" title="Can the recall close?" chip="validator on recalls" tone={closeRefused ? 'bad' : null}>
          {closeRefused ? `Refused: ${num(st.close.open)} ${wd.units} open` : `Only with 0 ${wd.units} open`}
        </Node>

        <Node id="locked" icon={closed ? <Icon.Check /> : <Icon.Lock />} kind={closed ? 'All clear' : 'Locked'} title="Recall closed" extra={closed ? 'done' : 'locked'}>
          {closed ? `Every ${wd.unit} is explained` : `${num(s.recall.open)} ${wd.units} still open`}
        </Node>

        <Tins s={s} />

      </div>
    </div>
  );
}
