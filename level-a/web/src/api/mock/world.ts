// The mock backend's engine: a discrete-event simulation of one All Clear run, replayed from
// scratch for every read. Its state is a function of (the epoch, the operator's action log, now),
// so the 3D screen and the panel page, in two tabs, always agree. Every ledger write goes through
// post(), which applies the same rules Atlas enforces (conservation, fixed needs evidence, the
// panel outranks the phone) and quotes the refusal verbatim, so mass balance is 2,400 in every
// state by construction and is checked again by invariant 4.

import type {
  CallInFlight,
  ConnectedPanel,
  Edge,
  FeedLine,
  FoundVia,
  Level,
  Metrics,
  OperatorAction,
  OperatorResult,
  PanelActionResult,
  PanelEventView,
  PanelPageState,
  Party,
  PartyState,
  SignalView,
  SystemView,
  WallState,
  WorkerView,
} from "../types";
import { APPROVE_AT, MockClock, REFERENCE_S_PER_HOUR, RUN_END, SIM_START, SLICE_AT, hourLabel } from "./clock";
import {
  AFFECTED,
  BATCH,
  FIXED_VERSION,
  INVARIANT_NAMES,
  MODEL,
  ORDER_SYSTEMS,
  PANELS,
  PARTIES,
  PARTY_BY_ID,
  RECALL_INFO,
  SLICE_PANEL,
  UNITS_TOTAL,
  type Forward,
  type PanelSpec,
  type PartySpec,
} from "./scenario";

// ---------------------------------------------------------------------------------------------
// Inputs

export interface Epoch {
  id: string;
  /** Date.now() when the epoch began, paired with startMockMs. */
  startWall: number;
  startMockMs: number;
  frozen: boolean;
  /** Demo controls held: the signal approval and every live beat wait for the operator. */
  hold: boolean;
}

export type ActionEntry =
  | { type: "operator"; action: OperatorAction }
  | { type: "panel_test" | "panel_update"; serial: string };

export type LoggedAction = ActionEntry & { id: string; ms: number };

export type ActionOutcome =
  | { kind: "operator"; result: OperatorResult }
  | { kind: "panel"; result: Omit<PanelActionResult, "state">; serial: string };

/** A view of a party with the open questions the inspector lists (not in the shared contract). */
export type MockParty = Party & { open_questions: string[] };

// ---------------------------------------------------------------------------------------------
// Constants of the run

const LEASE_S = 8;
const LEASE_H = LEASE_S / REFERENCE_S_PER_HOUR;
const RESTART_H = 3 / REFERENCE_S_PER_HOUR;
const PROPOSE_AT = 7 + 55 / 60;
const NORTH_SWITCH_AT = 3 * 24 + 9 + 20 / 60;
const KILL_TIMES = [14.3, 29.7, 46.2, 61.5, 77.8, 95.1, 112.6, 131.4, 150.2, 171.9, 196.5];
const SLICE_SCRIPT = { release: SLICE_AT + 0.25, failpoint: SLICE_AT + 1, update: SLICE_AT + 2.2, test: SLICE_AT + 2.4,
  close: SLICE_AT + 2.75 };
const REP_REPORT_AT = 8 * 24 + 10;
const QUESTIONS = ["q-01", "q-02", "q-03"];
const WORKERS = ["w1", "w2", "w3", "w4"];
const SABOTEUR = "w5 (saboteur)";

// ---------------------------------------------------------------------------------------------
// Small helpers

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  h ^= h >>> 15;
  h = Math.imul(h, 2246822507);
  h ^= h >>> 13;
  return (h >>> 0) / 4294967296;
}

const dayOf = (h: number) => Math.floor(h / 24);
const hourOfDay = (h: number) => h - dayOf(h) * 24;
const nextMorning = (h: number, at = 9) => (dayOf(h) + 1) * 24 + at;
/** The first business moment (08:00–18:00) at or after h. */
function business(h: number): number {
  const hod = hourOfDay(h);
  if (hod < 8) return dayOf(h) * 24 + 8 + (hod % 1) * 0.5;
  if (hod >= 18) return nextMorning(h, 8.5);
  return h;
}
const pad4 = (n: number) => String(n).padStart(4, "0");
const fmt = (n: number) => n.toLocaleString("en-US");
const isAffected = (v: string | null) => v !== null && AFFECTED.includes(v);

interface Books4 {
  in_stock: number;
  installed: number;
  fixed: number;
  returned: number;
}

interface ClaimOpts {
  received?: number;
  forwards?: Forward[];
  checks?: { model_seen: string; version_seen: string; sounder_test: boolean | null };
  source: "voice" | "sim" | "form" | "rep" | "order_system";
  /** The feed line for the accepted claim, written before the parties it names appear. */
  said?: string;
}

function describe(b: Books4, forwards: Forward[] = []): string {
  const parts: string[] = [];
  if (b.in_stock) parts.push(`${b.in_stock} in stock`);
  if (b.returned) parts.push(`${b.returned} returned`);
  if (b.installed) parts.push(`${b.installed} installed`);
  if (b.fixed) parts.push(`${b.fixed} fixed`);
  for (const f of forwards) {
    const to = PARTY_BY_ID.get(f.to);
    parts.push(`${f.qty} ${to?.type === "owner" ? "installed at" : "passed to"} ${to?.name ?? f.to}`);
  }
  return parts.join(", ") || "nothing on hand";
}

// ---------------------------------------------------------------------------------------------
// Records

interface PartyRec {
  spec: PartySpec;
  shipped_in: number;
  in_stock: number;
  installed: number;
  fixed: number;
  returned: number;
  forwarded: number;
  unresolved: number;
  fixed_by_panel: number;
  checks: { model_seen: string | null; version_seen: string | null; sounder_test: boolean | null } | null;
  state: PartyState;
  claimed: boolean;
  found_via: FoundVia;
  ladder_step: number;
  contacts: number;
  next_action_at: number | null;
  nextSeq: number;
  hold: boolean;
  attempts: number;
  live: { attempt_id: string; state: "intent" | "dialed" | "connected"; real: boolean; sinceSim: number } | null;
  lease: { owner: string; token: number; claimMs: number; deadSim: number | null } | null;
  token: number;
  in_flight: string | null;
  escalation: string | null;
  open_questions: string[];
  visited: boolean;
  visitAt: number | null;
  fixRejects: number;
}

interface EdgeRec {
  from: string;
  to: string;
  sender_qty: number;
  recipient_qty: number | null;
  corroborated_qty: number;
  disputed_qty: number;
}

interface PanelRec {
  spec: PanelSpec;
  sw_version: string;
  fault: boolean;
  party_id: string | null;
  last: { event_id: string; result: "pass" | "fail"; ms: number; sim: number } | null;
  fixed: boolean;
}

interface PanelEventRec {
  event_id: string;
  serial: string;
  test_id: string;
  kind: "monthly_test" | "fault";
  result: "pass" | "fail";
  sw_version: string;
  site_label: string;
  ms: number;
  sim: number;
  effect: string | null;
}

interface SignalRec {
  _id: string;
  events: string[];
  sites: string[];
  status: "proposed" | "approved" | "dismissed";
  approved_by: string | null;
  recall_id: string | null;
}

interface WorkerRec {
  id: string;
  alive: boolean;
  saboteur: boolean;
  party: string | null;
  kind: string | null;
  sinceSim: number;
  restarts: number;
}

type OpState = "intent" | "sent" | "verified" | "needs_reverify" | "failed";
interface OpRec {
  id: string;
  kind: string;
  state: OpState;
  adapter_version: number;
}

interface SystemRec {
  key: "north" | "east";
  liveVersion: 1 | 2;
  adapterVersion: number;
  adapterSource: "seed" | "model" | "episode";
  status: SystemView["status"];
  ops: OpRec[];
}

interface Ev {
  ms: number;
  seq: number;
  fn: () => void;
}

class Queue {
  private heap: Ev[] = [];
  push(ev: Ev): void {
    const h = this.heap;
    h.push(ev);
    let i = h.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (!Queue.less(h[i], h[p])) break;
      [h[i], h[p]] = [h[p], h[i]];
      i = p;
    }
  }
  peek(): Ev | undefined {
    return this.heap[0];
  }
  pop(): Ev | undefined {
    const h = this.heap;
    const top = h[0];
    const last = h.pop();
    if (h.length && last) {
      h[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < h.length && Queue.less(h[l], h[m])) m = l;
        if (r < h.length && Queue.less(h[r], h[m])) m = r;
        if (m === i) break;
        [h[i], h[m]] = [h[m], h[i]];
        i = m;
      }
    }
    return top;
  }
  private static less(a: Ev, b: Ev): boolean {
    return a.ms < b.ms || (a.ms === b.ms && a.seq < b.seq);
  }
}

// ---------------------------------------------------------------------------------------------
// The world

interface ContactPlan {
  call: boolean;
  answers: boolean;
  duration: number;
  kind: string;
  steps: { at: number; fn: () => void }[];
}

export class World {
  readonly clock: MockClock;
  private readonly queue = new Queue();
  private seq = 0;
  private nowMs = 0;

  private recall: { status: "open" | "closed"; openedMs: number; closedMs: number | null } | null = null;
  private readonly parties = new Map<string, PartyRec>();
  private readonly edges: EdgeRec[] = [];
  private readonly panels = new Map<string, PanelRec>();
  private readonly panelEvents: PanelEventRec[] = [];
  private readonly signals: SignalRec[] = [];
  private readonly workers: WorkerRec[] = WORKERS.map((id) => ({
    id, alive: true, saboteur: false, party: null, kind: null, sinceSim: SIM_START, restarts: 0,
  }));
  private readonly feedLines: FeedLine[] = [];
  private readonly refusalList: WallState["refusals"] = [];
  private readonly systems: Record<"north" | "east", SystemRec> = {
    north: { key: "north", liveVersion: 1, adapterVersion: 1, adapterSource: "seed", status: "ok", ops: [] },
    east: { key: "east", liveVersion: 1, adapterVersion: 1, adapterSource: "seed", status: "ok", ops: [] },
  };
  private episode: string | null = null;
  private failpoint: string | null = null;
  private saboteurOn = false;
  private pendingKillUntil: number | null = null;
  private lastInvariant: { sim: number; passed: boolean[] } | null = null;
  private counters = { steps: 0, kills: 0, adoptions: 0, modelCalls: 0, decisions: 0, attempts: 0, real: 0, sim: 0,
    accepted: 0, feed: 0, panelEvents: 0, ops: 0, testIds: 0 };
  readonly outcomes = new Map<string, ActionOutcome>();

  constructor(private readonly epoch: Epoch, log: LoggedAction[]) {
    this.clock = new MockClock(epoch.hold, World.approvalMs(epoch, log));
    for (const spec of PANELS) {
      this.panels.set(spec.serial, { spec, sw_version: spec.version, fault: spec.fault, party_id: null, last: null,
        fixed: false });
    }
    this.script();
    for (const a of log) this.atMs(a.ms, () => this.perform(a));
  }

  /** In hold mode the clock waits at 08:05 for the first approval logged after the signal. */
  static approvalMs(epoch: Epoch, log: LoggedAction[]): number | null {
    if (!epoch.hold) return null;
    const proposed = new MockClock(true, null).msAt(PROPOSE_AT);
    const hit = log.find((a) => a.type === "operator" && a.action.path === "/admin/signals/approve" && a.ms >= proposed);
    return hit ? hit.ms : null;
  }

  run(untilMs: number): void {
    for (let ev = this.queue.peek(); ev && ev.ms <= untilMs; ev = this.queue.peek()) {
      this.queue.pop();
      this.nowMs = ev.ms;
      ev.fn();
    }
    this.nowMs = untilMs;
  }

  // ---- time -----------------------------------------------------------------------------------

  private get sim(): number {
    return this.clock.simAt(this.nowMs);
  }

  private atMs(ms: number, fn: () => void): void {
    if (Number.isFinite(ms)) this.queue.push({ ms, seq: this.seq++, fn });
  }

  private atSim(sim: number, fn: () => void): void {
    this.atMs(this.clock.msAt(sim), fn);
  }

  private later(dh: number, fn: () => void): void {
    this.atMs(this.clock.after(this.nowMs, dh), fn);
  }

  private iso(ms: number): string {
    return new Date(this.epoch.startWall + (ms - this.epoch.startMockMs)).toISOString();
  }

  private ref(sinceSim: number): number {
    return Math.max(0, Math.round((this.sim - sinceSim) * REFERENCE_S_PER_HOUR));
  }

  // ---- feed -----------------------------------------------------------------------------------

  private feed(system: string, text: string, level: Level, kind: string, party: string | null = null): void {
    this.feedLines.push({ id: `f-${pad4(++this.counters.feed)}`, at: this.iso(this.nowMs), sim_label: hourLabel(this.sim),
      system, text, level, party_id: party, kind });
  }

  private refuse(rule: string, message: string, party: string | null, system = "ledger", kind = "claim_rejected"): void {
    this.feed(system, `REJECTED by Atlas: ${message}`, "bad", kind, party);
    this.refusalList.unshift({ at: this.iso(this.nowMs), sim_label: hourLabel(this.sim), rule, message, party_id: party });
  }

  // ---- the script ---------------------------------------------------------------------------

  private script(): void {
    // Day 0: the 24 connected panels report their monthly test; the 7th, 11th and 12th fail.
    PANELS.forEach((p, i) => {
      const at = i < 12 ? SIM_START + 5 / 60 + i / 6 : 8.25 + (i - 12) * 0.25;
      this.atSim(at, () => this.panelTest(p.serial, "monthly_test", null));
    });
    // Faulty panels on an affected version report faults until they are fixed.
    PANELS.filter((p) => p.fault).forEach((p, i) => {
      const times = [30 + 7 * i + 0.4, 76 + 5 * i + 0.7, 128 + 9 * i + 0.2];
      if (p.serial === SLICE_PANEL) times.push(6 * 24 + 15 + 40 / 60);
      for (const t of times) this.atSim(t, () => this.panelTest(p.serial, "fault", null));
    });
    for (const t of KILL_TIMES) this.atSim(t, () => this.randomKill());
    for (let h = 9; h <= RUN_END; h++) this.atSim(h, () => this.invariants(h % 6 === 0));
    if (!this.epoch.hold) {
      this.atSim(APPROVE_AT, () => this.approve("sig-01", "operator-1"));
      this.atSim(NORTH_SWITCH_AT, () => this.switchSystem("north"));
      this.atSim(SLICE_SCRIPT.release, () => this.releaseSlice("harbour-fire", "operator-1"));
      this.atSim(SLICE_SCRIPT.failpoint, () => this.armFailpoint("after_dial_request"));
      this.atSim(SLICE_SCRIPT.update, () => this.panelUpdate(SLICE_PANEL));
      this.atSim(SLICE_SCRIPT.test, () => this.panelTest(SLICE_PANEL, "monthly_test", "page"));
      this.atSim(SLICE_SCRIPT.close, () => this.close("operator-1"));
    }
  }

  // ---- operator and panel-page actions -------------------------------------------------------

  private perform(a: LoggedAction): void {
    if (a.type !== "operator") {
      const result = a.type === "panel_test" ? this.panelTest(a.serial, "monthly_test", "page") : this.panelUpdate(a.serial);
      this.outcomes.set(a.id, { kind: "panel", result, serial: a.serial });
      return;
    }
    this.outcomes.set(a.id, { kind: "operator", result: this.operate(a.action) });
  }

  private operate(act: OperatorAction): OperatorResult {
    switch (act.path) {
      case "/admin/signals/approve":
        return this.approve(act.body.signal_id, act.body.approved_by);
      case "/admin/release":
        return this.releaseSlice(act.body.party_id, "operator");
      case "/admin/failpoint":
        return this.armFailpoint(act.body.name);
      case "/admin/order-system":
        return act.body.version === 2 ? this.switchSystem(act.body.system) : this.armFault(act.body.system, act.body.fault ?? null);
      case "/admin/saboteur":
        return this.saboteur(act.body.on, act.body.seconds ?? 20);
      case "/admin/close":
        return this.close("operator");
      case "/admin/lie":
        return this.lie(act.body.party_id ?? "harbour-fire");
      case "/admin/kill":
        return this.killNow(act.body.worker ?? null);
      case "/admin/supervisor": {
        const message = `Random kills ${act.body.paused ? "paused" : "resumed"}`;
        this.feed("supervisor", message, "info", "supervisor");
        return { ok: true, message };
      }
      case "/admin/clock":
        return { ok: false, message: "The mock run's clock is fixed: reload the screen with ?t=<simulated hour> to jump" };
    }
  }

  // ---- monitoring ---------------------------------------------------------------------------

  private panelTest(serial: string, kind: "monthly_test" | "fault", source: "page" | null): Omit<PanelActionResult, "state"> {
    const panel = this.panels.get(serial);
    if (!panel) return { ok: false, effect: null, refused: { rule: "unknown panel", message: `No connected panel ${serial}` } };
    if (kind === "fault" && (panel.fixed || !panel.fault || !isAffected(panel.sw_version))) return { ok: true, effect: null };
    const result: "pass" | "fail" = isAffected(panel.sw_version) && panel.fault ? "fail" : "pass";
    const ev: PanelEventRec = {
      event_id: `pe-${pad4(++this.counters.panelEvents)}`, serial, test_id: `${source ?? "t"}-${++this.counters.testIds}`,
      kind, result, sw_version: panel.sw_version, site_label: panel.spec.site, ms: this.nowMs, sim: this.sim, effect: null,
    };
    this.panelEvents.push(ev);
    panel.last = { event_id: ev.event_id, result, ms: this.nowMs, sim: this.sim };
    this.counters.steps += 1;
    if (result === "fail") {
      this.feed("panel", `${serial} ${kind === "fault" ? "fault: " : ""}FAIL on ${panel.sw_version} (sounder circuit). ${panel.spec.site}`,
        "warn", "panel_event", panel.party_id);
      this.signalRule(ev);
      return { ok: true, event: this.eventView(ev), effect: null };
    }
    const { moved, note } = this.fixByPanel(panel, ev);
    const party = panel.party_id ? this.parties.get(panel.party_id) : undefined;
    if (moved && party) {
      ev.effect = `${party.spec.name}: 1 panel fixed`;
      this.feed("panel", `${serial} PASS on ${panel.sw_version}. ${party.spec.name}: 1 panel fixed (event ${ev.event_id})`, "good",
        "panel_fixed", party.spec.id);
    } else if (source === "page" || panel.sw_version === FIXED_VERSION) {
      this.feed("panel", `${serial} PASS on ${panel.sw_version}. ${note}`, "info", "panel_event", panel.party_id);
    }
    return { ok: true, event: this.eventView(ev), effect: note };
  }

  /** A PASS on the fixed version after the recall opened moves one panel installed → fixed. */
  private fixByPanel(panel: PanelRec, ev: PanelEventRec): { moved: boolean; note: string } {
    const no = (note: string) => ({ moved: false, note });
    if (panel.sw_version !== FIXED_VERSION) return no(`${panel.sw_version} is not the fixed version: nothing moved`);
    if (!this.recall) return no("No recall is open: nothing moved");
    if (panel.fixed) return no("Already counted fixed: nothing moved");
    const party = panel.party_id ? this.parties.get(panel.party_id) : undefined;
    if (!party) return no(`${panel.spec.serial} is not linked to a party yet: nothing moved`);
    if (party.installed < 1) return no(`${party.spec.name} has no installed panel on the books yet: nothing moved`);
    party.installed -= 1;
    party.fixed += 1;
    party.fixed_by_panel += 1;
    panel.fixed = true;
    this.counters.accepted += 1;
    this.restate(party);
    return { moved: true, note: `${party.spec.name}: 1 panel moved installed → fixed (event ${ev.event_id})` };
  }

  private panelUpdate(serial: string): Omit<PanelActionResult, "state"> {
    const panel = this.panels.get(serial);
    if (!panel) return { ok: false, effect: null, refused: { rule: "unknown panel", message: `No connected panel ${serial}` } };
    if (panel.sw_version === FIXED_VERSION) return { ok: true, effect: `Already on ${FIXED_VERSION}` };
    const old = panel.sw_version;
    panel.sw_version = FIXED_VERSION;
    this.feed("panel", `${serial} software ${old} → ${FIXED_VERSION} (simulated panel page)`, "info", "panel_update", panel.party_id);
    return { ok: true, effect: `Software ${old} → ${FIXED_VERSION}. Run a test to prove the fix` };
  }

  private signalRule(ev: PanelEventRec): void {
    const open = this.signals.find((s) => s.status !== "dismissed");
    if (open) {
      open.events.push(ev.event_id);
      if (!open.sites.includes(ev.site_label)) open.sites.push(ev.site_label);
      if (open.status === "proposed" || ev.kind === "monthly_test") {
        this.feed("signal", `${ev.serial} FAIL added to ${open._id} (${open.events.length} events, ${open.sites.length} sites)`,
          "info", "signal_updated");
      }
      return;
    }
    const fails = this.panelEvents.filter((e) => e.result === "fail" && this.sim - e.sim <= 30 * 24);
    const sites = [...new Set(fails.map((e) => e.site_label))];
    if (fails.length < 3 || sites.length < 3) return;
    this.signals.push({ _id: "sig-01", events: fails.map((e) => e.event_id), sites, status: "proposed", approved_by: null,
      recall_id: null });
    this.feed("signal", `${fails.length} failed tests at ${sites.length} sites, batch ${BATCH}. Signal sig-01 proposed`, "warn",
      "signal_proposed");
    this.feed("planner", "Signal sig-01 waits for a person at the supplier. No agent can approve it", "info", "boss");
  }

  private approve(signalId: string, by: string): OperatorResult {
    const sig = this.signals.find((s) => s._id === signalId);
    if (!sig) return { ok: false, message: `No signal ${signalId} has been proposed` };
    if (sig.status === "approved") return { ok: false, message: `Signal ${sig._id} is already approved by ${sig.approved_by}` };
    sig.status = "approved";
    sig.approved_by = by;
    sig.recall_id = RECALL_INFO._id;
    this.recall = { status: "open", openedMs: this.nowMs, closedMs: null };
    const tier1 = PARTIES.filter((p) => p.tier === 1);
    this.feed("signal", `Approved by ${by}. Recall ${RECALL_INFO._id} opened with ${tier1.length} parties on the shipment records`,
      "good", "signal_approved");
    for (const spec of tier1) this.discover(spec, "shipment");
    this.later(0.25, () => this.distributor("north"));
    this.later(0.9, () => this.distributor("east"));
    return { ok: true, message: `Signal ${sig._id} approved by ${by}. Recall ${RECALL_INFO._id} opened` };
  }

  // ---- parties and the ledger ----------------------------------------------------------------

  private discover(spec: PartySpec, via: FoundVia): PartyRec {
    const known = this.parties.get(spec.id);
    if (known) return known;
    const tier1 = spec.tier === 1;
    const rec: PartyRec = {
      spec, shipped_in: tier1 ? spec.shipped : 0, in_stock: 0, installed: 0, fixed: 0, returned: 0, forwarded: 0,
      unresolved: tier1 ? spec.shipped : 0, fixed_by_panel: 0, checks: null, state: "pending", claimed: false, found_via: via,
      ladder_step: 1, contacts: 0, next_action_at: null, nextSeq: 0, hold: !!spec.slice && tier1, attempts: 0, live: null,
      lease: null, token: 0, in_flight: null, escalation: null,
      open_questions: spec.type === "owner" ? [...QUESTIONS] : [], visited: false, visitAt: null, fixRejects: 0,
    };
    this.parties.set(spec.id, rec);
    this.counters.steps += 1;
    if (spec.type === "owner") {
      const linked = [...this.panels.values()].filter((p) => p.spec.owner === spec.id);
      for (const p of linked) p.party_id = spec.id;
      if (linked.length) {
        this.feed("panel", `${linked.length} connected panel${linked.length > 1 ? "s" : ""} linked to ${spec.name} by site label` +
          ` (${linked.map((p) => p.spec.serial).join(", ")})`, "info", "panel_linked", spec.id);
      }
      if (spec.visitAfterDays) {
        const visit = business(this.sim + spec.visitAfterDays * 24 - hourOfDay(this.sim) + 10 + hash(spec.id) * 2);
        rec.visitAt = visit;
        this.atSim(visit, () => this.installerVisit(rec));
      }
    }
    return rec;
  }

  private edge(from: string, to: string): EdgeRec | undefined {
    return this.edges.find((e) => e.from === from && e.to === to);
  }

  private connectedUnfixed(p: PartyRec): number {
    let n = 0;
    for (const panel of this.panels.values()) if (panel.party_id === p.spec.id && !panel.fixed) n++;
    return n;
  }

  /**
   * Post a claim: the same rules as the validator on `parties`. Returns the refusal text, or null
   * when Atlas accepted it. Nothing is written on a refusal.
   */
  private post(p: PartyRec, b: Books4, o: ClaimOpts): string | null {
    const inEdge = p.spec.from ? this.edge(p.spec.from, p.spec.id) : undefined;
    let shipped = p.shipped_in;
    if (o.received !== undefined && inEdge) shipped = Math.min(inEdge.sender_qty, o.received);
    const fresh = (o.forwards ?? []).filter((f) => !this.edge(p.spec.id, f.to));
    const forwarded = this.edges.filter((e) => e.from === p.spec.id).reduce((s, e) => s + e.sender_qty, 0) +
      fresh.reduce((s, f) => s + f.qty, 0);
    const reported = b.in_stock + b.installed + b.fixed + b.returned + forwarded;
    const checks = o.checks ?? p.checks;
    const version = checks?.version_seen ?? null;
    const cu = this.connectedUnfixed(p);
    let refusal: { rule: string; message: string } | null = null;
    if (reported > shipped) {
      refusal = { rule: "conservation", message: `Document failed validation on parties. Reported ${reported}, shipped_in ${shipped}` };
    } else if (b.fixed > p.fixed_by_panel && (version === null || isAffected(version) || checks?.sounder_test !== true)) {
      refusal = { rule: "fixed needs evidence", message: version !== null && isAffected(version)
        ? `Document failed validation on parties. fixed ${b.fixed} with version_seen ${version} (affected)`
        : `Document failed validation on parties. fixed ${b.fixed} without a sounder test` };
    } else if (b.installed < cu) {
      refusal = { rule: "panel outranks the phone",
        message: `Document failed validation on parties. installed ${b.installed} below connected_unfixed ${cu}` };
    }
    if (refusal) {
      this.refuse(refusal.rule, refusal.message, p.spec.id);
      return refusal.message;
    }
    if (inEdge && o.received !== undefined) {
      inEdge.recipient_qty = o.received;
      inEdge.corroborated_qty = Math.min(inEdge.sender_qty, o.received);
      inEdge.disputed_qty = Math.abs(inEdge.sender_qty - o.received);
      p.shipped_in = inEdge.corroborated_qty;
    }
    for (const f of fresh) {
      this.edges.push({ from: p.spec.id, to: f.to, sender_qty: f.qty, recipient_qty: null, corroborated_qty: 0, disputed_qty: 0 });
    }
    p.in_stock = b.in_stock;
    p.installed = b.installed;
    p.fixed = b.fixed;
    p.returned = b.returned;
    p.forwarded = forwarded;
    p.unresolved = p.shipped_in - reported;
    if (o.checks) p.checks = { ...o.checks };
    if (p.spec.type === "owner") p.open_questions = p.installed > 0 ? ["q-02", "q-03"] : [];
    p.claimed = true;
    if (o.source !== "order_system") this.counters.accepted += 1;
    this.counters.steps += 1;
    if (o.said) this.feed(o.source === "form" ? "form" : "phone", o.said, "good", o.source === "form" ? "form_claim" : "claim_accepted",
      p.spec.id);
    for (const f of fresh) {
      const spec = PARTY_BY_ID.get(f.to);
      if (!spec) continue;
      const isNew = !this.parties.has(spec.id);
      if (isNew && o.source !== "order_system") {
        this.feed("ledger", `${p.spec.name} names "${spec.name}": new party, tier ${spec.tier}`, "info", "store_discovered", spec.id);
      }
      const rec = this.discover(spec, o.source === "order_system" ? "order_system" : "claim");
      if (isNew && o.source !== "order_system") this.contactAt(rec, spec.slice ? this.sim + 0.8 : business(this.sim + 1.5));
    }
    this.restate(p);
    if (p.spec.from) {
      const sender = this.parties.get(p.spec.from);
      if (sender) this.restate(sender);
    }
    for (const f of fresh) {
      const r = this.parties.get(f.to);
      if (r) this.restate(r);
    }
    if (inEdge && o.received !== undefined) {
      const sender = this.parties.get(inEdge.from)?.spec.name ?? inEdge.from;
      if (inEdge.disputed_qty > 0) {
        this.feed("ledger", `Transfer ${sender} → ${p.spec.name} disputed: ${inEdge.disputed_qty} panels (sender ${inEdge.sender_qty}, ` +
          `recipient ${inEdge.recipient_qty})`, "warn", "edge_disputed", p.spec.id);
      }
    }
    return null;
  }

  /** The party ladder: reached on an accepted claim, claimed once nothing at the party is open. */
  private restate(p: PartyRec): void {
    if (!p.claimed) return;
    const edgesOk = this.edges.filter((e) => e.from === p.spec.id || e.to === p.spec.id)
      .every((e) => e.recipient_qty !== null && e.disputed_qty === 0);
    if (p.unresolved === 0 && p.installed === 0 && edgesOk) {
      if (p.state !== "claimed" && p.state !== "reconciled") {
        p.state = "claimed";
        this.later(3, () => {
          if (p.state === "claimed") p.state = "reconciled";
        });
      }
    } else {
      p.state = "reached";
    }
  }

  // ---- contacts ------------------------------------------------------------------------------

  private contactAt(p: PartyRec, sim: number): void {
    const seq = ++p.nextSeq;
    p.next_action_at = sim;
    this.atSim(Math.max(sim, this.sim), () => {
      if (p.nextSeq === seq) this.due(p);
    });
  }

  private freeWorker(): WorkerRec | undefined {
    const free = this.workers.filter((w) => w.alive && !w.saboteur && !w.party);
    return free.sort((a, b) => a.sinceSim - b.sinceSim)[0];
  }

  private due(p: PartyRec): void {
    if (p.hold || !this.recall) return;
    const w = this.freeWorker();
    if (!w || p.lease) {
      this.later(0.2, () => this.due(p));
      return;
    }
    const plan = this.plan(p);
    if (!plan) {
      p.next_action_at = null;
      return;
    }
    this.start(p, w, plan);
  }

  private start(p: PartyRec, w: WorkerRec, plan: ContactPlan): void {
    p.token += 1;
    p.lease = { owner: w.id, token: p.token, claimMs: this.nowMs, deadSim: null };
    p.next_action_at = null;
    w.party = p.spec.id;
    w.kind = plan.kind;
    w.sinceSim = this.sim;
    this.counters.steps += 2;
    this.counters.decisions += 1;
    this.counters.modelCalls += 1;
    if (!p.claimed) p.state = "contacting";
    const real = !!p.spec.voice;
    if (plan.call) {
      const id = `at-${pad4(++this.counters.attempts)}`;
      p.attempts += 1;
      if (real) this.counters.real += 1;
      else this.counters.sim += 1;
      p.in_flight = id;
      p.live = { attempt_id: id, state: "dialed", real, sinceSim: this.sim };
      this.feed("phone", `${p.spec.name}: calling ${real ? `${p.spec.phone} (real call)` : "(simulated)"}. Attempt ${id} sent`,
        "info", "call_placed", p.spec.id);
      if (plan.answers) {
        this.later(Math.min(0.04, plan.duration / 4), () => {
          if (p.live?.attempt_id === id) p.live.state = "connected";
        });
      }
      if (this.failpoint) {
        const name = this.failpoint;
        this.failpoint = null;
        this.later(0.004, () => this.failpointFires(w, p, name));
      } else if (this.pendingKillUntil !== null && this.sim <= this.pendingKillUntil) {
        this.pendingKillUntil = null;
        this.later(0.05, () => this.kill(w, "random"));
      }
    }
    for (const s of plan.steps) this.later(s.at, s.fn);
    this.later(plan.duration, () => this.finish(p));
  }

  private finish(p: PartyRec): void {
    const lease = p.lease;
    p.live = null;
    p.contacts += 1;
    p.ladder_step = Math.min(5, Math.max(p.ladder_step, p.contacts + 1));
    this.counters.steps += 2;
    if (lease) this.releaseLease(p);
  }

  /** Release and result in one step: the lease, in_flight and the holder's slot. A lease whose
   * holder died stays until another worker adopts the party. */
  private releaseLease(p: PartyRec): void {
    if (!p.lease || p.lease.deadSim !== null) return;
    const holder = this.workers.find((w) => w.id === p.lease?.owner);
    p.lease = null;
    p.in_flight = null;
    if (holder && holder.party === p.spec.id) {
      holder.party = null;
      holder.kind = null;
      holder.sinceSim = this.sim;
    }
  }

  private releaseSlice(partyId: string, by: string): OperatorResult {
    if (!this.recall) return { ok: false, message: "No recall is open yet: approve the signal first" };
    const p = this.parties.get(partyId);
    if (!p) return { ok: false, message: `Unknown party ${partyId}` };
    if (!p.hold) return { ok: true, message: `${p.spec.name} is not held: nothing to release` };
    const held = [...this.parties.values()].filter((x) => x.hold && x.spec.slice);
    for (const x of held) x.hold = false;
    this.feed("phone", `Released the live slice by ${by}: ${held.map((x) => x.spec.name).join(", ")} due now`, "info",
      "sim_released", p.spec.id);
    held.forEach((x, i) => this.contactAt(x, this.sim + 0.05 + i * 0.05));
    return { ok: true, message: `Released ${held.map((x) => x.spec.name).join(", ")}: due now at ${hourLabel(this.sim)}` };
  }

  // ---- what each party does on a contact ----------------------------------------------------

  private plan(p: PartyRec): ContactPlan | null {
    const s = p.spec;
    const n = p.contacts;
    const claim = (at: number, fn: () => void, duration = at + 0.06): ContactPlan =>
      ({ call: true, answers: true, duration, kind: "contact_party", steps: [{ at, fn }] });
    const noAnswer = (next: number): ContactPlan => ({
      call: true, answers: false, duration: 0.1, kind: "contact_party", steps: [{ at: 0.09, fn: () => {
        this.feed("phone", `${s.name}: no answer, attempt ${p.attempts}. Next action ${hourLabel(next)}`, "info", "attempt_closed", s.id);
        this.contactAt(p, next);
      } }],
    });
    const formLink = (submits: boolean): ContactPlan => ({
      call: false, answers: false, duration: 0.04, kind: "send_form_link", steps: [{ at: 0.03, fn: () => {
        this.feed("form", `${s.name}: form link sent (ladder step 3)`, "info", "form_link_sent", s.id);
        if (submits) {
          this.later(5, () => {
            if (this.post(p, this.books(s), { source: "form", received: s.shipped }) === null) {
              this.feed("form", `${s.name}: form submitted, claim accepted: ${describe(this.books(s))}. Form link verified`, "good",
                "form_claim", s.id);
            }
          });
        } else {
          this.contactAt(p, this.sim + 24);
        }
      } }],
    });
    const accept = (books: Books4, o: ClaimOpts, text?: string) => {
      this.post(p, books, { ...o, said: text ?? `${s.name}: claim accepted: ${describe(books, o.forwards)}. Attempt ${p.in_flight} verified` });
    };

    if (s.type === "owner" && p.claimed) return this.fixCheck(p);
    switch (s.profile) {
      case "slice":
        return this.slicePlan(p);
      case "straight":
      case "forwarder":
        if (s.type === "owner") return this.ownerFirstClaim(p);
        return claim(0.3, () => accept(this.books(s), { source: "sim", received: s.tier > 1 ? s.shipped : undefined,
          forwards: s.forwards }));
      case "short_receiver":
        return this.ownerFirstClaim(p);
      case "over_reporter": {
        const lie = this.books(s);
        lie.in_stock += (s.firstReport ?? s.shipped) - s.shipped;
        return {
          call: true, answers: true, duration: 0.4, kind: "contact_party", steps: [
            { at: 0.2, fn: () => {
              if (this.post(p, lie, { source: "sim", received: s.shipped }) !== null) {
                this.feed("phone", `${s.name}: agent challenges: reported ${s.firstReport}, shipped_in ${s.shipped}`, "warn",
                  "tool_event", s.id);
                this.feed("memory", `Episode saved for ${s.name}: over_reported. Ask for the delivery note number first`, "info",
                  "memory_note", s.id);
              }
            } },
            { at: 0.33, fn: () => accept(this.books(s), { source: "sim", received: s.shipped },
              `${s.name}: corrected, claim accepted: ${describe(this.books(s))}. Attempt ${p.in_flight} verified`) },
          ],
        };
      }
      case "reviser":
        return claim(0.3, () => {
          accept(this.books(s), { source: "sim", received: s.shipped });
          this.later(48, () => {
            const revised = { ...this.books(s), in_stock: s.revised?.in_stock ?? 0, returned: s.revised?.returned ?? 0 };
            if (this.post(p, revised, { source: "sim" }) === null) {
              this.feed("phone", `${s.name} revised its accepted claim: ${describe(revised)} (claim version 2). Accepted`, "good",
                "claim_accepted", s.id);
            }
          });
        });
      case "callback":
        if (n === 0) {
          return { call: true, answers: true, duration: 0.12, kind: "contact_party", steps: [{ at: 0.1, fn: () => {
            const next = nextMorning(this.sim);
            this.feed("phone", `${s.name}: asked to be called back tomorrow at 9. Next action ${hourLabel(next)}`, "info",
              "callback_requested", s.id);
            this.contactAt(p, next);
          } }] };
        }
        return claim(0.3, () => accept(this.books(s), { source: "sim", received: s.shipped }));
      case "no_answer_call":
        if (n === 0) return noAnswer(business(this.sim + 4));
        if (n === 1) return noAnswer(nextMorning(this.sim));
        if (n === 2) return formLink(false);
        return claim(0.3, () => accept(this.books(s), { source: "sim", received: s.shipped }));
      case "no_answer_form":
        if (n === 0) return noAnswer(business(this.sim + 4));
        if (n === 1) return noAnswer(nextMorning(this.sim));
        return formLink(true);
      case "no_answer_hours":
        if (n === 0) return noAnswer(nextMorning(this.sim));
        if (n === 1) {
          const plan = noAnswer(dayOf(this.sim) * 24 + 14);
          plan.steps.push({ at: 0.095, fn: () => this.feed("memory",
            `Episode saved for ${s.name}: answers after 14:00 on weekdays. Next call at that hour`, "info", "memory_note", s.id) });
          return plan;
        }
        return claim(0.3, () => accept(this.books(s), { source: "sim", received: s.shipped }));
      case "unreachable":
        if (n === 0) return noAnswer(business(this.sim + 4));
        if (n === 1) return noAnswer(nextMorning(this.sim));
        if (n === 2) return formLink(false);
        return { call: true, answers: false, duration: 0.1, kind: "escalate", steps: [{ at: 0.09, fn: () => {
          const rep = this.parties.get(s.from ?? "")?.spec.name ?? "the distributor";
          p.escalation = `Escalated to ${rep}'s rep (ladder step 5)`;
          p.ladder_step = 5;
          this.feed("planner", `${s.name}: no answer after 4 contacts. Escalated to ${rep}'s rep (ladder step 5)`, "warn",
            "needs_human", s.id);
          this.atSim(Math.max(REP_REPORT_AT, this.sim + 1), () => {
            if (this.post(p, this.books(s), { source: "rep", received: s.shipped }) === null) {
              p.escalation = null;
              this.feed("phone", `${rep}'s rep reports for ${s.name}: ${describe(this.books(s))}. Claim accepted`, "good",
                "report_received", s.id);
            }
          });
        } }] };
      case "order_system":
        return null;
    }
  }

  private books(s: PartySpec): Books4 {
    return { in_stock: s.books.in_stock, installed: s.books.installed ?? 0, fixed: 0, returned: s.books.returned };
  }

  private ownerChecks(s: PartySpec, version = s.version ?? "2.4") {
    return { model_seen: MODEL, version_seen: version, sounder_test: true };
  }

  private ownerFirstClaim(p: PartyRec): ContactPlan {
    const s = p.spec;
    const received = s.firstReport ?? s.shipped;
    return { call: true, answers: true, duration: 0.36, kind: "contact_party", steps: [{ at: 0.3, fn: () => {
      const books = { in_stock: 0, installed: received, fixed: 0, returned: 0 };
      const said = `${s.name}: ${received} panels received, all installed. Model ${MODEL}, display shows ${s.version}. Claim accepted`;
      if (this.post(p, books, { source: "sim", received, checks: this.ownerChecks(s), said }) !== null) return;
      if (s.profile === "short_receiver") this.scheduleDisputeFollowUp(p);
      if (p.visitAt !== null && this.sim + 48 < p.visitAt) this.contactAt(p, this.sim + 48);
    } }] };
  }

  private scheduleDisputeFollowUp(p: PartyRec): void {
    const s = p.spec;
    const sender = this.parties.get(s.from ?? "");
    if (!sender) return;
    const at = business(this.sim + 20);
    this.feed("planner", `Dispute ${sender.spec.name} → ${s.name}: follow-up calls to both parties at ${hourLabel(at)}`, "info",
      "followup_opened", s.id);
    this.atSim(at, () => {
      const edge = this.edge(sender.spec.id, s.id);
      if (!edge || edge.disputed_qty === 0) return;
      if (s.id === "liberty-village-lofts") {
        const books = { in_stock: 0, installed: p.installed + edge.disputed_qty, fixed: p.fixed, returned: 0 };
        if (this.post(p, books, { source: "sim", received: edge.sender_qty }) === null) {
          this.feed("phone", `${s.name}: follow-up: found ${edge.disputed_qty} more installed at the West building. Received ` +
            `${edge.sender_qty}. Transfer corroborated`, "good", "edge_corroborated", s.id);
        }
      } else {
        const gap = edge.disputed_qty;
        edge.sender_qty -= gap;
        edge.corroborated_qty = edge.sender_qty;
        edge.disputed_qty = 0;
        const books = { in_stock: sender.in_stock + gap, installed: sender.installed, fixed: sender.fixed, returned: sender.returned };
        if (this.post(sender, books, { source: "sim" }) === null) {
          this.feed("phone", `${sender.spec.name}: follow-up: sent ${edge.sender_qty} to ${s.name}, ${gap} still in stock. ` +
            `Transfer corroborated`, "good", "edge_corroborated", sender.spec.id);
          this.restate(p);
        }
      }
    });
  }

  private installerVisit(p: PartyRec): void {
    if (!p.claimed) {
      this.later(6, () => this.installerVisit(p));
      return;
    }
    p.visited = true;
    const installer = this.parties.get(p.spec.from ?? "")?.spec.name ?? "The installer";
    const linked = [...this.panels.values()].filter((x) => x.party_id === p.spec.id);
    this.feed("phone", `${installer} reports its visit to ${p.spec.name}: software ${FIXED_VERSION} on ${p.installed} panels`,
      "info", "sim_reply", p.spec.id);
    linked.forEach((panel, i) => {
      panel.sw_version = FIXED_VERSION;
      this.later(0.5 * (i + 1), () => this.panelTest(panel.spec.serial, "monthly_test", null));
    });
    const early = p.spec.id === "bathurst-commons";
    this.contactAt(p, this.sim + (early ? 0.7 : 0.5 * (linked.length + 1) + 0.3));
  }

  private fixCheck(p: PartyRec): ContactPlan | null {
    const s = p.spec;
    if (p.installed === 0) return null;
    const steps: ContactPlan["steps"] = [];
    if (!p.visited) {
      steps.push({ at: 0.25, fn: () => {
        if (s.id === "ossington-studios" && p.fixRejects === 0) {
          p.fixRejects += 1;
          this.post(p, { in_stock: 0, installed: 0, fixed: p.installed + p.fixed, returned: 0 },
            { source: "sim", checks: this.ownerChecks(s) });
          this.feed("phone", `${s.name}: says all fixed, but the display shows ${s.version}. Installer visit pending`, "warn",
            "tool_event", s.id);
        } else {
          this.feed("phone", `${s.name}: fix check: display shows ${s.version}, installer visit pending. ` +
            `Next fix check after the visit, ${hourLabel(p.visitAt ?? this.sim + 48)}`, "info", "answer_recorded", s.id);
        }
        p.next_action_at = p.visitAt;
      } });
      return { call: true, answers: true, duration: 0.3, kind: "fix_check", steps };
    }
    steps.push({ at: 0.25, fn: () => {
      const total = p.installed + p.fixed;
      const books = { in_stock: 0, installed: 0, fixed: total, returned: 0 };
      const refused = this.post(p, books, { source: "sim", checks: this.ownerChecks(s, FIXED_VERSION) });
      if (refused === null) {
        p.open_questions = [];
        this.feed("phone", `${s.name}: fix check: version ${FIXED_VERSION} on the display, sounder rang. ${total} panels fixed ` +
          `(${p.fixed_by_panel} by their own test). Claim accepted`, "good", "claim_accepted", s.id);
      } else {
        p.fixRejects += 1;
        this.feed("phone", `${s.name}: a connected panel has not passed its own test yet. The panel outranks the phone`, "warn",
          "tool_event", s.id);
        this.contactAt(p, this.sim + 1.0);
      }
    } });
    return { call: true, answers: true, duration: 0.3, kind: "fix_check", steps };
  }

  /** The live slice: Harbour Fire lies, Queen St Alarm names Corner Properties, Corner Properties answers 25. */
  private slicePlan(p: PartyRec): ContactPlan | null {
    const s = p.spec;
    if (p.claimed) return null;
    if (s.id === "harbour-fire") {
      const lie = { in_stock: (s.firstReport ?? 60) - s.books.returned, installed: 0, fixed: 0, returned: s.books.returned };
      return { call: true, answers: true, duration: 1.0, kind: "contact_party", steps: [
        { at: 0.5, fn: () => {
          if (this.post(p, lie, { source: "voice" }) !== null) {
            this.feed("phone", `${s.name}: agent challenges with the database's numbers: reported ${s.firstReport}, shipped_in ` +
              `${s.shipped}`, "warn", "tool_event", s.id);
          }
        } },
        { at: 0.9, fn: () => {
          if (this.post(p, this.books(s), { source: "voice" }) === null) {
            this.feed("phone", `${s.name}: corrected, claim accepted: ${describe(this.books(s))}. Attempt ${p.in_flight} verified`,
              "good", "claim_accepted", s.id);
          }
        } },
      ] };
    }
    if (s.id === "corner-properties") {
      return { call: true, answers: true, duration: 0.3, kind: "contact_party", steps: [{ at: 0.2, fn: () => {
        const received = s.firstReport ?? 25;
        const books = { in_stock: 0, installed: received, fixed: 0, returned: 0 };
        const said = `${s.name}: "We received ${received}, all installed, the display says ${s.version}." Claim accepted ` +
          `(attempt ${p.in_flight})`;
        if (this.post(p, books, { source: "voice", received, checks: this.ownerChecks(s), said }) === null) {
          this.feed("phone", `${s.name}: fix check due ${hourLabel(this.sim + 48)}`, "info", "attempt_closed", s.id);
          this.later(0.8, () => {
            const at = Math.min(this.sim + 1.5, RUN_END);
            this.feed("planner", `Dispute Queen St Alarm → ${s.name} (15 panels): follow-up calls to both parties at ` +
              `${hourLabel(at)}`, "info", "followup_opened", s.id);
            this.atSim(at, () => this.feed("phone", `Queen St Alarm: follow-up: still reports 40 installed at ${s.name} ` +
              `(delivery note DN-48214). The dispute stands`, "warn", "tool_event", "queen-st-alarm"));
          });
        }
      } }] };
    }
    return { call: true, answers: true, duration: 0.4, kind: "contact_party", steps: [{ at: 0.35, fn: () => {
      this.post(p, this.books(s), { source: "sim", forwards: s.forwards,
        said: `${s.name}: claim accepted: ${describe(this.books(s), s.forwards)}. Attempt ${p.in_flight} verified` });
    } }] };
  }

  // ---- distributors and their order systems ------------------------------------------------

  private op(sys: SystemRec, kind: string, state: OpState): OpRec {
    const rec = { id: `op-${pad4(++this.counters.ops)}`, kind, state, adapter_version: sys.adapterVersion };
    sys.ops.push(rec);
    this.counters.steps += 1;
    return rec;
  }

  private distributor(key: "north" | "east"): void {
    const sys = this.systems[key];
    const info = ORDER_SYSTEMS[key];
    const p = this.parties.get(info.party);
    const w = this.freeWorker();
    if (!p || !w) {
      this.later(0.2, () => this.distributor(key));
      return;
    }
    p.token += 1;
    p.lease = { owner: w.id, token: p.token, claimMs: this.nowMs, deadSim: null };
    p.state = "contacting";
    w.party = p.spec.id;
    w.kind = "place_hold";
    w.sinceSim = this.sim;
    const holdId = key === "north" ? "H-7781" : "H-5520";
    const hold = this.op(sys, "place_hold", "sent");
    p.in_flight = hold.id;
    this.feed(info.label, `Hold ${holdId} on batch ${BATCH} sent (${hold.id}, key ${RECALL_INFO._id}:${p.spec.id}:place_hold)`, "info",
      "operation_sent", p.spec.id);
    this.later(0.15, () => {
      hold.state = "verified";
      this.feed(info.label, `Hold ${holdId} verified by read-back (${hold.id}): status on_hold`, "good", "operation_verified",
        p.spec.id);
      const read = this.op(sys, "read_accounts", "sent");
      p.in_flight = read.id;
      this.later(0.15, () => {
        read.state = "verified";
        const spec = p.spec;
        if (this.post(p, this.books(spec), { source: "order_system", forwards: spec.forwards }) === null) {
          this.feed(info.label, `${spec.forwards?.length ?? 0} accounts for batch ${BATCH} read back (${read.id}): ` +
            `${spec.forwards?.length ?? 0} new parties, tier 2. ${spec.books.in_stock} panels held in the warehouse`, "good",
            "operation_verified", spec.id);
          const ranked = (spec.forwards ?? []).slice().sort((a, b) => b.qty - a.qty);
          const top = PARTY_BY_ID.get(ranked[0]?.to ?? "");
          if (top) {
            this.feed("planner", `Ranked ${ranked.length} new parties by open panels: ${top.name} first (${ranked[0].qty})`, "info", "boss");
          }
          for (const f of spec.forwards ?? []) {
            const rec = this.parties.get(f.to);
            if (rec?.spec.firstContact !== undefined) this.contactAt(rec, rec.spec.firstContact);
          }
        }
        this.releaseLease(p);
        this.stockChecks(key, this.sim + 6 + (key === "north" ? 0 : 1));
      });
    });
  }

  /** Read-backs of stock at the accounts, every 8 simulated hours, until the system changes. */
  private stockChecks(key: "north" | "east", at: number): void {
    this.atSim(at, () => {
      const sys = this.systems[key];
      if (sys.liveVersion !== 1 || at > 4 * 24) return;
      const batch = [this.op(sys, "read_stock", "sent"), this.op(sys, "read_stock", "sent")];
      this.later(0.4, () => {
        for (const o of batch) if (o.state === "sent") o.state = "verified";
      });
      this.stockChecks(key, at + 8);
    });
  }

  private switchSystem(key: "north" | "east"): OperatorResult {
    const sys = this.systems[key];
    const label = ORDER_SYSTEMS[key].label;
    if (sys.liveVersion === 2) return { ok: false, message: `${label} is already on v2` };
    sys.liveVersion = 2;
    this.feed(label, `Order system switched to v2: batch → batch_code, qty_in_stock → inventory.available, holds need a confirm call`,
      "warn", "portal_switched");
    this.later(0.08, () => {
      const n = key === "north" ? 4 : 3;
      const sent = Array.from({ length: n }, () => this.op(sys, "read_stock", "sent"));
      this.feed(label, `${n} stock read-backs sent (${sent[0].id}…${sent[n - 1].id}) on adapter v${sys.adapterVersion}`, "info",
        "operation_sent");
      this.later(0.08, () => this.contractFailed(sys));
    });
    if (key === "north") this.later(0.2, () => this.switchSystem("east"));
    return { ok: true, message: `${label} switched to v2. Watch for the contract failure and the repair` };
  }

  private contractFailed(sys: SystemRec): void {
    const label = ORDER_SYSTEMS[sys.key].label;
    const flagged = sys.ops.filter((o) => o.state === "intent" || o.state === "sent");
    for (const o of flagged) o.state = "needs_reverify";
    const untouched = sys.ops.filter((o) => o.state === "verified").length;
    sys.status = "contract_failed";
    this.feed(label, `Contract failed: field qty_in_stock missing. ${flagged.length} operations flagged, ${untouched} untouched`,
      "bad", "contract_failed");
    const canary = ORDER_SYSTEMS[sys.key].canary;
    const activate = (source: "model" | "episode") => {
      sys.adapterVersion = 2;
      sys.adapterSource = source;
      sys.status = "ok";
      this.feed(label, `Adapter v2 active (${source === "model" ? "repaired by model" : `from memory, episode ${this.episode}`}): ` +
        `canary ${canary.account} ${canary.value} = ${canary.value}${source === "episode" ? ", 0 model calls" : ""}. ` +
        `Retrying ${flagged.length} operations`, "good", "adapter_activated");
      this.later(0.06, () => {
        for (const o of flagged) {
          o.state = "verified";
          o.adapter_version = 2;
        }
        this.feed(label, `${flagged.length} operations verified on adapter v2. Verified operations kept their adapter version`,
          "good", "operation_verified");
      });
    };
    if (this.episode) {
      const episode = this.episode;
      this.later(0.03, () => {
        sys.status = "repairing";
        this.feed("memory", `Episode ${episode} retrieved by Vector Search on the failure text`, "info", "memory_hit");
        this.later(0.03, () => activate("episode"));
      });
      return;
    }
    this.feed("planner", `Work on ${label} paused. Adapter repair requested`, "info", "boss");
    this.later(0.03, () => {
      sys.status = "repairing";
      this.counters.modelCalls += 1;
      this.feed(label, `Model proposed a mapping: qty_shipped → inventory.available`, "info", "adapter_proposed");
      this.later(0.03, () => {
        this.feed(label, `Canary ${canary.account} qty_shipped: ledger ${canary.value}, proposed adapter reads ${canary.wrong}. ` +
          `Mapping discarded, not activated`, "warn", "adapter_rejected");
        this.counters.modelCalls += 1;
        this.later(0.04, () => {
          this.feed(label, `Model proposed a mapping: batch → batch_code, qty_shipped → shipped, qty_in_stock → inventory.available`,
            "info", "adapter_proposed");
          this.later(0.02, () => {
            activate("model");
            this.episode = "e-0412";
            this.later(0.08, () => this.feed("memory", `Episode e-0412 saved: ${label} v1 → v2 mapping and its failure text`, "info",
              "memory_note"));
          });
        });
      });
    });
  }

  private armFault(key: "north" | "east", fault: string | null): OperatorResult {
    const label = ORDER_SYSTEMS[key].label;
    const text = fault ? `Fault ${fault} armed on ${label}` : `Faults cleared on ${label}`;
    this.feed(label, text, "info", "portal_fault");
    return { ok: true, message: text };
  }

  // ---- kills, adoptions, failpoints, the saboteur -------------------------------------------

  private kill(w: WorkerRec, why: "random" | "operator" | "failpoint"): void {
    if (!w.alive) return;
    w.alive = false;
    this.counters.kills += 1;
    this.feed("supervisor", `Worker ${w.id} killed (${this.counters.kills})${why === "operator" ? " by the operator" : ""}` +
      `${w.party ? "" : ": it held no lease"}`, why === "random" ? "warn" : "bad", "supervisor");
    const held = w.party ? this.parties.get(w.party) : undefined;
    if (held?.lease && held.lease.owner === w.id) {
      held.lease.deadSim = this.sim;
      this.later(LEASE_H + 0.03, () => this.adopt(held, 0));
    }
    this.later(RESTART_H, () => {
      w.alive = true;
      w.restarts += 1;
      w.party = null;
      w.kind = null;
      w.sinceSim = this.sim;
      this.feed("supervisor", `Worker ${w.id} restarted after 3 s`, "info", "worker_started");
    });
  }

  private adopt(p: PartyRec, tries: number): void {
    const lease = p.lease;
    if (!lease || lease.deadSim === null) return;
    const dead = this.workers.find((w) => w.id === lease.owner);
    const w = this.workers.find((x) => x.alive && !x.saboteur && !x.party && x.id !== lease.owner) ??
      (tries > 2 ? this.freeWorker() : undefined);
    if (!w) {
      this.later(0.05, () => this.adopt(p, tries + 1));
      return;
    }
    if (dead && dead.party === p.spec.id) {
      dead.party = null;
      dead.kind = null;
    }
    p.token += 1;
    p.lease = { owner: w.id, token: p.token, claimMs: this.nowMs, deadSim: null };
    w.party = p.spec.id;
    w.kind = "resolve_in_doubt";
    w.sinceSim = this.sim;
    this.counters.adoptions += 1;
    this.counters.steps += 2;
    const doubt = p.in_flight ?? "the attempt";
    const done = p.live === null;
    this.feed(`worker ${w.id}`, `Adopted ${p.spec.name}: 1 in doubt (${doubt}). tool_events show the call, 0 redials. ` +
      `4 documents read, 0 steps replayed`, "good", "attempt_adopted", p.spec.id);
    if (done) this.later(0.02, () => this.releaseLease(p));
  }

  /** The supervisor's random kill lands on the next worker to dial, or on an idle one if none
   * dials within 10 simulated hours. */
  private randomKill(): void {
    const busy = this.workers.find((w) => w.alive && !w.saboteur && w.party && this.parties.get(w.party)?.live);
    if (busy) {
      this.kill(busy, "random");
      return;
    }
    const until = this.sim + 10;
    this.pendingKillUntil = until;
    this.atSim(until, () => {
      if (this.pendingKillUntil !== until) return;
      this.pendingKillUntil = null;
      const idle = this.workers.find((w) => w.alive && !w.saboteur);
      if (idle) this.kill(idle, "random");
    });
  }

  private killNow(target: string | null): OperatorResult {
    const alive = this.workers.filter((w) => w.alive && !w.saboteur);
    const w = target ? this.workers.find((x) => x.id === target) : alive.find((x) => x.party) ?? alive[0];
    if (!w) return { ok: false, message: target ? `Unknown worker ${target}` : "No live worker to kill" };
    if (!w.alive) return { ok: false, message: `Worker ${w.id} is already down` };
    const held = w.party ? this.parties.get(w.party)?.spec.name : null;
    this.kill(w, "operator");
    return { ok: true, message: `Killed ${w.id} (SIGKILL)${held ? ` holding ${held}: its lease drains, then another worker adopts` :
      ": it held no lease"}. The supervisor restarts it in 3 s` };
  }

  private armFailpoint(name: string | null): OperatorResult {
    this.failpoint = name;
    if (!name) {
      this.feed("worker", "All failpoints disarmed", "info", "failpoint_armed");
      return { ok: true, message: "All failpoints disarmed" };
    }
    this.feed("worker", `Failpoint ${name} armed: the next call's worker dies after the dial request, before saving it`, "warn",
      "failpoint_armed");
    return { ok: true, message: `Failpoint ${name} armed. The next worker to dial dies before it saves the call` };
  }

  private failpointFires(w: WorkerRec, p: PartyRec, name: string): void {
    this.feed(`worker ${w.id}`, `Failpoint ${name} fired on ${p.spec.name}: the call was requested, the worker died before ` +
      `saving it`, "bad", "failpoint_fired", p.spec.id);
    this.kill(w, "failpoint");
  }

  private saboteur(on: boolean, seconds: number): OperatorResult {
    if (!on) {
      this.saboteurOff();
      return { ok: true, message: "Saboteur off" };
    }
    if (this.saboteurOn) return { ok: true, message: "The saboteur is already running" };
    this.saboteurOn = true;
    const w: WorkerRec = { id: SABOTEUR, alive: true, saboteur: true, party: null, kind: "break_rules", sinceSim: this.sim, restarts: 0 };
    this.workers.push(w);
    this.feed("supervisor", `Saboteur ${SABOTEUR} started for ${seconds} s`, "warn", "saboteur");
    const target = [...this.parties.values()].find((p) => p.lease && p.lease.owner !== SABOTEUR) ??
      this.parties.get("harbour-fire");
    const holder = target?.lease ? `Holder is ${target.lease.owner}, token ${target.lease.token}` : "No lease is held on it";
    const name = target?.spec.name ?? "Harbour Fire";
    this.later(0.03, () => this.feed("gate", `REFUSED ${SABOTEUR}: place_call on ${name}. Rule: lease. ${holder}`, "warn",
      "gate_refused", target?.spec.id ?? null));
    this.later(0.08, () => this.refuse("operations: verified needs read-back",
      "Document failed validation on operations. state verified without readback_value", null, "atlas", "write_rejected"));
    this.later(0.12, () => this.feed("gate", `REFUSED ${SABOTEUR}: close_recall on ${RECALL_INFO._id}. Rule: allowlist`, "warn",
      "gate_refused"));
    this.later(0.17, () => this.feed("gate", `REFUSED ${SABOTEUR}: approve_signal on sig-01. Rule: allowlist`, "warn",
      "gate_refused"));
    this.later(0.22, () => this.feed("gate", `STOPPED ${SABOTEUR}: budget_exceeded after 6 tool calls. Party ${name} is due again`,
      "warn", "gate_stopped", target?.spec.id ?? null));
    this.later(seconds / REFERENCE_S_PER_HOUR, () => this.saboteurOff());
    return { ok: true, message: `Saboteur on for ${seconds} s. Watch the gate and Atlas refuse it` };
  }

  private saboteurOff(): void {
    if (!this.saboteurOn) return;
    this.saboteurOn = false;
    const i = this.workers.findIndex((w) => w.saboteur);
    if (i >= 0) this.workers.splice(i, 1);
    this.feed("supervisor", `Saboteur stopped. Invariants ${this.invariantCount()} of 4 hold`, "info", "saboteur");
  }

  private lie(partyId: string): OperatorResult {
    const p = this.parties.get(partyId);
    if (!this.recall) return { ok: false, message: "No recall is open yet" };
    if (!p) return { ok: false, message: `Unknown party ${partyId}` };
    const over = p.shipped_in + 20;
    const message = this.post(p, { in_stock: over - p.forwarded, installed: 0, fixed: 0, returned: 0 }, { source: "form" });
    if (message === null) return { ok: true, message: `${p.spec.name}: Atlas accepted the claim` };
    return { ok: false, message: `REJECTED by Atlas: ${message}`, refused: { rule: "conservation", message } };
  }

  private close(by: string): OperatorResult {
    if (!this.recall) return { ok: false, message: "No recall is open" };
    if (this.recall.status === "closed") return { ok: true, message: "ALL CLEAR: the recall is already closed" };
    const m = this.metrics();
    if (m.open_units > 0) {
      const headline = this.headline(m);
      const message = `Document failed validation on recalls. status closed with open_units ${m.open_units}`;
      this.refuse("close gate", message, null, "atlas", "close_refused");
      this.feed("ledger", `Close requested by ${by}. ${headline}`, "bad", "close_refused");
      this.feed("planner", `Close refused: ${m.open_units} panels open. Re-ranked the parties holding them`, "info", "boss");
      return { ok: false, message: headline, refused: { rule: "close gate", message } };
    }
    this.recall.status = "closed";
    this.recall.closedMs = this.nowMs;
    this.feed("ledger", `ALL CLEAR: every panel found and fixed or returned. Recall ${RECALL_INFO._id} closed`, "good", "recall_closed");
    return { ok: true, message: "ALL CLEAR" };
  }

  private invariantCount(): number {
    return this.invariantChecks().filter(Boolean).length;
  }

  private invariantChecks(): boolean[] {
    const live = new Set<string>();
    let one = true;
    for (const p of this.parties.values()) {
      if (p.live) {
        if (live.has(p.spec.id)) one = false;
        live.add(p.spec.id);
      }
    }
    const balance = this.recall ? this.metrics().mass_balance === UNITS_TOTAL : true;
    return [one, true, true, balance];
  }

  private invariants(announce: boolean): void {
    if (!this.recall) return;
    const passed = this.invariantChecks();
    this.lastInvariant = { sim: this.sim, passed };
    const n = passed.filter(Boolean).length;
    if (announce || n < 4) this.feed("invariants", `${n} of 4 hold`, n === 4 ? "good" : "bad", "invariants");
  }

  // ---- views ---------------------------------------------------------------------------------

  private metrics(): Metrics {
    let located = 0, fixedOrReturned = 0, installed = 0, unresolved = 0, onParties = 0, claimed = 0;
    for (const p of this.parties.values()) {
      located += p.in_stock + p.installed + p.fixed + p.returned;
      fixedOrReturned += p.fixed + p.returned;
      installed += p.installed;
      unresolved += p.unresolved;
      onParties += p.in_stock + p.installed + p.fixed + p.returned + p.unresolved;
      if (p.state === "claimed" || p.state === "reconciled") claimed++;
    }
    let awaiting = 0, disputed = 0, onEdges = 0;
    for (const e of this.edges) {
      const open = e.sender_qty - e.corroborated_qty;
      onEdges += open;
      if (e.recipient_qty === null) awaiting += open;
      else disputed += e.disputed_qty;
    }
    return {
      units_total: UNITS_TOTAL,
      checks_completed: [claimed, this.parties.size],
      panels_located: [located, UNITS_TOTAL],
      fixed_or_returned: [fixedOrReturned, UNITS_TOTAL],
      open_units: unresolved + installed + onEdges,
      not_located: unresolved + awaiting,
      awaiting_fix: installed,
      disputed_units: disputed,
      mass_balance: onParties + onEdges,
      status: this.recall?.status ?? "open",
    };
  }

  private headline(m: Metrics | null): string {
    if (!this.recall || !m) {
      const sig = this.signals.find((s) => s.status === "proposed");
      return sig ? `SIGNAL PROPOSED: batch ${BATCH} awaiting approval by a person` : `MONITORING ${PANELS.length} CONNECTED PANELS: NO RECALL OPEN`;
    }
    if (this.recall.status === "closed") return "ALL CLEAR";
    if (m.open_units === 0) return "0 panels open: the recall can close";
    const parts = [
      m.not_located ? `${fmt(m.not_located)} not located` : "",
      m.disputed_units ? `${fmt(m.disputed_units)} disputed` : "",
      m.awaiting_fix ? `${fmt(m.awaiting_fix)} awaiting fix` : "",
    ].filter(Boolean);
    return `NOT ALL CLEAR: ${fmt(m.open_units)} panels open (${parts.join(", ")})`;
  }

  private partyView(p: PartyRec): MockParty {
    const s = p.spec;
    let lease: Party["lease"] = null;
    if (p.lease) {
      const remaining = p.lease.deadSim === null
        ? LEASE_S - (((this.nowMs - p.lease.claimMs) / 1000) % 2)
        : LEASE_S - (this.sim - p.lease.deadSim) * REFERENCE_S_PER_HOUR;
      lease = { owner: p.lease.owner, token: p.lease.token, remaining_s: Math.round(remaining * 10) / 10, expired: remaining <= 0 };
    }
    const cu = this.connectedUnfixed(p);
    const linked = [...this.panels.values()].filter((x) => x.party_id === s.id).length;
    const disputedIn = this.edges.filter((e) => e.to === s.id).reduce((n, e) => n + e.disputed_qty, 0);
    return {
      party_id: s.id, name: s.name, type: s.type, tier: s.tier,
      channel: s.type === "distributor" ? "order_system" : s.voice ? "voice" : "sim",
      simulated: !s.voice, state: p.state, shipped_in: p.shipped_in,
      in_stock: p.in_stock, installed: p.installed, fixed: p.fixed, returned: p.returned, forwarded: p.forwarded,
      unresolved: p.unresolved, fixed_by_panel: p.fixed_by_panel, connected_unfixed: cu, connected_panels: linked,
      checks: p.checks, found_via: p.found_via, ladder_step: p.ladder_step, next_action_at: p.next_action_at,
      next_label: p.next_action_at === null ? null : hourLabel(p.next_action_at), hold_for_demo: p.hold, slice: !!s.slice,
      live_attempt: p.live ? { attempt_id: p.live.attempt_id, state: p.live.state, real: p.live.real, since_s: this.ref(p.live.sinceSim) } : null,
      attempts: p.attempts, lease, in_flight: p.in_flight,
      priority: p.unresolved + p.installed + disputedIn,
      order_system: s.orderSystem && s.type === "distributor" ? s.orderSystem : null,
      sites: s.sites ?? [], escalation: p.escalation, form_url: null, open_questions: p.open_questions,
    };
  }

  private eventView(e: PanelEventRec): PanelEventView {
    return { event_id: e.event_id, serial: e.serial, test_id: e.test_id, kind: e.kind, result: e.result, sw_version: e.sw_version,
      site_label: e.site_label, at: this.iso(e.ms), sim_label: hourLabel(e.sim), effect: e.effect };
  }

  private systemView(sys: SystemRec): SystemView {
    const ops = { intent: 0, sent: 0, verified: 0, needs_reverify: 0, failed: 0 };
    for (const o of sys.ops) ops[o.state] += 1;
    const source = { seed: "seed adapter", model: "repaired by model", episode: "from memory" }[sys.adapterSource];
    return { system: sys.key, label: ORDER_SYSTEMS[sys.key].label, status: sys.status,
      detail: `adapter v${sys.adapterVersion} (${source})  ${ops.verified} operations verified`,
      adapter_version: sys.adapterVersion, adapter_source: sys.adapterSource, operations: ops };
  }

  wallState(admin: boolean): WallState {
    const m = this.recall ? this.metrics() : null;
    const parties = [...this.parties.values()];
    const sim = this.sim;
    const workers: WorkerView[] = this.workers.map((w) => {
      const p = w.party ? this.parties.get(w.party) : undefined;
      return { id: w.id, alive: w.alive, saboteur: w.saboteur, party_id: p?.spec.id ?? null, party_name: p?.spec.name ?? null,
        kind: w.kind, since_s: p ? this.ref(w.sinceSim) : null, lease_token: p?.lease?.token ?? null, restarts: w.restarts };
    });
    const inFlight: CallInFlight[] = parties.filter((p) => p.live).map((p) => ({
      attempt_id: p.live!.attempt_id, party_id: p.spec.id, party_name: p.spec.name, real: p.live!.real, state: p.live!.state,
      since_s: this.ref(p.live!.sinceSim) }));
    const accepted = this.counters.accepted;
    const signals = this.signals.map<SignalView>((s) => ({ _id: s._id, batch_code: BATCH, rule: "3 failed tests at 3 sites within 30 days",
      events: [...s.events], sites: [...s.sites], shipped_to: PARTIES.filter((p) => p.tier === 1).map((p) => p.name),
      status: s.status, approved_by: s.approved_by, recall_id: s.recall_id }));
    const panels: ConnectedPanel[] = [...this.panels.values()].map((p) => ({ serial: p.spec.serial, site_label: p.spec.site,
      party_id: p.party_id, sw_version: p.sw_version, fault: p.fault, last_result: p.last?.result ?? null,
      last_at: p.last ? this.iso(p.last.ms) : null, fixed: p.fixed }));
    const events = this.panelEvents.slice(-20).reverse().map((e) => this.eventView(e));
    const inv = this.lastInvariant;
    const approved = this.signals.filter((s) => s.status === "approved").length;
    return {
      now: this.iso(this.nowMs),
      recall: this.recall ? { ...RECALL_INFO, status: this.recall.status, source: "signal", source_signal: "sig-01", mode: "demo",
        opened_at: this.iso(this.recall.openedMs), closed_at: this.recall.closedMs === null ? null : this.iso(this.recall.closedMs) } : null,
      recalls: this.recall ? [{ _id: RECALL_INFO._id, status: this.recall.status, batch_code: BATCH }] : [],
      metrics: m,
      headline: this.headline(m),
      parties: parties.map((p) => this.partyView(p)),
      edges: this.edges.map<Edge>((e) => ({ from_party: e.from, to_party: e.to, sender_qty: e.sender_qty, recipient_qty: e.recipient_qty,
        corroborated_qty: e.corroborated_qty, disputed_qty: e.disputed_qty,
        state: e.recipient_qty === null ? "awaiting_recipient" : e.disputed_qty > 0 ? "disputed" : "corroborated" })),
      feed: this.feedLines.slice(-80).reverse(),
      workers,
      calls: { total: this.counters.real + this.counters.sim, real: this.counters.real, simulated: this.counters.sim },
      calls_in_flight: inFlight,
      run: {
        sim_now: Math.round(sim * 1000) / 1000, sim_label: hourLabel(sim), day: Math.min(10, dayOf(sim)), days_total: 10,
        real_elapsed_s: Math.round(this.nowMs / 1000), seconds_per_sim_hour: this.epoch.frozen ? 0 : this.clock.speedAt(this.nowMs),
        paused: this.epoch.frozen || this.clock.speedAt(this.nowMs) === 0,
        parties: { total: parties.length, at_start: Math.min(parties.length, 5), found: Math.max(0, parties.length - 5) },
        steps: this.counters.steps, kills: this.counters.kills, adoptions: this.counters.adoptions, duplicates: 0,
        model_calls: this.counters.modelCalls,
        tokens_per_decision: 1838 + Math.round(hash(`tok${this.counters.decisions}`) * 24),
        invariants: { at_label: inv ? hourLabel(inv.sim) : null, passed: inv?.passed ?? [true, true, true, true], names: INVARIANT_NAMES,
          ok: (inv?.passed ?? []).every(Boolean) },
        supervisor: { paused: sim >= SLICE_AT }, saboteur: { on: this.saboteurOn },
      },
      systems: [
        { system: "phone", label: "phone", status: "ok", detail: `${this.counters.attempts} attempts, ${accepted} accepted claims` },
        this.systemView(this.systems.north),
        this.systemView(this.systems.east),
        { system: "panels", label: "panels", status: "ok",
          detail: `${this.panelEvents.length} test events, ${approved} signal${approved === 1 ? "" : "s"} approved` },
      ],
      monitoring: {
        connected: PANELS.length,
        failing: panels.filter((p) => p.last_result === "fail" && !p.fixed).length,
        passed_after_fix: panels.filter((p) => p.fixed).length,
        last_event: events[0] ?? null, events, panels, signals,
      },
      memory_ablation: this.recall ? { with_memory: 4, without_memory: 7 } : null,
      refusals: this.refusalList.slice(0, 40),
      admin,
      failpoint: this.failpoint,
    };
  }

  panelState(serial: string): PanelPageState | null {
    const panel = this.panels.get(serial);
    if (!panel) return null;
    const party = panel.party_id ? this.parties.get(panel.party_id) : undefined;
    const mine = this.panelEvents.filter((e) => e.serial === serial);
    const last = mine[mine.length - 1];
    return {
      serial, model: MODEL, batch_code: BATCH, sw_version: panel.sw_version, site_label: panel.spec.site,
      party_id: party?.spec.id ?? null, party_name: party?.spec.name ?? null, fault: panel.fault,
      recall: this.recall ? { _id: RECALL_INFO._id, affected_versions: AFFECTED, fixed_version: FIXED_VERSION, status: this.recall.status } : null,
      last_test: last ? { event_id: last.event_id, result: last.result, detail: last.result === "fail" ? "sounder circuit" :
        "sounder circuit normal", at: this.iso(last.ms), sim_label: hourLabel(last.sim) } : null,
      events: mine.slice(-10).reverse().map((e) => this.eventView(e)),
      fixed: panel.fixed,
    };
  }
}
