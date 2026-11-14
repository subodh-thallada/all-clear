// The instanced city: architecture in four materials, the 2,400 panels, the ghosts, the trees, and
// one invisible hit box per party for hover and click.

import { Line } from "@react-three/drei";
import { useFrame, useThree, type ThreeEvent } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { COLORS } from "../theme";
import { BoxLayer, capacityFor, PanelLayer, TreeLayer, type RiseOf } from "./instancing";
import type { BoxInst, CityLayout, PanelInst, PartyPlace, TreeInst } from "./layout";
import { patchMaterial, type SceneUniforms } from "./materials";

interface Materials {
  plate: THREE.MeshLambertMaterial;
  solid: THREE.MeshStandardMaterial;
  solidDepth: THREE.MeshDepthMaterial;
  glass: THREE.MeshStandardMaterial;
  light: THREE.MeshBasicMaterial;
  facade: THREE.MeshLambertMaterial;
  panel: THREE.MeshStandardMaterial;
  ghost: THREE.MeshBasicMaterial;
  tree: THREE.MeshLambertMaterial;
}

// Large, quiet surfaces (lots, anonymous buildings, trees) use Lambert shading; the party
// architecture and the panels keep physically based shading; only the glass samples the dusk
// environment. That split is what keeps the city at frame rate on a laptop's integrated GPU.
function useMaterials(uniforms: SceneUniforms, envMap: THREE.Texture | null): Materials {
  const mats = useMemo<Materials>(
    () => ({
      plate: patchMaterial(new THREE.MeshLambertMaterial(), uniforms),
      solid: patchMaterial(new THREE.MeshStandardMaterial({ roughness: 0.74, metalness: 0.18 }), uniforms),
      solidDepth: patchMaterial(new THREE.MeshDepthMaterial({ depthPacking: THREE.RGBADepthPacking }), uniforms),
      glass: patchMaterial(
        new THREE.MeshStandardMaterial({ transparent: true, opacity: 0.2, roughness: 0.05, metalness: 0.9, depthWrite: false, envMap, envMapIntensity: 1.5 }),
        uniforms,
      ),
      light: patchMaterial(new THREE.MeshBasicMaterial({ color: new THREE.Color(3.2, 3.2, 3.2) }), uniforms),
      facade: patchMaterial(new THREE.MeshLambertMaterial(), uniforms, { facade: true }),
      panel: patchMaterial(new THREE.MeshStandardMaterial({ roughness: 0.42, metalness: 0.2 }), uniforms, { anim: true }),
      ghost: patchMaterial(
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.55, depthWrite: false, blending: THREE.AdditiveBlending }),
        uniforms,
        { anim: true, edges: true },
      ),
      tree: patchMaterial(new THREE.MeshLambertMaterial(), uniforms),
    }),
    [uniforms, envMap],
  );
  useEffect(() => () => Object.values(mats).forEach((m) => m.dispose()), [mats]);
  return mats;
}

function BoxLayerView({
  list,
  material,
  riseOf,
  shadows,
  receive = shadows,
  depth,
}: {
  list: BoxInst[];
  material: THREE.Material;
  riseOf: RiseOf;
  shadows: boolean;
  receive?: boolean;
  depth?: THREE.Material;
}) {
  const cap = capacityFor(list.length);
  const layer = useMemo(() => new BoxLayer(material, cap), [material, cap]);
  useEffect(() => () => layer.dispose(), [layer]);
  useLayoutEffect(() => {
    layer.mesh.castShadow = shadows;
    layer.mesh.receiveShadow = receive;
    if (depth) layer.mesh.customDepthMaterial = depth;
    layer.write(list, riseOf);
  }, [layer, list, riseOf, shadows, receive, depth]);
  return <primitive object={layer.mesh} />;
}

function TreeLayerView({ list, material, riseOf }: { list: TreeInst[]; material: THREE.Material; riseOf: RiseOf }) {
  const cap = capacityFor(list.length);
  const layer = useMemo(() => new TreeLayer(material, cap), [material, cap]);
  useEffect(() => () => layer.dispose(), [layer]);
  useLayoutEffect(() => {
    layer.mesh.castShadow = true;
    layer.mesh.receiveShadow = true;
    layer.write(list, riseOf);
  }, [layer, list, riseOf]);
  return <primitive object={layer.mesh} />;
}

function PanelLayerView({
  list,
  material,
  riseOf,
  animate,
  popOnMove,
  renderOrder,
}: {
  list: PanelInst[];
  material: THREE.Material;
  riseOf: RiseOf;
  animate: boolean;
  popOnMove: boolean;
  renderOrder: number;
}) {
  const clock = useThree((s) => s.clock);
  const cap = capacityFor(list.length);
  const previous = useRef<PanelLayer | null>(null);
  const layer = useMemo(() => {
    const l = new PanelLayer(material, cap, popOnMove);
    if (previous.current) l.takeMemoFrom(previous.current);
    return l;
  }, [material, cap, popOnMove]);
  useEffect(() => {
    previous.current = layer;
    return () => layer.dispose();
  }, [layer]);
  useLayoutEffect(() => {
    layer.mesh.renderOrder = renderOrder;
    layer.mesh.receiveShadow = renderOrder === 0;
    layer.write(list, riseOf, clock.elapsedTime, animate);
  }, [layer, list, riseOf, animate, clock, renderOrder]);
  return <primitive object={layer.mesh} />;
}

export function Structures({
  layout,
  uniforms,
  envMap,
  riseOf,
  animate,
}: {
  layout: CityLayout;
  uniforms: SceneUniforms;
  envMap: THREE.Texture | null;
  riseOf: RiseOf;
  animate: boolean;
}) {
  const m = useMaterials(uniforms, envMap);
  return (
    <>
      <BoxLayerView list={layout.plates} material={m.plate} riseOf={riseOf} shadows={false} receive />
      <BoxLayerView list={layout.solid} material={m.solid} riseOf={riseOf} shadows depth={m.solidDepth} />
      <BoxLayerView list={layout.facade} material={m.facade} riseOf={riseOf} shadows depth={m.solidDepth} />
      <BoxLayerView list={layout.light} material={m.light} riseOf={riseOf} shadows={false} />
      <TreeLayerView list={layout.trees} material={m.tree} riseOf={riseOf} />
      <PanelLayerView list={layout.panels} material={m.panel} riseOf={riseOf} animate={animate} popOnMove renderOrder={0} />
      <BoxLayerView list={layout.glass} material={m.glass} riseOf={riseOf} shadows={false} />
      <PanelLayerView list={layout.ghosts} material={m.ghost} riseOf={riseOf} animate={animate} popOnMove={false} renderOrder={3} />
    </>
  );
}

/** Shadows are re-rendered only while something is rising, not every frame. */
export function ShadowDirector({ shape }: { shape: number }) {
  const gl = useThree((s) => s.gl);
  const clock = useThree((s) => s.clock);
  const until = useRef(0);
  useEffect(() => {
    gl.shadowMap.autoUpdate = false;
    gl.shadowMap.needsUpdate = true;
  }, [gl]);
  useEffect(() => {
    until.current = clock.elapsedTime + 3.2;
  }, [shape, clock]);
  useFrame(({ clock: c }) => {
    if (c.elapsedTime < until.current) gl.shadowMap.needsUpdate = true;
  });
  return null;
}

// ---------------------------------------------------------------------------------------------
// Picking

export function PartyHits({
  places,
  onHover,
  onSelect,
}: {
  places: PartyPlace[];
  onHover: (id: string | null) => void;
  onSelect: (id: string) => void;
}) {
  const cap = capacityFor(places.length);
  const mesh = useMemo(() => {
    const mat = new THREE.MeshBasicMaterial({ visible: false });
    const m = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), mat, cap);
    m.frustumCulled = false;
    return m;
  }, [cap]);
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
    places.forEach((p, i) => {
      const h = Math.max(2.5, p.top);
      mat.makeScale(p.w, h, p.d).setPosition(p.x, h / 2, p.z);
      mesh.setMatrixAt(i, mat);
    });
    mesh.count = places.length;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
  }, [mesh, places]);
  const idAt = (e: ThreeEvent<PointerEvent | MouseEvent>) => (e.instanceId !== undefined ? places[e.instanceId]?.id ?? null : null);
  return (
    <primitive
      object={mesh}
      onPointerMove={(e: ThreeEvent<PointerEvent>) => {
        e.stopPropagation();
        onHover(idAt(e));
      }}
      onPointerOut={() => onHover(null)}
      onClick={(e: ThreeEvent<MouseEvent>) => {
        e.stopPropagation();
        if (e.delta > 4) return;
        const id = idAt(e);
        if (id) onSelect(id);
      }}
    />
  );
}

/** A lit outline around the hovered and the selected lot. */
export function LotHighlight({ place, kind }: { place: PartyPlace | null; kind: "hover" | "select" }) {
  const points = useMemo(() => {
    if (!place) return null;
    const hw = place.w / 2 + 0.35;
    const hd = place.d / 2 + 0.35;
    const y = 0.16;
    return [
      new THREE.Vector3(place.x - hw, y, place.z - hd),
      new THREE.Vector3(place.x + hw, y, place.z - hd),
      new THREE.Vector3(place.x + hw, y, place.z + hd),
      new THREE.Vector3(place.x - hw, y, place.z + hd),
      new THREE.Vector3(place.x - hw, y, place.z - hd),
    ];
  }, [place]);
  if (!points) return null;
  return (
    <Line
      points={points}
      color={kind === "select" ? COLORS.live : "#ffffff"}
      lineWidth={kind === "select" ? 3 : 1.6}
      transparent
      opacity={kind === "select" ? 1 : 0.7}
      toneMapped={false}
    />
  );
}
