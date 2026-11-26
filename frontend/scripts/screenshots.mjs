// Captures the README screenshots from a running stack (`make dev`), light theme,
// at 2x. Usage: node scripts/screenshots.mjs <shot>... (no args = every static shot)
//
//   node scripts/screenshots.mjs overview deployments benchmarks
//   DEPLOYMENT=<id> node scripts/screenshots.mjs evidence
import { chromium } from "@playwright/test";
import { mkdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const OUT = resolve(import.meta.dirname, "../../docs/screenshots");
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();

async function open(path, { width = 1440, height = 900, video, freeze, base = BASE } = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 2,
    colorScheme: "light",
    ...(video ? { recordVideo: { dir: video, size: { width, height } } } : {}),
  });
  await context.addInitScript(() => localStorage.setItem("theme", "light"));
  const page = await context.newPage();
  if (freeze) {
    // Serve every API read from its first answer once frozen, so numbers hold
    // still across the frames of an animation. The live stream passes through.
    const cache = new Map();
    await page.route(/:8000\/api\/(?!stream)/, async (route) => {
      const key = route.request().url();
      if (freeze.on && cache.has(key)) return route.fulfill(cache.get(key));
      const response = await route.fetch();
      const cached = { status: response.status(), headers: response.headers(), body: await response.body() };
      cache.set(key, cached);
      return route.fulfill(cached);
    });
  }
  await page.goto(base + path, { waitUntil: base === BASE ? "networkidle" : "load" });
  // The Next.js dev-mode indicator isn't part of the product.
  await page.addStyleTag({ content: "nextjs-portal { display: none !important; } header { position: static !important; }" });
  // Charts animate in; give them, and the live stream's first refetch, a moment.
  await page.waitForTimeout(2500);
  return { context, page };
}

async function viewport(name, path, opts) {
  const { context, page } = await open(path, opts);
  await page.screenshot({ path: `${OUT}/${name}.png` });
  await context.close();
  console.log(`wrote ${name}.png`);
}

/** The innermost card (or section) holding a heading with this exact text. */
async function card(page, title) {
  const heading = page.getByRole("heading", { name: title, exact: true }).or(page.locator('[data-slot="card-title"]', { hasText: title })).first();
  return heading.locator("xpath=ancestor::*[self::section or @data-slot='card'][1]");
}

async function element(name, locator, pad = 16) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  const page = locator.page();
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    fullPage: true,
    clip: { x: box.x - pad, y: box.y - pad + (await page.evaluate(() => scrollY)), width: box.width + pad * 2, height: box.height + pad * 2 },
  });
  console.log(`wrote ${name}.png`);
}

const dep = (id) => `/deployments/${id}`;

const shots = {
  overview: () => viewport("overview", "/"),
  deployments: () => viewport("deployments", "/deployments"),
  benchmarks: () => viewport("benchmarks", "/benchmarks", { height: 800 }),
  models: () => viewport("models", "/models"),
  // Any other page (Grafana, Jaeger): URL=... NAME=... [WAIT=ms] [HEIGHT=px]
  async url() {
    const u = new URL(process.env.URL);
    const { context, page } = await open(u.pathname + u.search, { base: u.origin, height: Number(process.env.HEIGHT ?? 900) });
    await page.waitForTimeout(Number(process.env.WAIT ?? 4000));
    await page.screenshot({ path: `${OUT}/${process.env.NAME}.png` });
    await context.close();
    console.log(`wrote ${process.env.NAME}.png`);
  },
  gateway: () => viewport("gateway", "/gateway", { height: Number(process.env.HEIGHT ?? 1000) }),
  live: () => viewport("deployment-live", dep(process.env.DEPLOYMENT), { height: 1000 }),

  // One rolled-back rollout: what changed over time, and the evidence for the verdict.
  async evidence() {
    const { context, page } = await open(dep(process.env.DEPLOYMENT), { height: 1100 });
    await element("rollout-over-time", await card(page, "Over time"));
    await page.getByRole("tab", { name: /policy checks/i }).click();
    await page.waitForTimeout(800);
    await element("policy-evidence", page.locator('[role="tabpanel"]:visible').first(), 8);
    await page.getByRole("tab", { name: /timeline/i }).click();
    await page.waitForTimeout(800);
    await element("timeline", page.locator('[role="tabpanel"]:visible').first(), 8);
    await context.close();
  },

  // One live rollout's card on the Gateway page as a seamless looping animation:
  // CSS animations are paused and stepped frame by frame, each one's period
  // nudged so that it divides the loop exactly. Turn the frames into a GIF with
  //   ffmpeg -framerate 20 -i docs/.frames/f%03d.png -vf "scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse" out.gif
  async "gateway-gif"() {
    const model = process.env.MODEL ?? "fraud-model";
    const freeze = { on: false };
    const { context, page } = await open("/gateway", { height: 1200, freeze });
    await page.waitForTimeout(2000);
    freeze.on = true;
    await page.waitForTimeout(1600);
    const cardEl = page.locator('[data-slot="card"]').filter({ has: page.getByTestId(`gateway-flow-${model}`) });
    await cardEl.scrollIntoViewIfNeeded();
    const frames = resolve(OUT, "../.frames");
    rmSync(frames, { recursive: true, force: true });
    mkdirSync(frames, { recursive: true });
    const LOOP = 3000, FPS = 20;
    for (let i = 0; i < (LOOP / 1000) * FPS; i++) {
      await page.evaluate(([t, loop]) => {
        for (const a of document.getAnimations()) {
          const d = Number(a.effect.getComputedTiming().duration) || loop;
          const k = Math.max(1, Math.round(loop / d));
          a.pause();
          a.currentTime = ((t / loop) * k * d) % d;
        }
      }, [(i * 1000) / FPS, LOOP]);
      await cardEl.screenshot({ path: `${frames}/f${String(i).padStart(3, "0")}.png` });
    }
    await context.close();
    console.log(`FRAMES ${frames}`);
  },
};

const wanted = process.argv.slice(2);
for (const name of wanted.length ? wanted : ["overview", "deployments", "benchmarks", "models"]) {
  if (!shots[name]) throw new Error(`unknown shot ${name}; one of ${Object.keys(shots).join(", ")}`);
  await shots[name]();
}
await browser.close();
