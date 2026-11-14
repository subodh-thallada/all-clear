// Text drawn into canvas textures (the headline billboard, signs, the order-system screens, the
// panel's LCD). Canvas text uses the bundled fonts once they are loaded; textures are redrawn only
// when their content changes.

import * as THREE from "three";

export interface CanvasSurface {
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  texture: THREE.CanvasTexture;
}

export function createSurface(width: number, height: number): CanvasSurface {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2D canvas is not available");
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 8;
  texture.generateMipmaps = true;
  texture.minFilter = THREE.LinearMipmapLinearFilter;
  return { canvas, ctx, texture };
}

export function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** Sets the largest font size (down to `min`) at which `text` fits in `maxWidth`. */
export function fitFont(ctx: CanvasRenderingContext2D, text: string, weight: number, family: string, size: number, maxWidth: number, min = 10): number {
  let s = size;
  ctx.font = `${weight} ${s}px ${family}`;
  while (s > min && ctx.measureText(text).width > maxWidth) {
    s -= 2;
    ctx.font = `${weight} ${s}px ${family}`;
  }
  return s;
}

/** Text with extra letter spacing (canvas letterSpacing is not available everywhere). */
export function spacedText(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, spacing: number, align: "left" | "center" | "right" = "left") {
  const widths = [...text].map((ch) => ctx.measureText(ch).width);
  const total = widths.reduce((s, w) => s + w, 0) + spacing * Math.max(0, text.length - 1);
  let cx = align === "left" ? x : align === "center" ? x - total / 2 : x - total;
  const saved = ctx.textAlign;
  ctx.textAlign = "left";
  [...text].forEach((ch, i) => {
    ctx.fillText(ch, cx, y);
    cx += widths[i] + spacing;
  });
  ctx.textAlign = saved;
  return total;
}
