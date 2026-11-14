import type { ReactNode } from "react";
import type { Metrics as MetricsData } from "../api/types";
import { fmt, pct, ratio } from "./format";

interface MetricProps {
  kind: "checks" | "located" | "fixed";
  label: string;
  value: [number, number] | null;
  unit: string;
  children?: ReactNode;
}

function Metric({ kind, label, value, unit, children }: MetricProps) {
  const [n, d] = value ?? [0, 0];
  const share = pct(n, d);
  return (
    <section className={`metric glass ${kind}`} aria-label={value ? `${label}: ${fmt(n)} of ${fmt(d)} ${unit} (${share})` : label}>
      <div className="metric-head">
        <span className="label">{label}</span>
        {value && <span className="metric-pct">{share}</span>}
      </div>
      <div className="metric-value">
        <span className="big">{value ? fmt(n) : "—"}</span>
        <span className="of">{value ? `of ${fmt(d)} ${unit}` : unit}</span>
      </div>
      <div className="meter" role="presentation">
        <span style={{ width: `${ratio(n, d) * 100}%` }} />
      </div>
      <div className="metric-sub">{children}</div>
    </section>
  );
}

/** The three numbers the spec keeps apart: never merged into one. */
export default function Metrics({ metrics }: { metrics: MetricsData | null }) {
  const m = metrics;
  return (
    <div className="bottom" role="group" aria-label="Three measures, reported apart">
      <Metric kind="checks" label="Checks completed" value={m?.checks_completed ?? null} unit="parties">
        <span>parties claimed or reconciled</span>
      </Metric>
      <Metric kind="located" label="Panels located" value={m?.panels_located ?? null} unit="panels">
        {m && (
          <>
            <span className={m.not_located ? "warn" : ""}>
              not located <b className="num">{fmt(m.not_located)}</b>
            </span>
            <span className={m.disputed_units ? "bad" : ""}>
              disputed <b className="num">{fmt(m.disputed_units)}</b>
            </span>
          </>
        )}
      </Metric>
      <Metric kind="fixed" label="Fixed or returned" value={m?.fixed_or_returned ?? null} unit="panels">
        {m && (
          <>
            <span className={m.awaiting_fix ? "warn" : ""}>
              awaiting fix <b className="num">{fmt(m.awaiting_fix)}</b>
            </span>
            <span className={m.mass_balance === m.units_total ? "" : "bad"}
              title="Every panel on a party plus every panel still on a transfer">
              mass balance <b className="num">{fmt(m.mass_balance)}</b>
            </span>
          </>
        )}
      </Metric>
    </div>
  );
}
