// The 3D screen: the recall of batch B-4471 drawn as a city at dusk. The HUD renders
// <CityScene {...} /> full-bleed behind its overlays and <PanelModel {...} /> on the panel page.
// Everything here is derived from WallState (types.ts); no positions come from the server and the
// headline is only ever the server's own words.

import { PerformanceMonitor } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PanelPageState, WallState } from "../api/types";
import { CameraRig } from "./city/CameraRig";
import { Dispatch } from "./city/Dispatch";
import { Edges, ReturnFlows } from "./city/Edges";
import { FxView, useFxPool } from "./city/Fx";
import { buildLabels, LABEL_CSS } from "./city/labelContent";
import { LabelLayer, LabelProjector, LabelRegistry, type Insets } from "./city/Labels";
import { Headline, PlantMarks, Refusals, Terminals, UnknownMarks } from "./city/Landmarks";
import { buildCity, CityMemory, type CityLayout } from "./city/layout";
import { createUniforms } from "./city/materials";
import { Monitoring } from "./city/Monitoring";
import { PostFX } from "./city/PostFX";
import { LotHighlight, PartyHits, ShadowDirector, Structures } from "./city/Structures";
import { useDuskEnvironment, World } from "./city/World";
import { PanelModelView } from "./panel/PanelModelView";
import { loadSceneFonts } from "./theme";

export interface CitySceneProps {
  state: WallState | null;
  selectedPartyId: string | null;
  onSelectParty: (partyId: string | null) => void;
  /** HUD overlays occupy these screen edges (px) — keep important 3D content out from under them. */
  insets?: { top: number; right: number; bottom: number; left: number };
  /** Reduce motion (prefers-reduced-motion or operator toggle). */
  calm?: boolean;
}

interface ContentProps {
  state: WallState | null;
  layout: CityLayout;
  selected: string | null;
  hovered: string | null;
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
  insets: Insets;
  calm: boolean;
  fontsReady: boolean;
  registry: LabelRegistry;
}

function CityContent({ state, layout, selected, hovered, onHover, onSelect, insets, calm, fontsReady, registry }: ContentProps) {
  const uniforms = useMemo(createUniforms, []);
  const fx = useFxPool(uniforms);
  const envMap = useDuskEnvironment();
  fx.enabled = !calm;
  useFrame(({ clock }) => {
    uniforms.uTime.value = clock.elapsedTime;
    uniforms.uMotion.value = calm ? 0 : 1;
  }, -1);

  // When each lot rises: the first city rises in a wave from the dispatch, later parties as they
  // are discovered. Calm mode skips the motion.
  const births = useRef(new Map<string, number>());
  const clockRef = useRef(0);
  useFrame(({ clock }) => {
    clockRef.current = clock.elapsedTime;
  }, -2);
  const riseOf = useMemo(() => {
    const map = births.current;
    const now = clockRef.current;
    const first = map.size === 0;
    for (const [key, [x, z]] of layout.riseKeys) {
      if (map.has(key)) continue;
      if (calm) map.set(key, -100);
      else if (first) map.set(key, now + 0.35 + Math.hypot(x, z) * 0.014);
      else map.set(key, now);
    }
    return (key: string) => map.get(key) ?? -100;
  }, [layout, calm]);

  const [flashAt, setFlashAt] = useState(-100);
  const hoverPlace = hovered ? layout.places.get(hovered) ?? null : null;
  const selectPlace = selected ? layout.places.get(selected) ?? null : null;

  return (
    <>
      {!DBG.includes('noworld') && <World calm={calm} />}
      {!DBG.includes('nostruct') && <Structures layout={layout} uniforms={uniforms} envMap={envMap} riseOf={riseOf} animate={!calm} />}
      <ShadowDirector shape={layout.shape} />
      {!DBG.includes('nohits') && <PartyHits places={layout.order} onHover={onHover} onSelect={onSelect} />}
      {hoverPlace && hoverPlace !== selectPlace && <LotHighlight place={hoverPlace} kind="hover" />}
      {selectPlace && <LotHighlight place={selectPlace} kind="select" />}
      {!DBG.includes('noedges') && <Edges layout={layout} focus={hovered ?? selected} calm={calm} />}
      <ReturnFlows state={state} layout={layout} fx={fx} />
      {!DBG.includes('nodispatch') && <Dispatch state={state} layout={layout} uniforms={uniforms} fx={fx} calm={calm} />}
      {!DBG.includes('nomon') && <Monitoring state={state} layout={layout} fx={fx} calm={calm} />}
      {!DBG.includes('noplant') && <PlantMarks state={state} layout={layout} fontsReady={fontsReady} />}
      {!DBG.includes('noterm') && <Terminals state={state} layout={layout} fontsReady={fontsReady} calm={calm} />}
      {!DBG.includes('nounk') && <UnknownMarks layout={layout} fontsReady={fontsReady} />}
      <Refusals state={state} layout={layout} fx={fx} onCloseRefused={setFlashAt} />
      {!DBG.includes('nohead') && <Headline state={state} at={layout.dispatch.headline} fontsReady={fontsReady} flashAt={flashAt} calm={calm} />}
      {!DBG.includes('nofxview') && <FxView pool={fx} />}
      <CameraRig layout={layout} selected={selected} insets={insets} calm={calm} />
      {!DBG.includes('noproj') && <LabelProjector registry={registry} insets={insets} />}
      {!DBG.includes('nofx') && <PostFX />}
    </>
  );
}

const DBG = new URLSearchParams(window.location.search).get('dbg') ?? '';
const NO_INSETS: Insets = { top: 0, right: 0, bottom: 0, left: 0 };

export default function CityScene(props: CitySceneProps) {
  const { state, selectedPartyId, onSelectParty, calm = false } = props;
  const raw = props.insets ?? NO_INSETS;
  const insets = useMemo<Insets>(() => ({ top: raw.top, right: raw.right, bottom: raw.bottom, left: raw.left }), [raw.top, raw.right, raw.bottom, raw.left]);

  const memory = useRef(new CityMemory());
  const layout = useMemo(() => buildCity(state, memory.current), [state]);
  const [hovered, setHovered] = useState<string | null>(null);
  const [fontsReady, setFontsReady] = useState(false);
  const [created, setCreated] = useState(false);
  const [dpr, setDpr] = useState<number | [number, number]>([1, 2]);
  const registry = useRef(new LabelRegistry());

  useEffect(() => {
    let live = true;
    loadSceneFonts().then(() => {
      if (live) setFontsReady(true);
    });
    return () => {
      live = false;
    };
  }, []);

  // A hovered party that disappears (new recall) stops being hovered.
  const hoveredLive = hovered && layout.places.has(hovered) ? hovered : null;
  const labels = useMemo(() => buildLabels(state, layout, hoveredLive, selectedPartyId), [state, layout, hoveredLive, selectedPartyId]);
  const select = useCallback((id: string) => onSelectParty(id), [onSelectParty]);

  return (
    <div
      className="acs-root"
      style={{ cursor: hoveredLive ? "pointer" : undefined }}
      data-ready={fontsReady && created ? "1" : "0"}
      data-panels-drawn={layout.counts.drawn}
      data-panels-expected={layout.counts.expected}
      data-units-total={layout.counts.unitsTotal}
    >
      <style>{LABEL_CSS}</style>
      <Canvas
        shadows={DBG.includes('noshadow') ? false : "percentage"}
        dpr={dpr}
        gl={{ antialias: false, powerPreference: "high-performance", stencil: false }}
        camera={{ fov: 34, near: 0.5, far: 1600, position: [-40, 90, 150] }}
        onCreated={() => setCreated(true)}
        onPointerMissed={() => onSelectParty(null)}
      >
        <PerformanceMonitor onDecline={() => setDpr(1)} onIncline={() => setDpr([1, 2])} />
        <CityContent
          state={state}
          layout={layout}
          selected={selectedPartyId}
          hovered={hoveredLive}
          onHover={setHovered}
          onSelect={select}
          insets={insets}
          calm={calm}
          fontsReady={fontsReady}
          registry={registry.current}
        />
      </Canvas>
      {!DBG.includes('nolabels') && <LabelLayer specs={labels} registry={registry.current} onHover={setHovered} onSelect={select} />}
    </div>
  );
}

export interface PanelModelProps {
  state: PanelPageState | null;
  /** "testing" while a Run test is in flight: the model animates its sounder/strobe and LCD. */
  phase: "idle" | "testing" | "updating";
  /** Result of the latest action, to flash PASS/FAIL on the model. */
  flash: "pass" | "fail" | null;
}

export function PanelModel(props: PanelModelProps) {
  return <PanelModelView {...props} />;
}
