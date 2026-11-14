import type { ReactNode } from "react";
import type { WallState } from "../api/types";
import { clockDuration, fmt, headlineTone, simParts, splitHeadline } from "./format";
import { useFitText } from "./hooks";
import { useUi } from "./store";

function Mark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <rect x="3" y="5" width="26" height="22" rx="3" fill="none" stroke="currentColor" strokeWidth="2.4" />
      <rect x="8" y="10" width="11" height="5" rx="1" fill="#4cc9f0" />
      <circle cx="23.5" cy="12.5" r="2.2" fill="#3ddc84" />
      <path d="M8 20.5h16" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

function Headline({ text }: { text: string }) {
  const tone = headlineTone(text);
  const [lead, rest] = splitHeadline(text);
  const ref = useFitText<HTMLParagraphElement>(text, 14);
  return (
    <section className={`headline glass ${tone}`} aria-labelledby="headline-label">
      <span id="headline-label" className="label">What the database allows</span>
      <p ref={ref} key={text} className="headline-text changed-in" role="status" aria-live="polite">
        <span className="lead">{lead}</span>
        {rest && <span className="rest">{rest}</span>}
      </p>
    </section>
  );
}

function Clock({ run }: { run: WallState["run"] }) {
  if (!run) {
    return (
      <section className="clock glass" aria-label="Run clock">
        <span className="label">Simulated time</span>
        <div className="clock-meta">No run yet</div>
      </section>
    );
  }
  const { day, time } = simParts(run.sim_now);
  const speed = run.seconds_per_sim_hour;
  return (
    <section className="clock glass" aria-label={`Simulated day ${day} of ${run.days_total}, ${time}`}>
      <span className="label">Simulated day {run.paused && <span className="chip warn">Paused</span>}</span>
      <div className="clock-day num">
        {day}
        <small> / {run.days_total}</small>
      </div>
      <div className="clock-time num">{time}</div>
      <div className="clock-days" aria-hidden="true">
        {Array.from({ length: run.days_total }, (_, i) => (
          <span key={i} className={i < day ? "done" : i === day ? "now" : ""} />
        ))}
      </div>
      <div className="clock-meta">
        <span>
          Real time <b className="num">{clockDuration(run.real_elapsed_s)}</b>
        </span>
        <span>
          Clock <b className="num">{speed > 0 ? `${speed < 1 ? speed.toFixed(1) : fmt(Math.round(speed))} s` : "stopped"}</b>
          {speed > 0 && " per sim hour"}
        </span>
      </div>
    </section>
  );
}

interface CounterProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "good" | "bad";
  title?: string;
}

function Counter({ label, value, sub, tone, title }: CounterProps) {
  return (
    <div className={`counter ${tone ?? ""}`} title={title}>
      <span className="label">{label}</span>
      <span className="counter-value">
        {value}
        {sub && <small>{sub}</small>}
      </span>
    </div>
  );
}

interface HeaderProps {
  state: WallState | null;
  stale: string | null;
  calm: boolean;
  reducedMotion: boolean;
}

export default function Header({ state, stale, calm, reducedMotion }: HeaderProps) {
  const setCalm = useUi((s) => s.setCalmToggle);
  const recall = state?.recall ?? null;
  const run = state?.run ?? null;
  const inv = run?.invariants;
  const held = inv ? inv.passed.filter(Boolean).length : 0;
  const recallLine = recall
    ? `Recall of batch ${recall.batch_code} · ${recall.model} · ${fmt(recall.units_total)} panels`
    : "No recall open · watching the connected panels";
  return (
    <header className="top">
      <section className="brand glass" aria-label="All Clear">
        <h1 className="wordmark">
          <Mark />
          ALL CLEAR
        </h1>
        <div className="recall-line" title={recall ? `${recall.product}. ${recall.hazard}` : undefined}>
          {recallLine}
        </div>
      </section>
      <Headline text={state?.headline ?? "Connecting to the recall…"} />
      <Clock run={run} />
      <section className="counters glass" aria-label="Run counters">
        <Counter label="Parties" value={run ? fmt(run.parties.total) : "—"}
          sub={run ? `${run.parties.at_start} at start, ${run.parties.found} found` : undefined} />
        <Counter label="Steps" value={run ? fmt(run.steps) : "—"} />
        <Counter label="Kills survived" value={run ? fmt(run.kills) : "—"} />
        <Counter label="Adoptions" value={run ? fmt(run.adoptions) : "—"} />
        <Counter label="Duplicate effects" value={run ? fmt(run.duplicates) : "—"}
          tone={run ? (run.duplicates === 0 ? "good" : "bad") : undefined} />
        <Counter label="Context per decision" value={run ? fmt(run.tokens_per_decision) : "—"} sub={run ? "tokens" : undefined}
          title="The model's brief has a fixed size: the same at step 300 as at step 1" />
        <Counter
          label="Invariants"
          tone={inv ? (inv.ok ? "good" : "bad") : undefined}
          title={inv ? inv.names.map((n, i) => `${inv.passed[i] ? "holds" : "FAILED"}: ${n}`).join("\n") : undefined}
          value={
            inv ? (
              <>
                {held} of {inv.passed.length}
                <span className="inv-dots" aria-hidden="true">
                  {inv.passed.map((ok, i) => <span key={i} className={`dot ${ok ? "good" : "bad"}`} />)}
                </span>
              </>
            ) : "—"
          }
          sub={inv?.at_label ?? undefined}
        />
        <div className="counters-tail">
          {stale && <span className="chip bad" role="status">{stale}</span>}
          {state?.run?.saboteur.on && <span className="chip warn">Saboteur on</span>}
          {state?.failpoint && <span className="chip warn">Failpoint {state.failpoint}</span>}
          <button
            type="button"
            className="toggle"
            aria-pressed={calm}
            disabled={reducedMotion}
            title={reducedMotion ? "Your system asks for reduced motion" : "Calm the motion on screen"}
            onClick={() => setCalm(!calm)}
          >
            Calm
          </button>
        </div>
      </section>
    </header>
  );
}
