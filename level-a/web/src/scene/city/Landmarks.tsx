// Things with words on them: the headline billboard (only ever the server's own headline), the
// plant's sign and the batch plate, the distributors' order-system screens, "?" over parties with
// panels nobody has located, and the shock of a refusal by Atlas where it happened.

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import type { SystemView, WallState } from "../../api/types";
import { createSurface, fitFont, roundRect, spacedText, type CanvasSurface } from "../canvasText";
import { COLORS, FONT_DISPLAY, FONT_MONO } from "../theme";
import type { FxPool } from "./Fx";
import type { CityLayout, PartyPlace, V3 } from "./layout";
import { systemTitle } from "./labelContent";

function useSurface(w: number, h: number): CanvasSurface {
  const s = useMemo(() => createSurface(w, h), [w, h]);
  useEffect(() => () => s.texture.dispose(), [s]);
  return s;
}

// ---------------------------------------------------------------------------------------------
// Headline

export function headlineParts(headline: string): { head: string; tail: string; tone: "clear" | "open" | "neutral" } {
  const text = headline.trim();
  const m = text.match(/^(.*?)\s*[:·]\s*(.+)$/);
  const head = m ? m[1] : text;
  const tail = m ? m[2] : "";
  const tone = /^ALL CLEAR\b/i.test(text) ? "clear" : /NOT ALL CLEAR/i.test(text) ? "open" : "neutral";
  return { head, tail, tone };
}

export function Headline({ state, at, fontsReady, flashAt, calm }: { state: WallState | null; at: V3; fontsReady: boolean; flashAt: number; calm: boolean }) {
  const surface = useSurface(2048, 440);
  const group = useRef<THREE.Group>(null);
  const material = useMemo(() => new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, toneMapped: false, fog: false }), []);
  useEffect(() => () => material.dispose(), [material]);
  const headline = state?.headline ?? "";
  const recall = state?.recall;
  const parts = headlineParts(headline);
  const accent = parts.tone === "clear" ? COLORS.good : parts.tone === "open" ? COLORS.bad : COLORS.ink2;

  useLayoutEffect(() => {
    const { ctx, canvas, texture } = surface;
    const W = canvas.width;
    const H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!headline) {
      texture.needsUpdate = true;
      return;
    }
    roundRect(ctx, 8, 8, W - 16, H - 16, 26);
    ctx.fillStyle = "rgba(7, 10, 16, 0.86)";
    ctx.fill();
    ctx.lineWidth = 4;
    ctx.strokeStyle = `${accent}aa`;
    ctx.stroke();
    ctx.fillStyle = accent;
    roundRect(ctx, 8, 8, 22, H - 16, 11);
    ctx.fill();
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#93a2b5";
    ctx.font = `500 46px ${FONT_MONO}`;
    const meta = recall ? `RECALL OF BATCH ${recall.batch_code} · ${recall.units_total.toLocaleString("en-US")} PANELS · ${recall.model}` : "RECALL";
    spacedText(ctx, meta, 84, 92, 3);
    ctx.fillStyle = accent;
    const headSize = fitFont(ctx, parts.head, 700, FONT_DISPLAY, parts.tail ? 190 : 230, W - 170, 60);
    spacedText(ctx, parts.head, 80, parts.tail ? 92 + headSize * 0.95 : H / 2 + headSize * 0.42 + 30, 6);
    if (parts.tail) {
      ctx.fillStyle = "#eef2f6";
      fitFont(ctx, parts.tail, 600, FONT_DISPLAY, 96, W - 170, 40);
      ctx.fillText(parts.tail, 82, H - 46);
    }
    texture.needsUpdate = true;
  }, [surface, headline, recall, parts.head, parts.tail, accent, fontsReady]);

  useLayoutEffect(() => {
    material.map = surface.texture;
    material.needsUpdate = true;
  }, [material, surface]);

  useFrame(({ camera, clock }) => {
    const g = group.current;
    if (!g) return;
    g.quaternion.copy(camera.quaternion);
    const t = clock.elapsedTime;
    g.position.set(at[0], at[1] + (calm ? 0 : Math.sin(t * 0.6) * 0.25), at[2]);
    const since = t - flashAt;
    const flash = since >= 0 && since < 1.4 ? Math.sin((since / 1.4) * Math.PI) : 0;
    material.color.setScalar(1.25 + flash * 1.4);
    g.scale.setScalar(1 + flash * 0.05);
  });

  if (!headline) return null;
  return (
    <group ref={group} position={at}>
      <mesh material={material} renderOrder={20}>
        <planeGeometry args={[46, (46 * 440) / 2048]} />
      </mesh>
    </group>
  );
}

// ---------------------------------------------------------------------------------------------
// Plant sign and batch plate

export function PlantMarks({ state, layout, fontsReady }: { state: WallState | null; layout: CityLayout; fontsReady: boolean }) {
  const sign = useSurface(1024, 150);
  const plate = useSurface(512, 680);
  const signMat = useMemo(() => new THREE.MeshBasicMaterial({ transparent: true, toneMapped: false }), []);
  const plateMat = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  useEffect(
    () => () => {
      signMat.dispose();
      plateMat.dispose();
    },
    [signMat, plateMat],
  );
  const recall = state?.recall;
  const supplier = recall?.supplier ?? "Kestrel Life Safety";

  useLayoutEffect(() => {
    const { ctx, canvas, texture } = sign;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ffe7c4";
    fitFont(ctx, supplier.toUpperCase(), 700, FONT_DISPLAY, 112, canvas.width - 40, 40);
    ctx.textBaseline = "middle";
    spacedText(ctx, supplier.toUpperCase(), canvas.width / 2, canvas.height / 2 + 6, 10, "center");
    texture.needsUpdate = true;
    signMat.map = texture;
    signMat.color.setScalar(1.6);
    signMat.needsUpdate = true;
  }, [sign, supplier, signMat, fontsReady]);

  useLayoutEffect(() => {
    const { ctx, canvas, texture } = plate;
    const W = canvas.width;
    const H = canvas.height;
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, "#1a0d0c");
    grad.addColorStop(1, "#0c0a0d");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "rgba(255,90,71,0.6)";
    ctx.lineWidth = 6;
    ctx.strokeRect(14, 14, W - 28, H - 28);
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#ff9a8c";
    ctx.font = `600 40px ${FONT_MONO}`;
    spacedText(ctx, "BATCH", W / 2, 96, 10, "center");
    ctx.fillStyle = "#ffffff";
    fitFont(ctx, recall?.batch_code ?? "—", 700, FONT_DISPLAY, 170, W - 60, 60);
    ctx.fillText(recall?.batch_code ?? "—", W / 2, 260);
    ctx.fillStyle = "#ffd0c8";
    ctx.font = `600 64px ${FONT_DISPLAY}`;
    ctx.fillText(recall ? `${recall.model} · ${recall.units_total.toLocaleString("en-US")} panels` : "awaiting a recall", W / 2, 350);
    ctx.fillStyle = "#c9b3b0";
    ctx.font = `500 34px ${FONT_MONO}`;
    if (recall) {
      ctx.fillText(`affected sw ${recall.affected_versions.join(" · ")}`, W / 2, 450);
      ctx.fillText(`fixed sw ${recall.fixed_version}`, W / 2, 500);
      ctx.fillStyle = recall.status === "closed" ? COLORS.good : COLORS.bad;
      ctx.font = `700 56px ${FONT_DISPLAY}`;
      spacedText(ctx, recall.status === "closed" ? "CLOSED" : "OPEN", W / 2, 610, 8, "center");
    }
    texture.needsUpdate = true;
    plateMat.map = texture;
    plateMat.color.setScalar(1.15);
    plateMat.needsUpdate = true;
  }, [plate, recall, plateMat, fontsReady]);

  const { x, z } = layout.plant;
  const p = layout.plant.plate;
  return (
    <group>
      <mesh material={signMat} position={[x - 3.5, 3.55, z - 5.0 + 4.27]}>
        <planeGeometry args={[10.5, (10.5 * 150) / 1024]} />
      </mesh>
      <mesh material={plateMat} position={[p[0], 2.52, p[2] + 0.21]}>
        <planeGeometry args={[3.1, 4.1]} />
      </mesh>
    </group>
  );
}

// ---------------------------------------------------------------------------------------------
// Order-system terminals

const SCREEN: Record<SystemView["status"], { bg: string; fg: string; word: string }> = {
  ok: { bg: "#06261a", fg: COLORS.good, word: "ACTIVE" },
  contract_failed: { bg: "#2a0806", fg: COLORS.bad, word: "CONTRACT FAILED" },
  repairing: { bg: "#2a1c04", fg: COLORS.warn, word: "REPAIRING" },
  paused: { bg: "#14181e", fg: COLORS.ink3, word: "PAUSED" },
};

function Terminal({ place, system, fontsReady, calm }: { place: PartyPlace; system: SystemView | undefined; fontsReady: boolean; calm: boolean }) {
  const surface = useSurface(512, 320);
  const screenMat = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  const lamp = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  useEffect(
    () => () => {
      screenMat.dispose();
      lamp.dispose();
    },
    [screenMat, lamp],
  );
  const status = system?.status ?? "paused";
  const look = SCREEN[status];
  const v2 = status === "ok" && (system?.adapter_version ?? 1) >= 2;
  const fg = status === "ok" && !v2 ? COLORS.live : look.fg;
  const title = system ? systemTitle(system) : place.party.name;

  useLayoutEffect(() => {
    const { ctx, canvas, texture } = surface;
    const W = canvas.width;
    const H = canvas.height;
    ctx.fillStyle = look.bg;
    ctx.fillRect(0, 0, W, H);
    for (let y = 0; y < H; y += 6) {
      ctx.fillStyle = "rgba(255,255,255,0.025)";
      ctx.fillRect(0, y, W, 2);
    }
    ctx.textAlign = "left";
    ctx.fillStyle = "#d8e2ee";
    fitFont(ctx, title.toUpperCase(), 700, FONT_DISPLAY, 70, W - 50, 30);
    ctx.fillText(title.toUpperCase(), 26, 86);
    ctx.fillStyle = fg;
    fitFont(ctx, look.word, 700, FONT_DISPLAY, 84, W - 50, 30);
    ctx.fillText(look.word, 26, 190);
    ctx.fillStyle = "#9fb0c2";
    ctx.font = `500 26px ${FONT_MONO}`;
    const ops = system?.operations;
    ctx.fillText(ops ? `verified ${ops.verified} · reverify ${ops.needs_reverify}` : system?.detail.slice(0, 30) ?? "", 26, 262);
    ctx.fillText(system?.adapter_source ? `adapter from ${system.adapter_source}` : "", 26, 298);
    texture.needsUpdate = true;
    screenMat.map = texture;
    screenMat.color.setScalar(1.35);
    screenMat.needsUpdate = true;
    lamp.color.set(fg).multiplyScalar(3);
  }, [surface, look, fg, title, system, screenMat, lamp, fontsReady]);

  useFrame(({ clock }) => {
    if (calm) return;
    const t = clock.elapsedTime;
    const k = status === "contract_failed" ? (Math.sin(t * 8) > 0 ? 3.2 : 0.4) : status === "repairing" ? 1.6 + 1.4 * Math.sin(t * 5) : 2.4;
    lamp.color.set(fg).multiplyScalar(k);
  });

  const t = place.terminal as V3;
  return (
    <group position={[t[0], 0, t[2]]}>
      <mesh material={screenMat} position={[0, 2.25, 0.05]} rotation-x={-0.28}>
        <planeGeometry args={[1.6, 1.0]} />
      </mesh>
      <mesh position={[0, 2.25, -0.02]} rotation-x={-0.28}>
        <boxGeometry args={[1.75, 1.15, 0.1]} />
        <meshStandardMaterial color="#20262f" roughness={0.5} metalness={0.5} />
      </mesh>
      <mesh material={lamp} position={[0, 2.98, -0.05]}>
        <sphereGeometry args={[0.13, 14, 10]} />
      </mesh>
    </group>
  );
}

export function Terminals({ state, layout, fontsReady, calm }: { state: WallState | null; layout: CityLayout; fontsReady: boolean; calm: boolean }) {
  return (
    <>
      {layout.order
        .filter((p) => p.terminal)
        .map((p) => (
          <Terminal key={p.id} place={p} system={state?.systems.find((s) => s.system === p.party.order_system)} fontsReady={fontsReady} calm={calm} />
        ))}
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// "?" over parties with panels nobody has located

export function UnknownMarks({ layout, fontsReady }: { layout: CityLayout; fontsReady: boolean }) {
  const surface = useSurface(128, 128);
  const material = useMemo(() => new THREE.SpriteMaterial({ transparent: true, depthWrite: false, toneMapped: false }), []);
  useEffect(() => () => material.dispose(), [material]);
  useLayoutEffect(() => {
    const { ctx, texture } = surface;
    ctx.clearRect(0, 0, 128, 128);
    ctx.beginPath();
    ctx.arc(64, 64, 54, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(40, 6, 4, 0.75)";
    ctx.fill();
    ctx.lineWidth = 7;
    ctx.strokeStyle = COLORS.bad;
    ctx.stroke();
    ctx.fillStyle = "#ffd2cb";
    ctx.font = `700 84px ${FONT_DISPLAY}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("?", 64, 70);
    texture.needsUpdate = true;
    material.map = texture;
    material.color.setScalar(1.5);
    material.needsUpdate = true;
  }, [surface, material, fontsReady]);
  return (
    <>
      {layout.order
        .filter((p) => p.ghostTop)
        .map((p) => {
          const g = p.ghostTop as V3;
          return <sprite key={p.id} material={material} position={[g[0], g[1] + 0.9, g[2]]} scale={[1.5, 1.5, 1]} />;
        })}
    </>
  );
}

// ---------------------------------------------------------------------------------------------
// Refusals by Atlas: a red shock where it happened (the plant for the close gate).

export function Refusals({ state, layout, fx, onCloseRefused }: { state: WallState | null; layout: CityLayout; fx: FxPool; onCloseRefused: (t: number) => void }) {
  const clock = useThree((s) => s.clock);
  const seen = useRef<Set<string> | null>(null);
  useEffect(() => {
    const list = state?.refusals ?? [];
    const keys = list.map((r) => `${r.at}|${r.rule}|${r.message}`);
    if (seen.current === null) {
      seen.current = new Set(keys);
      return;
    }
    const now = clock.elapsedTime;
    list.forEach((r, i) => {
      const k = keys[i];
      if (seen.current?.has(k)) return;
      seen.current?.add(k);
      const place = r.party_id ? layout.places.get(r.party_id) : undefined;
      if (place) {
        fx.shock([place.x, 0.35, place.z], COLORS.bad, place.ring + 3.5, now, 1.3);
        fx.shock([place.x, place.top * 0.6, place.z], COLORS.bad, place.ring + 1, now + 0.12, 1.0);
        fx.shock([place.x, 0.35, place.z], "#ffffff", place.ring * 0.6, now + 0.05, 0.6);
      } else {
        const p = layout.plant.plate;
        fx.shock([p[0], 0.4, p[2]], COLORS.bad, 12, now, 1.6);
        fx.shock([p[0], 0.4, p[2]], COLORS.bad, 7, now + 0.2, 1.2);
        fx.shock([layout.dispatch.hub[0], 0.4, layout.dispatch.hub[2]], COLORS.bad, 11, now + 0.1, 1.4);
        onCloseRefused(now);
      }
    });
  }, [state, layout, fx, clock, onCloseRefused]);
  return null;
}
