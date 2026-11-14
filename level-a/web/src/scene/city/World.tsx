// The stage: a dusk sky with the last light in the west (behind the plant), a road grid that runs
// on into the dark, street lamps, and lights tuned so the lit city reads against a dark UI.

import { Stars } from "@react-three/drei";
import { useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three";
import { GRID } from "./layout";

const MID_C = (GRID.cols - 1) / 2;
const MID_R = (GRID.rows - 1) / 2;

function Sky() {
  const material = useMemo(
    () =>
      new THREE.ShaderMaterial({
        side: THREE.BackSide,
        depthWrite: false,
        fog: false,
        vertexShader: /* glsl */ `
          varying vec3 vDir;
          void main() {
            vDir = normalize(position);
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }`,
        fragmentShader: /* glsl */ `
          varying vec3 vDir;
          void main() {
            float y = vDir.y;
            vec3 zenith = vec3(0.006, 0.010, 0.022);
            vec3 high = vec3(0.016, 0.026, 0.060);
            vec3 horizon = vec3(0.060, 0.058, 0.110);
            vec3 col = mix(horizon, high, smoothstep(0.0, 0.25, y));
            col = mix(col, zenith, smoothstep(0.25, 0.8, y));
            float west = pow(max(0.0, dot(normalize(vec3(vDir.x, 0.0, vDir.z)), vec3(-0.92, 0.0, 0.38))), 3.0);
            float band = exp(-abs(y - 0.02) * 16.0);
            col += vec3(0.95, 0.38, 0.14) * band * west * 0.55;
            col += vec3(0.45, 0.22, 0.32) * exp(-abs(y) * 6.0) * west * 0.25;
            col = mix(vec3(0.012, 0.016, 0.026), col, smoothstep(-0.08, 0.0, y));
            gl_FragColor = vec4(col, 1.0);
            #include <colorspace_fragment>
          }`,
      }),
    [],
  );
  useEffect(() => () => material.dispose(), [material]);
  return (
    <mesh material={material} renderOrder={-10} frustumCulled={false}>
      <sphereGeometry args={[520, 32, 16]} />
    </mesh>
  );
}

function Ground() {
  const material = useMemo(() => {
    const m = new THREE.MeshLambertMaterial({ color: "#ffffff" });
    m.onBeforeCompile = (shader) => {
      shader.vertexShader = shader.vertexShader
        .replace("#include <common>", "#include <common>\nvarying vec2 vGround;")
        .replace("#include <worldpos_vertex>", "#include <worldpos_vertex>\nvGround = (modelMatrix * vec4(transformed, 1.0)).xz;");
      shader.fragmentShader = shader.fragmentShader
        .replace("#include <common>", "#include <common>\nvarying vec2 vGround;")
        .replace(
          "#include <color_fragment>",
          /* glsl */ `#include <color_fragment>
          {
            float pitch = ${GRID.pitch.toFixed(1)};
            float halfLot = ${(GRID.lot / 2).toFixed(2)};
            vec2 cell = vGround / pitch + vec2(${MID_C.toFixed(2)}, ${MID_R.toFixed(2)});
            vec2 f = abs(fract(cell + 0.5) - 0.5) * pitch;      // distance from the lot centre
            float roadX = step(halfLot, f.x);
            float roadZ = step(halfLot, f.y);
            float road = max(roadX, roadZ);
            vec3 asphalt = vec3(0.034, 0.040, 0.052);
            vec3 lot = vec3(0.050, 0.058, 0.072);
            vec3 kerb = vec3(0.085, 0.094, 0.11);
            float kerbBand = step(halfLot, max(f.x, f.y)) - step(halfLot + 0.28, max(f.x, f.y));
            vec3 col = mix(lot, asphalt, road);
            col = mix(col, kerb, kerbBand * 0.85);
            // Dashed centre lines on the roads.
            float cx = 1.0 - smoothstep(0.04, 0.09, abs(f.x - pitch * 0.5));
            float cz = 1.0 - smoothstep(0.04, 0.09, abs(f.y - pitch * 0.5));
            float dashX = step(0.5, fract(vGround.y * 0.35)) * cx;
            float dashZ = step(0.5, fract(vGround.x * 0.35)) * cz;
            float crossing = roadX * roadZ;
            float lines = max(dashX, dashZ) * (1.0 - crossing);
            col = mix(col, vec3(0.32, 0.26, 0.15), lines * 0.55);
            float d = length(vGround) / 190.0;
            col *= mix(1.0, 0.45, smoothstep(0.55, 1.2, d));
            diffuseColor.rgb = col;
          }`,
        );
    };
    return m;
  }, []);
  useEffect(() => () => material.dispose(), [material]);
  return (
    <mesh rotation-x={-Math.PI / 2} position={[0, -0.01, 0]} receiveShadow material={material}>
      <planeGeometry args={[900, 900]} />
    </mesh>
  );
}

/** Lamps at the road crossings, each with a warm pool of light painted on the asphalt. */
function StreetLights() {
  const { poles, bulbs, pools } = useMemo(() => {
    const spots: [number, number][] = [];
    for (let r = -1; r < GRID.rows; r++) {
      for (let c = -1; c < GRID.cols; c++) {
        spots.push([(c - MID_C + 0.5) * GRID.pitch + 1.25, (r - MID_R + 0.5) * GRID.pitch + 1.25]);
      }
    }
    const pole = new THREE.InstancedMesh(new THREE.BoxGeometry(0.09, 2.6, 0.09).translate(0, 1.3, 0), new THREE.MeshStandardMaterial({ color: "#2c333d", roughness: 0.6, metalness: 0.4 }), spots.length);
    const bulb = new THREE.InstancedMesh(new THREE.BoxGeometry(0.34, 0.07, 0.2).translate(-0.18, 2.62, 0), new THREE.MeshBasicMaterial({ color: new THREE.Color("#ffc78a").multiplyScalar(4) }), spots.length);
    const poolMat = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position,1.0); }",
      fragmentShader:
        "varying vec2 vUv; void main(){ float r = length(vUv*2.0-1.0); float a = pow(max(0.0, 1.0 - r), 2.2) * 0.22; gl_FragColor = vec4(vec3(1.0,0.66,0.36)*a, a); }",
    });
    const pool = new THREE.InstancedMesh(new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2), poolMat, spots.length);
    const m = new THREE.Matrix4();
    spots.forEach(([x, z], i) => {
      m.makeTranslation(x, 0, z);
      pole.setMatrixAt(i, m);
      bulb.setMatrixAt(i, m);
      m.makeScale(7, 1, 7).setPosition(x - 0.4, 0.02, z);
      pool.setMatrixAt(i, m);
    });
    for (const o of [pole, bulb, pool]) {
      o.frustumCulled = false;
      o.instanceMatrix.needsUpdate = true;
    }
    pole.castShadow = true;
    pool.renderOrder = 1;
    return { poles: pole, bulbs: bulb, pools: pool };
  }, []);
  useEffect(
    () => () => {
      for (const o of [poles, bulbs, pools]) {
        o.geometry.dispose();
        (o.material as THREE.Material).dispose();
        o.dispose();
      }
    },
    [poles, bulbs, pools],
  );
  return (
    <>
      <primitive object={poles} />
      <primitive object={bulbs} />
      <primitive object={pools} />
    </>
  );
}

/** A small dusk environment (sunset strip in the west, blue sky above) baked once into a PMREM
 *  texture from a scene of glowing panels: reflections for the glass, no network fetch. */
export function useDuskEnvironment(): THREE.Texture {
  const gl = useThree((s) => s.gl);
  const target = useMemo(() => {
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#0b1222");
    const panels: [string, number, number, number, [number, number, number]][] = [
      ["#ff9a5c", 3.2, 140, 12, [-80, 6, 10]],
      ["#4c6cff", 0.8, 160, 160, [0, 70, 0]],
      ["#9fb6ff", 0.7, 120, 14, [80, 10, -20]],
      ["#ffd9a6", 1.6, 24, 24, [20, 16, 60]],
      ["#1a1410", 1, 200, 200, [0, -40, 0]],
    ];
    const disposables: { dispose: () => void }[] = [];
    for (const [color, k, w, h, pos] of panels) {
      const geo = new THREE.PlaneGeometry(w, h);
      const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(k), side: THREE.DoubleSide });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(pos[0], pos[1], pos[2]);
      mesh.lookAt(0, 0, 0);
      scene.add(mesh);
      disposables.push(geo, mat);
    }
    const pmrem = new THREE.PMREMGenerator(gl);
    const rt = pmrem.fromScene(scene, 0.02);
    pmrem.dispose();
    disposables.forEach((d) => d.dispose());
    return rt;
  }, [gl]);
  useEffect(() => () => target.dispose(), [target]);
  return target.texture;
}

export function World({ calm }: { calm: boolean }) {
  return (
    <>
      <color attach="background" args={["#05070c"]} />
      <fog attach="fog" args={["#0a0f1a", 150, 420]} />
      <Sky />
      <Stars radius={360} depth={60} count={1400} factor={5} saturation={0} fade speed={calm ? 0 : 0.3} />
      <hemisphereLight args={["#46608f", "#120e0b", 0.75]} />
      <ambientLight intensity={0.12} color="#8fa6d6" />
      <directionalLight
        position={[-110, 62, 46]}
        intensity={2.1}
        color="#ffb07a"
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-camera-left={-100}
        shadow-camera-right={100}
        shadow-camera-top={80}
        shadow-camera-bottom={-80}
        shadow-camera-near={10}
        shadow-camera-far={320}
        shadow-bias={-0.0004}
        shadow-normalBias={0.04}
      />
      <directionalLight position={[70, 90, -60]} intensity={0.65} color="#86a8ff" />
      <Ground />
      <StreetLights />
    </>
  );
}
