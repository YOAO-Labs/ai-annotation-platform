import { chromium } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";

const baseUrl = process.env.POINTCLOUD_BENCH_BASE_URL ?? "http://127.0.0.1:3000";
const projectId = process.env.POINTCLOUD_BENCH_PROJECT_ID;
const taskId = process.env.POINTCLOUD_BENCH_TASK_ID;
const email = process.env.POINTCLOUD_BENCH_EMAIL ?? "admin";
const password = process.env.POINTCLOUD_BENCH_PASSWORD ?? "123456";
const rounds = Number.parseInt(process.env.POINTCLOUD_BENCH_ROUNDS ?? "1", 10);
const traceEnabled = process.env.POINTCLOUD_BENCH_TRACE === "1";
const warmPrefetchLeadMs = Number.parseInt(
  process.env.POINTCLOUD_BENCH_WARM_PREFETCH_LEAD_MS ?? "0",
  10,
);
const refinementReopens = Number.parseInt(
  process.env.POINTCLOUD_BENCH_REFINEMENT_REOPENS ?? "1",
  10,
);
const frameCycles = Number.parseInt(process.env.POINTCLOUD_BENCH_FRAME_CYCLES ?? "1", 10);
const memorySettleMs = Number.parseInt(process.env.POINTCLOUD_BENCH_MEMORY_SETTLE_MS ?? "0", 10);
const coldFrames = (
  process.env.POINTCLOUD_BENCH_COLD_FRAMES ??
  "5,12,19,26,33,2,9,16,23,30,37,6,13,20,27,34,3,10,17,24"
)
  .split(",")
  .map(Number)
  .filter(Number.isFinite);
const viewport = { width: 1440, height: 900 };
const deviceScaleFactor = Number(process.env.POINTCLOUD_BENCH_DPR ?? "1");
const resourcePhases = [
  "pcd-frame-ready",
  "camera-bitmaps-ready",
  "camera-depth-ready",
  "camera-textures-ready",
];

if (!projectId || !taskId) {
  throw new Error("POINTCLOUD_BENCH_PROJECT_ID and POINTCLOUD_BENCH_TASK_ID are required");
}
const parsedBaseUrl = new URL(baseUrl);
if (password === "123456" && !["localhost", "127.0.0.1", "::1"].includes(parsedBaseUrl.hostname)) {
  throw new Error("The default dev password may only be used against a loopback URL");
}
if (coldFrames.length === 0 || coldFrames.some((frame) => frame < 0)) {
  throw new Error("POINTCLOUD_BENCH_COLD_FRAMES must contain non-negative frame indexes");
}
if (!Number.isInteger(rounds) || rounds < 1 || rounds > 10) {
  throw new Error("POINTCLOUD_BENCH_ROUNDS must be an integer between 1 and 10");
}
if (!Number.isInteger(warmPrefetchLeadMs) || warmPrefetchLeadMs < 0) {
  throw new Error("POINTCLOUD_BENCH_WARM_PREFETCH_LEAD_MS must be a non-negative integer");
}
if (!Number.isInteger(refinementReopens) || refinementReopens < 0 || refinementReopens > 3) {
  throw new Error("POINTCLOUD_BENCH_REFINEMENT_REOPENS must be an integer between 0 and 3");
}
if (!Number.isInteger(frameCycles) || frameCycles < 1 || frameCycles > 5) {
  throw new Error("POINTCLOUD_BENCH_FRAME_CYCLES must be an integer between 1 and 5");
}
if (!Number.isInteger(memorySettleMs) || memorySettleMs < 0 || memorySettleMs > 10_000) {
  throw new Error("POINTCLOUD_BENCH_MEMORY_SETTLE_MS must be an integer between 0 and 10000");
}
if (!Number.isFinite(deviceScaleFactor) || deviceScaleFactor <= 0) {
  throw new Error("POINTCLOUD_BENCH_DPR must be a positive number");
}

function percentile(values, quantile) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * quantile) - 1)];
}

function summarize(values) {
  return {
    samples: values.length,
    p50Ms: percentile(values, 0.5),
    p95Ms: percentile(values, 0.95),
    minMs: values.length ? Math.min(...values) : null,
    maxMs: values.length ? Math.max(...values) : null,
  };
}

function improvementPercent(baseline, candidate) {
  if (baseline == null || candidate == null || baseline <= 0) return null;
  return ((baseline - candidate) / baseline) * 100;
}

function summarizeStages(samples) {
  return Object.fromEntries(
    resourcePhases.map((phase) => {
      const events = samples.flatMap((sample) =>
        sample.stages.filter((event) => event.phase === phase),
      );
      const cacheSamples = events.filter((event) => typeof event.cacheHit === "boolean");
      return [
        phase,
        {
          ...summarize(events.map((event) => event.durationMs)),
          cacheHits: cacheSamples.filter((event) => event.cacheHit).length,
          cacheHitRate:
            cacheSamples.length > 0
              ? cacheSamples.filter((event) => event.cacheHit).length / cacheSamples.length
              : null,
        },
      ];
    }),
  );
}

function isFullyWarmSample(sample) {
  const cacheStages = sample.stages.filter((event) => typeof event.cacheHit === "boolean");
  return cacheStages.length > 0 && cacheStages.every((event) => event.cacheHit);
}

async function api(path, init = {}) {
  const response = await fetch(new URL(`/api/v1${path}`, baseUrl), {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  if (!response.ok) throw new Error(`${init.method ?? "GET"} ${path}: HTTP ${response.status}`);
  if (response.status === 204) return null;
  return response.json();
}

async function waitForTwoFrames(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      }),
  );
}

async function waitForTimingPhase(page, phase) {
  await page.waitForFunction(
    (targetPhase) => window.__pointCloudTimingEvents?.some((event) => event.phase === targetPhase),
    phase,
    { timeout: 30_000 },
  );
  return page.evaluate(
    (targetPhase) =>
      window.__pointCloudTimingEvents.findLast((event) => event.phase === targetPhase),
    phase,
  );
}

async function beginFrameDiagnostics(page, label) {
  return page.evaluate((diagnosticLabel) => {
    const startedAt = performance.now();
    const session = { active: true, label: diagnosticLabel, rafGaps: [] };
    window.__pointCloudFrameDiagnostics = session;
    performance.mark(`aap-bench-start:${diagnosticLabel}`);
    console.timeStamp(`aap-bench-start:${diagnosticLabel}`);
    let previous = startedAt;
    const sample = (now) => {
      if (!session.active) return;
      const gap = now - previous;
      if (gap > 0) session.rafGaps.push(gap);
      previous = now;
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
    return startedAt;
  });
}

async function endFrameDiagnostics(page, startedAt, completedAt) {
  const diagnostics = await page.evaluate(
    ({ after, before }) => {
      const session = window.__pointCloudFrameDiagnostics;
      if (session) session.active = false;
      if (session?.label) {
        performance.mark(`aap-bench-end:${session.label}`);
        console.timeStamp(`aap-bench-end:${session.label}`);
      }
      return {
        rafGaps: session?.rafGaps ?? [],
        longTasks: (window.__pointCloudLongTasks ?? [])
          .filter((entry) => entry.startTime >= after && entry.startTime <= before)
          .map((entry) => entry.duration),
      };
    },
    { after: startedAt, before: completedAt },
  );
  return {
    raf: summarize(diagnostics.rafGaps),
    longTasks: {
      ...summarize(diagnostics.longTasks),
      totalMs: diagnostics.longTasks.reduce((total, duration) => total + duration, 0),
    },
  };
}

async function startDevToolsTrace(context, page) {
  const client = await context.newCDPSession(page);
  const events = [];
  client.on("Tracing.dataCollected", ({ value }) => events.push(...value));
  await client.send("Tracing.start", {
    categories: [
      "devtools.timeline",
      "v8.execute",
      "blink.user_timing",
      "disabled-by-default-devtools.timeline",
    ].join(","),
    options: "sampling-frequency=10000",
  });
  return { client, events, stopped: false };
}

function summarizeDevToolsTrace(events) {
  const mainThread = events.find(
    (event) =>
      event.ph === "M" && event.name === "thread_name" && event.args?.name === "CrRendererMain",
  );
  if (!mainThread) return { eventCount: events.length, mainThreadFound: false, longTasks: [] };
  const mainEvents = events.filter(
    (event) => event.pid === mainThread.pid && event.tid === mainThread.tid,
  );
  const markers = mainEvents
    .flatMap((event) => {
      const message = event.args?.data?.message ?? event.name;
      return message.startsWith("aap-bench-") ? [{ at: event.ts, message }] : [];
    })
    .sort((a, b) => a.at - b.at);
  const longTasks = mainEvents
    .filter((event) => event.name === "RunTask" && (event.dur ?? 0) >= 50_000)
    .map((task) => {
      const taskEnd = task.ts + task.dur;
      const startMarker = markers.findLast(
        (marker) => marker.at <= task.ts && marker.message.startsWith("aap-bench-start:"),
      );
      const endMarker = markers.find(
        (marker) => marker.at >= taskEnd && marker.message.startsWith("aap-bench-end:"),
      );
      const label =
        startMarker &&
        endMarker &&
        endMarker.message.slice("aap-bench-end:".length) ===
          startMarker.message.slice("aap-bench-start:".length)
          ? startMarker.message.slice("aap-bench-start:".length)
          : null;
      const children = mainEvents
        .filter(
          (event) =>
            event !== task &&
            event.ph === "X" &&
            (event.dur ?? 0) >= 1_000 &&
            event.ts >= task.ts &&
            event.ts + event.dur <= taskEnd,
        )
        .sort((a, b) => b.dur - a.dur)
        .slice(0, 12)
        .map((event) => ({
          name: event.name,
          durationMs: event.dur / 1_000,
          functionName: event.args?.data?.functionName ?? null,
          url: event.args?.data?.url ?? null,
          lineNumber: event.args?.data?.lineNumber ?? null,
        }));
      return { label, durationMs: task.dur / 1_000, children };
    });
  return { eventCount: events.length, mainThreadFound: true, longTasks };
}

async function stopDevToolsTrace(session) {
  if (!session || session.stopped) return null;
  session.stopped = true;
  const completed = new Promise((resolve) =>
    session.client.once("Tracing.tracingComplete", resolve),
  );
  await session.client.send("Tracing.end");
  await completed;
  return summarizeDevToolsTrace(session.events);
}

async function measureFrame(page, frame, kind, mode) {
  const target = page.getByTestId(`scene-timeline-frame-${frame}`);
  await page.waitForFunction((index) => {
    const container = document.querySelector('[aria-label="Scene 帧列表"]');
    if (!(container instanceof HTMLElement)) return false;
    container.scrollLeft = Math.max(0, index * 40 - container.clientWidth / 2);
    const button = document.querySelector(`[data-testid="scene-timeline-frame-${index}"]`);
    return button instanceof HTMLButtonElement && !button.disabled;
  }, frame);
  if (kind === "warm-adjacent" && warmPrefetchLeadMs > 0) {
    await target.hover();
    await page.waitForTimeout(warmPrefetchLeadMs);
  }
  await page.evaluate(() => {
    window.__pointCloudTimingEvents = [];
    window.__pointCloudNavigationEvents = [];
  });
  const diagnosticsStartedAt = await beginFrameDiagnostics(page, `${mode}:${kind}:${frame}`);
  await page.waitForFunction((index) => {
    const container = document.querySelector('[aria-label="Scene 帧列表"]');
    if (container instanceof HTMLElement) {
      container.scrollLeft = Math.max(0, index * 40 - container.clientWidth / 2);
    }
    const button = document.querySelector(`[data-testid="scene-timeline-frame-${index}"]`);
    if (!(button instanceof HTMLButtonElement) || button.disabled) return null;
    window.__pointCloudBenchmarkClickAt = null;
    button.addEventListener(
      "click",
      () => {
        window.__pointCloudBenchmarkClickAt = performance.now();
      },
      { capture: true, once: true },
    );
    button.click();
    return typeof window.__pointCloudBenchmarkClickAt === "number";
  }, frame);
  const startedAt = await page.evaluate(() => window.__pointCloudBenchmarkClickAt);
  if (typeof startedAt !== "number")
    throw new Error(`No click timestamp recorded for frame ${frame}`);
  const geometry = await waitForTimingPhase(page, "geometry-ready");
  const color = await waitForTimingPhase(page, "camera-color-ready");
  const stages = await page.evaluate(
    ({ after, phases, pointCloudUrl, completedAt }) =>
      phases.flatMap((phase) => {
        const event = window.__pointCloudTimingEvents.findLast(
          (candidate) =>
            candidate.at >= after &&
            candidate.at <= completedAt &&
            candidate.phase === phase &&
            candidate.pointCloudUrl === pointCloudUrl &&
            typeof candidate.durationMs === "number",
        );
        return event ? [event] : [];
      }),
    {
      after: startedAt,
      phases: resourcePhases,
      pointCloudUrl: color.pointCloudUrl,
      completedAt: color.at,
    },
  );
  const diagnostics = await endFrameDiagnostics(page, diagnosticsStartedAt, color.at);
  const navigation = await page.evaluate(
    ({ after, before }) =>
      (window.__pointCloudNavigationEvents ?? []).find(
        (event) => event.at >= after && event.at <= before,
      ) ?? null,
    { after: startedAt, before: color.at },
  );
  if (!navigation) throw new Error(`No history update recorded for frame ${frame}`);
  const rendererStartedAt = navigation.at;
  const pcdReady = stages.find((event) => event.phase === "pcd-frame-ready");
  return {
    kind,
    frame,
    geometryMs: geometry.at - rendererStartedAt,
    rgbMs: color.at - rendererStartedAt,
    segments: {
      clickToHistoryUpdateMs: rendererStartedAt - startedAt,
      historyUpdateToPcdReadyMs: pcdReady ? pcdReady.at - rendererStartedAt : null,
      navigationToPcdReadyMs: pcdReady ? pcdReady.at - rendererStartedAt : null,
      pcdReadyToGeometryMs: pcdReady ? geometry.at - pcdReady.at : null,
      geometryToRgbMs: color.at - geometry.at,
    },
    diagnostics,
    stages,
  };
}

async function sampleAnimationFrames(page, durationMs = 2_000) {
  const intervals = await page.evaluate(
    (duration) =>
      new Promise((resolve) => {
        const values = [];
        const startedAt = performance.now();
        let previous = startedAt;
        const sample = (now) => {
          values.push(now - previous);
          previous = now;
          if (now - startedAt >= duration) resolve(values.slice(1));
          else requestAnimationFrame(sample);
        };
        requestAnimationFrame(sample);
      }),
    durationMs,
  );
  return summarize(intervals);
}

function statusKilobytes(status, field) {
  const match = status.match(new RegExp(`^${field}:\\s+(\\d+)\\s+kB$`, "m"));
  return match ? Number.parseInt(match[1], 10) : null;
}

async function readProcessMemory(pid) {
  if (process.platform === "darwin") {
    try {
      const rssKiB = Number(
        execFileSync("ps", ["-o", "rss=", "-p", String(pid)], { encoding: "utf8" }).trim(),
      );
      return { rssBytes: rssKiB * 1024, highWaterBytes: null };
    } catch {
      return null;
    }
  }
  if (process.platform !== "linux") return null;
  try {
    const status = await readFile(`/proc/${pid}/status`, "utf8");
    const rssKiB = statusKilobytes(status, "VmRSS");
    const highWaterKiB = statusKilobytes(status, "VmHWM");
    return {
      rssBytes: rssKiB === null ? null : rssKiB * 1024,
      highWaterBytes: highWaterKiB === null ? null : highWaterKiB * 1024,
    };
  } catch {
    return null;
  }
}

async function sampleMemory(browser, page, label) {
  if (memorySettleMs > 0) await page.waitForTimeout(memorySettleMs);
  await waitForTwoFrames(page);
  const pageSession = await page.context().newCDPSession(page);
  await pageSession.send("HeapProfiler.collectGarbage").catch(() => {});
  const heapUsage = await pageSession.send("Runtime.getHeapUsage").catch(() => null);
  await pageSession.detach();
  const heap = await page.evaluate(() => {
    const memory = performance.memory;
    return memory
      ? {
          usedJsHeapBytes: memory.usedJSHeapSize,
          totalJsHeapBytes: memory.totalJSHeapSize,
        }
      : null;
  });
  const pointCloudDiagnostics = await page.evaluate(() => ({
    assetCache: window.__pointCloudAssetCacheDiagnostics?.() ?? null,
    computeSession: window.__pointCloudComputeDiagnostics?.() ?? null,
  }));
  const browserSession = await browser.newBrowserCDPSession();
  const { processInfo } = await browserSession.send("SystemInfo.getProcessInfo");
  await browserSession.detach();
  const processes = await Promise.all(
    processInfo.map(async (entry) => ({
      type: entry.type,
      pid: entry.id,
      ...(await readProcessMemory(entry.id)),
    })),
  );
  const rssByType = {};
  for (const entry of processes) {
    if (entry.rssBytes === null || entry.rssBytes === undefined) continue;
    rssByType[entry.type] = (rssByType[entry.type] ?? 0) + entry.rssBytes;
  }
  return {
    label,
    at: new Date().toISOString(),
    heap,
    heapUsage,
    pointCloudDiagnostics,
    rssByType,
    processes,
  };
}

async function runMode(browser, token, user, mode) {
  const context = await browser.newContext({ baseURL: baseUrl, viewport, deviceScaleFactor });
  // Layout commands are real UI operations, but benchmark-only changes must not
  // overwrite the account's workspace (including an originally absent context).
  let benchmarkPreferences = await api("/auth/me/preferences", {
    headers: { Authorization: `Bearer ${token}` },
  });
  await context.route("**/api/v1/auth/me/preferences", async (route) => {
    const request = route.request();
    if (request.method() !== "PATCH") return route.continue();
    const payload = request.postDataJSON();
    const workspacePatch = payload.workbench?.layout?.workspace;
    if (!workspacePatch) return route.continue();
    const { workspace: _workspace, ...layout } = payload.workbench.layout;
    const { layout: _layout, ...workbench } = payload.workbench;
    const { workbench: _workbench, ...other } = payload;
    if (Object.keys(layout).length || Object.keys(workbench).length || Object.keys(other).length) {
      const response = await route.fetch({
        postData: JSON.stringify({ ...other, workbench: { ...workbench, layout } }),
      });
      if (!response.ok()) return route.fulfill({ response });
      benchmarkPreferences = await response.json();
    }
    benchmarkPreferences.workbench.layout.workspace = {
      engine: "dockview@8",
      contexts: {
        ...benchmarkPreferences.workbench.layout.workspace?.contexts,
        ...workspacePatch.contexts,
      },
    };
    await route.fulfill({ json: benchmarkPreferences });
  });
  await context.addInitScript(
    ({ accessToken, currentUser, enabled }) => {
      localStorage.setItem("token", accessToken);
      localStorage.setItem(
        "auth-storage",
        JSON.stringify({ state: { token: accessToken, user: currentUser }, version: 0 }),
      );
      localStorage.setItem("aap.experiment.pointCloudWebGpuRenderer", enabled ? "1" : "0");
      const NativeWorker = window.Worker;
      const WrappedWorker = function (...args) {
        if (String(args[0]).includes("pointcloud.worker")) {
          window.__pointCloudWorkerCount = (window.__pointCloudWorkerCount ?? 0) + 1;
        }
        return new NativeWorker(...args);
      };
      WrappedWorker.prototype = NativeWorker.prototype;
      window.Worker = WrappedWorker;
      window.__pointCloudWorkerCount = 0;
      window.__pointCloudImageReadbacks = 0;
      window.__pointCloudTimingEvents = [];
      window.__pointCloudLongTasks = [];
      window.__pointCloudNavigationEvents = [];
      for (const method of ["pushState", "replaceState"]) {
        const originalHistoryMethod = history[method];
        history[method] = function (...args) {
          const result = originalHistoryMethod.apply(this, args);
          window.__pointCloudNavigationEvents.push({
            method,
            at: performance.now(),
            url: String(args[2] ?? location.href),
          });
          return result;
        };
      }
      try {
        const observer = new PerformanceObserver((list) => {
          window.__pointCloudLongTasks.push(
            ...list.getEntries().map((entry) => ({
              startTime: entry.startTime,
              duration: entry.duration,
            })),
          );
        });
        observer.observe({ type: "longtask", buffered: true });
        window.__pointCloudLongTaskObserver = observer;
      } catch {
        // Long Tasks is optional; rAF gaps still expose presentation stalls.
      }
      window.addEventListener("aap:pointcloud-render-timing", (event) => {
        window.__pointCloudTimingEvents.push(event.detail);
      });
      const original = CanvasRenderingContext2D.prototype.getImageData;
      CanvasRenderingContext2D.prototype.getImageData = function (...args) {
        if (Number(args[2]) * Number(args[3]) > 1) window.__pointCloudImageReadbacks += 1;
        return original.apply(this, args);
      };
    },
    { accessToken: token, currentUser: user, enabled: mode === "webgpu-experimental" },
  );
  const page = await context.newPage();
  const traceSession = traceEnabled ? await startDevToolsTrace(context, page) : null;
  const runtimeErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => runtimeErrors.push(`pageerror: ${error.message}`));

  try {
    await page.goto(`/projects/${projectId}/annotate?task=${taskId}`);
    await page.getByTestId("three-d-scene-timeline").waitFor({ state: "visible", timeout: 30_000 });
    // The compact scene timeline defaults to collapsed; frame navigation below needs
    // the expanded frame list. Expand before the initial geometry wait so background
    // timeline loading matches the previously always-expanded layout.
    const expandTimeline = page.getByRole("button", { name: "展开 Scene 时间轴" });
    if (
      (await expandTimeline.count()) > 0 &&
      (await expandTimeline.getAttribute("aria-expanded")) !== "true"
    ) {
      await expandTimeline.click();
    }
    const initialGeometryMs = (await waitForTimingPhase(page, "geometry-ready")).at;
    const initialRgbMs = (await waitForTimingPhase(page, "camera-color-ready")).at;
    const backendBadge = page.getByTestId("pointcloud-renderer-backend");
    const actualBackend = await backendBadge.getAttribute("data-backend");
    const rendererViewport = page.getByTestId("pc-viewport");
    const rendererOwnerCount = Number(
      (await rendererViewport.getAttribute("data-pointcloud-renderer-count")) ?? "0",
    );

    const idleRaf = await sampleAnimationFrames(page);
    await page.waitForTimeout(300);
    const idleSubmitBefore = Number(
      (await rendererViewport.getAttribute("data-pointcloud-submit-count")) ?? "0",
    );
    await page.waitForTimeout(300);
    const idleSubmitAfter = Number(
      (await rendererViewport.getAttribute("data-pointcloud-submit-count")) ?? "0",
    );
    let refinement = null;
    const refinementReopen = [];
    const firstBox = page.locator('[data-testid^="box-list-item-"]').first();
    if ((await firstBox.count()) === 0) {
      const manualSection = page.getByTestId("section-header-manual");
      if (
        (await manualSection.count()) > 0 &&
        (await manualSection.getAttribute("aria-expanded")) === "false"
      ) {
        await manualSection.click();
        await firstBox.waitFor({ state: "visible", timeout: 5_000 }).catch(() => {});
      }
    }
    if ((await firstBox.count()) > 0) {
      // Since the dockview layout rework, panel visibility lives in the workbench
      // settings dialog (面板与高级布局 details); the tri-view panel no longer has
      // an inline collapse button or a layout menu item.
      const openTriViewToggle = async () => {
        await page.getByRole("button", { name: "工作台设置" }).click();
        const dialog = page.getByRole("dialog");
        await dialog.waitFor({ state: "visible" });
        const panelDetails = page.locator("details", { hasText: "面板与高级布局" });
        if (!(await panelDetails.getAttribute("open"))) {
          await page.getByText("面板与高级布局", { exact: true }).click();
        }
        return page.locator("button[aria-label='三视图精修']");
      };
      const hideTriViewIfOpen = async () => {
        const toggle = await openTriViewToggle();
        if ((await toggle.getAttribute("aria-pressed")) === "true") {
          await toggle.click();
          await page.waitForTimeout(200);
        }
        await page.keyboard.press("Escape");
        await page.getByRole("dialog").waitFor({ state: "hidden" });
      };
      await hideTriViewIfOpen();
      await firstBox.click({ position: { x: 12, y: 16 } });
      await waitForTwoFrames(page);
      const measureRefinement = async (label) => {
        // Opening the settings dialog is outside the visibility-to-first-render measurement.
        const openRefinement = await openTriViewToggle();
        await openRefinement.waitFor({ state: "visible" });
        const diagnosticsStartedAt = await beginFrameDiagnostics(page, `${mode}:${label}`);
        const activeRenderCountBefore = Number(
          (await rendererViewport.getAttribute("data-pointcloud-tri-pass-count")) ?? "0",
        );
        const startedAt = await openRefinement.evaluate((button) => {
          const at = performance.now();
          button.click();
          return at;
        });
        // Keep the settings dialog open while the panel expands: dismissing it right
        // after the click can race the dialog's Escape handling.
        await page.getByTestId("tri-view-renderer-panel").waitFor({ state: "visible" });
        await page.waitForFunction((previousCount) => {
          const viewport = document.querySelector('[data-testid="pc-viewport"]');
          return (
            Number(viewport?.getAttribute("data-pointcloud-tri-pass-count") ?? "0") > previousCount
          );
        }, activeRenderCountBefore);
        const firstFrameAt = Number(
          await rendererViewport.getAttribute("data-pointcloud-tri-active-render-at"),
        );
        const openMs = firstFrameAt - startedAt;
        const raf = await sampleAnimationFrames(page);
        const diagnosticsCompletedAt = await page.evaluate(() => performance.now());
        const result = {
          openMs,
          raf,
          diagnostics: await endFrameDiagnostics(
            page,
            diagnosticsStartedAt,
            diagnosticsCompletedAt,
          ),
        };
        await page.keyboard.press("Escape");
        await page.getByRole("dialog").waitFor({ state: "hidden" });
        await hideTriViewIfOpen();
        return result;
      };
      refinement = await measureRefinement("refinement");
      for (let index = 0; index < refinementReopens; index += 1) {
        await waitForTwoFrames(page);
        refinementReopen.push(await measureRefinement(`refinement-reopen-${index + 1}`));
      }
    }

    const frames = [];
    const memoryCheckpoints = [await sampleMemory(browser, page, "initial")];
    for (let cycle = 1; cycle <= frameCycles; cycle += 1) {
      for (const coldFrame of coldFrames) {
        frames.push(await measureFrame(page, coldFrame, "cold", mode));
        await page.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => {});
        const warmFrame = coldFrame + 1;
        if ((await page.getByTestId(`scene-timeline-frame-${warmFrame}`).count()) > 0) {
          frames.push(await measureFrame(page, warmFrame, "warm-adjacent", mode));
        }
      }
      memoryCheckpoints.push(await sampleMemory(browser, page, `after-cycle-${cycle}`));
    }

    const cold = frames.filter((sample) => sample.kind === "cold");
    const warm = frames.filter((sample) => sample.kind === "warm-adjacent");
    const warmReady = warm.filter(isFullyWarmSample);
    const trace = await stopDevToolsTrace(traceSession);
    return {
      requestedMode: mode,
      actualBackend,
      initial: { geometryMs: initialGeometryMs, rgbMs: initialRgbMs },
      cold: {
        geometry: summarize(cold.map((sample) => sample.geometryMs)),
        rgb: summarize(cold.map((sample) => sample.rgbMs)),
        stages: summarizeStages(cold),
      },
      warmAdjacent: {
        geometry: summarize(warm.map((sample) => sample.geometryMs)),
        rgb: summarize(warm.map((sample) => sample.rgbMs)),
        stages: summarizeStages(warm),
      },
      warmAdjacentReady: {
        geometry: summarize(warmReady.map((sample) => sample.geometryMs)),
        rgb: summarize(warmReady.map((sample) => sample.rgbMs)),
      },
      idleRaf,
      rendererOwnerCount,
      idleSubmitDelta: idleSubmitAfter - idleSubmitBefore,
      refinement,
      refinementReopen,
      workerCount: await page.evaluate(() => window.__pointCloudWorkerCount ?? 0),
      imageReadbacks: await page.evaluate(() => window.__pointCloudImageReadbacks ?? 0),
      memoryCheckpoints,
      runtimeErrors,
      trace,
      samples: frames,
    };
  } finally {
    await stopDevToolsTrace(traceSession).catch(() => {});
    await context.close();
  }
}

async function ensureBenchmarkBox(headers) {
  const annotations = await api(`/tasks/${taskId}/annotations`, { headers });
  const visibleBox = annotations.find(
    (annotation) => annotation.geometry?.type === "box_3d" && !annotation.is_hidden,
  );
  if (visibleBox) return null;

  const project = await api(`/projects/${projectId}`, { headers });
  const className =
    project.tool_bindings?.lidar_box_3d?.classes?.[0]?.name ?? project.classes?.[0] ?? null;
  if (!className) {
    throw new Error("The benchmark project has no lidar_box_3d class for a temporary fixture");
  }
  return api(`/tasks/${taskId}/annotations`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      annotation_type: "box_3d",
      tool_unit_id: "lidar_box_3d",
      class_name: className,
      geometry: {
        type: "box_3d",
        center: [1, 0, 1],
        size: [4, 2, 1.6],
        rotation: [0, 0, 0],
        convention_at_create: "iso_8855",
      },
      attributes: { benchmark_fixture: true },
    }),
  });
}

function buildReport(legacy, experimental, round) {
  const refinementReopen = experimental.refinementReopen[0] ?? null;
  const runValidity = {
    actualWebGpu: experimental.actualBackend === "webgpu",
    noExperimentalImageReadback: experimental.imageReadbacks === 0,
    noRuntimeErrors: legacy.runtimeErrors.length === 0 && experimental.runtimeErrors.length === 0,
  };
  const promotionChecks = {
    enoughColdSamples: experimental.cold.geometry.samples >= 10,
    enoughWarmSamples: experimental.warmAdjacent.geometry.samples >= 20,
    enoughWarmReadySamples:
      legacy.warmAdjacentReady.rgb.samples >= 16 &&
      experimental.warmAdjacentReady.rgb.samples >= 16,
    coldGeometryP95AtMost250Ms: experimental.cold.geometry.p95Ms <= 250,
    warmGeometryP95AtMost120Ms: experimental.warmAdjacent.geometry.p95Ms <= 120,
    coldRgbP95AtMost700Ms: experimental.cold.rgb.p95Ms <= 700,
    warmRgbP95AtMost250Ms: experimental.warmAdjacent.rgb.p95Ms <= 250,
    warmReadyRgbP95ImprovementAtLeast60Percent:
      improvementPercent(
        legacy.warmAdjacentReady.rgb.p95Ms,
        experimental.warmAdjacentReady.rgb.p95Ms,
      ) >= 60,
    warmDepthRasterCacheHitRateAtLeast80Percent:
      experimental.warmAdjacent.stages["camera-depth-ready"].cacheHitRate >= 0.8,
    refinementMeasured: legacy.refinement !== null && experimental.refinement !== null,
    refinementReopenMeasured: refinementReopen !== null,
    refinementOpenAtMost50Ms: experimental.refinement?.openMs <= 50,
    refinementOpenWithin10MsOfReopen:
      refinementReopen !== null && experimental.refinement?.openMs <= refinementReopen.openMs + 10,
    refinementRafP95NoWorseThan10Percent:
      experimental.refinement?.raf.p95Ms <= legacy.refinement?.raf.p95Ms * 1.1,
    refinementHasNoFrameGapOver100Ms: experimental.refinement?.raf.maxMs <= 100,
  };
  return {
    round,
    generatedAt: new Date().toISOString(),
    baseUrl,
    projectId,
    taskId,
    viewport,
    deviceScaleFactor,
    coldFrames,
    frameCycles,
    memorySettleMs,
    temporaryBoxCreated: temporaryBox !== null,
    legacy,
    experimental,
    comparison: {
      coldGeometryP95ImprovementPercent: improvementPercent(
        legacy.cold.geometry.p95Ms,
        experimental.cold.geometry.p95Ms,
      ),
      coldRgbP95ImprovementPercent: improvementPercent(
        legacy.cold.rgb.p95Ms,
        experimental.cold.rgb.p95Ms,
      ),
      warmGeometryP95ImprovementPercent: improvementPercent(
        legacy.warmAdjacent.geometry.p95Ms,
        experimental.warmAdjacent.geometry.p95Ms,
      ),
      warmRgbP95ImprovementPercent: improvementPercent(
        legacy.warmAdjacent.rgb.p95Ms,
        experimental.warmAdjacent.rgb.p95Ms,
      ),
      warmReadyRgbP95ImprovementPercent: improvementPercent(
        legacy.warmAdjacentReady.rgb.p95Ms,
        experimental.warmAdjacentReady.rgb.p95Ms,
      ),
    },
    runValidity,
    promotionGate: {
      passed:
        Object.values(runValidity).every(Boolean) && Object.values(promotionChecks).every(Boolean),
      checks: promotionChecks,
    },
  };
}

const login = await api("/auth/login", {
  method: "POST",
  body: JSON.stringify({ email, password }),
});
const headers = { Authorization: `Bearer ${login.access_token}` };
const user = await api("/auth/me", { headers });
const preferences = await api("/auth/me/preferences", { headers });
const originalColorize = preferences?.workbench?.pointcloud?.colorizeWithCamera ?? false;
let temporaryBox = null;
let browser = null;

try {
  temporaryBox = await ensureBenchmarkBox(headers);
  await api("/auth/me/preferences", {
    method: "PATCH",
    headers,
    body: JSON.stringify({ workbench: { pointcloud: { colorizeWithCamera: true } } }),
  });
  browser = await chromium.launch(
    process.platform === "darwin"
      ? { channel: "chrome", headless: false }
      : {
          headless: true,
          args: [
            "--enable-unsafe-webgpu",
            "--enable-features=Vulkan",
            "--use-angle=vulkan",
            "--disable-vulkan-surface",
            "--ignore-gpu-blocklist",
          ],
        },
  );
  const reports = [];
  for (let round = 1; round <= rounds; round += 1) {
    const legacy = await runMode(browser, login.access_token, user, "legacy");
    const experimental = await runMode(browser, login.access_token, user, "webgpu-experimental");
    reports.push(buildReport(legacy, experimental, round));
  }
  const output = rounds === 1 ? reports[0] : { rounds: reports };
  process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
  if (reports.some((report) => !Object.values(report.runValidity).every(Boolean))) {
    process.exitCode = 1;
  }
} finally {
  await browser?.close();
  try {
    await api("/auth/me/preferences", {
      method: "PATCH",
      headers,
      body: JSON.stringify({
        workbench: { pointcloud: { colorizeWithCamera: originalColorize } },
      }),
    });
  } catch (error) {
    process.stderr.write(`Failed to restore point-cloud preference: ${String(error)}\n`);
    process.exitCode = 1;
  }
  if (temporaryBox) {
    try {
      await api(`/tasks/${taskId}/annotations/${temporaryBox.id}`, {
        method: "DELETE",
        headers,
      });
    } catch (error) {
      process.stderr.write(
        `Failed to remove temporary benchmark annotation ${temporaryBox.id}: ${String(error)}\n`,
      );
      process.exitCode = 1;
    }
  }
}
