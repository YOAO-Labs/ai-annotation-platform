import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskPointCloudManifestResponse } from "@/api/generated";
import * as imageDisplay from "@/utils/originalImageDisplay";
import * as originals from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";
import type { SensorCalibration } from "@/types";

vi.mock("@/utils/originalImageDisplay", () => ({
  typedOriginalImageBlob: async (blob: Blob) => blob,
  createOriginalImageAddress: vi.fn(),
}));

const buildDepthRastersMock = vi.hoisted(() => vi.fn());
const decodePointCloudFrameAsyncMock = vi.hoisted(() => vi.fn());

vi.mock("./geometry/pointCloudComputeSession", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./geometry/pointCloudComputeSession")>();
  return {
    ...actual,
    decodePointCloudFrameAsync: decodePointCloudFrameAsyncMock,
    getPointCloudDepthSession: () => ({ buildDepthRasters: buildDepthRastersMock }),
  };
});

import {
  acquireCameraBitmap,
  cancelPointCloudFramePrefetch,
  getPointCloudAssetCacheDiagnostics,
  loadPointCloudDepthRasters,
  loadPointCloudBuffer,
  loadTimedDecodedPointCloudFrame,
  prefetchPointCloudFrameAssets,
  prefetchPointCloudBuffer,
} from "./pointCloudAssetCache";
import { PointCloudComputeSession } from "./geometry/pointCloudComputeSession";

const CALIBRATION: SensorCalibration = {
  extrinsic: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
  intrinsic: [1, 0, 0, 0, 1, 0, 0, 0, 1],
};

function depthResult(length = 4) {
  return [{ cols: length, rows: 1, depth: new Float32Array(length).fill(1) }];
}

describe("pointCloudAssetCache", () => {
  beforeEach(() => {
    useAuthStore.setState({ user: null, token: null, sessionId: `test-${Math.random()}` });
    vi.mocked(imageDisplay.createOriginalImageAddress).mockReset();
    buildDepthRastersMock.mockReset().mockResolvedValue(depthResult());
    decodePointCloudFrameAsyncMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("terminates an interruptible decode worker when its frame becomes obsolete", async () => {
    const controller = new AbortController();
    const worker = {
      postMessage: vi.fn(),
      terminate: vi.fn(),
      onmessage: null,
      onerror: null,
    } as unknown as Worker;
    const session = new PointCloudComputeSession(() => worker);
    const decode = session.decodePcd(new Uint8Array([1, 2, 3]).buffer, "iso_8855", 500_000, {
      signal: controller.signal,
      terminateWorkerOnAbort: true,
    });

    controller.abort();

    await expect(decode).rejects.toMatchObject({ name: "AbortError" });
    expect(worker.terminate).toHaveBeenCalledTimes(1);
    expect(session.getDiagnostics()).toEqual({ workerActive: false, pendingRequests: 0 });
  });

  it("reuses an in-flight prefetched PCD request when the frame becomes current", async () => {
    const bytes = new Uint8Array([1, 2, 3]).buffer;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(bytes, {
        status: 200,
        headers: { "Content-Type": "application/octet-stream" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const prefetch = prefetchPointCloudBuffer("https://assets.test/frame-101.pcd");
    const loaded = loadPointCloudBuffer("https://assets.test/frame-101.pcd");

    await expect(prefetch).resolves.toBeUndefined();
    await expect(loaded).resolves.toEqual(bytes);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("evicts a failed request so a later frame load can retry", async () => {
    const bytes = new Uint8Array([4, 5, 6]).buffer;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("failed", { status: 503 }))
      .mockResolvedValueOnce(new Response(bytes, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadPointCloudBuffer("https://assets.test/frame-retry.pcd")).rejects.toThrow(
      "503",
    );
    await expect(loadPointCloudBuffer("https://assets.test/frame-retry.pcd")).resolves.toEqual(
      bytes,
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("aborts an obsolete active-frame decode before starting the latest frame", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]), { status: 200 })),
    );
    const pending = new Map<
      string,
      {
        signal: AbortSignal;
        resolve: (value: { positions: Float32Array }) => void;
        reject: (reason: unknown) => void;
      }
    >();
    decodePointCloudFrameAsyncMock.mockImplementation(
      (_buffer, _convention, _threshold, options: { lane?: string; signal?: AbortSignal } = {}) =>
        new Promise((resolve, reject) => {
          const url = pending.size === 0 ? "first" : "latest";
          const signal = options.signal!;
          pending.set(url, { signal, resolve, reject });
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Point-cloud computation aborted", "AbortError")),
            { once: true },
          );
        }),
    );

    const first = loadTimedDecodedPointCloudFrame(
      "https://assets.test/active-first.pcd",
      "iso_8855",
      500_000,
    );
    const firstRejected = expect(first).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(pending.has("first")).toBe(true));
    const latest = loadTimedDecodedPointCloudFrame(
      "https://assets.test/active-latest.pcd",
      "iso_8855",
      500_000,
    );
    await vi.waitFor(() => expect(pending.has("latest")).toBe(true));

    expect(pending.get("first")?.signal.aborted).toBe(true);
    await firstRejected;
    pending.get("latest")?.resolve({ positions: new Float32Array([0, 0, 1]) });
    await expect(latest).resolves.toMatchObject({ positions: expect.any(Float32Array) });
    expect(decodePointCloudFrameAsyncMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ byteLength: 1 }),
      "iso_8855",
      500_000,
      expect.objectContaining({ lane: "active", signal: expect.any(AbortSignal) }),
    );
  });

  it("aborts the obsolete PCD download before the latest frame waits for a network slot", async () => {
    const fetchSignals: Array<AbortSignal | undefined> = [];
    const resolveFetches: Array<(response: Response) => void> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            const signal = init?.signal ?? undefined;
            fetchSignals.push(signal);
            resolveFetches.push(resolve);
            signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Point-cloud fetch aborted", "AbortError")),
              { once: true },
            );
          }),
      ),
    );
    decodePointCloudFrameAsyncMock.mockImplementation(
      (_buffer, _convention, _threshold, options: { signal?: AbortSignal } = {}) => {
        if (options.signal?.aborted) {
          return Promise.reject(new DOMException("Point-cloud computation aborted", "AbortError"));
        }
        return Promise.resolve({ positions: new Float32Array([0, 0, 1]) });
      },
    );

    const obsolete: Array<Promise<unknown>> = [];
    for (let frame = 0; frame < 5; frame += 1) {
      obsolete.push(
        loadTimedDecodedPointCloudFrame(
          `https://assets.test/network-obsolete-${frame}.pcd`,
          "iso_8855",
          500_000,
        ).catch((error) => error),
      );
      await vi.waitFor(() => expect(resolveFetches).toHaveLength(frame + 1));
    }
    const latest = loadTimedDecodedPointCloudFrame(
      "https://assets.test/network-latest.pcd",
      "iso_8855",
      500_000,
    );
    await vi.waitFor(() => expect(resolveFetches).toHaveLength(6));

    resolveFetches.forEach((resolve, index) => {
      resolve(new Response(new Uint8Array([index]), { status: 200 }));
    });
    await expect(latest).resolves.toMatchObject({ positions: expect.any(Float32Array) });
    await Promise.all(obsolete);

    expect(fetchSignals).toHaveLength(6);
    for (const obsoleteSignal of fetchSignals.slice(0, -1)) {
      expect(obsoleteSignal).toBeInstanceOf(AbortSignal);
      expect(obsoleteSignal?.aborted).toBe(true);
    }
    expect(fetchSignals[fetchSignals.length - 1]?.aborted).toBe(false);
  });

  it("旧邻帧的 signal 不会取消同 URL 的当前帧下载", async () => {
    const requests: Array<{
      signal: AbortSignal | undefined;
      resolve: (response: Response) => void;
    }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            const signal = init?.signal ?? undefined;
            requests.push({ signal, resolve });
            signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Point-cloud fetch aborted", "AbortError")),
              { once: true },
            );
          }),
      ),
    );
    decodePointCloudFrameAsyncMock.mockResolvedValue({
      positions: new Float32Array([0, 0, 1]),
    });
    const neighborController = new AbortController();
    const url = "https://assets.test/shared-owner.pcd";

    const neighbor = loadPointCloudBuffer(url, neighborController.signal).catch((error) => error);
    const active = loadTimedDecodedPointCloudFrame(url, "iso_8855", 500_000);
    await vi.waitFor(() => expect(requests).toHaveLength(1));

    neighborController.abort();
    requests[0].resolve(new Response(new Uint8Array([1]), { status: 200 }));

    await expect(active).resolves.toMatchObject({ positions: expect.any(Float32Array) });
    await neighbor;
    expect(requests[0].signal?.aborted).toBe(false);
  });

  it("keeps rapid prefetches latest-only without cancelling the active-frame lane", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]), { status: 200 })),
    );
    const signals: AbortSignal[] = [];
    decodePointCloudFrameAsyncMock.mockImplementation(
      (_buffer, _convention, _threshold, options: { lane?: string; signal?: AbortSignal } = {}) =>
        new Promise((resolve, reject) => {
          const signal = options.signal!;
          signals.push(signal);
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Point-cloud computation aborted", "AbortError")),
            { once: true },
          );
          if (signals.length === 2) resolve({ positions: new Float32Array([0, 0, 1]) });
        }),
    );
    const manifest = (frame: number) =>
      ({
        point_cloud_url: `https://assets.test/prefetch-${frame}.pcd`,
        axis_convention: "iso_8855",
        cameras: [],
      }) as unknown as TaskPointCloudManifestResponse;

    const first = prefetchPointCloudFrameAssets(manifest(1));
    await vi.waitFor(() => expect(signals).toHaveLength(1));
    const latest = prefetchPointCloudFrameAssets(manifest(2));
    await vi.waitFor(() => expect(signals).toHaveLength(2));

    expect(signals[0].aborted).toBe(true);
    await expect(first).resolves.toBeUndefined();
    await expect(latest).resolves.toBeUndefined();
    expect(decodePointCloudFrameAsyncMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ byteLength: 1 }),
      "iso_8855",
      500_000,
      expect.objectContaining({ lane: "prefetch", signal: expect.any(AbortSignal) }),
    );
  });

  it("aborts obsolete camera downloads when a newer frame prefetch starts", async () => {
    const requests: Array<{
      url: string;
      signal: AbortSignal | undefined;
      resolve: (response: Response) => void;
    }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (input: RequestInfo | URL, init?: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            const signal = init?.signal ?? undefined;
            requests.push({ url: String(input), signal, resolve });
            signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Asset prefetch aborted", "AbortError")),
              { once: true },
            );
          }),
      ),
    );
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockResolvedValue({ width: 8, height: 4, close: vi.fn() } as unknown as ImageBitmap),
    );
    decodePointCloudFrameAsyncMock.mockResolvedValue({ positions: new Float32Array([0, 0, 1]) });
    const manifest = (frame: number) =>
      ({
        point_cloud_url: `https://assets.test/prefetch-camera-${frame}.pcd`,
        axis_convention: "iso_8855",
        cameras: Array.from({ length: 6 }, (_, index) => ({
          image_url: `https://assets.test/prefetch-camera-${frame}-${index}.jpg`,
          calibration: CALIBRATION,
        })),
      }) as unknown as TaskPointCloudManifestResponse;

    const first = prefetchPointCloudFrameAssets(manifest(1));
    await vi.waitFor(() => expect(requests).toHaveLength(7));
    const latest = prefetchPointCloudFrameAssets(manifest(2));
    await vi.waitFor(() => expect(requests).toHaveLength(14));

    try {
      const obsoleteCameras = requests
        .slice(0, 7)
        .filter((request) => request.url.endsWith(".jpg"));
      expect(obsoleteCameras).toHaveLength(6);
      expect(obsoleteCameras.every((request) => request.signal instanceof AbortSignal)).toBe(true);
      expect(obsoleteCameras.every((request) => request.signal?.aborted)).toBe(true);
    } finally {
      for (const request of requests) {
        request.resolve(new Response(new Uint8Array([1]), { status: 200 }));
      }
      await Promise.allSettled([first, latest]);
    }
  });

  it("does not let an in-flight prefetch block the same frame becoming active", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]), { status: 200 })),
    );
    const pending: Array<{
      lane: string | undefined;
      signal: AbortSignal | undefined;
      resolve: (value: { positions: Float32Array }) => void;
      reject: (reason: unknown) => void;
    }> = [];
    decodePointCloudFrameAsyncMock.mockImplementation(
      (_buffer, _convention, _threshold, options: { lane?: string; signal?: AbortSignal } = {}) =>
        new Promise((resolve, reject) => {
          pending.push({ lane: options.lane, signal: options.signal, resolve, reject });
          options.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Point-cloud computation aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    const manifest = {
      point_cloud_url: "https://assets.test/prefetch-promoted.pcd",
      axis_convention: "iso_8855",
      cameras: [],
    } as unknown as TaskPointCloudManifestResponse;

    const prefetch = prefetchPointCloudFrameAssets(manifest);
    await vi.waitFor(() => expect(pending).toHaveLength(1));
    const active = loadTimedDecodedPointCloudFrame(manifest.point_cloud_url!, "iso_8855", 500_000);
    await vi.waitFor(() => expect(pending).toHaveLength(1));

    expect(pending[0].signal?.aborted).toBe(false);
    pending[0].resolve({ positions: new Float32Array([0, 0, 1]) });
    await expect(active).resolves.toMatchObject({ positions: expect.any(Float32Array) });
    await expect(prefetch).resolves.toBeUndefined();
  });

  it("reuses decoded bitmaps and closes an unpinned LRU entry", async () => {
    const closeById = new Map<number, ReturnType<typeof vi.fn>>();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]), { status: 200 })),
    );
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockImplementation(async () => {
        const id = closeById.size;
        const close = vi.fn();
        closeById.set(id, close);
        return { width: 8, height: 4, close } as unknown as ImageBitmap;
      }),
    );

    const [firstA, firstB] = await Promise.all([
      acquireCameraBitmap("https://assets.test/camera-shared.jpg"),
      acquireCameraBitmap("https://assets.test/camera-shared.jpg"),
    ]);
    expect(firstA.bitmap).toBe(firstB.bitmap);
    expect(firstA.cacheReady).toBe(false);
    expect(firstB.cacheReady).toBe(false);
    const ready = await acquireCameraBitmap("https://assets.test/camera-shared.jpg");
    expect(ready.cacheReady).toBe(true);
    firstA.release();
    firstB.release();
    ready.release();

    const handles = await Promise.all(
      Array.from({ length: 18 }, (_, index) =>
        acquireCameraBitmap(`https://assets.test/camera-${index}.jpg`),
      ),
    );
    handles.forEach((handle) => handle.release());

    expect(closeById.get(0)).toHaveBeenCalledTimes(1);
  });

  it("merges concurrent depth-raster requests and reports the cache hit", async () => {
    let resolveDepth!: (value: ReturnType<typeof depthResult>) => void;
    buildDepthRastersMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveDepth = resolve;
      }),
    );
    const positions = new Float32Array([0, 0, 1]);
    const cameras = [{ calibration: CALIBRATION, width: 8, height: 8 }];

    const first = loadPointCloudDepthRasters(
      "https://assets.test/depth-shared.pcd",
      positions,
      cameras,
    );
    const second = loadPointCloudDepthRasters(
      "https://assets.test/depth-shared.pcd",
      positions,
      cameras,
    );
    await vi.waitFor(() => expect(buildDepthRastersMock).toHaveBeenCalledTimes(1));
    resolveDepth(depthResult());

    await expect(first).resolves.toMatchObject({ cacheHit: false });
    await expect(second).resolves.toMatchObject({ cacheHit: true });
    expect(buildDepthRastersMock).toHaveBeenCalledTimes(1);
  });

  it("removes failed depth requests so the same frame can retry", async () => {
    buildDepthRastersMock
      .mockRejectedValueOnce(new Error("worker failed"))
      .mockResolvedValueOnce(depthResult());
    const positions = new Float32Array([0, 0, 1]);
    const cameras = [{ calibration: CALIBRATION, width: 8, height: 8 }];

    await expect(
      loadPointCloudDepthRasters("https://assets.test/depth-retry.pcd", positions, cameras),
    ).rejects.toThrow("worker failed");
    await expect(
      loadPointCloudDepthRasters("https://assets.test/depth-retry.pcd", positions, cameras),
    ).resolves.toMatchObject({ cacheHit: false });
    expect(buildDepthRastersMock).toHaveBeenCalledTimes(2);
  });

  it("evicts depth rasters beyond the eight-key LRU limit", async () => {
    const positions = new Float32Array([0, 0, 1]);
    const cameras = [{ calibration: CALIBRATION, width: 8, height: 8 }];
    for (let index = 0; index < 9; index += 1) {
      await loadPointCloudDepthRasters(
        `https://assets.test/depth-lru-${index}.pcd`,
        positions,
        cameras,
      );
    }
    await loadPointCloudDepthRasters("https://assets.test/depth-lru-0.pcd", positions, cameras);

    expect(buildDepthRastersMock).toHaveBeenCalledTimes(10);
  });

  it("does not retain a single depth raster larger than the byte budget", async () => {
    buildDepthRastersMock.mockResolvedValue(depthResult(2_097_153));
    const positions = new Float32Array([0, 0, 1]);
    const cameras = [{ calibration: CALIBRATION, width: 8, height: 8 }];
    const url = "https://assets.test/depth-over-budget.pcd";

    await loadPointCloudDepthRasters(url, positions, cameras);
    await loadPointCloudDepthRasters(url, positions, cameras);

    expect(buildDepthRastersMock).toHaveBeenCalledTimes(2);
  });

  it("把深度预取保留在可取消的帧预取生命周期内", async () => {
    let resolveDepth!: (value: ReturnType<typeof depthResult>) => void;
    buildDepthRastersMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveDepth = resolve;
      }),
    );
    decodePointCloudFrameAsyncMock.mockResolvedValueOnce({
      positions: new Float32Array([0, 0, 1]),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]), { status: 200 })),
    );
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockResolvedValue({ width: 8, height: 4, close: vi.fn() }),
    );
    const manifest = {
      point_cloud_url: "https://assets.test/frame-background-depth.pcd",
      axis_convention: "iso_8855",
      cameras: [
        {
          role: "front",
          image_url: "https://assets.test/frame-background-depth.jpg",
          calibration: CALIBRATION,
        },
      ],
    } as unknown as TaskPointCloudManifestResponse;

    const prefetch = prefetchPointCloudFrameAssets(manifest, { depthRasters: true });
    await vi.waitFor(() => expect(buildDepthRastersMock).toHaveBeenCalledTimes(1));
    const completedBeforeDepth = await Promise.race([
      prefetch.then(() => true),
      new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 0)),
    ]);
    resolveDepth(depthResult());
    await prefetch;

    expect(completedBeforeDepth).toBe(false);
  });
  it("isolates account/session caches while preserving same-session token refresh", async () => {
    useAuthStore.getState().setAuth("token-a", { id: "account-a" } as never);
    const fetcher = vi.fn().mockImplementation(async () => new Response(new Uint8Array([1])));
    vi.stubGlobal("fetch", fetcher);
    const url = "https://assets.test/owner-cache.pcd";
    await loadPointCloudBuffer(url);
    useAuthStore.getState().setToken("token-refreshed");
    await loadPointCloudBuffer(url);
    expect(fetcher).toHaveBeenCalledTimes(1);
    useAuthStore.getState().setAuth("new-login", { id: "account-a" } as never);
    await loadPointCloudBuffer(url);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenLastCalledWith(url, expect.objectContaining({ credentials: "omit" }));
  });

  it("rejects old-owner waiters immediately and discards a transport that ignores abort", async () => {
    let finish!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      )
      .mockImplementation(async () => new Response(new Uint8Array([2])));
    vi.stubGlobal("fetch", fetcher);
    const url = "https://assets.test/late-owner.pcd";
    const old = loadPointCloudBuffer(url);
    const rejected = expect(old).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    useAuthStore.setState({ sessionId: "replacement" });
    await rejected;
    finish(new Response(new Uint8Array([1])));
    await expect(loadPointCloudBuffer(url)).resolves.toEqual(new Uint8Array([2]).buffer);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("keeps a retired bitmap alive until every renderer lease releases", async () => {
    const close = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]))),
    );
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({ width: 8, height: 4, close }));
    const first = await acquireCameraBitmap("https://assets.test/pinned-owner.jpg");
    const second = await acquireCameraBitmap("https://assets.test/pinned-owner.jpg");
    useAuthStore.setState({ sessionId: "retired-owner" });
    expect(close).not.toHaveBeenCalled();
    first.release();
    first.release();
    expect(close).not.toHaveBeenCalled();
    second.release();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("closes a late bitmap after its login owner has been retired", async () => {
    let finish!: (bitmap: ImageBitmap) => void;
    const close = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]))),
    );
    const decode = vi.fn().mockImplementation(
      () =>
        new Promise<ImageBitmap>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal("createImageBitmap", decode);
    const old = acquireCameraBitmap("https://assets.test/late-bitmap.jpg");
    const rejected = expect(old).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    useAuthStore.setState({ sessionId: "bitmap-owner-replaced" });
    await rejected;
    finish({ width: 8, height: 4, close } as unknown as ImageBitmap);
    await vi.waitFor(() => expect(close).toHaveBeenCalledTimes(1));
  });

  it("pins prefetch bitmaps through depth work and releases them after completion", async () => {
    let finish!: (value: ReturnType<typeof depthResult>) => void;
    buildDepthRastersMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    decodePointCloudFrameAsyncMock.mockResolvedValue({ positions: new Float32Array([0, 0, 1]) });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response(new Uint8Array([1]))),
    );
    const closes: ReturnType<typeof vi.fn>[] = [];
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn().mockImplementation(async () => {
        const close = vi.fn();
        closes.push(close);
        return { width: 8, height: 4, close };
      }),
    );
    const prefetch = prefetchPointCloudFrameAssets(
      {
        point_cloud_url: "https://assets.test/pinned-depth.pcd",
        cameras: [{ image_url: "https://assets.test/pinned-depth.jpg", calibration: CALIBRATION }],
      } as TaskPointCloudManifestResponse,
      { depthRasters: true },
    );
    await vi.waitFor(() => expect(buildDepthRastersMock).toHaveBeenCalledTimes(1));
    const leases = await Promise.all(
      Array.from({ length: 19 }, (_, i) =>
        acquireCameraBitmap(`https://assets.test/depth-pressure-${i}.jpg`),
      ),
    );
    expect(closes[0]).not.toHaveBeenCalled();
    finish(depthResult());
    await prefetch;
    expect(closes[0]).toHaveBeenCalledTimes(1);
    leases.forEach((lease) => lease.release());
  });

  it("shares a private authenticated read and promotes it without handing credentials to image URLs", async () => {
    useAuthStore.getState().setAuth("secret-bearer", { id: "account" } as never);
    let finish!: (blob: Blob) => void;
    const read = vi.spyOn(originals, "readOriginalBytes").mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const promote = vi.spyOn(originals, "promoteOriginalRead");
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const url = `/api/v1/files/tasks/11111111-1111-4111-8111-111111111111/original?content_id=22222222-2222-4222-8222-222222222222&sha256=${"a".repeat(64)}`;
    const cancel = new AbortController();
    const first = loadPointCloudBuffer(url, cancel.signal);
    const rejected = expect(first).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    const active = loadPointCloudBuffer(url);
    const options = read.mock.calls[0][1]!;
    expect(options.owner).toEqual(originals.captureOriginalOwner());
    expect(options.signal).not.toBe(cancel.signal);
    cancel.abort();
    await rejected;
    expect(options.signal?.aborted).toBe(false);
    expect(promote).toHaveBeenCalledWith(options.signal);
    finish(await new Response(new Uint8Array([3])).blob());
    await expect(active).resolves.toEqual(new Uint8Array([3]).buffer);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("recovers a cache first observed during an incomplete cross-tab credential pair", async () => {
    useAuthStore.getState().setAuth("before-refresh", { id: "account" } as never);
    const key = originals.originalOwnerKey();
    localStorage.setItem("token", "after-refresh");
    expect(originals.captureOriginalOwner()).toBeNull();
    getPointCloudAssetCacheDiagnostics();
    useAuthStore.getState().setToken("after-refresh");
    expect(originals.originalOwnerKey()).toBe(key);
    const read = vi
      .spyOn(originals, "readOriginalBytes")
      .mockResolvedValue(await new Response(new Uint8Array([7])).blob());
    const url = `/api/v1/files/tasks/11111111-1111-4111-8111-111111111111/original?content_id=22222222-2222-4222-8222-222222222222&sha256=${"b".repeat(64)}`;
    await expect(loadPointCloudBuffer(url)).resolves.toEqual(new Uint8Array([7]).buffer);
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("cancels speculative consumers on workbench disposal without cancelling a joined foreground read", async () => {
    let finish!: (response: Response) => void;
    let transportSignal: AbortSignal | undefined;
    const fetcher = vi.fn().mockImplementation(
      (_url, init: RequestInit) =>
        new Promise<Response>((resolve) => {
          finish = resolve;
          transportSignal = init.signal ?? undefined;
        }),
    );
    vi.stubGlobal("fetch", fetcher);
    const url = "https://assets.test/disposed-prefetch.pcd";
    const prefetch = prefetchPointCloudFrameAssets({
      point_cloud_url: url,
      cameras: [],
    } as unknown as TaskPointCloudManifestResponse);
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    const active = loadPointCloudBuffer(url);
    cancelPointCloudFramePrefetch();
    await expect(prefetch).resolves.toBeUndefined();
    expect(transportSignal?.aborted).toBe(false);
    finish(new Response(new Uint8Array([8])));
    await expect(active).resolves.toEqual(new Uint8Array([8]).buffer);
  });

  it("aborts an unshared speculative transport when its workbench is disposed", async () => {
    let transportSignal: AbortSignal | undefined;
    const fetcher = vi.fn().mockImplementation(
      (_url, init: RequestInit) =>
        new Promise<Response>(() => {
          transportSignal = init.signal ?? undefined;
        }),
    );
    vi.stubGlobal("fetch", fetcher);
    const prefetch = prefetchPointCloudFrameAssets({
      point_cloud_url: "https://assets.test/disposed-unshared.pcd",
      cameras: [],
    } as unknown as TaskPointCloudManifestResponse);
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    cancelPointCloudFramePrefetch();
    await expect(prefetch).resolves.toBeUndefined();
    expect(transportSignal?.aborted).toBe(true);
  });
  it("decodes SVG through an opaque Image address before creating the GPU bitmap", async () => {
    const release = vi.fn();
    vi.mocked(imageDisplay.createOriginalImageAddress).mockResolvedValue({
      url: "data:image/svg+xml;base64,PHN2Zy8+",
      release,
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(
          async () => new Response("<svg/>", { headers: { "Content-Type": "image/svg+xml" } }),
        ),
    );
    const decode = vi.fn().mockResolvedValue(undefined);
    const images: Array<{ src: string; decoding: string; decode: typeof decode }> = [];
    class DecodedImage {
      src = "";
      decoding = "";
      decode = decode;
      constructor() {
        images.push(this);
      }
    }
    vi.stubGlobal("Image", DecodedImage);
    const close = vi.fn();
    const bitmap = { width: 640, height: 480, close };
    const convert = vi.fn().mockImplementation(async (source) => {
      expect(source).toBe(images[0]);
      expect(source.src).toBe("data:image/svg+xml;base64,PHN2Zy8+");
      expect(decode).toHaveBeenCalledTimes(1);
      return bitmap;
    });
    vi.stubGlobal("createImageBitmap", convert);
    const lease = await acquireCameraBitmap("https://assets.test/gpu-svg.svg");
    expect(lease.bitmap).toBe(bitmap);
    expect(convert).toHaveBeenCalledTimes(1);
    expect(images[0].src).toBe("");
    expect(release).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
    lease.release();
  });

  it("aborts SVG image decoding and releases its address after the real decode settles", async () => {
    const release = vi.fn();
    let finish!: () => void;
    vi.mocked(imageDisplay.createOriginalImageAddress).mockResolvedValue({
      url: "data:image/svg+xml;base64,PHN2Zy8+",
      release,
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(
          async () => new Response("<svg/>", { headers: { "Content-Type": "image/svg+xml" } }),
        ),
    );
    const decode = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const images: Array<{ src: string }> = [];
    class PendingImage {
      src = "";
      decode = decode;
      constructor() {
        images.push(this);
      }
    }
    vi.stubGlobal("Image", PendingImage);
    const convert = vi.fn();
    vi.stubGlobal("createImageBitmap", convert);
    const controller = new AbortController();
    const pending = acquireCameraBitmap(
      "https://assets.test/gpu-svg-cancel.svg",
      controller.signal,
    );
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(decode).toHaveBeenCalledTimes(1));
    controller.abort();
    await rejected;
    expect(images[0].src).toBe("");
    expect(release).not.toHaveBeenCalled();
    finish();
    await vi.waitFor(() => expect(release).toHaveBeenCalledTimes(1));
    expect(convert).not.toHaveBeenCalled();
  });

  it("closes a late SVG GPU bitmap after owner retirement before releasing its opaque address", async () => {
    const release = vi.fn(),
      close = vi.fn();
    let finish!: (bitmap: ImageBitmap) => void;
    vi.mocked(imageDisplay.createOriginalImageAddress).mockResolvedValue({
      url: "data:image/svg+xml;base64,PHN2Zy8+",
      release,
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(
          async () => new Response("<svg/>", { headers: { "Content-Type": "image/svg+xml" } }),
        ),
    );
    class DecodedImage {
      src = "";
      decode = vi.fn().mockResolvedValue(undefined);
    }
    vi.stubGlobal("Image", DecodedImage);
    const convert = vi.fn().mockImplementation(
      () =>
        new Promise<ImageBitmap>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal("createImageBitmap", convert);
    const pending = acquireCameraBitmap("https://assets.test/gpu-svg-late.svg");
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(convert).toHaveBeenCalledTimes(1));
    useAuthStore.setState({ sessionId: "svg-owner-retired" });
    await rejected;
    expect(release).not.toHaveBeenCalled();
    finish({ width: 640, height: 480, close } as unknown as ImageBitmap);
    await vi.waitFor(() => expect(close).toHaveBeenCalledTimes(1));
    expect(release).toHaveBeenCalledTimes(1);
    expect(close.mock.invocationCallOrder[0]).toBeLessThan(release.mock.invocationCallOrder[0]);
  });
});
