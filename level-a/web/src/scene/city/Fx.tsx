// Transient effects from fixed-size ring buffers: shock rings (a refusal, a kill, an adoption, a
// panel test) and comets (a panel event travelling to the panel service, work flowing along a
// worker's beam, returned panels travelling home to the plant). Spawning writes one slot; the
// motion itself is evaluated in the vertex shader, so a frame costs no CPU work and no allocation.

import { useEffect, useMemo } from "react";
import * as THREE from "three";
import type { V3 } from "./layout";
import { cometMaterial, linear, shockMaterial, type SceneUniforms } from "./materials";

const SHOCKS = 64;
const COMETS = 768;
const TRAIL = [0, 0.035, 0.07, 0.105];

export class FxPool {
  readonly shocks: THREE.InstancedMesh;
  readonly comets: THREE.InstancedMesh;
  private shockIdx = 0;
  private cometIdx = 0;
  private readonly shockAttr: THREE.InstancedBufferAttribute;
  private readonly shockColor: THREE.InstancedBufferAttribute;
  private readonly p0: THREE.InstancedBufferAttribute;
  private readonly p1: THREE.InstancedBufferAttribute;
  private readonly p2: THREE.InstancedBufferAttribute;
  private readonly timing: THREE.InstancedBufferAttribute;
  private readonly cometColor: THREE.InstancedBufferAttribute;
  private readonly m = new THREE.Matrix4();
  enabled = true;

  constructor(uniforms: SceneUniforms) {
    const ring = new THREE.PlaneGeometry(2, 2).rotateX(-Math.PI / 2);
    this.shockAttr = new THREE.InstancedBufferAttribute(new Float32Array(SHOCKS * 2).fill(-1000), 2);
    this.shockColor = new THREE.InstancedBufferAttribute(new Float32Array(SHOCKS * 3), 3);
    ring.setAttribute("aShock", this.shockAttr);
    ring.setAttribute("aColor", this.shockColor);
    for (let i = 0; i < SHOCKS; i++) this.shockAttr.setXY(i, -1000, 1);
    this.shocks = new THREE.InstancedMesh(ring, shockMaterial(uniforms), SHOCKS);
    this.shocks.frustumCulled = false;
    this.shocks.renderOrder = 5;

    const dot = new THREE.OctahedronGeometry(1, 1);
    this.p0 = new THREE.InstancedBufferAttribute(new Float32Array(COMETS * 3), 3);
    this.p1 = new THREE.InstancedBufferAttribute(new Float32Array(COMETS * 3), 3);
    this.p2 = new THREE.InstancedBufferAttribute(new Float32Array(COMETS * 3), 3);
    this.timing = new THREE.InstancedBufferAttribute(new Float32Array(COMETS * 4), 4);
    this.cometColor = new THREE.InstancedBufferAttribute(new Float32Array(COMETS * 3), 3);
    for (let i = 0; i < COMETS; i++) this.timing.setXYZW(i, -1000, 1, 0, 0);
    dot.setAttribute("aP0", this.p0);
    dot.setAttribute("aP1", this.p1);
    dot.setAttribute("aP2", this.p2);
    dot.setAttribute("aTiming", this.timing);
    dot.setAttribute("aColor", this.cometColor);
    this.comets = new THREE.InstancedMesh(dot, cometMaterial(uniforms), COMETS);
    this.comets.frustumCulled = false;
    this.comets.renderOrder = 6;
  }

  /** An expanding ring on the ground (or at height y) of the given radius. */
  shock(at: V3, color: string, radius: number, start: number, duration = 1.2) {
    if (!this.enabled) return;
    const i = this.shockIdx;
    this.shockIdx = (i + 1) % SHOCKS;
    this.m.makeScale(radius, 1, radius).setPosition(at[0], at[1], at[2]);
    this.shocks.setMatrixAt(i, this.m);
    this.shockAttr.setXY(i, start, duration);
    const c = linear(color);
    this.shockColor.setXYZ(i, c.r, c.g, c.b);
    this.shocks.instanceMatrix.needsUpdate = true;
    this.shockAttr.needsUpdate = true;
    this.shockColor.needsUpdate = true;
  }

  /** A bright point with a short trail along the quadratic arc p0 -> p1 -> p2. */
  comet(p0: V3, p1: V3, p2: V3, color: string, start: number, duration: number, size = 0.16) {
    if (!this.enabled) return;
    const c = linear(color);
    for (const lag of TRAIL) {
      const i = this.cometIdx;
      this.cometIdx = (i + 1) % COMETS;
      this.p0.setXYZ(i, p0[0], p0[1], p0[2]);
      this.p1.setXYZ(i, p1[0], p1[1], p1[2]);
      this.p2.setXYZ(i, p2[0], p2[1], p2[2]);
      this.timing.setXYZW(i, start, duration, lag, size);
      this.cometColor.setXYZ(i, c.r, c.g, c.b);
    }
    this.p0.needsUpdate = true;
    this.p1.needsUpdate = true;
    this.p2.needsUpdate = true;
    this.timing.needsUpdate = true;
    this.cometColor.needsUpdate = true;
  }

  dispose() {
    for (const mesh of [this.shocks, this.comets]) {
      mesh.geometry.dispose();
      (mesh.material as THREE.Material).dispose();
      mesh.dispose();
    }
  }
}

export function useFxPool(uniforms: SceneUniforms): FxPool {
  const pool = useMemo(() => new FxPool(uniforms), [uniforms]);
  useEffect(() => () => pool.dispose(), [pool]);
  return pool;
}

export function FxView({ pool }: { pool: FxPool }) {
  return (
    <>
      <primitive object={pool.shocks} />
      <primitive object={pool.comets} />
    </>
  );
}

/** A lift for an arc between two points, matching the transfer arcs. */
export function arcControl(a: V3, b: V3, lift: number): V3 {
  const dist = Math.hypot(b[0] - a[0], b[2] - a[2]);
  return [(a[0] + b[0]) / 2, Math.max(a[1], b[1]) + lift + dist * 0.2, (a[2] + b[2]) / 2];
}
