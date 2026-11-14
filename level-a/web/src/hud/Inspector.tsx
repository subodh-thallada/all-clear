import { useEffect } from "react";
import type { Edge, Party, WallState } from "../api/types";
import { BUCKETS, CHANNEL_TEXT, FOUND_VIA_TEXT, LADDER_STEPS, TYPE_TEXT, clockDuration, fmt } from "./format";
import { useUi } from "./store";

/** The shared contract has no open questions on a party; a server that sends them is shown them. */
type PartyWithQuestions = Party & { open_questions?: string[] };

const QUESTION_TEXT: Record<string, string> = {
  "q-01": "q-01 model on the front",
  "q-02": "q-02 version on the display",
  "q-03": "q-03 sounder test",
};

function Buckets({ p }: { p: Party }) {
  const total = BUCKETS.reduce((n, b) => n + p[b.key], 0);
  const summary = BUCKETS.map((b) => `${b.label} ${p[b.key]}`).join(", ");
  return (
    <div className="insp-sec">
      <span className="label">
        Panels · shipped in <span className="num">{fmt(p.shipped_in)}</span>
      </span>
      <div className={`stack${total ? "" : " empty"}`} role="img" aria-label={`Buckets of ${p.name}: ${summary}`}>
        {total > 0 &&
          BUCKETS.filter((b) => p[b.key] > 0).map((b) => (
            <span key={b.key} style={{ flex: `${p[b.key]} 1 0`, background: b.color }} title={`${b.label}: ${fmt(p[b.key])}`} />
          ))}
      </div>
      <ul className="legend">
        {BUCKETS.map((b) => (
          <li key={b.key} className={p[b.key] ? "" : "zero"}>
            <span className="swatch" style={{ background: b.color }} />
            {b.label}
            <b>{fmt(p[b.key])}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EdgeRow({ e, other, dir }: { e: Edge; other: string; dir: "in" | "out" }) {
  const state = e.state === "disputed" ? "bad" : e.state === "corroborated" ? "good" : "ghost";
  return (
    <div className="edge-row">
      <span className={`chip ${state}`}>{e.state === "awaiting_recipient" ? "awaiting" : e.state}</span>
      <span className="who">
        {dir === "in" ? "from " : "to "}
        {other}
      </span>
      <span className="qty num">
        sent {fmt(e.sender_qty)} · got {e.recipient_qty === null ? "—" : fmt(e.recipient_qty)}
        {e.disputed_qty > 0 && (
          <>
            {" "}
            · <span className="bad">disputed {fmt(e.disputed_qty)}</span>
          </>
        )}
      </span>
    </div>
  );
}

export default function Inspector({ state }: { state: WallState | null }) {
  const selected = useUi((s) => s.selected);
  const select = useUi((s) => s.select);
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") select(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [select]);

  const p = state?.parties.find((x) => x.party_id === selected) as PartyWithQuestions | undefined;
  if (!p || !state) return null;
  const names = new Map(state.parties.map((x) => [x.party_id, x.name]));
  const inbound = state.edges.filter((e) => e.to_party === p.party_id);
  const outbound = state.edges.filter((e) => e.from_party === p.party_id);
  const checks = p.checks;
  const affected = state.recall?.affected_versions ?? [];
  const version = checks?.version_seen ?? null;
  const lease = p.lease;
  const questions = p.open_questions ?? [];
  const step = Math.max(1, Math.min(LADDER_STEPS.length, p.ladder_step));
  return (
    <aside className="inspector glass" aria-labelledby="insp-name">
      <div className="insp-head">
        <div>
          <h2 id="insp-name" className="insp-name">{p.name}</h2>
          <div className="insp-tags">
            {p.simulated ? <span className="chip sim">Simulated</span> : <span className="chip real">Real phone</span>}
            <span className="chip">{TYPE_TEXT[p.type]}</span>
            <span className="chip">Tier {p.tier}</span>
            <span className={`chip ${p.state === "reconciled" || p.state === "claimed" ? "good" : p.state === "pending" ? "ghost" : "warn"}`}>
              {p.state}
            </span>
            {p.slice && <span className="chip real">Live slice</span>}
            {p.hold_for_demo && <span className="chip warn">Held for the demo</span>}
          </div>
        </div>
        <button type="button" className="icon-btn" onClick={() => select(null)} aria-label="Close the party inspector">
          <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
            <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="insp-body scroll">
        <div className="insp-sec">
          <dl className="facts">
            <dt>Found via</dt>
            <dd>{FOUND_VIA_TEXT[p.found_via]}</dd>
            <dt>Channel</dt>
            <dd>{CHANNEL_TEXT[p.channel]}{p.order_system ? ` (${p.order_system})` : ""}</dd>
            {p.sites.length > 0 && (
              <>
                <dt>Sites</dt>
                <dd>{p.sites.join(", ")}</dd>
              </>
            )}
          </dl>
        </div>
        <Buckets p={p} />
        {(checks || p.connected_panels > 0) && (
          <div className="insp-sec">
            <span className="label">Checks and evidence</span>
            <dl className="facts">
              {checks && (
                <>
                  <dt>Model</dt>
                  <dd>{checks.model_seen ?? "—"}</dd>
                  <dt>Version</dt>
                  <dd className={version === null ? "" : affected.includes(version) ? "affected" : "okv"}>
                    {version ?? "—"}
                    {version !== null && (affected.includes(version) ? " · affected" : " · not affected")}
                  </dd>
                  <dt>Sounder</dt>
                  <dd>{checks.sounder_test === null ? "—" : checks.sounder_test ? "rang" : "did not ring"}</dd>
                </>
              )}
              <dt>Connected</dt>
              <dd className="num">
                {fmt(p.connected_panels)} linked · {fmt(p.fixed_by_panel)} fixed by own test · {fmt(p.connected_unfixed)} unfixed
              </dd>
            </dl>
          </div>
        )}
        <div className="insp-sec">
          <span className="label">Lease and work</span>
          {lease ? (
            <div className={`insp-lease${lease.expired ? " expired" : ""}`}>
              <div className="meter">
                <span style={{ width: `${Math.max(0, Math.min(1, lease.remaining_s / 8)) * 100}%` }} />
              </div>
              <span className="num">{lease.expired ? "expired" : `${lease.remaining_s.toFixed(1)} s`}</span>
            </div>
          ) : null}
          <dl className="facts">
            <dt>Holder</dt>
            <dd className="num">{lease ? `${lease.owner} · token ${lease.token}` : "free"}</dd>
            <dt>In flight</dt>
            <dd className="num">{p.in_flight ?? "—"}</dd>
            {p.live_attempt && (
              <>
                <dt>Call</dt>
                <dd className="num">
                  {p.live_attempt.attempt_id} · {p.live_attempt.real ? "real" : "simulated"} · {p.live_attempt.state} ·{" "}
                  {clockDuration(p.live_attempt.since_s)}
                </dd>
              </>
            )}
            <dt>Ladder</dt>
            <dd>
              step {step} of {LADDER_STEPS.length}: {LADDER_STEPS[step - 1]}
            </dd>
            <dt>Next</dt>
            <dd className="num">{p.next_label ?? "—"}</dd>
            <dt>Attempts</dt>
            <dd className="num">{fmt(p.attempts)}</dd>
            {p.escalation && (
              <>
                <dt>Escalation</dt>
                <dd>{p.escalation}</dd>
              </>
            )}
          </dl>
        </div>
        {(inbound.length > 0 || outbound.length > 0) && (
          <div className="insp-sec">
            <span className="label">Transfers</span>
            {inbound.map((e) => (
              <EdgeRow key={`i-${e.from_party}`} e={e} dir="in" other={names.get(e.from_party) ?? e.from_party} />
            ))}
            {outbound.map((e) => (
              <EdgeRow key={`o-${e.to_party}`} e={e} dir="out" other={names.get(e.to_party) ?? e.to_party} />
            ))}
          </div>
        )}
        {questions.length > 0 && (
          <div className="insp-sec">
            <span className="label">Open questions</span>
            <div className="qchips">
              {questions.map((q) => <span key={q} className="chip">{QUESTION_TEXT[q] ?? q}</span>)}
            </div>
          </div>
        )}
        {p.form_url && (
          <div className="insp-sec">
            <a className="btn" href={p.form_url} target="_blank" rel="noreferrer">
              Open the form
            </a>
          </div>
        )}
      </div>
    </aside>
  );
}
