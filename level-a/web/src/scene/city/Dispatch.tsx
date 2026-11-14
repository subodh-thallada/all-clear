// All Clear dispatch: four identical worker towers (and the saboteur when it is on), a beam from each
// worker to the party it holds the lease on, a ring around that party that drains with the lease,
// and the phone ringing at parties with a live attempt (gold for a real call, cyan for simulated).
// A killed worker goes dark and red and its beam snaps; when another worker adopts the party a new
// beam appears and the party flashes.

import { QuadraticBezierLine } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { WallState, WorkerView } from "../../api/types";
import { COLORS } from "../theme";
import { arcControl, type FxPool } from "./Fx";
import type { CityLayout, PartyPlace, V3 } from "./layout";
import { linear, rippleMaterial, ringMaterial, type SceneUniforms } from "./materials";

const LEASE_SECONDS = 8;
const SABOTEUR = "#ff4fa3";

function towerIndex(w: WorkerView): number {
  if (w.saboteur) return 4;
  const n = parseInt(w.id.replace(/\D/g, ""), 10);
  return Number.isFinite(n) ? Math.max(0, Math.min(3, n - 1)) : 0;
}

function workerColor(w: WorkerView | undefined): string {
  if (!w) return "#55606f";
  if (!w.alive) return COLORS.bad;
  if (w.saboteur) return SABOTEUR;
  return w.party_id ? COLORS.live : "#8796ab";
}

function beamTarget(place: PartyPlace): V3 {
  const roof = Math.max(1.5, ...place.buildings.map((b) => b.roofY));
  return [place.x, Math.min(place.top, roof + 0.4), place.z];
}

function Tower({ at, worker, calm }: { at: V3; worker: WorkerView | undefined; calm: boolean }) {
  const crown = useRef<THREE.Mesh>(null);
  const mats = useMemo(
    () => ({
      body: new THREE.MeshStandardMaterial({ color: "#2a313c", roughness: 0.5, metalness: 0.55 }),
      glow: new THREE.MeshStandardMaterial({ color: "#10151b", emissive: new THREE.Color("#6cc4ff"), emissiveIntensity: 2.4, roughness: 0.4 }),
    }),
    [],
  );
  useEffect(() => () => Object.values(mats).forEach((m) => m.dispose()), [mats]);
  const color = workerColor(worker);
  const dead = !!worker && !worker.alive;
  useLayoutEffect(() => {
    mats.glow.emissive.set(color);
    mats.glow.emissiveIntensity = worker ? (worker.party_id || dead ? 2.6 : 0.9) : 0.25;
  }, [color, worker, dead, mats]);
  useFrame(({ clock }, dt) => {
    if (!crown.current) return;
    if (!calm && worker?.alive && worker.party_id) crown.current.rotation.z += dt * 1.6;
    if (dead) mats.glow.emissiveIntensity = calm ? 1.6 : 0.4 + 2.4 * (Math.sin(clock.elapsedTime * 7) > 0 ? 1 : 0);
  });
  const [x, y, z] = at;
  return (
    <group position={[x, 0, z]}>
      <mesh material={mats.body} position={[0, 0.25, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[0.85, 1.05, 0.5, 20]} />
      </mesh>
      <mesh material={mats.body} position={[0, 0.5 + (y - 0.9) / 2, 0]} castShadow>
        <boxGeometry args={[0.5, y - 0.9, 0.5]} />
      </mesh>
      <mesh material={mats.glow} position={[0, 0.5 + (y - 0.9) / 2, 0.255]}>
        <boxGeometry args={[0.1, y - 1.4, 0.02]} />
      </mesh>
      <mesh ref={crown} material={mats.glow} position={[0, y, 0]} rotation-x={Math.PI / 2}>
        <torusGeometry args={[0.8, 0.075, 10, 40, Math.PI * 1.6]} />
      </mesh>
      <mesh material={mats.glow} position={[0, y, 0]}>
        <icosahedronGeometry args={[0.26, 1]} />
      </mesh>
    </group>
  );
}

function Hub({ at }: { at: V3 }) {
  const mats = useMemo(
    () => ({
      base: new THREE.MeshStandardMaterial({ color: "#1b212a", roughness: 0.6, metalness: 0.4 }),
      ring: new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.live).multiplyScalar(1.6), transparent: true, opacity: 0.55 }),
    }),
    [],
  );
  useEffect(() => () => Object.values(mats).forEach((m) => m.dispose()), [mats]);
  return (
    <group position={[at[0], 0, at[2]]}>
      <mesh material={mats.base} position={[0, 0.2, 0]} receiveShadow>
        <cylinderGeometry args={[9.6, 9.8, 0.2, 64]} />
      </mesh>
      <mesh material={mats.ring} position={[0, 0.31, 0]} rotation-x={-Math.PI / 2}>
        <ringGeometry args={[9.25, 9.4, 96]} />
      </mesh>
      <mesh material={mats.ring} position={[0, 0.31, 0]} rotation-x={-Math.PI / 2}>
        <ringGeometry args={[2.6, 2.72, 64]} />
      </mesh>
      <mesh material={mats.base} position={[0, 0.6, 0]} castShadow>
        <cylinderGeometry args={[1.6, 1.9, 0.6, 32]} />
      </mesh>
    </group>
  );
}

interface LeaseMark {
  place: PartyPlace;
  remaining: number;
  expired: boolean;
  color: string;
}

function LeaseRings({ marks, uniforms }: { marks: LeaseMark[]; uniforms: SceneUniforms }) {
  const clock = useThree((s) => s.clock);
  const stamp = useRef(0);
  const mesh = useMemo(() => {
    const geo = new THREE.PlaneGeometry(2, 2).rotateX(-Math.PI / 2);
    geo.setAttribute("aRing", new THREE.InstancedBufferAttribute(new Float32Array(16 * 4), 4));
    geo.setAttribute("aColor", new THREE.InstancedBufferAttribute(new Float32Array(16 * 3), 3));
    const m = new THREE.InstancedMesh(geo, ringMaterial(uniforms), 16);
    m.frustumCulled = false;
    m.renderOrder = 4;
    m.count = 0;
    return m;
  }, [uniforms]);
  useEffect(
    () => () => {
      mesh.geometry.dispose();
      (mesh.material as THREE.Material).dispose();
      mesh.dispose();
    },
    [mesh],
  );
  useLayoutEffect(() => {
    stamp.current = clock.elapsedTime;
    const mat = new THREE.Matrix4();
    const col = mesh.geometry.getAttribute("aColor") as THREE.InstancedBufferAttribute;
    const n = Math.min(16, marks.length);
    for (let i = 0; i < n; i++) {
      const mk = marks[i];
      mat.makeScale(mk.place.ring, 1, mk.place.ring).setPosition(mk.place.x, 0.16, mk.place.z);
      mesh.setMatrixAt(i, mat);
      const c = linear(mk.expired ? COLORS.bad : mk.color);
      col.setXYZ(i, c.r, c.g, c.b);
    }
    mesh.count = n;
    mesh.instanceMatrix.needsUpdate = true;
    col.needsUpdate = true;
  }, [marks, mesh, clock]);
  useFrame(({ clock: c }) => {
    const ring = mesh.geometry.getAttribute("aRing") as THREE.InstancedBufferAttribute;
    const elapsed = c.elapsedTime - stamp.current;
    for (let i = 0; i < mesh.count; i++) {
      const mk = marks[i];
      const rem = mk.remaining - elapsed;
      const expired = mk.expired || rem <= 0;
      ring.setXYZW(i, expired ? 1 : Math.min(1, rem / LEASE_SECONDS), expired ? 1 : 0, 0.07, 1);
    }
    ring.needsUpdate = true;
  });
  return <primitive object={mesh} />;
}

interface CallMark {
  place: PartyPlace;
  real: boolean;
  state: "intent" | "dialed" | "connected";
}

function CallRipples({ calls, uniforms }: { calls: CallMark[]; uniforms: SceneUniforms }) {
  const mesh = useMemo(() => {
    const geo = new THREE.PlaneGeometry(2, 2).rotateX(-Math.PI / 2);
    geo.setAttribute("aCall", new THREE.InstancedBufferAttribute(new Float32Array(24 * 4), 4));
    geo.setAttribute("aColor", new THREE.InstancedBufferAttribute(new Float32Array(24 * 3), 3));
    const m = new THREE.InstancedMesh(geo, rippleMaterial(uniforms), 24);
    m.frustumCulled = false;
    m.renderOrder = 4;
    m.count = 0;
    return m;
  }, [uniforms]);
  useEffect(
    () => () => {
      mesh.geometry.dispose();
      (mesh.material as THREE.Material).dispose();
      mesh.dispose();
    },
    [mesh],
  );
  useLayoutEffect(() => {
    const mat = new THREE.Matrix4();
    const call = mesh.geometry.getAttribute("aCall") as THREE.InstancedBufferAttribute;
    const col = mesh.geometry.getAttribute("aColor") as THREE.InstancedBufferAttribute;
    let n = 0;
    for (const c of calls) {
      if (n >= 23) break;
      const color = linear(c.real ? COLORS.real : COLORS.live);
      // speed, density, strength, phase
      const style: [number, number, number] =
        c.state === "dialed" ? [1.25, 3.2, 1.25] : c.state === "connected" ? [0.45, 2.2, 1.05] : [0.3, 1.6, 0.5];
      const phase = (c.place.x * 0.37 + c.place.z * 0.21) % 1;
      // A halo over the roof, and a wider one on the lot.
      const top = c.place.top + 0.35;
      mat.makeScale(3.4, 1, 3.4).setPosition(c.place.x, top, c.place.z);
      mesh.setMatrixAt(n, mat);
      call.setXYZW(n, style[0], style[1], style[2], phase);
      col.setXYZ(n, color.r, color.g, color.b);
      n++;
      mat.makeScale(c.place.ring * 1.25, 1, c.place.ring * 1.25).setPosition(c.place.x, 0.12, c.place.z);
      mesh.setMatrixAt(n, mat);
      call.setXYZW(n, style[0] * 0.8, style[1] * 0.8, style[2] * 0.7, phase + 0.5);
      col.setXYZ(n, color.r, color.g, color.b);
      n++;
    }
    mesh.count = n;
    mesh.instanceMatrix.needsUpdate = true;
    call.needsUpdate = true;
    col.needsUpdate = true;
  }, [calls, mesh]);
  return <primitive object={mesh} />;
}

interface Beam {
  key: string;
  from: V3;
  mid: V3;
  to: V3;
  color: string;
}

export function Dispatch({ state, layout, uniforms, fx, calm }: { state: WallState | null; layout: CityLayout; uniforms: SceneUniforms; fx: FxPool; calm: boolean }) {
  const clock = useThree((s) => s.clock);
  const workers = useMemo(() => state?.workers ?? [], [state]);
  const byTower = useMemo(() => {
    const out: (WorkerView | undefined)[] = [undefined, undefined, undefined, undefined, undefined];
    for (const w of workers) out[towerIndex(w)] = w;
    return out;
  }, [workers]);
  const showSaboteur = !!byTower[4] || !!state?.run?.saboteur.on;

  const beams = useMemo<Beam[]>(() => {
    const out: Beam[] = [];
    for (const w of workers) {
      if (!w.alive || !w.party_id) continue;
      const place = layout.places.get(w.party_id);
      if (!place) continue;
      const from = layout.dispatch.towers[towerIndex(w)];
      const to = beamTarget(place);
      out.push({ key: `${w.id}>${w.party_id}`, from, mid: arcControl(from, to, 3), to, color: w.saboteur ? SABOTEUR : COLORS.live });
    }
    return out;
  }, [workers, layout]);

  const leases = useMemo<LeaseMark[]>(() => {
    const out: LeaseMark[] = [];
    for (const place of layout.order) {
      const lease = place.party.lease;
      if (!lease) continue;
      const holder = workers.find((w) => w.id === lease.owner);
      out.push({ place, remaining: lease.remaining_s, expired: lease.expired, color: holder?.saboteur ? SABOTEUR : COLORS.live });
    }
    return out;
  }, [layout, workers]);

  const calls = useMemo<CallMark[]>(() => {
    const out: CallMark[] = [];
    const seen = new Set<string>();
    for (const c of state?.calls_in_flight ?? []) {
      const place = layout.places.get(c.party_id);
      if (!place || seen.has(c.party_id)) continue;
      seen.add(c.party_id);
      out.push({ place, real: c.real, state: c.state });
    }
    for (const place of layout.order) {
      const a = place.party.live_attempt;
      if (!a || seen.has(place.id)) continue;
      seen.add(place.id);
      out.push({ place, real: a.real, state: a.state });
    }
    return out;
  }, [state, layout]);

  // Kills and adoptions: compare with what the previous state said.
  const prevWorkers = useRef(new Map<string, WorkerView>());
  const prevOwners = useRef(new Map<string, string>());
  useEffect(() => {
    const now = clock.elapsedTime;
    for (const w of workers) {
      const before = prevWorkers.current.get(w.id);
      if (before && before.alive && !w.alive) {
        const tower = layout.dispatch.towers[towerIndex(w)];
        fx.shock([tower[0], 0.4, tower[2]], COLORS.bad, 4.5, now, 1.1);
        fx.shock([tower[0], tower[1], tower[2]], COLORS.bad, 2.4, now, 0.7);
        const place = before.party_id ? layout.places.get(before.party_id) : undefined;
        if (place) {
          const to = beamTarget(place);
          const mid = arcControl(tower, to, 3);
          // The beam snaps: sparks fall from the break and the stub recoils to the tower.
          const brk: V3 = [0.25 * tower[0] + 0.5 * mid[0] + 0.25 * to[0], 0.25 * tower[1] + 0.5 * mid[1] + 0.25 * to[1], 0.25 * tower[2] + 0.5 * mid[2] + 0.25 * to[2]];
          for (let i = 0; i < 10; i++) {
            const a = (i / 10) * Math.PI * 2;
            const land: V3 = [brk[0] + Math.cos(a) * (2 + (i % 3)), 0.2, brk[2] + Math.sin(a) * (2 + (i % 3))];
            fx.comet(brk, [brk[0] + Math.cos(a) * 1.2, brk[1] + 1.5, brk[2] + Math.sin(a) * 1.2], land, COLORS.bad, now + i * 0.02, 0.9, 0.12);
          }
          fx.comet(brk, mid, tower, COLORS.bad, now, 0.45, 0.2);
          fx.comet(brk, [(brk[0] + to[0]) / 2, brk[1] + 0.5, (brk[2] + to[2]) / 2], to, COLORS.bad, now, 0.45, 0.2);
        }
      }
      prevWorkers.current.set(w.id, w);
    }
    for (const place of layout.order) {
      const owner = place.party.lease?.owner;
      const before = prevOwners.current.get(place.id);
      if (owner && before && owner !== before) {
        const w = workers.find((x) => x.id === owner);
        fx.shock([place.x, 0.3, place.z], w?.saboteur ? SABOTEUR : COLORS.live, place.ring + 1.5, now, 1.3);
        fx.shock([place.x, 0.3, place.z], "#ffffff", place.ring * 0.7, now + 0.15, 0.9);
      }
      if (owner) prevOwners.current.set(place.id, owner);
      else prevOwners.current.delete(place.id);
    }
  }, [workers, layout, fx, clock]);

  // Work flowing out along each beam.
  const nextSpark = useRef(0);
  useFrame(({ clock: c }) => {
    if (calm || beams.length === 0) return;
    const now = c.elapsedTime;
    if (now < nextSpark.current) return;
    nextSpark.current = now + 0.42;
    for (const b of beams) fx.comet(b.from, b.mid, b.to, b.color, now, 1.25, 0.13);
  });

  return (
    <group>
      <Hub at={layout.dispatch.hub} />
      {layout.dispatch.towers.slice(0, showSaboteur ? 5 : 4).map((t, i) => (
        <Tower key={i} at={t} worker={byTower[i]} calm={calm} />
      ))}
      {beams.map((b) => (
        <QuadraticBezierLine key={b.key} start={b.from} mid={b.mid} end={b.to} color={b.color} lineWidth={2.2} transparent opacity={0.85} toneMapped={false} />
      ))}
      <LeaseRings marks={leases} uniforms={uniforms} />
      <CallRipples calls={calls} uniforms={uniforms} />
    </group>
  );
}
