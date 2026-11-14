import type { SystemView, WallState } from "../api/types";
import { ADAPTER_SOURCE_TEXT, STATUS_TEXT, fmt, systemTone } from "./format";

function OpsCounts({ ops }: { ops: NonNullable<SystemView["operations"]> }) {
  const inFlight = ops.intent + ops.sent;
  return (
    <span className="ops-counts" aria-label={`Operations: ${inFlight} sent, ${ops.verified} verified by read-back`}>
      <span className="op-count sent" title="Sent to the order system, not yet confirmed by a read-back">
        <span className="label">Sent</span>
        <b>{fmt(inFlight)}</b>
      </span>
      <span className="op-count verified" title="A fresh read of the order system shows the effect">
        <span className="label">Verified</span>
        <b>{fmt(ops.verified)}</b>
      </span>
      {ops.needs_reverify > 0 && (
        <span className="op-count reverify" title="Flagged by a contract failure: read back again before any resend">
          <span className="label">Reverify</span>
          <b>{fmt(ops.needs_reverify)}</b>
        </span>
      )}
      {ops.failed > 0 && (
        <span className="op-count failed">
          <span className="label">Failed</span>
          <b>{fmt(ops.failed)}</b>
        </span>
      )}
    </span>
  );
}

function SystemRow({ sys }: { sys: SystemView }) {
  const tone = systemTone(sys.status);
  const orderSystem = (sys.system === "north" || sys.system === "east") && sys.adapter_version !== undefined;
  return (
    <li className={`sys-row${orderSystem ? " order" : ""}`}>
      <span className="sys-name">{sys.label}</span>
      {orderSystem ? (
        <span className="sys-detail">
          adapter <b>v{sys.adapter_version}</b>
          {sys.adapter_source && <> · {ADAPTER_SOURCE_TEXT[sys.adapter_source]}</>}
        </span>
      ) : (
        <span className="sys-detail" title={sys.detail}>{sys.detail}</span>
      )}
      <span className={`chip ${tone}`}>{STATUS_TEXT[sys.status]}</span>
      {sys.operations && <OpsCounts ops={sys.operations} />}
    </li>
  );
}

export default function Systems({ state }: { state: WallState | null }) {
  const systems = state?.systems ?? [];
  const ablation = state?.memory_ablation ?? null;
  const calls = state?.calls;
  return (
    <section className="panel glass" aria-labelledby="systems-title">
      <div className="panel-head">
        <h2 id="systems-title" className="panel-title">Systems</h2>
        {calls && (
          <span className="panel-meta num">
            calls {fmt(calls.real)} real · {fmt(calls.simulated)} sim
          </span>
        )}
      </div>
      <ul className="sys-list">
        {systems.map((s) => <SystemRow key={s.system} sys={s} />)}
      </ul>
      {ablation && (
        <div className="memory-line" title="Contacts needed to find every party, with memory and without it (the memory proof)">
          <span className="label">Memory</span> <b className="num">{ablation.with_memory}</b> contacts with ·{" "}
          <b className="num">{ablation.without_memory}</b> without
        </div>
      )}
    </section>
  );
}
