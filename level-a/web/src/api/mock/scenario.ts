// The demo's world as data: batch B-4471 of 2,400 DEMO-7 panels, 40 parties and 24 connected
// panels. Names and numbers are invented (spec "Scenario at scale", BUILD_PLAN §1). Every
// party's final books add up to what it was shipped, so the ledger the mock builds from these
// conserves 2,400 panels at every step.

export const UNITS_TOTAL = 2400;
export const AFFECTED = ["2.3", "2.4"];
export const FIXED_VERSION = "2.5";
export const BATCH = "B-4471";
export const MODEL = "DEMO-7";

export type Profile =
  | "straight"
  | "forwarder"
  | "no_answer_call" // answers only on the third call
  | "no_answer_form" // never picks up; submits the form link
  | "no_answer_hours" // answers only after 14:00; memory learns the hour
  | "callback"
  | "over_reporter"
  | "reviser"
  | "short_receiver"
  | "unreachable"
  | "slice"
  | "order_system";

export interface Books {
  in_stock: number;
  returned: number;
  /** Owners: panels installed at their buildings (fixed later by the fix script). */
  installed?: number;
}

export interface Forward {
  to: string;
  qty: number;
}

export interface PartySpec {
  id: string;
  name: string;
  type: "distributor" | "installer" | "owner";
  tier: 1 | 2 | 3;
  voice?: boolean; // a real phone (Harbour Fire, Corner Properties)
  slice?: boolean;
  profile: Profile;
  /** Tier 1: shipped by the supplier. Others: sent by `from`. */
  shipped: number;
  from: string | null;
  orderSystem?: "north" | "east";
  account?: string;
  books: Books;
  forwards?: Forward[];
  /** Over-reporter: the bucket total first reported. Short receiver: the count first received. */
  firstReport?: number;
  /** Reviser: the corrected books, two days after the first claim. */
  revised?: Books;
  /** Owners: the version the display shows before the installer's visit. */
  version?: string;
  sites?: string[];
  /** Tier 2: the simulated hour of the first contact (the planner's ranking, spread over days 0–2). */
  firstContact?: number;
  /** Owners: days from discovery to the installer's visit. */
  visitAfterDays?: number;
  phone: string;
}

const phone = (n: number) => `+1-416-555-01${String(n).padStart(2, "0")}`;

const d = (day: number, hh: number, mm = 0) => day * 24 + hh + mm / 60;

export const PARTIES: PartySpec[] = [
  // ---- tier 1: the shipment records -------------------------------------------------------
  {
    id: "dist-north", name: "Distributor North", type: "distributor", tier: 1, profile: "order_system",
    shipped: 1300, from: null, orderSystem: "north", books: { in_stock: 100, returned: 0 },
    forwards: [
      { to: "bloor-alarm", qty: 60 }, { to: "annex-fire", qty: 140 }, { to: "danforth-signal", qty: 90 },
      { to: "kensington-life-safety", qty: 110 }, { to: "leslieville-fire", qty: 100 },
      { to: "riverdale-alarm", qty: 80 }, { to: "roncesvalles-fire", qty: 70 },
      { to: "etobicoke-sprinkler", qty: 120 }, { to: "davisville-systems", qty: 48 },
      { to: "weston-fire-tech", qty: 130 }, { to: "yorkville-alarm", qty: 92 },
      { to: "scarborough-fire-works", qty: 160 },
    ],
    phone: phone(10),
  },
  {
    id: "dist-east", name: "Distributor East", type: "distributor", tier: 1, profile: "order_system",
    shipped: 860, from: null, orderSystem: "east", books: { in_stock: 60, returned: 0 },
    forwards: [
      { to: "beaches-fire", qty: 110 }, { to: "pape-alarm", qty: 70 }, { to: "leaside-fire-systems", qty: 95 },
      { to: "cabbagetown-signal", qty: 60 }, { to: "don-mills-alarm", qty: 125 }, { to: "agincourt-fire", qty: 85 },
      { to: "guildwood-protection", qty: 75 }, { to: "highland-creek-alarm", qty: 100 },
      { to: "rouge-valley-fire", qty: 80 },
    ],
    phone: phone(11),
  },
  {
    id: "harbour-fire", name: "Harbour Fire", type: "installer", tier: 1, voice: true, slice: true, profile: "slice",
    shipped: 40, from: null, books: { in_stock: 32, returned: 8 }, firstReport: 60, phone: phone(41),
  },
  {
    id: "queen-st-alarm", name: "Queen St Alarm", type: "installer", tier: 1, slice: true, profile: "slice",
    shipped: 120, from: null, books: { in_stock: 50, returned: 30 }, forwards: [{ to: "corner-properties", qty: 40 }],
    phone: phone(42),
  },
  {
    id: "lakeside-systems", name: "Lakeside Systems", type: "installer", tier: 1, slice: true, profile: "slice",
    shipped: 80, from: null, books: { in_stock: 20, returned: 60 }, phone: phone(43),
  },

  // ---- tier 2: Distributor North's accounts ----------------------------------------------
  {
    id: "bloor-alarm", name: "Bloor Alarm", type: "installer", tier: 2, profile: "forwarder", shipped: 60,
    from: "dist-north", orderSystem: "north", account: "N-001", books: { in_stock: 10, returned: 20 },
    forwards: [{ to: "junction-fire-services", qty: 30 }], firstContact: d(0, 8, 45), phone: phone(27),
  },
  {
    id: "annex-fire", name: "Annex Fire", type: "installer", tier: 2, profile: "forwarder", shipped: 140,
    from: "dist-north", orderSystem: "north", account: "N-002", books: { in_stock: 15, returned: 25 },
    forwards: [
      { to: "maple-court-residences", qty: 36 }, { to: "liberty-village-lofts", qty: 44 },
      { to: "parkdale-alarm-services", qty: 20 },
    ],
    firstContact: d(0, 12), phone: phone(28),
  },
  {
    id: "danforth-signal", name: "Danforth Signal", type: "installer", tier: 2, profile: "reviser", shipped: 90,
    from: "dist-north", orderSystem: "north", account: "N-003", books: { in_stock: 40, returned: 50 },
    revised: { in_stock: 25, returned: 65 }, firstContact: d(1, 11, 45), phone: phone(29),
  },
  {
    id: "kensington-life-safety", name: "Kensington Life Safety", type: "installer", tier: 2, profile: "forwarder",
    shipped: 110, from: "dist-north", orderSystem: "north", account: "N-004", books: { in_stock: 0, returned: 20 },
    forwards: [
      { to: "spadina-works", qty: 30 }, { to: "st-clair-medical", qty: 24 }, { to: "bathurst-commons", qty: 20 },
      { to: "front-st-offices", qty: 16 },
    ],
    firstContact: d(1, 8, 30), phone: phone(30),
  },
  {
    id: "leslieville-fire", name: "Leslieville Fire Protection", type: "installer", tier: 2, profile: "straight",
    shipped: 100, from: "dist-north", orderSystem: "north", account: "N-005", books: { in_stock: 30, returned: 70 },
    firstContact: d(0, 9, 50), phone: phone(31),
  },
  {
    id: "riverdale-alarm", name: "Riverdale Alarm Co", type: "installer", tier: 2, profile: "no_answer_call",
    shipped: 80, from: "dist-north", orderSystem: "north", account: "N-006", books: { in_stock: 20, returned: 60 },
    firstContact: d(0, 10, 55), phone: phone(32),
  },
  {
    id: "roncesvalles-fire", name: "Roncesvalles Fire", type: "installer", tier: 2, profile: "over_reporter",
    shipped: 70, from: "dist-north", orderSystem: "north", account: "N-007", books: { in_stock: 25, returned: 45 },
    firstReport: 85, firstContact: d(0, 14, 10), phone: phone(33),
  },
  {
    id: "etobicoke-sprinkler", name: "Etobicoke Sprinkler & Alarm", type: "installer", tier: 2, profile: "callback",
    shipped: 120, from: "dist-north", orderSystem: "north", account: "N-008", books: { in_stock: 50, returned: 70 },
    firstContact: d(0, 13, 5), phone: phone(34),
  },
  {
    id: "davisville-systems", name: "Davisville Systems", type: "installer", tier: 2, profile: "no_answer_form",
    shipped: 48, from: "dist-north", orderSystem: "north", account: "N-009", books: { in_stock: 8, returned: 40 },
    firstContact: d(0, 17, 25), phone: phone(35),
  },
  {
    id: "weston-fire-tech", name: "Weston Fire Tech", type: "installer", tier: 2, profile: "over_reporter",
    shipped: 130, from: "dist-north", orderSystem: "north", account: "N-010", books: { in_stock: 40, returned: 90 },
    firstReport: 150, firstContact: d(1, 9, 35), phone: phone(36),
  },
  {
    id: "yorkville-alarm", name: "Yorkville Alarm", type: "installer", tier: 2, profile: "straight", shipped: 92,
    from: "dist-north", orderSystem: "north", account: "N-011", books: { in_stock: 12, returned: 80 },
    firstContact: d(1, 13, 55), phone: phone(37),
  },
  {
    id: "scarborough-fire-works", name: "Scarborough Fire Works", type: "installer", tier: 2, profile: "callback",
    shipped: 160, from: "dist-north", orderSystem: "north", account: "N-012", books: { in_stock: 60, returned: 100 },
    firstContact: d(2, 9, 35), phone: phone(38),
  },

  // ---- tier 2: Distributor East's accounts -----------------------------------------------
  {
    id: "beaches-fire", name: "Beaches Fire & Safety", type: "installer", tier: 2, profile: "callback", shipped: 110,
    from: "dist-east", orderSystem: "east", account: "E-001", books: { in_stock: 30, returned: 80 },
    firstContact: d(0, 15, 15), phone: phone(44),
  },
  {
    id: "pape-alarm", name: "Pape Alarm", type: "installer", tier: 2, profile: "over_reporter", shipped: 70,
    from: "dist-east", orderSystem: "east", account: "E-002", books: { in_stock: 20, returned: 50 },
    firstReport: 90, firstContact: d(1, 10, 40), phone: phone(45),
  },
  {
    id: "leaside-fire-systems", name: "Leaside Fire Systems", type: "installer", tier: 2, profile: "forwarder",
    shipped: 95, from: "dist-east", orderSystem: "east", account: "E-003", books: { in_stock: 0, returned: 10 },
    forwards: [
      { to: "bayview-annex-mgmt", qty: 30 }, { to: "humber-fire", qty: 15 }, { to: "ossington-studios", qty: 18 },
      { to: "midtown-seniors", qty: 22 },
    ],
    firstContact: d(2, 8, 30), phone: phone(46),
  },
  {
    id: "cabbagetown-signal", name: "Cabbagetown Signal", type: "installer", tier: 2, profile: "no_answer_call",
    shipped: 60, from: "dist-east", orderSystem: "east", account: "E-004", books: { in_stock: 15, returned: 45 },
    firstContact: d(1, 12, 50), phone: phone(47),
  },
  {
    id: "don-mills-alarm", name: "Don Mills Alarm", type: "installer", tier: 2, profile: "straight", shipped: 125,
    from: "dist-east", orderSystem: "east", account: "E-005", books: { in_stock: 45, returned: 80 },
    firstContact: d(0, 16, 20), phone: phone(48),
  },
  {
    id: "agincourt-fire", name: "Agincourt Fire", type: "installer", tier: 2, profile: "reviser", shipped: 85,
    from: "dist-east", orderSystem: "east", account: "E-006", books: { in_stock: 35, returned: 50 },
    revised: { in_stock: 20, returned: 65 }, firstContact: d(1, 15), phone: phone(49),
  },
  {
    id: "guildwood-protection", name: "Guildwood Protection", type: "installer", tier: 2, profile: "no_answer_form",
    shipped: 75, from: "dist-east", orderSystem: "east", account: "E-007", books: { in_stock: 25, returned: 50 },
    firstContact: d(1, 16, 5), phone: phone(50),
  },
  {
    id: "highland-creek-alarm", name: "Highland Creek Alarm", type: "installer", tier: 2, profile: "no_answer_hours",
    shipped: 100, from: "dist-east", orderSystem: "east", account: "E-008", books: { in_stock: 40, returned: 60 },
    firstContact: d(1, 17, 10), phone: phone(51),
  },
  {
    id: "rouge-valley-fire", name: "Rouge Valley Fire", type: "installer", tier: 2, profile: "unreachable",
    shipped: 80, from: "dist-east", orderSystem: "east", account: "E-009", books: { in_stock: 30, returned: 50 },
    firstContact: d(2, 10, 40), phone: phone(52),
  },

  // ---- tier 3: named in claims ------------------------------------------------------------
  {
    id: "corner-properties", name: "Corner Properties", type: "owner", tier: 3, voice: true, slice: true,
    profile: "slice", shipped: 40, from: "queen-st-alarm", books: { in_stock: 0, returned: 0, installed: 25 },
    firstReport: 25, version: "2.4",
    sites: ["Corner Properties, Tower 1", "Corner Properties, Tower 2", "Corner Properties, Tower 3"],
    phone: phone(60),
  },
  {
    id: "junction-fire-services", name: "Junction Fire Services", type: "installer", tier: 3, profile: "forwarder",
    shipped: 30, from: "bloor-alarm", books: { in_stock: 0, returned: 6 }, forwards: [{ to: "dundas-lofts", qty: 24 }],
    phone: phone(61),
  },
  {
    id: "parkdale-alarm-services", name: "Parkdale Alarm Services", type: "installer", tier: 3, profile: "straight",
    shipped: 20, from: "annex-fire", books: { in_stock: 8, returned: 12 }, phone: phone(62),
  },
  {
    id: "humber-fire", name: "Humber Fire Contractors", type: "installer", tier: 3, profile: "straight", shipped: 15,
    from: "leaside-fire-systems", books: { in_stock: 0, returned: 15 }, phone: phone(63),
  },
  {
    id: "maple-court-residences", name: "Maple Court Residences", type: "owner", tier: 3, profile: "straight",
    shipped: 36, from: "annex-fire", books: { in_stock: 0, returned: 0, installed: 36 }, version: "2.4",
    sites: ["Maple Court", "Maple Court, Block B", "Maple Court, Block C"], visitAfterDays: 3, phone: phone(64),
  },
  {
    id: "liberty-village-lofts", name: "Liberty Village Lofts", type: "owner", tier: 3, profile: "short_receiver",
    shipped: 44, from: "annex-fire", books: { in_stock: 0, returned: 0, installed: 44 }, firstReport: 40,
    version: "2.4", sites: ["Liberty Village Lofts, East", "Liberty Village Lofts, West"], visitAfterDays: 4,
    phone: phone(65),
  },
  {
    id: "spadina-works", name: "Spadina Works Ltd", type: "owner", tier: 3, profile: "straight", shipped: 30,
    from: "kensington-life-safety", books: { in_stock: 0, returned: 0, installed: 30 }, version: "2.3",
    sites: ["Spadina Works", "Spadina Works, Annex", "Spadina Works, Loft"], visitAfterDays: 2, phone: phone(66),
  },
  {
    id: "st-clair-medical", name: "St Clair Medical Centre", type: "owner", tier: 3, profile: "short_receiver",
    shipped: 24, from: "kensington-life-safety", books: { in_stock: 0, returned: 0, installed: 20 }, firstReport: 20,
    version: "2.4", sites: ["St Clair Medical Centre", "St Clair Medical Centre, Clinic"], visitAfterDays: 3,
    phone: phone(67),
  },
  {
    id: "bathurst-commons", name: "Bathurst Commons", type: "owner", tier: 3, profile: "straight", shipped: 20,
    from: "kensington-life-safety", books: { in_stock: 0, returned: 0, installed: 20 }, version: "2.3",
    sites: ["Bathurst Commons, North", "Bathurst Commons, South"], visitAfterDays: 2, phone: phone(68),
  },
  {
    id: "front-st-offices", name: "Front St Offices", type: "owner", tier: 3, profile: "straight", shipped: 16,
    from: "kensington-life-safety", books: { in_stock: 0, returned: 0, installed: 16 }, version: "2.4",
    sites: ["Front St Offices"], visitAfterDays: 4, phone: phone(69),
  },
  {
    id: "bayview-annex-mgmt", name: "Bayview Annex Management", type: "owner", tier: 3, profile: "straight",
    shipped: 30, from: "leaside-fire-systems", books: { in_stock: 0, returned: 0, installed: 30 }, version: "2.4",
    sites: ["Bayview Annex", "Bayview Annex, Garden Wing", "Bayview Annex, North"], visitAfterDays: 2,
    phone: phone(70),
  },
  {
    id: "ossington-studios", name: "Ossington Studios", type: "owner", tier: 3, profile: "straight", shipped: 18,
    from: "leaside-fire-systems", books: { in_stock: 0, returned: 0, installed: 18 }, version: "2.4",
    sites: ["Ossington Studios", "Ossington Studios, Rear"], visitAfterDays: 3, phone: phone(71),
  },
  {
    id: "midtown-seniors", name: "Midtown Seniors Residence", type: "owner", tier: 3, profile: "straight",
    shipped: 22, from: "leaside-fire-systems", books: { in_stock: 0, returned: 0, installed: 22 }, version: "2.4",
    sites: ["Midtown Seniors Residence", "Midtown Seniors Residence, Wing B"], visitAfterDays: 4, phone: phone(72),
  },
  {
    id: "dundas-lofts", name: "Dundas Lofts", type: "owner", tier: 3, profile: "straight", shipped: 24,
    from: "junction-fire-services", books: { in_stock: 0, returned: 0, installed: 24 }, version: "2.3",
    sites: ["Dundas Lofts, Tower A", "Dundas Lofts, Tower B", "Dundas Lofts, Podium"], visitAfterDays: 3,
    phone: phone(73),
  },
];

export const PARTY_BY_ID = new Map(PARTIES.map((p) => [p.id, p]));

export interface PanelSpec {
  serial: string;
  site: string;
  owner: string;
  version: string;
  fault: boolean;
}

// In the order they report their monthly test on day 0: the 7th, 11th and 12th fail at three
// sites (spec: events pe-0007, pe-0011, pe-0012 at Maple Court, Bayview Annex, Spadina Works).
export const PANELS: PanelSpec[] = [
  { serial: "D7-4471-0102", site: "Ossington Studios", owner: "ossington-studios", version: "2.4", fault: false },
  { serial: "D7-4471-0108", site: "Front St Offices", owner: "front-st-offices", version: "2.4", fault: false },
  { serial: "D7-4471-0111", site: "Bathurst Commons, North", owner: "bathurst-commons", version: "2.3", fault: false },
  { serial: "D7-4471-0115", site: "Midtown Seniors Residence", owner: "midtown-seniors", version: "2.4", fault: false },
  { serial: "D7-4471-0121", site: "Maple Court, Block B", owner: "maple-court-residences", version: "2.4", fault: false },
  { serial: "D7-4471-0126", site: "St Clair Medical Centre", owner: "st-clair-medical", version: "2.4", fault: false },
  { serial: "D7-4471-0117", site: "Maple Court", owner: "maple-court-residences", version: "2.4", fault: true },
  { serial: "D7-4471-0130", site: "Spadina Works, Annex", owner: "spadina-works", version: "2.3", fault: false },
  { serial: "D7-4471-0134", site: "Liberty Village Lofts, West", owner: "liberty-village-lofts", version: "2.4", fault: false },
  { serial: "D7-4471-0139", site: "Dundas Lofts, Tower A", owner: "dundas-lofts", version: "2.3", fault: false },
  { serial: "D7-4471-0142", site: "Bayview Annex", owner: "bayview-annex-mgmt", version: "2.4", fault: true },
  { serial: "D7-4471-0147", site: "Spadina Works", owner: "spadina-works", version: "2.3", fault: true },
  { serial: "D7-4471-0150", site: "Bayview Annex, Garden Wing", owner: "bayview-annex-mgmt", version: "2.4", fault: false },
  { serial: "D7-4471-0156", site: "Midtown Seniors Residence, Wing B", owner: "midtown-seniors", version: "2.4", fault: false },
  { serial: "D7-4471-0161", site: "Ossington Studios, Rear", owner: "ossington-studios", version: "2.4", fault: false },
  { serial: "D7-4471-0165", site: "Maple Court, Block C", owner: "maple-court-residences", version: "2.4", fault: false },
  { serial: "D7-4471-0193", site: "Corner Properties, Tower 2", owner: "corner-properties", version: "2.4", fault: true },
  { serial: "D7-4471-0170", site: "Dundas Lofts, Tower B", owner: "dundas-lofts", version: "2.4", fault: true },
  { serial: "D7-4471-0174", site: "Bathurst Commons, South", owner: "bathurst-commons", version: "2.4", fault: false },
  { serial: "D7-4471-0178", site: "Liberty Village Lofts, East", owner: "liberty-village-lofts", version: "2.4", fault: true },
  { serial: "D7-4471-0181", site: "St Clair Medical Centre, Clinic", owner: "st-clair-medical", version: "2.4", fault: false },
  { serial: "D7-4471-0185", site: "Spadina Works, Loft", owner: "spadina-works", version: "2.4", fault: false },
  { serial: "D7-4471-0188", site: "Bayview Annex, North", owner: "bayview-annex-mgmt", version: "2.3", fault: false },
  { serial: "D7-4471-0190", site: "Dundas Lofts, Podium", owner: "dundas-lofts", version: "2.4", fault: false },
];

/** The live slice's connected panel: the simulated panel page of the demo. */
export const SLICE_PANEL = "D7-4471-0193";

export const RECALL_INFO = {
  _id: "r-1",
  batch_code: BATCH,
  model: MODEL,
  product: "DEMO-7 fire alarm control panel",
  supplier: "Kestrel Life Safety",
  hazard: "The sounder circuit may fail to activate in an alarm on software 2.3 and 2.4",
  affected_versions: AFFECTED,
  fixed_version: FIXED_VERSION,
  units_total: UNITS_TOTAL,
};

export const INVARIANT_NAMES = [
  "No party has two live attempts",
  "No idempotency key has two holds",
  "Every accepted claim has evidence",
  "Mass balance equals units_total",
];

export const ORDER_SYSTEMS = {
  north: { label: "North orders", party: "dist-north", canary: { account: "N-009", value: 48, wrong: 22 } },
  east: { label: "East orders", party: "dist-east", canary: { account: "E-004", value: 60, wrong: 15 } },
} as const;
