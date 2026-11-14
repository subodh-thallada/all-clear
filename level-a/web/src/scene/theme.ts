// Shared look of the 3D screen: one palette (the same meanings as the HUD: red = open / refused /
// disputed, green = fixed / holding, amber = awaiting fix, cyan = simulated work in flight, gold =
// a real phone call) and the two bundled typefaces, registered under scene-private family names so
// the scene never depends on the HUD's stylesheet and never fetches anything from the network.

export const COLORS = {
  void: "#05070c",
  sky: "#070b14",
  ground: "#0b0f16",
  road: "#090c12",
  lot: "#121821",
  ink: "#eef2f6",
  ink2: "#b6c0cc",
  ink3: "#8f9bab",
  bad: "#ff5a47",
  good: "#3fe08f",
  warn: "#f5b841",
  live: "#6cc4ff",
  real: "#ffc94a",
  memory: "#c3a0ff",
  slice: "#5ad8cc",
  // panel buckets
  inStock: "#8a95a5",
  installed: "#ffaa2b",
  fixed: "#3fe08f",
  returned: "#4f8dff",
  unresolved: "#ff5a47",
  disputed: "#ff3b2f",
  transit: "#b8c6ff",
} as const;

export const BUCKET_LABELS: { key: "in_stock" | "installed" | "fixed" | "returned" | "forwarded" | "unresolved"; label: string; color: string }[] = [
  { key: "in_stock", label: "in stock", color: COLORS.inStock },
  { key: "installed", label: "installed, not fixed", color: COLORS.installed },
  { key: "fixed", label: "fixed", color: COLORS.fixed },
  { key: "returned", label: "returned", color: COLORS.returned },
  { key: "forwarded", label: "forwarded", color: COLORS.transit },
  { key: "unresolved", label: "not located", color: COLORS.unresolved },
];

export const FONT_DISPLAY = '"ACS Barlow Condensed", "Barlow Condensed", "Arial Narrow", "Segoe UI", sans-serif';
export const FONT_MONO = '"ACS Plex Mono", "IBM Plex Mono", "Cascadia Mono", Consolas, ui-monospace, monospace';

const FACES: [family: string, file: string, weight: string][] = [
  ["ACS Barlow Condensed", "BarlowCondensed-500.woff2", "500"],
  ["ACS Barlow Condensed", "BarlowCondensed-600.woff2", "600"],
  ["ACS Barlow Condensed", "BarlowCondensed-700.woff2", "700"],
  ["ACS Plex Mono", "IBMPlexMono-400.woff2", "400"],
  ["ACS Plex Mono", "IBMPlexMono-500.woff2", "500"],
  ["ACS Plex Mono", "IBMPlexMono-600.woff2", "600"],
];

let fontsReady: Promise<void> | null = null;

/** Registers the bundled woff2 files (served from web/public/fonts under the Vite base). Resolves
 *  when they are usable by canvas text; a missing file only falls back to the system stack. */
export function loadSceneFonts(): Promise<void> {
  if (fontsReady) return fontsReady;
  const base = import.meta.env.BASE_URL.endsWith("/") ? import.meta.env.BASE_URL : `${import.meta.env.BASE_URL}/`;
  fontsReady = Promise.all(
    FACES.map(([family, file, weight]) => {
      const face = new FontFace(family, `url(${base}fonts/${file}) format("woff2")`, { weight, display: "block" });
      document.fonts.add(face);
      return face.load().then(
        () => undefined,
        () => undefined,
      );
    }),
  ).then(() => undefined);
  return fontsReady;
}

export function hexToRgb(hex: string): [number, number, number] {
  const v = parseInt(hex.slice(1), 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}
