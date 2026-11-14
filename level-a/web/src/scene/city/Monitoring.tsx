// Live monitoring in the city: the 24 connected panels as antennas on the roofs of their buildings
// (or on an anonymous roof until the owner is found and linked), the panel service beside the
// dispatch, a pulse for every new panel event (red for a fail, green for a pass), and the signal:
// red threads from the failing sites converging on the batch at the plant; once a person approves
// it, the threads turn gold and the recall goes out along the shipment arcs.

import { Line } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { Line2 } from "three-stdlib";
import type { WallState } from "../../api/types";
import { COLORS } from "../theme";
import { arcControl, type FxPool } from "./Fx";
import { quadPoint, signalSources, type BeaconPlace, type CityLayout, type V3 } from "./layout";
import { linear } from "./materials";
import { latestSignal } from "./labelContent";

const CAP = 64;

function beaconColor(b: BeaconPlace): string {
  const p = b.panel;
  if (p.fixed) return COLORS.good;
  if (p.last_result === "fail") return COLORS.bad;
  if (p.last_result === "pass") return COLORS.installed;
  return "#8fa3bd";
}

function ServiceNode({ at }: { at: V3 }) {
  const dish = useRef<THREE.Group>(null);
  const mats = useMemo(
    () => ({
      body: new THREE.MeshStandardMaterial({ color: "#2b333e", roughness: 0.45, metalness: 0.6 }),
      dish: new THREE.MeshStandardMaterial({ color: "#c9d3df", roughness: 0.35, metalness: 0.4, side: THREE.DoubleSide }),
      glow: new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.good).multiplyScalar(2.2) }),
    }),
    [],
  );
  useEffect(() => () => Object.values(mats).forEach((m) => m.dispose()), [mats]);
  useFrame((_, dt) => {
    if (dish.current) dish.current.rotation.y += dt * 0.25;
  });
  return (
    <group position={[at[0], 0, at[2]]}>
      <mesh material={mats.body} position={[0, 0.5, 0]} castShadow receiveShadow>
        <boxGeometry args={[2.2, 1.0, 1.6]} />
      </mesh>
      <mesh material={mats.glow} position={[0, 0.75, 0.81]}>
        <boxGeometry args={[1.6, 0.06, 0.02]} />
      </mesh>
      <mesh material={mats.body} position={[0, 1.6, 0]} castShadow>
        <cylinderGeometry args={[0.1, 0.14, 1.4, 10]} />
      </mesh>
      <group ref={dish} position={[0, at[1], 0]}>
        <mesh material={mats.dish} rotation-x={-0.9}>
          <sphereGeometry args={[1.2, 28, 10, 0, Math.PI * 2, 0, 0.75]} />
        </mesh>
        <mesh material={mats.glow} position={[0, 0.55, 0.45]}>
          <icosahedronGeometry args={[0.12, 1]} />
        </mesh>
      </group>
    </group>
  );
}

function Beacons({ beacons, calm }: { beacons: BeaconPlace[]; calm: boolean }) {
  const { masts, tips } = useMemo(() => {
    const mast = new THREE.InstancedMesh(new THREE.BoxGeometry(0.05, 1, 0.05).translate(0, 0.5, 0), new THREE.MeshStandardMaterial({ color: "#9aa7b8", roughness: 0.4, metalness: 0.7 }), CAP);
    const tip = new THREE.InstancedMesh(new THREE.IcosahedronGeometry(0.15, 1), new THREE.MeshBasicMaterial({ color: new THREE.Color(2.4, 2.4, 2.4) }), CAP);
    tip.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(CAP * 3), 3);
    for (const m of [mast, tip]) {
      m.frustumCulled = false;
      m.count = 0;
    }
    return { masts: mast, tips: tip };
  }, []);
  useEffect(
    () => () => {
      for (const m of [masts, tips]) {
        m.geometry.dispose();
        (m.material as THREE.Material).dispose();
        m.dispose();
      }
    },
    [masts, tips],
  );
  useLayoutEffect(() => {
    const mat = new THREE.Matrix4();
    const n = Math.min(CAP, beacons.length);
    for (let i = 0; i < n; i++) {
      const b = beacons[i];
      const h = b.linked ? 1.15 : 0.9;
      mat.makeScale(1, h, 1).setPosition(b.x, b.y, b.z);
      masts.setMatrixAt(i, mat);
      mat.makeScale(b.linked ? 1 : 0.8, b.linked ? 1 : 0.8, b.linked ? 1 : 0.8).setPosition(b.x, b.y + h + 0.08, b.z);
      tips.setMatrixAt(i, mat);
    }
    masts.count = n;
    tips.count = n;
    masts.instanceMatrix.needsUpdate = true;
    tips.instanceMatrix.needsUpdate = true;
  }, [beacons, masts, tips]);
  useFrame(({ clock }) => {
    const col = tips.instanceColor as THREE.InstancedBufferAttribute;
    const blink = calm || Math.sin(clock.elapsedTime * 5) > -0.2;
    for (let i = 0; i < tips.count; i++) {
      const b = beacons[i];
      const c = linear(beaconColor(b));
      const failing = !b.panel.fixed && b.panel.last_result === "fail";
      const k = (b.linked ? 1 : 0.6) * (failing && !blink ? 0.2 : 1);
      col.setXYZ(i, c.r * k, c.g * k, c.b * k);
    }
    col.needsUpdate = true;
  });
  return (
    <>
      <primitive object={masts} />
      <primitive object={tips} />
    </>
  );
}

const tmp: V3 = [0, 0, 0];

function SignalThreads({ state, layout, fx, calm }: { state: WallState | null; layout: CityLayout; fx: FxPool; calm: boolean }) {
  const clock = useThree((s) => s.clock);
  const signal = latestSignal(state);
  const plate = layout.plant.plate;
  const threads = useMemo(() => {
    if (!signal || signal.status === "dismissed") return [];
    return signalSources(signal, layout.beacons).map((src, i) => {
      const mid = arcControl(src, plate, 6);
      const pts: THREE.Vector3[] = [];
      for (let k = 0; k <= 48; k++) {
        quadPoint(src, mid, plate, k / 48, tmp);
        pts.push(new THREE.Vector3(tmp[0], tmp[1], tmp[2]));
      }
      return { key: `${signal._id}:${i}`, src, mid, pts };
    });
  }, [signal, layout.beacons, plate]);

  const refs = useRef(new Map<string, Line2>());
  const red = useMemo(() => new THREE.Color(COLORS.bad).multiplyScalar(2.2), []);
  const gold = useMemo(() => new THREE.Color(COLORS.warn).multiplyScalar(1.1), []);
  const proposed = signal?.status === "proposed";

  // A signal appearing, and a signal being approved, are both moments worth marking.
  const previous = useRef<{ id: string; status: string } | null>(null);
  useEffect(() => {
    if (!signal) {
      previous.current = null;
      return;
    }
    const before = previous.current;
    const now = clock.elapsedTime;
    if (before && (before.id !== signal._id || before.status !== signal.status)) {
      if (signal.status === "proposed") {
        for (const t of threads) {
          fx.shock([t.src[0], t.src[1] - 1, t.src[2]], COLORS.bad, 3, now, 1.2);
          fx.comet(t.src, t.mid, plate, COLORS.bad, now, 1.6, 0.2);
        }
        fx.shock([plate[0], 0.4, plate[2]], COLORS.bad, 6, now + 1.6, 1.4);
      } else if (signal.status === "approved") {
        fx.shock([plate[0], 0.4, plate[2]], COLORS.warn, 9, now, 1.6);
        for (const s of layout.shipments) {
          for (let i = 0; i < 4; i++) fx.comet(s.p0, s.p1, s.p2, COLORS.warn, now + 0.3 + i * 0.15, 2.0, 0.2);
        }
      }
    }
    previous.current = { id: signal._id, status: signal.status };
  }, [signal, threads, fx, clock, plate, layout.shipments]);

  useFrame(({ clock: c }, dt) => {
    for (const t of threads) {
      const line = refs.current.get(t.key);
      if (!line) continue;
      if (proposed) {
        if (!calm) line.material.dashOffset -= dt * 2.2;
        line.material.color.copy(red).multiplyScalar(calm ? 1 : 0.7 + 0.5 * Math.sin(c.elapsedTime * 5));
      }
    }
  });

  return (
    <group>
      {threads.map((t) => (
        <Line
          key={t.key}
          ref={(l: Line2 | null) => {
            if (l) refs.current.set(t.key, l);
            else refs.current.delete(t.key);
          }}
          points={t.pts}
          color={proposed ? red : gold}
          lineWidth={proposed ? 2.6 : 1.4}
          transparent
          opacity={proposed ? 1 : 0.35}
          dashed={proposed}
          dashSize={0.9}
          gapSize={0.5}
          depthWrite={false}
        />
      ))}
    </group>
  );
}

function EventPulses({ state, layout, fx }: { state: WallState | null; layout: CityLayout; fx: FxPool }) {
  const clock = useThree((s) => s.clock);
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    const events = state?.monitoring?.events ?? [];
    if (seen.current === null) {
      seen.current = new Set(events.map((e) => e.event_id));
      return;
    }
    const fresh = events.filter((e) => !seen.current?.has(e.event_id)).slice(0, 8).reverse();
    const now = clock.elapsedTime;
    const svc = layout.dispatch.service;
    const dishTop: V3 = [svc[0], svc[1] + 0.6, svc[2]];
    fresh.forEach((e, i) => {
      seen.current?.add(e.event_id);
      const b = layout.beacons.find((x) => x.serial === e.serial);
      if (!b) return;
      const tip: V3 = [b.x, b.y + 1.3, b.z];
      const color = e.result === "fail" ? COLORS.bad : COLORS.good;
      const t0 = now + i * 0.35;
      fx.shock([b.x, b.y + 0.05, b.z], color, 1.6, t0, 0.9);
      fx.comet(tip, arcControl(tip, dishTop, 5), dishTop, color, t0, 1.7, 0.22);
      fx.shock([svc[0], 0.3, svc[2]], color, 3.2, t0 + 1.7, 1.0);
      if (e.effect && b.panel.party_id) {
        const place = layout.places.get(b.panel.party_id);
        if (place) {
          fx.shock([place.x, 0.3, place.z], COLORS.good, place.ring + 2, t0 + 0.2, 1.6);
          fx.shock([b.x, b.y + 0.3, b.z], COLORS.good, 2.6, t0 + 0.4, 1.2);
        }
      }
    });
  }, [state, layout, fx, clock]);
  return null;
}

export function Monitoring({ state, layout, fx, calm }: { state: WallState | null; layout: CityLayout; fx: FxPool; calm: boolean }) {
  return (
    <group>
      <ServiceNode at={layout.dispatch.service} />
      <Beacons beacons={layout.beacons} calm={calm} />
      <SignalThreads state={state} layout={layout} fx={fx} calm={calm} />
      <EventPulses state={state} layout={layout} fx={fx} />
    </group>
  );
}
