import type {
  RasterMaskCompareMode,
  RasterMaskCompareMetrics,
  RasterMaskMorphologyRoiRequest,
  RasterMaskMorphologyRoiResponse,
  RasterMaskPackedTileOverride,
  RasterMaskTileOverride,
  RasterMaskTileRect,
  RasterMaskTransferredRle,
} from "./rasterMaskWorkerProtocol";
import { applyMaskMorphology } from "./geometry/maskOperations";
import { MASK_HISTORY_TILE_SIZE, type MaskHistoryPatch } from "./maskHistory";

const MAX_MORPHOLOGY_ROI_PIXELS = 16_777_216;

export interface RasterMaskWorkerSession {
  sha256: string;
  size: [number, number];
  counts: Uint32Array;
  runEnds: Uint32Array;
}

export function compareRasterMaskSessionMetrics(
  current: RasterMaskWorkerSession,
  baseline: RasterMaskWorkerSession,
): RasterMaskCompareMetrics {
  if (current.size[0] !== baseline.size[0] || current.size[1] !== baseline.size[1]) {
    throw new Error("mask comparison sessions have different dimensions");
  }
  const pixels = current.size[0] * current.size[1];
  let currentIndex = 0;
  let baselineIndex = 0;
  let offset = 0;
  let currentAreaPixels = 0;
  let baselineAreaPixels = 0;
  let intersectionPixels = 0;
  while (offset < pixels) {
    while (current.runEnds[currentIndex] === offset) currentIndex += 1;
    while (baseline.runEnds[baselineIndex] === offset) baselineIndex += 1;
    const next = Math.min(current.runEnds[currentIndex], baseline.runEnds[baselineIndex]);
    if (!Number.isSafeInteger(next) || next <= offset || next > pixels) {
      throw new Error("mask comparison RLE is invalid");
    }
    const span = next - offset;
    const currentOn = (currentIndex & 1) === 1;
    const baselineOn = (baselineIndex & 1) === 1;
    if (currentOn) currentAreaPixels += span;
    if (baselineOn) baselineAreaPixels += span;
    if (currentOn && baselineOn) intersectionPixels += span;
    offset = next;
  }
  const unionPixels = currentAreaPixels + baselineAreaPixels - intersectionPixels;
  const addedPixels = currentAreaPixels - intersectionPixels;
  const removedPixels = baselineAreaPixels - intersectionPixels;
  return {
    currentAreaPixels,
    baselineAreaPixels,
    intersectionPixels,
    unionPixels,
    changedPixels: addedPixels + removedPixels,
    addedPixels,
    removedPixels,
  };
}

function checkedDimensions(size: [number, number]): {
  height: number;
  width: number;
  pixels: number;
} {
  const [height, width] = size;
  if (!Number.isSafeInteger(height) || height <= 0 || !Number.isSafeInteger(width) || width <= 0) {
    throw new Error("mask size must contain positive integers");
  }
  const pixels = height * width;
  if (!Number.isSafeInteger(pixels) || pixels > 0xffff_ffff) {
    throw new Error("mask pixel count exceeds the transferable RLE limit");
  }
  return { height, width, pixels };
}

export function buildRasterMaskWorkerSession(
  sha256: string,
  rle: RasterMaskTransferredRle,
): RasterMaskWorkerSession {
  const { pixels } = checkedDimensions(rle.size);
  if (!(rle.counts instanceof Uint32Array) || rle.counts.length === 0) {
    throw new Error("mask counts must be a non-empty Uint32Array");
  }
  const runEnds = new Uint32Array(rle.counts.length);
  let total = 0;
  for (let index = 0; index < rle.counts.length; index += 1) {
    total += rle.counts[index];
    if (total > pixels) throw new Error("sum(counts) exceeds height * width");
    runEnds[index] = total;
  }
  if (total !== pixels) throw new Error("sum(counts) must equal height * width");
  return {
    sha256,
    size: [rle.size[0], rle.size[1]],
    counts: rle.counts,
    runEnds,
  };
}

export function decodeRasterMaskTransferredRle(rle: RasterMaskTransferredRle): Uint8Array {
  const { height, width, pixels } = checkedDimensions(rle.size);
  if (!(rle.counts instanceof Uint32Array) || rle.counts.length === 0) {
    throw new Error("mask counts must be a non-empty Uint32Array");
  }
  const alpha = new Uint8Array(width * height);
  let offset = 0;
  for (let runIndex = 0; runIndex < rle.counts.length; runIndex += 1) {
    const runLength = rle.counts[runIndex];
    if (offset + runLength > pixels) throw new Error("sum(counts) exceeds height * width");
    if ((runIndex & 1) === 1) {
      for (let index = offset; index < offset + runLength; index += 1) {
        const x = Math.floor(index / height);
        const y = index % height;
        alpha[y * width + x] = 255;
      }
    }
    offset += runLength;
  }
  if (offset !== pixels) throw new Error("sum(counts) must equal height * width");
  return alpha;
}

function validateTileRect(session: RasterMaskWorkerSession, rect: RasterMaskTileRect): void {
  const [height, width] = session.size;
  if (
    !Number.isSafeInteger(rect.x) ||
    !Number.isSafeInteger(rect.y) ||
    !Number.isSafeInteger(rect.width) ||
    !Number.isSafeInteger(rect.height) ||
    rect.x < 0 ||
    rect.y < 0 ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    rect.x + rect.width > width ||
    rect.y + rect.height > height
  ) {
    throw new Error("mask tile rectangle is outside the session bounds");
  }
}

function runIndexAt(runEnds: Uint32Array, offset: number): number {
  let low = 0;
  let high = runEnds.length;
  while (low < high) {
    const mid = low + Math.floor((high - low) / 2);
    if (runEnds[mid] <= offset) low = mid + 1;
    else high = mid;
  }
  if (low >= runEnds.length) throw new Error("mask RLE offset is outside the session");
  return low;
}

export function decodeRasterMaskSessionTile(
  session: RasterMaskWorkerSession,
  rect: RasterMaskTileRect,
): Uint8Array {
  validateTileRect(session, rect);
  const [sourceHeight] = session.size;
  const alpha = new Uint8Array(rect.width * rect.height);
  for (let localX = 0; localX < rect.width; localX += 1) {
    let sourceOffset = (rect.x + localX) * sourceHeight + rect.y;
    let runIndex = runIndexAt(session.runEnds, sourceOffset);
    for (let localY = 0; localY < rect.height; localY += 1) {
      while (sourceOffset >= session.runEnds[runIndex]) runIndex += 1;
      if ((runIndex & 1) === 1) alpha[localY * rect.width + localX] = 255;
      sourceOffset += 1;
    }
  }
  return alpha;
}

function runtimeNowMs(): number {
  return globalThis.performance?.now?.() ?? Date.now();
}

export function validatePackedTileOverride(
  session: RasterMaskWorkerSession,
  tile: RasterMaskPackedTileOverride,
): void {
  validateTileRect(session, tile);
  const [sourceHeight, sourceWidth] = session.size;
  if (
    !Number.isSafeInteger(tile.tileX) ||
    tile.tileX < 0 ||
    !Number.isSafeInteger(tile.tileY) ||
    tile.tileY < 0 ||
    !Number.isSafeInteger(tile.revision) ||
    tile.revision < 0 ||
    tile.x !== tile.tileX * MASK_HISTORY_TILE_SIZE ||
    tile.y !== tile.tileY * MASK_HISTORY_TILE_SIZE ||
    tile.width !== Math.min(MASK_HISTORY_TILE_SIZE, sourceWidth - tile.x) ||
    tile.height !== Math.min(MASK_HISTORY_TILE_SIZE, sourceHeight - tile.y) ||
    !(tile.bits instanceof Uint8Array) ||
    tile.bits.length !== Math.ceil((tile.width * tile.height) / 8)
  ) {
    throw new Error("packed mask tile override does not match the session tile grid");
  }
}

function applyPackedOverridesToRoi(
  session: RasterMaskWorkerSession,
  input: RasterMaskTileRect,
  alpha: Uint8Array,
  overrides: readonly RasterMaskPackedTileOverride[],
): void {
  const seen = new Set<string>();
  for (const tile of overrides) {
    validatePackedTileOverride(session, tile);
    const key = `${tile.tileY}:${tile.tileX}`;
    if (seen.has(key)) throw new Error("packed mask tile overrides contain duplicates");
    seen.add(key);
    const x0 = Math.max(input.x, tile.x);
    const y0 = Math.max(input.y, tile.y);
    const x1 = Math.min(input.x + input.width, tile.x + tile.width);
    const y1 = Math.min(input.y + input.height, tile.y + tile.height);
    if (x1 <= x0 || y1 <= y0) continue;
    for (let y = y0; y < y1; y += 1) {
      for (let x = x0; x < x1; x += 1) {
        const tileIndex = (y - tile.y) * tile.width + x - tile.x;
        const enabled = (tile.bits[tileIndex >> 3] & (1 << (tileIndex & 7))) !== 0;
        alpha[(y - input.y) * input.width + x - input.x] = enabled ? 255 : 0;
      }
    }
  }
}

export type RasterMaskMorphologyRuntimeRequest = Omit<
  RasterMaskMorphologyRoiRequest,
  "kind" | "id" | "sessionId" | "sha256" | "dirtyOverrides"
> & { dirtyOverrides: readonly RasterMaskPackedTileOverride[] };

export interface RasterMaskPreparedMorphologyRoi {
  source: Uint8Array;
  materializeMs: number;
}

export interface RasterMaskPreparedPackedMorphologyRoi {
  sourceWords: Uint32Array;
  wordsPerRow: number;
  packedPrepareMs: number;
  prepareStrategy: "direct-rle" | "packed-cache";
  directRleScanMs: number;
  baseCacheFillMs: number;
  packedAssembleMs: number;
  dirtyOverlayMs: number;
  baseCacheHitTiles: number;
  baseCacheMissTiles: number;
  baseCacheEvictedTiles: number;
  baseCacheRetainedBytes: number;
  sourceScratchCapacityBytes: number;
}

export interface RasterMaskDenseCpuByteEstimate {
  sourceBytes: number;
  workspaceBytes: number;
  patchUpperBoundBytes: number;
  requiredBytes: number;
}

function checkedCpuBytes(values: readonly number[]): number {
  let total = 0;
  for (const value of values) {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error("Raster Mask dense CPU byte estimate exceeds the safe limit");
    }
    total += value;
    if (!Number.isSafeInteger(total)) {
      throw new Error("Raster Mask dense CPU byte estimate exceeds the safe limit");
    }
  }
  return total;
}

export function estimateRasterMaskDenseCpuBytes(
  request: RasterMaskMorphologyRuntimeRequest,
): RasterMaskDenseCpuByteEstimate {
  const sourceBytes = checkedCpuBytes([request.input.width * request.input.height]);
  const patchUpperBoundBytes = Math.ceil((request.core.width * request.core.height) / 8);
  const isTwoPass =
    request.operation.operation === "open" || request.operation.operation === "close";
  let workspaceBytes: number;
  if (request.operation.kernelShape === "square") {
    workspaceBytes = sourceBytes * (isTwoPass ? 3 : 2);
  } else if (request.operation.operation === "erode") {
    const paddedWidth = request.input.width + 2;
    const paddedHeight = request.input.height + 2;
    const paddedPixels = paddedWidth * paddedHeight;
    workspaceBytes = checkedCpuBytes([
      paddedPixels * 6,
      paddedWidth * Float64Array.BYTES_PER_ELEMENT * 2,
      isTwoPass ? sourceBytes : 0,
    ]);
  } else {
    workspaceBytes = checkedCpuBytes([
      sourceBytes * 6,
      request.input.width * Float64Array.BYTES_PER_ELEMENT * 2,
      isTwoPass ? sourceBytes : 0,
    ]);
  }
  return {
    sourceBytes,
    workspaceBytes,
    patchUpperBoundBytes,
    requiredBytes: checkedCpuBytes([sourceBytes, workspaceBytes, patchUpperBoundBytes]),
  };
}

export interface RasterMaskPackedBaseTile {
  tileX: number;
  tileY: number;
  x: number;
  y: number;
  width: number;
  height: number;
  wordsPerRow: number;
  words: Uint32Array;
  byteSize: number;
}

export interface RasterMaskMorphologyRoiDiff {
  changedPixels: number;
  changedBounds: RasterMaskTileRect | null;
  patches: MaskHistoryPatch[];
  diffMs: number;
  xorOutputStrategy?: "dense-word-scatter";
  xorTotalWords?: number;
  xorNonZeroWords?: number;
  xorWordDensity?: number;
  xorTouchedTiles?: number;
  xorScanMs?: number;
  xorPatchAllocateMs?: number;
  xorPatchScatterMs?: number;
  wordPatchBuildMs?: number;
}

export function validateRasterMaskMorphologyRoiRequest(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
): void {
  validateTileRect(session, request.core);
  validateTileRect(session, request.input);
  if (!Number.isSafeInteger(request.sourceRevision) || request.sourceRevision < 0) {
    throw new Error("Raster Mask morphology source revision must be a non-negative integer");
  }
  if (!Number.isSafeInteger(request.cpuComputeBudgetBytes) || request.cpuComputeBudgetBytes <= 0) {
    throw new Error("Raster Mask morphology CPU compute budget must be a positive integer");
  }
  if (!Number.isSafeInteger(request.gpuBufferBudgetBytes) || request.gpuBufferBudgetBytes < 0) {
    throw new Error("Raster Mask morphology GPU buffer budget must be non-negative");
  }
  if (
    !["dilate", "erode", "open", "close"].includes(request.operation.operation) ||
    !["disk", "square"].includes(request.operation.kernelShape) ||
    !Number.isInteger(request.operation.radius) ||
    request.operation.radius < 1 ||
    request.operation.radius > 32
  ) {
    throw new Error("Raster Mask morphology operation is invalid");
  }
  const [sourceHeight, sourceWidth] = session.size;
  const passes =
    request.operation.operation === "open" || request.operation.operation === "close" ? 2 : 1;
  const halo = request.operation.radius * passes;
  const expectedInput = {
    x: Math.max(0, request.core.x - halo),
    y: Math.max(0, request.core.y - halo),
    width:
      Math.min(sourceWidth, request.core.x + request.core.width + halo) -
      Math.max(0, request.core.x - halo),
    height:
      Math.min(sourceHeight, request.core.y + request.core.height + halo) -
      Math.max(0, request.core.y - halo),
  };
  if (
    request.input.x !== expectedInput.x ||
    request.input.y !== expectedInput.y ||
    request.input.width !== expectedInput.width ||
    request.input.height !== expectedInput.height
  ) {
    throw new Error("Raster Mask morphology input does not contain the required clipped halo");
  }
  if (request.input.width * request.input.height > MAX_MORPHOLOGY_ROI_PIXELS) {
    throw new Error("Raster Mask morphology input exceeds the ROI pixel budget");
  }
}

export function prepareRasterMaskMorphologyRoi(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
): RasterMaskPreparedMorphologyRoi {
  validateRasterMaskMorphologyRoiRequest(session, request);

  const materializeStarted = runtimeNowMs();
  const source = decodeRasterMaskSessionTile(session, request.input);
  applyPackedOverridesToRoi(session, request.input, source, request.dirtyOverrides);
  return { source, materializeMs: runtimeNowMs() - materializeStarted };
}

function clearUnusedPackedRowBits(
  words: Uint32Array,
  width: number,
  height: number,
  wordsPerRow: number,
): void {
  const remainder = width & 31;
  if (remainder === 0) return;
  const validMask = 0xffff_ffff >>> (32 - remainder);
  for (let y = 0; y < height; y += 1) {
    words[y * wordsPerRow + wordsPerRow - 1] &= validMask;
  }
}

function applyPackedOverridesToPackedRoi(
  session: RasterMaskWorkerSession,
  input: RasterMaskTileRect,
  words: Uint32Array,
  wordsPerRow: number,
  overrides: readonly RasterMaskPackedTileOverride[],
): void {
  const seen = new Set<string>();
  for (const tile of overrides) {
    validatePackedTileOverride(session, tile);
    const key = `${tile.tileY}:${tile.tileX}`;
    if (seen.has(key)) throw new Error("packed mask tile overrides contain duplicates");
    seen.add(key);
    const x0 = Math.max(input.x, tile.x);
    const y0 = Math.max(input.y, tile.y);
    const x1 = Math.min(input.x + input.width, tile.x + tile.width);
    const y1 = Math.min(input.y + input.height, tile.y + tile.height);
    if (x1 <= x0 || y1 <= y0) continue;
    for (let y = y0; y < y1; y += 1) {
      const targetRow = (y - input.y) * wordsPerRow;
      const tileRow = (y - tile.y) * tile.width;
      for (let x = x0; x < x1; x += 1) {
        const tileIndex = tileRow + x - tile.x;
        const targetX = x - input.x;
        const targetIndex = targetRow + (targetX >>> 5);
        const targetMask = (1 << (targetX & 31)) >>> 0;
        if ((tile.bits[tileIndex >>> 3] & (1 << (tileIndex & 7))) !== 0) {
          words[targetIndex] |= targetMask;
        } else {
          words[targetIndex] &= ~targetMask;
        }
      }
    }
  }
  clearUnusedPackedRowBits(words, input.width, input.height, wordsPerRow);
}

export function preparePackedRasterMaskMorphologyRoi(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
): RasterMaskPreparedPackedMorphologyRoi {
  validateRasterMaskMorphologyRoiRequest(session, request);
  const started = runtimeNowMs();
  const wordsPerRow = Math.ceil(request.input.width / 32);
  const sourceWords = new Uint32Array(wordsPerRow * request.input.height);
  const [sourceHeight] = session.size;
  for (let localX = 0; localX < request.input.width; localX += 1) {
    const columnStart = (request.input.x + localX) * sourceHeight;
    const inputStart = columnStart + request.input.y;
    const inputEnd = inputStart + request.input.height;
    const targetWord = localX >>> 5;
    const targetMask = (1 << (localX & 31)) >>> 0;
    let sourceOffset = inputStart;
    let runIndex = runIndexAt(session.runEnds, sourceOffset);
    while (sourceOffset < inputEnd) {
      while (session.runEnds[runIndex] <= sourceOffset) runIndex += 1;
      const nextOffset = Math.min(inputEnd, session.runEnds[runIndex]);
      if ((runIndex & 1) === 1) {
        for (let offset = sourceOffset; offset < nextOffset; offset += 1) {
          sourceWords[(offset - inputStart) * wordsPerRow + targetWord] |= targetMask;
        }
      }
      sourceOffset = nextOffset;
    }
  }
  const directRleScanMs = runtimeNowMs() - started;
  const dirtyStarted = runtimeNowMs();
  applyPackedOverridesToPackedRoi(
    session,
    request.input,
    sourceWords,
    wordsPerRow,
    request.dirtyOverrides,
  );
  const dirtyOverlayMs = runtimeNowMs() - dirtyStarted;
  return {
    sourceWords,
    wordsPerRow,
    packedPrepareMs: runtimeNowMs() - started,
    prepareStrategy: "direct-rle",
    directRleScanMs,
    baseCacheFillMs: 0,
    packedAssembleMs: 0,
    dirtyOverlayMs,
    baseCacheHitTiles: 0,
    baseCacheMissTiles: 0,
    baseCacheEvictedTiles: 0,
    baseCacheRetainedBytes: 0,
    sourceScratchCapacityBytes: 0,
  };
}

export function buildRasterMaskPackedBaseTile(
  session: RasterMaskWorkerSession,
  tileX: number,
  tileY: number,
): RasterMaskPackedBaseTile {
  const [sourceHeight, sourceWidth] = session.size;
  if (!Number.isSafeInteger(tileX) || tileX < 0 || !Number.isSafeInteger(tileY) || tileY < 0) {
    throw new Error("packed base tile coordinates must be non-negative integers");
  }
  const x = tileX * MASK_HISTORY_TILE_SIZE;
  const y = tileY * MASK_HISTORY_TILE_SIZE;
  if (x >= sourceWidth || y >= sourceHeight) {
    throw new Error("packed base tile is outside the session bounds");
  }
  const width = Math.min(MASK_HISTORY_TILE_SIZE, sourceWidth - x);
  const height = Math.min(MASK_HISTORY_TILE_SIZE, sourceHeight - y);
  const wordsPerRow = Math.ceil(width / 32);
  const words = new Uint32Array(wordsPerRow * height);
  for (let localX = 0; localX < width; localX += 1) {
    const columnStart = (x + localX) * sourceHeight;
    const inputStart = columnStart + y;
    const inputEnd = inputStart + height;
    const targetWord = localX >>> 5;
    const targetMask = (1 << (localX & 31)) >>> 0;
    let sourceOffset = inputStart;
    let runIndex = runIndexAt(session.runEnds, sourceOffset);
    while (sourceOffset < inputEnd) {
      while (session.runEnds[runIndex] <= sourceOffset) runIndex += 1;
      const nextOffset = Math.min(inputEnd, session.runEnds[runIndex]);
      if ((runIndex & 1) === 1) {
        for (let offset = sourceOffset; offset < nextOffset; offset += 1) {
          words[(offset - inputStart) * wordsPerRow + targetWord] |= targetMask;
        }
      }
      sourceOffset = nextOffset;
    }
  }
  clearUnusedPackedRowBits(words, width, height, wordsPerRow);
  return {
    tileX,
    tileY,
    x,
    y,
    width,
    height,
    wordsPerRow,
    words,
    byteSize: words.byteLength,
  };
}

export function diffRasterMaskMorphologyRoi(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
  source: Uint8Array,
  readAfter: (inputIndex: number, localX: number, localY: number) => boolean,
): RasterMaskMorphologyRoiDiff {
  if (source.length !== request.input.width * request.input.height) {
    throw new Error("Raster Mask morphology source does not match its input rectangle");
  }
  const [sourceHeight, sourceWidth] = session.size;
  const diffStarted = runtimeNowMs();
  const patchesByTile = new Map<string, MaskHistoryPatch>();
  let changedPixels = 0;
  let minX = sourceWidth;
  let minY = sourceHeight;
  let maxX = -1;
  let maxY = -1;
  for (let y = request.core.y; y < request.core.y + request.core.height; y += 1) {
    for (let x = request.core.x; x < request.core.x + request.core.width; x += 1) {
      const inputIndex = (y - request.input.y) * request.input.width + x - request.input.x;
      const afterEnabled = readAfter(inputIndex, x - request.input.x, y - request.input.y);
      if ((source[inputIndex] !== 0) === afterEnabled) continue;
      const tileX = Math.floor(x / MASK_HISTORY_TILE_SIZE);
      const tileY = Math.floor(y / MASK_HISTORY_TILE_SIZE);
      const key = `${tileY}:${tileX}`;
      let patch = patchesByTile.get(key);
      if (!patch) {
        const tileWidth = Math.min(
          MASK_HISTORY_TILE_SIZE,
          sourceWidth - tileX * MASK_HISTORY_TILE_SIZE,
        );
        const tileHeight = Math.min(
          MASK_HISTORY_TILE_SIZE,
          sourceHeight - tileY * MASK_HISTORY_TILE_SIZE,
        );
        patch = {
          tileX,
          tileY,
          width: tileWidth,
          height: tileHeight,
          xorBits: new Uint8Array(Math.ceil((tileWidth * tileHeight) / 8)),
        };
        patchesByTile.set(key, patch);
      }
      const localX = x - tileX * MASK_HISTORY_TILE_SIZE;
      const localY = y - tileY * MASK_HISTORY_TILE_SIZE;
      const tileIndex = localY * patch.width + localX;
      patch.xorBits[tileIndex >> 3] |= 1 << (tileIndex & 7);
      changedPixels += 1;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
  }
  const diffMs = runtimeNowMs() - diffStarted;
  return {
    changedPixels,
    changedBounds:
      changedPixels === 0
        ? null
        : { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 },
    patches: [...patchesByTile.values()].sort(
      (left, right) => left.tileY - right.tileY || left.tileX - right.tileX,
    ),
    diffMs,
  };
}

function lowBitMask(bitCount: number): number {
  return bitCount >= 32 ? 0xffff_ffff : 0xffff_ffff >>> (32 - bitCount);
}

function popcount32(value: number): number {
  let count = value >>> 0;
  count -= (count >>> 1) & 0x5555_5555;
  count = (count & 0x3333_3333) + ((count >>> 2) & 0x3333_3333);
  count = (count + (count >>> 4)) & 0x0f0f_0f0f;
  return Math.imul(count, 0x0101_0101) >>> 24;
}

function orPackedBits(
  target: Uint8Array,
  targetBitOffset: number,
  sourceWord: number,
  bitCount: number,
): void {
  let sourceBitOffset = 0;
  let targetOffset = targetBitOffset;
  while (sourceBitOffset < bitCount) {
    const targetByte = targetOffset >>> 3;
    const targetBit = targetOffset & 7;
    const chunkBits = Math.min(bitCount - sourceBitOffset, 8 - targetBit);
    const chunkMask = (1 << chunkBits) - 1;
    const chunk = (sourceWord >>> sourceBitOffset) & chunkMask;
    target[targetByte] |= chunk << targetBit;
    sourceBitOffset += chunkBits;
    targetOffset += chunkBits;
  }
}

export function buildRasterMaskMorphologyPatchesFromXorWords(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
  xorWords: Uint32Array,
  xorWordsPerRow: number,
): RasterMaskMorphologyRoiDiff {
  validateRasterMaskMorphologyRoiRequest(session, request);
  const expectedWordsPerRow = Math.ceil(request.core.width / 32);
  if (
    !Number.isSafeInteger(xorWordsPerRow) ||
    xorWordsPerRow !== expectedWordsPerRow ||
    !(xorWords instanceof Uint32Array) ||
    xorWords.length !== xorWordsPerRow * request.core.height
  ) {
    throw new Error("Raster Mask morphology XOR plane does not match the core rectangle");
  }
  const [sourceHeight, sourceWidth] = session.size;
  const started = runtimeNowMs();
  let nonZeroWordCount = 0;
  let changedPixels = 0;
  let minX = sourceWidth;
  let minY = sourceHeight;
  let maxX = -1;
  let maxY = -1;
  const remainder = request.core.width & 31;
  const lastWordMask = remainder === 0 ? 0xffff_ffff : 0xffff_ffff >>> (32 - remainder);
  const scanStarted = runtimeNowMs();
  for (let localY = 0; localY < request.core.height; localY += 1) {
    const rowStart = localY * xorWordsPerRow;
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      const wordIndex = rowStart + wordX;
      let word = xorWords[wordIndex] >>> 0;
      if (wordX === xorWordsPerRow - 1) {
        if ((word & ~lastWordMask) !== 0) {
          throw new Error("Raster Mask morphology XOR plane contains non-zero tail bits");
        }
        word &= lastWordMask;
      }
      if (word === 0) continue;
      nonZeroWordCount += 1;
      changedPixels += popcount32(word);
      const firstBit = 31 - Math.clz32((word & -word) >>> 0);
      const lastBit = 31 - Math.clz32(word);
      minX = Math.min(minX, request.core.x + wordX * 32 + firstBit);
      minY = Math.min(minY, request.core.y + localY);
      maxX = Math.max(maxX, request.core.x + wordX * 32 + lastBit);
      maxY = Math.max(maxY, request.core.y + localY);
    }
  }
  const xorScanMs = runtimeNowMs() - scanStarted;
  const tilesPerRow = Math.ceil(sourceWidth / MASK_HISTORY_TILE_SIZE);
  const patchesByTile = new Map<number, MaskHistoryPatch>();
  const allocationStarted = runtimeNowMs();
  for (let localY = 0; localY < request.core.height; localY += 1) {
    const rowStart = localY * xorWordsPerRow;
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      const word =
        (xorWords[rowStart + wordX] &
          (wordX === xorWordsPerRow - 1 ? lastWordMask : 0xffff_ffff)) >>>
        0;
      if (word === 0) continue;
      const validBits = Math.min(32, request.core.width - wordX * 32);
      const absoluteWordX = request.core.x + wordX * 32;
      const absoluteY = request.core.y + localY;
      let sourceBit = 0;
      while (sourceBit < validBits) {
        const absoluteX = absoluteWordX + sourceBit;
        const tileX = Math.floor(absoluteX / MASK_HISTORY_TILE_SIZE);
        const tileY = Math.floor(absoluteY / MASK_HISTORY_TILE_SIZE);
        const segmentBits = Math.min(
          validBits - sourceBit,
          (tileX + 1) * MASK_HISTORY_TILE_SIZE - absoluteX,
        );
        const segmentWord = (word >>> sourceBit) & lowBitMask(segmentBits);
        if (segmentWord !== 0) {
          const key = tileY * tilesPerRow + tileX;
          if (!patchesByTile.has(key)) {
            const tileWidth = Math.min(
              MASK_HISTORY_TILE_SIZE,
              sourceWidth - tileX * MASK_HISTORY_TILE_SIZE,
            );
            const tileHeight = Math.min(
              MASK_HISTORY_TILE_SIZE,
              sourceHeight - tileY * MASK_HISTORY_TILE_SIZE,
            );
            patchesByTile.set(key, {
              tileX,
              tileY,
              width: tileWidth,
              height: tileHeight,
              xorBits: new Uint8Array(Math.ceil((tileWidth * tileHeight) / 8)),
            });
          }
        }
        sourceBit += segmentBits;
      }
    }
  }
  const xorPatchAllocateMs = runtimeNowMs() - allocationStarted;
  const scatterStarted = runtimeNowMs();
  for (let localY = 0; localY < request.core.height; localY += 1) {
    const rowStart = localY * xorWordsPerRow;
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      const word =
        (xorWords[rowStart + wordX] &
          (wordX === xorWordsPerRow - 1 ? lastWordMask : 0xffff_ffff)) >>>
        0;
      if (word === 0) continue;
      const validBits = Math.min(32, request.core.width - wordX * 32);
      const absoluteWordX = request.core.x + wordX * 32;
      const absoluteY = request.core.y + localY;
      let sourceBit = 0;
      while (sourceBit < validBits) {
        const absoluteX = absoluteWordX + sourceBit;
        const tileX = Math.floor(absoluteX / MASK_HISTORY_TILE_SIZE);
        const tileY = Math.floor(absoluteY / MASK_HISTORY_TILE_SIZE);
        const segmentBits = Math.min(
          validBits - sourceBit,
          (tileX + 1) * MASK_HISTORY_TILE_SIZE - absoluteX,
        );
        const segmentWord = (word >>> sourceBit) & lowBitMask(segmentBits);
        if (segmentWord !== 0) {
          const patch = patchesByTile.get(tileY * tilesPerRow + tileX);
          if (!patch) {
            throw new Error("Raster Mask morphology XOR patch allocation is incomplete");
          }
          const tileLocalX = absoluteX - tileX * MASK_HISTORY_TILE_SIZE;
          const tileLocalY = absoluteY - tileY * MASK_HISTORY_TILE_SIZE;
          orPackedBits(
            patch.xorBits,
            tileLocalY * patch.width + tileLocalX,
            segmentWord,
            segmentBits,
          );
        }
        sourceBit += segmentBits;
      }
    }
  }
  const patches = [...patchesByTile.values()].sort(
    (left, right) => left.tileY - right.tileY || left.tileX - right.tileX,
  );
  const xorPatchScatterMs = runtimeNowMs() - scatterStarted;
  const diffMs = runtimeNowMs() - started;
  return {
    changedPixels,
    changedBounds:
      changedPixels === 0
        ? null
        : { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 },
    patches,
    diffMs,
    xorOutputStrategy: "dense-word-scatter",
    xorTotalWords: xorWords.length,
    xorNonZeroWords: nonZeroWordCount,
    xorWordDensity: xorWords.length === 0 ? 0 : nonZeroWordCount / xorWords.length,
    xorTouchedTiles: patches.length,
    xorScanMs,
    xorPatchAllocateMs,
    xorPatchScatterMs,
    wordPatchBuildMs: xorPatchAllocateMs + xorPatchScatterMs,
  };
}

export function morphologyRasterMaskSessionRoi(
  session: RasterMaskWorkerSession,
  request: RasterMaskMorphologyRuntimeRequest,
): Omit<RasterMaskMorphologyRoiResponse, "kind" | "id" | "ok" | "sessionId" | "sha256"> {
  const totalStarted = runtimeNowMs();
  const denseBytes = estimateRasterMaskDenseCpuBytes(request);
  if (denseBytes.requiredBytes > request.cpuComputeBudgetBytes) {
    throw new Error("Raster Mask CPU compute budget is insufficient");
  }
  const prepared = prepareRasterMaskMorphologyRoi(session, request);
  const computeStarted = runtimeNowMs();
  const after = applyMaskMorphology(
    prepared.source,
    request.input.width,
    request.input.height,
    request.operation,
  ).alpha;
  const computeMs = runtimeNowMs() - computeStarted;
  const diff = diffRasterMaskMorphologyRoi(
    session,
    request,
    prepared.source,
    (inputIndex) => after[inputIndex] !== 0,
  );
  return {
    sourceRevision: request.sourceRevision,
    backend: "cpu",
    fallbackReason: request.backendPolicy === "cpu" ? "gate-disabled" : "initializing",
    changedPixels: diff.changedPixels,
    changedBounds: diff.changedBounds,
    patches: diff.patches,
    metrics: {
      totalMs: runtimeNowMs() - totalStarted,
      cpuStrategy: "dense",
      failureStage: null,
      inputPixels: request.input.width * request.input.height,
      corePixels: request.core.width * request.core.height,
      backendPrepareMs: prepared.materializeMs,
      prepareStrategy: "dense-cpu",
      directRleScanMs: 0,
      baseCacheFillMs: 0,
      packedAssembleMs: 0,
      dirtyOverlayMs: 0,
      baseCacheHitTiles: 0,
      baseCacheMissTiles: 0,
      baseCacheEvictedTiles: 0,
      baseCacheRetainedBytes: 0,
      sourceScratchCapacityBytes: 0,
      computeMs,
      diffOrPatchMs: diff.diffMs,
      xorOutputStrategy: "cpu-dense",
      xorTotalWords: 0,
      xorNonZeroWords: 0,
      xorWordDensity: 0,
      xorChangedPixels: diff.changedPixels,
      xorTouchedTiles: diff.patches.length,
      xorScanMs: 0,
      xorPatchAllocateMs: 0,
      xorPatchScatterMs: 0,
      wordPatchBuildMs: 0,
      gpuUploadSubmitMs: null,
      gpuReadbackMs: null,
      gpuPassMs: null,
      fallbackMaterializeMs: null,
      cpuBudgetBytes: request.cpuComputeBudgetBytes,
      gpuBudgetBytes: request.gpuBufferBudgetBytes,
      cpuTransientBytes: denseBytes.requiredBytes,
      denseTransientBytes: denseBytes.requiredBytes,
      packedIntermediateBytes: 0,
      patchUpperBoundBytes: denseBytes.patchUpperBoundBytes,
      inputAlphaBytes: prepared.source.byteLength,
      packedSourceBytes: 0,
      xorReadbackBytes: 0,
      allocatedGpuBytes: 0,
      gpuSourceCapacityBytes: 0,
      gpuXorCapacityBytes: 0,
      gpuReadbackCapacityBytes: 0,
      webGpuCircuitState: "eligible",
      webGpuCooldownRemainingMs: 0,
      webGpuConsecutiveFailures: 0,
      webGpuDeviceLost: 0,
    },
  };
}

const CELL_BASELINE_ONLY = 1;
const CELL_CURRENT_ONLY = 2;
const CELL_OVERLAP = 4;

function mergeColumnComparisonCells(
  current: RasterMaskWorkerSession,
  baseline: RasterMaskWorkerSession,
  rect: RasterMaskTileRect,
  sampleStep: number,
  flags: Uint8Array,
  rasterWidth: number,
): void {
  const [sourceHeight] = current.size;
  const rectEndY = rect.y + rect.height;
  for (let sourceX = rect.x; sourceX < rect.x + rect.width; sourceX += 1) {
    const columnStart = sourceX * sourceHeight;
    let offset = columnStart + rect.y;
    const columnEnd = columnStart + rectEndY;
    let currentIndex = runIndexAt(current.runEnds, offset);
    let baselineIndex = runIndexAt(baseline.runEnds, offset);
    const localX = Math.floor((sourceX - rect.x) / sampleStep);
    while (offset < columnEnd) {
      while (current.runEnds[currentIndex] <= offset) currentIndex += 1;
      while (baseline.runEnds[baselineIndex] <= offset) baselineIndex += 1;
      const next = Math.min(
        columnEnd,
        current.runEnds[currentIndex],
        baseline.runEnds[baselineIndex],
      );
      const localY0 = Math.floor((offset - columnStart - rect.y) / sampleStep);
      const localY1 = Math.floor((next - 1 - columnStart - rect.y) / sampleStep);
      const currentOn = (currentIndex & 1) === 1;
      const baselineOn = (baselineIndex & 1) === 1;
      const flag = currentOn
        ? baselineOn
          ? CELL_OVERLAP
          : CELL_CURRENT_ONLY
        : baselineOn
          ? CELL_BASELINE_ONLY
          : 0;
      if (flag !== 0) {
        for (let localY = localY0; localY <= localY1; localY += 1) {
          flags[localY * rasterWidth + localX] |= flag;
        }
      }
      offset = next;
    }
  }
}

function markCellYRange(
  flags: Uint8Array,
  rasterWidth: number,
  rect: RasterMaskTileRect,
  sampleStep: number,
  sourceX: number,
  sourceY0: number,
  sourceY1: number,
  bit: number,
): void {
  if (
    sourceX < rect.x ||
    sourceX >= rect.x + rect.width ||
    sourceY1 <= rect.y ||
    sourceY0 >= rect.y + rect.height
  )
    return;
  const clippedY0 = Math.max(rect.y, sourceY0);
  const clippedY1 = Math.min(rect.y + rect.height, sourceY1);
  if (clippedY1 <= clippedY0) return;
  const localX = Math.floor((sourceX - rect.x) / sampleStep);
  const localY0 = Math.floor((clippedY0 - rect.y) / sampleStep);
  const localY1 = Math.floor((clippedY1 - 1 - rect.y) / sampleStep);
  for (let localY = localY0; localY <= localY1; localY += 1) {
    flags[localY * rasterWidth + localX] |= bit;
  }
}

function markSessionBoundaryCells(
  session: RasterMaskWorkerSession,
  rect: RasterMaskTileRect,
  sampleStep: number,
  flags: Uint8Array,
  rasterWidth: number,
  bit: number,
): void {
  const [sourceHeight, sourceWidth] = session.size;
  const rectEndY = rect.y + rect.height;

  for (let sourceX = rect.x; sourceX < rect.x + rect.width; sourceX += 1) {
    const columnStart = sourceX * sourceHeight;
    const columnEnd = columnStart + sourceHeight;
    let offset = columnStart;
    let runIndex = runIndexAt(session.runEnds, offset);
    while (offset < columnEnd) {
      while (session.runEnds[runIndex] <= offset) runIndex += 1;
      const next = Math.min(columnEnd, session.runEnds[runIndex]);
      if ((runIndex & 1) === 1) {
        const sourceY0 = offset - columnStart;
        const sourceY1 = next - columnStart;
        if (sourceY0 === 0 || offset > columnStart) {
          markCellYRange(
            flags,
            rasterWidth,
            rect,
            sampleStep,
            sourceX,
            sourceY0,
            sourceY0 + 1,
            bit,
          );
        }
        if (sourceY1 === sourceHeight || next < columnEnd) {
          markCellYRange(
            flags,
            rasterWidth,
            rect,
            sampleStep,
            sourceX,
            sourceY1 - 1,
            sourceY1,
            bit,
          );
        }
      }
      offset = next;
    }
  }

  const firstRightX = Math.max(1, rect.x);
  const lastRightX = Math.min(sourceWidth - 1, rect.x + rect.width);
  for (let rightX = firstRightX; rightX <= lastRightX; rightX += 1) {
    const leftX = rightX - 1;
    const leftColumnStart = leftX * sourceHeight;
    const rightColumnStart = rightX * sourceHeight;
    let sourceY = rect.y;
    let leftIndex = runIndexAt(session.runEnds, leftColumnStart + sourceY);
    let rightIndex = runIndexAt(session.runEnds, rightColumnStart + sourceY);
    while (sourceY < rectEndY) {
      const leftOffset = leftColumnStart + sourceY;
      const rightOffset = rightColumnStart + sourceY;
      while (session.runEnds[leftIndex] <= leftOffset) leftIndex += 1;
      while (session.runEnds[rightIndex] <= rightOffset) rightIndex += 1;
      const nextY = Math.min(
        rectEndY,
        session.runEnds[leftIndex] - leftColumnStart,
        session.runEnds[rightIndex] - rightColumnStart,
      );
      const leftOn = (leftIndex & 1) === 1;
      const rightOn = (rightIndex & 1) === 1;
      if (leftOn !== rightOn) {
        markCellYRange(
          flags,
          rasterWidth,
          rect,
          sampleStep,
          leftOn ? leftX : rightX,
          sourceY,
          nextY,
          bit,
        );
      }
      sourceY = nextY;
    }
  }

  if (rect.x === 0) {
    let offset = rect.y;
    let runIndex = runIndexAt(session.runEnds, offset);
    while (offset < rectEndY) {
      while (session.runEnds[runIndex] <= offset) runIndex += 1;
      const next = Math.min(rectEndY, session.runEnds[runIndex]);
      if ((runIndex & 1) === 1) {
        markCellYRange(flags, rasterWidth, rect, sampleStep, 0, offset, next, bit);
      }
      offset = next;
    }
  }
  if (rect.x + rect.width === sourceWidth) {
    const sourceX = sourceWidth - 1;
    const columnStart = sourceX * sourceHeight;
    let offset = columnStart + rect.y;
    const end = columnStart + rectEndY;
    let runIndex = runIndexAt(session.runEnds, offset);
    while (offset < end) {
      while (session.runEnds[runIndex] <= offset) runIndex += 1;
      const next = Math.min(end, session.runEnds[runIndex]);
      if ((runIndex & 1) === 1) {
        markCellYRange(
          flags,
          rasterWidth,
          rect,
          sampleStep,
          sourceX,
          offset - columnStart,
          next - columnStart,
          bit,
        );
      }
      offset = next;
    }
  }
}

function boundaryAt(
  alpha: Uint8Array,
  expanded: RasterMaskTileRect,
  sourceWidth: number,
  sourceHeight: number,
  x: number,
  y: number,
): boolean {
  const localX = x - expanded.x;
  const localY = y - expanded.y;
  if (alpha[localY * expanded.width + localX] === 0) return false;
  for (const [dx, dy] of [
    [-1, 0],
    [1, 0],
    [0, -1],
    [0, 1],
  ] as const) {
    const neighborX = x + dx;
    const neighborY = y + dy;
    if (neighborX < 0 || neighborX >= sourceWidth || neighborY < 0 || neighborY >= sourceHeight) {
      return true;
    }
    const neighborLocalX = neighborX - expanded.x;
    const neighborLocalY = neighborY - expanded.y;
    if (alpha[neighborLocalY * expanded.width + neighborLocalX] === 0) return true;
  }
  return false;
}

export function compareRasterMaskSessionTile(
  current: RasterMaskWorkerSession,
  baseline: RasterMaskWorkerSession,
  rect: RasterMaskTileRect,
  mode: RasterMaskCompareMode,
  sampleStep = 1,
): Uint8Array {
  validateTileRect(current, rect);
  validateTileRect(baseline, rect);
  if (current.size[0] !== baseline.size[0] || current.size[1] !== baseline.size[1]) {
    throw new Error("mask comparison sessions have different dimensions");
  }
  if (!Number.isSafeInteger(sampleStep) || sampleStep < 1) {
    throw new Error("mask comparison sample step must be a positive integer");
  }
  if (sampleStep > 1) {
    const rasterWidth = Math.ceil(rect.width / sampleStep);
    const rasterHeight = Math.ceil(rect.height / sampleStep);
    const codes = new Uint8Array(rasterWidth * rasterHeight);
    if (mode === "boundary") {
      markSessionBoundaryCells(baseline, rect, sampleStep, codes, rasterWidth, 1);
      markSessionBoundaryCells(current, rect, sampleStep, codes, rasterWidth, 2);
      return codes;
    }
    const flags = new Uint8Array(codes.length);
    mergeColumnComparisonCells(current, baseline, rect, sampleStep, flags, rasterWidth);
    for (let index = 0; index < flags.length; index += 1) {
      const flag = flags[index];
      if (mode === "overlay") {
        codes[index] =
          (flag & (CELL_BASELINE_ONLY | CELL_OVERLAP) ? 1 : 0) |
          (flag & (CELL_CURRENT_ONLY | CELL_OVERLAP) ? 2 : 0);
      } else if (mode === "xor") {
        codes[index] = (flag & CELL_BASELINE_ONLY ? 1 : 0) | (flag & CELL_CURRENT_ONLY ? 2 : 0);
      } else if (mode === "added") {
        codes[index] = flag & CELL_CURRENT_ONLY ? 2 : 0;
      } else {
        codes[index] = flag & CELL_BASELINE_ONLY ? 1 : 0;
      }
    }
    return codes;
  }
  const [sourceHeight, sourceWidth] = current.size;
  const expanded =
    mode === "boundary"
      ? {
          x: Math.max(0, rect.x - 1),
          y: Math.max(0, rect.y - 1),
          width: Math.min(sourceWidth, rect.x + rect.width + 1) - Math.max(0, rect.x - 1),
          height: Math.min(sourceHeight, rect.y + rect.height + 1) - Math.max(0, rect.y - 1),
        }
      : rect;
  const currentAlpha = decodeRasterMaskSessionTile(current, expanded);
  const baselineAlpha = decodeRasterMaskSessionTile(baseline, expanded);
  const codes = new Uint8Array(rect.width * rect.height);
  for (let localY = 0; localY < rect.height; localY += 1) {
    for (let localX = 0; localX < rect.width; localX += 1) {
      const x = rect.x + localX;
      const y = rect.y + localY;
      const expandedIndex = (y - expanded.y) * expanded.width + x - expanded.x;
      const currentOn =
        mode === "boundary"
          ? boundaryAt(currentAlpha, expanded, sourceWidth, sourceHeight, x, y)
          : currentAlpha[expandedIndex] > 0;
      const baselineOn =
        mode === "boundary"
          ? boundaryAt(baselineAlpha, expanded, sourceWidth, sourceHeight, x, y)
          : baselineAlpha[expandedIndex] > 0;
      let code = (baselineOn ? 1 : 0) | (currentOn ? 2 : 0);
      if (mode === "xor" && code === 3) code = 0;
      if (mode === "added" && code !== 2) code = 0;
      if (mode === "removed" && code !== 1) code = 0;
      codes[localY * rect.width + localX] = code;
    }
  }
  return codes;
}

interface TileColumnInterval {
  start: number;
  end: number;
  tile: RasterMaskTileOverride;
  localX: number;
}

export function mergeRasterMaskSessionTiles(
  session: RasterMaskWorkerSession,
  tiles: readonly RasterMaskTileOverride[],
): RasterMaskTransferredRle {
  const [height, width] = session.size;
  const intervals: TileColumnInterval[] = [];
  for (const tile of tiles) {
    validateTileRect(session, tile);
    if (!(tile.alpha instanceof Uint8Array) || tile.alpha.length !== tile.width * tile.height) {
      throw new Error("mask tile alpha length does not match its rectangle");
    }
    for (const value of tile.alpha) {
      if (value !== 0 && value !== 255) throw new Error("mask tile alpha must be binary");
    }
    for (let localX = 0; localX < tile.width; localX += 1) {
      const start = (tile.x + localX) * height + tile.y;
      intervals.push({ start, end: start + tile.height, tile, localX });
    }
  }
  intervals.sort((left, right) => left.start - right.start || left.end - right.end);
  let previousEnd = 0;
  for (let index = 1; index < intervals.length; index += 1) {
    previousEnd = Math.max(previousEnd, intervals[index - 1].end);
    if (intervals[index].start < previousEnd) {
      throw new Error("mask tile overrides overlap");
    }
  }

  const counts: number[] = [];
  let outputForeground = false;
  let outputRunLength = 0;
  const append = (foreground: boolean, length: number) => {
    if (length <= 0) return;
    if (foreground === outputForeground) outputRunLength += length;
    else {
      counts.push(outputRunLength);
      outputRunLength = length;
      outputForeground = foreground;
    }
  };

  let cursor = 0;
  let baseRunIndex = 0;
  const advanceBaseTo = (target: number, emit: boolean) => {
    while (cursor < target) {
      while (baseRunIndex < session.runEnds.length && cursor >= session.runEnds[baseRunIndex]) {
        baseRunIndex += 1;
      }
      if (baseRunIndex >= session.runEnds.length)
        throw new Error("mask RLE ended before the image boundary");
      const length = Math.min(target, session.runEnds[baseRunIndex]) - cursor;
      if (emit) append((baseRunIndex & 1) === 1, length);
      cursor += length;
    }
  };

  for (const interval of intervals) {
    advanceBaseTo(interval.start, true);
    const { tile, localX } = interval;
    let localY = 0;
    while (localY < tile.height) {
      const foreground = tile.alpha[localY * tile.width + localX] > 0;
      let endY = localY + 1;
      while (endY < tile.height && tile.alpha[endY * tile.width + localX] > 0 === foreground)
        endY += 1;
      append(foreground, endY - localY);
      localY = endY;
    }
    advanceBaseTo(interval.end, false);
  }
  advanceBaseTo(width * height, true);
  counts.push(outputRunLength);
  return {
    size: [session.size[0], session.size[1]],
    counts: Uint32Array.from(counts),
  };
}
