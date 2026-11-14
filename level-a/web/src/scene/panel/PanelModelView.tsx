// The simulated DEMO-7 fire alarm control panel, for the panel page. A red steel cabinet on a plant
// room wall: a hinged door with a window, behind it an LCD that shows the model, serial, software
// version and last test from PanelPageState, the LED column (POWER, ALARM, TROUBLE, SUPERVISORY) and
// a keypad; a horn-strobe beside it and a SIMULATED placard above it.
//
// phase "testing": the strobe flashes and the sounder rings (visible sound waves), unless the test
// fails (flash = "fail"): then the sounder stays silent and TROUBLE lights. phase "updating": a
// progress bar runs on the LCD toward the fixed version. flash colours the display pass / fail.

import { Environment, Lightformer, OrbitControls, RoundedBox } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import type { PanelPageState } from "../../api/types";
import { createSurface, fitFont, roundRect, spacedText, type CanvasSurface } from "../canvasText";
import { PostFX } from "../city/PostFX";
import { COLORS, FONT_DISPLAY, FONT_MONO, loadSceneFonts } from "../theme";

type Phase = "idle" | "testing" | "updating";
type Flash = "pass" | "fail" | null;

interface Props {
  state: PanelPageState | null;
  phase: Phase;
  flash: Flash;
}

const RED = "#b3221c";
const LCD_W = 2.3;
const LCD_H = 0.95;
const LCD_X = 0.4;
const LCD_Y = 1.68;

function useSurface(w: number, h: number): CanvasSurface {
  const s = useMemo(() => createSurface(w, h), [w, h]);
  useEffect(() => () => s.texture.dispose(), [s]);
  return s;
}

function versionWord(state: PanelPageState): { word: string; color: string } {
  const rc = state.recall;
  if (!rc) return { word: "", color: "#1d2a10" };
  if (state.sw_version === rc.fixed_version) return { word: "FIXED VERSION", color: "#0f3b1d" };
  if (rc.affected_versions.includes(state.sw_version)) return { word: "AFFECTED", color: "#5a0d07" };
  return { word: "", color: "#1d2a10" };
}

function drawLcd(s: CanvasSurface, state: PanelPageState | null, phase: Phase, flash: Flash) {
  const { ctx, canvas, texture } = s;
  const W = canvas.width;
  const H = canvas.height;
  const bg = flash === "pass" ? "#a6e8a0" : flash === "fail" ? "#f2a493" : "#b9cf7a";
  const ink = flash === "fail" ? "#3a0703" : flash === "pass" ? "#08300f" : "#1c260c";
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
  const glow = ctx.createRadialGradient(W / 2, H / 2, 40, W / 2, H / 2, W * 0.7);
  glow.addColorStop(0, "rgba(255,255,255,0.18)");
  glow.addColorStop(1, "rgba(0,0,0,0.12)");
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = ink;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  const mono = (size: number, weight = 600) => {
    ctx.font = `${weight} ${size}px ${FONT_MONO}`;
  };
  if (!state) {
    mono(48);
    ctx.fillText("NO PANEL STATE", 40, 120);
    mono(34, 500);
    ctx.fillText("waiting for /panel-api", 40, 190);
    texture.needsUpdate = true;
    return;
  }
  mono(40);
  ctx.fillText(`KESTREL ${state.model}`, 36, 62);
  ctx.textAlign = "right";
  ctx.fillText("SIMULATED", W - 36, 62);
  ctx.textAlign = "left";
  ctx.fillRect(36, 80, W - 72, 3);
  mono(40, 500);
  ctx.fillText(`SN ${state.serial}`, 36, 136);
  ctx.textAlign = "right";
  ctx.fillText(`BATCH ${state.batch_code}`, W - 36, 136);
  ctx.textAlign = "left";

  if (phase === "updating") {
    mono(52);
    ctx.fillText(`UPDATING ${state.sw_version} > ${state.recall?.fixed_version ?? ""}`, 36, 214);
    mono(30, 500);
    ctx.fillText("do not power down", 36, 256);
    ctx.strokeStyle = ink;
    ctx.lineWidth = 4;
    ctx.strokeRect(36, 290, W - 72, 56);
  } else {
    const v = versionWord(state);
    mono(56);
    ctx.fillText(`SOFTWARE ${state.sw_version}`, 36, 216);
    if (v.word) {
      mono(34);
      const tw = ctx.measureText(v.word).width;
      ctx.fillStyle = v.color;
      roundRect(ctx, W - 36 - tw - 28, 176, tw + 28, 50, 6);
      ctx.fill();
      ctx.fillStyle = bg;
      ctx.fillText(v.word, W - 36 - tw - 14, 213);
      ctx.fillStyle = ink;
    }
    if (phase === "testing") {
      mono(48);
      ctx.fillText(flash === "fail" ? "TEST: NO SOUNDER" : "TESTING  SOUNDER + STROBE", 36, 300);
    } else if (flash === "pass" || flash === "fail") {
      ctx.font = `700 92px ${FONT_DISPLAY}`;
      spacedText(ctx, flash === "pass" ? "TEST PASS" : "TEST FAIL", 36, 330, 6);
      mono(30, 500);
      const detail = state.last_test?.detail ?? "";
      ctx.textAlign = "right";
      ctx.fillText(detail.slice(0, 26), W - 36, 330);
      ctx.textAlign = "left";
    } else {
      const lt = state.last_test;
      mono(44);
      ctx.fillText(lt ? `LAST TEST ${lt.result.toUpperCase()}` : "NO TEST YET", 36, 296);
      mono(30, 500);
      ctx.fillText(lt ? `${lt.detail}  ${lt.sim_label}`.slice(0, 44) : "press Run test", 36, 340);
    }
  }
  mono(26, 500);
  ctx.fillText(state.fixed ? "FIXED BY ITS OWN TEST" : state.site_label.slice(0, 40), 36, H - 26);
  texture.needsUpdate = true;
}

function drawFace(s: CanvasSurface, model: string) {
  // The faceplate behind the window: LED legends, keypad legends, the brand.
  const { ctx, canvas, texture } = s;
  const W = canvas.width;
  const H = canvas.height;
  ctx.fillStyle = "#1c2128";
  ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 3;
  ctx.strokeRect(10, 10, W - 20, H - 20);
  ctx.fillStyle = "#c9d1dc";
  ctx.font = `600 30px ${FONT_MONO}`;
  ctx.textBaseline = "middle";
  const leds = ["POWER", "ALARM", "TROUBLE", "SUPERVISORY"];
  leds.forEach((l, i) => ctx.fillText(l, 104, 420 + i * 74));
  ctx.font = `600 24px ${FONT_MONO}`;
  ctx.textAlign = "center";
  const keys = ["ACK", "SILENCE", "RESET", "DRILL"];
  keys.forEach((k, i) => ctx.fillText(k, 470 + i * 128, 778));
  ctx.textAlign = "left";
  ctx.fillStyle = "#e7ebf0";
  ctx.font = `700 44px ${FONT_DISPLAY}`;
  spacedText(ctx, "KESTREL", 40, 716, 6);
  ctx.fillStyle = "#8d98a6";
  ctx.font = `500 22px ${FONT_MONO}`;
  ctx.fillText("LIFE SAFETY", 42, 756);
  ctx.fillStyle = "#e7ebf0";
  ctx.font = `700 54px ${FONT_DISPLAY}`;
  ctx.fillText(model, 40, 150);
  ctx.fillStyle = "#8d98a6";
  ctx.font = `500 22px ${FONT_MONO}`;
  ctx.fillText("CONTROL PANEL", 42, 196);
  texture.needsUpdate = true;
}

function drawDoorPlate(s: CanvasSurface, model: string) {
  const { ctx, canvas, texture } = s;
  const W = canvas.width;
  const H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = "#f4efe8";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  fitFont(ctx, "FIRE ALARM", 700, FONT_DISPLAY, 120, W - 40);
  spacedText(ctx, "FIRE ALARM", W / 2, H * 0.3, 10, "center");
  ctx.font = `600 50px ${FONT_DISPLAY}`;
  spacedText(ctx, "CONTROL PANEL", W / 2, H * 0.55, 8, "center");
  ctx.font = `500 34px ${FONT_MONO}`;
  ctx.fillText(`KESTREL LIFE SAFETY · ${model}`, W / 2, H * 0.8);
  texture.needsUpdate = true;
}

function drawPlacard(s: CanvasSurface) {
  const { ctx, canvas, texture } = s;
  const W = canvas.width;
  const H = canvas.height;
  ctx.fillStyle = "#f5c518";
  ctx.fillRect(0, 0, W, H);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, W, 34);
  ctx.rect(0, H - 34, W, 34);
  ctx.clip();
  for (let x = -H; x < W + H; x += 56) {
    ctx.fillStyle = "#16181b";
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x + 28, 0);
    ctx.lineTo(x + 28 + H, H);
    ctx.lineTo(x + H, H);
    ctx.closePath();
    ctx.fill();
  }
  ctx.restore();
  ctx.fillStyle = "#16181b";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  fitFont(ctx, "SIMULATED", 700, FONT_DISPLAY, 150, W - 80);
  spacedText(ctx, "SIMULATED", W / 2, H * 0.45, 14, "center");
  ctx.font = `600 36px ${FONT_MONO}`;
  ctx.fillText("not a real fire alarm system", W / 2, H * 0.74);
  texture.needsUpdate = true;
}

function Led({ position, color, mode }: { position: [number, number, number]; color: string; mode: "off" | "on" | "blink" | "fast" }) {
  const mat = useMemo(() => new THREE.MeshStandardMaterial({ color: "#20252b", emissive: new THREE.Color(color), emissiveIntensity: 0, roughness: 0.25 }), [color]);
  useEffect(() => () => mat.dispose(), [mat]);
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    const on = mode === "on" || (mode === "blink" && Math.sin(t * 6) > 0) || (mode === "fast" && Math.sin(t * 14) > 0);
    mat.emissiveIntensity = on ? 3.4 : 0.04;
  });
  return (
    <mesh material={mat} position={position} rotation-x={Math.PI / 2}>
      <cylinderGeometry args={[0.065, 0.075, 0.06, 18]} />
    </mesh>
  );
}

function Key({ position, w = 0.22, color = "#c8ced6" }: { position: [number, number, number]; w?: number; color?: string }) {
  return (
    <RoundedBox args={[w, 0.17, 0.07]} radius={0.025} smoothness={3} position={position}>
      <meshStandardMaterial color={color} roughness={0.55} />
    </RoundedBox>
  );
}

/** Rings of sound from the horn while it sounds. */
function SoundWaves({ active, origin }: { active: boolean; origin: [number, number, number] }) {
  const refs = useRef<(THREE.Mesh | null)[]>([]);
  const mats = useMemo(() => [0, 1, 2, 3].map(() => new THREE.MeshBasicMaterial({ color: new THREE.Color(COLORS.real).multiplyScalar(2), transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide })), []);
  useEffect(() => () => mats.forEach((m) => m.dispose()), [mats]);
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    refs.current.forEach((m, i) => {
      if (!m) return;
      const k = (t * 0.9 + i * 0.25) % 1;
      m.scale.setScalar(0.3 + k * 2.6);
      m.position.z = origin[2] + 0.1 + k * 1.4;
      mats[i].opacity = active ? (1 - k) * 0.75 : 0;
    });
  });
  return (
    <group position={[origin[0], origin[1], 0]}>
      {mats.map((m, i) => (
        <mesh
          key={i}
          ref={(el) => {
            refs.current[i] = el;
          }}
          material={m}
        >
          <torusGeometry args={[0.5, 0.025, 8, 48]} />
        </mesh>
      ))}
    </group>
  );
}

function Device({ state, phase, flash, fontsReady }: Props & { fontsReady: boolean }) {
  const lcd = useSurface(1024, 410);
  const face = useSurface(1024, 840);
  const plate = useSurface(1024, 420);
  const placard = useSurface(1024, 360);
  const progress = useRef<THREE.Mesh>(null);
  const strobe = useRef<THREE.PointLight>(null);
  const progressStart = useRef(0);
  const lens = useMemo(() => new THREE.MeshStandardMaterial({ color: "#e8eef5", emissive: new THREE.Color("#ffffff"), emissiveIntensity: 0, transparent: true, opacity: 0.85, roughness: 0.1 }), []);
  const lcdMat = useMemo(() => new THREE.MeshBasicMaterial({ toneMapped: false }), []);
  const lcdInk = useMemo(() => new THREE.MeshBasicMaterial({ color: "#1c260c" }), []);
  useEffect(
    () => () => {
      lens.dispose();
      lcdMat.dispose();
      lcdInk.dispose();
    },
    [lens, lcdMat, lcdInk],
  );

  useLayoutEffect(() => {
    drawLcd(lcd, state, phase, flash);
    lcdMat.map = lcd.texture;
    lcdMat.color.setScalar(flash ? 1.35 : 1.15);
    lcdMat.needsUpdate = true;
    lcdInk.color.set(flash === "fail" ? "#3a0703" : flash === "pass" ? "#08300f" : "#1c260c");
  }, [lcd, state, phase, flash, fontsReady, lcdMat, lcdInk]);
  useLayoutEffect(() => drawFace(face, state?.model ?? "DEMO-7"), [face, state?.model, fontsReady]);
  useLayoutEffect(() => drawDoorPlate(plate, state?.model ?? "DEMO-7"), [plate, state?.model, fontsReady]);
  useLayoutEffect(() => drawPlacard(placard), [placard, fontsReady]);
  useEffect(() => {
    if (phase === "updating") progressStart.current = performance.now() / 1000;
  }, [phase]);

  const failing = flash === "fail" || (phase === "idle" && !flash && state?.last_test?.result === "fail" && !state.fixed);
  const sounding = phase === "testing" && flash !== "fail";

  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    const flashOn = phase === "testing" && t % 0.75 < 0.09;
    lens.emissiveIntensity = flashOn ? 9 : 0.05;
    if (strobe.current) strobe.current.intensity = flashOn ? 60 : 0;
    if (progress.current) {
      const k = phase === "updating" ? Math.min(1, (performance.now() / 1000 - progressStart.current) / 2.6) : 0;
      progress.current.visible = phase === "updating";
      progress.current.scale.x = Math.max(0.001, k);
      progress.current.position.x = LCD_X - LCD_W * 0.45 + (LCD_W * 0.9 * k) / 2;
    }
  });

  const ledMode = {
    power: "on" as const,
    alarm: phase === "testing" ? ("blink" as const) : ("off" as const),
    trouble: failing ? (phase === "testing" || flash === "fail" ? ("fast" as const) : ("blink" as const)) : ("off" as const),
    supervisory: "off" as const,
  };

  // Cabinet: 4.2 x 5.4 x 1.0, front face at z = 0.5.
  const doorZ = 0.56;
  const faceZ = 0.46;
  return (
    <group position={[0, 3.2, 0]}>
      <RoundedBox args={[4.2, 5.4, 1.0]} radius={0.08} smoothness={4} castShadow receiveShadow>
        <meshStandardMaterial color={RED} roughness={0.42} metalness={0.35} />
      </RoundedBox>
      {/* Faceplate behind the window */}
      <mesh position={[0, 1.0, faceZ + 0.02]}>
        <planeGeometry args={[3.3, 2.7]} />
        <meshBasicMaterial map={face.texture} />
      </mesh>
      <mesh material={lcdMat} position={[LCD_X, LCD_Y, faceZ + 0.035]}>
        <planeGeometry args={[LCD_W, LCD_H]} />
      </mesh>
      <mesh position={[LCD_X, LCD_Y, faceZ + 0.03]}>
        <boxGeometry args={[LCD_W + 0.12, LCD_H + 0.12, 0.02]} />
        <meshStandardMaterial color="#0b0d10" roughness={0.6} />
      </mesh>
      <mesh ref={progress} material={lcdInk} position={[LCD_X - LCD_W * 0.45, LCD_Y - LCD_H * 0.262, faceZ + 0.04]} visible={false}>
        <planeGeometry args={[LCD_W * 0.9, 0.1]} />
      </mesh>
      <Led position={[-1.44, 1.0, faceZ + 0.04]} color={COLORS.good} mode={ledMode.power} />
      <Led position={[-1.44, 1.0 - 0.238, faceZ + 0.04]} color="#ff2a1a" mode={ledMode.alarm} />
      <Led position={[-1.44, 1.0 - 0.476, faceZ + 0.04]} color="#ffb300" mode={ledMode.trouble} />
      <Led position={[-1.44, 1.0 - 0.714, faceZ + 0.04]} color="#ffe14a" mode={ledMode.supervisory} />
      {/* Keypad: 3 x 4 digits and the four function keys */}
      {Array.from({ length: 12 }, (_, i) => (
        <Key key={i} position={[0.32 + (i % 3) * 0.28, 0.98 - Math.floor(i / 3) * 0.22, faceZ + 0.05]} />
      ))}
      {[0, 1, 2, 3].map((i) => (
        <Key key={`f${i}`} position={[-0.1 + i * 0.41, 0.98 - 4 * 0.22 + 0.02, faceZ + 0.05]} w={0.34} color={i === 3 ? "#d8463a" : "#e6c14a"} />
      ))}
      {/* Door: a frame around the window, the lower panel with the printed legend, hinges, lock */}
      <group position={[0, 0, doorZ]}>
        <RoundedBox args={[4.0, 0.4, 0.08]} radius={0.03} smoothness={3} position={[0, 2.4, 0]} castShadow>
          <meshStandardMaterial color={RED} roughness={0.38} metalness={0.4} />
        </RoundedBox>
        <RoundedBox args={[4.0, 2.2, 0.08]} radius={0.03} smoothness={3} position={[0, -1.5, 0]} castShadow>
          <meshStandardMaterial color={RED} roughness={0.38} metalness={0.4} />
        </RoundedBox>
        <RoundedBox args={[0.36, 2.6, 0.08]} radius={0.03} smoothness={3} position={[-1.82, 0.9, 0]}>
          <meshStandardMaterial color={RED} roughness={0.38} metalness={0.4} />
        </RoundedBox>
        <RoundedBox args={[0.36, 2.6, 0.08]} radius={0.03} smoothness={3} position={[1.82, 0.9, 0]}>
          <meshStandardMaterial color={RED} roughness={0.38} metalness={0.4} />
        </RoundedBox>
        <mesh position={[0, 0.9, 0.0]}>
          <planeGeometry args={[3.3, 2.6]} />
          <meshPhysicalMaterial color="#cfe3ff" transparent opacity={0.12} roughness={0.04} metalness={0.1} clearcoat={1} envMapIntensity={1.4} depthWrite={false} />
        </mesh>
        <mesh position={[0, -1.32, 0.045]}>
          <planeGeometry args={[3.4, 1.4]} />
          <meshBasicMaterial map={plate.texture} transparent toneMapped={false} />
        </mesh>
        <mesh position={[1.82, -0.1, 0.06]} rotation-x={Math.PI / 2}>
          <cylinderGeometry args={[0.1, 0.1, 0.06, 20]} />
          <meshStandardMaterial color="#c9ced6" roughness={0.25} metalness={0.9} />
        </mesh>
        <mesh position={[1.82, -0.1, 0.095]}>
          <boxGeometry args={[0.025, 0.09, 0.01]} />
          <meshBasicMaterial color="#111" />
        </mesh>
        {[1.9, -2.0].map((y) => (
          <mesh key={y} position={[-2.06, y, -0.02]}>
            <cylinderGeometry args={[0.055, 0.055, 0.5, 14]} />
            <meshStandardMaterial color="#8d949c" roughness={0.3} metalness={0.9} />
          </mesh>
        ))}
      </group>
      {/* Conduit to the ceiling and to the sounder */}
      <mesh position={[-1.2, 4.6, 0]}>
        <cylinderGeometry args={[0.1, 0.1, 3.8, 14]} />
        <meshStandardMaterial color="#9aa1aa" roughness={0.35} metalness={0.8} />
      </mesh>
      <mesh position={[3.0, 2.05, -0.15]} rotation-z={Math.PI / 2}>
        <cylinderGeometry args={[0.07, 0.07, 1.9, 12]} />
        <meshStandardMaterial color="#9aa1aa" roughness={0.35} metalness={0.8} />
      </mesh>
      {/* Horn-strobe */}
      <group position={[4.35, 2.05, -0.1]}>
        <RoundedBox args={[1.1, 1.55, 0.5]} radius={0.08} smoothness={4} castShadow>
          <meshStandardMaterial color={RED} roughness={0.42} metalness={0.3} />
        </RoundedBox>
        <mesh position={[0, -0.25, 0.26]} rotation-x={Math.PI / 2}>
          <cylinderGeometry args={[0.38, 0.4, 0.06, 32]} />
          <meshStandardMaterial color="#1a1c20" roughness={0.7} />
        </mesh>
        {[0.12, 0.22, 0.32].map((r) => (
          <mesh key={r} position={[0, -0.25, 0.3]}>
            <torusGeometry args={[r, 0.012, 6, 36]} />
            <meshStandardMaterial color="#3a3e45" roughness={0.6} />
          </mesh>
        ))}
        <mesh material={lens} position={[0, 0.48, 0.3]}>
          <boxGeometry args={[0.7, 0.36, 0.22]} />
        </mesh>
        <pointLight ref={strobe} position={[0, 0.5, 1.0]} color="#ffffff" intensity={0} distance={14} decay={2} />
        <SoundWaves active={sounding} origin={[0, -0.25, 0.3]} />
      </group>
      {/* SIMULATED placard */}
      <mesh position={[0.9, 4.15, -0.42]}>
        <planeGeometry args={[4.6, (4.6 * 360) / 1024]} />
        <meshStandardMaterial map={placard.texture} roughness={0.6} emissive="#ffffff" emissiveMap={placard.texture} emissiveIntensity={0.25} />
      </mesh>
    </group>
  );
}

function Room() {
  return (
    <group>
      <mesh position={[0, 4, -0.52]} receiveShadow>
        <planeGeometry args={[40, 20]} />
        <meshStandardMaterial color="#3a4048" roughness={0.92} />
      </mesh>
      <mesh rotation-x={-Math.PI / 2} position={[0, -0.5, 6]} receiveShadow>
        <planeGeometry args={[40, 14]} />
        <meshStandardMaterial color="#202429" roughness={0.85} />
      </mesh>
    </group>
  );
}

export function PanelModelView({ state, phase, flash }: Props) {
  const [fontsReady, setFontsReady] = useState(false);
  useEffect(() => {
    let live = true;
    loadSceneFonts().then(() => {
      if (live) setFontsReady(true);
    });
    return () => {
      live = false;
    };
  }, []);
  return (
    <div style={{ position: "relative", width: "100%", height: "100%", background: "#0b0e13" }} data-ready={fontsReady ? "1" : "0"}>
      <Canvas shadows dpr={[1, 2]} gl={{ antialias: false, stencil: false }} camera={{ fov: 30, position: [3.2, 5.2, 15.5], near: 0.1, far: 120 }}>
        <color attach="background" args={["#0b0e13"]} />
        <hemisphereLight args={["#9fb4d8", "#1a1410", 0.6]} />
        <spotLight
          position={[-6, 11, 9]}
          angle={0.55}
          penumbra={0.7}
          intensity={260}
          color="#ffe2c2"
          castShadow
          shadow-mapSize={[1024, 1024]}
          shadow-bias={-0.0004}
        />
        <directionalLight position={[8, 4, 6]} intensity={0.5} color="#8fb0ff" />
        <Environment resolution={128} frames={1}>
          <Lightformer form="rect" intensity={2.4} color="#ffe2c2" position={[-5, 8, 8]} scale={[8, 4, 1]} />
          <Lightformer form="rect" intensity={1.2} color="#9fb4ff" position={[8, 3, 6]} rotation-y={-Math.PI / 3} scale={[6, 6, 1]} />
          <Lightformer form="ring" intensity={1.6} color="#ffffff" position={[0, 6, 10]} scale={3} />
        </Environment>
        <Room />
        <Device state={state} phase={phase} flash={flash} fontsReady={fontsReady} />
        <OrbitControls
          makeDefault
          target={[1.1, 4.2, 0]}
          enablePan={false}
          enableDamping
          minDistance={8}
          maxDistance={22}
          minPolarAngle={1.05}
          maxPolarAngle={1.75}
          minAzimuthAngle={-0.6}
          maxAzimuthAngle={0.6}
        />
        <PostFX strength={0.6} />
      </Canvas>
    </div>
  );
}
