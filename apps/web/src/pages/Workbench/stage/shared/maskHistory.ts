import type { DirtyRect } from "./geometry/maskBuffer";

const MIB = 1024 * 1024;

export const MASK_HISTORY_TILE_SIZE = 512;
export const MASK_HISTORY_MAX_COMMANDS = 100;

const COMMAND_OVERHEAD_BYTES = 64;
const PATCH_OVERHEAD_BYTES = 32;

export interface MaskHistoryPatch {
  tileX: number;
  tileY: number;
  width: number;
  height: number;
  xorBits: Uint8Array;
}

export interface MaskHistoryCommand {
  name: string;
  sourceRevision: number;
  patches: MaskHistoryPatch[];
  changedPixels: number;
  chargedBytes: number;
}

export interface MaskHistoryResources {
  maxBytes: number;
  maxCommands: number;
  retainedBytes: number;
  undoCommands: number;
  redoCommands: number;
  evictedCommands: number;
  droppedCommands: number;
}

export interface MaskHistoryLifecycle {
  onRetain?: (command: MaskHistoryCommand) => boolean | void;
  onRelease?: (command: MaskHistoryCommand) => void;
}

interface CapturedTile {
  tileX: number;
  tileY: number;
  width: number;
  height: number;
  beforeBits: Uint8Array;
}

function assertDimensions(width: number, height: number): void {
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
    throw new Error("mask history dimensions must be positive integers");
  }
}

function tileKey(tileX: number, tileY: number): string {
  return `${tileY}:${tileX}`;
}

function tileDimensions(
  canvasWidth: number,
  canvasHeight: number,
  tileX: number,
  tileY: number,
): { width: number; height: number } {
  if (!Number.isInteger(tileX) || tileX < 0 || !Number.isInteger(tileY) || tileY < 0) {
    throw new Error("mask history tile coordinates must be non-negative integers");
  }
  const x0 = tileX * MASK_HISTORY_TILE_SIZE;
  const y0 = tileY * MASK_HISTORY_TILE_SIZE;
  const width = Math.min(MASK_HISTORY_TILE_SIZE, canvasWidth - x0);
  const height = Math.min(MASK_HISTORY_TILE_SIZE, canvasHeight - y0);
  if (width <= 0 || height <= 0) throw new Error("mask history tile is outside the canvas");
  return { width, height };
}

function packDenseTile(
  alpha: Uint8Array,
  canvasWidth: number,
  canvasHeight: number,
  tileX: number,
  tileY: number,
): { width: number; height: number; bits: Uint8Array } {
  assertDimensions(canvasWidth, canvasHeight);
  if (alpha.length !== canvasWidth * canvasHeight) {
    throw new Error("mask history alpha length must match the canvas");
  }
  const dimensions = tileDimensions(canvasWidth, canvasHeight, tileX, tileY);
  const bits = new Uint8Array(Math.ceil((dimensions.width * dimensions.height) / 8));
  const x0 = tileX * MASK_HISTORY_TILE_SIZE;
  const y0 = tileY * MASK_HISTORY_TILE_SIZE;
  for (let y = 0; y < dimensions.height; y += 1) {
    const sourceRow = (y0 + y) * canvasWidth + x0;
    const tileRow = y * dimensions.width;
    for (let x = 0; x < dimensions.width; x += 1) {
      if (alpha[sourceRow + x] === 0) continue;
      const bitIndex = tileRow + x;
      bits[bitIndex >> 3] |= 1 << (bitIndex & 7);
    }
  }
  return { ...dimensions, bits };
}

function packTileAlpha(alpha: Uint8Array, width: number, height: number): Uint8Array {
  if (alpha.length !== width * height) {
    throw new Error("mask history tile alpha length must match its dimensions");
  }
  const bits = new Uint8Array(Math.ceil((width * height) / 8));
  for (let index = 0; index < alpha.length; index += 1) {
    if (alpha[index] !== 0) bits[index >> 3] |= 1 << (index & 7);
  }
  return bits;
}

function countBits(value: number): number {
  let bits = value;
  bits -= (bits >> 1) & 0x55;
  bits = (bits & 0x33) + ((bits >> 2) & 0x33);
  return (bits + (bits >> 4)) & 0x0f;
}

export function maskHistoryBudgetBytes(deviceMemory?: number | null): number {
  if (
    deviceMemory != null &&
    Number.isFinite(deviceMemory) &&
    deviceMemory > 0 &&
    deviceMemory <= 2
  ) {
    return 16 * MIB;
  }
  if (deviceMemory != null && Number.isFinite(deviceMemory) && deviceMemory >= 8) {
    return 64 * MIB;
  }
  return 32 * MIB;
}

export function navigatorMaskHistoryBudgetBytes(): number {
  if (typeof navigator === "undefined") return maskHistoryBudgetBytes();
  const value = (navigator as Navigator & { deviceMemory?: unknown }).deviceMemory;
  return maskHistoryBudgetBytes(typeof value === "number" ? value : undefined);
}

export function chargeMaskHistoryPatches(patches: readonly MaskHistoryPatch[]): number {
  return (
    COMMAND_OVERHEAD_BYTES +
    patches.reduce((total, patch) => total + PATCH_OVERHEAD_BYTES + patch.xorBits.byteLength, 0)
  );
}

export function countMaskHistoryPatchPixels(patch: MaskHistoryPatch): number {
  let changedPixels = 0;
  for (const byte of patch.xorBits) changedPixels += countBits(byte);
  return changedPixels;
}

export function createMaskHistoryCommandFromPatches(
  name: string,
  sourceRevision: number,
  patches: readonly MaskHistoryPatch[],
): MaskHistoryCommand | null {
  const retained: MaskHistoryPatch[] = [];
  let changedPixels = 0;
  for (const patch of patches) {
    const tileChangedPixels = countMaskHistoryPatchPixels(patch);
    if (tileChangedPixels === 0) continue;
    retained.push(patch);
    changedPixels += tileChangedPixels;
  }
  if (retained.length === 0) return null;
  return {
    name,
    sourceRevision,
    patches: retained,
    changedPixels,
    chargedBytes: chargeMaskHistoryPatches(retained),
  };
}

export class MaskHistoryCheckpoint {
  private readonly tiles = new Map<string, CapturedTile>();

  constructor(
    private readonly canvasWidth: number,
    private readonly canvasHeight: number,
  ) {
    assertDimensions(canvasWidth, canvasHeight);
  }

  captureDenseRect(alpha: Uint8Array, rect: DirtyRect): void {
    const x0 = Math.max(0, Math.floor(rect.x0));
    const y0 = Math.max(0, Math.floor(rect.y0));
    const x1 = Math.min(this.canvasWidth, Math.ceil(rect.x1));
    const y1 = Math.min(this.canvasHeight, Math.ceil(rect.y1));
    if (x1 <= x0 || y1 <= y0) return;
    const firstTileX = Math.floor(x0 / MASK_HISTORY_TILE_SIZE);
    const lastTileX = Math.floor((x1 - 1) / MASK_HISTORY_TILE_SIZE);
    const firstTileY = Math.floor(y0 / MASK_HISTORY_TILE_SIZE);
    const lastTileY = Math.floor((y1 - 1) / MASK_HISTORY_TILE_SIZE);
    for (let tileY = firstTileY; tileY <= lastTileY; tileY += 1) {
      for (let tileX = firstTileX; tileX <= lastTileX; tileX += 1) {
        const key = tileKey(tileX, tileY);
        if (this.tiles.has(key)) continue;
        const tile = packDenseTile(alpha, this.canvasWidth, this.canvasHeight, tileX, tileY);
        this.tiles.set(key, {
          tileX,
          tileY,
          width: tile.width,
          height: tile.height,
          beforeBits: tile.bits,
        });
      }
    }
  }

  captureTile(
    tileX: number,
    tileY: number,
    width: number,
    height: number,
    alpha: Uint8Array,
  ): void {
    const expected = tileDimensions(this.canvasWidth, this.canvasHeight, tileX, tileY);
    if (width !== expected.width || height !== expected.height) {
      throw new Error("mask history tile dimensions do not match the canvas grid");
    }
    const key = tileKey(tileX, tileY);
    if (this.tiles.has(key)) return;
    this.tiles.set(key, {
      tileX,
      tileY,
      width,
      height,
      beforeBits: packTileAlpha(alpha, width, height),
    });
  }

  finish(
    name: string,
    sourceRevision: number,
    readTile: (tileX: number, tileY: number, width: number, height: number) => Uint8Array,
  ): MaskHistoryCommand | null {
    return this.finishBits(name, sourceRevision, (captured) =>
      packTileAlpha(
        readTile(captured.tileX, captured.tileY, captured.width, captured.height),
        captured.width,
        captured.height,
      ),
    );
  }

  private finishBits(
    name: string,
    sourceRevision: number,
    readBits: (captured: CapturedTile) => Uint8Array,
  ): MaskHistoryCommand | null {
    const patches: MaskHistoryPatch[] = [];
    let changedPixels = 0;
    try {
      for (const captured of this.tiles.values()) {
        const afterBits = readBits(captured);
        const xorBits = new Uint8Array(captured.beforeBits.length);
        let tileChangedPixels = 0;
        for (let index = 0; index < xorBits.length; index += 1) {
          const xor = captured.beforeBits[index] ^ afterBits[index];
          xorBits[index] = xor;
          tileChangedPixels += countBits(xor);
        }
        if (tileChangedPixels === 0) continue;
        changedPixels += tileChangedPixels;
        patches.push({
          tileX: captured.tileX,
          tileY: captured.tileY,
          width: captured.width,
          height: captured.height,
          xorBits,
        });
      }
    } finally {
      this.tiles.clear();
    }
    if (patches.length === 0) return null;
    return {
      name,
      sourceRevision,
      patches,
      changedPixels,
      chargedBytes: chargeMaskHistoryPatches(patches),
    };
  }

  finishDense(name: string, sourceRevision: number, alpha: Uint8Array): MaskHistoryCommand | null {
    return this.finishBits(
      name,
      sourceRevision,
      (captured) =>
        packDenseTile(alpha, this.canvasWidth, this.canvasHeight, captured.tileX, captured.tileY)
          .bits,
    );
  }
}

export function createDenseMaskHistoryCommand(
  name: string,
  sourceRevision: number,
  before: Uint8Array,
  after: Uint8Array,
  width: number,
  height: number,
  bounds?: DirtyRect | null,
): MaskHistoryCommand | null {
  if (before.length !== after.length) {
    throw new Error("mask history before/after alpha lengths must match");
  }
  const checkpoint = new MaskHistoryCheckpoint(width, height);
  checkpoint.captureDenseRect(before, bounds ?? { x0: 0, y0: 0, x1: width, y1: height });
  return checkpoint.finishDense(name, sourceRevision, after);
}

export class MaskHistoryStore {
  private undoStack: MaskHistoryCommand[] = [];
  private redoStack: MaskHistoryCommand[] = [];
  private retainedBytes = 0;
  private evictedCommands = 0;
  private droppedCommands = 0;

  constructor(
    private readonly maxBytes: number,
    private readonly maxCommands = MASK_HISTORY_MAX_COMMANDS,
    private readonly lifecycle: MaskHistoryLifecycle = {},
  ) {
    if (!Number.isFinite(maxBytes) || maxBytes <= 0) {
      throw new Error("mask history byte budget must be positive");
    }
    if (!Number.isInteger(maxCommands) || maxCommands <= 0) {
      throw new Error("mask history command limit must be a positive integer");
    }
  }

  get canUndo(): boolean {
    return this.undoStack.length > 0;
  }

  get canRedo(): boolean {
    return this.redoStack.length > 0;
  }

  clear(): void {
    for (const command of this.undoStack) this.lifecycle.onRelease?.(command);
    for (const command of this.redoStack) this.lifecycle.onRelease?.(command);
    this.undoStack = [];
    this.redoStack = [];
    this.retainedBytes = 0;
    this.evictedCommands = 0;
    this.droppedCommands = 0;
  }

  push(command: MaskHistoryCommand): boolean {
    if (!Number.isSafeInteger(command.chargedBytes) || command.chargedBytes <= 0) {
      throw new Error("mask history command charge must be a positive safe integer");
    }
    if (command.chargedBytes > this.maxBytes) {
      this.droppedCommands += 1;
      return false;
    }
    if (this.lifecycle.onRetain?.(command) === false) {
      this.droppedCommands += 1;
      return false;
    }
    for (const redo of this.redoStack) {
      this.retainedBytes -= redo.chargedBytes;
      this.lifecycle.onRelease?.(redo);
    }
    this.redoStack = [];
    this.undoStack.push(command);
    this.retainedBytes += command.chargedBytes;
    while (this.undoStack.length > this.maxCommands || this.retainedBytes > this.maxBytes) {
      const evicted = this.undoStack.shift();
      if (!evicted) break;
      this.retainedBytes -= evicted.chargedBytes;
      this.evictedCommands += 1;
      this.lifecycle.onRelease?.(evicted);
    }
    return this.undoStack.includes(command);
  }

  undo(apply: (command: MaskHistoryCommand) => void): MaskHistoryCommand | null {
    const command = this.undoStack[this.undoStack.length - 1];
    if (!command) return null;
    apply(command);
    this.undoStack.pop();
    this.redoStack.push(command);
    return command;
  }

  redo(apply: (command: MaskHistoryCommand) => void): MaskHistoryCommand | null {
    const command = this.redoStack[this.redoStack.length - 1];
    if (!command) return null;
    apply(command);
    this.redoStack.pop();
    this.undoStack.push(command);
    return command;
  }

  snapshot(): MaskHistoryResources {
    return {
      maxBytes: this.maxBytes,
      maxCommands: this.maxCommands,
      retainedBytes: this.retainedBytes,
      undoCommands: this.undoStack.length,
      redoCommands: this.redoStack.length,
      evictedCommands: this.evictedCommands,
      droppedCommands: this.droppedCommands,
    };
  }
}
