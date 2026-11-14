// The in-browser mock backend (?mock=1). It plays the spec's demo as data: the 40-party long run
// compressed into a few minutes, then the live slice at the real run's speed. Its state is a pure
// function of an epoch, an action log and the time, both kept in localStorage, so the 3D screen
// and the simulated panel page (another tab) replay the same run and see each other's actions.
//
// URL options on the screen: t=<simulated hour to start at>, freeze=1 (stop the clock there, for
// screenshots), hold=1 (the demo controls wait for the operator: approval, release, failpoint,
// the order-system change, the panel and the close).

import type {
  OperatorAction,
  OperatorResult,
  PanelActionResult,
  PanelPageState,
  WallState,
} from "./types";
import { APPROVE_AT, MockClock, SIM_START } from "./mock/clock";
import { World, type ActionEntry, type Epoch, type LoggedAction } from "./mock/world";

const EPOCH_KEY = "allclear.mock.epoch";
const LOG_PREFIX = "allclear.mock.log.";

let memoryEpoch: Epoch | null = null;
const memoryLogs = new Map<string, LoggedAction[]>();

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage blocked (private window, preview): the run stays in this tab's memory.
  }
}

function parseEpoch(raw: string | null): Epoch | null {
  if (!raw) return null;
  try {
    const e = JSON.parse(raw) as Partial<Epoch>;
    if (typeof e.id === "string" && typeof e.startWall === "number" && typeof e.startMockMs === "number") {
      return { id: e.id, startWall: e.startWall, startMockMs: e.startMockMs, frozen: !!e.frozen, hold: !!e.hold };
    }
  } catch {
    // A malformed record starts a new run.
  }
  return null;
}

function currentEpoch(): Epoch {
  const stored = parseEpoch(read(EPOCH_KEY));
  if (stored) {
    memoryEpoch = stored;
    return stored;
  }
  return memoryEpoch ?? startRun({ freeze: false, hold: false });
}

function readLog(id: string): LoggedAction[] {
  const raw = read(LOG_PREFIX + id);
  if (raw) {
    try {
      const log = JSON.parse(raw) as LoggedAction[];
      if (Array.isArray(log)) {
        memoryLogs.set(id, log);
        return log;
      }
    } catch {
      // Fall through to the copy in memory.
    }
  }
  return memoryLogs.get(id) ?? [];
}

function writeLog(id: string, log: LoggedAction[]): void {
  memoryLogs.set(id, log);
  write(LOG_PREFIX + id, JSON.stringify(log));
}

function mockNow(e: Epoch): number {
  return e.frozen ? e.startMockMs : e.startMockMs + (Date.now() - e.startWall);
}

/** The screen starts a fresh run on every load. */
export function startRun(opts: { t?: number; freeze: boolean; hold: boolean }): Epoch {
  const id = `run-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
  const log: LoggedAction[] = [];
  const t = opts.t !== undefined && Number.isFinite(opts.t) ? Math.max(SIM_START, opts.t) : SIM_START;
  let approvalMs: number | null = null;
  if (opts.hold && t > APPROVE_AT) {
    // Starting past 08:05 in hold mode means the signal was already approved, on time.
    approvalMs = new MockClock(true, null).msAt(APPROVE_AT);
    log.push({ id: "approve-0", ms: approvalMs, type: "operator",
      action: { path: "/admin/signals/approve", body: { signal_id: "sig-01", approved_by: "operator-1" } } });
  }
  const startMockMs = new MockClock(opts.hold, approvalMs).msAt(t);
  const epoch: Epoch = { id, startWall: Date.now(), startMockMs, frozen: opts.freeze, hold: opts.hold };
  const previous = parseEpoch(read(EPOCH_KEY));
  if (previous) {
    try {
      window.localStorage.removeItem(LOG_PREFIX + previous.id);
    } catch {
      // Nothing to clean when storage is blocked.
    }
  }
  memoryEpoch = epoch;
  write(EPOCH_KEY, JSON.stringify(epoch));
  writeLog(id, log);
  return epoch;
}

/** The panel page joins the run the screen started (or starts one of its own). */
export function joinRun(): Epoch {
  return currentEpoch();
}

function replay(): { world: World; epoch: Epoch } {
  const epoch = currentEpoch();
  const world = new World(epoch, readLog(epoch.id));
  world.run(mockNow(epoch));
  return { world, epoch };
}

function act(entry: ActionEntry): { world: World; id: string } {
  const epoch = currentEpoch();
  const log = readLog(epoch.id);
  const id = `a-${log.length + 1}-${Date.now().toString(36)}`;
  const ms = Math.max(mockNow(epoch), log.length ? log[log.length - 1].ms : 0);
  writeLog(epoch.id, [...log, { ...entry, id, ms }]);
  const { world } = replay();
  return { world, id };
}

export function wallState(admin: boolean): WallState {
  return replay().world.wallState(admin);
}

export function panelState(serial: string): PanelPageState | null {
  return replay().world.panelState(serial);
}

export function panelAction(serial: string, kind: "test" | "update"): PanelActionResult | null {
  const { world, id } = act({ type: kind === "test" ? "panel_test" : "panel_update", serial });
  const state = world.panelState(serial);
  const outcome = world.outcomes.get(id);
  if (!state || !outcome || outcome.kind !== "panel") return null;
  return { ...outcome.result, state };
}

export function operator(action: OperatorAction): OperatorResult {
  const { world, id } = act({ type: "operator", action });
  const outcome = world.outcomes.get(id);
  return outcome?.kind === "operator" ? outcome.result : { ok: false, message: "The mock did not record the action" };
}

/** Calls `onChange` when another tab acts on the same run. */
export function watch(onChange: () => void): () => void {
  const handler = (e: StorageEvent) => {
    if (e.key === EPOCH_KEY || (e.key?.startsWith(LOG_PREFIX) ?? false)) onChange();
  };
  window.addEventListener("storage", handler);
  return () => window.removeEventListener("storage", handler);
}
