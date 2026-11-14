import { useState } from "react";
import { forgetAdminKey, panelPageUrl, sendOperator } from "../api/client";
import type { OperatorAction, WallState } from "../api/types";
import { useUi } from "./store";

const SLICE_VOICE_INSTALLER = "Harbour Fire";

/** The live slice's connected panel: Corner Properties' panel, else one linked to a slice party. */
function demoPanel(state: WallState): string | null {
  const panels = state.monitoring.panels;
  const slice = new Set(state.parties.filter((p) => p.slice).map((p) => p.party_id));
  const pick = panels.find((p) => p.site_label.startsWith("Corner Properties")) ??
    panels.find((p) => p.party_id !== null && slice.has(p.party_id)) ??
    panels.find((p) => p.fault && !p.fixed) ??
    panels[0];
  return pick?.serial ?? null;
}

interface ButtonSpec {
  id: string;
  label: string;
  sub: string;
  tone?: "danger" | "primary";
  armed?: boolean;
  disabled?: boolean;
  describe: string;
  run: () => Promise<void> | void;
}

export default function Operator({ state, onAction }: { state: WallState; onAction: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const pushToast = useUi((s) => s.pushToast);
  const selected = useUi((s) => s.selected);

  const send = async (id: string, title: string, action: OperatorAction) => {
    setBusy(id);
    try {
      pushToast(title, await sendOperator(action));
    } finally {
      setBusy(null);
      onAction();
    }
  };

  const harbour = state.parties.find((p) => p.name === SLICE_VOICE_INSTALLER) ??
    state.parties.find((p) => p.slice && p.type === "installer" && p.channel === "voice");
  const north = state.systems.find((s) => s.system === "north");
  const serial = demoPanel(state);
  const recallId = state.recall?._id ?? null;
  const saboteurOn = state.run?.saboteur.on ?? false;
  const liar = selected ? state.parties.find((p) => p.party_id === selected) : undefined;

  const buttons: ButtonSpec[] = [
    {
      id: "release", label: "Release Harbour Fire", sub: harbour?.hold_for_demo ? "held · the judge's call" : "not held",
      disabled: !harbour, describe: "Release Harbour Fire to the timers: a worker calls the judge's phone",
      run: async () => {
        if (harbour) await send("release", "Release Harbour Fire", { path: "/admin/release", body: { party_id: harbour.party_id } });
      },
    },
    {
      id: "failpoint", label: state.failpoint ? "Disarm failpoint" : "Arm failpoint", armed: !!state.failpoint,
      sub: state.failpoint ? `armed: ${state.failpoint}` : "kill · after the dial request", tone: "danger",
      describe: "Arm the failpoint: the next worker to dial dies after requesting the call, before saving it",
      run: () => send("failpoint", state.failpoint ? "Disarm the failpoint" : "Arm the failpoint",
        { path: "/admin/failpoint", body: { name: state.failpoint ? null : "after_dial_request" } }),
    },
    {
      id: "north", label: "Switch North to v2", tone: "danger",
      sub: north?.adapter_version && north.adapter_version >= 2 ? "break · adapter already v2" : "break the order system",
      describe: "Switch Distributor North's order system to v2: the contract fails and the adapter is repaired",
      run: () => send("north", "Switch North to v2", { path: "/admin/order-system", body: { system: "north", version: 2 } }),
    },
    {
      id: "saboteur", label: saboteurOn ? "Stop saboteur" : "Saboteur 20 s", armed: saboteurOn, tone: "danger",
      sub: saboteurOn ? "breaking rules now" : "a worker that breaks rules",
      describe: "Run the saboteur worker for 20 seconds: the gate and Atlas refuse it",
      run: () => send("saboteur", saboteurOn ? "Stop the saboteur" : "Saboteur for 20 s",
        { path: "/admin/saboteur", body: saboteurOn ? { on: false } : { on: true, seconds: 20 } }),
    },
    {
      id: "panel", label: "Open the panel page", sub: serial ? `${serial} ↗` : "no connected panel", disabled: !serial,
      describe: "Open the simulated panel page in a new tab",
      run: () => {
        if (serial) window.open(panelPageUrl(serial), "_blank", "noopener");
      },
    },
    {
      id: "close", label: "Close recall", sub: "ask Atlas for all clear", tone: "primary", disabled: !recallId,
      describe: "Try to close the recall: Atlas refuses while any panel is open",
      run: async () => {
        if (recallId) await send("close", "Close the recall", { path: "/admin/close", body: { recall_id: recallId } });
      },
    },
    {
      id: "lie", label: "Lie to it", sub: liar ? `over-report for ${liar.name}` : "over-report for Harbour Fire", tone: "danger",
      disabled: !recallId, describe: "Submit an over-report through the claim path: Atlas refuses it",
      run: () => send("lie", "Lie to it", { path: "/admin/lie", body: liar ? { party_id: liar.party_id } : {} }),
    },
    {
      id: "kill", label: "Kill a worker", sub: "SIGKILL · restart in 3 s", tone: "danger",
      describe: "Kill one worker now: its lease drains and another worker adopts its party",
      run: () => send("kill", "Kill a worker", { path: "/admin/kill", body: {} }),
    },
  ];

  return (
    <nav className="ops glass" aria-label="Operator controls">
      <div className="ops-id">
        <span className="label">Operator</span>
        <button type="button" className="link-btn" onClick={() => { forgetAdminKey(); onAction(); }}>
          forget key
        </button>
      </div>
      <div className="ops-buttons">
        {buttons.map((b) => (
          <button key={b.id} type="button"
            className={`btn op-btn${b.tone ? ` ${b.tone}` : ""}${b.armed ? " armed" : ""}`}
            disabled={b.disabled || busy !== null} aria-busy={busy === b.id} title={b.describe}
            onClick={() => void b.run()}>
            <span className="op-main">
              {b.armed && <span className="dot warn" aria-hidden="true" />}
              {busy === b.id ? "Sending…" : b.label}
            </span>
            <span className="op-sub">{b.sub}</span>
          </button>
        ))}
      </div>
    </nav>
  );
}
