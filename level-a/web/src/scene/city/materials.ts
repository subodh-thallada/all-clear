// Shader patches for the instanced city. Everything that moves on its own (a lot rising when its
// party is discovered, a panel popping when it changes bucket, ghosts bobbing, disputed panels
// pulsing, lit windows on the anonymous buildings) is computed on the GPU from per-instance
// attributes and one shared time uniform, so the CPU writes instance data only when the state
// changes and nothing is allocated per frame.

import * as THREE from "three";

export interface SceneUniforms {
  uTime: { value: number };
  /** 1 = normal motion, 0 = calm (no bobbing, no pulsing). */
  uMotion: { value: number };
}

export function createUniforms(): SceneUniforms {
  return { uTime: { value: 0 }, uMotion: { value: 1 } };
}

const VERT_COMMON = /* glsl */ `
uniform float uTime;
uniform float uMotion;
attribute vec2 aRise;
float acsEaseOut(float t) { return 1.0 - pow(1.0 - t, 3.0); }
#ifdef ACS_ANIM
attribute vec3 aPrevColor;
attribute vec4 aAnim;
varying float vAcsGlow;
varying float vAcsFx;
float acsBack(float t) { float c1 = 1.70158; float c3 = c1 + 1.0; return 1.0 + c3 * pow(t - 1.0, 3.0) + c1 * pow(t - 1.0, 2.0); }
#endif
#ifdef ACS_FACADE
varying vec3 vAcsLocal;
varying vec3 vAcsSize;
varying vec3 vAcsNormal;
varying float vAcsSeed;
#endif
#ifdef ACS_EDGES
varying vec3 vAcsBox;
#endif
`;

const VERT_BEGIN = /* glsl */ `
#ifdef ACS_ANIM
{
  float pt = clamp((uTime - aAnim.x) / 0.75, 0.0, 1.0);
  if (aAnim.y > 0.5 && aAnim.y < 1.5) transformed *= max(acsBack(pt), 0.0);
  else if (aAnim.y > 1.5) transformed *= 1.0 + 0.7 * sin(pt * 3.14159265);
}
#endif
#ifdef ACS_FACADE
vAcsSize = vec3(length(instanceMatrix[0].xyz), length(instanceMatrix[1].xyz), length(instanceMatrix[2].xyz));
vAcsLocal = (position + 0.5) * vAcsSize;
vAcsNormal = normal;
vAcsSeed = fract(sin(dot(instanceMatrix[3].xz, vec2(12.9898, 78.233))) * 43758.5453);
#endif
#ifdef ACS_EDGES
vAcsBox = position;
#endif
`;

const VERT_COLOR = /* glsl */ `
#ifdef ACS_ANIM
{
  float ct = clamp((uTime - aAnim.x) / 0.9, 0.0, 1.0);
  if (aAnim.y > 1.5) vColor.xyz = mix(aPrevColor, vColor.xyz, ct);
  vAcsGlow = aAnim.z;
  vAcsFx = aAnim.w;
}
#endif
`;

const VERT_PROJECT = /* glsl */ `
vec4 mvPosition = vec4( transformed, 1.0 );
#ifdef USE_INSTANCING
  mvPosition = instanceMatrix * mvPosition;
#endif
{
  float rt = clamp((uTime - aRise.x) / 1.2, 0.0, 1.0);
  mvPosition.y = aRise.y + (mvPosition.y - aRise.y) * acsEaseOut(rt);
#ifdef ACS_ANIM
  if (aAnim.w > 1.5) mvPosition.y += sin(uTime * 1.4 + mvPosition.x * 0.9 + mvPosition.z * 0.6) * 0.08 * uMotion;
#endif
}
mvPosition = modelViewMatrix * mvPosition;
gl_Position = projectionMatrix * mvPosition;
`;

const FRAG_COMMON = /* glsl */ `
uniform float uTime;
uniform float uMotion;
#ifdef ACS_ANIM
varying float vAcsGlow;
varying float vAcsFx;
#endif
#ifdef ACS_FACADE
varying vec3 vAcsLocal;
varying vec3 vAcsSize;
varying vec3 vAcsNormal;
varying float vAcsSeed;
#endif
#ifdef ACS_EDGES
varying vec3 vAcsBox;
#endif
`;

const FRAG_EMISSIVE = /* glsl */ `
#ifdef ACS_ANIM
{
  float g = vAcsGlow;
  if (vAcsFx > 0.5 && vAcsFx < 1.5) g *= mix(1.0, 0.35 + 0.65 * (0.5 + 0.5 * sin(uTime * 4.2)), uMotion);
  totalEmissiveRadiance += vColor.rgb * g;
}
#endif
#ifdef ACS_FACADE
{
  vec3 n = vAcsNormal;
  if (abs(n.y) < 0.5) {
    bool sideX = abs(n.x) > 0.5;
    float along = sideX ? vAcsLocal.z : vAcsLocal.x;
    float span = sideX ? vAcsSize.z : vAcsSize.x;
    float cols = max(1.0, floor(span / 0.6));
    float rows = max(1.0, floor((vAcsSize.y - 0.5) / 0.7));
    float u = along / span * cols;
    float v = (vAcsLocal.y - 0.35) / (vAcsSize.y - 0.5) * rows;
    vec2 cell = floor(vec2(u, v));
    vec2 f = fract(vec2(u, v));
    float inside = step(0.0, v) * step(v, rows);
    float win = step(0.2, f.x) * step(f.x, 0.8) * step(0.24, f.y) * step(f.y, 0.76) * inside;
    float h = fract(sin(dot(cell + vec2(vAcsSeed * 91.0, n.x * 7.0 + n.z * 13.0), vec2(12.9898, 78.233))) * 43758.5453);
    float lit = step(0.6, h);
    vec3 wc = mix(vec3(1.0, 0.66, 0.36), vec3(0.58, 0.74, 1.0), step(0.9, h));
    totalEmissiveRadiance += wc * win * lit * (0.35 + 0.5 * fract(h * 7.0));
    diffuseColor.rgb *= 1.0 - 0.6 * win;
  }
}
#endif
`;

const FRAG_EDGES = /* glsl */ `
#ifdef ACS_EDGES
{
  vec3 a = abs(vAcsBox) * 2.0;
  float e = step(0.8, a.x) + step(0.8, a.y) + step(0.8, a.z);
  float edge = step(1.5, e);
  diffuseColor.a *= mix(0.14, 1.0, edge);
  diffuseColor.rgb *= mix(0.65, 1.5, edge);
}
#endif
`;

export interface PatchOptions {
  anim?: boolean;
  facade?: boolean;
  edges?: boolean;
}

/** Adds the rise animation (always) and the optional per-instance effects to a built-in material. */
export function patchMaterial<T extends THREE.Material>(mat: T, uniforms: SceneUniforms, opts: PatchOptions = {}): T {
  const defines = [opts.anim ? "#define ACS_ANIM" : "", opts.facade ? "#define ACS_FACADE" : "", opts.edges ? "#define ACS_EDGES" : ""].join("\n");
  mat.onBeforeCompile = (shader) => {
    shader.uniforms.uTime = uniforms.uTime;
    shader.uniforms.uMotion = uniforms.uMotion;
    shader.vertexShader = `${defines}\n${shader.vertexShader}`
      .replace("#include <common>", `#include <common>\n${VERT_COMMON}`)
      .replace("#include <begin_vertex>", `#include <begin_vertex>\n${VERT_BEGIN}`)
      .replace("#include <color_vertex>", `#include <color_vertex>\n${VERT_COLOR}`)
      .replace("#include <project_vertex>", VERT_PROJECT);
    shader.fragmentShader = `${defines}\n${shader.fragmentShader}`
      .replace("#include <common>", `#include <common>\n${FRAG_COMMON}`)
      .replace("#include <emissivemap_fragment>", `#include <emissivemap_fragment>\n${FRAG_EMISSIVE}`)
      .replace("#include <color_fragment>", `#include <color_fragment>\n${FRAG_EDGES}`);
  };
  mat.customProgramCacheKey = () => `acs:${opts.anim ? 1 : 0}${opts.facade ? 1 : 0}${opts.edges ? 1 : 0}:${mat.type}`;
  return mat;
}

const linearCache = new Map<string, THREE.Color>();
/** A hex colour converted once to the renderer's linear working space. */
export function linear(hex: string): THREE.Color {
  let c = linearCache.get(hex);
  if (!c) {
    c = new THREE.Color(hex);
    linearCache.set(hex, c);
  }
  return c;
}

// ---------------------------------------------------------------------------------------------
// Small shader materials used by effects (rings, ripples, sky). Each takes the shared time.

export function ringMaterial(uniforms: SceneUniforms): THREE.ShaderMaterial {
  // A flat ring whose lit arc is `aFrac` of the circle (a lease draining), optionally blinking.
  return new THREE.ShaderMaterial({
    uniforms: { uTime: uniforms.uTime, uMotion: uniforms.uMotion },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    vertexShader: /* glsl */ `
      attribute vec4 aRing;   // frac, blink, width, opacity
      attribute vec3 aColor;
      varying vec2 vUv;
      varying vec4 vRing;
      varying vec3 vColor;
      void main() {
        vUv = uv;
        vRing = aRing;
        vColor = aColor;
        gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform float uMotion;
      varying vec2 vUv;
      varying vec4 vRing;
      varying vec3 vColor;
      void main() {
        vec2 p = vUv * 2.0 - 1.0;
        float r = length(p);
        float w = vRing.z;
        float band = smoothstep(1.0 - w - 0.03, 1.0 - w, r) * (1.0 - smoothstep(0.97, 1.0, r));
        float ang = atan(p.x, p.y) / 6.2831853 + 0.5;
        float lit = step(ang, vRing.x);
        float track = 0.16;
        float blink = mix(1.0, 0.35 + 0.65 * step(0.5, fract(uTime * 1.6)), vRing.y * uMotion);
        float a = band * mix(track, 1.0, lit) * blink * vRing.w;
        gl_FragColor = vec4(vColor * a, a);
      }`,
  });
}

export function rippleMaterial(uniforms: SceneUniforms): THREE.ShaderMaterial {
  // Concentric rings running outward: a phone ringing at a party. aCall: speed, density, strength, phase.
  return new THREE.ShaderMaterial({
    uniforms: { uTime: uniforms.uTime, uMotion: uniforms.uMotion },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    vertexShader: /* glsl */ `
      attribute vec4 aCall;
      attribute vec3 aColor;
      varying vec2 vUv;
      varying vec4 vCall;
      varying vec3 vColor;
      void main() {
        vUv = uv;
        vCall = aCall;
        vColor = aColor;
        gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform float uMotion;
      varying vec2 vUv;
      varying vec4 vCall;
      varying vec3 vColor;
      void main() {
        vec2 p = vUv * 2.0 - 1.0;
        float r = length(p);
        if (r > 1.0) discard;
        float t = uTime * vCall.x * uMotion + vCall.w;
        float waves = pow(0.5 + 0.5 * cos((r * vCall.y - t) * 6.2831853), 10.0);
        float fade = (1.0 - smoothstep(0.55, 1.0, r)) * smoothstep(0.08, 0.22, r);
        float core = (1.0 - smoothstep(0.0, 0.14, r)) * 0.9;
        float a = (waves * fade + core) * vCall.z;
        gl_FragColor = vec4(vColor * a, a);
      }`,
  });
}

export function shockMaterial(uniforms: SceneUniforms): THREE.ShaderMaterial {
  // One expanding ring per instance: aShock = start time, duration, max radius (via scale), kind.
  return new THREE.ShaderMaterial({
    uniforms: { uTime: uniforms.uTime },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    vertexShader: /* glsl */ `
      uniform float uTime;
      attribute vec2 aShock;
      attribute vec3 aColor;
      varying vec2 vUv;
      varying float vT;
      varying vec3 vColor;
      void main() {
        vUv = uv;
        vColor = aColor;
        float t = clamp((uTime - aShock.x) / aShock.y, 0.0, 1.0);
        vT = (uTime < aShock.x) ? 1.0 : t;
        float s = 0.15 + 0.85 * (1.0 - pow(1.0 - t, 3.0));
        gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(position * s, 1.0);
      }`,
    fragmentShader: /* glsl */ `
      varying vec2 vUv;
      varying float vT;
      varying vec3 vColor;
      void main() {
        if (vT >= 1.0) discard;
        vec2 p = vUv * 2.0 - 1.0;
        float r = length(p);
        float w = 0.08 + 0.1 * (1.0 - vT);
        float ring = smoothstep(1.0 - w, 1.0 - w * 0.4, r) * (1.0 - smoothstep(0.98, 1.0, r));
        float fill = (1.0 - smoothstep(0.0, 1.0, r)) * 0.18;
        float a = (ring * 1.6 + fill) * (1.0 - vT);
        gl_FragColor = vec4(vColor * a, a);
      }`,
  });
}

export function cometMaterial(uniforms: SceneUniforms): THREE.ShaderMaterial {
  // Bright points that travel a quadratic arc: aP0/aP1/aP2 and aTiming = start, duration, lag, size.
  return new THREE.ShaderMaterial({
    uniforms: { uTime: uniforms.uTime },
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    vertexShader: /* glsl */ `
      uniform float uTime;
      attribute vec3 aP0;
      attribute vec3 aP1;
      attribute vec3 aP2;
      attribute vec4 aTiming;
      attribute vec3 aColor;
      varying vec3 vColor;
      varying float vA;
      void main() {
        float t = (uTime - aTiming.x - aTiming.z) / aTiming.y;
        float alive = step(0.0, t) * step(t, 1.0);
        t = clamp(t, 0.0, 1.0);
        vec3 p = (1.0 - t) * (1.0 - t) * aP0 + 2.0 * (1.0 - t) * t * aP1 + t * t * aP2;
        float size = aTiming.w * alive * (1.0 - aTiming.z * 2.5);
        vColor = aColor;
        vA = alive * (1.0 - aTiming.z * 3.0);
        vec4 mv = modelViewMatrix * vec4(p + position * size, 1.0);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */ `
      varying vec3 vColor;
      varying float vA;
      void main() {
        if (vA <= 0.0) discard;
        gl_FragColor = vec4(vColor * 2.6 * vA, vA);
      }`,
  });
}
