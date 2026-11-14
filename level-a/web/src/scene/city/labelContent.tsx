// What the labels say. Words follow the screen's vocabulary: panels, batch, parties, "not located",
// "awaiting fix", "disputed", SIMULATED beside every simulated party; Atlas's own text is never
// rewritten (the HUD's feed carries it; the city only marks where it happened).

import type { CSSProperties } from "react";
import type { Party, SignalView, SystemView, WallState, WorkerView } from "../../api/types";
import { BUCKET_LABELS, COLORS, FONT_DISPLAY, FONT_MONO } from "../theme";
import type { LabelSpec } from "./Labels";
import { TONE_COLOR, type CityLayout, type PartyPlace } from "./layout";

export const LABEL_CSS = `
.acs-root { position: absolute; inset: 0; overflow: hidden; background: ${COLORS.void}; isolation: isolate; z-index: 0; }
.acs-root canvas { display: block; touch-action: none; }
.acs-labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; z-index: 1; }
.acs-label { position: absolute; left: 0; top: 0; white-space: nowrap; will-change: transform; transition: opacity 180ms ease; opacity: 0; pointer-events: none; transform: translate3d(-9999px, 0, 0); }
.acs-label[data-on="1"] { opacity: 1; pointer-events: auto; }
.acs-label[data-on="1"]:not(:has(.acs-pill)) { pointer-events: none; }
.acs-pill { position: relative; display: flex; flex-direction: column; gap: 2px; padding: 4px 9px 5px; border-radius: 6px; background: rgba(9, 12, 18, 0.86); border: 1px solid rgba(160, 180, 205, 0.16); color: ${COLORS.ink}; box-shadow: 0 8px 22px rgba(0, 0, 0, 0.45); cursor: pointer; }
.acs-pill::after { content: ""; position: absolute; left: 50%; top: 100%; width: 1px; height: 9px; background: linear-gradient(rgba(200, 215, 235, 0.5), rgba(200, 215, 235, 0)); }
.acs-pill.is-selected { border-color: ${COLORS.live}; box-shadow: 0 0 0 1px ${COLORS.live}55, 0 10px 26px rgba(0, 0, 0, 0.55); }
.acs-pill.is-quiet { padding: 3px 7px 4px; background: rgba(9, 12, 18, 0.72); }
.acs-name { display: flex; align-items: center; gap: 6px; font: 600 15px/1.08 ${FONT_DISPLAY}; letter-spacing: 0.01em; }
.acs-pill.is-quiet .acs-name { font-size: 13px; font-weight: 500; color: ${COLORS.ink2}; }
.acs-dot { width: 7px; height: 7px; border-radius: 50%; flex: none; box-shadow: 0 0 8px currentColor; }
.acs-sub { font: 500 11px/1.25 ${FONT_MONO}; color: #a3b1c2; }
.acs-tag { font: 600 9px/1 ${FONT_MONO}; letter-spacing: 0.08em; padding: 2px 4px 2px; border-radius: 3px; color: #c7d2df; border: 1px solid rgba(199, 210, 223, 0.35); }
.acs-chips { display: flex; gap: 4px; margin-top: 2px; }
.acs-chip { display: inline-flex; align-items: center; gap: 4px; font: 600 10px/1 ${FONT_MONO}; letter-spacing: 0.04em; padding: 3px 5px; border-radius: 4px; }
.acs-chip i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; display: inline-block; }
.acs-chip.blink i { animation: acs-blink 0.9s steps(2, start) infinite; }
@keyframes acs-blink { to { visibility: hidden; } }
.acs-card { width: 290px; padding: 9px 11px 10px; gap: 5px; white-space: normal; }
.acs-card .acs-name { font-size: 18px; }
.acs-bar { display: flex; height: 7px; border-radius: 2px; overflow: hidden; background: rgba(255, 255, 255, 0.06); margin: 3px 0 1px; }
.acs-legend { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px; font: 500 11px/1.35 ${FONT_MONO}; color: #c3cedb; }
.acs-legend span { display: flex; align-items: center; gap: 5px; }
.acs-legend b { margin-left: auto; font-weight: 600; color: ${COLORS.ink}; }
.acs-legend em { width: 7px; height: 7px; border-radius: 1px; display: inline-block; }
.acs-rule { height: 1px; background: rgba(160, 180, 205, 0.16); margin: 2px 0; }
.acs-worker { display: flex; align-items: baseline; gap: 6px; padding: 2px 7px 3px; border-radius: 5px; background: rgba(9, 12, 18, 0.82); border: 1px solid rgba(108, 196, 255, 0.35); color: ${COLORS.ink}; pointer-events: none; }
.acs-worker b { font: 700 15px/1 ${FONT_DISPLAY}; letter-spacing: 0.02em; }
.acs-worker span { font: 500 10.5px/1 ${FONT_MONO}; color: #9fb0c2; }
.acs-flag { display: flex; flex-direction: column; gap: 1px; padding: 4px 8px 5px; border-radius: 5px; background: rgba(9, 12, 18, 0.86); border-left: 3px solid; color: ${COLORS.ink}; pointer-events: none; }
.acs-flag b { font: 700 14px/1.05 ${FONT_DISPLAY}; letter-spacing: 0.03em; text-transform: uppercase; }
.acs-flag span { font: 500 10.5px/1.25 ${FONT_MONO}; color: #a3b1c2; }
.acs-signal { width: 270px; white-space: normal; }
`;

const TYPE_WORD: Record<Party["type"], string> = { distributor: "distributor", installer: "installer", owner: "building owner" };

function mmss(s: number) {
  const t = Math.max(0, Math.floor(s));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}

export function partySummary(place: PartyPlace): string {
  const p = place.party;
  const parts: string[] = [];
  const disputed = place.disputedIn + place.disputedOut;
  if (disputed > 0) parts.push(`${disputed} disputed`);
  const notLocated = p.unresolved + place.transitIn;
  if (notLocated > 0) parts.push(`${notLocated} not located`);
  if (p.installed > 0) parts.push(`${p.installed} awaiting fix`);
  if (parts.length === 0) parts.push(p.state === "reconciled" || p.state === "claimed" ? p.state : `${p.state} · ${p.shipped_in} shipped in`);
  return parts.join(" · ");
}

function Chips({ party }: { party: Party }) {
  const call = party.live_attempt;
  const lease = party.lease;
  if (!call && !lease) return null;
  return (
    <div className="acs-chips">
      {call && (
        <span
          className={`acs-chip${call.state === "dialed" ? " blink" : ""}`}
          style={{ color: call.real ? COLORS.real : COLORS.live, background: call.real ? "rgba(255,201,74,0.13)" : "rgba(108,196,255,0.12)" }}
        >
          <i />
          {call.real ? "REAL CALL" : "SIM CALL"} · {call.state} {mmss(call.since_s)}
        </span>
      )}
      {lease && (
        <span className="acs-chip" style={{ color: lease.expired ? COLORS.bad : "#9fd8ff", background: lease.expired ? "rgba(255,90,71,0.13)" : "rgba(108,196,255,0.08)" }}>
          {lease.owner} · {lease.expired ? "lease expired" : `lease ${Math.max(0, Math.ceil(lease.remaining_s))}s`}
        </span>
      )}
    </div>
  );
}

function PartyPill({ place, selected, quiet }: { place: PartyPlace; selected: boolean; quiet: boolean }) {
  const p = place.party;
  return (
    <div className={`acs-pill${selected ? " is-selected" : ""}${quiet ? " is-quiet" : ""}`}>
      <div className="acs-name">
        <span className="acs-dot" style={{ color: TONE_COLOR[place.tone], background: TONE_COLOR[place.tone] }} />
        {p.name}
        {p.simulated && !quiet && <span className="acs-tag">SIMULATED</span>}
      </div>
      {!quiet && <div className="acs-sub">{partySummary(place)}</div>}
      {!quiet && <Chips party={p} />}
    </div>
  );
}

function PartyCard({ place, selected }: { place: PartyPlace; selected: boolean }) {
  const p = place.party;
  const total = Math.max(1, p.shipped_in);
  const checks = p.checks;
  return (
    <div className={`acs-pill acs-card${selected ? " is-selected" : ""}`}>
      <div className="acs-name">
        <span className="acs-dot" style={{ color: TONE_COLOR[place.tone], background: TONE_COLOR[place.tone] }} />
        {p.name}
        {p.simulated && <span className="acs-tag">SIMULATED</span>}
      </div>
      <div className="acs-sub">
        {TYPE_WORD[p.type]} · tier {p.tier} · found via {p.found_via.replace("_", " ")} · {p.channel === "order_system" ? "order system" : p.channel}
      </div>
      <div className="acs-sub">
        {p.state} · attempts {p.attempts}
        {p.next_label ? ` · next ${p.next_label}` : ""}
        {p.hold_for_demo ? " · held for demo" : ""}
      </div>
      <div className="acs-rule" />
      <div className="acs-sub" style={{ color: COLORS.ink }}>
        {p.shipped_in.toLocaleString("en-US")} panels shipped in · {partySummary(place)}
      </div>
      <div className="acs-bar">
        {BUCKET_LABELS.map((b) => (p[b.key] > 0 ? <div key={b.key} style={{ width: `${(p[b.key] / total) * 100}%`, background: b.color }} /> : null))}
      </div>
      <div className="acs-legend">
        {BUCKET_LABELS.map((b) => (
          <span key={b.key} style={{ opacity: p[b.key] > 0 ? 1 : 0.45 }}>
            <em style={{ background: b.color }} />
            {b.label}
            <b>{p[b.key]}</b>
          </span>
        ))}
      </div>
      {(p.connected_panels > 0 || checks) && <div className="acs-rule" />}
      {p.connected_panels > 0 && (
        <div className="acs-sub">
          connected panels {p.connected_panels} · fixed by own test {p.fixed_by_panel} · without a pass {p.connected_unfixed}
        </div>
      )}
      {checks && (
        <div className="acs-sub">
          model {checks.model_seen ?? "?"} · display {checks.version_seen ?? "?"} · sounder {checks.sounder_test === null ? "?" : checks.sounder_test ? "yes" : "no"}
        </div>
      )}
      {p.escalation && <div className="acs-sub" style={{ color: COLORS.warn }}>{p.escalation}</div>}
      <Chips party={p} />
    </div>
  );
}

function Flag({ color, title, sub, style }: { color: string; title: string; sub?: string; style?: CSSProperties }) {
  return (
    <div className="acs-flag" style={{ borderLeftColor: color, ...style }}>
      <b style={{ color }}>{title}</b>
      {sub && <span>{sub}</span>}
    </div>
  );
}

function workerText(w: WorkerView): string {
  if (!w.alive) return "killed";
  if (!w.party_name) return "idle";
  return w.party_name;
}

const SYSTEM_STATUS: Record<SystemView["status"], { word: string; color: string }> = {
  ok: { word: "ok", color: COLORS.good },
  contract_failed: { word: "contract failed", color: COLORS.bad },
  repairing: { word: "repairing", color: COLORS.warn },
  paused: { word: "paused", color: COLORS.ink3 },
};

export function systemTitle(sys: SystemView): string {
  return sys.adapter_version ? `${sys.label} v${sys.adapter_version}` : sys.label;
}

function signalSub(s: SignalView): string {
  if (s.status === "approved") return `approved by ${s.approved_by ?? "?"} · recall ${s.recall_id ?? "?"} opened`;
  if (s.status === "dismissed") return "dismissed";
  return `${s.rule} · ${s.sites.join(", ")} · awaiting a person's approval`;
}

export function buildLabels(
  state: WallState | null,
  layout: CityLayout,
  hovered: string | null,
  selected: string | null,
): LabelSpec[] {
  const specs: LabelSpec[] = [];
  for (const place of layout.order) {
    const p = place.party;
    const isSel = place.id === selected;
    const isHov = place.id === hovered;
    let priority = 100 + Math.min(300, place.open / 4);
    if (place.tone === "disputed") priority += 450;
    else if (place.tone === "unlocated") priority += 350;
    else if (place.tone === "awaiting") priority += 250;
    if (p.tier === 1) priority += 150;
    if (p.slice) priority += 120;
    if (p.lease) priority += 300;
    if (p.live_attempt) priority += 500;
    const quiet = !isSel && !isHov && priority < 260 && place.tone === "done";
    specs.push({
      key: `party:${place.id}`,
      anchor: place.label,
      priority: isHov ? 9000 : isSel ? 8000 : priority,
      pinned: isHov || isSel,
      partyId: place.id,
      node: isHov ? <PartyCard place={place} selected={isSel} /> : <PartyPill place={place} selected={isSel} quiet={quiet} />,
    });
  }

  for (const e of layout.edges) {
    if (e.disputed <= 0) continue;
    const from = layout.places.get(e.from)?.party.name ?? e.from;
    const to = layout.places.get(e.to)?.party.name ?? e.to;
    specs.push({
      key: `edge:${e.key}`,
      anchor: [e.mid[0], e.mid[1] + 1.4, e.mid[2]],
      priority: 700 + e.disputed,
      node: <Flag color={COLORS.bad} title={`${e.disputed} disputed`} sub={`${from} → ${to}`} />,
    });
  }

  const towers = layout.dispatch.towers;
  for (const w of state?.workers ?? []) {
    const idx = w.saboteur ? 4 : Math.max(0, Math.min(3, parseInt(w.id.replace(/\D/g, ""), 10) - 1));
    const t = towers[idx];
    const color = !w.alive ? COLORS.bad : w.saboteur ? "#ff4fa3" : w.party_id ? COLORS.live : COLORS.ink3;
    specs.push({
      key: `worker:${w.id}`,
      anchor: [t[0], t[1] + 1.2, t[2]],
      priority: 760,
      lift: 6,
      node: (
        <div className="acs-worker" style={{ borderColor: `${color}88` }}>
          <b style={{ color }}>{w.id}</b>
          <span>{workerText(w)}</span>
        </div>
      ),
    });
  }

  for (const sys of state?.systems ?? []) {
    if (sys.system !== "north" && sys.system !== "east") continue;
    const place = layout.order.find((pp) => pp.party.order_system === sys.system);
    if (!place?.terminal) continue;
    const st = SYSTEM_STATUS[sys.status];
    specs.push({
      key: `system:${sys.system}`,
      anchor: [place.terminal[0], place.terminal[1] + 1.3, place.terminal[2]],
      priority: sys.status === "ok" ? 520 : 820,
      node: <Flag color={st.color} title={systemTitle(sys)} sub={st.word} />,
    });
  }

  const recall = state?.recall;
  specs.push({
    key: "plant",
    anchor: layout.plant.label,
    priority: 600,
    node: (
      <Flag
        color={COLORS.warn}
        title={recall?.supplier ?? "Kestrel Life Safety"}
        sub={recall ? `plant · batch ${recall.batch_code} · ${recall.model}` : "plant"}
      />
    ),
  });

  const totalReturned = (state?.parties ?? []).reduce((s, p) => s + p.returned, 0);
  if (totalReturned > 0) {
    specs.push({
      key: "returns",
      anchor: [layout.plant.yard[0], 2.4, layout.plant.yard[2]],
      priority: 240,
      node: <Flag color={COLORS.returned} title={`${totalReturned.toLocaleString("en-US")} returned`} sub="back at the plant" />,
    });
  }

  const mon = state?.monitoring;
  const svc = layout.dispatch.service;
  specs.push({
    key: "service",
    anchor: [svc[0], svc[1] + 2.6, svc[2]],
    priority: 640,
    node: (
      <Flag
        color={(mon?.failing ?? 0) > 0 ? COLORS.bad : COLORS.good}
        title="panel service"
        sub={mon ? `${mon.connected} connected · ${mon.failing} failing · ${mon.passed_after_fix} passed after fix` : "connected panels"}
      />
    ),
  });

  const signal = latestSignal(state);
  if (signal) {
    const color = signal.status === "proposed" ? COLORS.bad : signal.status === "approved" ? COLORS.warn : COLORS.ink3;
    specs.push({
      key: "signal",
      anchor: [layout.plant.plate[0], layout.plant.plate[1] + 1.2, layout.plant.plate[2]],
      priority: signal.status === "proposed" ? 2000 : 560,
      pinned: signal.status === "proposed",
      node: (
        <div className="acs-signal">
          <Flag color={color} title={`signal ${signal._id} · ${signal.batch_code} · ${signal.status}`} sub={signalSub(signal)} />
        </div>
      ),
    });
  }

  specs.push({
    key: "dispatch",
    anchor: [layout.dispatch.hub[0], 0.6, layout.dispatch.hub[2] + 9.6],
    priority: 180,
    lift: 2,
    node: <Flag color={COLORS.live} title="All Clear dispatch" sub={state ? `${(state.workers ?? []).filter((w) => w.alive).length} workers alive · ${state.calls_in_flight.length} calls in flight` : "workers"} />,
  });
  return specs;
}

export function latestSignal(state: WallState | null): SignalView | null {
  const list = state?.monitoring?.signals ?? [];
  return list.find((s) => s.status === "proposed") ?? list.find((s) => s.status === "approved") ?? list[0] ?? null;
}
