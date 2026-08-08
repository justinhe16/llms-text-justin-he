/**
 * Rendered-output smoke test.
 *
 * WHY THIS EXISTS
 *
 * `tsc --noEmit`, eslint and `next build` all pass on a page that renders wrong. A font
 * wiring bug that made every page silently fall back to Times cleared all three gates and
 * was caught only by measuring computed styles in a real browser. Every gate above this
 * one reads source code; this one reads what the browser resolved.
 *
 * It is deliberately a handful of assertions against one page and not a browser test
 * suite — Playwright and end-to-end coverage are out of scope. It runs in a few seconds
 * and needs no network beyond localhost.
 *
 * WHAT IT ASSERTS, AND WHICH BUG EACH ONE CATCHES
 *
 *   1. `/` answers 200, `<html lang>` is set, the document has a title.
 *   2. `--font-geist-sans` and `--font-geist-mono` are readable **on `<html>`** — the
 *      element app/globals.css resolves `font-sans` against. Moving the font classes down
 *      to `<body>` leaves that rule pointing at an undefined variable and is invisible to
 *      every static gate.
 *   3. Both faces actually load (`document.fonts.load`), so a font file that 404s fails
 *      here rather than degrading silently in production.
 *   4. The family the browser resolved for `<html>` is the sans face itself, not a
 *      generic fallback — this is the Times bug, stated directly.
 *   5. Sans and mono resolve to *different* families, which a copy-pasted variable name
 *      would break.
 *   6. `<body>` paints the gradient from app/globals.css.
 *   7. The same page under `prefers-color-scheme: dark` computes identical colours. Light
 *      theme only is a repository rule (CLAUDE.md rule 7); this is the rendered proof.
 *   8. A heading is genuinely visible — non-zero box, and `checkVisibility` including
 *      ancestor opacity, so a hydration failure that leaves the entry animation stuck at
 *      opacity 0 fails instead of shipping a blank page.
 *   9. No uncaught exception, no console error, no same-origin response >= 400.
 *
 * Then `/docs`, whose diagram is wired to its prose by string ids that no compiler checks:
 *
 *  10. Every id in `lib/docs/sections.ts` resolves to an `h2` on the rendered page. This is
 *      the gate the diagram cannot live without. `rehype-slug` derives each heading's id
 *      from its *text*, so renaming `## Fetch` silently turns one diagram node into a
 *      button that scrolls nowhere — `tsc`, eslint and `next build` all pass on it. The
 *      check reads the ids off the rendered nodes rather than parsing the TypeScript, so it
 *      tests the actual relationship between the two columns.
 *  11. Seven nodes and six beams, and every beam's path endpoints land on the centres of the
 *      two nodes it connects. A beam measured before webfonts settle lands wrong, which is
 *      invisible to every gate above this one and to a hot reload.
 *  12. Clicking each of the seven nodes scrolls its heading to the top of the viewport. All
 *      seven, because six working nodes and one dead one is the failure worth catching.
 *  13. Under `prefers-reduced-motion: reduce` the beams drop their animated gradient. (That
 *      is the media feature this repository allows; `prefers-color-scheme` is the banned
 *      one, and assertion 7 above covers it.)
 *
 * Then the tab nav PER-192 added, and `/docs/architecture`, whose topology is wired to its
 * prose the same string-id way the pipeline is:
 *
 *  14. `/docs`'s tab nav renders three links — Features, How a run works, Architecture, in
 *      that display order — `aria-current="page"` on the active one and no value on the
 *      other two, with no `role="tablist"` anywhere on the page. A `startsWith` regression in
 *      `DocsTabs` would light more than one tab the moment a route nested under `/docs`
 *      existed, and this is the assertion that catches it from the `/docs` side.
 *  15. THE DEEP-LINK GATE: `/docs#fetch` — a published, external link — still lands on the
 *      Fetch heading after the route-group restructure that moved the run tab under
 *      `app/docs/(run)/`. `next build` has no opinion about a URL fragment; only a real
 *      browser navigation proves the anchor still resolves. Also proves Features being drawn
 *      *first* in the tab nav never made it the index route — `/docs` is still the run tab.
 *  16. `GET /docs/architecture` returns 200, and its diagram renders seven nodes and eight
 *      beams — a different count from the pipeline's seven nodes and six beams, because a
 *      topology is a graph and not every node has exactly one neighbour below it. Two of the
 *      eight run between the same pair of nodes (`redis` and `worker`, one each direction) —
 *      the queue loop the reviewer asked to see drawn.
 *  17. Every node id in `lib/docs/architecture.ts` resolves to an `h2` on the rendered page —
 *      the same anchor gate as assertion 10, for the second file that can drift the same way.
 *  18. Every topology beam lands on the centres of the nodes ITS EDGE names, offset by that
 *      edge's `laneOffsetPx` where one is given, paired by the `data-arch-edges` list rather
 *      than by array adjacency. The pipeline's beam *i* always connects node *i* and node
 *      *i+1*; the topology's does not (`api` feeds both `redis` and `supabase`), so this
 *      pairing has to come from the edge list, not from position.
 *  19. The worker→anthropic edge is the only dashed beam. A regression that lost the dash
 *      would silently reclassify a call this deployment never makes as one that happens; a
 *      regression that dashed every beam would make the distinction meaningless.
 *  20. Clicking each of the seven topology nodes scrolls to its section (several nodes share
 *      one — that is expected), the diagram is static under `prefers-reduced-motion:
 *      reduce`, and it renders with no horizontal overflow at 375px, beams still aligned.
 *  21. The tab nav mirrors assertion 14 on this route: three links, `aria-current="page"` on
 *      Architecture this time, no value on the other two, still no `role="tablist"`.
 *
 * Then `/docs/features`, PER-192's third tab — accepted scope creep the author approved,
 * overriding the ticket's own "out of scope: a third tab" — a contents list rather than a
 * diagram, since there is no graph to draw for "what things are":
 *
 *  22. `GET /docs/features` returns 200 and its headings render.
 *  23. Every link in the contents list (`lib/docs/features.ts`'s ids, rendered by
 *      `FeaturesContents`) resolves to a real `h2` on the page — the same anchor gate as
 *      assertions 10 and 17, for the third file that can drift the same way, gated by
 *      `data-features-link` rather than `data-docs-node`/`data-arch-node`.
 *  24. The tab nav renders here too: three links, `aria-current="page"` on Features — the
 *      leftmost position — no value on the other two, still no `role="tablist"`.
 *
 * USAGE
 *
 *     npm run build && npm run smoke        # starts `next start` itself
 *     SMOKE_BASE_URL=http://localhost:3000 npm run smoke   # against a running server
 *
 * Chrome: uses a browser already installed on the machine — puppeteer-core downloads
 * none. Set CHROME_PATH to point at a specific binary.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import puppeteer from "puppeteer-core";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = Number(process.env.SMOKE_PORT ?? 3100);
const EXTERNAL_BASE_URL = process.env.SMOKE_BASE_URL;
const BASE_URL = EXTERNAL_BASE_URL ?? `http://127.0.0.1:${PORT}`;
const SERVER_START_TIMEOUT_MS = 90_000;

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  process.env.PUPPETEER_EXECUTABLE_PATH,
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/opt/google/chrome/chrome",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
];

// Chrome requests /favicon.ico on every navigation and the app ships none, so it 404s on
// every run and logs a console error. A smoke test is not the place to legislate a
// favicon, and one guaranteed false positive would train everyone to ignore this step.
// Nothing else belongs on this list without a reason as boring as this one.
const IGNORED_URL_SUFFIXES = ["/favicon.ico"];

const isIgnoredUrl = (url) =>
  typeof url === "string" && IGNORED_URL_SUFFIXES.some((suffix) => url.endsWith(suffix));

const failures = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function check(ok, label, detail) {
  if (ok) {
    console.log(`ok    ${label}`);
    return;
  }
  failures.push(label);
  console.log(`FAIL  ${label}${detail === undefined ? "" : ` — ${detail}`}`);
}

function findChrome() {
  const found = CHROME_CANDIDATES.find((candidate) => candidate && existsSync(candidate));
  if (!found) {
    throw new Error(
      "no Chrome binary found. Install Google Chrome, or set CHROME_PATH to its " +
        `executable. Looked at: ${CHROME_CANDIDATES.filter(Boolean).join(", ")}`,
    );
  }
  return found;
}

/** Start `next start`, or return null when SMOKE_BASE_URL points at a running server. */
function startServer() {
  if (EXTERNAL_BASE_URL) {
    console.log(`using the server already running at ${EXTERNAL_BASE_URL}`);
    return null;
  }
  console.log(`starting \`next start\` on port ${PORT}`);
  // detached so the whole process group can be signalled: `npm run` spawns next as a
  // child, and killing only npm would leave the server holding the port.
  const child = spawn(
    "npm",
    ["run", "start", "--", "--hostname", "127.0.0.1", "--port", String(PORT)],
    { cwd: frontendDir, detached: true, stdio: ["ignore", "pipe", "pipe"] },
  );
  const tail = [];
  const record = (chunk) => {
    tail.push(String(chunk));
    if (tail.length > 40) tail.shift();
  };
  child.stdout.on("data", record);
  child.stderr.on("data", record);
  child.on("exit", (code) => {
    if (code !== 0 && code !== null) {
      console.error(`the server exited with code ${code}:\n${tail.join("")}`);
    }
  });
  return { child, tail };
}

function stopServer(server) {
  if (!server?.child?.pid) return;
  try {
    process.kill(-server.child.pid, "SIGTERM");
  } catch {
    // Already gone.
  }
}

async function waitForServer(server) {
  const deadline = Date.now() + SERVER_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (server?.child?.exitCode !== null && server?.child?.exitCode !== undefined) {
      throw new Error(`the server exited before serving a request:\n${server.tail.join("")}`);
    }
    try {
      const response = await fetch(BASE_URL, { redirect: "manual" });
      if (response.status < 500) return;
    } catch {
      // Not listening yet.
    }
    await sleep(250);
  }
  throw new Error(
    `${BASE_URL} did not answer within ${SERVER_START_TIMEOUT_MS / 1000}s` +
      (server ? `:\n${server.tail.join("")}` : ""),
  );
}

/** Everything measured inside the page, in one round trip. */
async function probePage() {
  const unquote = (value) => String(value).trim().replace(/^["']|["']$/g, "");
  const firstFamily = (value) => unquote(String(value).split(",")[0] ?? "");

  const htmlStyle = getComputedStyle(document.documentElement);
  const sansVar = htmlStyle.getPropertyValue("--font-geist-sans").trim();
  const monoVar = htmlStyle.getPropertyValue("--font-geist-mono").trim();

  // Force both faces to load rather than waiting for the page to happen to use them:
  // whether any element on today's placeholder page renders in mono is not the point.
  const loadFamily = async (value) => {
    const family = firstFamily(value);
    if (!family) return { family: "", requested: false, loaded: false };
    try {
      const faces = await document.fonts.load(`16px "${family}"`);
      return {
        family,
        requested: true,
        loaded: faces.length > 0 && faces.every((face) => face.status === "loaded"),
        statuses: faces.map((face) => face.status),
      };
    } catch (error) {
      return { family, requested: true, loaded: false, error: String(error) };
    }
  };

  const sans = await loadFamily(sansVar);
  const mono = await loadFamily(monoVar);
  await document.fonts.ready;

  const loadedFamilies = [];
  document.fonts.forEach((face) => {
    if (face.status === "loaded") loadedFamilies.push(unquote(face.family));
  });

  const bodyStyle = getComputedStyle(document.body);
  const heading = document.querySelector("h1, h2");
  const headingBox = heading?.getBoundingClientRect();

  return {
    lang: document.documentElement.lang,
    title: document.title,
    sansVar,
    monoVar,
    sans,
    mono,
    loadedFamilies,
    htmlFontFamily: htmlStyle.fontFamily,
    htmlFirstFamily: firstFamily(htmlStyle.fontFamily),
    bodyBackgroundImage: bodyStyle.backgroundImage,
    bodyBackgroundColor: bodyStyle.backgroundColor,
    bodyColor: bodyStyle.color,
    heading: heading
      ? {
          tag: heading.tagName.toLowerCase(),
          text: (heading.textContent ?? "").trim(),
          width: headingBox.width,
          height: headingBox.height,
          // Includes ancestor opacity and visibility, which a plain getComputedStyle on
          // the heading itself would miss: the entry animation puts opacity on a wrapper.
          visible:
            typeof heading.checkVisibility === "function"
              ? heading.checkVisibility({
                  opacityProperty: true,
                  visibilityProperty: true,
                  contentVisibilityAuto: true,
                })
              : true,
        }
      : null,
    textLength: (document.body.innerText ?? "").trim().length,
  };
}

/**
 * Everything measured about one diagram: its nodes, its beams, and whether each node's
 * section id resolves to a heading in the prose column.
 *
 * Generalized over the two diagrams PER-192 leaves this repository with, rather than one
 * probe per diagram, because the two differ only in their attribute names:
 * `containerAttr`/`nodeAttr` select the container and its nodes; `sectionAttr` reads the
 * heading id a node scrolls to, falling back to the node's own id when absent — the pipeline
 * diagram has no separate `data-docs-section`, because there a node's id IS the section id;
 * `edgesAttr`, when given, reads the `data-arch-edges` encoding straight off the container,
 * for `checkArchitecture`'s edge-driven beam pairing below.
 *
 * The beams are selected as *direct* `<svg>` children of the diagram container — every icon
 * inside a node button is an `<svg>` too, but nested, never a direct child.
 */
function probeDiagram({ containerAttr, nodeAttr, sectionAttr, edgesAttr }) {
  const container = document.querySelector(`[${containerAttr}]`);
  if (!container) return { container: false };

  const containerRect = container.getBoundingClientRect();
  const nodes = [...container.querySelectorAll(`[${nodeAttr}]`)].map((button) => {
    const rect = button.getBoundingClientRect();
    const id = button.getAttribute(nodeAttr);
    const sectionId = sectionAttr ? button.getAttribute(sectionAttr) : id;
    const heading = document.getElementById(sectionId);
    return {
      id,
      sectionId,
      // The accessible name is the button's own text (visible label + an sr-only
      // continuation), never an aria-label — see `description` in lib/docs/sections.ts.
      name: (button.textContent ?? "").trim(),
      centerX: rect.left - containerRect.left + rect.width / 2,
      centerY: rect.top - containerRect.top + rect.height / 2,
      headingTag: heading?.tagName ?? null,
    };
  });

  const beams = [...container.querySelectorAll(":scope > svg")].map((svg) => ({
    d: svg.querySelector("path")?.getAttribute("d") ?? "",
    // The animated beam draws a second path plus a <linearGradient>; the static one does not.
    gradients: svg.querySelectorAll("linearGradient").length,
    // "none" for an ordinary beam, "4px, 4px" (or similar) for the one PER-192 draws dashed.
    dash: getComputedStyle(svg.querySelector("path") ?? svg).strokeDasharray,
  }));

  return {
    container: true,
    nodes,
    beams,
    edges: edgesAttr ? container.getAttribute(edgesAttr) : null,
  };
}

/** `M x,y Q cx,cy ex,ey` → its endpoints, or null when the path never got measured. */
function beamEndpoints(d) {
  const match = d.match(/M ([\d.-]+),([\d.-]+) Q [\d.-]+,[\d.-]+ ([\d.-]+),([\d.-]+)/);
  if (!match) return null;
  return { startX: +match[1], startY: +match[2], endX: +match[3], endY: +match[4] };
}

/** Assertions 10-13. */
async function checkDocs(page) {
  const response = await page.goto(`${BASE_URL}/docs`, { waitUntil: "load" });
  check(response?.status() === 200, "GET /docs returns 200", `got ${response?.status()}`);

  // The gotcha this whole section exists for: a beam measured before the font swap lands a
  // few pixels off, and the swap has not happened yet on a cold load.
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(
    () => {
      const container = document.querySelector("[data-docs-diagram]");
      const svg = container?.querySelector(":scope > svg path");
      return Boolean(svg?.getAttribute("d"));
    },
    { timeout: 10_000 },
  ).catch(() => {
    // Reported by the assertions below, with the measurements attached.
  });

  const docs = await page.evaluate(probeDiagram, {
    containerAttr: "data-docs-diagram",
    nodeAttr: "data-docs-node",
  });
  if (!docs.container) {
    check(false, "the /docs diagram renders", "no [data-docs-diagram] in the document");
    return;
  }

  check(docs.nodes.length === 7, "/docs renders seven diagram nodes", `got ${docs.nodes.length}`);
  check(docs.beams.length === 6, "/docs renders six beams", `got ${docs.beams.length}`);

  // THE ANCHOR GATE. A renamed heading fails here and nowhere else.
  const dead = docs.nodes.filter((node) => node.headingTag !== "H2");
  check(
    dead.length === 0 && docs.nodes.length > 0,
    "every diagram node id resolves to an h2 heading",
    dead.length > 0
      ? `no <h2 id> for: ${dead.map((node) => node.id).join(", ")} — a heading in ` +
          "app/docs/page.mdx was renamed, or lib/docs/sections.ts names an id that " +
          "rehype-slug never generated"
      : "the diagram rendered no nodes at all",
  );

  check(
    // `.length > 0` is not redundant: `.every()` on an empty array is vacuously true, so a
    // diagram that rendered no nodes at all would pass this assertion on its way past.
    docs.nodes.length > 0 && docs.nodes.every((node) => (node.name ?? "").length > 0),
    "every diagram node has an accessible name",
    docs.nodes
      .filter((node) => !(node.name ?? "").length)
      .map((node) => node.id)
      .join(", "),
  );

  const misaligned = docs.beams
    .map((beam, index) => {
      const points = beamEndpoints(beam.d);
      const from = docs.nodes[index];
      const to = docs.nodes[index + 1];
      if (!points || !from || !to) return `beam ${index}: unmeasured (d="${beam.d}")`;
      const drift = Math.max(
        Math.abs(points.startX - from.centerX),
        Math.abs(points.startY - from.centerY),
        Math.abs(points.endX - to.centerX),
        Math.abs(points.endY - to.centerY),
      );
      // One pixel of slack for sub-pixel layout; a beam measured before the font swap is
      // out by far more than that.
      return drift <= 1 ? null : `${from.id}→${to.id} out by ${drift.toFixed(1)}px`;
    })
    .filter(Boolean);
  check(
    misaligned.length === 0,
    "every beam lands on the centres of the nodes it connects",
    misaligned.join(", "),
  );

  // Click → scroll, all seven. Reduced motion is emulated so the scroll is instant and the
  // step needs no sleeping; it also proves the beams go static, which is assertion 13.
  // Both features in one call, deliberately. `Emulation.setEmulatedMedia` *replaces* the
  // override list rather than merging into it, so naming only reduced-motion here would
  // silently drop the `prefers-color-scheme: light` override set earlier and hand the rest
  // of the run whatever the CI runner's real OS preference is.
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
  await page.reload({ waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);

  const notScrolled = [];
  for (const node of docs.nodes) {
    const landed = await page.evaluate(async (id) => {
      window.scrollTo(0, 0);
      const button = document.querySelector(`[data-docs-node="${id}"]`);
      if (!button) return { clicked: false };
      button.click();
      // `behavior: "auto"` under reduced motion, so one frame is enough.
      await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
      const heading = document.getElementById(id);
      return { clicked: true, top: heading?.getBoundingClientRect().top ?? null };
    }, node.id);
    // Headings carry scroll-mt-24 (96px), so a scrolled-to heading sits just below it.
    if (!landed.clicked || landed.top === null || landed.top < 0 || landed.top > 160) {
      notScrolled.push(`${node.id} (heading top ${landed.top})`);
    }
  }
  check(
    notScrolled.length === 0,
    "clicking each of the seven nodes scrolls to its section",
    notScrolled.join(", "),
  );

  // Scroll → highlight. The click loop above proves the anchors resolve; this proves the
  // observer in lib/docs/use-active-section.ts actually engages. Without it, a hook that
  // silently never sets an active section (its headings missing at effect time, say) would
  // leave every assertion here green and the diagram permanently unlit.
  const unlit = [];
  for (const node of docs.nodes) {
    const active = await page.evaluate(async (id) => {
      document.getElementById(id)?.scrollIntoView({ behavior: "auto", block: "start" });
      await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
      await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
      return document.querySelector("[data-docs-node][data-active]")?.dataset.docsNode ?? null;
    }, node.id);
    if (active !== node.id) unlit.push(`${node.id} lit ${active}`);
  }
  check(
    unlit.length === 0,
    "scrolling to a section highlights its node",
    unlit.join(", "),
  );

  const reduced = await page.evaluate(probeDiagram, {
    containerAttr: "data-docs-diagram",
    nodeAttr: "data-docs-node",
  });
  check(
    reduced.beams.length === 6 && reduced.beams.every((beam) => beam.gradients === 0),
    "beams are static under prefers-reduced-motion: reduce",
    JSON.stringify(reduced.beams.map((beam) => beam.gradients)),
  );
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "no-preference" },
  ]);

  // 14. The tab nav is three links — Features, How a run works, Architecture, in that
  //     display order — aria-current on the active one (How a run works, index 1, since
  //     `/docs` itself is not Features — see docs-tabs.tsx), and no tablist anywhere.
  const tabs = await page.evaluate(() =>
    [...document.querySelectorAll("[data-docs-tab]")].map((a) => ({
      tag: a.tagName,
      href: a.getAttribute("data-docs-tab"),
      current: a.getAttribute("aria-current"),
    })),
  );
  const hasTablist = await page.evaluate(
    () => document.querySelector('[role="tablist"]') !== null,
  );
  check(
    tabs.length === 3 &&
      tabs[0]?.current === null &&
      tabs[1]?.current === "page" &&
      tabs[2]?.current === null &&
      tabs.every((tab) => tab.tag === "A") &&
      !hasTablist,
    "/docs renders three tab links with aria-current on the active one and no role=tablist",
    JSON.stringify(tabs),
  );

  // 15. THE DEEP-LINK GATE. /docs#fetch is already a published link; the route group must
  //     not break it. `next build` has no opinion about a hash.
  await page.goto(`${BASE_URL}/docs#fetch`, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  const anchor = await page.evaluate(() => {
    const heading = document.getElementById("fetch");
    return heading ? { scrollY: window.scrollY, top: heading.getBoundingClientRect().top } : null;
  });
  // The band is deliberately loose: headings carry scroll-mt-24 (96px) and the font swap
  // moves things, so the point is "the anchor resolved and the document scrolled", not a
  // pixel.
  check(
    anchor !== null && anchor.scrollY > 0 && anchor.top > -50 && anchor.top < 200,
    "/docs#fetch lands on the Fetch heading",
    JSON.stringify(anchor),
  );
}

/** The `data-arch-edges` container attribute, parsed back into `{ from, to, offsetX }`
 *  triples. `offsetX` is the edge's `laneOffsetPx` (0 when the encoding omits it) — the
 *  worker→redis edge draws a few pixels off the direct line between the two node centres so
 *  it doesn't sit on top of the redis→worker beam running the opposite way, and this is what
 *  lets `misalignedArchBeams` tell "drawn where the data says to" from "drawn wrong". */
function parseArchEdges(edgesAttr) {
  return (edgesAttr ?? "")
    .split(",")
    .filter(Boolean)
    .map((pair) => {
      const [path, offset] = pair.split(":");
      const [from, to] = path.split(">");
      return { from, to, offsetX: offset ? Number(offset) : 0 };
    });
}

/** Every beam's endpoints checked against the centres its edge names, not against array
 *  adjacency — the topology is a graph, so beam *i* does not necessarily connect node *i*
 *  and node *i+1* the way the pipeline's beams do. An edge's `offsetX` (see `parseArchEdges`)
 *  shifts the expected x-coordinate at both ends by the same amount, matching the
 *  `startXOffset`/`endXOffset` `ArchitectureDiagram` feeds `AnimatedBeam` for that one edge. */
function misalignedArchBeams(diagram) {
  const byId = new Map(diagram.nodes.map((node) => [node.id, node]));
  const edges = parseArchEdges(diagram.edges);
  return diagram.beams
    .map((beam, index) => {
      const edge = edges[index];
      const points = beamEndpoints(beam.d);
      const from = edge && byId.get(edge.from);
      const to = edge && byId.get(edge.to);
      if (!points || !from || !to) return `beam ${index}: unmeasured (d="${beam.d}")`;
      const offsetX = edge.offsetX ?? 0;
      const drift = Math.max(
        Math.abs(points.startX - (from.centerX + offsetX)),
        Math.abs(points.startY - from.centerY),
        Math.abs(points.endX - (to.centerX + offsetX)),
        Math.abs(points.endY - to.centerY),
      );
      // One pixel of slack for sub-pixel layout, same tolerance checkDocs uses.
      return drift <= 1 ? null : `${edge.from}→${edge.to} out by ${drift.toFixed(1)}px`;
    })
    .filter(Boolean);
}

/** Assertions 16-20: `/docs/architecture`'s topology diagram, its anchors, its beams, and the
 *  tab nav's state on this route. Run from `main()` immediately after `checkDocs`, on the
 *  same `page`, so the console-error and bad-response listeners set up there cover this route
 *  too. */
async function checkArchitecture(page) {
  const ARCH_NODE_COUNT = 7;
  const ARCH_BEAM_COUNT = 8;

  const response = await page.goto(`${BASE_URL}/docs/architecture`, { waitUntil: "load" });
  check(
    response?.status() === 200,
    "GET /docs/architecture returns 200",
    `got ${response?.status()}`,
  );

  await page.evaluate(() => document.fonts.ready);
  await page
    .waitForFunction(
      () => {
        const container = document.querySelector("[data-arch-diagram]");
        const svg = container?.querySelector(":scope > svg path");
        return Boolean(svg?.getAttribute("d"));
      },
      { timeout: 10_000 },
    )
    .catch(() => {
      // Reported by the assertions below, with the measurements attached.
    });

  const arch = await page.evaluate(probeDiagram, {
    containerAttr: "data-arch-diagram",
    nodeAttr: "data-arch-node",
    sectionAttr: "data-arch-section",
    edgesAttr: "data-arch-edges",
  });
  if (!arch.container) {
    check(false, "the /docs/architecture diagram renders", "no [data-arch-diagram] in the document");
    return;
  }

  check(
    arch.nodes.length === ARCH_NODE_COUNT,
    "/docs/architecture renders seven topology nodes",
    `got ${arch.nodes.length}`,
  );
  check(
    arch.beams.length === ARCH_BEAM_COUNT,
    "/docs/architecture renders eight beams",
    `got ${arch.beams.length}`,
  );

  // THE ANCHOR GATE — the same argument as assertion 10, for the second file that can drift
  // the same way: lib/docs/architecture.ts names a sectionId, rehype-slug derives the actual
  // heading id from app/docs/architecture/page.mdx's text, and nothing type-checks the join.
  const dead = arch.nodes.filter((node) => node.headingTag !== "H2");
  check(
    dead.length === 0 && arch.nodes.length > 0,
    "every topology node's section id resolves to an h2 heading",
    dead.length > 0
      ? `no <h2 id> for: ${dead.map((node) => `${node.id}->${node.sectionId}`).join(", ")} — a ` +
          "heading in app/docs/architecture/page.mdx was renamed, or lib/docs/architecture.ts " +
          "names a sectionId rehype-slug never generated"
      : "the diagram rendered no nodes at all",
  );

  check(
    // `.length > 0` is not redundant — see the identical guard in checkDocs.
    arch.nodes.length > 0 && arch.nodes.every((node) => (node.name ?? "").length > 0),
    "every topology node has an accessible name",
    arch.nodes
      .filter((node) => !(node.name ?? "").length)
      .map((node) => node.id)
      .join(", "),
  );

  const edges = parseArchEdges(arch.edges);
  check(
    edges.length === ARCH_BEAM_COUNT,
    "the diagram publishes one edge per beam",
    `${edges.length} edges vs ${arch.beams.length} beams`,
  );

  const misaligned = misalignedArchBeams(arch);
  check(
    misaligned.length === 0,
    "every topology beam lands on the centres of the nodes its edge names",
    misaligned.join(", "),
  );

  // The dashed edge: worker→anthropic, and only it. Catches both a lost dash and a dash that
  // leaked onto every beam.
  const dashedIndexes = edges
    .map((edge, index) => (edge.to === "anthropic" ? index : -1))
    .filter((index) => index >= 0);
  check(
    dashedIndexes.length === 1 &&
      arch.beams[dashedIndexes[0]]?.dash !== "none" &&
      arch.beams.filter((_, index) => index !== dashedIndexes[0]).every((beam) => beam.dash === "none"),
    "the Anthropic edge is the only dashed beam",
    JSON.stringify(arch.beams.map((beam) => beam.dash)),
  );

  // Tab nav on this route — mirror of checkDocs's assertion 14: three links, aria-current
  // on Architecture (index 2) this time, absent on the other two, still no role=tablist.
  const tabs = await page.evaluate(() =>
    [...document.querySelectorAll("[data-docs-tab]")].map((a) => ({
      tag: a.tagName,
      href: a.getAttribute("data-docs-tab"),
      current: a.getAttribute("aria-current"),
    })),
  );
  const hasTablist = await page.evaluate(
    () => document.querySelector('[role="tablist"]') !== null,
  );
  check(
    tabs.length === 3 &&
      tabs[0]?.current === null &&
      tabs[1]?.current === null &&
      tabs[2]?.current === "page" &&
      tabs.every((tab) => tab.tag === "A") &&
      !hasTablist,
    "/docs/architecture renders three tab links with aria-current on the active one and no role=tablist",
    JSON.stringify(tabs),
  );

  // Click → scroll, all seven. Reduced motion is emulated so the scroll is instant, exactly
  // as checkDocs does — see that function's comment about setEmulatedMedia replacing rather
  // than merging the override list.
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
  await page.reload({ waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);

  const notScrolled = [];
  for (const node of arch.nodes) {
    const landed = await page.evaluate(
      async ({ id, sectionId }) => {
        window.scrollTo(0, 0);
        const button = document.querySelector(`[data-arch-node="${id}"]`);
        if (!button) return { clicked: false };
        button.click();
        // `behavior: "auto"` under reduced motion, so one frame is enough.
        await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
        const heading = document.getElementById(sectionId);
        return { clicked: true, top: heading?.getBoundingClientRect().top ?? null };
      },
      { id: node.id, sectionId: node.sectionId },
    );
    // Headings carry scroll-mt-24 (96px), so a scrolled-to heading sits just below it. Several
    // nodes share a section id — that is expected, and each still has to land.
    if (!landed.clicked || landed.top === null || landed.top < 0 || landed.top > 160) {
      notScrolled.push(`${node.id} (heading top ${landed.top})`);
    }
  }
  check(
    notScrolled.length === 0,
    "clicking each of the seven topology nodes scrolls to its section",
    notScrolled.join(", "),
  );

  const reduced = await page.evaluate(probeDiagram, {
    containerAttr: "data-arch-diagram",
    nodeAttr: "data-arch-node",
    sectionAttr: "data-arch-section",
    edgesAttr: "data-arch-edges",
  });
  check(
    reduced.beams.length === ARCH_BEAM_COUNT && reduced.beams.every((beam) => beam.gradients === 0),
    "topology beams are static under prefers-reduced-motion: reduce",
    JSON.stringify(reduced.beams.map((beam) => beam.gradients)),
  );

  // 375px, still under reduced motion so nothing animates while measuring. The stacked column
  // is the layout most likely to leave a beam measured against the wide one.
  await page.setViewport({ width: 375, height: 812 });
  await page.reload({ waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  // Same bounded wait `checkDocs`'s initial load uses: a reload at a new viewport re-runs
  // AnimatedBeam's measuring effect from scratch, and probing before it has committed a path
  // reads every beam as unmeasured (d="") rather than misaligned — a fast local machine can
  // hide that race entirely, which is exactly what happened here before this wait existed.
  await page
    .waitForFunction(
      () => {
        const container = document.querySelector("[data-arch-diagram]");
        const svg = container?.querySelector(":scope > svg path");
        return Boolean(svg?.getAttribute("d"));
      },
      { timeout: 10_000 },
    )
    .catch(() => {
      // Reported by the assertions below, with the measurements attached.
    });
  const narrow = await page.evaluate(probeDiagram, {
    containerAttr: "data-arch-diagram",
    nodeAttr: "data-arch-node",
    sectionAttr: "data-arch-section",
    edgesAttr: "data-arch-edges",
  });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check(
    narrow.nodes.length === ARCH_NODE_COUNT &&
      narrow.beams.length === ARCH_BEAM_COUNT &&
      overflow <= 1,
    "the topology renders at 375px with no horizontal overflow",
    `${narrow.nodes.length} nodes, ${narrow.beams.length} beams, overflow ${overflow}px`,
  );

  const narrowMisaligned = misalignedArchBeams(narrow);
  check(
    narrowMisaligned.length === 0,
    "every topology beam still lands on its nodes' centres at 375px",
    narrowMisaligned.join(", "),
  );

  await page.setViewport({ width: 1280, height: 900 });
  // Restored before returning so the two trailing checks in main() — no console errors, every
  // same-origin asset loads — see a normal page rather than a narrow, reduced-motion one.
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "no-preference" },
  ]);
}

/** Every link in `/docs/features`'s in-page contents list, and whether its `href` resolves
 *  to a real heading — the third instance of `probeDiagram`'s anchor-gate argument, shaped
 *  for a plain list rather than a diagram: no nodes to measure, no beams, just `{id, href,
 *  headingTag}` per link. `data-features-contents` / `data-features-link` mirror
 *  `data-docs-diagram`/`data-docs-node` and `data-arch-diagram`/`data-arch-node`. */
function probeFeaturesContents() {
  const container = document.querySelector("[data-features-contents]");
  if (!container) return { container: false, links: [] };

  const links = [...container.querySelectorAll("[data-features-link]")].map((a) => {
    const id = a.getAttribute("data-features-link");
    const heading = document.getElementById(id ?? "");
    return {
      id,
      href: a.getAttribute("href"),
      name: (a.textContent ?? "").trim(),
      headingTag: heading?.tagName ?? null,
    };
  });

  return { container: true, links };
}

/** Assertions 22-24: `/docs/features`, PER-192's third tab. Run from `main()` immediately
 *  after `checkArchitecture`, on the same `page`, so the console-error and bad-response
 *  listeners set up in `main()` cover this route too. */
async function checkFeatures(page) {
  const response = await page.goto(`${BASE_URL}/docs/features`, { waitUntil: "load" });
  check(
    response?.status() === 200,
    "GET /docs/features returns 200",
    `got ${response?.status()}`,
  );

  await page.evaluate(() => document.fonts.ready);

  const headingCount = await page.evaluate(() => document.querySelectorAll("h2").length);
  check(headingCount > 0, "/docs/features renders headings", `got ${headingCount} h2 elements`);

  // THE ANCHOR GATE — the same argument as assertions 10 and 17, for the third file that can
  // drift the same way: lib/docs/features.ts names an id, rehype-slug derives the actual
  // heading id from app/docs/features/page.mdx's text, and nothing type-checks the join.
  const contents = await page.evaluate(probeFeaturesContents);
  if (!contents.container) {
    check(false, "the /docs/features contents list renders", "no [data-features-contents] in the document");
  } else {
    const dead = contents.links.filter((link) => link.headingTag !== "H2");
    check(
      dead.length === 0 && contents.links.length > 0,
      "every contents-list link resolves to an h2 heading",
      dead.length > 0
        ? `no <h2 id> for: ${dead.map((link) => link.id).join(", ")} — a heading in ` +
            "app/docs/features/page.mdx was renamed, or lib/docs/features.ts names an id " +
            "rehype-slug never generated"
        : "the contents list rendered no links at all",
    );
    check(
      contents.links.length > 0 && contents.links.every((link) => (link.name ?? "").length > 0),
      "every contents-list link has visible text",
      contents.links
        .filter((link) => !(link.name ?? "").length)
        .map((link) => link.id)
        .join(", "),
    );
  }

  // Tab nav on this route — mirror of checkDocs's assertion 14: three links, aria-current on
  // Features (index 0, the leftmost tab) this time, absent on the other two, still no
  // role=tablist.
  const tabs = await page.evaluate(() =>
    [...document.querySelectorAll("[data-docs-tab]")].map((a) => ({
      tag: a.tagName,
      href: a.getAttribute("data-docs-tab"),
      current: a.getAttribute("aria-current"),
    })),
  );
  const hasTablist = await page.evaluate(
    () => document.querySelector('[role="tablist"]') !== null,
  );
  check(
    tabs.length === 3 &&
      tabs[0]?.current === "page" &&
      tabs[1]?.current === null &&
      tabs[2]?.current === null &&
      tabs.every((tab) => tab.tag === "A") &&
      !hasTablist,
    "/docs/features renders three tab links with aria-current on the active one and no role=tablist",
    JSON.stringify(tabs),
  );
}

async function main() {
  const executablePath = findChrome();
  console.log(`chrome: ${executablePath}`);

  const server = startServer();
  let browser;
  try {
    await waitForServer(server);

    browser = await puppeteer.launch({
      executablePath,
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    });
    const page = await browser.newPage();
    page.setDefaultTimeout(30_000);
    await page.setViewport({ width: 1280, height: 900 });

    const consoleErrors = [];
    const badResponses = [];
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const url = message.location()?.url;
      if (isIgnoredUrl(url)) return;
      consoleErrors.push(`${message.text()}${url ? ` (${url})` : ""}`);
    });
    page.on("pageerror", (error) => consoleErrors.push(`uncaught: ${error.message}`));
    page.on("response", (response) => {
      const url = response.url();
      if (!url.startsWith(BASE_URL) || isIgnoredUrl(url)) return;
      if (response.status() >= 400) badResponses.push(`${response.status()} ${url}`);
    });
    page.on("requestfailed", (request) => {
      if (request.url().startsWith(BASE_URL)) {
        badResponses.push(`failed ${request.url()} (${request.failure()?.errorText})`);
      }
    });

    const response = await page.goto(BASE_URL, { waitUntil: "load" });
    check(response?.status() === 200, "GET / returns 200", `got ${response?.status()}`);

    // The entry animation starts at opacity 0 and is driven by client JavaScript, so a
    // page that never hydrates never becomes visible. Give it a bounded moment.
    await page
      .waitForFunction(
        () => {
          const heading = document.querySelector("h1, h2");
          if (!heading) return false;
          const box = heading.getBoundingClientRect();
          if (box.width === 0 || box.height === 0) return false;
          return typeof heading.checkVisibility === "function"
            ? heading.checkVisibility({ opacityProperty: true, visibilityProperty: true })
            : true;
        },
        { timeout: 10_000 },
      )
      .catch(() => {
        // Reported by the heading assertion below, with the measurements attached.
      });

    const probe = await page.evaluate(probePage);

    console.log(`\n--- measured ---\n${JSON.stringify(probe, null, 2)}\n`);

    check(probe.lang === "en", "<html lang> is en", `got "${probe.lang}"`);
    check(probe.title.length > 0, "the document has a title", `got "${probe.title}"`);

    check(
      probe.sansVar.length > 0,
      "--font-geist-sans is readable on <html>",
      "empty: app/globals.css resolves font-sans on <html>, so the font classes must be " +
        "on <html> too",
    );
    check(
      probe.monoVar.length > 0,
      "--font-geist-mono is readable on <html>",
      "empty: the variable is declared below <html>",
    );
    check(probe.sans.loaded, "the sans face loads", JSON.stringify(probe.sans));
    check(probe.mono.loaded, "the mono face loads", JSON.stringify(probe.mono));

    check(
      probe.htmlFirstFamily.length > 0 &&
        probe.htmlFirstFamily === probe.sans.family &&
        probe.loadedFamilies.includes(probe.htmlFirstFamily),
      "<html> resolves to the loaded sans face, not a fallback",
      `resolved "${probe.htmlFontFamily}", expected to start with "${probe.sans.family}"`,
    );
    check(
      probe.sans.family !== "" && probe.sans.family !== probe.mono.family,
      "sans and mono are different families",
      `both are "${probe.sans.family}"`,
    );

    check(
      probe.bodyBackgroundImage.startsWith("linear-gradient("),
      "<body> paints the page gradient",
      `background-image is "${probe.bodyBackgroundImage}"`,
    );

    // Light theme only (CLAUDE.md rule 7): the same document under a dark colour-scheme
    // preference must compute identical colours.
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
    const dark = await page.evaluate(() => {
      const style = getComputedStyle(document.body);
      return {
        backgroundColor: style.backgroundColor,
        color: style.color,
        backgroundImage: style.backgroundImage,
      };
    });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    check(
      dark.backgroundColor === probe.bodyBackgroundColor &&
        dark.color === probe.bodyColor &&
        dark.backgroundImage === probe.bodyBackgroundImage,
      "the page ignores prefers-color-scheme: dark",
      `light ${probe.bodyBackgroundColor}/${probe.bodyColor} vs dark ${dark.backgroundColor}/${dark.color}`,
    );

    check(
      probe.heading !== null &&
        probe.heading.width > 0 &&
        probe.heading.height > 0 &&
        probe.heading.visible,
      "a heading is rendered and visible",
      JSON.stringify(probe.heading),
    );
    check(probe.textLength > 0, "the page renders text", `innerText length ${probe.textLength}`);

    // `/docs` — the diagram, its anchors, and its beams. Runs on the same page object, so
    // the console and response listeners above cover it too.
    await checkDocs(page);

    // `/docs/architecture` — the topology diagram PER-192 added, same page object again.
    await checkArchitecture(page);

    // `/docs/features` — PER-192's third tab, the contents list rather than a diagram, same
    // page object again.
    await checkFeatures(page);

    check(consoleErrors.length === 0, "no console errors", consoleErrors.join(" | "));
    check(badResponses.length === 0, "every same-origin asset loads", badResponses.join(" | "));
  } finally {
    await browser?.close().catch(() => {});
    stopServer(server);
  }

  console.log("");
  if (failures.length > 0) {
    console.error(`smoke: ${failures.length} failing assertion(s): ${failures.join(", ")}`);
    process.exitCode = 1;
    return;
  }
  console.log("smoke: the rendered page is what it should be");
}

await main();
