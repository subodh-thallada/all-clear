// All Clear — the screen contract.
//
// GET /wall/state           -> WallState      (polled once a second by the 3D screen)
// GET /panel-api/{serial}   -> PanelPageState (polled by the simulated panel page)
// POST /panel-api/{serial}/test    -> PanelActionResult   (Run test: posts a panel event through the same path as a real panel)
// POST /panel-api/{serial}/update  -> PanelActionResult   (Apply update: sets the panel's software to the fixed version)
// POST /admin/...           -> operator actions (header X-Admin-Token: <ADMIN_TOKEN>), see OperatorAction
//
// The Python API (levela/api/wall.py, levela/api/panel.py) must produce exactly these shapes.
// Datetimes are ISO-8601 strings. Simulated times are hours since the run began (sim_now) plus a
// label like "d6 14:00". Counts are integers. Nothing here is optional unless marked `?` or `| null`.

export type PartyType = "distributor" | "installer" | "owner";
export type PartyState = "pending" | "contacting" | "reached" | "claimed" | "reconciled";
export type Channel = "voice" | "sim" | "form" | "order_system";
export type FoundVia = "shipment" | "order_system" | "claim" | "memory";
export type EdgeState = "awaiting_recipient" | "corroborated" | "disputed";
export type Level = "good" | "info" | "warn" | "bad";

/** The six buckets every party's panels fall into. shipped_in = sum of these. */
export interface Buckets {
  in_stock: number;
  installed: number; // installed at a building and not yet confirmed fixed
  fixed: number;
  returned: number;
  forwarded: number; // passed on / installed at another party (lives on the edge)
  unresolved: number;
}

export interface Recall {
  _id: string; // "r-1"
  batch_code: string; // "B-4471"
  model: string; // "DEMO-7"
  product: string; // "DEMO-7 fire alarm control panel"
  supplier: string; // invented supplier name
  hazard: string;
  affected_versions: string[]; // ["2.3", "2.4"]
  fixed_version: string; // "2.5"
  units_total: number; // 2400
  status: "open" | "closed";
  source: "signal" | "manual";
  source_signal: string | null;
  mode: "demo" | "sim";
  opened_at: string;
  closed_at: string | null;
}

export interface Metrics {
  units_total: number;
  /** [done, total] parties in claimed or reconciled */
  checks_completed: [number, number];
  /** [n, total] panels in stock, installed, fixed or returned */
  panels_located: [number, number];
  /** [n, total] panels fixed or returned */
  fixed_or_returned: [number, number];
  open_units: number; // what blocks All Clear
  not_located: number; // unresolved + awaiting recipient
  awaiting_fix: number; // installed, not fixed
  disputed_units: number;
  mass_balance: number; // must equal units_total
  status: "open" | "closed";
}

export interface LiveAttempt {
  attempt_id: string;
  state: "intent" | "dialed" | "connected";
  real: boolean; // a real phone call (ElevenLabs/Twilio) vs simulated
  since_s: number;
}

export interface Lease {
  owner: string; // worker id, e.g. "w2"
  token: number;
  remaining_s: number; // can be negative while expired and not yet adopted
  expired: boolean;
}

export interface Party extends Buckets {
  party_id: string;
  name: string;
  type: PartyType;
  tier: 1 | 2 | 3;
  channel: Channel;
  simulated: boolean; // shown as SIMULATED beside the name
  state: PartyState;
  shipped_in: number;
  fixed_by_panel: number;
  connected_unfixed: number;
  connected_panels: number; // connected panels linked to this party
  checks: { model_seen: string | null; version_seen: string | null; sounder_test: boolean | null } | null;
  found_via: FoundVia;
  ladder_step: number;
  next_action_at: number | null; // sim hours
  next_label: string | null; // "d7 09:00"
  hold_for_demo: boolean;
  slice: boolean; // part of the 4-party live slice
  live_attempt: LiveAttempt | null;
  attempts: number;
  lease: Lease | null;
  in_flight: string | null; // attempt or operation id being resolved
  priority: number;
  order_system: "north" | "east" | null; // distributors only
  sites: string[]; // building labels (owners) — used to draw buildings
  escalation: string | null;
  form_url: string | null;
}

export interface Edge {
  from_party: string;
  to_party: string;
  sender_qty: number;
  recipient_qty: number | null;
  corroborated_qty: number;
  disputed_qty: number;
  state: EdgeState;
}

export interface FeedLine {
  id: string; // stable id for animation keys
  at: string;
  sim_label: string; // "d6 14:00"
  system: string; // phone | ledger | atlas | gate | worker w3 | supervisor | North orders | East orders | panel | signal | invariants | memory | planner
  text: string; // raw text; Atlas refusals are verbatim ("REJECTED by Atlas: Document failed validation ...")
  level: Level;
  party_id: string | null;
  kind: string;
}

export interface WorkerView {
  id: string; // "w1".."w4" (and "w5 (saboteur)")
  alive: boolean;
  saboteur: boolean;
  party_id: string | null; // party it holds the lease on
  party_name: string | null;
  kind: string | null; // task kind, e.g. contact_party
  since_s: number | null;
  lease_token: number | null;
  restarts: number;
}

export interface CallInFlight {
  attempt_id: string;
  party_id: string;
  party_name: string;
  real: boolean;
  state: "intent" | "dialed" | "connected";
  since_s: number;
}

export interface SystemView {
  system: "phone" | "north" | "east" | "panels";
  label: string; // "phone" | "North orders" | "East orders" | "panels"
  status: "ok" | "contract_failed" | "repairing" | "paused";
  detail: string; // "58 attempts, 29 accepted claims" | "adapter v2 (repaired by model)  24 operations verified"
  adapter_version?: number;
  adapter_source?: "seed" | "model" | "rules" | "episode";
  operations?: { intent: number; sent: number; verified: number; needs_reverify: number; failed: number };
}

export interface ConnectedPanel {
  serial: string; // "D7-4471-0193"
  site_label: string; // "Corner Properties, Tower 2"
  party_id: string | null; // linked owner, null until linked
  sw_version: string;
  fault: boolean;
  last_result: "pass" | "fail" | null;
  last_at: string | null;
  fixed: boolean; // fixed_event set
}

export interface PanelEventView {
  event_id: string; // "pe-0012"
  serial: string;
  test_id: string;
  kind: "monthly_test" | "fault";
  result: "pass" | "fail";
  sw_version: string;
  site_label: string;
  at: string;
  sim_label: string;
  effect: string | null; // "Corner Properties: 1 panel fixed"
}

export interface SignalView {
  _id: string; // "sig-01"
  batch_code: string;
  rule: string;
  events: string[];
  sites: string[];
  shipped_to: string[]; // party names
  status: "proposed" | "approved" | "dismissed";
  approved_by: string | null;
  recall_id: string | null;
}

export interface Monitoring {
  connected: number; // 24
  failing: number;
  passed_after_fix: number;
  last_event: PanelEventView | null;
  events: PanelEventView[]; // newest first, up to 20
  panels: ConnectedPanel[];
  signals: SignalView[];
}

export interface Invariants {
  at_label: string | null;
  passed: boolean[]; // 4 checks
  names: string[]; // the 4 check names
  ok: boolean;
}

export interface RunView {
  sim_now: number;
  sim_label: string; // "d6 14:00"
  day: number;
  days_total: number;
  real_elapsed_s: number;
  seconds_per_sim_hour: number;
  paused: boolean;
  parties: { total: number; at_start: number; found: number };
  steps: number;
  kills: number;
  adoptions: number;
  duplicates: number;
  model_calls: number;
  tokens_per_decision: number;
  invariants: Invariants;
  supervisor: { paused: boolean };
  saboteur: { on: boolean };
}

export interface WallState {
  now: string;
  recall: Recall | null;
  recalls: { _id: string; status: "open" | "closed"; batch_code: string }[];
  metrics: Metrics | null;
  /** "NOT ALL CLEAR: 39 panels open (15 disputed, 24 awaiting fix)" or "ALL CLEAR" — the server's words */
  headline: string;
  parties: Party[];
  edges: Edge[];
  feed: FeedLine[]; // newest first, up to 80
  workers: WorkerView[];
  calls: { total: number; real: number; simulated: number };
  calls_in_flight: CallInFlight[];
  run: RunView | null;
  systems: SystemView[];
  monitoring: Monitoring;
  memory_ablation: { with_memory: number; without_memory: number } | null;
  /** Atlas refusals seen this run, newest first (also in feed) */
  refusals: { at: string; sim_label: string; rule: string; message: string; party_id: string | null }[];
  admin: boolean; // true when the request carried a valid admin key
  failpoint: string | null;
}

// ---------------------------------------------------------------------------
// The simulated panel page (/panel/{serial})

export interface PanelPageState {
  serial: string;
  model: string;
  batch_code: string;
  sw_version: string;
  site_label: string;
  party_id: string | null;
  party_name: string | null;
  fault: boolean;
  recall: { _id: string; affected_versions: string[]; fixed_version: string; status: "open" | "closed" } | null;
  last_test: { event_id: string; result: "pass" | "fail"; detail: string; at: string; sim_label: string } | null;
  events: PanelEventView[]; // newest first, up to 10
  fixed: boolean;
}

export interface PanelActionResult {
  ok: boolean;
  event?: PanelEventView;
  effect?: string | null; // e.g. "Corner Properties: 1 panel moved installed → fixed (event pe-0031)"
  refused?: { rule: string; message: string } | null; // raw Atlas message when a write was refused
  state: PanelPageState;
}

// ---------------------------------------------------------------------------
// Operator actions (POST, header X-Admin-Token). All return { ok: boolean, message: string, refused?: {...} }

export type OperatorAction =
  | { path: "/admin/release"; body: { party_id: string } } // Release Harbour Fire
  | { path: "/admin/failpoint"; body: { name: "after_intent" | "after_dial_request" | "after_dialed" | null } } // Arm failpoint (kill)
  | { path: "/admin/order-system"; body: { system: "north" | "east"; version?: 2; fault?: string | null } } // Break: switch North to v2
  | { path: "/admin/saboteur"; body: { on: boolean; seconds?: number } }
  | { path: "/admin/close"; body: { recall_id: string } } // Close recall -> Atlas refuses while open_units > 0
  | { path: "/admin/signals/approve"; body: { signal_id: string; approved_by: string } }
  | { path: "/admin/lie"; body: { party_id?: string } } // submits an over-report through the form/claim path; Atlas refuses
  | { path: "/admin/kill"; body: { worker?: string } } // kill one worker now (SIGKILL); supervisor restarts it
  | { path: "/admin/supervisor"; body: { paused: boolean } }
  | { path: "/admin/clock"; body: { seconds_per_sim_hour?: number; paused?: boolean } };

export interface OperatorResult {
  ok: boolean;
  message: string;
  refused?: { rule: string; message: string } | null;
}
