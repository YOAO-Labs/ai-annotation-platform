export type RasterMaskPackedCpuStrategy = "packed-direct" | "packed-separable";

export interface RasterMaskPackedSquareDilateOptions {
  sourceWords: Uint32Array;
  sourceWordsPerRow: number;
  inputWidth: number;
  inputHeight: number;
  coreOffsetX: number;
  coreOffsetY: number;
  coreWidth: number;
  coreHeight: number;
  radius: number;
}

export interface RasterMaskPackedSquareDilateResult {
  xorWords: Uint32Array;
  xorWordsPerRow: number;
  intermediateBytes: number;
  strategy: RasterMaskPackedCpuStrategy;
}

export interface RasterMaskPackedCpuByteEstimate {
  sourceChargeBytes: number;
  baseCacheRetainedBytes: number;
  horizontalIntermediateBytes: number;
  xorOutputBytes: number;
  patchUpperBoundBytes: number;
  requiredBytes: number;
}

function checkedPositiveInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`Raster Mask packed CPU ${label} must be a positive integer`);
  }
}

function checkedNonNegativeInteger(value: number, label: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`Raster Mask packed CPU ${label} must be a non-negative integer`);
  }
}

function checkedAdd(values: readonly number[], label: string): number {
  let total = 0;
  for (const value of values) {
    checkedNonNegativeInteger(value, label);
    total += value;
    if (!Number.isSafeInteger(total)) {
      throw new Error(`Raster Mask packed CPU ${label} exceeds the safe byte limit`);
    }
  }
  return total;
}

function validateOptions(options: RasterMaskPackedSquareDilateOptions): number {
  checkedPositiveInteger(options.inputWidth, "input width");
  checkedPositiveInteger(options.inputHeight, "input height");
  checkedPositiveInteger(options.coreWidth, "core width");
  checkedPositiveInteger(options.coreHeight, "core height");
  checkedNonNegativeInteger(options.coreOffsetX, "core x offset");
  checkedNonNegativeInteger(options.coreOffsetY, "core y offset");
  if (
    options.coreOffsetX + options.coreWidth > options.inputWidth ||
    options.coreOffsetY + options.coreHeight > options.inputHeight
  ) {
    throw new Error("Raster Mask packed CPU core rectangle is outside the input plane");
  }
  if (!Number.isInteger(options.radius) || options.radius < 1 || options.radius > 31) {
    throw new Error("Raster Mask packed CPU radius must be an integer in [1, 31]");
  }
  const expectedSourceWordsPerRow = Math.ceil(options.inputWidth / 32);
  if (
    !(options.sourceWords instanceof Uint32Array) ||
    options.sourceWordsPerRow !== expectedSourceWordsPerRow ||
    options.sourceWords.length !== expectedSourceWordsPerRow * options.inputHeight
  ) {
    throw new Error("Raster Mask packed CPU source plane does not match its dimensions");
  }
  return Math.ceil(options.coreWidth / 32);
}

function sourceWord(
  sourceWords: Uint32Array,
  sourceWordsPerRow: number,
  inputHeight: number,
  y: number,
  wordX: number,
): number {
  if (y < 0 || y >= inputHeight || wordX < 0 || wordX >= sourceWordsPerRow) return 0;
  return sourceWords[y * sourceWordsPerRow + wordX] >>> 0;
}

function alignedSourceWord(
  options: RasterMaskPackedSquareDilateOptions,
  y: number,
  bitOrigin: number,
): number {
  if (bitOrigin <= -32) return 0;
  if (bitOrigin < 0) {
    return (
      (sourceWord(options.sourceWords, options.sourceWordsPerRow, options.inputHeight, y, 0) <<
        -bitOrigin) >>>
      0
    );
  }
  const wordX = Math.floor(bitOrigin / 32);
  const shift = bitOrigin & 31;
  const lower = sourceWord(
    options.sourceWords,
    options.sourceWordsPerRow,
    options.inputHeight,
    y,
    wordX,
  );
  if (shift === 0) return lower;
  const upper = sourceWord(
    options.sourceWords,
    options.sourceWordsPerRow,
    options.inputHeight,
    y,
    wordX + 1,
  );
  return ((lower >>> shift) | (upper << (32 - shift))) >>> 0;
}

function horizontallyExpandedWord(
  options: RasterMaskPackedSquareDilateOptions,
  y: number,
  bitOrigin: number,
): number {
  const center = alignedSourceWord(options, y, bitOrigin);
  const previous = alignedSourceWord(options, y, bitOrigin - 32);
  const next = alignedSourceWord(options, y, bitOrigin + 32);
  let expanded = center;
  for (let shift = 1; shift <= options.radius; shift += 1) {
    expanded |= center << shift;
    expanded |= center >>> shift;
    expanded |= previous >>> (32 - shift);
    expanded |= next << (32 - shift);
  }
  return expanded >>> 0;
}

function tailMask(width: number): number {
  const remainder = width & 31;
  return remainder === 0 ? 0xffff_ffff : 0xffff_ffff >>> (32 - remainder);
}

export function squareDilatePackedXorDirect(
  options: RasterMaskPackedSquareDilateOptions,
): RasterMaskPackedSquareDilateResult {
  const xorWordsPerRow = validateOptions(options);
  const xorWords = new Uint32Array(xorWordsPerRow * options.coreHeight);
  const lastWordMask = tailMask(options.coreWidth);
  for (let localY = 0; localY < options.coreHeight; localY += 1) {
    const sourceY = options.coreOffsetY + localY;
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      const bitOrigin = options.coreOffsetX + wordX * 32;
      let dilated = 0;
      for (let dy = -options.radius; dy <= options.radius; dy += 1) {
        dilated |= horizontallyExpandedWord(options, sourceY + dy, bitOrigin);
      }
      const center = alignedSourceWord(options, sourceY, bitOrigin);
      const mask = wordX === xorWordsPerRow - 1 ? lastWordMask : 0xffff_ffff;
      xorWords[localY * xorWordsPerRow + wordX] = (dilated ^ center) & mask;
    }
  }
  return {
    xorWords,
    xorWordsPerRow,
    intermediateBytes: 0,
    strategy: "packed-direct",
  };
}

export function squareDilatePackedXorSeparable(
  options: RasterMaskPackedSquareDilateOptions,
): RasterMaskPackedSquareDilateResult {
  const xorWordsPerRow = validateOptions(options);
  const horizontal = new Uint32Array(xorWordsPerRow * options.inputHeight);
  for (let y = 0; y < options.inputHeight; y += 1) {
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      horizontal[y * xorWordsPerRow + wordX] = horizontallyExpandedWord(
        options,
        y,
        options.coreOffsetX + wordX * 32,
      );
    }
  }

  const xorWords = new Uint32Array(xorWordsPerRow * options.coreHeight);
  const lastWordMask = tailMask(options.coreWidth);
  for (let localY = 0; localY < options.coreHeight; localY += 1) {
    const sourceY = options.coreOffsetY + localY;
    for (let wordX = 0; wordX < xorWordsPerRow; wordX += 1) {
      let dilated = 0;
      const firstY = Math.max(0, sourceY - options.radius);
      const lastY = Math.min(options.inputHeight - 1, sourceY + options.radius);
      for (let y = firstY; y <= lastY; y += 1) {
        dilated |= horizontal[y * xorWordsPerRow + wordX];
      }
      const center = alignedSourceWord(options, sourceY, options.coreOffsetX + wordX * 32);
      const mask = wordX === xorWordsPerRow - 1 ? lastWordMask : 0xffff_ffff;
      xorWords[localY * xorWordsPerRow + wordX] = (dilated ^ center) & mask;
    }
  }
  return {
    xorWords,
    xorWordsPerRow,
    intermediateBytes: horizontal.byteLength,
    strategy: "packed-separable",
  };
}

export function estimateRasterMaskPackedCpuBytes(options: {
  inputWidth: number;
  inputHeight: number;
  coreWidth: number;
  coreHeight: number;
  sourceChargeBytes?: number;
  baseCacheRetainedBytes?: number;
}): RasterMaskPackedCpuByteEstimate {
  checkedPositiveInteger(options.inputWidth, "input width");
  checkedPositiveInteger(options.inputHeight, "input height");
  checkedPositiveInteger(options.coreWidth, "core width");
  checkedPositiveInteger(options.coreHeight, "core height");
  const inputWordsPerRow = Math.ceil(options.inputWidth / 32);
  const xorWordsPerRow = Math.ceil(options.coreWidth / 32);
  const sourceChargeBytes = options.sourceChargeBytes ?? inputWordsPerRow * options.inputHeight * 4;
  const baseCacheRetainedBytes = options.baseCacheRetainedBytes ?? 0;
  const horizontalIntermediateBytes = xorWordsPerRow * options.inputHeight * 4;
  const xorOutputBytes = xorWordsPerRow * options.coreHeight * 4;
  const patchUpperBoundBytes = Math.ceil((options.coreWidth * options.coreHeight) / 8);
  const requiredBytes = checkedAdd(
    [
      sourceChargeBytes,
      baseCacheRetainedBytes,
      horizontalIntermediateBytes,
      xorOutputBytes,
      patchUpperBoundBytes,
    ],
    "byte estimate",
  );
  return {
    sourceChargeBytes,
    baseCacheRetainedBytes,
    horizontalIntermediateBytes,
    xorOutputBytes,
    patchUpperBoundBytes,
    requiredBytes,
  };
}
