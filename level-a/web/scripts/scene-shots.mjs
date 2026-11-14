// Screenshots of the 3D scene dev page (web/scene.html) with the system Edge in headless mode.
//
//   node scripts/scene-shots.mjs [baseUrl] [outDir] [only]
//
// baseUrl defaults to http://localhost:5183/screen/scene.html (npx vite --port 5183 --strictPort),
// outDir defaults to <os tmp>/allclear-scene-shots, `only` filters shots by name substring.
// Each shot waits for the canvas and the scene's data-ready flag, lets the intro settle, checks
// that the panels drawn equal the batch (mass balance), and logs page errors.

import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const base = process.argv[2] || "http://localhost:5183/screen/scene.html";
const outDir = process.argv[3] || join(tmpdir(), "allclear-scene-shots");
const only = process.argv[4] || "";
mkdirSync(outDir, { recursive: true });

const FULL = { width: 1920, height: 1080 };
const LAPTOP = { width: 1366, height: 768 };

/** name, query, viewport, extra settle ms, optional keys pressed after load (with a pause each). */
const SHOTS = [
  ["early-1080", "state=early&auto=1", FULL, 0, []],
  ["mid-1080", "state=mid&auto=1", FULL, 0, []],
  ["mid-hud-1080", "state=mid&auto=1&hud=1", FULL, 0, []],
  ["late-1080", "state=late&auto=1", FULL, 0, []],
  ["closed-1080", "state=closed&auto=1", FULL, 0, []],
  ["mid-768", "state=mid&auto=1", LAPTOP, 0, []],
  ["late-hud-768", "state=late&auto=1&hud=1", LAPTOP, 0, []],
  ["select-corner-1080", "state=late&auto=1&select=corner-properties", FULL, 2500, []],
  ["select-north-1080", "state=mid&auto=1&select=dist-north", FULL, 2500, []],
  ["mid-kill-1080", "state=mid&auto=1", FULL, 0, [["k", 900]]],
  ["mid-adopted-1080", "state=mid&auto=1", FULL, 0, [["k", 6500]]],
  ["early-signal-proposed-1080", "state=early&auto=1", FULL, 0, [["s", 2200]]],
  ["late-close-refused-1080", "state=late&auto=1", FULL, 0, [["x", 500]]],
  ["mid-pass-1080", "state=mid&auto=1&select=bayview-annex", FULL, 2500, [["p", 900]]],
  ["panel-idle", "panel=1&auto=1&sw=2.4", FULL, 0, []],
  ["panel-testing-fail", "panel=1&auto=1&sw=2.4&phase=testing&flash=fail", FULL, 0, []],
  ["panel-testing", "panel=1&auto=1&sw=2.5&phase=testing", FULL, 0, []],
  ["panel-updating", "panel=1&auto=1&sw=2.4", FULL, 0, [["u", 1400]]],
  ["panel-pass", "panel=1&auto=1&sw=2.5&flash=pass&fixed=1", LAPTOP, 0, []],
];

const browser = await chromium.launch({
  executablePath: EDGE,
  headless: true,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});

let failures = 0;
for (const [name, query, viewport, settle, keys] of SHOTS) {
  if (only && !name.includes(only)) continue;
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") errors.push(`${m.type()}: ${m.text()}`);
  });
  const url = `${base}?${query}`;
  const t0 = Date.now();
  await page.goto(url, { waitUntil: "load" });
  await page.waitForSelector("canvas", { timeout: 60000 });
  await page.waitForSelector('[data-ready="1"]', { timeout: 60000 });
  // Let the intro rise, the fonts and the first label layout settle (time-based animations).
  await page.waitForTimeout(5200 + settle);
  for (const [key, pause] of keys) {
    await page.keyboard.press(key);
    await page.waitForTimeout(pause);
  }
  const counts = await page.evaluate(() => {
    const el = document.querySelector(".acs-root");
    return el ? { drawn: el.getAttribute("data-panels-drawn"), expected: el.getAttribute("data-panels-expected"), total: el.getAttribute("data-units-total") } : null;
  });
  const file = join(outDir, `${name}.png`);
  await page.screenshot({ path: file });
  const balance = counts ? `panels drawn ${counts.drawn} / expected ${counts.expected} / units ${counts.total}` : "panel page";
  const ok = !counts || (counts.drawn === counts.expected && counts.expected === counts.total);
  if (!ok) failures++;
  console.log(`${ok ? "ok  " : "FAIL"} ${name.padEnd(30)} ${((Date.now() - t0) / 1000).toFixed(1)}s  ${balance}  -> ${file}`);
  for (const e of errors.slice(0, 8)) console.log(`     ${e}`);
  await page.close();
}
await browser.close();
process.exit(failures ? 1 : 0);
