// The screen's and the panel page's only door to the API (or to the in-browser mock, ?mock=1).
//
// Admin key: the operator opens the screen once with ?key=<ADMIN_TOKEN>. The key moves to this
// browser's localStorage and is removed from the address bar, so it never sits in a URL that is
// projected, bookmarked or shared. It is sent as X-Admin-Token on /wall/state (the server answers
// admin: true) and on every operator POST.

import type {
  OperatorAction,
  OperatorResult,
  PanelActionResult,
  PanelPageState,
  WallState,
} from "./types";
import * as mock from "./mock";

const KEY_STORE = "allclear.adminKey";
const params = new URLSearchParams(window.location.search);

export const MOCK = params.get("mock") === "1";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function loadKey(): string | null {
  const fromUrl = params.get("key");
  if (fromUrl !== null) {
    try {
      if (fromUrl) window.localStorage.setItem(KEY_STORE, fromUrl);
      else window.localStorage.removeItem(KEY_STORE);
    } catch {
      // Storage blocked: the key lives for this page only.
    }
    const url = new URL(window.location.href);
    url.searchParams.delete("key");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    return fromUrl || null;
  }
  try {
    return window.localStorage.getItem(KEY_STORE);
  } catch {
    return null;
  }
}

let adminKey = loadKey();

export function hasAdminKey(): boolean {
  return !!adminKey;
}

export function forgetAdminKey(): void {
  adminKey = null;
  try {
    window.localStorage.removeItem(KEY_STORE);
  } catch {
    // Nothing stored.
  }
}

/** The screen starts the mock run; the panel page joins it. A no-op against the real API. */
export function initMock(role: "screen" | "panel"): void {
  if (!MOCK) return;
  if (role === "panel") {
    mock.joinRun();
    return;
  }
  const t = params.get("t");
  mock.startRun({ t: t === null ? undefined : Number(t), freeze: params.get("freeze") === "1", hold: params.get("hold") === "1" });
}

/** Re-read promptly when another tab changes the mock run. */
export function watchMock(onChange: () => void): () => void {
  return MOCK ? mock.watch(onChange) : () => undefined;
}

function headers(json: boolean): HeadersInit {
  const h: Record<string, string> = { Accept: "application/json" };
  if (json) h["Content-Type"] = "application/json";
  if (adminKey) h["X-Admin-Token"] = adminKey;
  return h;
}

/** FastAPI errors carry `detail` as a string or a list of {msg}; quote the server's words. */
function detailOf(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  if (typeof b.message === "string") return b.message;
  if (typeof b.detail === "string") return b.detail;
  if (Array.isArray(b.detail)) {
    return b.detail.map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d))).join("; ");
  }
  return null;
}

async function readJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: headers(false), signal, cache: "no-store" });
  const body = await readJson(res);
  if (!res.ok) throw new ApiError(res.status, detailOf(body) ?? `${res.status} ${res.statusText}`);
  return body as T;
}

export async function getWallState(signal?: AbortSignal): Promise<WallState> {
  if (MOCK) return mock.wallState(!!adminKey);
  return getJson<WallState>("/wall/state", signal);
}

export async function getPanelState(serial: string, signal?: AbortSignal): Promise<PanelPageState> {
  if (MOCK) {
    const state = mock.panelState(serial);
    if (!state) throw new ApiError(404, `No connected panel ${serial}`);
    return state;
  }
  return getJson<PanelPageState>(`/panel-api/${encodeURIComponent(serial)}`, signal);
}

/** Run test / Apply update. A refusal by Atlas comes back in `refused`, verbatim. */
export async function panelAction(serial: string, kind: "test" | "update"): Promise<PanelActionResult> {
  if (MOCK) {
    const result = mock.panelAction(serial, kind);
    if (!result) throw new ApiError(404, `No connected panel ${serial}`);
    return result;
  }
  const res = await fetch(`/panel-api/${encodeURIComponent(serial)}/${kind}`, { method: "POST", headers: headers(true), body: "{}" });
  const body = await readJson(res);
  if (!res.ok) throw new ApiError(res.status, detailOf(body) ?? `${res.status} ${res.statusText}`);
  return body as PanelActionResult;
}

function isOperatorResult(body: unknown): body is OperatorResult {
  return !!body && typeof body === "object" && typeof (body as OperatorResult).ok === "boolean" &&
    typeof (body as OperatorResult).message === "string";
}

/** An operator action. HTTP errors become { ok: false } with the server's words. */
export async function sendOperator(action: OperatorAction): Promise<OperatorResult> {
  if (MOCK) return mock.operator(action);
  let res: Response;
  try {
    res = await fetch(action.path, { method: "POST", headers: headers(true), body: JSON.stringify(action.body) });
  } catch (err) {
    return { ok: false, message: `The API did not answer: ${err instanceof Error ? err.message : String(err)}` };
  }
  const body = await readJson(res);
  if (isOperatorResult(body)) return body;
  if (!res.ok) {
    const refused = body && typeof body === "object" ? ((body as { refused?: OperatorResult["refused"] }).refused ?? null) : null;
    const message = res.status === 401 ? "The admin key was refused (401). Open the screen again with ?key=<admin token>"
      : detailOf(body) ?? `${res.status} ${res.statusText}`;
    return { ok: false, message, refused };
  }
  return { ok: true, message: detailOf(body) ?? (typeof body === "string" ? body : "Done") };
}

/** Where the simulated panel page lives: served by the API, or by the dev server in development. */
export function panelPageUrl(serial: string): string {
  const query = MOCK ? "mock=1" : "";
  if (import.meta.env.DEV) {
    return `${import.meta.env.BASE_URL}panel.html?serial=${encodeURIComponent(serial)}${query ? `&${query}` : ""}`;
  }
  return `/panel/${encodeURIComponent(serial)}${query ? `?${query}` : ""}`;
}

/** The serial this panel page shows: /panel/{serial}, or ?serial= in development. */
export function serialFromLocation(): string | null {
  const fromQuery = params.get("serial");
  if (fromQuery) return fromQuery;
  const m = window.location.pathname.match(/\/panel\/([^/?#]+)\/?$/);
  return m ? decodeURIComponent(m[1]) : null;
}
