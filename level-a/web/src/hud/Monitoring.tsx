import { useState } from "react";
import { sendOperator } from "../api/client";
import type { ConnectedPanel, Monitoring as MonitoringData, SignalView } from "../api/types";
import { fmt } from "./format";
import { useUi } from "./store";

const APPROVER = "operator-1";

function panelClass(p: ConnectedPanel): string {
  if (p.fixed) return "fixed";
  if (p.last_result === "fail") return "fail";
  if (p.last_result === null) return "unknown";
  return "";
}

function panelText(p: ConnectedPanel): string {
  const result = p.fixed ? "fixed by its own test" : p.last_result ? `last test ${p.last_result.toUpperCase()}` : "no test yet";
  return `${p.serial}, ${p.site_label}: software ${p.sw_version}, ${result}`;
}

function SignalCard({ signal, admin, onDone }: { signal: SignalView; admin: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const pushToast = useUi((s) => s.pushToast);
  const approve = async () => {
    setBusy(true);
    const result = await sendOperator({ path: "/admin/signals/approve", body: { signal_id: signal._id, approved_by: APPROVER } });
    setBusy(false);
    pushToast("Approve the recall", result);
    onDone();
  };
  return (
    <div className="signal-card" role="group" aria-label={`Signal ${signal._id} proposed`}>
      <div className="row">
        <span className="chip warn">Signal proposed</span>
        <span className="num">{signal._id}</span>
      </div>
      <p>
        <b>{signal.rule}</b> · batch {signal.batch_code}
      </p>
      <p>
        {fmt(signal.events.length)} failed tests at: <b>{signal.sites.join(", ")}</b>
      </p>
      <p>
        Shipped to: <b>{signal.shipped_to.join(", ")}</b>
      </p>
      {admin ? (
        <button type="button" className="btn primary" disabled={busy} onClick={approve}
          aria-label={`Approve signal ${signal._id} and open the recall, as ${APPROVER}`}>
          {busy ? "Approving…" : "Approve the recall"}
        </button>
      ) : (
        <p>A person at the supplier approves it. No agent can.</p>
      )}
    </div>
  );
}

interface Props {
  monitoring: MonitoringData | null;
  admin: boolean;
  onAction: () => void;
}

export default function Monitoring({ monitoring, admin, onAction }: Props) {
  const m = monitoring;
  const proposed = m?.signals.filter((s) => s.status === "proposed") ?? [];
  const approved = m?.signals.filter((s) => s.status === "approved") ?? [];
  const last = m?.last_event ?? null;
  return (
    <section className="panel glass" aria-labelledby="monitoring-title">
      <div className="panel-head">
        <h2 id="monitoring-title" className="panel-title">Monitoring</h2>
        <span className="chip sim">Simulated panels</span>
      </div>
      <div className="mon-stats">
        <div className="mon-stat">
          <span className="label">Connected</span>
          <div className="value num">{m ? fmt(m.connected) : "—"}</div>
        </div>
        <div className={`mon-stat ${m && m.failing > 0 ? "bad" : ""}`}>
          <span className="label">Failing</span>
          <div className="value num">{m ? fmt(m.failing) : "—"}</div>
        </div>
        <div className={`mon-stat ${m && m.passed_after_fix > 0 ? "good" : ""}`}>
          <span className="label">Passed after fix</span>
          <div className="value num">{m ? fmt(m.passed_after_fix) : "—"}</div>
        </div>
      </div>
      {m && m.panels.length > 0 && (
        <div className="panel-grid" role="list" aria-label="Connected panels">
          {m.panels.map((p) => (
            <span key={p.serial} role="listitem" className={panelClass(p)} title={panelText(p)} aria-label={panelText(p)} />
          ))}
        </div>
      )}
      <div className="mon-last">
        <span className="num">{last ? last.sim_label : "—"}</span>
        <span>
          {last
            ? `${last.serial} ${last.result.toUpperCase()} on ${last.sw_version}${last.effect ? ` · ${last.effect}` : ` · ${last.site_label}`}`
            : "No panel event yet"}
        </span>
      </div>
      {proposed.map((s) => <SignalCard key={s._id} signal={s} admin={admin} onDone={onAction} />)}
      {approved.map((s) => (
        <div key={s._id} className="signal-line">
          Signal <b className="num">{s._id}</b>: {s.events.length} failed tests at {s.sites.length} sites, approved by{" "}
          <b>{s.approved_by}</b>
          {s.recall_id && <> → recall <b className="num">{s.recall_id}</b></>}
        </div>
      ))}
    </section>
  );
}
