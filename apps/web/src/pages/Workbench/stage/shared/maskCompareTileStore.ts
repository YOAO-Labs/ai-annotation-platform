import type { CocoRle } from "./geometry/maskRle";
import { MASK_HISTORY_TILE_SIZE } from "./maskHistory";
import type { RasterMaskWorkerRunOptions } from "./rasterMaskWorkerPool";
import type {
  RasterMaskCompareMode,
  RasterMaskCompareMetrics,
  RasterMaskCompareSessionRef,
  RasterMaskTileRect,
} from "./rasterMaskWorkerProtocol";

const MAX_COMPARE_TILES = 16;
let nextCompareSessionId = 0;

export interface MaskCompareTileBackend {
  registerSession: (sessionId: string, sha256: string, rle: CocoRle) => void;
  releaseSession: (sessionId: string) => void;
  compareTile: (
    current: RasterMaskCompareSessionRef,
    baseline: RasterMaskCompareSessionRef,
    rect: RasterMaskTileRect,
    mode: RasterMaskCompareMode,
    sampleStep?: number,
    options?: RasterMaskWorkerRunOptions,
  ) => Promise<{
    current: RasterMaskCompareSessionRef;
    baseline: RasterMaskCompareSessionRef;
    rect: RasterMaskTileRect;
    mode: RasterMaskCompareMode;
    sampleStep: number;
    codes: Uint8Array;
  }>;
  compareMetrics: (
    current: RasterMaskCompareSessionRef,
    baseline: RasterMaskCompareSessionRef,
    options?: RasterMaskWorkerRunOptions,
  ) => Promise<{
    current: RasterMaskCompareSessionRef;
    baseline: RasterMaskCompareSessionRef;
    metrics: RasterMaskCompareMetrics;
  }>;
}

export interface MaskCompareRenderableTile extends RasterMaskTileRect {
  key: string;
  sampleStep: number;
  rasterWidth: number;
  rasterHeight: number;
  codes: Uint8Array;
}

export interface MaskCompareViewportRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface MaskCompareDisplayContext {
  annotationId: string;
  hideAiCandidate: boolean;
  hideTrackerCandidate: boolean;
}

export function maskCompareCompanionVisible(
  display: MaskCompareDisplayContext | null | undefined,
  companion: { source: "annotation" | "tracker" | "ai"; id?: string },
): boolean {
  if (!display) return true;
  if (companion.source === "annotation") return companion.id !== display.annotationId;
  if (companion.source === "tracker") return !display.hideTrackerCandidate;
  return !display.hideAiCandidate;
}

export class MaskCompareStaleGenerationError extends Error {
  constructor() {
    super("Mask comparison generation is stale");
    this.name = "MaskCompareStaleGenerationError";
  }
}

function sameRef(left: RasterMaskCompareSessionRef, right: RasterMaskCompareSessionRef): boolean {
  return left.sessionId === right.sessionId && left.sha256 === right.sha256;
}

function sameRect(left: RasterMaskTileRect, right: RasterMaskTileRect): boolean {
  return (
    left.x === right.x &&
    left.y === right.y &&
    left.width === right.width &&
    left.height === right.height
  );
}

function tileKey(tileX: number, tileY: number): string {
  return `${tileY}:${tileX}`;
}

export class MaskCompareTileStore {
  readonly width: number;
  readonly height: number;
  readonly display: MaskCompareDisplayContext | null;
  mode: RasterMaskCompareMode;

  private readonly backend: MaskCompareTileBackend;
  private readonly current: RasterMaskCompareSessionRef;
  private readonly baseline: RasterMaskCompareSessionRef;
  private readonly tiles = new Map<string, MaskCompareRenderableTile>();
  private generation = 0;
  private readonly lifecycleController = new AbortController();
  private inFlight: {
    signature: string;
    promise: Promise<MaskCompareRenderableTile[]>;
  } | null = null;
  private desiredSignature: string | null = null;
  private disposed = false;

  constructor(options: {
    backend: MaskCompareTileBackend;
    current: { sha256: string; rle: CocoRle };
    baseline: { sha256: string; rle: CocoRle };
    mode: RasterMaskCompareMode;
    scopeKey?: string;
    display?: MaskCompareDisplayContext;
  }) {
    const [height, width] = options.current.rle.size;
    if (height !== options.baseline.rle.size[0] || width !== options.baseline.rle.size[1]) {
      throw new Error("Mask comparison sides have different dimensions");
    }
    this.width = width;
    this.height = height;
    this.mode = options.mode;
    this.display = options.display ?? null;
    this.backend = options.backend;
    const prefix = `mask-compare:${options.scopeKey ?? "view"}:${++nextCompareSessionId}`;
    this.current = { sessionId: `${prefix}:current`, sha256: options.current.sha256 };
    this.baseline = { sessionId: `${prefix}:baseline`, sha256: options.baseline.sha256 };
    this.backend.registerSession(this.current.sessionId, this.current.sha256, options.current.rle);
    try {
      this.backend.registerSession(
        this.baseline.sessionId,
        this.baseline.sha256,
        options.baseline.rle,
      );
    } catch (error) {
      this.backend.releaseSession(this.current.sessionId);
      throw error;
    }
  }

  private selectedTiles(viewport: MaskCompareViewportRect): Array<{
    key: string;
    rect: RasterMaskTileRect;
    sampleStep: number;
  }> {
    const x0 = Math.max(0, Math.min(this.width - 1, Math.floor(viewport.x)));
    const y0 = Math.max(0, Math.min(this.height - 1, Math.floor(viewport.y)));
    const x1 = Math.max(x0 + 1, Math.min(this.width, Math.ceil(viewport.x + viewport.width)));
    const y1 = Math.max(y0 + 1, Math.min(this.height, Math.ceil(viewport.y + viewport.height)));
    let sampleStep = 1;
    let tileWorldSize = MASK_HISTORY_TILE_SIZE;
    let visibleMinX = 0;
    let visibleMinY = 0;
    let visibleMaxX = 0;
    let visibleMaxY = 0;
    while (true) {
      tileWorldSize = MASK_HISTORY_TILE_SIZE * sampleStep;
      visibleMinX = Math.floor(x0 / tileWorldSize);
      visibleMinY = Math.floor(y0 / tileWorldSize);
      visibleMaxX = Math.floor((x1 - 1) / tileWorldSize);
      visibleMaxY = Math.floor((y1 - 1) / tileWorldSize);
      const visibleCount = (visibleMaxX - visibleMinX + 1) * (visibleMaxY - visibleMinY + 1);
      if (visibleCount <= MAX_COMPARE_TILES) break;
      sampleStep *= 2;
    }
    const centerX = (visibleMinX + visibleMaxX) / 2;
    const centerY = (visibleMinY + visibleMaxY) / 2;
    const maxTileX = Math.ceil(this.width / tileWorldSize) - 1;
    const maxTileY = Math.ceil(this.height / tileWorldSize) - 1;
    const candidates: Array<{ tileX: number; tileY: number; visible: boolean; distance: number }> =
      [];
    for (
      let tileY = Math.max(0, visibleMinY - 1);
      tileY <= Math.min(maxTileY, visibleMaxY + 1);
      tileY += 1
    ) {
      for (
        let tileX = Math.max(0, visibleMinX - 1);
        tileX <= Math.min(maxTileX, visibleMaxX + 1);
        tileX += 1
      ) {
        const visible =
          tileX >= visibleMinX &&
          tileX <= visibleMaxX &&
          tileY >= visibleMinY &&
          tileY <= visibleMaxY;
        candidates.push({
          tileX,
          tileY,
          visible,
          distance: Math.abs(tileX - centerX) + Math.abs(tileY - centerY),
        });
      }
    }
    return candidates
      .sort(
        (left, right) =>
          Number(right.visible) - Number(left.visible) ||
          left.distance - right.distance ||
          left.tileY - right.tileY ||
          left.tileX - right.tileX,
      )
      .slice(0, MAX_COMPARE_TILES)
      .map(({ tileX, tileY }) => {
        const x = tileX * tileWorldSize;
        const y = tileY * tileWorldSize;
        return {
          key: `${this.mode}:${sampleStep}:${tileKey(tileX, tileY)}`,
          rect: {
            x,
            y,
            width: Math.min(tileWorldSize, this.width - x),
            height: Math.min(tileWorldSize, this.height - y),
          },
          sampleStep,
        };
      });
  }

  viewportSignature(viewport: MaskCompareViewportRect): string {
    if (this.disposed) return "disposed";
    return this.selectedTiles(viewport)
      .map((item) => item.key)
      .join("|");
  }

  async loadViewport(viewport: MaskCompareViewportRect): Promise<MaskCompareRenderableTile[]> {
    if (this.disposed) throw new Error("Mask comparison tile store is disposed");
    const selected = this.selectedTiles(viewport);
    const signature = selected.map((item) => item.key).join("|");
    const generation = ++this.generation;
    this.desiredSignature = signature;
    if (this.inFlight?.signature === signature) {
      const resolved = await this.inFlight.promise;
      if (this.disposed || generation !== this.generation || this.desiredSignature !== signature)
        throw new MaskCompareStaleGenerationError();
      const selectedKeys = new Set(selected.map((item) => item.key));
      for (const key of this.tiles.keys()) {
        if (!selectedKeys.has(key)) this.tiles.delete(key);
      }
      return resolved;
    }
    if (this.inFlight) {
      try {
        await this.inFlight.promise;
      } catch {
        // The newest viewport still gets a chance to load after an older failure.
      }
      if (this.disposed || generation !== this.generation || this.desiredSignature !== signature) {
        throw new MaskCompareStaleGenerationError();
      }
    }
    const selectedKeys = new Set(selected.map((item) => item.key));
    const mode = this.mode;
    const promise = Promise.all(
      selected.map(async ({ key, rect, sampleStep }) => {
        const cached = this.tiles.get(key);
        if (cached) return cached;
        const response = await this.backend.compareTile(
          this.current,
          this.baseline,
          rect,
          mode,
          sampleStep,
          { priority: "current", signal: this.lifecycleController.signal },
        );
        if (this.disposed) throw new MaskCompareStaleGenerationError();
        if (
          !sameRef(response.current, this.current) ||
          !sameRef(response.baseline, this.baseline) ||
          response.mode !== mode ||
          response.sampleStep !== sampleStep ||
          !sameRect(response.rect, rect) ||
          !(response.codes instanceof Uint8Array) ||
          response.codes.length !==
            Math.ceil(rect.width / sampleStep) * Math.ceil(rect.height / sampleStep)
        )
          throw new Error("Mask comparison tile response does not match its request");
        const tile = {
          key,
          ...rect,
          sampleStep,
          rasterWidth: Math.ceil(rect.width / sampleStep),
          rasterHeight: Math.ceil(rect.height / sampleStep),
          codes: response.codes,
        };
        this.tiles.set(key, tile);
        return tile;
      }),
    );
    this.inFlight = { signature, promise };
    let resolved: MaskCompareRenderableTile[];
    try {
      resolved = await promise;
    } finally {
      if (this.inFlight?.promise === promise) this.inFlight = null;
    }
    if (this.disposed || generation !== this.generation || this.desiredSignature !== signature)
      throw new MaskCompareStaleGenerationError();
    for (const key of this.tiles.keys()) {
      if (!selectedKeys.has(key)) this.tiles.delete(key);
    }
    return resolved;
  }

  snapshot(): { generation: number; tiles: number; disposed: boolean } {
    return { generation: this.generation, tiles: this.tiles.size, disposed: this.disposed };
  }

  async metrics(options?: RasterMaskWorkerRunOptions): Promise<RasterMaskCompareMetrics> {
    if (this.disposed) throw new Error("Mask comparison tile store is disposed");
    const response = await this.backend.compareMetrics(this.current, this.baseline, options);
    if (!sameRef(response.current, this.current) || !sameRef(response.baseline, this.baseline)) {
      throw new Error("Mask comparison metrics response does not match its request");
    }
    return response.metrics;
  }

  setMode(mode: RasterMaskCompareMode): void {
    if (this.disposed) throw new Error("Mask comparison tile store is disposed");
    if (this.mode === mode) return;
    this.mode = mode;
    this.generation += 1;
    this.desiredSignature = null;
    this.tiles.clear();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.generation += 1;
    this.desiredSignature = null;
    this.lifecycleController.abort();
    this.inFlight = null;
    this.tiles.clear();
    this.backend.releaseSession(this.current.sessionId);
    this.backend.releaseSession(this.baseline.sessionId);
  }
}
