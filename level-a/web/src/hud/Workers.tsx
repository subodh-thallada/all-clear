import type { Party, WallState, WorkerView } from "../api/types";
import { clockDuration, fmt } from "./format";
import { useUi } from "./store";

const LEASE_SECONDS = 8;

function leaseFraction(remaining: number): number {
  return Math.max(0, Math.min(1, remaining / Math.max(LEASE_SECONDS, remaining)));
}

function WorkerRow({ w, party }: { w: WorkerView; party: Party | undefined }) {
  const select = useUi((s) => s.select);
  const lease = party?.lease && party.lease.owner === w.id ? party.lease : null;
  const status = !w.alive ? "Killed" : w.saboteur ? "Saboteur" : w.party_id ? "Working" : "Idle";
  const cls = !w.alive ? "dead" : w.saboteur ? "saboteur" : "";
  return (
    <li className={`worker-row ${cls}`} title={`Worker ${w.id}: ${status}`}>
      <span className="w-id">
        <span className={`dot ${!w.alive ? "bad" : w.saboteur ? "warn" : w.party_id ? "good" : ""}`} />
        {w.id.replace(" (saboteur)", "")}
      </span>
      <div className="w-main">
        {w.party_id && w.party_name ? (
          <div className="w-party">
            <span>
              <button type="button" className="party-link" onClick={() => select(w.party_id)}>
                {w.party_name}
              </button>
            </span>
            {!w.alive && <span className="chip bad">Killed</span>}
            {party && !party.simulated && <span className="chip real">Real</span>}
          </div>
        ) : (
          <div className={`w-party ${w.alive ? "idle" : ""}`}>
            <span>{w.alive ? (w.saboteur ? "breaking rules on purpose" : "waiting for due work") : "down"}</span>
            {!w.alive && <span className="chip bad">Killed</span>}
          </div>
        )}
        <div className="w-meta">
          {[w.kind, w.lease_token !== null ? `token ${w.lease_token}` : null, w.since_s !== null ? clockDuration(w.since_s) : null,
            w.restarts ? `restarts ${w.restarts}` : null].filter(Boolean).join(" · ") || "no lease"}
        </div>
      </div>
      <div className="w-lease" aria-hidden={!lease}>
        {lease ? (
          <>
            <div className="meter" title={lease.expired ? "Lease expired: another worker adopts the party" : "Lease remaining"}>
              <span style={{ width: `${leaseFraction(lease.remaining_s) * 100}%` }} />
            </div>
            <span className="num">{lease.expired ? "expired" : `${lease.remaining_s.toFixed(1)} s`}</span>
          </>
        ) : (
          <span className="num muted">
            —
          </span>
        )}
      </div>
    </li>
  );
}

export default function Workers({ state }: { state: WallState | null }) {
  const workers = state?.workers ?? [];
  const byId = new Map((state?.parties ?? []).map((p) => [p.party_id, p]));
  const inFlight = state?.calls_in_flight ?? [];
  const real = inFlight.filter((c) => c.real).length;
  const alive = workers.filter((w) => w.alive && !w.saboteur).length;
  const select = useUi((s) => s.select);
  return (
    <section className="panel glass grow" aria-labelledby="workers-title">
      <div className="panel-head">
        <h2 id="workers-title" className="panel-title">Workers</h2>
        <span className="panel-meta">
          {fmt(alive)} alive · leases on Atlas
        </span>
      </div>
      <div className="scroll">
        <ul className="worker-list">
          {workers.map((w) => <WorkerRow key={w.id} w={w} party={w.party_id ? byId.get(w.party_id) : undefined} />)}
        </ul>
        <div className="calls-head">
          <span className="label">Calls in flight</span>
          <span className="panel-meta">
            <span className="chip real">{real} real</span> <span className="chip sim">{inFlight.length - real} simulated</span>
          </span>
        </div>
        {inFlight.length ? (
          <ul className="call-list">
            {inFlight.map((c) => (
              <li key={c.attempt_id} className={`call-row ${c.real ? "real" : "sim"}`}>
                <span className={`dot ${c.real ? "real" : "sim"}`} aria-hidden="true" />
                <button type="button" className="party-link party"
                  onClick={() => select(c.party_id)}>
                  {c.party_name}
                </button>
                <span className="state">{c.real ? "real call" : "simulated"} · {c.state}</span>
                <span className="num">{clockDuration(c.since_s)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="calls-empty">No call in flight</div>
        )}
      </div>
    </section>
  );
}
