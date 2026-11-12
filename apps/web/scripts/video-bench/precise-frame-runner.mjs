import { chromium } from "@playwright/test";
import { browserEnvironment } from "../../e2e/helpers/browser-environment.mjs";

const STAGE_SELECTOR = '[data-testid="video-konva-stage"]';
const VIDEO_SELECTOR = '[data-testid="video-konva-source"]';
const SLIDER_SELECTOR = 'input[aria-label="视频帧时间轴"]';
const PLAY_SELECTOR = 'button[aria-label="播放 / 暂停"]';
const WEBCODECS_PLAYER_URL = "WebCodecs::VideoDecoder";

function percentile(values, quantile) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * quantile) - 1)];
}

function absoluteTaskUrl(baseUrl, rawUrl) {
  return new URL(rawUrl, baseUrl).toString();
}

function taskUrlWithWebCodecs(rawUrl, enabled) {
  const url = new URL(rawUrl);
  url.searchParams.set("webcodecs", enabled ? "1" : "0");
  return url.toString();
}

function sliderValueForFrame(targetFrame, maxFrame, sliderMax = 10_000) {
  if (!Number.isInteger(targetFrame) || targetFrame < 0 || targetFrame > maxFrame) {
    throw new Error(`target frame ${targetFrame} is outside 0..${maxFrame}`);
  }
  if (!Number.isInteger(maxFrame) || maxFrame <= 0) {
    throw new Error(`invalid max frame ${maxFrame}`);
  }
  return Math.round((targetFrame / maxFrame) * sliderMax);
}

function seededSampleTargets(frames, count, seed = 0x23_15_20_26) {
  if (!Array.isArray(frames) || frames.length === 0 || count <= 0) return [];
  let state = seed >>> 0;
  const random = () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
    return state / 0x1_0000_0000;
  };
  const targets = [];
  while (targets.length < count) {
    const shuffled = [...frames];
    for (let index = shuffled.length - 1; index > 0; index -= 1) {
      const swapIndex = Math.floor(random() * (index + 1));
      [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
    }
    targets.push(...shuffled.slice(0, count - targets.length));
  }
  return targets;
}

async function readBrowserGpuEnvironment(browser) {
  try {
    const session = await browser.newBrowserCDPSession();
    const info = await session.send("SystemInfo.getInfo");
    await session.detach();
    const profiles = Array.isArray(info.gpu?.videoDecoding) ? info.gpu.videoDecoding : [];
    return {
      gpuFeatureStatus: info.gpu?.featureStatus ?? null,
      hardwareVideoDecodeProfiles: profiles,
      hardwareVideoDecode:
        info.gpu?.featureStatus?.video_decode === "enabled" && profiles.length > 0,
    };
  } catch {
    return {
      gpuFeatureStatus: null,
      hardwareVideoDecodeProfiles: null,
      hardwareVideoDecode: null,
    };
  }
}

function classifyVideoDecoderEvidence(
  players,
  { available = true, platform = process.platform, arch = process.arch } = {},
) {
  if (platform !== "darwin") {
    return {
      status: "not-applicable",
      matchedPlayerCount: 0,
      decoderName: null,
      platformDecoder: null,
      reason: null,
    };
  }
  if (arch !== "arm64") {
    return {
      status: "unsupported-host",
      matchedPlayerCount: 0,
      decoderName: null,
      platformDecoder: null,
      reason: "Intel macOS VideoToolbox may use a software fallback",
    };
  }
  const matched = players.filter((player) => player.loadUrls?.includes(WEBCODECS_PLAYER_URL));
  const decoderNamesFor = (player) =>
    Array.isArray(player.decoderNames)
      ? player.decoderNames
      : player.decoderName !== null && player.decoderName !== undefined
        ? [player.decoderName]
        : [];
  const platformValuesFor = (player) =>
    Array.isArray(player.platformDecoderValues)
      ? player.platformDecoderValues
      : typeof player.platformDecoder === "boolean"
        ? [player.platformDecoder]
        : [];
  // Chromium 会为未走到 initialize 的瞬时 VideoDecoder 只发 kLoad；它们没有选择 decoder，
  // 不能作为正证据或反证据。任何发布过一侧属性的 player 都算 initialized，并必须两侧完整。
  const initialized = matched.filter(
    (player) => decoderNamesFor(player).length > 0 || platformValuesFor(player).length > 0,
  );
  const decoderNames = [...new Set(initialized.flatMap(decoderNamesFor).filter(Boolean))];
  const platformValues = initialized.flatMap(platformValuesFor);
  const verified =
    available &&
    matched.length > 0 &&
    initialized.length > 0 &&
    initialized.every(
      (player) =>
        decoderNamesFor(player).length > 0 &&
        decoderNamesFor(player).every((name) => name === "VideoToolboxVideoDecoder") &&
        platformValuesFor(player).length > 0 &&
        platformValuesFor(player).every((value) => value === true),
    );
  let reason = null;
  if (!available) reason = "CDP Media domain unavailable or detached";
  else if (matched.length === 0) reason = "WebCodecs VideoDecoder player not observed";
  else if (!verified) reason = "WebCodecs player did not publish platform VideoToolbox evidence";
  return {
    status: verified ? "verified" : "unverified",
    matchedPlayerCount: matched.length,
    initializedPlayerCount: initialized.length,
    decoderName: decoderNames.length === 1 ? decoderNames[0] : null,
    platformDecoder:
      initialized.length > 0 &&
      initialized.every(
        (player) =>
          platformValuesFor(player).length > 0 && platformValuesFor(player).every(Boolean),
      )
        ? true
        : platformValues.some((value) => value === false)
          ? false
          : null,
    reason,
  };
}

async function installVideoDecoderEvidenceProbe(page, host = {}) {
  const players = new Map();
  let available = true;
  let session = null;
  const player = (playerId) => {
    if (!players.has(playerId)) {
      players.set(playerId, { loadUrls: [], decoderNames: [], platformDecoderValues: [] });
    }
    return players.get(playerId);
  };
  try {
    session = await page.context().newCDPSession(page);
    session.on("close", () => {
      available = false;
    });
    session.on("Media.playerEventsAdded", ({ playerId, events = [] }) => {
      const entry = player(playerId);
      for (const event of events) {
        try {
          const value = JSON.parse(event.value);
          if (value?.event === "kLoad" && typeof value.url === "string") {
            entry.loadUrls.push(value.url);
          }
        } catch {
          // 非 JSON 的媒体日志不是 decoder 身份证据。
        }
      }
    });
    session.on("Media.playerPropertiesChanged", ({ playerId, properties = [] }) => {
      const entry = player(playerId);
      for (const property of properties) {
        if (property.name === "kVideoDecoderName") entry.decoderNames.push(property.value);
        if (property.name === "kIsPlatformVideoDecoder") {
          entry.platformDecoderValues.push(property.value === true || property.value === "true");
        }
      }
    });
    await session.send("Media.enable");
  } catch {
    available = false;
  }
  return {
    snapshot: () => classifyVideoDecoderEvidence([...players.values()], { ...host, available }),
    close: async () => {
      if (!session) return;
      try {
        await session.detach();
      } catch {
        // context 关闭时 session 可能已 detach。
      }
    },
  };
}

async function seekFrame(page, targetFrame, maxFrame) {
  await page.evaluate(
    ({ selector, target, frameMax }) => {
      const slider = document.querySelector(selector);
      if (!(slider instanceof HTMLInputElement)) throw new Error("video timeline slider missing");
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (!setValue) throw new Error("native input value setter missing");
      const rangeMax = Number(slider.max);
      setValue.call(slider, String(Math.round((target / frameMax) * rangeMax)));
      slider.dispatchEvent(new Event("input", { bubbles: true }));
      slider.dispatchEvent(new Event("change", { bubbles: true }));
    },
    { selector: SLIDER_SELECTOR, target: targetFrame, frameMax: maxFrame },
  );
}

function preciseRequest(url) {
  const pathname = new URL(url).pathname;
  return (
    /^\/api\/v1\/tasks\/[^/]+\/video\/manifest-v2$/.test(pathname) ||
    /^\/api\/v1\/videos\/[^/]+\/chunks\/\d+(?:\/samples)?$/.test(pathname)
  );
}

async function installProbe(context) {
  await context.addInitScript(() => {
    const probe = {
      longTasks: [],
      longTaskSupported: false,
    };
    Object.defineProperty(window, "__preciseFrameBenchProbe", {
      configurable: false,
      enumerable: false,
      writable: false,
      value: probe,
    });
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          probe.longTasks.push({
            startTime: entry.startTime,
            duration: entry.duration,
          });
        }
      });
      observer.observe({ type: "longtask", buffered: true });
      probe.longTaskSupported = true;
    } catch {
      // 浏览器未开放 Long Tasks API；runner 会把该行标为 inconclusive。
    }
  });
}

async function readPreciseSnapshot(page) {
  return page.evaluate((selector) => {
    const stage = document.querySelector(selector);
    const store = window.__videoWorkbenchDiagnostics;
    const task = store?.activeTaskId ? store.byTask?.[store.activeTaskId] : null;
    return {
      source: stage?.getAttribute("data-video-frame-source") ?? null,
      state: stage?.getAttribute("data-video-precise-state") ?? null,
      frameIndex: Number(stage?.getAttribute("data-video-frame-index") ?? -1),
      paintedFrameIndex: Number(stage?.getAttribute("data-video-painted-frame-index") ?? -1),
      precise: task?.preciseFrame ?? null,
    };
  }, STAGE_SELECTOR);
}

async function waitForFrameTerminal(page, targetFrame) {
  try {
    await page.waitForFunction(
      ({ selector, target }) => {
        const stage = document.querySelector(selector);
        if (!stage || Number(stage.getAttribute("data-video-frame-index")) !== target) return false;
        const source = stage.getAttribute("data-video-frame-source");
        const state = stage.getAttribute("data-video-precise-state");
        const paintedFrame = Number(stage.getAttribute("data-video-painted-frame-index") ?? -1);
        const store = window.__videoWorkbenchDiagnostics;
        const task = store?.activeTaskId ? store.byTask?.[store.activeTaskId] : null;
        return (
          (source === "webcodecs" &&
            state === "ready" &&
            paintedFrame === target &&
            task?.preciseFrame?.frameIndex === target) ||
          state === "fallback"
        );
      },
      { selector: STAGE_SELECTOR, target: targetFrame },
      { timeout: 20_000 },
    );
  } catch (error) {
    const snapshot = await readPreciseSnapshot(page);
    throw new Error(
      `precise frame ${targetFrame} did not reach a terminal state: ${JSON.stringify(snapshot)}`,
      { cause: error },
    );
  }
}

async function waitForFrameResourcesSettled(page) {
  // Konva 已完成目标帧 draw 时，supplemental/prefetch bitmap 的异步 close 可能尚未发布到诊断。
  // 延迟统计已经截断；这里先跨两个常规帧窗口，再读取资源账本，避免 0 → 1 的发布竞态。
  await page.waitForTimeout(32);
  await page.waitForFunction(
    () => {
      const store = window.__videoWorkbenchDiagnostics;
      const task = store?.activeTaskId ? store.byTask?.[store.activeTaskId] : null;
      return task?.preciseFrame?.counters?.liveVideoFrames === 0;
    },
    undefined,
    { timeout: 5_000 },
  );
}

async function probeWindow(page, startTime, endTime) {
  return page.evaluate(
    ({ start, end }) => {
      const probe = window.__preciseFrameBenchProbe;
      if (!probe) return { longTasks: null };
      const overlaps = (entry) => entry.startTime < end && entry.startTime + entry.duration > start;
      const longTasks = probe.longTaskSupported ? probe.longTasks.filter(overlaps) : null;
      probe.longTasks = probe.longTasks.filter((entry) => entry.startTime + entry.duration > end);
      return {
        longTasks,
      };
    },
    { start: startTime, end: endTime },
  );
}

async function measureSeek(page, targetFrame, maxFrame, scenario) {
  const startTime = await page.evaluate(() => performance.now());
  const startedAt = performance.now();
  await seekFrame(page, targetFrame, maxFrame);
  await waitForFrameTerminal(page, targetFrame);
  const latencyMs = performance.now() - startedAt;
  // 延迟在真实 Konva draw 时截断；资源快照另等后台 supplemental/prefetch frame close，
  // 避免把允许存在的短时转换误报成“操作结束仍泄漏 VideoFrame”。
  await waitForFrameResourcesSettled(page);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
  const endTime = await page.evaluate(() => performance.now());
  const [windowProbe, snapshot] = await Promise.all([
    probeWindow(page, startTime, endTime),
    readPreciseSnapshot(page),
  ]);
  return {
    scenario,
    targetFrame,
    latencyMs,
    source: snapshot.source,
    state: snapshot.state,
    fallbackReason: snapshot.precise?.fallbackReason ?? null,
    // buildGopPlan 覆盖 sample 校验、GOP 规划、description 解码和 EncodedVideoChunk 构造，
    // 是可迁移到 Worker 的具名同步 slice；不能用整段操作的 event-loop delay 冒充。
    pipelineBlockingMs: snapshot.precise?.lastDemuxMs ?? null,
    queueMs: snapshot.precise?.lastQueueMs ?? null,
    codecMs: snapshot.precise?.lastCodecMs ?? null,
    decodeMs: snapshot.precise?.lastDecodeMs ?? null,
    bitmapMs: snapshot.precise?.lastBitmapMs ?? null,
    decodeMode: snapshot.precise?.lastDecodeMode ?? null,
    paintMs: snapshot.precise?.lastPaintMs ?? null,
    visibleMs: snapshot.precise?.lastVisibleMs ?? null,
    paintedFrameIndex: snapshot.paintedFrameIndex,
    longTasks: windowProbe.longTasks,
    diagnostics: snapshot.precise,
  };
}

async function measureSeekSeries(page, maxFrame, scenario, targets) {
  const observations = [];
  for (const target of targets) {
    const observation = await measureSeek(page, target, maxFrame, scenario);
    observations.push(observation);
    if (observation.source !== "webcodecs" || observation.state !== "ready") break;
  }
  return observations;
}

async function measureRapidScrub(page, targets, maxFrame) {
  const targetFrame = targets[targets.length - 1];
  const sliderMax = Number(await page.locator(SLIDER_SELECTOR).getAttribute("max"));
  await page.evaluate(
    async ({ selector, values, frameMax, rangeMax }) => {
      const slider = document.querySelector(selector);
      if (!(slider instanceof HTMLInputElement)) throw new Error("video timeline slider missing");
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (!setValue) throw new Error("native input value setter missing");
      for (const value of values) {
        setValue.call(slider, String(Math.round((value / frameMax) * rangeMax)));
        slider.dispatchEvent(new Event("input", { bubbles: true }));
        slider.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    },
    { selector: SLIDER_SELECTOR, values: targets, frameMax: maxFrame, rangeMax: sliderMax },
  );
  await waitForFrameTerminal(page, targetFrame);
  const snapshot = await readPreciseSnapshot(page);
  return {
    scenario: "rapid-scrub",
    targetFrame,
    finalFrame: snapshot.frameIndex,
    source: snapshot.source,
    state: snapshot.state,
    staleFrameActivations:
      snapshot.source === "webcodecs" &&
      snapshot.state === "ready" &&
      snapshot.frameIndex === targetFrame &&
      snapshot.precise?.frameIndex === targetFrame
        ? 0
        : 1,
    diagnostics: snapshot.precise,
  };
}

async function waitForNetworkPayload(read, timeoutMs = 5_000) {
  const startedAt = performance.now();
  while (performance.now() - startedAt < timeoutMs) {
    const value = read();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return null;
}

async function settleMemory(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 50))),
      ),
  );
  return memorySnapshot(page);
}

async function memorySnapshot(page) {
  return page.evaluate(async () => {
    const store = window.__videoWorkbenchDiagnostics;
    const task = store?.activeTaskId ? store.byTask?.[store.activeTaskId] : null;
    let userAgentBytes = null;
    const measure = performance.measureUserAgentSpecificMemory;
    if (typeof measure === "function") {
      try {
        userAgentBytes = (await measure.call(performance)).bytes;
      } catch {
        userAgentBytes = null;
      }
    }
    return {
      userAgentBytes,
      bitmapBytes: task?.preciseFrame?.cache?.bitmapBytes ?? null,
      bitmapBudgetBytes: task?.preciseFrame?.cache?.bitmapBudgetBytes ?? null,
      chunkBytes: task?.preciseFrame?.cache?.chunkBytes ?? null,
      chunkBudgetBytes: task?.preciseFrame?.cache?.chunkBudgetBytes ?? null,
      activeDecoders: task?.preciseFrame?.counters?.activeDecoders ?? null,
      liveVideoFrames: task?.preciseFrame?.counters?.liveVideoFrames ?? null,
    };
  });
}

async function taskEnvironment(page) {
  return page.evaluate(() => {
    const video = document.querySelector('[data-testid="video-konva-source"]');
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl");
    const debug = gl?.getExtension("WEBGL_debug_renderer_info");
    const gpuAdapter = gl && debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null;
    return {
      userAgent: navigator.userAgent,
      hardwareConcurrency: navigator.hardwareConcurrency ?? null,
      deviceMemoryGiB: navigator.deviceMemory ?? null,
      gpuAdapter,
      hardwareAcceleration:
        typeof gpuAdapter === "string" ? !/(swiftshader|software)/i.test(gpuAdapter) : null,
      width: video?.videoWidth || null,
      height: video?.videoHeight || null,
      durationSeconds: Number.isFinite(video?.duration) ? video.duration : null,
    };
  });
}

async function waitForPreciseRequestsToSettle(requestUrls, quietMs = 250, timeoutMs = 5_000) {
  const startedAt = performance.now();
  let lastCount = requestUrls.length;
  let quietStartedAt = startedAt;
  while (performance.now() - startedAt < timeoutMs) {
    await new Promise((resolve) => setTimeout(resolve, 50));
    if (requestUrls.length !== lastCount) {
      lastCount = requestUrls.length;
      quietStartedAt = performance.now();
    }
    if (performance.now() - quietStartedAt >= quietMs) return;
  }
  throw new Error("precise request stream did not settle before playback measurement");
}

async function playbackObservation(page, seconds, requestUrls, scenario) {
  // 前一个暂停态 seek 可能仍有已发起的同 GOP 预取；先等请求流静默，再把计数边界放到
  // play click 之前，避免把先前操作的尾请求误算成“播放态新增请求”。
  await waitForPreciseRequestsToSettle(requestUrls);
  const beforeRequests = requestUrls.length;
  const mediaBefore = await page.locator(VIDEO_SELECTOR).evaluate((video) => ({
    currentTime: video.currentTime,
    ended: video.ended,
  }));
  await page.locator(PLAY_SELECTOR).click();
  const raf = await page.evaluate(
    (durationMs) =>
      new Promise((resolve) => {
        const startedAt = performance.now();
        let frames = 0;
        const tick = (now) => {
          frames += 1;
          if (now - startedAt >= durationMs) {
            resolve({ frames, durationMs: now - startedAt });
            return;
          }
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }),
    seconds * 1000,
  );
  const preciseRequests = requestUrls.length - beforeRequests;
  const mediaAfter = await page.locator(VIDEO_SELECTOR).evaluate((video) => ({
    currentTime: video.currentTime,
    ended: video.ended,
    paused: video.paused,
  }));
  if (!mediaAfter.paused) await page.locator(PLAY_SELECTOR).click();
  const snapshot = await readPreciseSnapshot(page);
  return {
    scenario,
    requestedSeconds: seconds,
    observedSeconds: raf.durationMs / 1000,
    rafFps: raf.frames / (raf.durationMs / 1000),
    mediaTimeDeltaSeconds: Math.max(0, mediaAfter.currentTime - mediaBefore.currentTime),
    mediaEnded: mediaAfter.ended,
    preciseRequests,
    sourceAfterPause: snapshot.source,
  };
}

async function interactionRafObservation(page, targets, maxFrame, preciseEnabled) {
  await page.evaluate(() => {
    const probe = { active: true, frames: 0, startedAt: performance.now() };
    window.__preciseFrameInteractionRaf = probe;
    const tick = () => {
      if (!probe.active) return;
      probe.frames += 1;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  for (const target of targets) {
    await seekFrame(page, target, maxFrame);
    if (preciseEnabled) {
      await waitForFrameTerminal(page, target);
    } else {
      await page.waitForFunction(
        ({ selector, frame }) =>
          Number(document.querySelector(selector)?.getAttribute("data-video-frame-index")) ===
          frame,
        { selector: STAGE_SELECTOR, frame: target },
        { timeout: 10_000 },
      );
    }
    await page.waitForTimeout(16);
  }
  return page.evaluate(() => {
    const probe = window.__preciseFrameInteractionRaf;
    if (!probe) return null;
    probe.active = false;
    const durationMs = performance.now() - probe.startedAt;
    return {
      frames: probe.frames,
      durationMs,
      rafFps: durationMs > 0 ? probe.frames / (durationMs / 1000) : null,
    };
  });
}

function summarizeRow(row, config) {
  const seekObservations = row.observations.filter((item) => "latencyMs" in item);
  const sameGop = seekObservations.filter((item) => item.scenario === "warm-same-gop-seek");
  const sameChunk = seekObservations.filter(
    (item) => item.scenario === "warm-same-chunk-random-seek",
  );
  const warmSameGopSeekP95Ms = percentile(
    sameGop.map((item) => item.latencyMs),
    0.95,
  );
  const warmSameChunkRandomSeekP95Ms = percentile(
    sameChunk.map((item) => item.latencyMs),
    0.95,
  );
  const latencyWithinBudget =
    typeof warmSameGopSeekP95Ms === "number" &&
    warmSameGopSeekP95Ms <= config.budgets.warmSameGopSeekP95Ms &&
    typeof warmSameChunkRandomSeekP95Ms === "number" &&
    warmSameChunkRandomSeekP95Ms <= config.budgets.warmSameChunkRandomSeekP95Ms;
  const pipelineBlockingValues = seekObservations
    .map((item) => item.pipelineBlockingMs)
    .filter((value) => typeof value === "number");
  const phaseP95 = (field) =>
    percentile(
      seekObservations.map((item) => item[field]).filter((value) => typeof value === "number"),
      0.95,
    );
  const fallbackCount = seekObservations.filter(
    (item) => item.source !== "webcodecs" || item.state !== "ready",
  ).length;
  const attributedLongTaskGte50Ms = seekObservations.filter(
    (item) =>
      item.pipelineBlockingMs >= 50 && item.longTasks?.some((entry) => entry.duration >= 50),
  ).length;
  const activeDecoderValues = seekObservations
    .map((item) => item.diagnostics?.counters?.activeDecoders)
    .filter((value) => typeof value === "number");
  const liveFrameValues = seekObservations
    .map((item) => item.diagnostics?.counters?.liveVideoFrames)
    .filter((value) => typeof value === "number");
  const crossGopStarts = new Set(
    seekObservations
      .filter((item) => item.scenario === "cross-gop-roundtrip")
      .map((item) => item.diagnostics?.gopStartDecodeIndex)
      .filter((value) => typeof value === "number"),
  );
  const sameGopStarts = new Set(
    sameGop
      .map((item) => item.diagnostics?.gopStartDecodeIndex)
      .filter((value) => typeof value === "number"),
  );
  const ledgerBytes = (snapshot) =>
    typeof snapshot?.bitmapBytes === "number" && typeof snapshot?.chunkBytes === "number"
      ? snapshot.bitmapBytes + snapshot.chunkBytes
      : null;
  const plateauStartBytes = ledgerBytes(row.memoryPlateauStart);
  const finalLedgerBytes = ledgerBytes(row.memoryAfter);
  const ledgerGrowthBytes =
    plateauStartBytes === null || finalLedgerBytes === null
      ? null
      : Math.max(0, finalLedgerBytes - plateauStartBytes);
  const resourceSnapshots = [
    ...seekObservations.map((item) => ({
      activeDecoders: item.diagnostics?.counters?.activeDecoders,
      liveVideoFrames: item.diagnostics?.counters?.liveVideoFrames,
      bitmapBytes: item.diagnostics?.cache?.bitmapBytes,
      bitmapBudgetBytes: item.diagnostics?.cache?.bitmapBudgetBytes,
      chunkBytes: item.diagnostics?.cache?.chunkBytes,
      chunkBudgetBytes: item.diagnostics?.cache?.chunkBudgetBytes,
    })),
    row.memoryPlateauStart,
    row.memoryAfter,
  ];
  const resourcesWithinBudget = resourceSnapshots.every(
    (snapshot) =>
      typeof snapshot?.activeDecoders === "number" &&
      snapshot.activeDecoders <= config.budgets.activeDecodersMax &&
      typeof snapshot?.liveVideoFrames === "number" &&
      snapshot.liveVideoFrames === config.budgets.liveVideoFramesAfterOps &&
      typeof snapshot?.bitmapBytes === "number" &&
      typeof snapshot?.bitmapBudgetBytes === "number" &&
      snapshot.bitmapBytes <= snapshot.bitmapBudgetBytes &&
      typeof snapshot?.chunkBytes === "number" &&
      typeof snapshot?.chunkBudgetBytes === "number" &&
      snapshot.chunkBytes <= snapshot.chunkBudgetBytes,
  );
  const expectedSamples =
    config.samples.warmSameGop +
    config.samples.warmSameChunkRandom +
    config.samples.crossGopRoundtrip +
    config.samples.stabilityOperations;
  const playbackCompleted = (playback) =>
    playback?.requestedSeconds >= config.samples.continuousPlaybackSeconds &&
    playback?.observedSeconds >= config.samples.continuousPlaybackSeconds &&
    playback?.mediaTimeDeltaSeconds >= config.samples.continuousPlaybackSeconds - 1 &&
    playback?.mediaEnded === false;
  const decoderEvidenceCompleted =
    row.videoDecoderEvidence?.required !== true || row.videoDecoderEvidence.status === "verified";
  const completed =
    row.capability === "ready" &&
    fallbackCount === 0 &&
    seekObservations.every(
      (item) => item.longTasks !== null && typeof item.pipelineBlockingMs === "number",
    ) &&
    seekObservations.length >= expectedSamples &&
    sameGop.length >= config.samples.warmSameGop &&
    sameChunk.length >= config.samples.warmSameChunkRandom &&
    sameGopStarts.size === 1 &&
    crossGopStarts.size >= 2 &&
    decoderEvidenceCompleted &&
    row.playback?.preciseRequests === 0 &&
    playbackCompleted(row.playback) &&
    row.playback?.rafFps >= config.budgets.continuousPlaybackRafFpsMin &&
    playbackCompleted(row.nativeBaselinePlayback) &&
    typeof row.interactionRaf?.rafFps === "number" &&
    typeof row.nativeBaselineInteractionRaf?.rafFps === "number" &&
    row.flagOffPreciseRequests === config.budgets.flagOffPreciseRequests &&
    row.rapidScrub?.staleFrameActivations === config.budgets.staleFrameActivations &&
    latencyWithinBudget &&
    resourcesWithinBudget &&
    ledgerGrowthBytes !== null &&
    ledgerGrowthBytes <= config.budgets.budgetEvictionMemoryGrowth;
  const interactionRafRatio =
    typeof row.interactionRaf?.rafFps === "number" &&
    typeof row.nativeBaselineInteractionRaf?.rafFps === "number" &&
    row.nativeBaselineInteractionRaf.rafFps > 0
      ? row.interactionRaf.rafFps / row.nativeBaselineInteractionRaf.rafFps
      : null;
  return {
    status: completed ? "completed" : "inconclusive",
    samples: seekObservations.length,
    warmSameGopSeekP95Ms,
    warmSameChunkRandomSeekP95Ms,
    queueP95Ms: phaseP95("queueMs"),
    codecP95Ms: phaseP95("codecMs"),
    decodeP95Ms: phaseP95("decodeMs"),
    bitmapP95Ms: phaseP95("bitmapMs"),
    paintP95Ms: phaseP95("paintMs"),
    visibleP95Ms: phaseP95("visibleMs"),
    latencyWithinBudget,
    pipelineBlockingP95Ms: percentile(pipelineBlockingValues, 0.95),
    attributedLongTaskGte50Ms,
    fallbackCount,
    activeDecodersMax: activeDecoderValues.length ? Math.max(...activeDecoderValues) : null,
    liveVideoFramesAfterOpsMax: liveFrameValues.length ? Math.max(...liveFrameValues) : null,
    ledgerGrowthBytes,
    resourcesWithinBudget,
    staleFrameActivations: row.rapidScrub?.staleFrameActivations ?? null,
    playbackPreciseRequests: row.playback?.preciseRequests ?? null,
    playbackMediaTimeDeltaSeconds: row.playback?.mediaTimeDeltaSeconds ?? null,
    playbackEnded: row.playback?.mediaEnded ?? null,
    flagOffPreciseRequests: row.flagOffPreciseRequests ?? null,
    videoDecoderEvidenceStatus: row.videoDecoderEvidence?.status ?? null,
    interactionRafRatio,
  };
}

function workerDecision(rows, budgets) {
  if (
    rows.length === 0 ||
    rows.some(
      (row) =>
        row.summary.status !== "completed" ||
        typeof row.summary.pipelineBlockingP95Ms !== "number" ||
        typeof row.summary.attributedLongTaskGte50Ms !== "number" ||
        typeof row.summary.interactionRafRatio !== "number",
    )
  ) {
    return {
      status: "inconclusive",
      triggered: null,
      rationale:
        "资格矩阵未全部完成或未获得真实 WebCodecs 输出；不能据此决定是否引入 Dedicated Worker。",
    };
  }
  const blockingTriggered = rows.some(
    (row) =>
      row.summary.pipelineBlockingP95Ms === null ||
      row.summary.pipelineBlockingP95Ms > budgets.pipelineBlockingP95Ms,
  );
  const longTaskTriggered = rows.some((row) => row.summary.attributedLongTaskGte50Ms > 0);
  const frameRateTriggered = rows.some(
    (row) => row.summary.interactionRafRatio < budgets.interactionRafRatioMin,
  );
  const triggered = blockingTriggered || longTaskTriggered || frameRateTriggered;
  return {
    status: triggered ? "triggered" : "not-triggered",
    triggered,
    rationale: triggered
      ? "真实矩阵观测达到 Worker 触发门；需实现 Worker 后重跑全部资格门。"
      : "全部真实矩阵完成，未出现超门限主线程 blocking、long task 或播放帧率下降。",
  };
}

async function measureRow(browser, browserGpuEnvironment, resolution, config, args) {
  console.log(`precise-frame ${resolution.id}: start`);
  const rawTaskUrl = args.taskUrls?.[resolution.taskUrlEnv] ?? process.env[resolution.taskUrlEnv];
  if (!rawTaskUrl) {
    return {
      resolutionId: resolution.id,
      taskUrlEnv: resolution.taskUrlEnv,
      capability: "not-run",
      reason: `missing ${resolution.taskUrlEnv}`,
      observations: [],
      summary: { status: "inconclusive" },
    };
  }
  const context = await browser.newContext({
    storageState: args.storageState || undefined,
    viewport: { width: 1440, height: 1000 },
  });
  await installProbe(context);
  const page = await context.newPage();
  const decoderEvidenceProbe = await installVideoDecoderEvidenceProbe(page);
  const preciseRequestUrls = [];
  const payloads = { manifest: null, samples: null };
  page.on("request", (request) => {
    if (preciseRequest(request.url())) preciseRequestUrls.push(request.url());
  });
  page.on("response", async (response) => {
    if (!response.ok()) return;
    const pathname = new URL(response.url()).pathname;
    try {
      if (/^\/api\/v1\/tasks\/[^/]+\/video\/manifest-v2$/.test(pathname)) {
        payloads.manifest = await response.json();
      } else if (/^\/api\/v1\/videos\/[^/]+\/chunks\/\d+\/samples$/.test(pathname)) {
        payloads.samples ??= await response.json();
      }
    } catch {
      // body 不可解析会让 fixture 合同标为 inconclusive。
    }
  });
  const taskUrl = taskUrlWithWebCodecs(absoluteTaskUrl(args.baseUrl, rawTaskUrl), true);
  const startedAt = performance.now();
  await page.goto(taskUrl, { waitUntil: "domcontentloaded" });
  await page.locator(STAGE_SELECTOR).waitFor({ state: "visible", timeout: 30_000 });
  await page.locator(SLIDER_SELECTOR).waitFor({ state: "attached", timeout: 10_000 });
  await waitForFrameTerminal(page, 0);
  const coldReadyMs = performance.now() - startedAt;
  const initial = await readPreciseSnapshot(page);
  const environment = {
    ...(await taskEnvironment(page)),
    ...browserGpuEnvironment,
  };
  const manifestPayload = await waitForNetworkPayload(() => payloads.manifest);
  const samplesPayload = await waitForNetworkPayload(() => payloads.samples);
  const maxFrame = Number(manifestPayload?.frame_count) - 1;
  const sampleEntries = Array.isArray(samplesPayload?.samples) ? samplesPayload.samples : [];
  const keyframes = sampleEntries
    .filter((sample) => sample.is_keyframe && Number.isInteger(sample.frame_index))
    .map((sample) => sample.frame_index)
    .sort((a, b) => a - b);
  const actualFps = Number(manifestPayload?.fps);
  const row = {
    resolutionId: resolution.id,
    taskUrlEnv: resolution.taskUrlEnv,
    taskUrl,
    environment,
    maxFrame,
    fixture: {
      fps: Number.isFinite(actualFps) ? actualFps : null,
      codec: samplesPayload?.codec_string ?? initial.precise?.codec ?? null,
      chunkSizeFrames: manifestPayload?.chunk_size_frames ?? null,
      firstChunkId: samplesPayload?.chunk_id ?? null,
      keyframes,
    },
    capability: initial.source === "webcodecs" && initial.state === "ready" ? "ready" : "fallback",
    initial,
    coldReadyMs,
    observations: [],
    memoryBefore: await memorySnapshot(page),
    memoryPlateauStart: null,
    playback: null,
    nativeBaselinePlayback: null,
    interactionRaf: null,
    nativeBaselineInteractionRaf: null,
    rapidScrub: null,
    flagOffPreciseRequests: null,
    memoryAfter: null,
    videoDecoderEvidence: {
      required: args.strict && process.platform === "darwin",
      status: "pending",
    },
  };
  const fpsMatches =
    Number.isFinite(actualFps) &&
    Math.abs(actualFps - resolution.fps) <= Math.max(0.5, resolution.fps * 0.01);
  const codecMatches = typeof row.fixture.codec === "string" && /^avc1\./i.test(row.fixture.codec);
  const chunkContractValid =
    Number.isInteger(row.fixture.chunkSizeFrames) &&
    row.fixture.chunkSizeFrames > 0 &&
    row.fixture.firstChunkId === 0 &&
    sampleEntries.length > 0 &&
    samplesPayload?.width === resolution.width &&
    samplesPayload?.height === resolution.height &&
    sampleEntries.every(
      (sample) =>
        Number.isInteger(sample.frame_index) &&
        sample.frame_index >= 0 &&
        sample.frame_index < row.fixture.chunkSizeFrames,
    ) &&
    keyframes.length >= 2;
  const softwareDiagnosticOverride =
    !args.strict && process.env.VIDEO_BENCH_ALLOW_SOFTWARE_DECODE === "1";
  environment.softwareDiagnosticOverride = softwareDiagnosticOverride;
  if (
    process.platform !== "darwin" &&
    environment.hardwareVideoDecode !== true &&
    !softwareDiagnosticOverride
  ) {
    row.capability = "hardware-video-decode-unavailable";
    row.reason =
      environment.hardwareVideoDecode === false
        ? "GPU compositing is available, but Chromium reports no hardware video decode profiles"
        : "Chromium hardware video decode capability could not be verified";
  } else if (
    environment.width !== resolution.width ||
    environment.height !== resolution.height ||
    maxFrame < 32 ||
    !fpsMatches ||
    !codecMatches ||
    !chunkContractValid
  ) {
    row.capability = "fixture-mismatch";
    row.reason =
      `expected ${resolution.width}x${resolution.height}@${resolution.fps} H.264, >=33 frames, ` +
      "positive chunk size and at least two keyframes in the sampled chunk";
  } else if (row.capability === "ready") {
    const firstGopFrames = sampleEntries
      .map((sample) => sample.frame_index)
      .filter((frame) => frame >= keyframes[0] && frame < keyframes[1])
      .sort((a, b) => a - b);
    const sameChunkFrames = sampleEntries
      .map((sample) => sample.frame_index)
      .filter((frame) => Number.isInteger(frame) && frame >= keyframes[0])
      .sort((a, b) => a - b);
    if (firstGopFrames.length === 0 || sameChunkFrames.length === 0) {
      row.capability = "fixture-mismatch";
      row.reason = "sampled chunk does not contain measurable GOP targets";
    } else {
      const sameGopTargets = Array.from(
        { length: config.samples.warmSameGop },
        (_, index) => firstGopFrames[index % firstGopFrames.length],
      );
      const randomTargets = seededSampleTargets(
        sameChunkFrames,
        config.samples.warmSameChunkRandom,
      );
      const crossTargets = Array.from({ length: config.samples.crossGopRoundtrip }, (_, index) =>
        index % 2 === 0 ? firstGopFrames[0] : keyframes[1],
      );
      // 互质步长跨全视频制造 GOP/chunk 切换和预算淘汰，避免固定两帧缓存命中假长稳。
      const stabilitySpan = Math.max(2, maxFrame);
      const stabilityTargets = Array.from(
        { length: args.stabilityOperations },
        (_, index) => ((index * (stabilitySpan - 1)) % stabilitySpan) + 1,
      );
      const stabilitySplit = Math.ceil(stabilityTargets.length / 2);
      const interactionTargets = seededSampleTargets(sameChunkFrames, 30, 0x23_15_1a_f0);
      row.interactionRaf = await interactionRafObservation(
        page,
        interactionTargets,
        maxFrame,
        true,
      );
      console.log(`precise-frame ${resolution.id}: interaction probe complete`);
      row.rapidScrub = await measureRapidScrub(
        page,
        Array.from(
          { length: 12 },
          (_, index) => ((index * (stabilitySpan - 1)) % stabilitySpan) + 1,
        ),
        maxFrame,
      );
      // cold-first-frame 已由页面首开记录；warm same-GOP 正式采样前先把目标 GOP/session
      // 预热一次，避免把跨场景切换成本混进 warm 指标。
      await seekFrame(page, firstGopFrames[0], maxFrame);
      await waitForFrameTerminal(page, firstGopFrames[0]);
      await waitForFrameResourcesSettled(page);
      row.observations.push(
        ...(await measureSeekSeries(page, maxFrame, "warm-same-gop-seek", sameGopTargets)),
        ...(await measureSeekSeries(page, maxFrame, "warm-same-chunk-random-seek", randomTargets)),
        ...(await measureSeekSeries(page, maxFrame, "cross-gop-roundtrip", crossTargets)),
        ...(await measureSeekSeries(
          page,
          maxFrame,
          "frame-step-stability",
          stabilityTargets.slice(0, stabilitySplit),
        )),
      );
      console.log(`precise-frame ${resolution.id}: first stability plateau complete`);
      row.memoryPlateauStart = await settleMemory(page);
      row.observations.push(
        ...(await measureSeekSeries(
          page,
          maxFrame,
          "frame-step-stability",
          stabilityTargets.slice(stabilitySplit),
        )),
      );
      console.log(
        `precise-frame ${resolution.id}: ${args.stabilityOperations} operations complete`,
      );
      await seekFrame(page, 0, maxFrame);
      await waitForFrameTerminal(page, 0);
      await waitForFrameResourcesSettled(page);
      row.playback = await playbackObservation(
        page,
        args.playbackSeconds,
        preciseRequestUrls,
        "continuous-playback-precise-on",
      );
      row.memoryAfter = await settleMemory(page);

      const flagOffStartRequests = preciseRequestUrls.length;
      await page.goto(taskUrlWithWebCodecs(taskUrl, false), {
        waitUntil: "domcontentloaded",
      });
      await page.locator(STAGE_SELECTOR).waitFor({ state: "visible", timeout: 30_000 });
      await page.locator(PLAY_SELECTOR).waitFor({ state: "visible", timeout: 10_000 });
      row.nativeBaselineInteractionRaf = await interactionRafObservation(
        page,
        interactionTargets,
        maxFrame,
        false,
      );
      await seekFrame(page, 0, maxFrame);
      await page.waitForFunction(
        ({ selector }) =>
          Number(document.querySelector(selector)?.getAttribute("data-video-frame-index")) === 0,
        { selector: STAGE_SELECTOR },
        { timeout: 10_000 },
      );
      row.nativeBaselinePlayback = await playbackObservation(
        page,
        args.playbackSeconds,
        preciseRequestUrls,
        "continuous-playback-native-baseline",
      );
      row.flagOffPreciseRequests = preciseRequestUrls.length - flagOffStartRequests;
    }
  }
  row.memoryAfter ??= await settleMemory(page);
  row.videoDecoderEvidence = {
    required: args.strict && process.platform === "darwin",
    ...decoderEvidenceProbe.snapshot(),
  };
  row.summary = summarizeRow(row, {
    ...config,
    samples: {
      ...config.samples,
      stabilityOperations: config.samples.stabilityOperations,
      continuousPlaybackSeconds: config.samples.continuousPlaybackSeconds,
    },
  });
  await decoderEvidenceProbe.close();
  await context.close();
  return row;
}

export async function runPreciseFrameMeasurements(config, args) {
  const missing = config.resolutions
    .map((resolution) => resolution.taskUrlEnv)
    .filter((envName) => !(args.taskUrls?.[envName] ?? process.env[envName]));
  if (missing.length > 0) {
    throw new Error(
      `precise-frame benchmark requires task URLs: ${missing.join(", ")}. ` +
        "Each task must match the configured resolution/fps and be authenticated by --storage-state.",
    );
  }
  const channel = process.env.VIDEO_BENCH_CHROMIUM_CHANNEL || undefined;
  // 无物理输出口的 GPU runner 会把有头页的 BeginFrame 降到约 1 Hz；关闭 GPU vsync
  // 可恢复 Chrome 的 60 Hz 调度，同时仍保留真实硬件 VideoDecoder / WebGL 适配器。
  const chromiumArgs = args.headed ? ["--disable-gpu-vsync"] : ["--enable-unsafe-swiftshader"];
  const browser = await chromium.launch({
    headless: !args.headed,
    args: chromiumArgs,
    channel,
    env: browserEnvironment(),
  });
  try {
    const browserGpuEnvironment = await readBrowserGpuEnvironment(browser);
    const rows = [];
    for (const resolution of config.resolutions) {
      rows.push(await measureRow(browser, browserGpuEnvironment, resolution, config, args));
    }
    return {
      environment: {
        chromiumVersion: browser.version(),
        chromiumChannel: channel ?? "bundled",
        chromiumArgs,
        headed: args.headed,
        performanceTier: process.env.VIDEO_BENCH_TIER ?? "standard",
        webcodecsFlag: true,
        hostPlatform: process.platform,
        hostArchitecture: process.arch,
        softwareDiagnosticOverride:
          !args.strict && process.env.VIDEO_BENCH_ALLOW_SOFTWARE_DECODE === "1",
        ...browserGpuEnvironment,
      },
      rows,
      workerDecision: workerDecision(rows, config.budgets),
    };
  } finally {
    await browser.close();
  }
}

export const preciseFrameMath = {
  percentile,
  seededSampleTargets,
  sliderValueForFrame,
  classifyVideoDecoderEvidence,
  installVideoDecoderEvidenceProbe,
  summarizeRow,
  workerDecision,
};
