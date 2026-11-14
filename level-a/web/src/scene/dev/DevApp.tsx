// Standalone dev page for the 3D scene (web/scene.html). Renders CityScene full-screen from the
// fixtures and lets you step through the run by keyboard; ?panel=1 renders the PanelModel instead.
//   ?state=early|mid|late|closed   ?auto=1 (no help, for screenshots)   ?select=<party_id>
//   ?hud=1 (simulated HUD insets)  ?insets=top,right,bottom,left        ?calm=1
//   ?panel=1&phase=idle|testing|updating&flash=pass|fail&sw=2.4|2.5&fixed=1

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { Party, WallState } from "../../api/types";
import CityScene, { PanelModel, type PanelModelProps } from "../CityScene";
import { buildStage, panelEvent, panelFixture, recompute, simLabel, type Stage } from "./fixtures";

const params = new URLSearchParams(window.location.search);
const AUTO = params.get("auto") === "1";

function clone(s: WallState): WallState {
  return structuredClone(s);
}

const HUD_INSETS = { top: 92, right: 400, bottom: 112, left: 24 };

function parseInsets(): { top: number; right: number; bottom: number; left: number } | undefined {
  const raw = params.get("insets");
  if (raw) {
    const [top, right, bottom, left] = raw.split(",").map((x) => Number(x) || 0);
    return { top, right, bottom, left };
  }
  return params.get("hud") === "1" ? HUD_INSETS : undefined;
}

const NEW_OWNERS = ["Bathurst Commons", "Ossington Yards", "Wychwood Barns", "Leslieville Court", "Roncesvalles House", "Trinity Bellwoods Lofts"];

let refusalSeq = 0;
function stamp(): string {
  refusalSeq += 1;
  return `2026-11-14T19:${String(10 + (refusalSeq % 50)).padStart(2, "0")}:00.${refusalSeq}-05:00`;
}

/** One second of the run as the API would report it on the next poll. */
function tick(prev: WallState, killedAt: Map<string, number>, t: number): WallState {
  const s = clone(prev);
  for (const w of s.workers) {
    if (w.since_s !== null) w.since_s += 1;
    const k = killedAt.get(w.id);
    if (!w.alive && k !== undefined && t - k >= 3) {
      w.alive = true;
      w.restarts += 1;
      w.party_id = null;
      w.party_name = null;
      w.kind = null;
      w.since_s = null;
      killedAt.delete(w.id);
    }
  }
  for (const p of s.parties) {
    if (p.live_attempt) p.live_attempt.since_s += 1;
    const l = p.lease;
    if (!l) continue;
    const owner = s.workers.find((w) => w.id === l.owner);
    if (owner?.alive) {
      l.remaining_s -= 1;
      if (l.remaining_s < 6) l.remaining_s = 8;
      l.expired = false;
    } else {
      l.remaining_s -= 1;
      l.expired = l.remaining_s <= 0;
      if (l.remaining_s <= -2) {
        const adopter = s.workers.find((w) => w.alive && !w.saboteur && w.id !== l.owner && !w.party_id) ?? s.workers.find((w) => w.alive && !w.saboteur && w.id !== l.owner && !s.parties.find((q) => q.party_id === w.party_id)?.live_attempt?.real);
        if (adopter) {
          const old = s.parties.find((q) => q.party_id === adopter.party_id);
          if (old && old !== p) old.lease = null;
          p.lease = { owner: adopter.id, token: l.token + 1, remaining_s: 8, expired: false };
          adopter.party_id = p.party_id;
          adopter.party_name = p.name;
          adopter.kind = "contact_party";
          adopter.since_s = 0;
          adopter.lease_token = l.token + 1;
          if (s.run) s.run.adoptions += 1;
          s.feed.unshift({ id: `f-ad-${t}`, at: stamp(), sim_label: s.run?.sim_label ?? "", system: `worker ${adopter.id}`, text: `adopted ${p.name}: 1 in doubt, 4 documents read, 0 steps replayed`, level: "info", party_id: p.party_id, kind: "adoption" });
        }
      }
    }
  }
  for (const c of s.calls_in_flight) c.since_s += 1;
  return s;
}

function Help({ stage, onStage }: { stage: Stage; onStage: (s: Stage) => void }) {
  const box: CSSProperties = { position: "fixed", left: 12, bottom: 12, padding: "10px 12px", background: "rgba(8,11,17,0.88)", border: "1px solid rgba(160,180,205,0.2)", borderRadius: 8, color: "#c7d2df", font: "12px/1.5 ui-monospace, Consolas, monospace", zIndex: 10, maxWidth: 380 };
  return (
    <div style={box}>
      <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
        {(["early", "mid", "late", "closed"] as Stage[]).map((s, i) => (
          <button key={s} onClick={() => onStage(s)} style={{ background: s === stage ? "#6cc4ff" : "#1a212b", color: s === stage ? "#03131e" : "#c7d2df", border: 0, borderRadius: 4, padding: "3px 8px", cursor: "pointer", font: "inherit" }}>
            {i + 1} {s}
          </button>
        ))}
      </div>
      k kill a worker · p panel PASS (fix) · f panel FAIL · r refusal · x close (refused)
      <br />n North orders status · s signal proposed/approved · w saboteur · d discover a party
      <br />l real call at Harbour Fire · c calm · i HUD insets · esc deselect · h hide
    </div>
  );
}

function HudMock({ insets }: { insets: { top: number; right: number; bottom: number; left: number } }) {
  const panel: CSSProperties = { position: "fixed", background: "rgba(10,14,20,0.82)", border: "1px solid rgba(160,180,205,0.14)", zIndex: 5, pointerEvents: "none", color: "#5d6b7c", font: "11px ui-monospace, Consolas, monospace", padding: 8, boxSizing: "border-box" };
  return (
    <>
      {insets.top > 0 && <div style={{ ...panel, left: 0, right: 0, top: 0, height: insets.top }}>HUD top</div>}
      {insets.right > 0 && <div style={{ ...panel, right: 0, top: insets.top, bottom: insets.bottom, width: insets.right }}>HUD right</div>}
      {insets.bottom > 0 && <div style={{ ...panel, left: 0, right: 0, bottom: 0, height: insets.bottom }}>HUD bottom</div>}
      {insets.left > 0 && <div style={{ ...panel, left: 0, top: insets.top, bottom: insets.bottom, width: insets.left }} />}
    </>
  );
}

function CityDev() {
  const [stage, setStage] = useState<Stage>(((params.get("state") as Stage) || "mid") as Stage);
  const [state, setState] = useState<WallState>(() => buildStage(stage));
  const [selected, setSelected] = useState<string | null>(params.get("select"));
  const [calm, setCalm] = useState(params.get("calm") === "1");
  const [help, setHelp] = useState(!AUTO);
  const [insets, setInsets] = useState(parseInsets);
  const killedAt = useRef(new Map<string, number>());
  const seconds = useRef(0);

  const goStage = useCallback((s: Stage) => {
    setStage(s);
    setState(buildStage(s));
    killedAt.current.clear();
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => {
      seconds.current += 1;
      setState((s) => tick(s, killedAt.current, seconds.current));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  const edit = useCallback((fn: (s: WallState) => void) => {
    setState((prev) => {
      const s = clone(prev);
      fn(s);
      return recompute(s);
    });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      if (k >= "1" && k <= "4") goStage((["early", "mid", "late", "closed"] as Stage[])[Number(k) - 1]);
      else if (k === "escape") setSelected(null);
      else if (k === "c") setCalm((v) => !v);
      else if (k === "h") setHelp((v) => !v);
      else if (k === "i") setInsets((v) => (v ? undefined : HUD_INSETS));
      else if (k === "k")
        edit((s) => {
          const w = s.workers.find((x) => x.id === "w2" && x.alive && x.party_id) ?? s.workers.find((x) => x.alive && x.party_id && !x.saboteur);
          if (!w) return;
          w.alive = false;
          w.party_id = null;
          w.party_name = null;
          w.kind = null;
          killedAt.current.set(w.id, seconds.current);
          if (s.run) s.run.kills += 1;
          s.feed.unshift({ id: `f-k-${seconds.current}`, at: stamp(), sim_label: s.run?.sim_label ?? "", system: "supervisor", text: `worker ${w.id} killed (${s.run?.kills ?? 1})`, level: "warn", party_id: null, kind: "kill" });
        });
      else if (k === "p" || k === "f")
        edit((s) => {
          const pass = k === "p";
          const panels = s.monitoring.panels;
          const eligible = (sp: string) => {
            const c = panels.find((x) => x.serial === sp);
            const party = c?.party_id ? s.parties.find((q) => q.party_id === c.party_id) : undefined;
            return c && !c.fixed && (!pass || (party && party.installed > 0));
          };
          const pick = ["D7-4471-0193", ...panels.map((x) => x.serial)].find((sp) => eligible(sp));
          const c = panels.find((x) => x.serial === pick);
          if (!c) return;
          const party = c.party_id ? s.parties.find((q) => q.party_id === c.party_id) : undefined;
          const n = 60 + s.monitoring.events.length;
          let effect: string | null = null;
          if (pass) {
            c.sw_version = "2.5";
            c.fault = false;
            c.fixed = true;
            if (party) {
              party.installed -= 1;
              party.fixed += 1;
              party.fixed_by_panel += 1;
              party.connected_unfixed = Math.max(0, party.connected_unfixed - 1);
              effect = `${party.name}: 1 panel fixed`;
            }
          }
          c.last_result = pass ? "pass" : "fail";
          const ev = panelEvent(n, c.serial, c.site_label, pass ? "pass" : "fail", c.sw_version, s.run?.sim_label ?? simLabel(0), effect);
          s.monitoring.events = [ev, ...s.monitoring.events].slice(0, 20);
          s.monitoring.last_event = ev;
        });
      else if (k === "r")
        edit((s) => {
          const candidates = s.parties.filter((p) => p.type !== "distributor");
          const p = candidates[(seconds.current * 7) % Math.max(1, candidates.length)];
          if (!p) return;
          s.refusals = [{ at: stamp(), sim_label: s.run?.sim_label ?? "", rule: "parties validator", message: `REJECTED by Atlas: Document failed validation on parties. Reported ${p.shipped_in + 20}, shipped_in ${p.shipped_in}`, party_id: p.party_id }, ...s.refusals];
        });
      else if (k === "x")
        edit((s) => {
          s.refusals = [{ at: stamp(), sim_label: s.run?.sim_label ?? "", rule: "close gate", message: `REJECTED by Atlas: Document failed validation on recalls. open_units ${s.metrics?.open_units ?? 0}`, party_id: null }, ...s.refusals];
        });
      else if (k === "n")
        edit((s) => {
          const sys = s.systems.find((x) => x.system === "north");
          if (!sys) return;
          const order = ["ok", "contract_failed", "repairing"] as const;
          const i = order.indexOf(sys.status as (typeof order)[number]);
          sys.status = order[(i + 1) % order.length];
          if (sys.status === "ok") {
            sys.adapter_version = (sys.adapter_version ?? 1) >= 2 ? 1 : 2;
            sys.adapter_source = sys.adapter_version === 2 ? "model" : "seed";
          }
        });
      else if (k === "s")
        edit((s) => {
          const sig = s.monitoring.signals[0];
          if (!sig) return;
          sig.status = sig.status === "proposed" ? "approved" : "proposed";
          sig.approved_by = sig.status === "approved" ? "operator-1" : null;
          sig.recall_id = sig.status === "approved" ? "r-1" : null;
        });
      else if (k === "w")
        edit((s) => {
          const on = !s.workers.some((w) => w.saboteur);
          if (s.run) s.run.saboteur.on = on;
          if (!on) {
            s.workers = s.workers.filter((w) => !w.saboteur);
            for (const p of s.parties) if (p.lease?.owner === "w5") p.lease = null;
            return;
          }
          const target = s.parties.find((p) => !p.lease && p.type !== "distributor" && p.state !== "reconciled") ?? s.parties[0];
          if (!target) return;
          target.lease = { owner: "w5", token: 1, remaining_s: 8, expired: false };
          s.workers.push({ id: "w5", alive: true, saboteur: true, party_id: target.party_id, party_name: target.name, kind: "saboteur", since_s: 0, lease_token: 1, restarts: 0 });
        });
      else if (k === "d")
        edit((s) => {
          const from = s.parties.find((p) => p.type === "installer" && p.in_stock >= 6);
          const name = NEW_OWNERS.find((nm) => !s.parties.some((p) => p.name === nm));
          if (!from || !name) return;
          const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-");
          const qty = Math.min(12, from.in_stock);
          from.in_stock -= qty;
          s.edges.push({ from_party: from.party_id, to_party: id, sender_qty: qty, recipient_qty: null, corroborated_qty: 0, disputed_qty: 0, state: "awaiting_recipient" });
          const stub: Party = {
            ...structuredClone(from),
            party_id: id,
            name,
            type: "owner",
            tier: 3,
            channel: "sim",
            simulated: true,
            state: "pending",
            shipped_in: 0,
            in_stock: 0,
            installed: 0,
            fixed: 0,
            returned: 0,
            forwarded: 0,
            unresolved: 0,
            fixed_by_panel: 0,
            connected_unfixed: 0,
            connected_panels: 0,
            checks: null,
            found_via: "claim",
            ladder_step: 0,
            hold_for_demo: false,
            slice: false,
            live_attempt: null,
            attempts: 0,
            lease: null,
            in_flight: null,
            order_system: null,
            sites: [name],
            escalation: null,
          };
          s.parties.push(stub);
        });
      else if (k === "l")
        edit((s) => {
          const p = s.parties.find((q) => q.party_id === "harbour-fire");
          if (!p) return;
          const order = [null, "intent", "dialed", "connected"] as const;
          const next = order[(order.indexOf(p.live_attempt?.state ?? null) + 1) % order.length];
          s.calls_in_flight = s.calls_in_flight.filter((c) => c.party_id !== p.party_id);
          if (!next) {
            p.live_attempt = null;
            return;
          }
          p.live_attempt = { attempt_id: "at-0500", state: next, real: true, since_s: 0 };
          s.calls_in_flight.push({ attempt_id: "at-0500", party_id: p.party_id, party_name: p.name, real: true, state: next, since_s: 0 });
        });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [edit, goStage]);

  return (
    <div style={{ position: "fixed", inset: 0, background: "#05070c" }}>
      <CityScene state={state} selectedPartyId={selected} onSelectParty={setSelected} insets={insets} calm={calm} />
      {insets && <HudMock insets={insets} />}
      {help && <Help stage={stage} onStage={goStage} />}
    </div>
  );
}

function PanelDev() {
  const [phase, setPhase] = useState<PanelModelProps["phase"]>((params.get("phase") as PanelModelProps["phase"]) || "idle");
  const [flash, setFlash] = useState<PanelModelProps["flash"]>((params.get("flash") as PanelModelProps["flash"]) || null);
  const [sw, setSw] = useState(params.get("sw") || "2.4");
  const [fixed, setFixed] = useState(params.get("fixed") === "1");
  const [last, setLast] = useState<"pass" | "fail" | null>(fixed ? "pass" : "fail");
  const state = useMemo(() => panelFixture(sw, last, fixed), [sw, last, fixed]);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase();
      window.clearTimeout(timer.current);
      if (k === "t") {
        setFlash(null);
        setPhase("testing");
        timer.current = window.setTimeout(() => {
          const pass = sw === "2.5";
          setPhase("idle");
          setFlash(pass ? "pass" : "fail");
          setLast(pass ? "pass" : "fail");
          if (pass) setFixed(true);
        }, 2600);
      } else if (k === "u") {
        setFlash(null);
        setPhase("updating");
        timer.current = window.setTimeout(() => {
          setPhase("idle");
          setSw("2.5");
        }, 2800);
      } else if (k === "r") {
        setPhase("idle");
        setFlash(null);
        setSw("2.4");
        setFixed(false);
        setLast("fail");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sw]);

  return (
    <div style={{ position: "fixed", inset: 0, background: "#0b0e13" }}>
      <PanelModel state={state} phase={phase} flash={flash} />
      {!AUTO && (
        <div style={{ position: "fixed", left: 12, bottom: 12, color: "#c7d2df", font: "12px ui-monospace, Consolas, monospace", background: "rgba(8,11,17,0.85)", padding: "8px 10px", borderRadius: 6 }}>
          t run test · u apply update · r reset · phase {phase} · flash {flash ?? "none"} · sw {sw}
        </div>
      )}
    </div>
  );
}

export default function DevApp() {
  return params.get("panel") === "1" ? <PanelDev /> : <CityDev />;
}
