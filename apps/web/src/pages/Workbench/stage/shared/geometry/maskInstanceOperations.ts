import { analyzeRasterMaskAlpha } from "../rasterMaskRender";
import { labelMaskRegions, type MaskConnectivity, type MaskRegion } from "./maskOperations";

export type MaskInstanceOperationKind =
  | "slice_mask"
  | "copy_component"
  | "copy_keyframe"
  | "split_components"
  | "join_masks"
  | "overlap";

export type MaskInstanceOperationSpec =
  | { type: "slice_mask"; cutPath: MaskSlicePath }
  | {
      type: "copy_component";
      x: number;
      y: number;
      connectivity: MaskConnectivity;
    }
  | {
      type: "split_components";
      connectivity: MaskConnectivity;
      keep: "largest" | "hit";
      x?: number;
      y?: number;
    };

export interface MaskInstanceOperationPlan {
  kind: MaskInstanceOperationKind;
  sourceCount: number;
  resultCount: number;
  sourceAreas: number[];
  resultAreas: number[];
  primary: Uint8Array;
  created: Uint8Array[];
  focusAlpha: Uint8Array;
  cutPath?: MaskSlicePath;
}

export type MaskSlicePath = [[number, number], [number, number]];

export function planMaskSlice(
  source: Uint8Array,
  width: number,
  height: number,
  cutPath: MaskSlicePath,
): MaskInstanceOperationPlan {
  assertSource(source, width, height);
  if (
    cutPath.length !== 2 ||
    cutPath.some(
      (point) =>
        point.length !== 2 ||
        point.some((value) => !Number.isFinite(value) || value < 0 || value > 1),
    ) ||
    (cutPath[0][0] === cutPath[1][0] && cutPath[0][1] === cutPath[1][1])
  )
    throw new Error("切线需要两个不同的归一化有限端点");
  const [[ax, ay], [bx, by]] = cutPath;
  const dx = bx - ax,
    dy = by - ay;
  const left = new Uint8Array(source.length),
    right = new Uint8Array(source.length);
  let leftArea = 0,
    rightArea = 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const index = y * width + x;
      if (!source[index]) continue;
      if (dx * ((y + 0.5) / height - ay) - dy * ((x + 0.5) / width - ax) >= 0) {
        left[index] = 255;
        leftArea += 1;
      } else {
        right[index] = 255;
        rightArea += 1;
      }
    }
  }
  if (!leftArea || !rightArea) throw new Error("切线必须将来源分成两个非空 Mask");
  const keepLeft = leftArea >= rightArea;
  const primary = keepLeft ? left : right,
    created = keepLeft ? right : left;
  return {
    kind: "slice_mask",
    sourceCount: 1,
    resultCount: 2,
    sourceAreas: [leftArea + rightArea],
    resultAreas: keepLeft ? [leftArea, rightArea] : [rightArea, leftArea],
    primary,
    created: [created],
    focusAlpha: created.slice(),
    cutPath: [
      [ax, ay],
      [bx, by],
    ],
  };
}

function assertSource(source: Uint8Array, width: number, height: number): void {
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
    throw new Error("mask dimensions must be positive integers");
  }
  if (source.length !== width * height)
    throw new Error("mask alpha length must equal width * height");
  for (const value of source) {
    if (value !== 0 && value !== 255) throw new Error("mask alpha must be binary (0 or 255)");
  }
}

function alphaForRegion(region: MaskRegion, width: number, length: number): Uint8Array {
  const alpha = new Uint8Array(length);
  for (const span of region.spans) {
    alpha.fill(255, span.y * width + span.x0, span.y * width + span.x1);
  }
  return alpha;
}

function area(alpha: Uint8Array, width: number, height: number): number {
  return analyzeRasterMaskAlpha(alpha, width, height).area;
}

export function planMaskComponentCopy(
  source: Uint8Array,
  width: number,
  height: number,
  options: { x: number; y: number; connectivity: MaskConnectivity },
): MaskInstanceOperationPlan | null {
  assertSource(source, width, height);
  const selected = labelMaskRegions(source, width, height, {
    value: 255,
    connectivity: options.connectivity,
  }).hit(options.x, options.y);
  if (!selected) return null;
  const component = alphaForRegion(selected, width, source.length);
  const sourceArea = area(source, width, height);
  return {
    kind: "copy_component",
    sourceCount: 1,
    resultCount: 2,
    sourceAreas: [sourceArea],
    resultAreas: [sourceArea, selected.area],
    primary: source.slice(),
    created: [component],
    focusAlpha: component.slice(),
  };
}

export function planMaskComponentSplit(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    connectivity: MaskConnectivity;
    keep: "largest" | "hit";
    x?: number;
    y?: number;
  },
): MaskInstanceOperationPlan | null {
  assertSource(source, width, height);
  const labels = labelMaskRegions(source, width, height, {
    value: 255,
    connectivity: options.connectivity,
  });
  if (labels.regions.length < 2) return null;
  let primaryRegion: MaskRegion | null = null;
  if (options.keep === "hit") {
    if (!Number.isFinite(options.x) || !Number.isFinite(options.y)) {
      throw new Error("split hit coordinates must be finite");
    }
    primaryRegion = labels.hit(options.x!, options.y!);
    if (!primaryRegion) return null;
  } else {
    primaryRegion = labels.regions.reduce((best, region) =>
      region.area > best.area || (region.area === best.area && region.id < best.id) ? region : best,
    );
  }
  const primary = alphaForRegion(primaryRegion, width, source.length);
  const created = labels.regions
    .filter((region) => region.id !== primaryRegion.id)
    .map((region) => alphaForRegion(region, width, source.length));
  const focusAlpha = new Uint8Array(source.length);
  for (const alpha of created) {
    for (let index = 0; index < alpha.length; index += 1) {
      if (alpha[index]) focusAlpha[index] = 255;
    }
  }
  const resultAlphas = [primary, ...created];
  return {
    kind: "split_components",
    sourceCount: 1,
    resultCount: resultAlphas.length,
    sourceAreas: [area(source, width, height)],
    resultAreas: resultAlphas.map((result) => area(result, width, height)),
    primary,
    created,
    focusAlpha,
  };
}

export function planMaskJoin(
  sources: readonly Uint8Array[],
  width: number,
  height: number,
): MaskInstanceOperationPlan {
  if (sources.length < 2) throw new Error("joining masks requires at least two sources");
  for (const source of sources) assertSource(source, width, height);
  const primary = new Uint8Array(width * height);
  for (const source of sources) {
    for (let index = 0; index < source.length; index += 1) {
      if (source[index]) primary[index] = 255;
    }
  }
  return {
    kind: "join_masks",
    sourceCount: sources.length,
    resultCount: 1,
    sourceAreas: sources.map((source) => area(source, width, height)),
    resultAreas: [area(primary, width, height)],
    primary,
    created: [],
    focusAlpha: primary.slice(),
  };
}

export function applyMaskInstanceOperation(
  source: Uint8Array,
  width: number,
  height: number,
  operation: MaskInstanceOperationSpec,
): MaskInstanceOperationPlan | null {
  if (operation.type === "slice_mask")
    return planMaskSlice(source, width, height, operation.cutPath);
  if (operation.type === "copy_component") {
    return planMaskComponentCopy(source, width, height, operation);
  }
  return planMaskComponentSplit(source, width, height, operation);
}
