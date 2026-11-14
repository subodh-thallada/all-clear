// Instanced layers: the CPU writes per-instance data only when the layout changes. A panel layer
// remembers each panel by a stable key, so a panel that changes bucket pops and cross-fades its
// colour, a new panel grows in, and everything else stays exactly where it was.

import * as THREE from "three";
import type { BoxInst, PanelInst, TreeInst } from "./layout";
import { linear } from "./materials";

export function capacityFor(n: number): number {
  let cap = 256;
  while (cap < n) cap *= 2;
  return cap;
}

function writeMatrix(a: Float32Array, i: number, x: number, y: number, z: number, sx: number, sy: number, sz: number, ry: number) {
  const o = i * 16;
  const c = Math.cos(ry);
  const s = Math.sin(ry);
  a[o] = c * sx;
  a[o + 1] = 0;
  a[o + 2] = -s * sx;
  a[o + 3] = 0;
  a[o + 4] = 0;
  a[o + 5] = sy;
  a[o + 6] = 0;
  a[o + 7] = 0;
  a[o + 8] = s * sz;
  a[o + 9] = 0;
  a[o + 10] = c * sz;
  a[o + 11] = 0;
  a[o + 12] = x;
  a[o + 13] = y;
  a[o + 14] = z;
  a[o + 15] = 1;
}

function writeColor(a: Float32Array, i: number, hex: string) {
  const c = linear(hex);
  a[i * 3] = c.r;
  a[i * 3 + 1] = c.g;
  a[i * 3 + 2] = c.b;
}

export type RiseOf = (key: string) => number;

class BaseLayer {
  readonly mesh: THREE.InstancedMesh;
  protected readonly rise: THREE.InstancedBufferAttribute;
  protected readonly color: THREE.InstancedBufferAttribute;

  constructor(geometry: THREE.BufferGeometry, material: THREE.Material, capacity: number) {
    this.rise = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 2), 2);
    geometry.setAttribute("aRise", this.rise);
    this.mesh = new THREE.InstancedMesh(geometry, material, capacity);
    this.color = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 3), 3);
    this.mesh.instanceColor = this.color;
    this.mesh.frustumCulled = false;
    this.mesh.count = 0;
  }

  protected commit(n: number) {
    this.mesh.count = n;
    this.mesh.instanceMatrix.needsUpdate = true;
    this.color.needsUpdate = true;
    this.rise.needsUpdate = true;
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.mesh.dispose();
  }
}

export class BoxLayer extends BaseLayer {
  constructor(material: THREE.Material, capacity: number) {
    super(new THREE.BoxGeometry(1, 1, 1), material, capacity);
  }

  write(list: BoxInst[], riseOf: RiseOf) {
    const m = this.mesh.instanceMatrix.array as Float32Array;
    const col = this.color.array as Float32Array;
    const rise = this.rise.array as Float32Array;
    for (let i = 0; i < list.length; i++) {
      const b = list[i];
      writeMatrix(m, i, b.x, b.y, b.z, b.sx, b.sy, b.sz, b.ry);
      writeColor(col, i, b.color);
      rise[i * 2] = riseOf(b.rise);
      rise[i * 2 + 1] = 0;
    }
    this.commit(list.length);
  }
}

function treeGeometry(): THREE.BufferGeometry {
  // One low-poly tree: trunk plus two stacked cones, merged by hand into a single geometry.
  const parts = [
    new THREE.CylinderGeometry(0.06, 0.08, 0.5, 5).translate(0, 0.25, 0),
    new THREE.ConeGeometry(0.55, 1.0, 7).translate(0, 0.9, 0),
    new THREE.ConeGeometry(0.4, 0.8, 7).translate(0, 1.35, 0),
  ].map((g) => g.toNonIndexed());
  const total = parts.reduce((s, g) => s + g.attributes.position.count, 0);
  const pos = new Float32Array(total * 3);
  const nor = new Float32Array(total * 3);
  let o = 0;
  for (const g of parts) {
    pos.set(g.attributes.position.array as Float32Array, o * 3);
    nor.set(g.attributes.normal.array as Float32Array, o * 3);
    o += g.attributes.position.count;
    g.dispose();
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("normal", new THREE.BufferAttribute(nor, 3));
  return geo;
}

export class TreeLayer extends BaseLayer {
  constructor(material: THREE.Material, capacity: number) {
    super(treeGeometry(), material, capacity);
  }

  write(list: TreeInst[], riseOf: RiseOf) {
    const m = this.mesh.instanceMatrix.array as Float32Array;
    const col = this.color.array as Float32Array;
    const rise = this.rise.array as Float32Array;
    for (let i = 0; i < list.length; i++) {
      const t = list[i];
      writeMatrix(m, i, t.x, 0, t.z, t.s, t.s * 1.15, t.s, 0);
      writeColor(col, i, i % 3 === 0 ? "#1d3a2a" : i % 3 === 1 ? "#24432f" : "#1a3326");
      rise[i * 2] = riseOf(t.rise);
      rise[i * 2 + 1] = 0;
    }
    this.commit(list.length);
  }
}

interface PanelMemo {
  color: string;
  x: number;
  y: number;
  z: number;
  start: number;
  mode: number;
  prev: string;
}

export class PanelLayer extends BaseLayer {
  private readonly prevColor: THREE.InstancedBufferAttribute;
  private readonly anim: THREE.InstancedBufferAttribute;
  private memo = new Map<string, PanelMemo>();

  constructor(material: THREE.Material, capacity: number, private readonly popOnMove: boolean) {
    super(new THREE.BoxGeometry(1, 1, 1), material, capacity);
    this.prevColor = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 3), 3);
    this.anim = new THREE.InstancedBufferAttribute(new Float32Array(capacity * 4), 4);
    this.mesh.geometry.setAttribute("aPrevColor", this.prevColor);
    this.mesh.geometry.setAttribute("aAnim", this.anim);
  }

  /** Keeps the per-panel history when the layer is rebuilt with a larger capacity. */
  takeMemoFrom(old: PanelLayer) {
    this.memo = old.memo;
  }

  /** `animate` is false for the first write (the intro rise covers it) and in calm mode. */
  write(list: PanelInst[], riseOf: RiseOf, now: number, animate: boolean) {
    const m = this.mesh.instanceMatrix.array as Float32Array;
    const col = this.color.array as Float32Array;
    const prev = this.prevColor.array as Float32Array;
    const anim = this.anim.array as Float32Array;
    const rise = this.rise.array as Float32Array;
    const next = new Map<string, PanelMemo>();
    for (let i = 0; i < list.length; i++) {
      const p = list[i];
      const old = this.memo.get(p.key);
      let memo: PanelMemo;
      if (!old) {
        memo = { color: p.color, x: p.x, y: p.y, z: p.z, start: animate ? now : -100, mode: animate ? 1 : 0, prev: p.color };
      } else if (old.color !== p.color) {
        memo = { color: p.color, x: p.x, y: p.y, z: p.z, start: animate ? now : -100, mode: animate ? 2 : 0, prev: old.color };
      } else if (this.popOnMove && (Math.abs(old.x - p.x) > 1e-3 || Math.abs(old.y - p.y) > 1e-3 || Math.abs(old.z - p.z) > 1e-3)) {
        memo = { color: p.color, x: p.x, y: p.y, z: p.z, start: animate ? now : -100, mode: animate ? 1 : 0, prev: p.color };
      } else {
        memo = { ...old, x: p.x, y: p.y, z: p.z };
      }
      next.set(p.key, memo);
      writeMatrix(m, i, p.x, p.y, p.z, p.sx, p.sy, p.sz, 0);
      writeColor(col, i, p.color);
      writeColor(prev, i, memo.prev);
      anim[i * 4] = memo.start;
      anim[i * 4 + 1] = memo.mode;
      anim[i * 4 + 2] = p.glow;
      anim[i * 4 + 3] = p.fx;
      rise[i * 2] = riseOf(p.rise);
      rise[i * 2 + 1] = 0;
    }
    this.memo = next;
    this.prevColor.needsUpdate = true;
    this.anim.needsUpdate = true;
    this.commit(list.length);
  }
}
