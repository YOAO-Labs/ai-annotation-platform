import { chromium } from "@playwright/test";
import { cpus, platform, release } from "node:os";

const baseUrl = process.env.RASTER_MASK_BENCH_BASE_URL ?? "http://localhost:3000";
const iterations = Number(process.env.RASTER_MASK_BENCH_ITERATIONS ?? 20);
const warmup = Number(process.env.RASTER_MASK_BENCH_WARMUP ?? 2);
const frameSwitches = Number(process.env.RASTER_MASK_BENCH_FRAME_SWITCHES ?? 50);

if (
  ![iterations, frameSwitches].every((value) => Number.isInteger(value) && value > 0) ||
  !Number.isInteger(warmup) ||
  warmup < 0
) {
  throw new Error("benchmark numeric options must be positive integers");
}

const browser = await chromium.launch({
  headless: true,
  args: ["--enable-precise-memory-info", "--js-flags=--expose-gc"],
});
const page = await browser.newPage();
await page.goto(baseUrl, { waitUntil: "domcontentloaded" });

const result = await page.evaluate(
  async ({ iterations, warmup, frameSwitches }) => {
    const moduleUrl = (path) => new URL(path, window.location.origin).href;
    const { encodeCocoRle } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/geometry/maskRle.ts")
    );
    const { applyMaskBrush, applyMaskPolygon } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/geometry/maskOperations.ts")
    );
    const { createTintedRasterMaskImage } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/rasterMaskRender.ts")
    );
    const { MaskBuffer } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/geometry/maskBuffer.ts")
    );
    const { MaskHistoryCheckpoint, MaskHistoryStore } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/maskHistory.ts")
    );
    const { RasterMaskWorkerPool } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/rasterMaskWorkerPool.ts")
    );
    const { SparseMaskTileStore } = await import(
      moduleUrl("/src/pages/Workbench/stage/shared/sparseMaskTileStore.ts")
    );

    const resolutions = [
      { name: "720p", width: 1280, height: 720 },
      { name: "1080p", width: 1920, height: 1080 },
      { name: "4k", width: 3840, height: 2160 },
    ];
    const yieldToBrowser = () => new Promise((resolve) => setTimeout(resolve, 0));
    const summarize = (values) => {
      const sorted = [...values].sort((left, right) => left - right);
      const percentile = (quantile) =>
        sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * quantile))];
      return {
        p50_ms: percentile(0.5),
        p95_ms: percentile(0.95),
        max_ms: Math.max(...values),
      };
    };
    const summarizeLongTasks = (values) => ({
      count: values.length,
      total_ms: values.reduce((sum, value) => sum + value, 0),
      max_ms: values.length ? Math.max(...values) : 0,
    });
    const summarizeBytes = (values) => {
      const sorted = [...values].sort((left, right) => left - right);
      const percentile = (quantile) =>
        sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * quantile))];
      return {
        p50_bytes: percentile(0.5),
        p95_bytes: percentile(0.95),
        max_bytes: Math.max(...values),
      };
    };
    const bytesForJson = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
    const longTaskDurations = [];
    const longTaskObserver =
      typeof PerformanceObserver !== "undefined" &&
      PerformanceObserver.supportedEntryTypes?.includes("longtask")
        ? new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) longTaskDurations.push(entry.duration);
          })
        : null;
    longTaskObserver?.observe({ type: "longtask", buffered: true });

    const resources = {
      workers_created: 0,
      workers_terminated: 0,
      bitmaps_created: 0,
      bitmaps_closed: 0,
      peak_in_flight: 0,
    };
    let inFlight = 0;

    const buildSamStyleAlpha = (width, height, seed = 0) => {
      const alpha = new Uint8Array(width * height);
      const ellipse = (centerX, centerY, radiusX, radiusY, value = 255) => {
        const x0 = Math.max(0, Math.floor(centerX - radiusX));
        const x1 = Math.min(width - 1, Math.ceil(centerX + radiusX));
        const y0 = Math.max(0, Math.floor(centerY - radiusY));
        const y1 = Math.min(height - 1, Math.ceil(centerY + radiusY));
        for (let y = y0; y <= y1; y += 1) {
          const dy = (y - centerY) / radiusY;
          const row = y * width;
          for (let x = x0; x <= x1; x += 1) {
            const dx = (x - centerX) / radiusX;
            if (dx * dx + dy * dy <= 1) alpha[row + x] = value;
          }
        }
      };
      const offsetX = (((seed * 37) % 11) - 5) / 100;
      const offsetY = (((seed * 19) % 9) - 4) / 100;
      ellipse(
        width * (0.5 + offsetX),
        height * (0.5 + offsetY),
        width * (0.12 + (seed % 3) * 0.01),
        height * (0.2 + (seed % 4) * 0.01),
      );
      ellipse(width * (0.48 + offsetX), height * (0.43 + offsetY), width * 0.045, height * 0.07, 0);
      ellipse(width * (0.39 + offsetX), height * (0.66 + offsetY), width * 0.035, height * 0.05);
      return alpha;
    };

    const rleModuleUrl = moduleUrl("/src/pages/Workbench/stage/shared/geometry/maskRle.ts");
    const renderModuleUrl = moduleUrl("/src/pages/Workbench/stage/shared/rasterMaskRender.ts");
    const workerSource = `
    let rleModulePromise;
    let renderModulePromise;
    self.onmessage = async (event) => {
      rleModulePromise ??= import(${JSON.stringify(rleModuleUrl)});
      renderModulePromise ??= import(${JSON.stringify(renderModuleUrl)});
      const [rleModule, renderModule] = await Promise.all([rleModulePromise, renderModulePromise]);
      const decodeStarted = performance.now();
      const [height, width] = event.data.rle.size;
      const alpha = rleModule.decodeCocoRle(event.data.rle);
      const decodeMs = performance.now() - decodeStarted;
      const analyzeStarted = performance.now();
      const analysis = renderModule.analyzeRasterMaskAlpha(alpha, width, height);
      const analyzeMs = performance.now() - analyzeStarted;
      self.postMessage({ analysis, decodeMs, analyzeMs }, [analysis.crop.alpha.buffer]);
    };
  `;
    const workerUrl = URL.createObjectURL(new Blob([workerSource], { type: "text/javascript" }));
    const analyzeWithEphemeralWorker = (rle) =>
      new Promise((resolve, reject) => {
        const worker = new Worker(workerUrl, { type: "module" });
        resources.workers_created += 1;
        inFlight += 1;
        resources.peak_in_flight = Math.max(resources.peak_in_flight, inFlight);
        const started = performance.now();
        const finish = () => {
          worker.terminate();
          resources.workers_terminated += 1;
          inFlight -= 1;
        };
        worker.onmessage = (event) => {
          const workerRoundTripMs = performance.now() - started;
          finish();
          resolve({ ...event.data, workerRoundTripMs });
        };
        worker.onerror = (event) => {
          finish();
          reject(new Error(event.message || "large-canvas benchmark worker failed"));
        };
        worker.postMessage({ rle });
      });

    await yieldToBrowser();
    globalThis.gc?.();
    await yieldToBrowser();
    const heapBefore = performance.memory?.usedJSHeapSize ?? null;
    let peakUsedHeap = heapBefore;
    const pipelineRows = [];
    for (const resolution of resolutions) {
      let alpha = buildSamStyleAlpha(resolution.width, resolution.height);
      let rle = encodeCocoRle(alpha, resolution.width, resolution.height);
      const samples = [];
      let measuredLongTaskStart = longTaskDurations.length;
      for (let index = 0; index < warmup + iterations; index += 1) {
        const pipelineStarted = performance.now();
        const analyzed = await analyzeWithEphemeralWorker(rle);
        const bitmapStarted = performance.now();
        const rendered = await createTintedRasterMaskImage(analyzed.analysis, "#8b5cf6");
        const bitmapMs = performance.now() - bitmapStarted;
        const pipelineMs = performance.now() - pipelineStarted;
        if (!rendered) throw new Error(`${resolution.name} produced an empty bitmap`);
        resources.bitmaps_created += 1;
        if (typeof rendered.image.close === "function") {
          rendered.image.close();
          resources.bitmaps_closed += 1;
        }
        const usedHeap = performance.memory?.usedJSHeapSize ?? null;
        if (usedHeap != null) peakUsedHeap = Math.max(peakUsedHeap ?? 0, usedHeap);
        if (index === warmup - 1) {
          await yieldToBrowser();
          measuredLongTaskStart = longTaskDurations.length;
        }
        if (index >= warmup) {
          samples.push({
            decodeMs: analyzed.decodeMs,
            analyzeMs: analyzed.analyzeMs,
            workerRoundTripMs: analyzed.workerRoundTripMs,
            bitmapMs,
            pipelineMs,
          });
        }
      }
      await yieldToBrowser();
      let crop = (await analyzeWithEphemeralWorker(rle)).analysis.crop;
      pipelineRows.push({
        name: resolution.name,
        resolution: [resolution.width, resolution.height],
        pixels: resolution.width * resolution.height,
        rle_runs: rle.counts.length,
        rle_json_bytes: bytesForJson(rle),
        crop_pixels: crop.width * crop.height,
        retained_cache_bytes_per_mask: crop.width * crop.height * 5 + rle.counts.length * 4,
        estimated_dense_editor_bytes: resolution.width * resolution.height * 9,
        decode: summarize(samples.map((sample) => sample.decodeMs)),
        analyze: summarize(samples.map((sample) => sample.analyzeMs)),
        worker_round_trip: summarize(samples.map((sample) => sample.workerRoundTripMs)),
        bitmap: summarize(samples.map((sample) => sample.bitmapMs)),
        pipeline: summarize(samples.map((sample) => sample.pipelineMs)),
        long_tasks: summarizeLongTasks(longTaskDurations.slice(measuredLongTaskStart)),
      });
      alpha = null;
      rle = null;
      crop = null;
    }

    const cacheRows = [];
    for (const count of [1, 10, 50]) {
      const entries = [];
      const started = performance.now();
      for (let index = 0; index < count; index += 1) {
        let alpha = buildSamStyleAlpha(1920, 1080, index);
        let rle = encodeCocoRle(alpha, 1920, 1080);
        let analyzed = await analyzeWithEphemeralWorker(rle);
        const crop = analyzed.analysis.crop;
        entries.push(crop.width * crop.height * 5 + rle.counts.length * 4);
        alpha = null;
        rle = null;
        analyzed = null;
      }
      cacheRows.push({
        masks: count,
        elapsed_ms: performance.now() - started,
        retained_bytes: entries.reduce((sum, value) => sum + value, 0),
        largest_entry_bytes: Math.max(...entries),
        exceeds_standard_128_mib:
          entries.reduce((sum, value) => sum + value, 0) > 128 * 1024 * 1024,
      });
    }

    const historyRows = [];
    for (const resolution of resolutions.filter((entry) => entry.name !== "720p")) {
      let base = buildSamStyleAlpha(resolution.width, resolution.height);
      const brushSamples = [];
      const lassoSamples = [];
      const historyBytes = [];
      let current = base;
      const historyLongTaskStart = longTaskDurations.length;
      for (let index = 0; index < warmup + iterations; index += 1) {
        const beforeStarted = performance.now();
        const before = encodeCocoRle(current, resolution.width, resolution.height);
        const brushedResult = applyMaskBrush(current, resolution.width, resolution.height, {
          cx: Math.floor(resolution.width * (0.25 + (index % 10) * 0.03)),
          cy: Math.floor(resolution.height * 0.25),
          radius: 24,
          shape: "circle",
          value: index % 2 === 0 ? 255 : 0,
        });
        const brushed = brushedResult.alpha;
        const after = encodeCocoRle(brushed, resolution.width, resolution.height);
        const brushMs = performance.now() - beforeStarted;
        const polygon = [
          [resolution.width * 0.2, resolution.height * 0.7],
          [resolution.width * 0.28, resolution.height * 0.64],
          [resolution.width * 0.34, resolution.height * 0.76],
        ];
        const lassoStarted = performance.now();
        const lassoedResult = applyMaskPolygon(brushed, resolution.width, resolution.height, {
          points: polygon,
          value: 255,
        });
        const lassoMs = performance.now() - lassoStarted;
        if (index >= warmup) {
          brushSamples.push(brushMs);
          lassoSamples.push(lassoMs);
          historyBytes.push(bytesForJson(before) + bytesForJson(after));
        }
        current = lassoedResult.alpha;
        await yieldToBrowser();
      }
      await yieldToBrowser();
      historyRows.push({
        name: resolution.name,
        resolution: [resolution.width, resolution.height],
        current_rle_snapshot_history: {
          command_bytes: summarizeBytes(historyBytes),
          twenty_command_bytes: historyBytes.reduce((sum, value) => sum + value, 0),
        },
        brush_plus_before_after_rle: summarize(brushSamples),
        lasso: summarize(lassoSamples),
        long_tasks: summarizeLongTasks(longTaskDurations.slice(historyLongTaskStart)),
        full_alpha_bytes: resolution.width * resolution.height,
      });
      base = null;
      current = null;
    }

    const frameRles = Array.from({ length: frameSwitches }, (_, index) => {
      const alpha = buildSamStyleAlpha(1280, 720, index);
      return encodeCocoRle(alpha, 1280, 720);
    });
    const switchStarted = performance.now();
    const frameLatencies = [];
    let nextFrame = 0;
    const consumeFrames = async () => {
      while (nextFrame < frameRles.length) {
        const frameIndex = nextFrame;
        nextFrame += 1;
        const started = performance.now();
        await analyzeWithEphemeralWorker(frameRles[frameIndex]);
        frameLatencies.push(performance.now() - started);
      }
    };
    await Promise.all(Array.from({ length: 4 }, () => consumeFrames()));
    const rapidFrameSwitch = {
      frames: frameSwitches,
      concurrency: 4,
      elapsed_ms: performance.now() - switchStarted,
      latency: summarize(frameLatencies),
      stale_commits: 0,
      final_queue: 0,
    };
    frameRles.length = 0;

    const largeCanvasCases = [
      { name: "5k_sparse", width: 5120, height: 2880, density: 0.01 },
      { name: "8k_sparse", width: 8192, height: 8192, density: 0.002 },
      { name: "ultrawide_sparse", width: 7680, height: 2160, density: 0.01 },
      { name: "8k_dense", width: 8192, height: 8192, density: 1 },
      { name: "8k_noise_50_percent", width: 8192, height: 8192, density: 0.5 },
      { name: "8k_checkerboard", width: 8192, height: 8192, density: 0.5 },
    ].map((entry) => {
      let currentCodecError = null;
      try {
        encodeCocoRle({ length: entry.width * entry.height }, entry.width, entry.height);
      } catch (error) {
        currentCodecError = error instanceof Error ? error.message : String(error);
      }
      const pixels = entry.width * entry.height;
      const tileColumns = Math.ceil(entry.width / 512);
      const tileRows = Math.ceil(entry.height / 512);
      const estimatedRuns = entry.name.includes("checkerboard")
        ? pixels
        : entry.name.includes("noise")
          ? Math.floor(pixels / 2)
          : null;
      return {
        ...entry,
        pixels,
        current_codec_error: currentCodecError,
        dense_alpha_bytes: pixels,
        full_rgba_bytes: pixels * 4,
        estimated_dense_editor_bytes: pixels * 9,
        tile_grid: [tileColumns, tileRows],
        tile_count: tileColumns * tileRows,
        estimated_materialized_tiles_for_local_1024_square: 9,
        estimated_runs: estimatedRuns,
        exceeds_run_limit: estimatedRuns == null ? null : estimatedRuns > 1_000_000,
        exceeds_dense_operation_limit: pixels > 16_777_216,
        enters_tiled_mode: entry.width > 4096 || entry.height > 4096 || pixels > 16_777_216,
      };
    });

    await yieldToBrowser();
    globalThis.gc?.();
    await yieldToBrowser();
    const implementationHeapBefore = performance.memory?.usedJSHeapSize ?? null;
    let implementationPeakHeap = implementationHeapBefore;
    const implementationPool = new RasterMaskWorkerPool({ size: 2, queueLimit: 32 });
    const implementationPipelineRows = [];
    let implementationBitmapsCreated = 0;
    let implementationBitmapsClosed = 0;
    for (const resolution of resolutions) {
      let alpha = buildSamStyleAlpha(resolution.width, resolution.height);
      let rle = encodeCocoRle(alpha, resolution.width, resolution.height);
      const samples = [];
      let measuredLongTaskStart = longTaskDurations.length;
      for (let index = 0; index < warmup + iterations; index += 1) {
        const started = performance.now();
        const analysis = await implementationPool.analyze(rle, { priority: "current" });
        const rendered = await createTintedRasterMaskImage(analysis, "#8b5cf6");
        if (!rendered)
          throw new Error(`${resolution.name} implementation pipeline produced an empty bitmap`);
        implementationBitmapsCreated += 1;
        if (typeof rendered.image.close === "function") {
          rendered.image.close();
          implementationBitmapsClosed += 1;
        }
        const elapsed = performance.now() - started;
        const usedHeap = performance.memory?.usedJSHeapSize ?? null;
        if (usedHeap != null)
          implementationPeakHeap = Math.max(implementationPeakHeap ?? 0, usedHeap);
        if (index === warmup - 1) {
          await yieldToBrowser();
          measuredLongTaskStart = longTaskDurations.length;
        }
        if (index >= warmup) samples.push(elapsed);
      }
      implementationPipelineRows.push({
        name: resolution.name,
        resolution: [resolution.width, resolution.height],
        pipeline: summarize(samples),
        long_tasks: summarizeLongTasks(longTaskDurations.slice(measuredLongTaskStart)),
      });
      alpha = null;
      rle = null;
    }

    let denseAlpha = buildSamStyleAlpha(3840, 2160);
    let denseBuffer = new MaskBuffer({ width: 3840, height: 2160 });
    denseBuffer.data.set(denseAlpha);
    denseAlpha = null;
    const denseHistory = new MaskHistoryStore(32 * 1024 * 1024);
    const densePointerSamples = [];
    const densePointerUpSamples = [];
    const denseHistoryLongTaskStart = longTaskDurations.length;
    for (let index = 0; index < warmup + iterations; index += 1) {
      const cx = 640 + (index % 4) * 12;
      const cy = 540;
      const checkpoint = new MaskHistoryCheckpoint(3840, 2160);
      const pointerStarted = performance.now();
      checkpoint.captureDenseRect(denseBuffer.data, {
        x0: cx - 24,
        y0: cy - 24,
        x1: cx + 25,
        y1: cy + 25,
      });
      denseBuffer.brush(cx, cy, 24, index % 2 === 0 ? 255 : 0, "circle");
      const pointerMs = performance.now() - pointerStarted;
      const pointerUpStarted = performance.now();
      const command = checkpoint.finishDense("benchmark-stroke", index, denseBuffer.data);
      if (command) denseHistory.push(command);
      const pointerUpMs = performance.now() - pointerUpStarted;
      if (index >= warmup) {
        densePointerSamples.push(pointerMs);
        densePointerUpSamples.push(pointerUpMs);
      }
      await yieldToBrowser();
    }
    const denseHistoryResult = {
      resolution: [3840, 2160],
      pointer: summarize(densePointerSamples),
      pointer_up: summarize(densePointerUpSamples),
      history: denseHistory.snapshot(),
      long_tasks: summarizeLongTasks(longTaskDurations.slice(denseHistoryLongTaskStart)),
    };
    denseHistory.clear();
    denseBuffer = null;

    const eightKSize = 8192;
    const eightKPixels = eightKSize * eightKSize;
    const eightKBase = {
      encoding: "coco_rle",
      size: [eightKSize, eightKSize],
      counts: [eightKPixels],
    };
    const eightKStore = new SparseMaskTileStore({
      sessionId: "benchmark-8k",
      sha256: "benchmark-8k-blank",
      baseRle: eightKBase,
      backend: implementationPool,
      deviceMemory: 4,
    });
    const eightKHistory = new MaskHistoryStore(32 * 1024 * 1024, 100, {
      onRetain: (command) => eightKStore.retainHistoryCommand(command),
      onRelease: (command) => eightKStore.releaseHistoryCommand(command),
    });
    eightKStore.setViewport({ x: 3840, y: 3840, width: 512, height: 512 });
    await eightKStore.loadViewport();
    const eightKBrushSamples = [];
    const eightKLassoSamples = [];
    const eightKLongTaskStart = longTaskDurations.length;
    for (let index = 0; index < warmup + iterations; index += 1) {
      const checkpoint = eightKStore.beginHistoryCheckpoint();
      const started = performance.now();
      await eightKStore.brush({
        cx: 4096,
        cy: 4096,
        radius: 24,
        value: index % 2 === 0 ? 255 : 0,
        shape: "circle",
        checkpoint,
      });
      const command = eightKStore.finishHistoryCheckpoint(checkpoint, "benchmark-brush", index);
      if (command) eightKHistory.push(command);
      if (index >= warmup) eightKBrushSamples.push(performance.now() - started);
      await yieldToBrowser();
    }
    const lasso = [
      [4000, 4000],
      [4192, 4016],
      [4080, 4192],
    ];
    for (let index = 0; index < warmup + iterations; index += 1) {
      const checkpoint = eightKStore.beginHistoryCheckpoint();
      const started = performance.now();
      await eightKStore.lasso(lasso, index % 2 === 0 ? 255 : 0, { checkpoint });
      const command = eightKStore.finishHistoryCheckpoint(checkpoint, "benchmark-lasso", index);
      if (command) eightKHistory.push(command);
      if (index >= warmup) eightKLassoSamples.push(performance.now() - started);
      await yieldToBrowser();
    }
    const finalCheckpoint = eightKStore.beginHistoryCheckpoint();
    await eightKStore.brush({
      cx: 4160,
      cy: 4160,
      radius: 24,
      value: 255,
      shape: "circle",
      checkpoint: finalCheckpoint,
    });
    const finalCommand = eightKStore.finishHistoryCheckpoint(
      finalCheckpoint,
      "benchmark-final-dirty",
      warmup + iterations,
    );
    if (finalCommand) eightKHistory.push(finalCommand);
    const mergeSamples = [];
    let mergedPixelSum = 0;
    for (let index = 0; index < warmup + iterations; index += 1) {
      const started = performance.now();
      const merged = await eightKStore.merge();
      if (index >= warmup) mergeSamples.push(performance.now() - started);
      mergedPixelSum = merged.counts.reduce((sum, count) => sum + count, 0);
    }
    const eightKResourcesBeforeDispose = eightKStore.snapshot();
    const eightKHistoryResources = eightKHistory.snapshot();
    const implementationPoolBeforeDispose = implementationPool.getSnapshot();
    const eightKResult = {
      resolution: [eightKSize, eightKSize],
      brush: summarize(eightKBrushSamples),
      lasso: summarize(eightKLassoSamples),
      merge: summarize(mergeSamples),
      long_tasks: summarizeLongTasks(longTaskDurations.slice(eightKLongTaskStart)),
      full_alpha_allocated: false,
      merged_pixel_sum: mergedPixelSum,
      resources: eightKResourcesBeforeDispose,
      history: eightKHistoryResources,
    };
    eightKHistory.clear();
    eightKStore.dispose();
    const eightKResourcesAfterDispose = eightKStore.snapshot();
    implementationPool.dispose();
    const implementationPoolAfterDispose = implementationPool.getSnapshot();
    await yieldToBrowser();
    const implementationHeapBeforeGc = performance.memory?.usedJSHeapSize ?? null;
    globalThis.gc?.();
    await yieldToBrowser();
    const implementationHeapAfter = performance.memory?.usedJSHeapSize ?? null;
    const postImplementation = {
      pipeline_rows: implementationPipelineRows,
      dense_4k_history: denseHistoryResult,
      sparse_8k: eightKResult,
      resources: {
        pool_before_dispose: implementationPoolBeforeDispose,
        pool_after_dispose: implementationPoolAfterDispose,
        tile_store_after_dispose: eightKResourcesAfterDispose,
        bitmaps_created: implementationBitmapsCreated,
        bitmaps_closed: implementationBitmapsClosed,
        live_bitmaps: implementationBitmapsCreated - implementationBitmapsClosed,
      },
      heap: {
        before_bytes: implementationHeapBefore,
        before_gc_bytes: implementationHeapBeforeGc,
        after_gc_bytes: implementationHeapAfter,
        gc_delta_bytes:
          implementationHeapBefore == null || implementationHeapAfter == null
            ? null
            : implementationHeapAfter - implementationHeapBefore,
        peak_used_bytes: implementationPeakHeap,
        peak_temporary_bytes:
          implementationHeapBefore == null || implementationPeakHeap == null
            ? null
            : implementationPeakHeap - implementationHeapBefore,
      },
    };

    URL.revokeObjectURL(workerUrl);
    longTaskObserver?.disconnect();
    await yieldToBrowser();
    const heapAfterBeforeGc = performance.memory?.usedJSHeapSize ?? null;
    globalThis.gc?.();
    await yieldToBrowser();
    const heapAfter = performance.memory?.usedJSHeapSize ?? null;
    return {
      iterations,
      warmup,
      pipeline_rows: pipelineRows,
      cache_rows: cacheRows,
      history_rows: historyRows,
      rapid_frame_switch: rapidFrameSwitch,
      large_canvas_cases: largeCanvasCases,
      post_implementation: postImplementation,
      resources: {
        ...resources,
        live_workers: resources.workers_created - resources.workers_terminated,
        live_bitmaps: resources.bitmaps_created - resources.bitmaps_closed,
        final_queue: 0,
        materialized_tiles: 0,
      },
      heap: {
        gc_available: typeof globalThis.gc === "function",
        before_bytes: heapBefore,
        after_before_gc_bytes: heapAfterBeforeGc,
        after_gc_bytes: heapAfter,
        gc_delta_bytes: heapBefore == null || heapAfter == null ? null : heapAfter - heapBefore,
        peak_used_bytes: peakUsedHeap,
        peak_temporary_bytes:
          heapBefore == null || peakUsedHeap == null ? null : peakUsedHeap - heapBefore,
      },
    };
  },
  { iterations, warmup, frameSwitches },
);

const browserVersion = browser.version();
await browser.close();
process.stdout.write(
  `${JSON.stringify(
    {
      generated_at: new Date().toISOString(),
      node: process.version,
      browser: `chromium-${browserVersion}`,
      os: `${platform()}-${release()}`,
      cpu: cpus()[0]?.model ?? "unknown",
      cpu_count: cpus().length,
      base_url: baseUrl,
      ...result,
    },
    null,
    2,
  )}\n`,
);
