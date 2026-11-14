import { useCallback, useEffect, useRef, useState } from "react";
import { getPanelState, panelAction, serialFromLocation } from "../api/client";
import type { PanelActionResult, PanelPageState } from "../api/types";
import { PanelModel } from "../scene/CityScene";
import { longSimLabel } from "../hud/format";
import { usePolling, usePrefersReducedMotion } from "../hud/hooks";

type Phase = "idle" | "testing" | "updating";

/** A test takes long enough on screen for the sounder and the display to be seen. */
const MIN_PHASE_MS = { testing: 1400, updating: 1000 };
const FLASH_MS = 2600;

const wait = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

interface Outcome {
  kind: "test" | "update";
  result: PanelActionResult | null;
  error: string | null;
}

function Display({ s }: { s: PanelPageState }) {
  const affected = s.recall?.affected_versions ?? [];
  const fixedVersion = s.recall?.fixed_version ?? null;
  const versionState = affected.includes(s.sw_version) ? "affected" : s.sw_version === fixedVersion ? "fixed" : "";
  const last = s.last_test;
  return (
    <div className="lcd" role="group" aria-label="The panel's display">
      <div className="lcd-top">
        <span>SIMULATED PANEL</span>
        <span>not a real fire alarm system</span>
      </div>
      <div className="lcd-row">
        <span>
          <i>Model</i> {s.model}
        </span>
        <span>
          <i>Serial</i> {s.serial}
        </span>
        <span className={`lcd-version ${versionState}`}>
          <i>Software</i> {s.sw_version}
          {versionState === "affected" && <em>affected</em>}
          {versionState === "fixed" && <em>fixed version</em>}
        </span>
      </div>
      <div className="lcd-row">
        <span>
          <i>Site:</i> {s.site_label}
        </span>
      </div>
      <div className="lcd-row">
        <span>
          <i>Last test:</i>{" "}
          {last ? (
            <>
              <b className={last.result}>{last.result.toUpperCase()}</b>
              <span className="lcd-gap">{last.detail}</span>
              <span className="lcd-gap">{longSimLabel(last.sim_label)}</span>
            </>
          ) : (
            "none yet"
          )}
        </span>
      </div>
    </div>
  );
}

function OutcomeLine({ o }: { o: Outcome }) {
  if (o.error) {
    return (
      <div className="pp-outcome bad" role="alert">
        <span className="label">The API refused</span>
        <pre className="verbatim">{o.error}</pre>
      </div>
    );
  }
  const r = o.result;
  if (!r) return null;
  if (r.refused) {
    return (
      <div className="pp-outcome bad" role="alert">
        <span className="label">Refused by Atlas · rule: {r.refused.rule}</span>
        <pre className="verbatim">{r.refused.message}</pre>
      </div>
    );
  }
  const moved = !!r.event?.effect;
  const head = o.kind === "update" ? "Apply update" : r.event ? `Run test · ${r.event.event_id} · ${r.event.result.toUpperCase()} on ${r.event.sw_version}` : "Run test";
  return (
    <div className={`pp-outcome ${moved ? "good" : r.event?.result === "fail" ? "warn" : "info"}`} role="status">
      <span className="label">{head}</span>
      <p className="pp-effect">{r.effect ?? (r.event?.result === "fail" ? "FAIL recorded. The panel stays unfixed" : "Recorded")}</p>
    </div>
  );
}

export default function PanelPage() {
  const serial = serialFromLocation();
  const load = useCallback(
    (signal: AbortSignal) => (serial ? getPanelState(serial, signal) : Promise.reject(new Error("No serial in the address"))),
    [serial],
  );
  const { data, error, refresh } = usePolling(load, 1000);
  const [phase, setPhase] = useState<Phase>("idle");
  const [flash, setFlash] = useState<"pass" | "fail" | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [override, setOverride] = useState<PanelPageState | null>(null);
  const reduced = usePrefersReducedMotion();
  const flashTimer = useRef<number | undefined>(undefined);

  useEffect(() => setOverride(null), [data]);
  useEffect(() => () => window.clearTimeout(flashTimer.current), []);
  useEffect(() => {
    document.title = serial ? `${serial} · Simulated panel · All Clear` : "Simulated panel · All Clear";
  }, [serial]);

  const state = override ?? data;

  const act = async (kind: "test" | "update") => {
    if (!serial || phase !== "idle") return;
    const next: Phase = kind === "test" ? "testing" : "updating";
    setPhase(next);
    setOutcome(null);
    const started = Date.now();
    let result: PanelActionResult | null = null;
    let err: string | null = null;
    try {
      result = await panelAction(serial, kind);
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
    const left = (reduced ? 0 : MIN_PHASE_MS[next]) - (Date.now() - started);
    if (left > 0) await wait(left);
    setPhase("idle");
    setOutcome({ kind, result, error: err });
    if (result) setOverride(result.state);
    if (result?.event) {
      setFlash(result.event.result);
      window.clearTimeout(flashTimer.current);
      flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
    }
    refresh();
  };

  if (!serial) {
    return (
      <div className="pp-shell">
        <Banner />
        <p className="pp-message glass">This page shows one simulated panel: open it as /panel/&lt;serial&gt;.</p>
      </div>
    );
  }

  const busy = phase !== "idle";
  const onFixed = !!state && state.sw_version === state.recall?.fixed_version;
  return (
    <div className="pp-shell">
      <Banner />
      <main className="pp-main">
        <section className="pp-model glass" aria-label={`3D model of panel ${serial}`}>
          <PanelModel state={state} phase={phase} flash={flash} />
          {flash && (
            <div className={`pp-flash ${flash}`} role="status">
              {flash === "pass" ? "PASS" : "FAIL"}
            </div>
          )}
        </section>
        <section className="pp-side">
          <div className="pp-card glass">
            {state ? <Display s={state} /> : (
              <div className="lcd" role="status">{error ? `Waiting for the API: ${error}` : "Reading the panel…"}</div>
            )}
            <div className="pp-actions">
              <button type="button" className="btn pp-btn primary" disabled={!state || busy} aria-busy={phase === "testing"}
                onClick={() => void act("test")}>
                {phase === "testing" ? "Testing…" : "Run test"}
              </button>
              <button type="button" className="btn pp-btn" disabled={!state || busy || onFixed} aria-busy={phase === "updating"}
                onClick={() => void act("update")}
                title={onFixed ? "Already on the fixed version" : "Set the panel's software to the fixed version"}>
                {phase === "updating" ? "Updating…" : onFixed ? "Up to date" : "Apply update"}
              </button>
            </div>
            <div aria-live="polite">{outcome && <OutcomeLine o={outcome} />}</div>
          </div>
          {state && (
            <div className="pp-card glass pp-facts">
              <dl>
                <dt>Linked party</dt>
                <dd>{state.party_name ?? "not linked yet: no claim has named this site"}</dd>
                <dt>Recall</dt>
                <dd>
                  {state.recall
                    ? `${state.recall._id} ${state.recall.status} · affected ${state.recall.affected_versions.join(", ")} · fixed ${state.recall.fixed_version}`
                    : "no recall open"}
                </dd>
                <dt>Counted fixed</dt>
                <dd className={state.fixed ? "good" : ""}>
                  {state.fixed ? "yes, by its own passing test" : "no: only a PASS on the fixed version counts"}
                </dd>
                <dt>Batch</dt>
                <dd>{state.batch_code}</dd>
              </dl>
            </div>
          )}
          {state && (
            <div className="pp-card glass pp-events">
              <h2 className="panel-title">Recent events</h2>
              {state.events.length ? (
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Event</th>
                      <th scope="col">When</th>
                      <th scope="col">Result</th>
                      <th scope="col">Software</th>
                      <th scope="col">Effect</th>
                    </tr>
                  </thead>
                  <tbody>
                    {state.events.map((e) => (
                      <tr key={e.event_id}>
                        <td className="num">{e.event_id}</td>
                        <td className="num">{e.sim_label}</td>
                        <td>
                          <span className={`chip ${e.result === "pass" ? "good" : "bad"}`}>{e.result}</span>
                          {e.kind === "fault" && <span className="pp-kind">fault</span>}
                        </td>
                        <td className="num">{e.sw_version}</td>
                        <td>{e.effect ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="pp-none">No events yet.</p>
              )}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function Banner() {
  return (
    <header className="sim-banner">
      <div className="sim-stripes" aria-hidden="true" />
      <p className="sim-plate">
        <strong>SIMULATED</strong>
        <span>— not a real fire alarm system</span>
      </p>
      <span className="sim-sub">All Clear · a stand-in for a connected DEMO-7 panel</span>
    </header>
  );
}
