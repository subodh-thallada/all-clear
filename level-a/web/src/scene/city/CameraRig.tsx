// The camera: frames the whole recall inside the part of the screen the HUD leaves free (the
// projection is shifted with setViewOffset, so the centre of attention is the centre of the free
// area), flies to a party when it is selected and back when it is cleared, drifts gently while
// nobody touches it, and hands control to OrbitControls the moment someone does.

import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import type { Insets } from "./Labels";
import type { CityLayout, PartyPlace } from "./layout";

const AZIMUTH = -0.3;
const ELEVATION = 0.64;
const FOCUS_ELEVATION = 0.58;
const RETURN_AFTER_S = 60;

interface View {
  pos: THREE.Vector3;
  target: THREE.Vector3;
}

const probe = new THREE.Vector3();

/** Smallest distance at which every corner of the city's box projects inside the free area. */
function fitOverview(layout: CityLayout, camera: THREE.PerspectiveCamera, width: number, height: number, insets: Insets): View {
  const b = layout.bounds;
  const target = new THREE.Vector3((b.x0 + b.x1) / 2, 2, (b.z0 + b.z1) / 2);
  const dir = new THREE.Vector3(Math.sin(AZIMUTH) * Math.cos(ELEVATION), Math.sin(ELEVATION), Math.cos(AZIMUTH) * Math.cos(ELEVATION));
  const h = layout.dispatch.headline;
  const corners: [number, number, number][] = [
    [b.x0, 0, b.z0],
    [b.x1, 0, b.z0],
    [b.x0, 0, b.z1],
    [b.x1, 0, b.z1],
    [b.x0, b.top, b.z0],
    [b.x1, b.top, b.z0],
    [h[0] - 23, h[1] + 4.6, h[2]],
    [h[0] + 23, h[1] + 4.6, h[2]],
  ];
  const cam = camera.clone();
  cam.aspect = width / Math.max(1, height);
  cam.setViewOffset(width, height, -(insets.left - insets.right) / 2, -(insets.top - insets.bottom) / 2, width, height);
  const fx0 = insets.left + 24;
  const fx1 = width - insets.right - 24;
  const fy0 = insets.top + 24;
  const fy1 = height - insets.bottom - 24;
  const fits = (d: number) => {
    cam.position.copy(target).addScaledVector(dir, d);
    cam.lookAt(target);
    cam.updateMatrixWorld();
    cam.updateProjectionMatrix();
    for (const c of corners) {
      probe.set(c[0], c[1], c[2]).project(cam);
      const sx = (probe.x * 0.5 + 0.5) * width;
      const sy = (-probe.y * 0.5 + 0.5) * height;
      if (probe.z > 1 || sx < fx0 || sx > fx1 || sy < fy0 || sy > fy1) return false;
    }
    return true;
  };
  let lo = 20;
  let hi = 600;
  for (let i = 0; i < 26; i++) {
    const mid = (lo + hi) / 2;
    if (fits(mid)) hi = mid;
    else lo = mid;
  }
  return { pos: target.clone().addScaledVector(dir, hi), target };
}

function focusView(place: PartyPlace, from: THREE.Vector3, target: THREE.Vector3): View {
  const t = new THREE.Vector3(place.x, Math.min(4, place.top * 0.35), place.z);
  const flat = new THREE.Vector3(from.x - target.x, 0, from.z - target.z);
  if (flat.lengthSq() < 1e-4) flat.set(Math.sin(AZIMUTH), 0, Math.cos(AZIMUTH));
  flat.normalize();
  const d = 30 + place.top * 1.1;
  const pos = t.clone().addScaledVector(flat, Math.cos(FOCUS_ELEVATION) * d);
  pos.y += Math.sin(FOCUS_ELEVATION) * d;
  return { pos, target: t };
}

function easeInOut(t: number) {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

export function CameraRig({ layout, selected, insets, calm }: { layout: CityLayout; selected: string | null; insets: Insets; calm: boolean }) {
  const controls = useRef<OrbitControlsImpl>(null);
  const camera = useThree((s) => s.camera) as THREE.PerspectiveCamera;
  const size = useThree((s) => s.size);
  const clock = useThree((s) => s.clock);
  const mode = useRef<"auto" | "focus" | "free">("auto");
  const lastTouch = useRef(-1e3);
  const flight = useRef({ active: false, start: 0, duration: 1.5, fromPos: new THREE.Vector3(), fromTarget: new THREE.Vector3(), toPos: new THREE.Vector3(), toTarget: new THREE.Vector3() });
  const drift = useRef({ start: 0, base: new THREE.Vector3() });
  const placed = useRef(false);

  const overview = useMemo(() => fitOverview(layout, camera, size.width, size.height, insets), [layout, camera, size.width, size.height, insets]);

  // Shift the projection so the free area is the centre of the view.
  useEffect(() => {
    camera.setViewOffset(size.width, size.height, -(insets.left - insets.right) / 2, -(insets.top - insets.bottom) / 2, size.width, size.height);
    camera.updateProjectionMatrix();
  }, [camera, size.width, size.height, insets]);

  const flyTo = useCallback(
    (v: View, duration: number) => {
      const c = controls.current;
      if (!c) return;
      const f = flight.current;
      f.active = true;
      f.start = clock.elapsedTime;
      f.duration = duration;
      f.fromPos.copy(camera.position);
      f.fromTarget.copy(c.target);
      f.toPos.copy(v.pos);
      f.toTarget.copy(v.target);
    },
    [camera, clock],
  );

  // Overview changes (new parties, resize, insets): reframe while in automatic mode.
  const overviewRef = useRef(overview);
  overviewRef.current = overview;
  useEffect(() => {
    const c = controls.current;
    if (!c) return;
    if (!placed.current) {
      camera.position.copy(overview.pos);
      c.target.copy(overview.target);
      c.update();
      placed.current = true;
      drift.current.start = clock.elapsedTime;
      drift.current.base.copy(overview.pos);
      return;
    }
    if (mode.current === "auto") flyTo(overview, 2.2);
  }, [overview, camera, clock, flyTo]);

  const place = selected ? layout.places.get(selected) ?? null : null;
  const placeRef = useRef(place);
  placeRef.current = place;
  const placeKey = place ? `${place.id}:${place.x}:${place.z}` : "";
  useEffect(() => {
    const c = controls.current;
    const target = placeRef.current;
    if (!c) return;
    if (target) {
      mode.current = "focus";
      flyTo(focusView(target, camera.position, c.target), 1.4);
    } else if (mode.current === "focus") {
      mode.current = "auto";
      flyTo(overviewRef.current, 1.6);
    }
  }, [placeKey, camera, flyTo]);

  useFrame(({ clock: c }) => {
    const ctl = controls.current;
    if (!ctl) return;
    const now = c.elapsedTime;
    const f = flight.current;
    if (f.active) {
      const t = Math.min(1, (now - f.start) / f.duration);
      const e = easeInOut(t);
      camera.position.lerpVectors(f.fromPos, f.toPos, e);
      ctl.target.lerpVectors(f.fromTarget, f.toTarget, e);
      if (t >= 1) {
        f.active = false;
        drift.current.start = now;
        drift.current.base.copy(camera.position);
      }
    } else if (mode.current === "auto" && !calm) {
      const a = Math.sin((now - drift.current.start) * 0.07) * 0.1;
      const off = probe.copy(drift.current.base).sub(ctl.target);
      const ca = Math.cos(a);
      const sa = Math.sin(a);
      camera.position.set(ctl.target.x + off.x * ca + off.z * sa, drift.current.base.y + Math.sin((now - drift.current.start) * 0.05) * 1.2, ctl.target.z - off.x * sa + off.z * ca);
    } else if (mode.current === "free" && !selected && now - lastTouch.current > RETURN_AFTER_S) {
      mode.current = "auto";
      flyTo(overview, 2.4);
    }
    ctl.target.x = THREE.MathUtils.clamp(ctl.target.x, -90, 90);
    ctl.target.z = THREE.MathUtils.clamp(ctl.target.z, -80, 80);
    ctl.target.y = THREE.MathUtils.clamp(ctl.target.y, 0, 24);
    ctl.update();
  });

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enableDamping
      dampingFactor={0.08}
      minDistance={9}
      maxDistance={420}
      minPolarAngle={0.12}
      maxPolarAngle={1.38}
      zoomSpeed={0.8}
      rotateSpeed={0.6}
      panSpeed={0.8}
      onStart={() => {
        flight.current.active = false;
        lastTouch.current = clock.elapsedTime;
        if (mode.current !== "focus") mode.current = "free";
      }}
      onEnd={() => {
        lastTouch.current = clock.elapsedTime;
      }}
    />
  );
}
