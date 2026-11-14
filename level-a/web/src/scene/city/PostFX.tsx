// Bloom for the lit city (lamps, panel displays, beams, the headline) over an MSAA render target,
// then one output pass for tone mapping and sRGB. Takes over rendering (priority 1).

import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo } from "react";
import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";

export function PostFX({ strength = 0.85 }: { strength?: number }) {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);
  const camera = useThree((s) => s.camera);
  const size = useThree((s) => s.size);
  const dpr = useThree((s) => s.viewport.dpr);

  const { composer, bloom } = useMemo(() => {
    const target = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: 4 });
    const c = new EffectComposer(gl, target);
    c.addPass(new RenderPass(scene, camera));
    const b = new UnrealBloomPass(new THREE.Vector2(256, 256), strength, 0.55, 0.82);
    c.addPass(b);
    c.addPass(new OutputPass());
    return { composer: c, bloom: b };
  }, [gl, scene, camera, strength]);

  useEffect(() => () => composer.dispose(), [composer]);
  useEffect(() => {
    composer.setPixelRatio(dpr);
    composer.setSize(size.width, size.height);
    bloom.resolution.set(size.width * dpr * 0.5, size.height * dpr * 0.5);
  }, [composer, bloom, size.width, size.height, dpr]);

  useFrame((_, delta) => {
    composer.render(delta);
  }, 1);
  return null;
}
