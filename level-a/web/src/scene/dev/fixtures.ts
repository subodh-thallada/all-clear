// Dev fixtures for the 3D scene: WallState snapshots of the long run of batch B-4471 (2,400 DEMO-7
// panels, 40 parties) at four moments, built from one table of parties so every snapshot conserves
// panels exactly (the same mass balance the API asserts). Names are fictional.

import type {
  CallInFlight,
  ConnectedPanel,
  Edge,
  FeedLine,
  Metrics,
  Party,
  PanelEventView,
  PanelPageState,
  Recall,
  SignalView,
  SystemView,
  WallState,
  WorkerView,
} from "../../api/types";

export type Stage = "early" | "mid" | "late" | "closed";

type Kind = Party["type"];

interface Def {
  id: string;
  name: string;
  type: Kind;
  tier: 1 | 2 | 3;
  parent: string | null; // null = shipped by the supplier (tier 1)
  qty: number; // what the sender shipped to it
  stock: number;
  fixed: number;
  returned: number;
  sites?: string[];
  order_system?: "north" | "east";
  voice?: boolean;
  slice?: boolean;
}

const D: Def[] = [
  { id: "harbour-fire", name: "Harbour Fire", type: "installer", tier: 1, parent: null, qty: 40, stock: 12, fixed: 0, returned: 28, voice: true, slice: true },
  { id: "queen-st-alarm", name: "Queen St Alarm", type: "installer", tier: 1, parent: null, qty: 120, stock: 50, fixed: 0, returned: 30, slice: true },
  { id: "lakeside-systems", name: "Lakeside Systems", type: "installer", tier: 1, parent: null, qty: 80, stock: 20, fixed: 0, returned: 60, slice: true },
  { id: "dist-north", name: "Distributor North", type: "distributor", tier: 1, parent: null, qty: 1300, stock: 0, fixed: 0, returned: 120, order_system: "north" },
  { id: "dist-east", name: "Distributor East", type: "distributor", tier: 1, parent: null, qty: 860, stock: 0, fixed: 0, returned: 60, order_system: "east" },
  // Distributor North's accounts
  { id: "bloor-alarm", name: "Bloor Alarm", type: "installer", tier: 2, parent: "dist-north", qty: 140, stock: 14, fixed: 44, returned: 18 },
  { id: "annex-fire", name: "Annex Fire", type: "installer", tier: 2, parent: "dist-north", qty: 120, stock: 10, fixed: 34, returned: 12 },
  { id: "danforth-fire", name: "Danforth Fire Protection", type: "installer", tier: 2, parent: "dist-north", qty: 96, stock: 18, fixed: 58, returned: 20 },
  { id: "yonge-signal", name: "Yonge Signal", type: "installer", tier: 2, parent: "dist-north", qty: 110, stock: 12, fixed: 40, returned: 18 },
  { id: "etobicoke-life-safety", name: "Etobicoke Life Safety", type: "installer", tier: 2, parent: "dist-north", qty: 85, stock: 15, fixed: 52, returned: 18 },
  { id: "leaside-fire", name: "Leaside Fire Systems", type: "installer", tier: 2, parent: "dist-north", qty: 99, stock: 11, fixed: 40, returned: 16 },
  { id: "keele-alarm", name: "Keele Alarm Co", type: "installer", tier: 2, parent: "dist-north", qty: 89, stock: 17, fixed: 50, returned: 22 },
  { id: "weston-fire", name: "Weston Fire & Security", type: "installer", tier: 2, parent: "dist-north", qty: 104, stock: 14, fixed: 44, returned: 16 },
  { id: "junction-alarm", name: "Junction Alarm", type: "installer", tier: 2, parent: "dist-north", qty: 102, stock: 20, fixed: 60, returned: 22 },
  { id: "davisville-alarm", name: "Davisville Alarm", type: "installer", tier: 2, parent: "dist-north", qty: 92, stock: 16, fixed: 56, returned: 20 },
  { id: "humber-signal", name: "Humber Signal", type: "installer", tier: 2, parent: "dist-north", qty: 70, stock: 10, fixed: 44, returned: 16 },
  { id: "rexdale-fire", name: "Rexdale Fire", type: "installer", tier: 2, parent: "dist-north", qty: 73, stock: 13, fixed: 40, returned: 20 },
  // Distributor East's accounts
  { id: "scarborough-fire", name: "Scarborough Fire Tech", type: "installer", tier: 2, parent: "dist-east", qty: 147, stock: 15, fixed: 44, returned: 18 },
  { id: "beaches-alarm", name: "Beaches Alarm", type: "installer", tier: 2, parent: "dist-east", qty: 120, stock: 14, fixed: 50, returned: 18 },
  { id: "guildwood-fire", name: "Guildwood Fire", type: "installer", tier: 2, parent: "dist-east", qty: 62, stock: 10, fixed: 38, returned: 14 },
  { id: "pickering-life-safety", name: "Pickering Life Safety", type: "installer", tier: 2, parent: "dist-east", qty: 97, stock: 17, fixed: 58, returned: 22 },
  { id: "ajax-alarm", name: "Ajax Alarm Services", type: "installer", tier: 2, parent: "dist-east", qty: 62, stock: 12, fixed: 36, returned: 14 },
  { id: "kennedy-fire", name: "Kennedy Fire Systems", type: "installer", tier: 2, parent: "dist-east", qty: 65, stock: 7, fixed: 24, returned: 10 },
  { id: "malvern-signal", name: "Malvern Signal", type: "installer", tier: 2, parent: "dist-east", qty: 96, stock: 16, fixed: 58, returned: 22 },
  { id: "port-union-fire", name: "Port Union Fire", type: "installer", tier: 2, parent: "dist-east", qty: 85, stock: 15, fixed: 50, returned: 20 },
  { id: "rouge-valley-alarm", name: "Rouge Valley Alarm", type: "installer", tier: 2, parent: "dist-east", qty: 66, stock: 12, fixed: 40, returned: 14 },
  // Named in claims: building owners and further installers
  { id: "maple-court", name: "Maple Court", type: "owner", tier: 3, parent: "bloor-alarm", qty: 30, stock: 0, fixed: 30, returned: 0, sites: ["Maple Court"] },
  { id: "cabbagetown-fire", name: "Cabbagetown Fire Services", type: "installer", tier: 3, parent: "bloor-alarm", qty: 34, stock: 4, fixed: 12, returned: 0 },
  { id: "riverdale-medical", name: "Riverdale Medical Centre", type: "owner", tier: 3, parent: "cabbagetown-fire", qty: 18, stock: 0, fixed: 18, returned: 0, sites: ["Riverdale Medical, East Wing", "Riverdale Medical, West Wing"] },
  { id: "dundas-lofts", name: "Dundas Lofts", type: "owner", tier: 3, parent: "annex-fire", qty: 36, stock: 0, fixed: 36, returned: 0, sites: ["Dundas Lofts North", "Dundas Lofts South"] },
  { id: "spadina-works", name: "Spadina Works", type: "owner", tier: 3, parent: "annex-fire", qty: 28, stock: 0, fixed: 28, returned: 0, sites: ["Spadina Works"] },
  { id: "midtown-office-park", name: "Midtown Office Park", type: "owner", tier: 3, parent: "yonge-signal", qty: 40, stock: 0, fixed: 40, returned: 0, sites: ["Midtown Office Park, Bldg A", "Midtown Office Park, Bldg B", "Midtown Office Park, Bldg C"] },
  { id: "bayview-annex", name: "Bayview Annex", type: "owner", tier: 3, parent: "leaside-fire", qty: 32, stock: 0, fixed: 32, returned: 0, sites: ["Bayview Annex"] },
  { id: "parkdale-alarm", name: "Parkdale Alarm", type: "installer", tier: 3, parent: "weston-fire", qty: 30, stock: 2, fixed: 6, returned: 0 },
  { id: "liberty-village", name: "Liberty Village Towers", type: "owner", tier: 3, parent: "parkdale-alarm", qty: 22, stock: 0, fixed: 22, returned: 0, sites: ["Liberty Village Towers 1", "Liberty Village Towers 2"] },
  { id: "don-mills-centre", name: "Don Mills Centre", type: "owner", tier: 3, parent: "scarborough-fire", qty: 44, stock: 0, fixed: 44, returned: 0, sites: ["Don Mills Centre"] },
  { id: "st-clair-commons", name: "St Clair Commons", type: "owner", tier: 3, parent: "scarborough-fire", qty: 26, stock: 0, fixed: 26, returned: 0, sites: ["St Clair Commons"] },
  { id: "harbourfront-residences", name: "Harbourfront Residences", type: "owner", tier: 3, parent: "beaches-alarm", qty: 38, stock: 0, fixed: 38, returned: 0, sites: ["Harbourfront Residences East", "Harbourfront Residences West"] },
  { id: "kingsway-place", name: "Kingsway Place", type: "owner", tier: 3, parent: "kennedy-fire", qty: 24, stock: 0, fixed: 24, returned: 0, sites: ["Kingsway Place"] },
  { id: "corner-properties", name: "Corner Properties", type: "owner", tier: 3, parent: "queen-st-alarm", qty: 40, stock: 0, fixed: 0, returned: 0, voice: true, slice: true, sites: ["Corner Properties, Tower 1", "Corner Properties, Tower 2", "Corner Properties, Tower 3"] },
];

const CONNECTED: [serial: string, site: string, party: string][] = [
  ["D7-4471-0117", "Maple Court", "maple-court"],
  ["D7-4471-0118", "Maple Court", "maple-court"],
  ["D7-4471-0119", "Maple Court", "maple-court"],
  ["D7-4471-0042", "Bayview Annex", "bayview-annex"],
  ["D7-4471-0043", "Bayview Annex", "bayview-annex"],
  ["D7-4471-0088", "Spadina Works", "spadina-works"],
  ["D7-4471-0089", "Spadina Works", "spadina-works"],
  ["D7-4471-0193", "Corner Properties, Tower 2", "corner-properties"],
  ["D7-4471-0301", "Dundas Lofts North", "dundas-lofts"],
  ["D7-4471-0302", "Dundas Lofts North", "dundas-lofts"],
  ["D7-4471-0310", "Dundas Lofts South", "dundas-lofts"],
  ["D7-4471-0411", "Midtown Office Park, Bldg A", "midtown-office-park"],
  ["D7-4471-0412", "Midtown Office Park, Bldg A", "midtown-office-park"],
  ["D7-4471-0430", "Midtown Office Park, Bldg C", "midtown-office-park"],
  ["D7-4471-0520", "Riverdale Medical, East Wing", "riverdale-medical"],
  ["D7-4471-0521", "Riverdale Medical, West Wing", "riverdale-medical"],
  ["D7-4471-0640", "Liberty Village Towers 1", "liberty-village"],
  ["D7-4471-0641", "Liberty Village Towers 1", "liberty-village"],
  ["D7-4471-0702", "Don Mills Centre", "don-mills-centre"],
  ["D7-4471-0703", "Don Mills Centre", "don-mills-centre"],
  ["D7-4471-0811", "Harbourfront Residences West", "harbourfront-residences"],
  ["D7-4471-0812", "Harbourfront Residences West", "harbourfront-residences"],
  ["D7-4471-0905", "Kingsway Place", "kingsway-place"],
  ["D7-4471-0920", "St Clair Commons", "st-clair-commons"],
];

const SIGNAL_SERIALS = ["D7-4471-0117", "D7-4471-0042", "D7-4471-0088"];

export const RECALL: Recall = {
  _id: "r-1",
  batch_code: "B-4471",
  model: "DEMO-7",
  product: "DEMO-7 fire alarm control panel",
  supplier: "Kestrel Life Safety",
  hazard: "The sounder circuit may fail to activate in an alarm on software 2.3 and 2.4",
  affected_versions: ["2.3", "2.4"],
  fixed_version: "2.5",
  units_total: 2400,
  status: "open",
  source: "signal",
  source_signal: "sig-01",
  mode: "sim",
  opened_at: "2026-11-14T16:55:00-05:00",
  closed_at: null,
};

export function simLabel(h: number): string {
  const d = Math.floor(h / 24);
  const hh = Math.floor(h % 24);
  const mm = Math.round((h % 1) * 60);
  return `d${d} ${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------------------------
// Ledger arithmetic shared by the fixtures and the dev page's live edits

export function recompute(state: WallState): WallState {
  const parties = state.parties;
  const edges = state.edges;
  let located = 0;
  let unresolved = 0;
  let installed = 0;
  let fixedOrReturned = 0;
  for (const p of parties) {
    p.forwarded = edges.filter((e) => e.from_party === p.party_id).reduce((s, e) => s + e.sender_qty, 0);
    p.unresolved = p.shipped_in - (p.in_stock + p.installed + p.fixed + p.returned + p.forwarded);
    if (p.unresolved < 0) throw new Error(`fixture does not conserve at ${p.party_id}: ${p.unresolved}`);
    located += p.in_stock + p.installed + p.fixed + p.returned;
    unresolved += p.unresolved;
    installed += p.installed;
    fixedOrReturned += p.fixed + p.returned;
  }
  let disputed = 0;
  let awaiting = 0;
  for (const e of edges) {
    const open = e.sender_qty - e.corroborated_qty;
    if (e.state === "disputed") disputed += open;
    else awaiting += open;
  }
  const total = state.recall?.units_total ?? 2400;
  const open = unresolved + installed + disputed + awaiting;
  const metrics: Metrics = {
    units_total: total,
    checks_completed: [parties.filter((p) => p.state === "claimed" || p.state === "reconciled").length, parties.length],
    panels_located: [located, total],
    fixed_or_returned: [fixedOrReturned, total],
    open_units: open,
    not_located: unresolved + awaiting,
    awaiting_fix: installed,
    disputed_units: disputed,
    mass_balance: located + unresolved + disputed + awaiting,
    status: state.recall?.status ?? "open",
  };
  if (metrics.mass_balance !== total) throw new Error(`mass balance ${metrics.mass_balance} != ${total}`);
  const parts = [disputed && `${disputed} disputed`, metrics.not_located && `${metrics.not_located} not located`, installed && `${installed} awaiting fix`].filter(Boolean);
  const mon = state.monitoring;
  mon.failing = mon.panels.filter((p) => !p.fixed && p.last_result === "fail").length;
  mon.passed_after_fix = mon.panels.filter((p) => p.fixed).length;
  return {
    ...state,
    metrics,
    headline: open === 0 && state.recall?.status === "closed" ? "ALL CLEAR" : `NOT ALL CLEAR: ${open.toLocaleString("en-US")} panels open (${parts.join(", ")})`,
  };
}

// ---------------------------------------------------------------------------------------------

interface PartyPlan {
  state: Party["state"];
  in_stock?: number;
  installed?: number;
  fixed?: number;
  returned?: number;
  received?: number; // recipient's claim (corroborated = min of the two)
  ladder?: number;
  next?: number;
  hold?: boolean;
  escalation?: string;
}

function makeParty(d: Def, plan: PartyPlan, shippedIn: number): Party {
  const simulated = !d.voice;
  return {
    party_id: d.id,
    name: d.name,
    type: d.type,
    tier: d.tier,
    channel: d.type === "distributor" ? "order_system" : d.voice ? "voice" : "sim",
    simulated,
    state: plan.state,
    shipped_in: shippedIn,
    in_stock: plan.in_stock ?? 0,
    installed: plan.installed ?? 0,
    fixed: plan.fixed ?? 0,
    returned: plan.returned ?? 0,
    forwarded: 0,
    unresolved: 0,
    fixed_by_panel: 0,
    connected_unfixed: 0,
    connected_panels: 0,
    checks: d.type === "owner" && plan.state !== "pending" ? { model_seen: "DEMO-7", version_seen: (plan.installed ?? 0) > 0 ? "2.4" : "2.5", sounder_test: (plan.installed ?? 0) === 0 } : null,
    found_via: d.tier === 1 ? "shipment" : d.tier === 2 ? "order_system" : "claim",
    ladder_step: plan.ladder ?? (plan.state === "pending" ? 0 : 1),
    next_action_at: plan.next ?? null,
    next_label: plan.next !== undefined ? simLabel(plan.next) : null,
    hold_for_demo: !!plan.hold,
    slice: !!d.slice,
    live_attempt: null,
    attempts: plan.state === "pending" ? 0 : plan.ladder ?? 1,
    lease: null,
    in_flight: null,
    priority: 0,
    order_system: d.order_system ?? null,
    sites: d.sites ?? [],
    escalation: plan.escalation ?? null,
    form_url: null,
  };
}

function finalPlan(d: Def): PartyPlan {
  return { state: "reconciled", in_stock: d.stock, fixed: d.fixed, returned: d.returned };
}

/** Mid-run (day 6) progress per party: what is still open. */
const MID: Record<string, PartyPlan> = {
  "harbour-fire": { state: "pending", hold: true, next: 0 },
  "queen-st-alarm": { state: "claimed", in_stock: 50, returned: 30 },
  "lakeside-systems": { state: "reconciled", in_stock: 20, returned: 60 },
  "dist-north": { state: "reached", returned: 120 },
  "dist-east": { state: "reached", returned: 60 },
  "bloor-alarm": { state: "reached", in_stock: 14, installed: 20, fixed: 24, returned: 18, next: 168 },
  "annex-fire": { state: "claimed", in_stock: 10, installed: 8, fixed: 26, returned: 12, next: 170 },
  "danforth-fire": { state: "reconciled", in_stock: 18, fixed: 58, returned: 20 },
  "yonge-signal": { state: "claimed", in_stock: 12, installed: 14, fixed: 26, returned: 18, next: 175 },
  "etobicoke-life-safety": { state: "reconciled", in_stock: 15, fixed: 52, returned: 18 },
  "leaside-fire": { state: "reached", in_stock: 11, installed: 22, fixed: 18, returned: 16, next: 166 },
  "keele-alarm": { state: "reconciled", in_stock: 17, fixed: 50, returned: 22 },
  "weston-fire": { state: "claimed", in_stock: 14, installed: 10, fixed: 34, returned: 16, next: 171 },
  "junction-alarm": { state: "reconciled", in_stock: 20, fixed: 60, returned: 22 },
  "davisville-alarm": { state: "contacting", ladder: 2, next: 158 },
  "humber-signal": { state: "contacting", ladder: 1, next: 177 },
  "rexdale-fire": { state: "contacting", ladder: 4, next: 189, escalation: "ladder step 4: third call at d7 21:00, then the distributor's rep" },
  "scarborough-fire": { state: "claimed", in_stock: 15, installed: 12, fixed: 32, returned: 18, next: 172 },
  "beaches-alarm": { state: "reached", in_stock: 14, installed: 16, fixed: 34, returned: 18, next: 169 },
  "guildwood-fire": { state: "reconciled", in_stock: 10, fixed: 38, returned: 14 },
  "pickering-life-safety": { state: "claimed", in_stock: 17, installed: 18, fixed: 40, returned: 22, next: 174 },
  "ajax-alarm": { state: "reconciled", in_stock: 12, fixed: 36, returned: 14 },
  "kennedy-fire": { state: "contacting", in_stock: 7, installed: 6, fixed: 18, returned: 10, ladder: 1, next: 158 },
  "malvern-signal": { state: "contacting", ladder: 1, next: 159 },
  "port-union-fire": { state: "pending", received: 0, next: 160 },
  "rouge-valley-alarm": { state: "pending", received: 0, next: 161 },
  "maple-court": { state: "claimed", installed: 6, fixed: 24, next: 176 },
  "cabbagetown-fire": { state: "reached", in_stock: 4, installed: 4, received: 26, next: 164 },
  "riverdale-medical": { state: "claimed", installed: 18, next: 180 },
  "dundas-lofts": { state: "reconciled", fixed: 36 },
  "spadina-works": { state: "claimed", installed: 28, next: 178 },
  "midtown-office-park": { state: "claimed", installed: 10, fixed: 30, next: 181 },
  "bayview-annex": { state: "claimed", installed: 32, next: 179 },
  "parkdale-alarm": { state: "claimed", in_stock: 2, installed: 6, next: 182 },
  "liberty-village": { state: "claimed", installed: 22, next: 183 },
  "don-mills-centre": { state: "reconciled", fixed: 44 },
  "st-clair-commons": { state: "claimed", installed: 10, fixed: 16, next: 184 },
  "harbourfront-residences": { state: "claimed", installed: 20, fixed: 18, next: 185 },
  "kingsway-place": { state: "claimed", installed: 24, next: 186 },
  "corner-properties": { state: "contacting", received: 0, next: 158 },
};

const MID_FIXED_PANELS = new Set(["D7-4471-0117", "D7-4471-0118", "D7-4471-0301", "D7-4471-0302", "D7-4471-0310", "D7-4471-0411", "D7-4471-0412", "D7-4471-0702", "D7-4471-0703"]);
const MID_FAILING = new Set(["D7-4471-0042", "D7-4471-0088", "D7-4471-0193"]);

function workers(list: [id: string, party: string | null, kind: string | null, since: number][], parties: Party[]): WorkerView[] {
  return list.map(([id, party, kind, since], i) => ({
    id,
    alive: true,
    saboteur: false,
    party_id: party,
    party_name: party ? parties.find((p) => p.party_id === party)?.name ?? party : null,
    kind,
    since_s: party ? since : null,
    lease_token: party ? 3 + i * 2 : null,
    restarts: [2, 3, 2, 2][i] ?? 0,
  }));
}

function lease(p: Party, owner: string, token: number, remaining: number) {
  p.lease = { owner, token, remaining_s: remaining, expired: false };
}

function call(p: Party, id: string, real: boolean, state: CallInFlight["state"], since: number): CallInFlight {
  p.live_attempt = { attempt_id: id, state, real, since_s: since };
  return { attempt_id: id, party_id: p.party_id, party_name: p.name, real, state, since_s: since };
}

function feed(lines: [label: string, system: string, text: string, level: FeedLine["level"], party?: string][]): FeedLine[] {
  return lines.map(([label, system, text, level, party], i) => ({
    id: `f-${String(lines.length - i).padStart(4, "0")}`,
    at: "2026-11-14T18:14:00-05:00",
    sim_label: label,
    system,
    text,
    level,
    party_id: party ?? null,
    kind: system,
  }));
}

function systems(north: SystemView["status"], northV: number, northSrc: SystemView["adapter_source"], eastV: number, eastSrc: SystemView["adapter_source"]): SystemView[] {
  return [
    { system: "phone", label: "phone", status: "ok", detail: "58 attempts, 29 accepted claims" },
    {
      system: "north",
      label: "North orders",
      status: north,
      detail: north === "contract_failed" ? "contract failed: field qty_in_stock missing" : `adapter v${northV} (${northSrc})`,
      adapter_version: northV,
      adapter_source: northSrc,
      operations: { intent: 0, sent: north === "ok" ? 0 : 4, verified: 20, needs_reverify: north === "ok" ? 0 : 4, failed: 0 },
    },
    {
      system: "east",
      label: "East orders",
      status: "ok",
      detail: `adapter v${eastV} (${eastSrc})`,
      adapter_version: eastV,
      adapter_source: eastSrc,
      operations: { intent: 0, sent: 0, verified: 19, needs_reverify: 0, failed: 0 },
    },
    { system: "panels", label: "panels", status: "ok", detail: "41 test events, 1 signal approved" },
  ];
}

function panelEvent(n: number, serial: string, site: string, result: "pass" | "fail", sw: string, label: string, effect: string | null): PanelEventView {
  return { event_id: `pe-${String(n).padStart(4, "0")}`, serial, test_id: `t-${n}`, kind: "monthly_test", result, sw_version: sw, site_label: site, at: "2026-11-14T17:00:00-05:00", sim_label: label, effect };
}

const SIGNAL: SignalView = {
  _id: "sig-01",
  batch_code: "B-4471",
  rule: "3 failed tests at 3 sites within 30 days",
  events: ["pe-0007", "pe-0011", "pe-0012"],
  sites: ["Maple Court", "Bayview Annex", "Spadina Works"],
  shipped_to: ["Distributor North", "Distributor East", "Harbour Fire", "Queen St Alarm", "Lakeside Systems"],
  status: "approved",
  approved_by: "operator-1",
  recall_id: "r-1",
};

function baseState(): WallState {
  return {
    now: "2026-11-14T18:14:00-05:00",
    recall: { ...RECALL },
    recalls: [{ _id: "r-1", status: "open", batch_code: "B-4471" }],
    metrics: null,
    headline: "",
    parties: [],
    edges: [],
    feed: [],
    workers: [],
    calls: { total: 58, real: 3, simulated: 55 },
    calls_in_flight: [],
    run: null,
    systems: [],
    monitoring: { connected: 24, failing: 0, passed_after_fix: 0, last_event: null, events: [], panels: [], signals: [{ ...SIGNAL }] },
    memory_ablation: { with_memory: 11, without_memory: 17 },
    refusals: [],
    admin: true,
    failpoint: null,
  };
}

function run(simNow: number, steps: number, kills: number, found: number, total: number, sab = false): WallState["run"] {
  return {
    sim_now: simNow,
    sim_label: simLabel(simNow),
    day: Math.floor(simNow / 24),
    days_total: 10,
    real_elapsed_s: simNow * 30,
    seconds_per_sim_hour: 30,
    paused: false,
    parties: { total, at_start: 5, found },
    steps,
    kills,
    adoptions: kills,
    duplicates: 0,
    model_calls: Math.round(steps * 0.4),
    tokens_per_decision: 1850,
    invariants: { at_label: simLabel(Math.floor(simNow)), passed: [true, true, true, true], names: ["one live attempt", "one hold per key", "claims have evidence", "mass balance"], ok: true },
    supervisor: { paused: false },
    saboteur: { on: sab },
  };
}

function connectedPanels(linkedParties: Set<string>, fixed: Set<string>, failing: Set<string>, swFixed: boolean): ConnectedPanel[] {
  return CONNECTED.map(([serial, site, party]) => {
    const isFixed = fixed.has(serial);
    return {
      serial,
      site_label: site,
      party_id: linkedParties.has(party) ? party : null,
      sw_version: isFixed || swFixed ? "2.5" : "2.4",
      fault: failing.has(serial),
      last_result: isFixed ? "pass" : failing.has(serial) ? "fail" : "pass",
      last_at: "2026-11-14T17:00:00-05:00",
      fixed: isFixed,
    };
  });
}

/** Sets each party's connected-panel fields from the panels linked to it. */
function linkPanels(parties: Party[], panels: ConnectedPanel[]) {
  for (const p of parties) {
    const mine = panels.filter((c) => c.party_id === p.party_id);
    p.connected_panels = mine.length;
    p.fixed_by_panel = mine.filter((c) => c.fixed).length;
    p.connected_unfixed = mine.filter((c) => !c.fixed).length;
  }
}

function buildParties(plans: (d: Def) => PartyPlan | null): { parties: Party[]; edges: Edge[] } {
  const parties: Party[] = [];
  const edges: Edge[] = [];
  for (const d of D) {
    const plan = plans(d);
    if (!plan) continue;
    let shippedIn = d.qty;
    if (d.parent) {
      const received = plan.received ?? (plan.state === "pending" ? 0 : d.qty);
      const corroborated = Math.min(received, d.qty);
      shippedIn = corroborated;
      edges.push({
        from_party: d.parent,
        to_party: d.id,
        sender_qty: d.qty,
        recipient_qty: plan.state === "pending" && plan.received === undefined ? null : received === 0 ? null : received,
        corroborated_qty: corroborated,
        disputed_qty: received > 0 && received !== d.qty ? d.qty - corroborated : 0,
        state: received === 0 ? "awaiting_recipient" : received !== d.qty ? "disputed" : "corroborated",
      });
    }
    const p = makeParty(d, plan, shippedIn);
    if (plan.state === "pending" || (d.parent && shippedIn === 0)) {
      p.in_stock = 0;
      p.installed = 0;
      p.fixed = 0;
      p.returned = 0;
    }
    parties.push(p);
  }
  return { parties, edges };
}

export function buildStage(stage: Stage): WallState {
  const s = baseState();
  if (stage === "early") {
    const simNow = 9;
    const { parties, edges } = buildParties((d) => (d.tier === 1 ? { state: d.id === "harbour-fire" ? "pending" : "contacting", hold: d.id === "harbour-fire", next: 9 } : null));
    s.parties = parties;
    s.edges = edges;
    const by = (id: string) => parties.find((p) => p.party_id === id) as Party;
    s.workers = workers(
      [
        ["w1", "dist-north", "place_hold", 4],
        ["w2", "dist-east", "place_hold", 3],
        ["w3", "queen-st-alarm", "contact_party", 6],
        ["w4", "lakeside-systems", "contact_party", 2],
      ],
      parties,
    );
    lease(by("dist-north"), "w1", 3, 6.4);
    lease(by("dist-east"), "w2", 5, 7.1);
    lease(by("queen-st-alarm"), "w3", 7, 5.2);
    lease(by("lakeside-systems"), "w4", 9, 7.8);
    s.calls_in_flight = [call(by("queen-st-alarm"), "at-0004", false, "dialed", 6), call(by("lakeside-systems"), "at-0005", false, "intent", 2)];
    s.calls = { total: 5, real: 0, simulated: 5 };
    s.systems = systems("ok", 1, "seed", 1, "seed");
    s.monitoring.panels = connectedPanels(new Set(), new Set(), new Set(SIGNAL_SERIALS), false);
    s.monitoring.events = [
      panelEvent(12, "D7-4471-0088", "Spadina Works", "fail", "2.4", "d0 07:40", null),
      panelEvent(11, "D7-4471-0042", "Bayview Annex", "fail", "2.4", "d0 07:10", null),
      panelEvent(7, "D7-4471-0117", "Maple Court", "fail", "2.4", "d0 06:20", null),
    ];
    s.run = run(simNow, 14, 0, 0, 5);
    s.feed = feed([
      ["d0 09:00", "worker w1", "claimed Distributor North: place_hold, intent op-0001", "info", "dist-north"],
      ["d0 08:05", "signal", "3 failed tests at 3 sites, batch B-4471. Approved by operator-1. Recall opened", "warn"],
    ]);
  } else if (stage === "mid") {
    const simNow = 158;
    const { parties, edges } = buildParties((d) => MID[d.id] ?? finalPlan(d));
    s.parties = parties;
    s.edges = edges;
    const by = (id: string) => parties.find((p) => p.party_id === id) as Party;
    s.workers = workers(
      [
        ["w1", "davisville-alarm", "contact_party", 8],
        ["w2", "corner-properties", "contact_party", 21],
        ["w3", "dist-north", "repair_adapter", 46],
        ["w4", "kennedy-fire", "fix_check", 3],
      ],
      parties,
    );
    lease(by("davisville-alarm"), "w1", 12, 6.8);
    lease(by("corner-properties"), "w2", 9, 7.4);
    lease(by("dist-north"), "w3", 18, 5.9);
    lease(by("kennedy-fire"), "w4", 6, 7.9);
    s.calls_in_flight = [
      call(by("corner-properties"), "at-0412", true, "connected", 19),
      call(by("davisville-alarm"), "at-0409", false, "dialed", 7),
      call(by("kennedy-fire"), "at-0413", false, "intent", 1),
    ];
    s.calls = { total: 58, real: 2, simulated: 56 };
    s.systems = systems("contract_failed", 1, "seed", 2, "episode");
    // Corner Properties has not answered yet, so its connected panel is not linked to it.
    const panels = connectedPanels(new Set(D.map((d) => d.id).filter((id) => id !== "corner-properties")), MID_FIXED_PANELS, MID_FAILING, false);
    s.monitoring.panels = panels;
    linkPanels(parties, panels);
    s.monitoring.events = [
      panelEvent(41, "D7-4471-0703", "Don Mills Centre", "pass", "2.5", "d6 11:10", "Don Mills Centre: 1 panel fixed"),
      panelEvent(40, "D7-4471-0042", "Bayview Annex", "fail", "2.4", "d6 07:00", null),
      panelEvent(38, "D7-4471-0117", "Maple Court", "pass", "2.5", "d5 16:30", "Maple Court: 1 panel fixed"),
    ];
    s.refusals = [
      { at: "2026-11-14T18:10:00-05:00", sim_label: "d6 11:40", rule: "parties validator", message: "REJECTED by Atlas: Document failed validation on parties. Reported 120, shipped_in 96", party_id: "malvern-signal" },
      { at: "2026-11-14T18:02:00-05:00", sim_label: "d6 09:15", rule: "fixed needs evidence", message: "REJECTED by Atlas: Document failed validation on parties. version_seen 2.4 is affected", party_id: "bayview-annex" },
    ];
    s.run = run(simNow, 412, 9, 35, 40);
    s.feed = feed([
      ["d6 14:00", "invariants", "4 of 4 hold", "good"],
      ["d6 13:20", "phone", "Davisville Alarm: no answer, attempt 2. Next action d7 09:00", "info", "davisville-alarm"],
      ["d6 12:05", "worker w3", "adopted Distributor North: 1 in doubt, 4 documents read, 0 steps replayed", "info", "dist-north"],
      ["d6 11:40", "ledger", "REJECTED by Atlas: Document failed validation on parties. Reported 120, shipped_in 96", "bad", "malvern-signal"],
      ["d3 09:30", "North orders", "contract failed: field qty_in_stock missing. 4 operations flagged, 20 untouched", "bad", "dist-north"],
    ]);
  } else {
    const simNow = stage === "late" ? 225 : 236;
    const closed = stage === "closed";
    const { parties, edges } = buildParties((d) => {
      if (d.id === "corner-properties") return closed ? { state: "reconciled", fixed: 25, received: 25 } : { state: "reached", installed: 24, fixed: 1, received: 25, next: 233 };
      if (d.id === "queen-st-alarm" && closed) return { state: "reconciled", in_stock: 50, returned: 45 };
      if (d.id === "queen-st-alarm") return { state: "claimed", in_stock: 50, returned: 30 };
      return finalPlan(d);
    });
    if (closed) {
      const e = edges.find((x) => x.to_party === "corner-properties") as Edge;
      e.sender_qty = 25;
      e.recipient_qty = 25;
      e.corroborated_qty = 25;
      e.disputed_qty = 0;
      e.state = "corroborated";
    }
    s.parties = parties;
    s.edges = edges;
    s.recall = { ...RECALL, status: closed ? "closed" : "open", closed_at: closed ? "2026-11-14T19:05:00-05:00" : null };
    s.recalls = [{ _id: "r-1", status: closed ? "closed" : "open", batch_code: "B-4471" }];
    s.workers = workers(
      [
        ["w1", null, null, 0],
        ["w2", null, null, 0],
        ["w3", closed ? null : "corner-properties", closed ? null : "fix_check", 4],
        ["w4", null, null, 0],
      ],
      parties,
    );
    if (!closed) lease(parties.find((p) => p.party_id === "corner-properties") as Party, "w3", 22, 6.2);
    s.systems = systems("ok", 2, "model", 2, "episode");
    const panels = connectedPanels(new Set(D.map((d) => d.id)), new Set(CONNECTED.map((c) => c[0])), new Set(), true);
    s.monitoring.panels = panels;
    linkPanels(parties, panels);
    s.monitoring.events = [panelEvent(57, "D7-4471-0193", "Corner Properties, Tower 2", "pass", "2.5", "d9 09:12", "Corner Properties: 1 panel fixed")];
    s.refusals = closed
      ? []
      : [{ at: "2026-11-14T19:01:00-05:00", sim_label: "d9 09:30", rule: "close gate", message: "REJECTED by Atlas: Document failed validation on recalls. open_units 39", party_id: null }];
    s.run = run(simNow, closed ? 655 : 631, 12, 35, 40);
    s.feed = feed([["d9 09:30", "atlas", "REJECTED by Atlas: Document failed validation on recalls. open_units 39", "bad"]]);
  }
  return recompute(s);
}

/** The simulated panel page fixture for D7-4471-0193 at Corner Properties, Tower 2. */
export function panelFixture(sw: string, lastResult: "pass" | "fail" | null, fixed: boolean): PanelPageState {
  const ev = lastResult ? panelEvent(57, "D7-4471-0193", "Corner Properties, Tower 2", lastResult, sw, "d6 15:40", lastResult === "pass" && fixed ? "Corner Properties: 1 panel fixed" : null) : null;
  return {
    serial: "D7-4471-0193",
    model: "DEMO-7",
    batch_code: "B-4471",
    sw_version: sw,
    site_label: "Corner Properties, Tower 2",
    party_id: "corner-properties",
    party_name: "Corner Properties",
    fault: sw !== "2.5",
    recall: { _id: "r-1", affected_versions: ["2.3", "2.4"], fixed_version: "2.5", status: "open" },
    last_test: ev ? { event_id: ev.event_id, result: ev.result, detail: ev.result === "fail" ? "sounder circuit" : "sounder ok", at: ev.at, sim_label: ev.sim_label } : null,
    events: ev ? [ev] : [],
    fixed,
  };
}

export { panelEvent };
