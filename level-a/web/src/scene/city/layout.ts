// The recall drawn as a city. Pure layout: WallState in, plain instance lists out. No positions come
// from the server; every placement follows from tier, type, the transfer graph and the order in
// which parties appear, and a placement, once made, is kept for the session (CityMemory) so a new
// party never pushes an existing one around.
//
// Every panel of the batch becomes exactly one instance, which is the mass balance drawn:
//   units_total = Σ parties (in_stock + installed + fixed + returned + unresolved)
//               + Σ edges (sender_qty − corroborated_qty)
// in_stock -> grey crates at the party, installed / fixed -> amber / green cabinets on the floors of
// the party's buildings, returned -> blue crates in the plant's returns yard, unresolved -> red ghost
// cloud over the party, an edge's open quantity -> red cabinets (disputed) or pale ghosts (awaiting
// the recipient) hovering on the arc between the two parties.

import type { ConnectedPanel, Edge, Party, SignalView, WallState } from "../../api/types";
import { COLORS } from "../theme";

export type V3 = [number, number, number];

export const GRID = { cols: 10, rows: 8, lot: 9, pitch: 12 } as const;
const MID_C = (GRID.cols - 1) / 2;
const MID_R = (GRID.rows - 1) / 2;
export const cellX = (c: number) => (c - MID_C) * GRID.pitch;
export const cellZ = (r: number) => (r - MID_R) * GRID.pitch;

interface Block {
  c0: number;
  r0: number;
  c1: number;
  r1: number;
}
const BLOCKS = {
  plant: { c0: 0, r0: 3, c1: 1, r1: 4 },
  dispatch: { c0: 4, r0: 3, c1: 5, r1: 4 },
  north: { c0: 4, r0: 0, c1: 5, r1: 0 },
  east: { c0: 8, r0: 3, c1: 9, r1: 3 },
} satisfies Record<string, Block>;

/** Lots the supplier shipped to directly (the live slice) sit in front of the dispatch. */
const TIER1_SLOTS: [number, number][] = [
  [4, 6],
  [6, 6],
  [2, 6],
  [8, 6],
  [1, 7],
  [3, 7],
];

function blockRect(b: Block) {
  const x0 = cellX(b.c0) - GRID.lot / 2;
  const x1 = cellX(b.c1) + GRID.lot / 2;
  const z0 = cellZ(b.r0) - GRID.lot / 2;
  const z1 = cellZ(b.r1) + GRID.lot / 2;
  return { x: (x0 + x1) / 2, z: (z0 + z1) / 2, w: x1 - x0, d: z1 - z0, cc: (b.c0 + b.c1) / 2, cr: (b.r0 + b.r1) / 2 };
}
export const PLANT = blockRect(BLOCKS.plant);
export const DISPATCH = blockRect(BLOCKS.dispatch);

const inBlock = (b: Block, c: number, r: number) => c >= b.c0 && c <= b.c1 && r >= b.r0 && r <= b.r1;
const reserved = (c: number, r: number) => Object.values(BLOCKS).some((b) => inBlock(b, c, r));
const cellKey = (c: number, r: number) => (r + 10) * 100 + (c + 10);
const keyCell = (k: number): [number, number] => [(k % 100) - 10, Math.floor(k / 100) - 10];

/** Placements that survive state updates for the life of the scene. */
export class CityMemory {
  cells = new Map<string, number>();
}

export function hash01(s: string | number): number {
  let h = 2166136261;
  const str = String(s);
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 1000003) / 1000003;
}

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// ---------------------------------------------------------------------------------------------
// Output types

export interface BoxInst {
  x: number;
  y: number; // centre
  z: number;
  sx: number;
  sy: number;
  sz: number;
  ry: number;
  color: string;
  rise: string;
}

export type PanelKind = "stock" | "installed" | "fixed" | "returned" | "disputed" | "connected_fail" | "connected_ok" | "connected_fixed";

export interface PanelInst {
  key: string;
  x: number;
  y: number;
  z: number;
  sx: number;
  sy: number;
  sz: number;
  color: string;
  glow: number;
  fx: number; // 0 none, 1 pulse, 2 bob
  rise: string;
}

export interface TreeInst {
  x: number;
  z: number;
  s: number;
  rise: string;
}

export interface BuildingPlace {
  x: number;
  z: number;
  w: number;
  d: number;
  roofY: number;
  site: string | null;
  slot0: V3 | null; // first panel slot (connected panels sit there)
}

export type PartyTone = "unlocated" | "disputed" | "awaiting" | "done" | "working";

export interface PartyPlace {
  id: string;
  party: Party;
  x: number;
  z: number;
  w: number;
  d: number;
  top: number;
  label: V3;
  ring: number;
  rise: string;
  buildings: BuildingPlace[];
  ghostTop: V3 | null;
  terminal: V3 | null;
  tone: PartyTone;
  open: number;
  disputedIn: number;
  disputedOut: number;
  transitIn: number;
}

export interface EdgePlace {
  key: string;
  edge: Edge;
  from: string;
  to: string;
  p0: V3;
  p1: V3;
  p2: V3;
  mid: V3;
  disputed: number;
  transit: number;
}

export interface BeaconPlace {
  serial: string;
  panel: ConnectedPanel;
  x: number;
  y: number;
  z: number;
  linked: boolean;
}

export interface ShipmentArc {
  key: string;
  to: string;
  p0: V3;
  p1: V3;
  p2: V3;
}

export interface CityLayout {
  places: Map<string, PartyPlace>;
  order: PartyPlace[];
  edges: EdgePlace[];
  shipments: ShipmentArc[];
  beacons: BeaconPlace[];
  solid: BoxInst[];
  glass: BoxInst[];
  light: BoxInst[];
  facade: BoxInst[];
  plates: BoxInst[];
  panels: PanelInst[];
  ghosts: PanelInst[];
  trees: TreeInst[];
  plant: { x: number; z: number; plate: V3; label: V3; yard: V3 };
  dispatch: { towers: V3[]; service: V3; hub: V3; headline: V3 };
  bounds: { x0: number; x1: number; z0: number; z1: number; top: number };
  counts: { drawn: number; expected: number; unitsTotal: number };
  riseKeys: Map<string, [number, number]>; // rise key -> lot centre (for the intro stagger)
  /** Changes only when shadow-casting geometry changes (the shadow map is re-rendered then). */
  shape: number;
}

// ---------------------------------------------------------------------------------------------
// Builder helpers

const PANEL = { w: 0.3, h: 0.38, d: 0.09 } as const;
const CRATE = 0.36;
const GHOST = 0.28;

const STEEL_TINTS = ["#5d6876", "#6a6058", "#506571", "#625b6d", "#58665c", "#6d6352"];

class Builder {
  plates: BoxInst[] = [];
  solid: BoxInst[] = [];
  glass: BoxInst[] = [];
  light: BoxInst[] = [];
  facade: BoxInst[] = [];
  panels: PanelInst[] = [];
  ghosts: PanelInst[] = [];
  trees: TreeInst[] = [];

  box(list: BoxInst[], x: number, y0: number, z: number, sx: number, sy: number, sz: number, color: string, rise: string, ry = 0) {
    list.push({ x, y: y0 + sy / 2, z, sx, sy, sz, ry, color, rise });
  }

  panel(key: string, x: number, y: number, z: number, kind: PanelKind, rise: string, fx = 0) {
    const s = PANEL_STYLE[kind];
    this.panels.push({ key, x, y, z, sx: s.size[0], sy: s.size[1], sz: s.size[2], color: s.color, glow: s.glow, fx, rise });
  }

  ghost(key: string, x: number, y: number, z: number, color: string, rise: string) {
    this.ghosts.push({ key, x, y, z, sx: GHOST, sy: GHOST, sz: GHOST, color, glow: 0, fx: 2, rise });
  }
}

const PANEL_STYLE: Record<PanelKind, { color: string; glow: number; size: V3 }> = {
  stock: { color: COLORS.inStock, glow: 0, size: [CRATE, CRATE * 0.8, CRATE] },
  returned: { color: COLORS.returned, glow: 0.35, size: [CRATE, CRATE * 0.8, CRATE] },
  installed: { color: COLORS.installed, glow: 1.25, size: [PANEL.w, PANEL.h, PANEL.d] },
  fixed: { color: COLORS.fixed, glow: 0.95, size: [PANEL.w, PANEL.h, PANEL.d] },
  disputed: { color: COLORS.disputed, glow: 1.6, size: [PANEL.w, PANEL.h, PANEL.w] },
  connected_fail: { color: COLORS.bad, glow: 2.2, size: [PANEL.w * 1.2, PANEL.h * 1.2, PANEL.d * 1.4] },
  connected_ok: { color: COLORS.installed, glow: 1.8, size: [PANEL.w * 1.2, PANEL.h * 1.2, PANEL.d * 1.4] },
  connected_fixed: { color: COLORS.fixed, glow: 1.8, size: [PANEL.w * 1.2, PANEL.h * 1.2, PANEL.d * 1.4] },
};

/** Stacks of crates on pallets, filled stack by stack so a new crate never moves an old one. */
function crateYard(
  b: Builder,
  keyBase: string,
  n: number,
  kind: PanelKind,
  rect: { x0: number; z0: number; x1: number; z1: number },
  perStack: number,
  rise: string,
) {
  if (n <= 0) return;
  const pitch = CRATE * 1.32;
  const cols = Math.max(1, Math.floor((rect.x1 - rect.x0) / pitch));
  const rows = Math.max(1, Math.floor((rect.z1 - rect.z0) / pitch));
  const cap = cols * rows;
  const stacks = Math.ceil(n / perStack);
  const height = stacks > cap ? Math.ceil(n / cap) : perStack;
  const h = CRATE * 0.8;
  for (let i = 0; i < n; i++) {
    const s = Math.floor(i / height);
    const level = i % height;
    const col = s % cols;
    const row = Math.floor(s / cols) % rows;
    const x = rect.x0 + pitch * (col + 0.5);
    const z = rect.z1 - pitch * (row + 0.5);
    if (level === 0) b.box(b.solid, x, 0.0, z, CRATE * 1.15, 0.07, CRATE * 1.15, "#4a3b2c", rise);
    b.panel(`${keyBase}:${i}`, x, 0.07 + h / 2 + level * (h + 0.02), z, kind, rise);
  }
}

/** A loose block of ghosts: the panels nobody has located yet. Returns the top centre. */
function ghostCloud(b: Builder, keyBase: string, n: number, cx: number, y0: number, cz: number, color: string, rise: string): V3 {
  const pitch = GHOST * 1.55;
  const side = Math.max(2, Math.min(15, Math.ceil(Math.sqrt(n / 3))));
  const perLayer = side * side;
  const layers = Math.ceil(n / perLayer);
  for (let i = 0; i < n; i++) {
    const layer = Math.floor(i / perLayer);
    const within = i % perLayer;
    const col = within % side;
    const row = Math.floor(within / side);
    const jitter = (hash01(`${keyBase}${i}`) - 0.5) * 0.12;
    b.ghost(
      `${keyBase}:${i}`,
      cx + (col - (side - 1) / 2) * pitch + jitter,
      y0 + layer * pitch,
      cz + (row - (side - 1) / 2) * pitch - jitter,
      color,
      rise,
    );
  }
  return [cx, y0 + layers * pitch + 0.2, cz];
}

interface Slot {
  key: string;
  kind: PanelKind;
}

/** A glass building cut open at the front, panels on the back wall of every floor. */
function glassBuilding(
  b: Builder,
  o: { x: number; z: number; w: number; d: number; slots: Slot[]; minFloors: number; tint: string; rise: string; site: string | null },
): BuildingPlace {
  const n = o.slots.length;
  const cols = Math.max(2, Math.floor((o.w - 0.5) / 0.42));
  const maxFloors = 16;
  const rowsPerFloor = Math.max(1, Math.ceil(n / (cols * maxFloors)));
  const floors = Math.min(maxFloors, Math.max(o.minFloors, Math.ceil(n / (cols * rowsPerFloor))));
  const fh = 0.62 + 0.46 * rowsPerFloor;
  const plinth = 0.18;
  const h = floors * fh;
  const { x, z, w, d, rise } = o;
  const back = z - d / 2;

  b.box(b.solid, x, 0, z, w + 0.3, plinth, d + 0.3, "#1d232c", rise);
  b.box(b.solid, x, plinth, back + 0.05, w, h, 0.1, "#141920", rise);
  for (let i = 0; i <= floors; i++) {
    b.box(b.solid, x, plinth + i * fh - (i === 0 ? 0 : 0.1), z, w, 0.1, d, i === floors ? "#2b323c" : "#38404b", rise);
    if (i > 0) b.box(b.light, x, plinth + i * fh - 0.15, z + d / 2 - 0.18, w - 0.3, 0.035, 0.05, "#ffd9a6", rise);
  }
  const cw = 0.13;
  for (const [sx, sz] of [
    [-1, -1],
    [1, -1],
    [-1, 1],
    [1, 1],
  ] as const) {
    b.box(b.solid, x + sx * (w / 2 - cw / 2), plinth, z + sz * (d / 2 - cw / 2), cw, h, cw, o.tint, rise);
  }
  b.box(b.glass, x - w / 2 + 0.02, plinth, z, 0.04, h - 0.1, d - 0.2, "#9cc4ff", rise);
  b.box(b.glass, x + w / 2 - 0.02, plinth, z, 0.04, h - 0.1, d - 0.2, "#9cc4ff", rise);
  b.box(b.glass, x, plinth, z + d / 2 - 0.03, w - 0.2, h - 0.1, 0.03, "#b5d3ff", rise);
  // Rooftop: parapet and a plant room.
  const roof = plinth + h;
  b.box(b.solid, x - w * 0.18, roof, z - d * 0.12, w * 0.42, 0.42, d * 0.42, "#2a3039", rise);
  b.box(b.light, x - w * 0.18, roof + 0.42, z - d * 0.12 + d * 0.21 + 0.005, w * 0.3, 0.03, 0.02, "#ff5a47", rise);

  const span = w - 0.5;
  const pitch = span / cols;
  let slot0: V3 | null = null;
  for (let i = 0; i < n; i++) {
    const floor = Math.floor(i / (cols * rowsPerFloor));
    const within = i % (cols * rowsPerFloor);
    const row = Math.floor(within / cols);
    const col = within % cols;
    const px = x - span / 2 + pitch * (col + 0.5);
    const py = plinth + floor * fh + 0.36 + row * 0.46 + PANEL.h / 2;
    const pz = back + 0.17;
    if (i === 0) slot0 = [px, py, pz];
    const s = o.slots[i];
    b.panel(s.key, px, py, pz, s.kind, rise);
  }
  return { x, z, w, d, roofY: roof + 0.1, site: o.site, slot0 };
}

function partyTint(id: string) {
  return STEEL_TINTS[Math.floor(hash01(id) * STEEL_TINTS.length)];
}

const PARTY_HUES = ["#ff7b54", "#5ad8cc", "#6cc4ff", "#c3a0ff", "#f5b841", "#7fd46b", "#ff8fb1"];
export function partyHue(id: string) {
  return PARTY_HUES[Math.floor(hash01(`hue${id}`) * PARTY_HUES.length)];
}

// ---------------------------------------------------------------------------------------------
// Placement

interface Placement {
  x: number;
  z: number;
  w: number;
  d: number;
  cc: number; // cell-space centre for distance ranking
  cr: number;
  block: "north" | "east" | null;
}

function assignPlacements(parties: Party[], edges: Edge[], memory: CityMemory): Map<string, Placement> {
  const out = new Map<string, Placement>();
  const ids = new Set(parties.map((p) => p.party_id));
  for (const id of [...memory.cells.keys()]) if (!ids.has(id)) memory.cells.delete(id);

  const taken = new Set<number>(memory.cells.values());
  const blockOwner = new Map<string, string>();

  const parentOf = new Map<string, string>();
  for (const e of edges) if (!parentOf.has(e.to_party)) parentOf.set(e.to_party, e.from_party);

  const order = [...parties].sort((a, b) => a.tier - b.tier);
  let tier1 = 0;

  const fromCell = (k: number): Placement => {
    const [c, r] = keyCell(k);
    return { x: cellX(c), z: cellZ(r), w: GRID.lot, d: GRID.lot, cc: c, cr: r, block: null };
  };
  const nearestFree = (tc: number, tr: number): number => {
    let best = -1;
    let bestScore = Infinity;
    for (let r = -2; r < GRID.rows + 2; r++) {
      for (let c = -1; c < GRID.cols + 1; c++) {
        const inside = c >= 0 && c < GRID.cols && r >= 0 && r < GRID.rows;
        if (inside && reserved(c, r)) continue;
        const k = cellKey(c, r);
        if (taken.has(k)) continue;
        const d2 = (c - tc) ** 2 + (r - tr) ** 2;
        const outward = (c - MID_C) ** 2 + (r - MID_R) ** 2;
        const score = d2 * 1000 - outward + (inside ? 0 : 60000);
        if (score < bestScore) {
          bestScore = score;
          best = k;
        }
      }
    }
    return best;
  };

  for (const p of order) {
    if (p.type === "distributor" && p.order_system && !blockOwner.has(p.order_system)) {
      blockOwner.set(p.order_system, p.party_id);
      const r = blockRect(BLOCKS[p.order_system]);
      out.set(p.party_id, { ...r, block: p.order_system });
      continue;
    }
    const known = memory.cells.get(p.party_id);
    if (known !== undefined) {
      out.set(p.party_id, fromCell(known));
      continue;
    }
    let k = -1;
    if (p.tier === 1) {
      while (tier1 < TIER1_SLOTS.length && k < 0) {
        const [c, r] = TIER1_SLOTS[tier1++];
        if (!taken.has(cellKey(c, r))) k = cellKey(c, r);
      }
      if (k < 0) k = nearestFree(MID_C, GRID.rows - 1.5);
    } else {
      const parent = parentOf.get(p.party_id);
      const pp = parent ? out.get(parent) : undefined;
      k = pp ? nearestFree(pp.cc, pp.cr) : nearestFree(MID_C, MID_R + 2);
    }
    memory.cells.set(p.party_id, k);
    taken.add(k);
    out.set(p.party_id, fromCell(k));
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// Per-type lots

function siteIndexFor(label: string, sites: string[], partyName: string): number {
  const nl = norm(label);
  const exact = sites.findIndex((s) => norm(s) === nl);
  if (exact >= 0) return exact;
  const partial = sites.findIndex((s) => nl.includes(norm(s)) || norm(s).includes(nl));
  if (partial >= 0) return partial;
  const np = norm(partyName);
  const rest = nl.startsWith(np) ? nl.slice(np.length).trim() : nl;
  const loose = sites.findIndex((s) => rest.length > 0 && norm(s).endsWith(rest));
  return loose >= 0 ? loose : 0;
}

/** Slots for the panels inside a party's buildings: connected panels first in their own building
 *  (coloured by their own state), then the rest, fixed before installed, dealt round-robin. */
function buildingSlots(p: Party, k: number, sites: string[], connected: ConnectedPanel[]): Slot[][] {
  const per: Slot[][] = Array.from({ length: k }, () => []);
  let fixedLeft = p.fixed;
  let installedLeft = p.installed;
  const linked = [...connected].sort((a, b) => a.serial.localeCompare(b.serial));
  for (const cp of linked) {
    const bi = siteIndexFor(cp.site_label, sites, p.name) % k;
    let kind: PanelKind | null = null;
    if (cp.fixed && fixedLeft > 0) {
      kind = "connected_fixed";
      fixedLeft--;
    } else if (!cp.fixed && installedLeft > 0) {
      kind = cp.last_result === "fail" ? "connected_fail" : "connected_ok";
      installedLeft--;
    }
    if (kind) per[bi].push({ key: `${p.party_id}:cp:${cp.serial}`, kind });
  }
  const total = fixedLeft + installedLeft;
  for (let g = 0; g < total; g++) {
    per[g % k].push({ key: `${p.party_id}:b:${g}`, kind: g < fixedLeft ? "fixed" : "installed" });
  }
  return per;
}

function ownerLot(b: Builder, pl: Placement, p: Party, connected: ConnectedPanel[], rise: string): BuildingPlace[] {
  const sites = p.sites.length > 0 ? p.sites.slice(0, 4) : [p.name];
  const k = sites.length;
  const per = buildingSlots(p, k, sites, connected);
  const tint = partyTint(p.party_id);
  const out: BuildingPlace[] = [];
  const gap = 0.6;
  if (k <= 3) {
    const w = Math.min(4.4, (pl.w - 1.0 - (k - 1) * gap) / k);
    for (let i = 0; i < k; i++) {
      const x = pl.x - ((k - 1) * (w + gap)) / 2 + i * (w + gap);
      out.push(glassBuilding(b, { x, z: pl.z - 1.6, w, d: 3.8, slots: per[i], minFloors: 3, tint, rise, site: sites[i] }));
    }
  } else {
    const w = 3.6;
    for (let i = 0; i < k; i++) {
      const x = pl.x + (i % 2 === 0 ? -2.1 : 2.1);
      const z = pl.z + (i < 2 ? -2.2 : 1.9);
      out.push(glassBuilding(b, { x, z, w, d: 3.0, slots: per[i], minFloors: 3, tint, rise, site: sites[i] }));
    }
  }
  crateYard(b, `${p.party_id}:s`, p.in_stock, "stock", { x0: pl.x - 4.2, z0: pl.z + 1.4, x1: pl.x - 0.4, z1: pl.z + 4.2 }, 3, rise);
  // Landscaping in front of the towers.
  for (let i = 0; i < 3; i++) b.trees.push({ x: pl.x + 1.4 + i * 1.3, z: pl.z + 3.3 - (i % 2) * 0.6, s: 0.8 + hash01(`${p.party_id}t${i}`) * 0.4, rise });
  return out;
}

function installerLot(b: Builder, pl: Placement, p: Party, connected: ConnectedPanel[], rise: string): BuildingPlace[] {
  const hue = partyHue(p.party_id);
  const tint = partyTint(p.party_id);
  const dx = pl.x - 2.0;
  const dz = pl.z - 2.5;
  // Depot: a garage with a roll-up door and the company stripe.
  b.box(b.solid, dx, 0, dz, 4.6, 0.12, 3.4, "#20262e", rise);
  b.box(b.solid, dx, 0.12, dz, 4.4, 2.2, 3.2, "#3b4350", rise);
  b.box(b.solid, dx, 2.32, dz, 4.6, 0.16, 3.4, "#2a3038", rise);
  b.box(b.light, dx, 1.9, dz + 1.61, 4.2, 0.12, 0.02, hue, rise);
  b.box(b.light, dx - 0.9, 0.12, dz + 1.61, 1.8, 1.45, 0.02, "#ffcf8a", rise);
  b.box(b.solid, dx + 1.3, 0.12, dz + 1.61, 1.2, 1.45, 0.03, "#59626f", rise);
  // Two vans out front.
  for (let i = 0; i < 2; i++) {
    const vx = pl.x - 3.6 + i * 1.35;
    const vz = pl.z + 1.2;
    b.box(b.solid, vx, 0.12, vz - 0.1, 0.8, 0.85, 1.55, "#d6dbe2", rise);
    b.box(b.solid, vx, 0.12, vz + 0.88, 0.8, 0.62, 0.5, "#c9ced6", rise);
    b.box(b.solid, vx, 0.48, vz + 1.1, 0.72, 0.24, 0.06, "#0d1218", rise);
    b.box(b.light, vx + 0.41, 0.5, vz - 0.1, 0.01, 0.12, 1.4, hue, rise);
    b.box(b.solid, vx, 0.0, vz + 0.4, 0.82, 0.14, 2.0, "#111418", rise);
  }
  const out: BuildingPlace[] = [];
  const n = p.installed + p.fixed;
  if (n > 0 || connected.length > 0) {
    const per = buildingSlots(p, 1, [p.name], connected);
    out.push(glassBuilding(b, { x: pl.x + 2.5, z: pl.z - 2.3, w: 3.0, d: 3.2, slots: per[0], minFloors: 2, tint, rise, site: null }));
  }
  crateYard(b, `${p.party_id}:s`, p.in_stock, "stock", { x0: pl.x - 1.0, z0: pl.z + 0.2, x1: pl.x + 4.2, z1: pl.z + 4.2 }, 4, rise);
  return out;
}

function distributorLot(b: Builder, pl: Placement, p: Party, connected: ConnectedPanel[], rise: string): { buildings: BuildingPlace[]; terminal: V3 } {
  const wx = pl.x - 3.2;
  const wz = pl.z - 1.9;
  const ww = 13;
  const wd = 4.6;
  b.box(b.solid, wx, 0, wz, ww + 0.4, 0.14, wd + 0.4, "#20262e", rise);
  b.box(b.solid, wx, 0.14, wz, ww, 3.4, wd, "#454e5b", rise);
  // Ribbed cladding and a skylit roof.
  for (let i = 0; i < 12; i++) b.box(b.solid, wx - ww / 2 + 0.55 + i * 1.08, 0.14, wz + wd / 2 + 0.02, 0.08, 3.4, 0.05, "#565f6c", rise);
  b.box(b.solid, wx, 3.54, wz, ww + 0.2, 0.2, wd + 0.2, "#2c333c", rise);
  for (let i = 0; i < 5; i++) b.box(b.light, wx - ww / 2 + 1.6 + i * 2.45, 3.74, wz, 1.4, 0.04, wd * 0.6, "#a9c8ff", rise);
  // Loading docks with lamps above.
  for (let i = 0; i < 4; i++) {
    const x = wx - ww / 2 + 1.7 + i * 2.2;
    b.box(b.solid, x, 0.14, wz + wd / 2 + 0.03, 1.5, 1.8, 0.04, "#1a1f26", rise);
    b.box(b.light, x, 2.15, wz + wd / 2 + 0.08, 0.5, 0.08, 0.08, "#ffd9a6", rise);
  }
  // A lorry at the first dock.
  const tx = wx - ww / 2 + 1.7;
  b.box(b.solid, tx, 0.25, wz + wd / 2 + 2.0, 1.3, 1.5, 3.4, "#cfd4db", rise);
  b.box(b.solid, tx, 0.1, wz + wd / 2 + 4.1, 1.25, 1.2, 1.0, partyHue(p.party_id), rise);
  b.box(b.solid, tx, 0, wz + wd / 2 + 2.6, 1.32, 0.25, 4.4, "#111418", rise);

  const buildings: BuildingPlace[] = [];
  if (p.installed + p.fixed > 0 || connected.length > 0) {
    const per = buildingSlots(p, 1, [p.name], connected);
    buildings.push(glassBuilding(b, { x: pl.x + 7.6, z: pl.z - 2.4, w: 3.2, d: 3.0, slots: per[0], minFloors: 2, tint: partyTint(p.party_id), rise, site: null }));
  }
  crateYard(b, `${p.party_id}:s`, p.in_stock, "stock", { x0: pl.x - 1.6, z0: pl.z + 0.9, x1: pl.x + 6.2, z1: pl.z + 4.3 }, 4, rise);

  // The order-system terminal: a kiosk with an angled screen and a status beacon.
  const kx = pl.x + 8.2;
  const kz = pl.z + 2.6;
  b.box(b.solid, kx, 0, kz, 1.6, 0.18, 1.2, "#1d232b", rise);
  b.box(b.solid, kx, 0.18, kz, 0.5, 1.5, 0.5, "#2c343f", rise);
  return { buildings, terminal: [kx, 2.35, kz + 0.05] };
}

// ---------------------------------------------------------------------------------------------
// Fixed landmarks

function plantSite(b: Builder, returned: number): { plate: V3; label: V3; yard: V3 } {
  const { x, z } = PLANT;
  const rise = "plant";
  b.box(b.plates, x, 0, z, PLANT.w - 0.6, 0.12, PLANT.d - 0.6, "#1a1f27", rise);
  // Main hall with a sawtooth roof.
  const hx = x - 3.5;
  const hz = z - 5.0;
  b.box(b.solid, hx, 0.12, hz, 12, 4.4, 8.5, "#4a525e", rise);
  for (let i = 0; i < 6; i++) {
    const sx = hx - 6 + 1 + i * 2;
    b.box(b.solid, sx, 4.4, hz, 2.05, 1.1, 8.5, "#3a414b", rise, 0);
    b.box(b.light, sx + 0.8, 4.55, hz, 0.12, 0.8, 8.2, "#ffd29a", rise);
  }
  for (let i = 0; i < 5; i++) b.box(b.light, hx - 4.8 + i * 2.4, 1.0, hz + 4.27, 1.3, 2.2, 0.02, "#ffcf8a", rise);
  // Office block (lit windows) and stacks.
  b.box(b.facade, x + 5.6, 0.12, z - 6.6, 6.5, 7.5, 5.5, "#2a313b", rise);
  b.box(b.solid, x + 5.6, 7.62, z - 6.6, 6.7, 0.25, 5.7, "#20262e", rise);
  b.box(b.light, x + 5.6, 6.6, z - 3.83, 5.6, 0.32, 0.03, "#ff5a47", rise);
  // Returns yard fence.
  const yard = { x0: x + 1.2, z0: z - 1.5, x1: x + 9.8, z1: z + 9.6 };
  b.box(b.solid, (yard.x0 + yard.x1) / 2, 0.12, yard.z0 - 0.15, yard.x1 - yard.x0, 0.5, 0.06, "#3b4552", rise);
  b.box(b.light, (yard.x0 + yard.x1) / 2, 0.62, yard.z0 - 0.15, yard.x1 - yard.x0, 0.03, 0.03, COLORS.returned, rise);
  crateYard(b, "plant:ret", returned, "returned", yard, 3, rise);
  // The batch plate: where the signal threads converge.
  const px = x - 4.5;
  const pz = z + 5.0;
  b.box(b.solid, px, 0.12, pz, 3.8, 0.3, 1.4, "#232a33", rise);
  b.box(b.solid, px, 0.42, pz, 3.2, 4.2, 0.4, "#1b2028", rise);
  b.box(b.light, px, 4.62, pz, 3.2, 0.06, 0.42, COLORS.bad, rise);
  b.box(b.light, px - 1.62, 0.42, pz, 0.05, 4.2, 0.42, COLORS.bad, rise);
  b.box(b.light, px + 1.62, 0.42, pz, 0.05, 4.2, 0.42, COLORS.bad, rise);
  return { plate: [px, 4.9, pz], label: [x - 3.5, 7.4, z - 5.0], yard: [(yard.x0 + yard.x1) / 2, 0.5, (yard.z0 + yard.z1) / 2] };
}

function dispatchSite(b: Builder): { towers: V3[]; service: V3; hub: V3; headline: V3 } {
  const { x, z } = DISPATCH;
  const rise = "dispatch";
  b.box(b.plates, x, 0, z, DISPATCH.w - 0.6, 0.1, DISPATCH.d - 0.6, "#151a22", rise);
  const towers: V3[] = [
    [x - 4.6, 7.0, z - 4.6],
    [x + 4.6, 7.0, z - 4.6],
    [x + 4.6, 7.0, z + 4.6],
    [x - 4.6, 7.0, z + 4.6],
    [x, 7.0, z + 7.8],
  ];
  return { towers, service: [x + 8.0, 2.6, z - 0.4], hub: [x, 1.2, z], headline: [x, 22, z - 2] };
}

function fillerCell(b: Builder, c: number, r: number) {
  const x = cellX(c);
  const z = cellZ(r);
  const h = hash01(`cell${c},${r}`);
  const rise = `f:${c},${r}`;
  b.box(b.plates, x, 0, z, GRID.lot, 0.06, GRID.lot, "#10151c", rise);
  if (h < 0.64) {
    const n = 1 + Math.floor(hash01(`n${c},${r}`) * 3);
    const spots: [number, number, number, number][] = [
      [-2.2, -2.0, 4.0, 3.8],
      [2.4, -1.6, 3.4, 4.2],
      [0.2, 2.6, 6.4, 2.8],
    ];
    for (let i = 0; i < n; i++) {
      const [ox, oz, w, d] = spots[i];
      const height = 1.8 + hash01(`h${c},${r},${i}`) * (i === 0 ? 8.5 : 4.5);
      const shade = ["#1c232d", "#212833", "#1a2029", "#252b35"][Math.floor(hash01(`s${c},${r},${i}`) * 4)];
      b.box(b.facade, x + ox, 0.06, z + oz, w, height, d, shade, rise);
      b.box(b.solid, x + ox, 0.06 + height, z + oz, w + 0.12, 0.12, d + 0.12, "#151a21", rise);
    }
  } else if (h < 0.84) {
    b.box(b.plates, x, 0.06, z, GRID.lot - 1.2, 0.08, GRID.lot - 1.2, "#13241c", rise);
    for (let i = 0; i < 9; i++) {
      const tx = x + (hash01(`tx${c},${r},${i}`) - 0.5) * 6.8;
      const tz = z + (hash01(`tz${c},${r},${i}`) - 0.5) * 6.8;
      b.trees.push({ x: tx, z: tz, s: 0.8 + hash01(`ts${c},${r},${i}`) * 0.7, rise });
    }
  } else {
    for (let i = 0; i < 6; i++) {
      if (hash01(`car${c},${r},${i}`) < 0.35) continue;
      const cx = x - 3.2 + (i % 3) * 3.2;
      const cz = z + (i < 3 ? -1.6 : 1.6);
      const paint = ["#5c6878", "#7a2f2f", "#d0d4da", "#2e4a6b", "#33373d"][Math.floor(hash01(`cp${c},${r},${i}`) * 5)];
      b.box(b.solid, cx, 0.06, cz, 0.85, 0.45, 1.6, paint, rise);
      b.box(b.solid, cx, 0.51, cz - 0.1, 0.75, 0.3, 0.9, "#14181e", rise);
    }
  }
}

// ---------------------------------------------------------------------------------------------
// Status of a party in one word, for colour and labels

function toneOf(p: Party, disputed: number, transitIn: number): PartyTone {
  if (disputed > 0) return "disputed";
  if (p.unresolved > 0 || transitIn > 0) return "unlocated";
  if (p.installed > 0) return "awaiting";
  if (p.state === "claimed" || p.state === "reconciled") return "done";
  return "working";
}

export const TONE_COLOR: Record<PartyTone, string> = {
  unlocated: COLORS.bad,
  disputed: COLORS.bad,
  awaiting: COLORS.installed,
  done: COLORS.good,
  working: COLORS.live,
};

// ---------------------------------------------------------------------------------------------

function arc(a: V3, c: V3, lift: number): { p1: V3; mid: V3 } {
  const dist = Math.hypot(c[0] - a[0], c[2] - a[2]);
  const p1: V3 = [(a[0] + c[0]) / 2, Math.max(a[1], c[1]) + lift + dist * 0.2, (a[2] + c[2]) / 2];
  const mid: V3 = [0.25 * a[0] + 0.5 * p1[0] + 0.25 * c[0], 0.25 * a[1] + 0.5 * p1[1] + 0.25 * c[1], 0.25 * a[2] + 0.5 * p1[2] + 0.25 * c[2]];
  return { p1, mid };
}

export function buildCity(state: WallState | null, memory: CityMemory): CityLayout {
  const b = new Builder();
  const parties = state?.parties ?? [];
  const edges = state?.edges ?? [];
  const placements = assignPlacements(parties, edges, memory);
  const monitoring = state?.monitoring;

  const connectedByParty = new Map<string, ConnectedPanel[]>();
  for (const cp of monitoring?.panels ?? []) {
    if (!cp.party_id || !placements.has(cp.party_id)) continue;
    const list = connectedByParty.get(cp.party_id) ?? [];
    list.push(cp);
    connectedByParty.set(cp.party_id, list);
  }

  const disputedIn = new Map<string, number>();
  const disputedOut = new Map<string, number>();
  const transitIn = new Map<string, number>();
  for (const e of edges) {
    const open = Math.max(0, e.sender_qty - e.corroborated_qty);
    if (e.state === "disputed") {
      disputedIn.set(e.to_party, (disputedIn.get(e.to_party) ?? 0) + open);
      disputedOut.set(e.from_party, (disputedOut.get(e.from_party) ?? 0) + open);
    } else transitIn.set(e.to_party, (transitIn.get(e.to_party) ?? 0) + open);
  }

  const totalReturned = parties.reduce((s, p) => s + p.returned, 0);
  const plant = plantSite(b, totalReturned);
  const dispatch = dispatchSite(b);

  const places = new Map<string, PartyPlace>();
  const riseKeys = new Map<string, [number, number]>([
    ["plant", [PLANT.x, PLANT.z]],
    ["dispatch", [DISPATCH.x, DISPATCH.z]],
  ]);

  for (const p of parties) {
    const pl = placements.get(p.party_id);
    if (!pl) continue;
    const rise = `p:${p.party_id}`;
    riseKeys.set(rise, [pl.x, pl.z]);
    // Lot plate.
    b.box(b.plates, pl.x, 0, pl.z, pl.w, 0.1, pl.d, "#171d27", rise);
    const connected = connectedByParty.get(p.party_id) ?? [];
    let buildings: BuildingPlace[] = [];
    let terminal: V3 | null = null;
    if (p.type === "distributor") {
      const r = distributorLot(b, pl, p, connected, rise);
      buildings = r.buildings;
      terminal = r.terminal;
    } else if (p.type === "installer") buildings = installerLot(b, pl, p, connected, rise);
    else buildings = ownerLot(b, pl, p, connected, rise);

    const structTop = Math.max(p.type === "distributor" ? 3.8 : 2.5, ...buildings.map((bb) => bb.roofY + 0.5));
    let ghostTop: V3 | null = null;
    if (p.unresolved > 0) ghostTop = ghostCloud(b, `${p.party_id}:u`, p.unresolved, pl.x, structTop + 1.4, pl.z + (p.type === "distributor" ? -1.5 : 0.4), COLORS.unresolved, rise);
    const top = ghostTop ? ghostTop[1] + 0.6 : structTop;
    const dIn = disputedIn.get(p.party_id) ?? 0;
    const dOut = disputedOut.get(p.party_id) ?? 0;
    const tIn = transitIn.get(p.party_id) ?? 0;
    places.set(p.party_id, {
      id: p.party_id,
      party: p,
      x: pl.x,
      z: pl.z,
      w: pl.w,
      d: pl.d,
      top,
      label: [pl.x, top + 0.9, pl.z],
      ring: Math.max(pl.w, pl.d) * 0.5 + 0.6,
      rise,
      buildings,
      ghostTop,
      terminal,
      tone: toneOf(p, dIn + dOut, tIn),
      open: p.unresolved + p.installed + dIn + tIn,
      disputedIn: dIn,
      disputedOut: dOut,
      transitIn: tIn,
    });
  }

  // Free cells become the anonymous city around the recall.
  const taken = new Set(memory.cells.values());
  for (let r = 0; r < GRID.rows; r++) {
    for (let c = 0; c < GRID.cols; c++) {
      if (reserved(c, r) || taken.has(cellKey(c, r))) continue;
      fillerCell(b, c, r);
    }
  }

  // Transfers: an arc per edge; its open quantity hovers on the arc.
  const edgePlaces: EdgePlace[] = [];
  const anchorOf = (id: string): V3 => {
    const pp = places.get(id);
    return pp ? [pp.x, 1.2, pp.z] : [plant.plate[0], 1.2, plant.plate[2]];
  };
  for (const e of edges) {
    const p0 = anchorOf(e.from_party);
    const p2 = anchorOf(e.to_party);
    const { p1, mid } = arc(p0, p2, 2.6);
    const open = Math.max(0, e.sender_qty - e.corroborated_qty);
    const disputed = e.state === "disputed" ? open : 0;
    const transit = open - disputed;
    const key = `${e.from_party}>${e.to_party}`;
    const edgeRise = places.get(e.to_party)?.rise ?? "dispatch";
    if (disputed > 0) {
      const side = Math.max(2, Math.ceil(Math.cbrt(disputed)));
      const pitch = 0.5;
      for (let i = 0; i < disputed; i++) {
        const col = i % side;
        const row = Math.floor(i / side) % side;
        const layer = Math.floor(i / (side * side));
        b.panel(
          `${key}:d:${i}`,
          mid[0] + (col - (side - 1) / 2) * pitch,
          mid[1] + 0.3 + layer * pitch,
          mid[2] + (row - (side - 1) / 2) * pitch,
          "disputed",
          edgeRise,
          1,
        );
      }
    }
    if (transit > 0) ghostCloud(b, `${key}:t`, transit, mid[0], mid[1] + (disputed > 0 ? 2.2 : 0.2), mid[2], COLORS.transit, edgeRise);
    edgePlaces.push({ key, edge: e, from: e.from_party, to: e.to_party, p0, p1, p2, mid, disputed, transit });
  }

  // What the supplier shipped directly: arcs from the batch plate to the tier-1 parties.
  const shipments: ShipmentArc[] = [];
  for (const p of parties) {
    if (p.found_via !== "shipment") continue;
    const pp = places.get(p.party_id);
    if (!pp) continue;
    const p0: V3 = [plant.plate[0], 4.0, plant.plate[2]];
    const p2: V3 = [pp.x, 1.0, pp.z];
    shipments.push({ key: `ship>${p.party_id}`, to: p.party_id, p0, p1: arc(p0, p2, 4).p1, p2 });
  }

  // Connected panels: on the roof of their building when linked, otherwise on an anonymous roof
  // somewhere in the city (the panel reports, but nobody on the recall knows its owner yet).
  const fillerRoofs = b.facade
    .filter((f) => f.rise.startsWith("f:"))
    .map((f) => [f.x, f.y + f.sy / 2 + 0.12, f.z] as V3)
    .sort((a, c) => a[0] - c[0] || a[2] - c[2]);
  const beacons: BeaconPlace[] = [];
  const usedRoofs = new Map<number, number>();
  for (const cp of monitoring?.panels ?? []) {
    const pp = cp.party_id ? places.get(cp.party_id) : undefined;
    if (pp && pp.buildings.length > 0) {
      const bi = siteIndexFor(cp.site_label, pp.buildings.map((bb) => bb.site ?? pp.party.name), pp.party.name) % pp.buildings.length;
      const bb = pp.buildings[bi];
      const n = beacons.filter((x) => x.linked && Math.abs(x.x - bb.x) < bb.w && Math.abs(x.z - bb.z) < bb.d).length;
      beacons.push({ serial: cp.serial, panel: cp, x: bb.x + bb.w / 2 - 0.35 - n * 0.42, y: bb.roofY, z: bb.z + bb.d / 2 - 0.4, linked: true });
    } else if (fillerRoofs.length > 0) {
      const idx = Math.floor(hash01(norm(cp.site_label)) * fillerRoofs.length);
      const n = usedRoofs.get(idx) ?? 0;
      usedRoofs.set(idx, n + 1);
      const roof = fillerRoofs[idx];
      beacons.push({ serial: cp.serial, panel: cp, x: roof[0] - 0.8 + n * 0.5, y: roof[1], z: roof[2] + 0.6, linked: false });
    } else {
      const a = hash01(cp.serial) * Math.PI * 2;
      beacons.push({ serial: cp.serial, panel: cp, x: dispatch.service[0] + Math.cos(a) * 3, y: 0.2, z: dispatch.service[2] + Math.sin(a) * 3, linked: false });
    }
  }

  // Framing bounds: the landmarks plus every lot in use.
  let x0 = PLANT.x - PLANT.w / 2;
  let x1 = DISPATCH.x + DISPATCH.w / 2 + 2;
  let z0 = DISPATCH.z - DISPATCH.d / 2;
  let z1 = DISPATCH.z + DISPATCH.d / 2;
  let top = 12;
  for (const pp of places.values()) {
    x0 = Math.min(x0, pp.x - pp.w / 2);
    x1 = Math.max(x1, pp.x + pp.w / 2);
    z0 = Math.min(z0, pp.z - pp.d / 2);
    z1 = Math.max(z1, pp.z + pp.d / 2);
    top = Math.max(top, pp.top);
  }
  for (const f of b.facade) if (f.rise.startsWith("f:")) riseKeys.set(f.rise, [f.x, f.z]);
  for (const s of b.plates) if (s.rise.startsWith("f:") && !riseKeys.has(s.rise)) riseKeys.set(s.rise, [s.x, s.z]);

  let shape = b.trees.length * 7919;
  for (const list of [b.solid, b.facade]) for (const x of list) shape += x.x * 1.31 + x.y * 7.07 + x.z * 3.71 + x.sy * 11.3 + x.sx * 0.37;

  const expected =
    parties.reduce((s, p) => s + p.in_stock + p.installed + p.fixed + p.returned + p.unresolved, 0) +
    edges.reduce((s, e) => s + Math.max(0, e.sender_qty - e.corroborated_qty), 0);

  return {
    places,
    order: [...places.values()],
    edges: edgePlaces,
    shipments,
    beacons,
    solid: b.solid,
    glass: b.glass,
    light: b.light,
    facade: b.facade,
    plates: b.plates,
    panels: b.panels,
    ghosts: b.ghosts,
    trees: b.trees,
    plant: { x: PLANT.x, z: PLANT.z, ...plant },
    dispatch,
    bounds: { x0, x1, z0, z1, top },
    counts: { drawn: b.panels.length + b.ghosts.length, expected, unitsTotal: state?.recall?.units_total ?? state?.metrics?.units_total ?? expected },
    riseKeys,
    shape: Math.round(shape * 1000) / 1000,
  };
}

/** Where the threads of a signal start: the connected panels at the signal's sites. */
export function signalSources(signal: SignalView, beacons: BeaconPlace[]): V3[] {
  const out: V3[] = [];
  for (const site of signal.sites) {
    const ns = norm(site);
    const hit = beacons.find((bc) => norm(bc.panel.site_label) === ns) ?? beacons.find((bc) => norm(bc.panel.site_label).includes(ns));
    if (hit) out.push([hit.x, hit.y + 1.2, hit.z]);
  }
  return out;
}

export function quadPoint(p0: V3, p1: V3, p2: V3, t: number, out: V3): V3 {
  const a = (1 - t) * (1 - t);
  const bb = 2 * (1 - t) * t;
  const c = t * t;
  out[0] = a * p0[0] + bb * p1[0] + c * p2[0];
  out[1] = a * p0[1] + bb * p1[1] + c * p2[1];
  out[2] = a * p0[2] + bb * p1[2] + c * p2[2];
  return out;
}
