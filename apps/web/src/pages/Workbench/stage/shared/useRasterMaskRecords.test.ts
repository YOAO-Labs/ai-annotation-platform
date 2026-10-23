import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnnotationResponse, CocoRleMaskRef } from "@/types";
import { rasterMasksApi } from "@/api/rasterMasks";
import { buildImageRasterMaskDescriptors } from "../../state/imageRasterMaskDescriptors";
import { decodeCocoRle, encodeCocoRle, type CocoRle } from "./geometry/maskRle";
import { analyzeRasterMaskAlpha } from "./rasterMaskRender";
import { RasterMaskWorkerError } from "./rasterMaskWorkerPool";
import { RasterResourceCoordinator } from "./rasterResourceCoordinator";
import {
  estimateCocoRleRetainedBytes,
  rasterMaskDeviceBudget,
  rasterMaskLoadError,
  rasterMaskRecordCacheKey,
  useRasterMaskRecords,
  type RasterMaskRecordDescriptor,
} from "./useRasterMaskRecords";
import { pickTopRasterMaskAt } from "./rasterMaskRender";

class FakeImageBitmap {
  close = vi.fn();
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flushAsync(rounds = 8) {
  await act(async () => {
    for (let index = 0; index < rounds; index += 1) await Promise.resolve();
  });
}

let digestCounter = 0;

function makeDescriptor(
  id: string,
  options: {
    revision?: number;
    selected?: boolean;
    zOrder?: number;
    load?: ReturnType<typeof vi.fn>;
  } = {},
) {
  const pixels = Uint8Array.from([255, 0, 0, 0, 255, 0]);
  const rle = encodeCocoRle(pixels, 3, 2);
  digestCounter += 1;
  const digest = digestCounter.toString(16).padStart(64, "0");
  const ref: CocoRleMaskRef = {
    encoding: "coco_rle_ref",
    size: rle.size,
    object_key: `raster-masks/${digest}.json`,
    sha256: digest,
    runs: rle.counts.length,
    bytes: JSON.stringify(rle).length,
  };
  const load = options.load ?? vi.fn(async () => rle);
  const descriptor: RasterMaskRecordDescriptor<"annotation"> = {
    id,
    source: "annotation",
    ref,
    revision: options.revision ?? 1,
    color: "#102030",
    colorRevision: "class-color-1",
    zOrder: options.zOrder ?? 0,
    selected: options.selected ?? false,
    load,
  };
  return { descriptor, load, rle };
}

describe("useRasterMaskRecords", () => {
  let bitmaps: FakeImageBitmap[];
  let createBitmap: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    digestCounter = 0;
    bitmaps = [];
    createBitmap = vi.fn(async () => {
      const bitmap = new FakeImageBitmap();
      bitmaps.push(bitmap);
      return bitmap;
    });
    vi.stubGlobal(
      "ImageData",
      class {
        constructor(
          public data: Uint8ClampedArray,
          public width: number,
          public height: number,
        ) {}
      },
    );
    vi.stubGlobal("ImageBitmap", FakeImageBitmap);
    vi.stubGlobal("createImageBitmap", createBitmap);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each(["missing_object", "digest_mismatch"])(
    "preserves structured corruption reason %s for object-scoped recovery",
    (reason) => {
      expect(
        rasterMaskLoadError({
          status: 409,
          detailRaw: {
            reason,
            retryable: true,
            message: `mask object is invalid: ${reason}`,
          },
        }),
      ).toEqual({
        state: "error",
        reason: "corrupt",
        backendReason: reason,
        retryable: true,
        message: `mask object is invalid: ${reason}`,
        httpStatus: 409,
      });
    },
  );

  it("does not retry a 409 unless the backend explicitly marks it retryable", () => {
    expect(
      rasterMaskLoadError({
        status: 409,
        detailRaw: {
          reason: "mask_task_context_invalid",
          message: "task dimensions are unavailable",
        },
      }),
    ).toEqual({
      state: "error",
      reason: "corrupt",
      backendReason: "mask_task_context_invalid",
      retryable: false,
      message: "task dimensions are unavailable",
      httpStatus: 409,
    });
  });

  it("keys content revisions without including selected or z-order", () => {
    const item = makeDescriptor("mask");
    const key = rasterMaskRecordCacheKey(item.descriptor);
    const selectionOnlyChange = {
      ...item.descriptor,
      selected: true,
      zOrder: 99,
    };

    expect(rasterMaskRecordCacheKey(selectionOnlyChange)).toBe(key);
    expect(rasterMaskRecordCacheKey({ ...item.descriptor, revision: 2 })).not.toBe(key);
    expect(
      rasterMaskRecordCacheKey({ ...item.descriptor, colorRevision: "class-color-2" }),
    ).not.toBe(key);
    expect(
      rasterMaskRecordCacheKey({
        ...item.descriptor,
        ref: { ...item.descriptor.ref, sha256: "f".repeat(64) },
      }),
    ).not.toBe(key);
  });

  it("renders an inline interactive candidate without inventing an object key", async () => {
    const pixels = Uint8Array.from([255, 0, 0, 0, 255, 0]);
    const rle = encodeCocoRle(pixels, 3, 2);
    const descriptor: RasterMaskRecordDescriptor<"interactive"> = {
      id: "candidate-1",
      source: "interactive",
      ref: { size: rle.size, sha256: "a".repeat(64) },
      revision: "prompt-1",
      color: "#a855f7",
      colorRevision: "sam-mask-purple",
      zOrder: 0,
      selected: true,
      load: vi.fn(async () => rle),
    };
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1:prompt-1",
        descriptors: [descriptor],
      }),
    );

    await flushAsync();

    expect(view.result.current.records[0]).toMatchObject({
      id: "candidate-1",
      source: "interactive",
      selected: true,
      area: 2,
    });
    expect(view.result.current.records[0]?.rle).toBeUndefined();
  });

  it("waits for the persisted image Mask id before loading its optimistic content", async () => {
    const item = makeDescriptor("persisted-mask");
    const load = vi
      .spyOn(rasterMasksApi, "annotationRasterMaskContent")
      .mockResolvedValue(item.rle);
    const annotation: AnnotationResponse = {
      id: "tmp_new-mask",
      task_id: "task-1",
      project_id: "project-1",
      user_id: "user-1",
      source: "manual",
      annotation_type: "raster_mask",
      class_name: "car",
      geometry: {
        type: "raster_mask",
        mask: {
          ...item.descriptor.ref,
          encoding: "coco_rle_ref",
          object_key: "raster-masks/new-mask.json",
          runs: item.rle.counts.length,
          bytes: JSON.stringify(item.rle).length,
        },
      },
      confidence: 1,
      parent_prediction_id: null,
      parent_annotation_id: null,
      lead_time: null,
      is_active: true,
      ground_truth: false,
      created_at: "2026-09-07T10:59:50Z",
      updated_at: null,
      render_key: "tmp_new-mask",
    };
    const view = renderHook(
      ({ annotation }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: buildImageRasterMaskDescriptors([annotation], new Set([annotation.id])),
        }),
      { initialProps: { annotation } },
    );

    await flushAsync();
    expect(load).not.toHaveBeenCalled();
    expect(view.result.current.statusById.size).toBe(0);
    expect(view.result.current.records).toEqual([]);

    // The create mutation keeps render_key stable while replacing only the API identity.
    view.rerender({ annotation: { ...annotation, id: "persisted-mask", version: 1 } });
    await flushAsync();

    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith("persisted-mask");
    expect(view.result.current.statusById.get("persisted-mask")?.state).toBe("ready");
    expect(view.result.current.records[0]).toMatchObject({
      id: "persisted-mask",
      selected: true,
      area: 2,
    });
  });

  it("does not transfer an in-flight temporary id failure to its persisted replacement", async () => {
    const pending = deferred<CocoRle>();
    const temporary = makeDescriptor("tmp_new-mask", { load: vi.fn(() => pending.promise) });
    const persisted = makeDescriptor("persisted-mask", { selected: true });
    persisted.descriptor.ref = temporary.descriptor.ref;
    const view = renderHook(
      ({ descriptor }) => useRasterMaskRecords({ scopeKey: "task-1", descriptors: [descriptor] }),
      { initialProps: { descriptor: temporary.descriptor } },
    );

    expect(temporary.load).toHaveBeenCalledTimes(1);
    view.rerender({ descriptor: persisted.descriptor });
    expect(view.result.current.statusById.get("persisted-mask")?.state).toBe("loading");

    pending.reject({ status: 422 });
    await flushAsync(16);

    expect(persisted.load).toHaveBeenCalledTimes(1);
    expect(view.result.current.statusById.has("tmp_new-mask")).toBe(false);
    expect(view.result.current.statusById.get("persisted-mask")?.state).toBe("ready");
    expect(view.result.current.records[0]).toMatchObject({ id: "persisted-mask", area: 2 });
  });

  it.each([
    [403, "forbidden"],
    [404, "not_found"],
    [409, "corrupt"],
    [503, "unavailable"],
  ])(
    "preserves a source's own HTTP %i failure after another source fails",
    async (status, reason) => {
      const pending = deferred<CocoRle>();
      const first = makeDescriptor("first", { load: vi.fn(() => pending.promise) });
      const second = makeDescriptor("second", { load: vi.fn().mockRejectedValue({ status }) });
      second.descriptor.ref = first.descriptor.ref;
      const view = renderHook(() =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: [first.descriptor, second.descriptor],
        }),
      );

      pending.reject({ status: 422 });
      await flushAsync(16);

      expect(first.load).toHaveBeenCalledTimes(1);
      expect(second.load).toHaveBeenCalledTimes(1);
      expect(view.result.current.statusById.get("first")).toMatchObject({
        state: "error",
        httpStatus: 422,
      });
      expect(view.result.current.statusById.get("second")).toMatchObject({
        state: "error",
        httpStatus: status,
        reason,
      });
      expect(view.result.current.records).toEqual([]);
    },
  );

  it("keeps an in-flight source failure when only its display color changes", async () => {
    const pending = deferred<CocoRle>();
    const item = makeDescriptor("same-mask", { load: vi.fn(() => pending.promise) });
    const recoloredLoad = vi.fn(async () => item.rle);
    const view = renderHook(
      ({ descriptor }) => useRasterMaskRecords({ scopeKey: "task-1", descriptors: [descriptor] }),
      { initialProps: { descriptor: item.descriptor } },
    );

    view.rerender({
      descriptor: { ...item.descriptor, colorRevision: "new-color", load: recoloredLoad },
    });
    pending.reject({ status: 403 });
    await flushAsync(16);

    expect(item.load).toHaveBeenCalledTimes(1);
    expect(recoloredLoad).not.toHaveBeenCalled();
    expect(view.result.current.statusById.get("same-mask")).toMatchObject({
      state: "error",
      reason: "forbidden",
      retryable: false,
      httpStatus: 403,
    });
  });

  it("keeps ready siblings when one object fails and retries only the target", async () => {
    const ready = makeDescriptor("ready");
    const retryingLoad = vi
      .fn()
      .mockRejectedValueOnce({ status: 503 })
      .mockResolvedValueOnce(ready.rle);
    const retrying = makeDescriptor("retrying", { load: retryingLoad });
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [ready.descriptor, retrying.descriptor],
      }),
    );

    await flushAsync();

    expect(view.result.current.records.map((record) => record.id)).toEqual(["ready"]);
    expect(view.result.current.statusById.get("ready")?.state).toBe("ready");
    expect(view.result.current.statusById.get("retrying")).toMatchObject({
      state: "error",
      reason: "unavailable",
      message: "Mask 内容暂时不可用",
      retryable: true,
      httpStatus: 503,
    });

    act(() => view.result.current.retry("retrying"));
    await flushAsync();

    expect(ready.load).toHaveBeenCalledTimes(1);
    expect(retryingLoad).toHaveBeenCalledTimes(2);
    expect(view.result.current.records.map((record) => record.id)).toEqual(["ready", "retrying"]);
  });

  it("自动重试一次瞬时 Worker 构建失败，无需刷新页面", async () => {
    const item = makeDescriptor("transient-render");
    const [height, width] = item.rle.size;
    const analysis = analyzeRasterMaskAlpha(decodeCocoRle(item.rle), width, height);
    const analyze = vi
      .fn()
      .mockRejectedValueOnce(new RasterMaskWorkerError("worker restarted"))
      .mockResolvedValueOnce(analysis);
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [item.descriptor],
        workerPool: { analyze } as never,
      }),
    );

    await flushAsync(16);

    expect(analyze).toHaveBeenCalledTimes(2);
    expect(view.result.current.statusById.get("transient-render")?.state).toBe("ready");
    expect(view.result.current.records).toHaveLength(1);
  });

  it("createImageBitmap 瞬时失败时降级到 Canvas2D，无需刷新页面", async () => {
    const putImageData = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      putImageData,
    } as never);
    createBitmap.mockRejectedValueOnce(new Error("bitmap allocation failed"));
    const item = makeDescriptor("canvas-fallback");
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [item.descriptor],
      }),
    );

    await flushAsync(12);

    expect(createBitmap).toHaveBeenCalledTimes(1);
    expect(putImageData).toHaveBeenCalledTimes(1);
    expect(view.result.current.statusById.get("canvas-fallback")?.state).toBe("ready");
    expect(view.result.current.records[0]?.image).toBeInstanceOf(HTMLCanvasElement);
  });

  it("does not reload when only selected and z-order change", async () => {
    const item = makeDescriptor("mask");
    const view = renderHook(
      ({ descriptor }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: [descriptor],
        }),
      { initialProps: { descriptor: item.descriptor } },
    );
    await flushAsync();

    const originalKey = rasterMaskRecordCacheKey(item.descriptor);
    view.rerender({
      descriptor: { ...item.descriptor, selected: true, zOrder: 12 },
    });

    expect(item.load).toHaveBeenCalledTimes(1);
    expect(view.result.current.records[0]).toMatchObject({
      selected: true,
      zOrder: 12,
      cacheKey: originalKey,
    });
  });

  it("withdraws a superseded record immediately and closes a late bitmap", async () => {
    const firstItem = makeDescriptor("mask");
    const lateLoad = deferred<CocoRle>();
    const lateItem = makeDescriptor("mask", { revision: 2, load: vi.fn(() => lateLoad.promise) });
    const latestItem = makeDescriptor("mask", { revision: 3 });
    // Keep the content digest stable so revision alone invalidates each cache key.
    lateItem.descriptor.ref = firstItem.descriptor.ref;
    latestItem.descriptor.ref = firstItem.descriptor.ref;
    const view = renderHook(
      ({ descriptor }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: [descriptor],
        }),
      { initialProps: { descriptor: firstItem.descriptor } },
    );

    await flushAsync();
    expect(view.result.current.records[0]?.cacheKey).toBe(
      rasterMaskRecordCacheKey(firstItem.descriptor),
    );

    view.rerender({ descriptor: lateItem.descriptor });
    expect(view.result.current.records).toEqual([]);
    expect(view.result.current.statusById.get("mask")?.state).toBe("loading");

    view.rerender({ descriptor: latestItem.descriptor });
    await flushAsync();
    expect(view.result.current.records).toEqual([]);
    expect(latestItem.load).not.toHaveBeenCalled();

    lateLoad.resolve(lateItem.rle);
    await flushAsync();

    expect(view.result.current.records[0]?.cacheKey).toBe(
      rasterMaskRecordCacheKey(latestItem.descriptor),
    );
    expect(bitmaps).toHaveLength(3);
    expect(bitmaps[0].close).not.toHaveBeenCalled();
    const currentImage = view.result.current.records[0]?.image as unknown as
      | FakeImageBitmap
      | undefined;
    expect(bitmaps.slice(1).filter((bitmap) => bitmap === currentImage)).toHaveLength(1);
    for (const bitmap of bitmaps.slice(1)) {
      if (bitmap === currentImage) expect(bitmap.close).not.toHaveBeenCalled();
      else expect(bitmap.close).toHaveBeenCalledTimes(1);
    }
  });

  it("uses the Standard concurrency of two and advances the queue one completion at a time", async () => {
    const pending = Array.from({ length: 6 }, () => deferred<CocoRle>());
    const items = pending.map((request, index) =>
      makeDescriptor(`mask-${index}`, {
        load: vi.fn(() => request.promise),
      }),
    );
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: items.map((item) => item.descriptor),
      }),
    );

    expect(items.map((item) => item.load.mock.calls.length)).toEqual([1, 1, 0, 0, 0, 0]);

    pending[0].resolve(items[0].rle);
    await flushAsync();
    expect(items.map((item) => item.load.mock.calls.length)).toEqual([1, 1, 1, 0, 0, 0]);

    for (let index = 1; index < pending.length; index += 1)
      pending[index].resolve(items[index].rle);
    await flushAsync(16);
    expect(view.result.current.records).toHaveLength(6);
  });

  it("LRU eviction, scope reset, and unmount close each cached bitmap once", async () => {
    const first = makeDescriptor("first");
    const second = makeDescriptor("second");
    const third = makeDescriptor("third");
    const view = renderHook(
      ({ scope, items }) =>
        useRasterMaskRecords({
          scopeKey: scope,
          descriptors: items,
          maxCachedRecords: 2,
        }),
      {
        initialProps: {
          scope: "task-1",
          items: [first.descriptor, second.descriptor],
        },
      },
    );
    await flushAsync();

    view.rerender({ scope: "task-1", items: [second.descriptor, third.descriptor] });
    await flushAsync();
    expect(bitmaps).toHaveLength(3);
    expect(bitmaps[0].close).toHaveBeenCalledTimes(1);
    expect(bitmaps[1].close).not.toHaveBeenCalled();
    expect(bitmaps[2].close).not.toHaveBeenCalled();

    view.rerender({ scope: "task-2", items: [] });
    await flushAsync();
    expect(bitmaps[1].close).toHaveBeenCalledTimes(1);
    expect(bitmaps[2].close).toHaveBeenCalledTimes(1);
    expect(view.result.current.resources).toMatchObject({
      liveRecords: 0,
      retainedBytes: 0,
      liveBitmaps: 0,
    });
    view.unmount();
    for (const bitmap of bitmaps) expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("evicts inactive records by retained-byte budget", async () => {
    const first = makeDescriptor("first");
    const second = makeDescriptor("second");
    const recordBytes = estimateCocoRleRetainedBytes(first.rle) + 20;
    const view = renderHook(
      ({ items }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: items,
          maxCacheBytes: recordBytes + 1,
        }),
      { initialProps: { items: [first.descriptor] } },
    );
    await flushAsync();
    expect(view.result.current.cacheBytes).toBe(recordBytes);

    view.rerender({ items: [second.descriptor] });
    await flushAsync();

    expect(view.result.current.cacheBytes).toBe(recordBytes);
    expect(bitmaps[0].close).toHaveBeenCalledTimes(1);
    expect(bitmaps[1].close).not.toHaveBeenCalled();
  });

  it("keeps cache bytes stable across 50 masks and 50 task scopes", async () => {
    const makeScope = (scopeIndex: number) =>
      Array.from(
        { length: 50 },
        (_, maskIndex) => makeDescriptor(`scope-${scopeIndex}-mask-${maskIndex}`).descriptor,
      );
    const sample = makeDescriptor("sample");
    const recordBytes = estimateCocoRleRetainedBytes(sample.rle) + 20;
    const view = renderHook(
      ({ scope, items }) =>
        useRasterMaskRecords({
          scopeKey: scope,
          descriptors: items,
          maxCacheBytes: recordBytes * 50 + 1,
        }),
      {
        initialProps: {
          scope: "task-0",
          items: makeScope(0),
        },
      },
    );
    await flushAsync(16);
    expect(view.result.current.cacheBytes).toBe(recordBytes * 50);

    for (let scopeIndex = 1; scopeIndex < 50; scopeIndex += 1) {
      view.rerender({
        scope: `task-${scopeIndex}`,
        items: makeScope(scopeIndex),
      });
      await flushAsync(16);
      expect(view.result.current.cacheBytes).toBe(recordBytes * 50);
    }

    expect(bitmaps).toHaveLength(2500);
    for (const bitmap of bitmaps.slice(0, -50)) {
      expect(bitmap.close).toHaveBeenCalledTimes(1);
    }
    for (const bitmap of bitmaps.slice(-50)) expect(bitmap.close).not.toHaveBeenCalled();
    view.unmount();
    for (const bitmap of bitmaps) expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("freezes Low, Standard, and High device budgets", () => {
    expect(rasterMaskDeviceBudget(2)).toMatchObject({
      tier: "low",
      maxConcurrent: 1,
      workerPoolSize: 1,
      maxCacheBytes: 64 * 1024 * 1024,
    });
    expect(rasterMaskDeviceBudget(undefined)).toMatchObject({
      tier: "standard",
      maxConcurrent: 2,
      workerPoolSize: 2,
      maxCacheBytes: 128 * 1024 * 1024,
    });
    expect(rasterMaskDeviceBudget(-1).tier).toBe("standard");
    expect(rasterMaskDeviceBudget(8)).toMatchObject({
      tier: "high",
      maxConcurrent: 4,
      workerPoolSize: 2,
      maxCacheBytes: 192 * 1024 * 1024,
    });
  });

  it("prioritizes the selected descriptor ahead of current-frame siblings", () => {
    const firstRequest = deferred<CocoRle>();
    const selectedRequest = deferred<CocoRle>();
    const first = makeDescriptor("first", { load: vi.fn(() => firstRequest.promise) });
    const selected = makeDescriptor("selected", {
      selected: true,
      load: vi.fn(() => selectedRequest.promise),
    });
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [first.descriptor, selected.descriptor],
        maxConcurrent: 1,
      }),
    );

    expect(selected.load).toHaveBeenCalledTimes(1);
    expect(first.load).not.toHaveBeenCalled();
    view.unmount();
    selectedRequest.resolve(selected.rle);
    firstRequest.resolve(first.rle);
  });

  it("single-flights concurrent immutable content loads by sha256", async () => {
    const first = makeDescriptor("first");
    const second = makeDescriptor("second");
    second.descriptor.ref = first.descriptor.ref;
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [first.descriptor, second.descriptor],
      }),
    );

    await flushAsync();

    expect(first.load).toHaveBeenCalledTimes(1);
    expect(second.load).not.toHaveBeenCalled();
    expect(view.result.current.records).toHaveLength(2);
  });

  it("uses a budgeted preview while retaining exact RLE picking", async () => {
    const width = 20;
    const height = 20;
    const rle = encodeCocoRle(new Uint8Array(width * height).fill(255), width, height);
    const descriptor: RasterMaskRecordDescriptor<"annotation"> = {
      id: "large",
      source: "annotation",
      ref: { size: rle.size, sha256: "b".repeat(64) },
      revision: 1,
      color: "#102030",
      colorRevision: "class-color-1",
      zOrder: 0,
      selected: true,
      load: vi.fn(async () => rle),
    };
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [descriptor],
        maxCacheBytes: 500,
      }),
    );

    await flushAsync();

    expect(view.result.current.statusById.get("large")).toMatchObject({
      state: "ready",
      preview: true,
    });
    expect(view.result.current.cacheBytes).toBeLessThanOrEqual(500);
    expect(view.result.current.resources).toMatchObject({
      liveRecords: 1,
      retainedAlphaBytes: 0,
      bitmapsCreated: 1,
      bitmapsClosed: 0,
      liveBitmaps: 1,
    });
    expect(pickTopRasterMaskAt(view.result.current.records, { x: 0.9, y: 0.9 })?.id).toBe("large");
  });

  it("protects selected records and defers unselected admission at the hard budget", async () => {
    const selected = makeDescriptor("selected", { selected: true });
    const deferredItem = makeDescriptor("deferred");
    const recordBytes = estimateCocoRleRetainedBytes(selected.rle) + 20;
    const view = renderHook(
      ({ items }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: items,
          maxCacheBytes: recordBytes,
          maxConcurrent: 1,
        }),
      { initialProps: { items: [selected.descriptor, deferredItem.descriptor] } },
    );
    await flushAsync();

    expect(view.result.current.cacheBytes).toBeLessThanOrEqual(recordBytes);
    expect(view.result.current.statusById.get("selected")?.state).toBe("ready");
    expect(view.result.current.statusById.get("deferred")).toMatchObject({
      state: "deferred",
      reason: "budget_exceeded",
    });

    view.rerender({
      items: [
        { ...selected.descriptor, selected: false },
        { ...deferredItem.descriptor, selected: true },
      ],
    });
    await flushAsync();

    expect(view.result.current.cacheBytes).toBeLessThanOrEqual(recordBytes);
    expect(view.result.current.statusById.get("deferred")?.state).toBe("ready");
    expect(view.result.current.statusById.get("selected")?.state).toBe("deferred");
    expect(bitmaps[0].close).toHaveBeenCalledTimes(1);
  });

  it("closes an image created after unmount exactly once", async () => {
    const bitmapReady = deferred<FakeImageBitmap>();
    const lateBitmap = new FakeImageBitmap();
    createBitmap.mockImplementationOnce(() => bitmapReady.promise);
    const item = makeDescriptor("late");
    const view = renderHook(() =>
      useRasterMaskRecords({
        scopeKey: "task-1",
        descriptors: [item.descriptor],
      }),
    );

    await flushAsync();
    expect(createBitmap).toHaveBeenCalledTimes(1);
    view.unmount();
    bitmapReady.resolve(lateBitmap);
    await flushAsync();

    expect(lateBitmap.close).toHaveBeenCalledTimes(1);
  });

  it("把渲染 reservation 原子提交给任务级账本并在卸载后归零", async () => {
    const coordinator = new RasterResourceCoordinator({
      budget: {
        tier: "standard",
        softBudgetBytes: 1_024,
        hardBudgetBytes: 2_048,
        hiddenFreezeMs: 10,
      },
    });
    const item = makeDescriptor("owned", { selected: true });
    const view = renderHook(
      ({ visible }) =>
        useRasterMaskRecords({
          scopeKey: "task-1",
          descriptors: visible ? [item.descriptor] : [],
          resourceCoordinator: coordinator,
          resourceOwner: "mask-render:test",
        }),
      { initialProps: { visible: true } },
    );

    await flushAsync();
    expect(view.result.current.statusById.get("owned")?.state).toBe("ready");
    expect(coordinator.getSnapshot()).toMatchObject({
      reservedBytes: 0,
      invariantOk: true,
    });
    expect(coordinator.getSnapshot().owners).toContainEqual(
      expect.objectContaining({
        owner: "mask-render:test",
        pinnedBytes: view.result.current.cacheBytes,
      }),
    );

    view.rerender({ visible: false });
    expect(view.result.current.records).toHaveLength(0);
    expect(coordinator.getSnapshot().owners).toContainEqual(
      expect.objectContaining({ owner: "mask-render:test", pinnedBytes: 0 }),
    );
    view.rerender({ visible: true });
    expect(view.result.current.statusById.get("owned")?.state).toBe("ready");
    expect(coordinator.getSnapshot().owners).toContainEqual(
      expect.objectContaining({
        owner: "mask-render:test",
        pinnedBytes: view.result.current.cacheBytes,
      }),
    );

    view.unmount();
    expect(coordinator.getSnapshot()).toMatchObject({ committedBytes: 0, reservedBytes: 0 });
  });
});
