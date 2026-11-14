// Transfers between parties as arcs: awaiting the recipient = a dashed line running toward the
// recipient, corroborated = a calm solid line, disputed = a pulsing red line (its panels hover at the
// apex, see layout). The supplier's direct shipments arc from the batch plate. When a party returns
// panels, they travel home to the plant as blue comets.

import { Line } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { Line2 } from "three-stdlib";
import type { WallState } from "../../api/types";
import { COLORS } from "../theme";
import { arcControl, type FxPool } from "./Fx";
import { quadPoint, type CityLayout, type V3 } from "./layout";

const SEGMENTS = 40;

function sample(p0: V3, p1: V3, p2: V3): THREE.Vector3[] {
  const out: THREE.Vector3[] = [];
  const tmp: V3 = [0, 0, 0];
  for (let i = 0; i <= SEGMENTS; i++) {
    quadPoint(p0, p1, p2, i / SEGMENTS, tmp);
    out.push(new THREE.Vector3(tmp[0], tmp[1], tmp[2]));
  }
  return out;
}

type Style = "awaiting" | "corroborated" | "disputed" | "shipment";

const STYLE: Record<Style, { color: string; gain: number; width: number; opacity: number; dashed: boolean }> = {
  awaiting: { color: COLORS.transit, gain: 1.5, width: 1.7, opacity: 0.9, dashed: true },
  corroborated: { color: "#4fd99a", gain: 0.9, width: 1.3, opacity: 0.5, dashed: false },
  disputed: { color: COLORS.disputed, gain: 2.2, width: 3.2, opacity: 1, dashed: false },
  shipment: { color: "#ffb07a", gain: 0.9, width: 1.2, opacity: 0.4, dashed: false },
};

interface Arc {
  key: string;
  style: Style;
  points: THREE.Vector3[];
  ends: [string, string | null];
}

export function Edges({ layout, focus, calm }: { layout: CityLayout; focus: string | null; calm: boolean }) {
  const arcs = useMemo<Arc[]>(() => {
    const out: Arc[] = layout.edges.map((e) => ({
      key: e.key,
      style: e.edge.state === "disputed" ? "disputed" : e.edge.state === "awaiting_recipient" ? "awaiting" : "corroborated",
      points: sample(e.p0, e.p1, e.p2),
      ends: [e.from, e.to],
    }));
    for (const s of layout.shipments) out.push({ key: s.key, style: "shipment", points: sample(s.p0, s.p1, s.p2), ends: [s.to, null] });
    return out;
  }, [layout]);

  const refs = useRef(new Map<string, Line2>());
  const base = useMemo(() => new Map<Style, THREE.Color>((Object.keys(STYLE) as Style[]).map((k) => [k, new THREE.Color(STYLE[k].color).multiplyScalar(STYLE[k].gain)])), []);

  useFrame(({ clock }, dt) => {
    const t = clock.elapsedTime;
    for (const a of arcs) {
      const line = refs.current.get(a.key);
      if (!line) continue;
      const mat = line.material;
      if (a.style === "awaiting" && !calm) mat.dashOffset -= dt * 1.4;
      if (a.style === "disputed") {
        const k = calm ? 1 : 0.65 + 0.55 * (0.5 + 0.5 * Math.sin(t * 4.2));
        mat.color.copy(base.get("disputed") as THREE.Color).multiplyScalar(k);
      }
    }
  });

  return (
    <group>
      {arcs.map((a) => {
        const st = STYLE[a.style];
        const touches = focus !== null && (a.ends[0] === focus || a.ends[1] === focus);
        const dim = focus !== null && !touches;
        return (
          <Line
            key={a.key}
            ref={(l: Line2 | null) => {
              if (l) refs.current.set(a.key, l);
              else refs.current.delete(a.key);
            }}
            points={a.points}
            color={base.get(a.style)}
            lineWidth={touches ? st.width + 1.2 : st.width}
            transparent
            opacity={dim ? st.opacity * 0.3 : touches ? 1 : st.opacity}
            dashed={st.dashed}
            dashSize={0.7}
            gapSize={0.45}
            depthWrite={false}
          />
        );
      })}
    </group>
  );
}

/** Panels a party returns travel to the plant's returns yard. */
export function ReturnFlows({ state, layout, fx }: { state: WallState | null; layout: CityLayout; fx: FxPool }) {
  const clock = useThree((s) => s.clock);
  const previous = useRef<Map<string, number> | null>(null);
  useEffect(() => {
    const now = clock.elapsedTime;
    const next = new Map<string, number>();
    for (const p of state?.parties ?? []) {
      next.set(p.party_id, p.returned);
      const before = previous.current?.get(p.party_id);
      if (before === undefined || p.returned <= before) continue;
      const place = layout.places.get(p.party_id);
      if (!place) continue;
      const from: V3 = [place.x, 1.4, place.z];
      const to: V3 = [layout.plant.yard[0], 1.0, layout.plant.yard[2]];
      const mid = arcControl(from, to, 5);
      const n = Math.min(14, p.returned - before);
      for (let i = 0; i < n; i++) fx.comet(from, mid, to, COLORS.returned, now + i * 0.12, 2.2, 0.18);
      fx.shock([layout.plant.yard[0], 0.3, layout.plant.yard[2]], COLORS.returned, 5, now + 2.2, 1.0);
    }
    previous.current = next;
  }, [state, layout, fx, clock]);
  return null;
}
