import { analyzeRasterMaskAlpha } from "../rasterMaskRender";

export type MaskConnectivity = 4 | 8;
export type MaskBrushShape = "circle" | "square";
export type MaskKernelShape = "disk" | "square";
export type MaskMorphologyOperation = "dilate" | "erode" | "open" | "close";

export interface MaskPixelBounds {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface MaskOperationReport {
  beforeArea: number;
  afterArea: number;
  changedPixels: number;
  beforeComponents: number;
  afterComponents: number;
  beforeHoles: number;
  afterHoles: number;
  bounds: MaskPixelBounds | null;
}

export interface MaskOperationResult {
  alpha: Uint8Array;
  report: MaskOperationReport;
}

export interface MaskRegionSpan {
  y: number;
  x0: number;
  x1: number;
}

export interface MaskRegion {
  id: number;
  value: 0 | 255;
  area: number;
  bounds: MaskPixelBounds;
  seed: { x: number; y: number };
  touchesBoundary: boolean;
  spans: MaskRegionSpan[];
}

export interface MaskRegionLabels {
  regions: MaskRegion[];
  hit: (x: number, y: number) => MaskRegion | null;
}

export type MaskOperationSpec =
  | {
      type: "polygon";
      points: ReadonlyArray<readonly [number, number]>;
      value: 0 | 255;
    }
  | {
      type: "flood_fill";
      x: number;
      y: number;
      value: 0 | 255;
      connectivity: MaskConnectivity;
    }
  | {
      type: "morphology";
      operation: MaskMorphologyOperation;
      kernelShape: MaskKernelShape;
      radius: number;
    }
  | {
      type: "component";
      action: "keep" | "delete";
      x: number;
      y: number;
      connectivity: MaskConnectivity;
    }
  | {
      type: "remove_small_components";
      maxArea: number;
      connectivity: MaskConnectivity;
    }
  | {
      type: "fill_holes";
      mode: "hit" | "max_area" | "all";
      x?: number;
      y?: number;
      maxArea?: number;
    }
  | {
      type: "smooth";
      kernelShape: MaskKernelShape;
      radius: number;
    };

function assertAlpha(alpha: Uint8Array, width: number, height: number): void {
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
    throw new Error("mask dimensions must be positive integers");
  }
  if (alpha.length !== width * height) {
    throw new Error("mask alpha length must equal width * height");
  }
  for (const value of alpha) {
    if (value !== 0 && value !== 255) throw new Error("mask alpha must be binary (0 or 255)");
  }
}

function changedBounds(
  before: Uint8Array,
  after: Uint8Array,
  width: number,
): {
  changedPixels: number;
  bounds: MaskPixelBounds | null;
} {
  let changedPixels = 0;
  let minX = width;
  let minY = Math.ceil(before.length / width);
  let maxX = -1;
  let maxY = -1;
  for (let index = 0; index < before.length; index += 1) {
    if (before[index] === after[index]) continue;
    changedPixels += 1;
    const x = index % width;
    const y = Math.floor(index / width);
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
  return {
    changedPixels,
    bounds: changedPixels === 0 ? null : { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 },
  };
}

function operationResult(
  before: Uint8Array,
  after: Uint8Array,
  width: number,
  height: number,
  connectivity: MaskConnectivity = 4,
): MaskOperationResult {
  const beforeAnalysis = analyzeRasterMaskAlpha(before, width, height, connectivity);
  const afterAnalysis = analyzeRasterMaskAlpha(after, width, height, connectivity);
  const changed = changedBounds(before, after, width);
  return {
    alpha: after,
    report: {
      beforeArea: beforeAnalysis.area,
      afterArea: afterAnalysis.area,
      changedPixels: changed.changedPixels,
      beforeComponents: beforeAnalysis.componentCount,
      afterComponents: afterAnalysis.componentCount,
      beforeHoles: beforeAnalysis.holeCount,
      afterHoles: afterAnalysis.holeCount,
      bounds: changed.bounds,
    },
  };
}

export function applyMaskBrush(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    cx: number;
    cy: number;
    radius: number;
    shape: MaskBrushShape;
    value: 0 | 255;
  },
): MaskOperationResult {
  assertAlpha(source, width, height);
  const { cx, cy, shape, value, radius } = options;
  if (![cx, cy, options.radius].every(Number.isFinite))
    throw new Error("brush values must be finite");
  if (!Number.isInteger(radius) || radius < 1 || radius > 200) {
    throw new Error("brush radius must be an integer in [1, 200]");
  }
  const after = source.slice();
  const x0 = Math.max(0, Math.floor(cx - radius));
  const y0 = Math.max(0, Math.floor(cy - radius));
  const x1 = Math.min(width - 1, Math.ceil(cx + radius));
  const y1 = Math.min(height - 1, Math.ceil(cy + radius));
  const radiusSquared = radius * radius;
  for (let y = y0; y <= y1; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      const dx = x - cx;
      const dy = y - cy;
      if (shape === "circle" && dx * dx + dy * dy > radiusSquared) continue;
      if (shape === "square" && (Math.abs(dx) > radius || Math.abs(dy) > radius)) continue;
      after[y * width + x] = value;
    }
  }
  return operationResult(source, after, width, height);
}

export function applyMaskPolygon(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    points: ReadonlyArray<readonly [number, number]>;
    value: 0 | 255;
  },
): MaskOperationResult {
  assertAlpha(source, width, height);
  const after = source.slice();
  const { points, value } = options;
  if (points.length < 3) return operationResult(source, after, width, height);
  if (points.some(([x, y]) => !Number.isFinite(x) || !Number.isFinite(y))) {
    throw new Error("polygon points must be finite");
  }

  let minY = Infinity;
  let maxY = -Infinity;
  for (const [, y] of points) {
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  }
  const firstY = Math.max(0, Math.ceil(minY - 0.5));
  const lastY = Math.min(height - 1, Math.ceil(maxY - 0.5) - 1);
  for (let y = firstY; y <= lastY; y += 1) {
    const sampleY = y + 0.5;
    const intersections: number[] = [];
    for (let index = 0; index < points.length; index += 1) {
      const [ax, ay] = points[index];
      const [bx, by] = points[(index + 1) % points.length];
      if (ay > sampleY === by > sampleY) continue;
      intersections.push(ax + ((sampleY - ay) * (bx - ax)) / (by - ay));
    }
    intersections.sort((left, right) => left - right);
    for (let index = 0; index + 1 < intersections.length; index += 2) {
      const firstX = Math.max(0, Math.ceil(intersections[index] - 0.5));
      const lastX = Math.min(width - 1, Math.ceil(intersections[index + 1] - 0.5) - 1);
      for (let x = firstX; x <= lastX; x += 1) after[y * width + x] = value;
    }
  }
  return operationResult(source, after, width, height);
}

function neighbours(connectivity: MaskConnectivity): ReadonlyArray<readonly [number, number]> {
  return connectivity === 8
    ? [
        [-1, -1],
        [0, -1],
        [1, -1],
        [-1, 0],
        [1, 0],
        [-1, 1],
        [0, 1],
        [1, 1],
      ]
    : [
        [0, -1],
        [-1, 0],
        [1, 0],
        [0, 1],
      ];
}

export function labelMaskRegions(
  source: Uint8Array,
  width: number,
  height: number,
  options: { value: 0 | 255; connectivity: MaskConnectivity },
): MaskRegionLabels {
  assertAlpha(source, width, height);
  const visited = new Uint8Array(source.length);
  const queue = new Int32Array(source.length);
  const offsets = neighbours(options.connectivity);
  const regions: MaskRegion[] = [];

  for (let seedIndex = 0; seedIndex < source.length; seedIndex += 1) {
    if (visited[seedIndex] || source[seedIndex] !== options.value) continue;
    let head = 0;
    let tail = 0;
    queue[tail] = seedIndex;
    tail += 1;
    visited[seedIndex] = 1;
    const members: number[] = [];
    let minX = width;
    let minY = height;
    let maxX = -1;
    let maxY = -1;
    let touchesBoundary = false;

    while (head < tail) {
      const index = queue[head];
      head += 1;
      members.push(index);
      const x = index % width;
      const y = Math.floor(index / width);
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
      touchesBoundary ||= x === 0 || y === 0 || x === width - 1 || y === height - 1;
      for (const [dx, dy] of offsets) {
        const nextX = x + dx;
        const nextY = y + dy;
        if (nextX < 0 || nextY < 0 || nextX >= width || nextY >= height) continue;
        const nextIndex = nextY * width + nextX;
        if (visited[nextIndex] || source[nextIndex] !== options.value) continue;
        visited[nextIndex] = 1;
        queue[tail] = nextIndex;
        tail += 1;
      }
    }

    members.sort((left, right) => left - right);
    const spans: MaskRegionSpan[] = [];
    for (let index = 0; index < members.length; ) {
      const member = members[index];
      const y = Math.floor(member / width);
      let x1 = member % width;
      index += 1;
      while (
        index < members.length &&
        Math.floor(members[index] / width) === y &&
        members[index] % width === x1 + 1
      ) {
        x1 += 1;
        index += 1;
      }
      spans.push({ y, x0: member % width, x1: x1 + 1 });
    }
    regions.push({
      id: regions.length + 1,
      value: options.value,
      area: members.length,
      bounds: { x0: minX, y0: minY, x1: maxX + 1, y1: maxY + 1 },
      seed: { x: seedIndex % width, y: Math.floor(seedIndex / width) },
      touchesBoundary,
      spans,
    });
  }

  return {
    regions,
    hit: (rawX: number, rawY: number) => {
      const x = Math.floor(rawX);
      const y = Math.floor(rawY);
      if (x < 0 || y < 0 || x >= width || y >= height || source[y * width + x] !== options.value) {
        return null;
      }
      return (
        regions.find(
          (region) =>
            x >= region.bounds.x0 &&
            x < region.bounds.x1 &&
            y >= region.bounds.y0 &&
            y < region.bounds.y1 &&
            region.spans.some((span) => span.y === y && x >= span.x0 && x < span.x1),
        ) ?? null
      );
    },
  };
}

export function applyMaskFloodFill(
  source: Uint8Array,
  width: number,
  height: number,
  options: { x: number; y: number; value: 0 | 255; connectivity: MaskConnectivity },
): MaskOperationResult {
  assertAlpha(source, width, height);
  const x = Math.floor(options.x);
  const y = Math.floor(options.y);
  const after = source.slice();
  if (x < 0 || y < 0 || x >= width || y >= height) {
    return operationResult(source, after, width, height, options.connectivity);
  }
  const target = source[y * width + x] as 0 | 255;
  if (target === options.value)
    return operationResult(source, after, width, height, options.connectivity);
  const labels = labelMaskRegions(source, width, height, {
    value: target,
    connectivity: options.connectivity,
  });
  const region = labels.hit(x, y);
  if (!region) return operationResult(source, after, width, height, options.connectivity);
  for (const span of region.spans) {
    after.fill(options.value, span.y * width + span.x0, span.y * width + span.x1);
  }
  return operationResult(source, after, width, height, options.connectivity);
}

function fillRegion(after: Uint8Array, width: number, region: MaskRegion, value: 0 | 255): void {
  for (const span of region.spans) {
    after.fill(value, span.y * width + span.x0, span.y * width + span.x1);
  }
}

export function applyMaskComponent(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    action: "keep" | "delete";
    x: number;
    y: number;
    connectivity: MaskConnectivity;
  },
): MaskOperationResult {
  assertAlpha(source, width, height);
  const labels = labelMaskRegions(source, width, height, {
    value: 255,
    connectivity: options.connectivity,
  });
  const selected = labels.hit(options.x, options.y);
  const after = options.action === "keep" ? new Uint8Array(source.length) : source.slice();
  if (selected) fillRegion(after, width, selected, options.action === "keep" ? 255 : 0);
  else if (options.action === "keep") after.set(source);
  return operationResult(source, after, width, height, options.connectivity);
}

export function removeSmallMaskComponents(
  source: Uint8Array,
  width: number,
  height: number,
  options: { maxArea: number; connectivity: MaskConnectivity },
): MaskOperationResult {
  assertAlpha(source, width, height);
  if (!Number.isInteger(options.maxArea) || options.maxArea < 1) {
    throw new Error("component area threshold must be a positive integer");
  }
  const after = source.slice();
  const labels = labelMaskRegions(source, width, height, {
    value: 255,
    connectivity: options.connectivity,
  });
  for (const region of labels.regions) {
    if (region.area <= options.maxArea) fillRegion(after, width, region, 0);
  }
  return operationResult(source, after, width, height, options.connectivity);
}

export function fillMaskHoles(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    mode: "hit" | "max_area" | "all";
    x?: number;
    y?: number;
    maxArea?: number;
  },
): MaskOperationResult {
  assertAlpha(source, width, height);
  if (options.mode === "hit" && (!Number.isFinite(options.x) || !Number.isFinite(options.y))) {
    throw new Error("hole hit coordinates must be finite");
  }
  if (options.mode === "max_area" && (!Number.isInteger(options.maxArea) || options.maxArea! < 1)) {
    throw new Error("hole area threshold must be a positive integer");
  }
  const labels = labelMaskRegions(source, width, height, { value: 0, connectivity: 4 });
  const after = source.slice();
  let selected: MaskRegion[];
  if (options.mode === "hit") {
    const hit = labels.hit(options.x!, options.y!);
    selected = hit && !hit.touchesBoundary ? [hit] : [];
  } else {
    selected = labels.regions.filter(
      (region) =>
        !region.touchesBoundary && (options.mode === "all" || region.area <= options.maxArea!),
    );
  }
  for (const region of selected) fillRegion(after, width, region, 255);
  return operationResult(source, after, width, height);
}

function squareMorphology(
  source: Uint8Array,
  width: number,
  height: number,
  radius: number,
  operation: "dilate" | "erode",
): Uint8Array {
  const horizontal = new Uint8Array(source.length);
  const output = new Uint8Array(source.length);
  const diameter = radius * 2 + 1;
  for (let y = 0; y < height; y += 1) {
    let count = 0;
    for (let x = -radius; x <= radius; x += 1) {
      if (x >= 0 && x < width && source[y * width + x] !== 0) count += 1;
    }
    for (let x = 0; x < width; x += 1) {
      const inside = x - radius >= 0 && x + radius < width;
      horizontal[y * width + x] =
        operation === "dilate" ? (count > 0 ? 255 : 0) : inside && count === diameter ? 255 : 0;
      const removeX = x - radius;
      const addX = x + radius + 1;
      if (removeX >= 0 && removeX < width && source[y * width + removeX] !== 0) count -= 1;
      if (addX >= 0 && addX < width && source[y * width + addX] !== 0) count += 1;
    }
  }
  for (let x = 0; x < width; x += 1) {
    let count = 0;
    for (let y = -radius; y <= radius; y += 1) {
      if (y >= 0 && y < height && horizontal[y * width + x] !== 0) count += 1;
    }
    for (let y = 0; y < height; y += 1) {
      const inside = y - radius >= 0 && y + radius < height;
      output[y * width + x] =
        operation === "dilate" ? (count > 0 ? 255 : 0) : inside && count === diameter ? 255 : 0;
      const removeY = y - radius;
      const addY = y + radius + 1;
      if (removeY >= 0 && removeY < height && horizontal[removeY * width + x] !== 0) count -= 1;
      if (addY >= 0 && addY < height && horizontal[addY * width + x] !== 0) count += 1;
    }
  }
  return output;
}

function distanceTransform1d(input: Float64Array, output: Float64Array): void {
  const length = input.length;
  const locations = new Int32Array(length);
  const boundaries = new Float64Array(length + 1);
  let envelope = 0;
  locations[0] = 0;
  boundaries[0] = Number.NEGATIVE_INFINITY;
  boundaries[1] = Number.POSITIVE_INFINITY;
  for (let q = 1; q < length; q += 1) {
    let intersection =
      (input[q] +
        q * q -
        (input[locations[envelope]] + locations[envelope] * locations[envelope])) /
      (2 * q - 2 * locations[envelope]);
    while (intersection <= boundaries[envelope]) {
      envelope -= 1;
      intersection =
        (input[q] +
          q * q -
          (input[locations[envelope]] + locations[envelope] * locations[envelope])) /
        (2 * q - 2 * locations[envelope]);
    }
    envelope += 1;
    locations[envelope] = q;
    boundaries[envelope] = intersection;
    boundaries[envelope + 1] = Number.POSITIVE_INFINITY;
  }
  envelope = 0;
  for (let q = 0; q < length; q += 1) {
    while (boundaries[envelope + 1] < q) envelope += 1;
    const distance = q - locations[envelope];
    output[q] = distance * distance + input[locations[envelope]];
  }
}

function withinDiskDistance(
  features: Uint8Array,
  width: number,
  height: number,
  radius: number,
): Uint8Array {
  const radiusSquared = radius * radius;
  const cap = radiusSquared + 1;
  const vertical = new Uint32Array(features.length);
  for (let x = 0; x < width; x += 1) {
    let nearest = -1;
    for (let y = 0; y < height; y += 1) {
      const index = y * width + x;
      if (features[index]) nearest = y;
      const distance = nearest < 0 ? cap : (y - nearest) * (y - nearest);
      vertical[index] = Math.min(cap, distance);
    }
    nearest = height;
    for (let y = height - 1; y >= 0; y -= 1) {
      const index = y * width + x;
      if (features[index]) nearest = y;
      const distance = nearest >= height ? cap : (nearest - y) * (nearest - y);
      vertical[index] = Math.min(vertical[index], cap, distance);
    }
  }
  const input = new Float64Array(width);
  const output = new Float64Array(width);
  const result = new Uint8Array(features.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) input[x] = vertical[y * width + x];
    distanceTransform1d(input, output);
    for (let x = 0; x < width; x += 1) {
      if (output[x] <= radiusSquared) result[y * width + x] = 1;
    }
  }
  return result;
}

function diskMorphology(
  source: Uint8Array,
  width: number,
  height: number,
  radius: number,
  operation: "dilate" | "erode",
): Uint8Array {
  if (operation === "dilate") {
    const features = Uint8Array.from(source, (value) => (value ? 1 : 0));
    const within = withinDiskDistance(features, width, height, radius);
    return Uint8Array.from(within, (value) => (value ? 255 : 0));
  }

  const paddedWidth = width + 2;
  const paddedHeight = height + 2;
  const background = new Uint8Array(paddedWidth * paddedHeight);
  background.fill(1);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      background[(y + 1) * paddedWidth + x + 1] = source[y * width + x] === 0 ? 1 : 0;
    }
  }
  const nearBackground = withinDiskDistance(background, paddedWidth, paddedHeight, radius);
  const result = new Uint8Array(source.length);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      result[y * width + x] = nearBackground[(y + 1) * paddedWidth + x + 1] ? 0 : 255;
    }
  }
  return result;
}

function oneMorphologyPass(
  source: Uint8Array,
  width: number,
  height: number,
  radius: number,
  kernelShape: MaskKernelShape,
  operation: "dilate" | "erode",
): Uint8Array {
  return kernelShape === "square"
    ? squareMorphology(source, width, height, radius, operation)
    : diskMorphology(source, width, height, radius, operation);
}

export function applyMaskMorphology(
  source: Uint8Array,
  width: number,
  height: number,
  options: {
    operation: MaskMorphologyOperation;
    kernelShape: MaskKernelShape;
    radius: number;
  },
): MaskOperationResult {
  assertAlpha(source, width, height);
  const { radius, kernelShape, operation } = options;
  if (!Number.isInteger(radius) || radius < 1 || radius > 32) {
    throw new Error("morphology radius must be an integer in [1, 32]");
  }
  let after: Uint8Array;
  if (operation === "open") {
    after = oneMorphologyPass(
      oneMorphologyPass(source, width, height, radius, kernelShape, "erode"),
      width,
      height,
      radius,
      kernelShape,
      "dilate",
    );
  } else if (operation === "close") {
    after = oneMorphologyPass(
      oneMorphologyPass(source, width, height, radius, kernelShape, "dilate"),
      width,
      height,
      radius,
      kernelShape,
      "erode",
    );
  } else {
    after = oneMorphologyPass(source, width, height, radius, kernelShape, operation);
  }
  return operationResult(source, after, width, height);
}

export function smoothMaskBoundary(
  source: Uint8Array,
  width: number,
  height: number,
  options: { kernelShape: MaskKernelShape; radius: number },
): MaskOperationResult {
  const closed = applyMaskMorphology(source, width, height, {
    operation: "close",
    kernelShape: options.kernelShape,
    radius: options.radius,
  }).alpha;
  const opened = applyMaskMorphology(closed, width, height, {
    operation: "open",
    kernelShape: options.kernelShape,
    radius: options.radius,
  }).alpha;
  return operationResult(source, opened, width, height);
}

export function applyMaskOperation(
  source: Uint8Array,
  width: number,
  height: number,
  operation: MaskOperationSpec,
): MaskOperationResult {
  if (operation.type === "polygon") {
    return applyMaskPolygon(source, width, height, operation);
  }
  if (operation.type === "flood_fill") {
    return applyMaskFloodFill(source, width, height, operation);
  }
  if (operation.type === "component") {
    return applyMaskComponent(source, width, height, operation);
  }
  if (operation.type === "remove_small_components") {
    return removeSmallMaskComponents(source, width, height, operation);
  }
  if (operation.type === "fill_holes") {
    return fillMaskHoles(source, width, height, operation);
  }
  if (operation.type === "smooth") {
    return smoothMaskBoundary(source, width, height, operation);
  }
  return applyMaskMorphology(source, width, height, operation);
}
