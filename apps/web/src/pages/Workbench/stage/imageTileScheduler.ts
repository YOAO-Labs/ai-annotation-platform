import { tasksApi } from "@/api/tasks";
import {
  chooseImagePyramidLevel,
  imageTileGeometry,
  imageTilesForRect,
  type ImagePyramidManifestV1,
  type ImageTileCoordinate,
  type ImageTileDeviceBudget,
  type ImageTileGeometry,
  type PixelRect,
} from "./imagePyramid";
import type {
  RasterResourceCoordinator,
  RasterResourceReservation,
} from "./shared/rasterResourceCoordinator";

export interface LoadedImageTile extends ImageTileGeometry {
  image: CanvasImageSource;
}

export interface ImageTileResourceSnapshot {
  sourceKind: "pyramid";
  generation: number;
  currentLevel: number;
  visibleTiles: number;
  retryingVisibleTiles: number;
  targetCoverageRatio: number;
  desiredTiles: number;
  queued: number;
  fetching: number;
  ready: number;
  deferred: number;
  errors: number;
  cacheHits: number;
  cacheMisses: number;
  evictions: number;
  requestedBytes: number;
  decodedBytes: number;
  reservedBytes: number;
  retainedBytes: number;
  budgetBytes: number;
  liveImageBitmaps: number;
  liveHtmlImages: number;
  liveObjectUrls: number;
  bitmapsCreated: number;
  bitmapsClosed: number;
  aborted: number;
  staleCommits: number;
  signBatches: number;
  urlRefreshes: number;
  prefetchPaused: boolean;
}

export interface DecodedImageTile {
  image: CanvasImageSource;
  width: number;
  height: number;
  kind: "bitmap" | "html";
  hasObjectUrl: boolean;
  release: () => void;
}

interface CacheEntry {
  geometry: ImageTileGeometry;
  decoded: DecodedImageTile;
  lastUsed: number;
  resource?: RasterResourceReservation;
}

interface QueueEntry {
  geometry: ImageTileGeometry;
  priority: number;
  url?: string;
}

export interface ImageTileSchedulerOptions {
  taskId: string;
  sourceIdentity: string;
  generation: number;
  manifest: ImagePyramidManifestV1;
  budget: ImageTileDeviceBudget;
  sign?: (coordinates: ImageTileCoordinate[], signal: AbortSignal) => Promise<Map<string, string>>;
  fetchBlob?: (url: string, signal: AbortSignal) => Promise<Blob>;
  decodeBlob?: (
    blob: Blob,
    geometry: ImageTileGeometry,
    signal: AbortSignal,
  ) => Promise<DecodedImageTile>;
  resourceCoordinator?: RasterResourceCoordinator;
  resourceOwner?: string;
}

interface ActiveTileLoad {
  controller: AbortController;
  priority: number;
  resource?: RasterResourceReservation;
}

function tileCoordinateKey(coordinate: ImageTileCoordinate): string {
  return `${coordinate.level}/${coordinate.x}/${coordinate.y}`;
}

function intersects(left: PixelRect, right: PixelRect): boolean {
  return (
    left.x < right.x + right.width &&
    left.x + left.width > right.x &&
    left.y < right.y + right.height &&
    left.y + left.height > right.y
  );
}

async function defaultFetchBlob(url: string, signal: AbortSignal): Promise<Blob> {
  const response = await fetch(url, { signal, credentials: "same-origin" });
  if (!response.ok) throw new Error(`tile_fetch_${response.status}`);
  return response.blob();
}

async function loadHtmlImage(
  blob: Blob,
  signal: AbortSignal,
): Promise<{
  image: HTMLImageElement;
  objectUrl: string;
}> {
  if (
    typeof Image === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    throw new Error("html_image_unavailable");
  }
  const objectUrl = URL.createObjectURL(blob);
  const image = new Image();
  image.decoding = "async";
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      image.onload = null;
      image.onerror = null;
      signal.removeEventListener("abort", onAbort);
    };
    const fail = (error: Error) => {
      cleanup();
      image.src = "";
      URL.revokeObjectURL(objectUrl);
      reject(error);
    };
    const onAbort = () => fail(new DOMException("Aborted", "AbortError"));
    image.onload = () => {
      cleanup();
      resolve({ image, objectUrl });
    };
    image.onerror = () => fail(new Error("html_image_decode_failed"));
    signal.addEventListener("abort", onAbort, { once: true });
    image.src = objectUrl;
  });
}

async function defaultDecodeBlob(
  blob: Blob,
  _geometry: ImageTileGeometry,
  signal: AbortSignal,
): Promise<DecodedImageTile> {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob);
      if (signal.aborted) {
        bitmap.close();
        throw new DOMException("Aborted", "AbortError");
      }
      let released = false;
      return {
        image: bitmap,
        width: bitmap.width,
        height: bitmap.height,
        kind: "bitmap",
        hasObjectUrl: false,
        release: () => {
          if (released) return;
          released = true;
          bitmap.close();
        },
      };
    } catch (error) {
      if (signal.aborted) throw error;
    }
  }
  const { image, objectUrl } = await loadHtmlImage(blob, signal);
  let released = false;
  return {
    image,
    width: image.naturalWidth,
    height: image.naturalHeight,
    kind: "html",
    hasObjectUrl: true,
    release: () => {
      if (released) return;
      released = true;
      image.src = "";
      URL.revokeObjectURL(objectUrl);
    },
  };
}

export class ImageTileScheduler {
  private readonly options: ImageTileSchedulerOptions;
  private readonly listeners = new Set<() => void>();
  private readonly cache = new Map<string, CacheEntry>();
  private drawableTiles: LoadedImageTile[] | null = null;
  private readonly active = new Map<string, ActiveTileLoad>();
  private readonly failures = new Map<string, { attempts: number; failedAt: number }>();
  private desired = new Map<string, QueueEntry>();
  private visibleKeys = new Set<string>();
  private queue: QueueEntry[] = [];
  private signing = false;
  private signController: AbortController | null = null;
  private disposed = false;
  private readonly prefetchPauseReasons = new Set<string>();
  private currentLevel = 0;
  private currentRect: PixelRect | null = null;
  private pressureLevelFloor = 0;
  private lastViewportScale = 1;
  private lastDevicePixelRatio = 1;
  private clock = 0;
  private reservedBytes = 0;
  private retainedBytes = 0;
  private deferred = 0;
  private errors = 0;
  private cacheHits = 0;
  private cacheMisses = 0;
  private evictions = 0;
  private requestedBytes = 0;
  private decodedBytes = 0;
  private liveImageBitmaps = 0;
  private liveHtmlImages = 0;
  private liveObjectUrls = 0;
  private bitmapsCreated = 0;
  private bitmapsClosed = 0;
  private aborted = 0;
  private staleCommits = 0;
  private signBatches = 0;
  private urlRefreshes = 0;
  private readonly resourceOwner: string;
  private unregisterResourceEvictor: (() => void) | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryAt = 0;

  constructor(options: ImageTileSchedulerOptions) {
    this.options = options;
    this.resourceOwner = options.resourceOwner ?? "background";
    if (options.resourceCoordinator) {
      this.unregisterResourceEvictor = options.resourceCoordinator.registerEvictor({
        owner: this.resourceOwner,
        evictableBytes: () => this.evictableBytes(),
        evict: (targetBytes, reason) => this.evictForPressure(targetBytes, reason),
        pausePrefetch: () => this.setPrefetchPaused(true, "coordinator"),
        resumePrefetch: (generation) => {
          if (generation !== options.resourceCoordinator?.generation) return;
          this.pressureLevelFloor = 0;
          this.setPrefetchPaused(false, "coordinator");
          if (this.currentRect) {
            this.update(this.currentRect, this.lastViewportScale, this.lastDevicePixelRatio);
          }
        },
      });
    }
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private notify(): void {
    if (this.disposed) return;
    for (const listener of this.listeners) listener();
  }

  setPrefetchPaused(paused: boolean, reason = "external"): void {
    const wasPaused = this.prefetchPauseReasons.size > 0;
    if (paused) this.prefetchPauseReasons.add(reason);
    else this.prefetchPauseReasons.delete(reason);
    if (wasPaused === this.prefetchPauseReasons.size > 0) return;
    if (this.currentRect) {
      this.updateViewport(this.currentRect, this.currentLevel, true);
    }
  }

  update(rect: PixelRect | null, viewportScale: number, devicePixelRatio: number): void {
    if (this.disposed) return;
    this.drawableTiles = null;
    if (!rect) {
      this.currentRect = null;
      this.replaceDesired(new Map(), new Set());
      return;
    }
    this.lastViewportScale = viewportScale;
    this.lastDevicePixelRatio = devicePixelRatio;
    const level = Math.max(
      this.pressureLevelFloor,
      chooseImagePyramidLevel(
        this.options.manifest,
        viewportScale,
        devicePixelRatio,
        this.currentLevel,
      ),
    );
    this.currentRect = rect;
    this.currentLevel = level;
    this.updateViewport(rect, level, false);
  }

  private updateViewport(rect: PixelRect, level: number, preserveLevel: boolean): void {
    if (!preserveLevel) this.currentLevel = level;
    const visible = imageTilesForRect(this.options.manifest, level, rect, 0);
    const desiredCoordinates =
      this.prefetchPauseReasons.size > 0
        ? visible
        : imageTilesForRect(this.options.manifest, level, rect, this.options.budget.overscanTiles);
    const visibleCoordinates = new Set(visible.map(tileCoordinateKey));
    const desired = new Map<string, QueueEntry>();
    for (const coordinate of desiredCoordinates) {
      const geometry = imageTileGeometry(
        this.options.sourceIdentity,
        this.options.manifest,
        coordinate,
      );
      const coordinateKey = tileCoordinateKey(coordinate);
      desired.set(geometry.key, {
        geometry,
        priority: visibleCoordinates.has(coordinateKey) ? 0 : 1,
      });
    }
    const visibleKeys = new Set(
      visible.map(
        (coordinate) =>
          imageTileGeometry(this.options.sourceIdentity, this.options.manifest, coordinate).key,
      ),
    );
    if (this.sameDesired(desired, visibleKeys)) return;
    this.replaceDesired(desired, visibleKeys);
  }

  private sameDesired(desired: Map<string, QueueEntry>, visibleKeys: Set<string>): boolean {
    if (desired.size !== this.desired.size || visibleKeys.size !== this.visibleKeys.size) {
      return false;
    }
    for (const [key, item] of desired) {
      if (this.desired.get(key)?.priority !== item.priority) return false;
      if (
        !this.cache.has(key) &&
        !this.active.has(key) &&
        !this.queue.some((queued) => queued.geometry.key === key)
      ) {
        return false;
      }
    }
    for (const key of visibleKeys) {
      if (!this.visibleKeys.has(key)) return false;
    }
    return true;
  }

  private replaceDesired(desired: Map<string, QueueEntry>, visibleKeys: Set<string>): void {
    this.desired = desired;
    this.visibleKeys = visibleKeys;
    for (const [key, active] of this.active) {
      if (!desired.has(key)) {
        active.controller.abort();
        this.aborted += 1;
      }
    }
    for (const [key, cached] of this.cache) {
      if (desired.has(key)) continue;
      cached.resource?.update({
        category: "background-detail",
        priority: 3,
        pinned: false,
      });
    }
    this.queue = [];
    const now = Date.now();
    for (const [key, item] of desired) {
      const cached = this.cache.get(key);
      if (cached) {
        cached.lastUsed = ++this.clock;
        const visible = visibleKeys.has(key);
        cached.resource?.update({
          category: visible ? "background-detail" : "background-prefetch",
          priority: visible ? 2 : 4,
          pinned: visible,
        });
        this.cacheHits += 1;
        continue;
      }
      const active = this.active.get(key);
      if (active) {
        const visible = visibleKeys.has(key);
        active.priority = item.priority;
        active.resource?.update({
          category: visible ? "background-detail" : "background-prefetch",
          priority: visible ? 2 : 4,
          pinned: visible,
        });
        continue;
      }
      const failure = this.failures.get(key);
      if (failure && failure.attempts >= 2 && now - failure.failedAt < 5_000) {
        this.scheduleRetry(failure.failedAt + 5_000);
        continue;
      }
      this.cacheMisses += 1;
      this.queue.push(item);
    }
    this.queue.sort((left, right) => left.priority - right.priority);
    this.evictToBudget(0);
    void this.signAndPump();
    this.notify();
  }

  private async signAndPump(): Promise<void> {
    if (this.disposed || this.signing) return;
    const unsigned = this.queue.filter((item) => !item.url).slice(0, 128);
    if (unsigned.length === 0) {
      this.pump();
      return;
    }
    this.signing = true;
    const controller = new AbortController();
    this.signController = controller;
    try {
      const coordinates = unsigned.map(({ geometry }) => ({
        level: geometry.level,
        x: geometry.x,
        y: geometry.y,
      }));
      const urls = this.options.sign
        ? await this.options.sign(coordinates, controller.signal)
        : await this.defaultSign(coordinates, controller.signal);
      this.signBatches += 1;
      for (const item of unsigned) {
        if (!this.desired.has(item.geometry.key)) continue;
        item.url = urls.get(tileCoordinateKey(item.geometry));
        if (!item.url) this.recordFailure(item.geometry.key);
      }
    } catch {
      if (!controller.signal.aborted) {
        for (const item of unsigned) this.recordFailure(item.geometry.key);
      }
    } finally {
      if (this.signController === controller) this.signController = null;
      this.signing = false;
      this.queue = this.queue.filter((item) => {
        if (!this.desired.has(item.geometry.key) || this.cache.has(item.geometry.key)) return false;
        const failure = this.failures.get(item.geometry.key);
        return (
          item.url != null ||
          failure == null ||
          failure.attempts < 2 ||
          Date.now() - failure.failedAt >= 5_000
        );
      });
      this.pump();
      if (this.queue.some((item) => !item.url)) void this.signAndPump();
      this.notify();
    }
  }

  private async defaultSign(
    coordinates: ImageTileCoordinate[],
    signal: AbortSignal,
  ): Promise<Map<string, string>> {
    const response = await tasksApi.getImagePyramidAssetUrls(
      this.options.taskId,
      coordinates.map((coordinate) => ({
        kind: "tile" as const,
        generation: this.options.generation,
        ...coordinate,
      })),
      { signal },
    );
    if (response.generation !== this.options.generation) {
      throw new Error("stale_generation");
    }
    return new Map(
      response.items.flatMap((item) =>
        item.kind === "tile" && item.level != null && item.x != null && item.y != null
          ? [[tileCoordinateKey({ level: item.level, x: item.x, y: item.y }), item.url]]
          : [],
      ),
    );
  }

  private pump(): void {
    if (this.disposed) return;
    while (this.active.size < this.options.budget.concurrency) {
      const index = this.queue.findIndex(
        (item) =>
          !!item.url &&
          this.desired.has(item.geometry.key) &&
          !this.active.has(item.geometry.key) &&
          !this.cache.has(item.geometry.key),
      );
      if (index < 0) break;
      const [item] = this.queue.splice(index, 1);
      const reservation = this.reserve(item);
      if (!reservation) {
        this.deferred += 1;
        continue;
      }
      const controller = new AbortController();
      const active: ActiveTileLoad = { controller, priority: item.priority, ...reservation };
      this.active.set(item.geometry.key, active);
      void this.fetchAndDecode(item, controller);
    }
  }

  private reserve(item: QueueEntry): { resource?: RasterResourceReservation } | null {
    const bytes = item.geometry.decodedBytes;
    this.evictToBudget(bytes);
    if (
      bytes > this.options.budget.retainedBytes ||
      this.retainedBytes + this.reservedBytes + bytes > this.options.budget.retainedBytes
    ) {
      return null;
    }
    const resource = this.options.resourceCoordinator?.tryReserve({
      owner: this.resourceOwner,
      category: item.priority === 0 ? "background-detail" : "background-prefetch",
      priority: item.priority === 0 ? 2 : 4,
      bytes,
      reconstructible: true,
      pinned: item.priority === 0,
    });
    if (this.options.resourceCoordinator && !resource) return null;
    this.reservedBytes += bytes;
    return resource ? { resource } : {};
  }

  private async fetchAndDecode(item: QueueEntry, controller: AbortController): Promise<void> {
    let decoded: DecodedImageTile | null = null;
    try {
      const blob = this.options.fetchBlob
        ? await this.options.fetchBlob(item.url!, controller.signal)
        : await defaultFetchBlob(item.url!, controller.signal);
      this.requestedBytes += blob.size;
      decoded = this.options.decodeBlob
        ? await this.options.decodeBlob(blob, item.geometry, controller.signal)
        : await defaultDecodeBlob(blob, item.geometry, controller.signal);
      if (decoded.kind === "bitmap") this.bitmapsCreated += 1;
      if (
        decoded.width !== item.geometry.decodedWidth ||
        decoded.height !== item.geometry.decodedHeight
      ) {
        throw new Error("tile_dimension_mismatch");
      }
      if (controller.signal.aborted || !this.desired.has(item.geometry.key) || this.disposed) {
        this.staleCommits += 1;
        this.releaseUncommitted(decoded);
        decoded = null;
        return;
      }
      const active = this.active.get(item.geometry.key);
      if (active?.resource && !active.resource.commit(item.geometry.decodedBytes)) {
        this.staleCommits += 1;
        this.releaseUncommitted(decoded);
        decoded = null;
        return;
      }
      this.cache.set(item.geometry.key, {
        geometry: item.geometry,
        decoded,
        lastUsed: ++this.clock,
        ...(active?.resource ? { resource: active.resource } : {}),
      });
      this.drawableTiles = null;
      if (active) active.resource = undefined;
      this.retainedBytes += item.geometry.decodedBytes;
      this.decodedBytes += item.geometry.decodedBytes;
      if (decoded.kind === "bitmap") {
        this.liveImageBitmaps += 1;
      } else {
        this.liveHtmlImages += 1;
      }
      if (decoded.hasObjectUrl) this.liveObjectUrls += 1;
      this.failures.delete(item.geometry.key);
      decoded = null;
      this.evictToBudget(0);
    } catch {
      if (decoded) this.releaseUncommitted(decoded);
      if (!controller.signal.aborted) {
        const attempts = this.recordFailure(item.geometry.key);
        const desired = this.desired.get(item.geometry.key);
        if (attempts < 2 && desired) {
          this.urlRefreshes += 1;
          this.queue.push({ geometry: desired.geometry, priority: desired.priority });
          this.queue.sort((left, right) => left.priority - right.priority);
        }
      }
    } finally {
      this.active.get(item.geometry.key)?.resource?.release();
      this.reservedBytes = Math.max(0, this.reservedBytes - item.geometry.decodedBytes);
      this.active.delete(item.geometry.key);
      const desired = this.desired.get(item.geometry.key);
      if (
        controller.signal.aborted &&
        desired &&
        !this.disposed &&
        !this.cache.has(item.geometry.key) &&
        !this.queue.some((queued) => queued.geometry.key === item.geometry.key)
      ) {
        this.queue.push(desired);
        this.queue.sort((left, right) => left.priority - right.priority);
      }
      this.pump();
      if (this.queue.some((queued) => !queued.url)) void this.signAndPump();
      this.notify();
    }
  }

  private releaseUncommitted(decoded: DecodedImageTile): void {
    decoded.release();
    if (decoded.kind === "bitmap") this.bitmapsClosed += 1;
  }

  private recordFailure(key: string): number {
    const previous = this.failures.get(key);
    const attempts = (previous?.attempts ?? 0) + 1;
    const failedAt = Date.now();
    this.failures.set(key, {
      attempts,
      failedAt,
    });
    if (attempts >= 2) this.scheduleRetry(failedAt + 5_000);
    this.errors += 1;
    return attempts;
  }

  private scheduleRetry(at: number): void {
    if (this.disposed || !this.currentRect) return;
    if (this.retryTimer && at >= this.retryAt) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryAt = at;
    this.retryTimer = setTimeout(
      () => {
        this.retryTimer = null;
        this.retryAt = 0;
        if (this.currentRect) this.updateViewport(this.currentRect, this.currentLevel, true);
      },
      Math.max(0, at - Date.now()),
    );
  }

  private evictToBudget(additionalBytes: number): void {
    if (
      this.retainedBytes + this.reservedBytes + additionalBytes <=
      this.options.budget.retainedBytes
    ) {
      return;
    }
    const candidates = [...this.cache.entries()]
      .filter(([key]) => !this.visibleKeys.has(key))
      .sort((left, right) => left[1].lastUsed - right[1].lastUsed);
    for (const [key] of candidates) {
      this.releaseCacheEntry(key);
      if (
        this.retainedBytes + this.reservedBytes + additionalBytes <=
        this.options.budget.retainedBytes
      ) {
        break;
      }
    }
  }

  private releaseCacheEntry(key: string): void {
    const entry = this.cache.get(key);
    if (!entry) return;
    this.cache.delete(key);
    this.drawableTiles = null;
    entry.resource?.release();
    entry.decoded.release();
    this.retainedBytes = Math.max(0, this.retainedBytes - entry.geometry.decodedBytes);
    if (entry.decoded.kind === "bitmap") {
      this.liveImageBitmaps = Math.max(0, this.liveImageBitmaps - 1);
      this.bitmapsClosed += 1;
    } else {
      this.liveHtmlImages = Math.max(0, this.liveHtmlImages - 1);
    }
    if (entry.decoded.hasObjectUrl) {
      this.liveObjectUrls = Math.max(0, this.liveObjectUrls - 1);
    }
    this.evictions += 1;
  }

  getTiles(): readonly LoadedImageTile[] {
    if (this.drawableTiles) return this.drawableTiles;
    const rect = this.currentRect;
    this.drawableTiles = rect
      ? [...this.cache.values()]
          .filter((entry) => intersects(entry.geometry.world, rect))
          .sort(
            (left, right) =>
              right.geometry.level - left.geometry.level ||
              left.geometry.y - right.geometry.y ||
              left.geometry.x - right.geometry.x,
          )
          .map((entry) => ({ ...entry.geometry, image: entry.decoded.image }))
      : [];
    return this.drawableTiles;
  }

  getSnapshot(): ImageTileResourceSnapshot {
    let visibleReady = 0;
    let retryingVisibleTiles = 0;
    for (const key of this.visibleKeys) {
      if (this.cache.has(key)) visibleReady += 1;
      else if (this.failures.has(key)) retryingVisibleTiles += 1;
    }
    return {
      sourceKind: "pyramid",
      generation: this.options.generation,
      currentLevel: this.currentLevel,
      visibleTiles: this.visibleKeys.size,
      retryingVisibleTiles,
      targetCoverageRatio: this.visibleKeys.size === 0 ? 1 : visibleReady / this.visibleKeys.size,
      desiredTiles: this.desired.size,
      queued: this.queue.length,
      fetching: this.active.size,
      ready: this.cache.size,
      deferred: this.deferred,
      errors: this.errors,
      cacheHits: this.cacheHits,
      cacheMisses: this.cacheMisses,
      evictions: this.evictions,
      requestedBytes: this.requestedBytes,
      decodedBytes: this.decodedBytes,
      reservedBytes: this.reservedBytes,
      retainedBytes: this.retainedBytes,
      budgetBytes: this.options.budget.retainedBytes,
      liveImageBitmaps: this.liveImageBitmaps,
      liveHtmlImages: this.liveHtmlImages,
      liveObjectUrls: this.liveObjectUrls,
      bitmapsCreated: this.bitmapsCreated,
      bitmapsClosed: this.bitmapsClosed,
      aborted: this.aborted,
      staleCommits: this.staleCommits,
      signBatches: this.signBatches,
      urlRefreshes: this.urlRefreshes,
      prefetchPaused: this.prefetchPauseReasons.size > 0,
    };
  }

  private evictableBytes(): number {
    let bytes = 0;
    for (const [key, entry] of this.cache) {
      if (!this.visibleKeys.has(key)) bytes += entry.geometry.decodedBytes;
    }
    return bytes;
  }

  private evictForPressure(
    targetBytes: number,
    reason:
      | "soft-budget"
      | "hard-budget"
      | "foreground-operation"
      | "hidden"
      | "bfcache"
      | "manual",
  ): number {
    let freed = 0;
    for (const active of this.active.values()) {
      if (reason !== "bfcache" && active.priority === 0) continue;
      active.controller.abort();
      this.aborted += 1;
    }
    const candidates = [...this.cache.entries()]
      .filter(([key]) => reason === "bfcache" || !this.visibleKeys.has(key))
      .sort((left, right) => left[1].lastUsed - right[1].lastUsed);
    for (const [key, entry] of candidates) {
      this.releaseCacheEntry(key);
      freed += entry.geometry.decodedBytes;
      if (freed >= targetBytes) break;
    }
    if (
      (reason === "hard-budget" || reason === "hidden" || reason === "bfcache") &&
      this.currentLevel < this.options.manifest.levels.length - 1
    ) {
      this.pressureLevelFloor = Math.max(this.pressureLevelFloor, this.currentLevel + 1);
    }
    this.notify();
    return freed;
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.signController?.abort();
    this.signController = null;
    for (const active of this.active.values()) {
      active.controller.abort();
      active.resource?.release();
    }
    this.active.clear();
    for (const key of [...this.cache.keys()]) this.releaseCacheEntry(key);
    this.queue = [];
    this.desired.clear();
    this.visibleKeys.clear();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.retryAt = 0;
    this.reservedBytes = 0;
    this.unregisterResourceEvictor?.();
    this.unregisterResourceEvictor = null;
    this.listeners.clear();
  }
}
