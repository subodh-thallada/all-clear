// Formatting and classification shared by the HUD and the panel page.

import type { FeedLine, Level, Party, SystemView } from "../api/types";

export const fmt = (n: number): string => n.toLocaleString("en-US");

export function pct(n: number, d: number): string {
  if (d <= 0) return "0%";
  const p = (100 * n) / d;
  return `${p >= 99.5 && n < d ? 99 : Math.round(p)}%`;
}

export const ratio = (n: number, d: number): number => (d > 0 ? Math.max(0, Math.min(1, n / d)) : 0);

/** 75 → "1:15", 4630 → "1:17:10". */
export function clockDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(sec).padStart(2, "0")}`;
}

/** Simulated hours since day 0 00:00 → { day, time: "14:00" }. */
export function simParts(simNow: number): { day: number; time: string } {
  const minutes = Math.max(0, Math.floor(simNow * 60 + 1e-6));
  const day = Math.floor(minutes / 1440);
  const m = minutes % 1440;
  return { day, time: `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}` };
}

/** "d6 15:40" → "day 6 15:40" for the panel display. */
export const longSimLabel = (label: string): string => label.replace(/^d(\d+)\b/, "day $1");

export type Tone = "good" | "warn" | "bad" | "info";

/** The server's words decide the colour: ALL CLEAR is green, NOT ALL CLEAR red, anything else amber. */
export function headlineTone(headline: string): Tone {
  const h = headline.trim().toUpperCase();
  if (h.startsWith("NOT ALL CLEAR")) return "bad";
  if (h.startsWith("ALL CLEAR")) return "good";
  return "warn";
}

/** Split "NOT ALL CLEAR: 39 panels open (…)" at its first colon, for emphasis only. */
export function splitHeadline(headline: string): [string, string] {
  const i = headline.indexOf(":");
  return i < 0 ? [headline, ""] : [headline.slice(0, i + 1), headline.slice(i + 1)];
}

export function systemTone(status: SystemView["status"]): Tone {
  if (status === "ok") return "good";
  if (status === "contract_failed") return "bad";
  return "warn";
}

export const STATUS_TEXT: Record<SystemView["status"], string> = {
  ok: "OK",
  contract_failed: "Contract failed",
  repairing: "Repairing",
  paused: "Paused",
};

export const ADAPTER_SOURCE_TEXT: Record<NonNullable<SystemView["adapter_source"]>, string> = {
  seed: "seed adapter",
  model: "repaired by model",
  rules: "repaired by rules",
  episode: "from memory",
};

const REFUSAL_KINDS = new Set(["claim_rejected", "close_refused", "attempt_refused", "write_rejected", "atlas_rejected"]);
const SENT_KINDS = new Set(["call_placed", "operation_sent", "form_link_sent", "hold_sent", "attempt_opened"]);
const VERIFIED_KINDS = new Set(["claim_accepted", "operation_verified", "form_claim", "form_link_verified", "edge_corroborated",
  "panel_fixed", "answer_recorded", "report_received"]);

/** An Atlas refusal: shown boxed, in mono, exactly as the database said it. */
export function isRefusal(line: FeedLine): boolean {
  return REFUSAL_KINDS.has(line.kind) || /^REJECTED by Atlas/i.test(line.text);
}

/** A refusal or stop by the tool gate: boxed, with the rule highlighted. */
export function isGate(line: FeedLine): boolean {
  return line.system === "gate" || line.kind === "gate_refused" || line.kind === "gate_stopped";
}

/** "sent" and "verified" are separate states (spec: what must be visible on screen). */
export function effectBadge(line: FeedLine): "sent" | "verified" | null {
  if (VERIFIED_KINDS.has(line.kind)) return "verified";
  if (SENT_KINDS.has(line.kind)) return "sent";
  if (/\bverified\b/i.test(line.text)) return "verified";
  if (/\bsent\b/i.test(line.text)) return "sent";
  return null;
}

/** The rule a gate line names ("Rule: lease."), for highlighting. */
export function splitRule(text: string): [string, string, string] | null {
  const m = text.match(/Rule:\s*([a-z_]+)/i);
  if (!m || m.index === undefined) return null;
  return [text.slice(0, m.index), m[0], text.slice(m.index + m[0].length)];
}

export const levelClass = (level: Level): string => level;

export const TYPE_TEXT: Record<Party["type"], string> = {
  distributor: "Distributor",
  installer: "Installer",
  owner: "Building owner",
};

export const FOUND_VIA_TEXT: Record<Party["found_via"], string> = {
  shipment: "shipment records",
  order_system: "order system accounts",
  claim: "named in a claim",
  memory: "memory",
};

export const CHANNEL_TEXT: Record<Party["channel"], string> = {
  voice: "real phone",
  sim: "simulated phone",
  form: "web form",
  order_system: "order system",
};

/** The six buckets, in the stacked bar's order (validated for colour-blind separation). */
export const BUCKETS = [
  { key: "fixed", label: "Fixed", color: "var(--fixed)" },
  { key: "returned", label: "Returned", color: "var(--returned)" },
  { key: "in_stock", label: "In stock", color: "var(--stock)" },
  { key: "installed", label: "Installed", color: "var(--warn)" },
  { key: "unresolved", label: "Unresolved", color: "var(--bad)" },
  { key: "forwarded", label: "Forwarded", color: "var(--forwarded)" },
] as const;

export const LADDER_STEPS = ["call", "call at a better hour", "form link", "third call", "escalate to the rep"];
