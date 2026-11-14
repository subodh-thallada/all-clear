// DOM labels pinned to points in the city. React renders their content when the state changes;
// the projector inside the render loop places them every frame, hides those under the HUD insets
// or behind the camera, and declutters by priority (a selected or hovered party, live calls and
// leases win; quiet parties give way). Text stays crisp at any zoom because it is real text.

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import * as THREE from "three";
import type { V3 } from "./layout";

export interface LabelSpec {
  key: string;
  anchor: V3;
  priority: number;
  /** Never hidden by decluttering (still hidden when off-screen or under the HUD). */
  pinned?: boolean;
  partyId?: string;
  /** Offset in px above the anchor. */
  lift?: number;
  node: ReactNode;
}

interface Entry {
  el: HTMLDivElement;
  anchor: THREE.Vector3;
  priority: number;
  pinned: boolean;
  lift: number;
  w: number;
  h: number;
  x: number;
  y: number;
  shown: boolean;
}

export interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export class LabelRegistry {
  private entries = new Map<string, Entry>();
  sorted: Entry[] = [];
  private dirty = true;

  upsert(key: string, el: HTMLDivElement, spec: LabelSpec) {
    const e = this.entries.get(key);
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    if (e) {
      e.el = el;
      e.anchor.set(spec.anchor[0], spec.anchor[1], spec.anchor[2]);
      if (e.priority !== spec.priority || e.pinned !== !!spec.pinned) this.dirty = true;
      e.priority = spec.priority;
      e.pinned = !!spec.pinned;
      e.lift = spec.lift ?? 10;
      e.w = w;
      e.h = h;
    } else {
      this.entries.set(key, {
        el,
        anchor: new THREE.Vector3(spec.anchor[0], spec.anchor[1], spec.anchor[2]),
        priority: spec.priority,
        pinned: !!spec.pinned,
        lift: spec.lift ?? 10,
        w,
        h,
        x: -1e4,
        y: -1e4,
        shown: false,
      });
      this.dirty = true;
    }
  }

  remove(key: string) {
    if (this.entries.delete(key)) this.dirty = true;
  }

  resort() {
    if (!this.dirty) return;
    this.dirty = false;
    this.sorted = [...this.entries.values()].sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.priority - a.priority);
    this.sorted.forEach((e, i) => {
      e.el.style.zIndex = String(1000 - i);
    });
  }
}

const v = new THREE.Vector3();

/** Lives inside the Canvas: projects and declutters every frame without allocating. */
export function LabelProjector({ registry, insets }: { registry: LabelRegistry; insets: Insets }) {
  const size = useThree((s) => s.size);
  const rects = useRef(new Float32Array(4 * 256));
  useFrame(({ camera }) => {
    registry.resort();
    const W = size.width;
    const H = size.height;
    const fx0 = insets.left + 4;
    const fx1 = W - insets.right - 4;
    const fy0 = insets.top + 4;
    const fy1 = H - insets.bottom - 4;
    let placed = 0;
    const r = rects.current;
    for (const e of registry.sorted) {
      v.copy(e.anchor).project(camera);
      const sx = (v.x * 0.5 + 0.5) * W;
      const sy = (-v.y * 0.5 + 0.5) * H;
      const x = Math.round(sx - e.w / 2);
      const y = Math.round(sy - e.h - e.lift);
      let show = v.z > -1 && v.z < 1 && sx > fx0 && sx < fx1 && sy > fy0 && sy < fy1 + e.h;
      if (show && (x < fx0 - e.w * 0.4 || x + e.w > fx1 + e.w * 0.4 || y < fy0 - 4)) show = e.pinned;
      if (show && !e.pinned) {
        for (let i = 0; i < placed; i++) {
          const o = i * 4;
          if (x < r[o + 2] + 3 && x + e.w + 3 > r[o] && y < r[o + 3] + 2 && y + e.h + 2 > r[o + 1]) {
            show = false;
            break;
          }
        }
      }
      if (show && placed < 256) {
        const o = placed * 4;
        r[o] = x;
        r[o + 1] = y;
        r[o + 2] = x + e.w;
        r[o + 3] = y + e.h;
        placed++;
      }
      if (show && (x !== e.x || y !== e.y)) {
        e.el.style.transform = `translate3d(${x}px,${y}px,0)`;
        e.x = x;
        e.y = y;
      }
      if (show !== e.shown) {
        e.el.dataset.on = show ? "1" : "0";
        e.shown = show;
      }
    }
  });
  return null;
}

function LabelView({ spec, registry, onEnter, onLeave, onClick }: { spec: LabelSpec; registry: LabelRegistry; onEnter?: () => void; onLeave?: () => void; onClick?: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (ref.current) registry.upsert(spec.key, ref.current, spec);
  });
  useEffect(() => () => registry.remove(spec.key), [registry, spec.key]);
  return (
    <div ref={ref} className="acs-label" data-on="0" onPointerEnter={onEnter} onPointerLeave={onLeave} onClick={onClick}>
      {spec.node}
    </div>
  );
}

export function LabelLayer({
  specs,
  registry,
  onHover,
  onSelect,
}: {
  specs: LabelSpec[];
  registry: LabelRegistry;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="acs-labels">
      {specs.map((s) => (
        <LabelView
          key={s.key}
          spec={s}
          registry={registry}
          onEnter={s.partyId ? () => onHover(s.partyId ?? null) : undefined}
          onLeave={s.partyId ? () => onHover(null) : undefined}
          onClick={s.partyId ? () => onSelect(s.partyId as string) : undefined}
        />
      ))}
    </div>
  );
}
