// Screenshots of the HUD and the simulated panel page, against the in-browser mock run.
//
//   npx vite --port 5184 --strictPort        (in another terminal)
//   node scripts/hud-shots.mjs [outDir] [baseUrl]
//
// Uses playwright-core with the system Edge (WebGL2 through SwiftShader). Captures, at 1920x1080
// and 1366x768: the proposed signal, the early run, the middle of the run with a party selected,
// the moment of a refusal, the refused close, and the panel page before and after Run test.
// Page errors and console errors are collected into errors.json; the script exits non-zero if any.

import { chromium } from "playwright-core";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const outDir = resolve(process.argv[2] ?? join(tmpdir(), "allclear-shots"));
const base = (process.argv[3] ?? "http://localhost:5184/screen/").replace(/\/?$/, "/");
const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const VIEWPORTS = [
  { name: "1920", width: 1920, height: 1080 },
  { name: "1366", width: 1366, height: 768 },
];
const SETTLE_MS = 3500;

mkdirSync(outDir, { recursive: true });
const errors = [];
const checks = [];

function watch(page, label) {
  page.on("pageerror", (err) => errors.push({ label, kind: "pageerror", text: String(err) }));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push({ label, kind: "console", text: msg.text() });
  });
  page.on("response", (res) => {
    if (res.status() >= 400) errors.push({ label, kind: "http", text: `${res.status()} ${res.url()}` });
  });
}

async function screen(page, query) {
  await page.goto(`${base}?mock=1&key=x&${query}`, { waitUntil: "networkidle" });
  await page.waitForSelector(".headline-text");
  await page.waitForTimeout(SETTLE_MS);
}

async function shot(page, vp, name) {
  const path = join(outDir, `${vp.name}-${name}.png`);
  await page.screenshot({ path, timeout: 180000 });
  return path;
}

/** Nothing in the HUD may overflow the viewport or overlap another panel. */
async function layoutCheck(page, label) {
  const result = await page.evaluate(() => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const doc = document.documentElement;
    const problems = [];
    if (doc.scrollWidth > vw || doc.scrollHeight > vh) problems.push(`page scrolls: ${doc.scrollWidth}x${doc.scrollHeight}`);
    const boxes = [".top", ".col-left", ".col-right", ".bottom", ".ops", ".inspector"]
      .map((sel) => [sel, document.querySelector(sel)?.getBoundingClientRect()])
      .filter(([, r]) => r);
    for (const [sel, r] of boxes) {
      if (r.left < 0 || r.top < 0 || r.right > vw + 0.5 || r.bottom > vh + 0.5) problems.push(`${sel} leaves the viewport`);
    }
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const [a, ra] = boxes[i];
        const [b, rb] = boxes[j];
        const overlap = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left) > 1 &&
          Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top) > 1;
        if (overlap) problems.push(`${a} overlaps ${b}`);
      }
    }
    for (const el of document.querySelectorAll(".panel, .metric, .counter, .op-btn")) {
      if (el.scrollWidth > el.clientWidth + 1) problems.push(`${el.className.split(" ")[0]} overflows horizontally`);
    }
    for (const el of document.querySelectorAll(".col-left, .col-left .scroll")) {
      if (el.scrollHeight > el.clientHeight + 1) problems.push(`${el.className} content is cut off by ${el.scrollHeight - el.clientHeight}px`);
    }
    return { problems, headline: document.querySelector(".headline-text")?.textContent ?? "" };
  });
  checks.push({ label, ...result });
}

const browser = await chromium.launch({
  executablePath: EDGE,
  headless: true,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});

const written = [];
try {
  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    watch(page, vp.name);

    // The signal is proposed: a person must approve it.
    await screen(page, "t=8.02&freeze=1&hold=1");
    await layoutCheck(page, `${vp.name} signal`);
    written.push(await shot(page, vp, "1-signal-proposed"));

    // Early run: day 1, parties being found.
    await screen(page, "t=30&freeze=1");
    await layoutCheck(page, `${vp.name} early`);
    written.push(await shot(page, vp, "2-early-run"));

    // Middle of the run, a party selected from the feed.
    await screen(page, "t=100.6&freeze=1");
    const partyButton = page.locator(".fl-party", { hasText: "Annex Fire" }).first();
    if (await partyButton.count()) await partyButton.click();
    else await page.locator(".fl-party").first().click();
    await page.waitForSelector(".inspector");
    await page.waitForTimeout(1200);
    await layoutCheck(page, `${vp.name} mid+inspector`);
    written.push(await shot(page, vp, "3-mid-run-party-selected"));

    // The live slice: Harbour Fire's lie, refused by Atlas; then the operator lies again.
    await screen(page, "t=229.82&freeze=1");
    written.push(await shot(page, vp, "4-refusal-feed"));
    await page.getByRole("button", { name: /over-report/i }).click();
    await page.waitForSelector(".toast");
    await page.waitForTimeout(600);
    await layoutCheck(page, `${vp.name} refusal`);
    written.push(await shot(page, vp, "5-refusal-toast"));

    // The refused close: 39 panels open.
    await screen(page, "t=231.8&freeze=1");
    await page.getByRole("button", { name: /^Close recall/i }).click();
    await page.waitForSelector(".toast");
    await page.waitForTimeout(600);
    await layoutCheck(page, `${vp.name} close refused`);
    written.push(await shot(page, vp, "6-close-refused"));

    // The panel page, in the same run (another tab): before and after Apply update + Run test.
    await screen(page, "t=231&freeze=1");
    const panel = await context.newPage();
    watch(panel, `${vp.name} panel`);
    await panel.goto(`${base}panel.html?mock=1&serial=D7-4471-0193`, { waitUntil: "networkidle" });
    await panel.waitForSelector(".lcd-row");
    await panel.waitForTimeout(SETTLE_MS);
    written.push(await shot(panel, vp, "7-panel-before"));
    await panel.getByRole("button", { name: "Apply update" }).click();
    await panel.waitForSelector(".pp-outcome");
    await panel.getByRole("button", { name: "Run test" }).click();
    await panel.waitForSelector(".pp-flash");
    await panel.waitForTimeout(400);
    written.push(await shot(panel, vp, "8-panel-after"));
    const effect = await panel.locator(".pp-effect").textContent();
    checks.push({ label: `${vp.name} panel effect`, problems: [], effect });

    // Back on the screen: the PASS shows in the feed and the monitoring strip.
    await page.bringToFront();
    await page.waitForTimeout(1500);
    written.push(await shot(page, vp, "9-screen-after-panel-pass"));
    await panel.close();

    // A live (unfrozen) moment, to see motion and lease bars mid-run.
    await screen(page, "t=60");
    await page.waitForTimeout(2500);
    await layoutCheck(page, `${vp.name} live`);
    written.push(await shot(page, vp, "10-live"));
    await context.close();
  }
} finally {
  await browser.close();
}

writeFileSync(join(outDir, "errors.json"), JSON.stringify({ errors, checks }, null, 2), "utf-8");
console.log(written.join("\n"));
for (const c of checks) {
  console.log(`${c.label}: ${c.problems.length ? c.problems.join("; ") : "ok"}${c.headline ? ` | ${c.headline}` : ""}` +
    `${c.effect ? ` | ${c.effect}` : ""}`);
}
console.log(`${errors.length} page/console errors`);
for (const e of errors.slice(0, 20)) console.log(`  [${e.label}] ${e.kind}: ${e.text}`);
process.exitCode = errors.length || checks.some((c) => c.problems.length) ? 1 : 0;
