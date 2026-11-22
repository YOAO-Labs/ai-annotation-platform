import polygonClipping, { type Polygon } from "polygon-clipping";

import { simplifyPolygon } from "./simplify";

export interface MaskLike {
  readonly width: number;
  readonly height: number;
  readonly data: Uint8Array | Uint8ClampedArray;
}

export interface MaskToPolygonOptions {
  threshold?: number;
  simplifyEpsilon?: number;
  pickLargest?: boolean;
}

export interface MaskToPolygonResult {
  points: [number, number][];
  multipleComponents: boolean;
  lossy: boolean;
  droppedComponents?: number;
  droppedHoles?: number;
  lossyReason?: string;
}

const LOSSY_REASON =
  "Mask 含多个连通分量或孔洞，当前 polygon 提交会丢失几何；请先擦除额外区域或填平孔洞";

function isSolid(mask: MaskLike, x: number, y: number, threshold: number): boolean {
  if (x < 0 || y < 0 || x >= mask.width || y >= mask.height) return false;
  return mask.data[y * mask.width + x] >= threshold;
}

function traceBoundary(
  mask: MaskLike,
  sx: number,
  sy: number,
  threshold: number,
): [number, number][] {
  const dirs: ReadonlyArray<readonly [number, number]> = [
    [-1, 0],
    [-1, -1],
    [0, -1],
    [1, -1],
    [1, 0],
    [1, 1],
    [0, 1],
    [-1, 1],
  ];
  const out: [number, number][] = [];
  let cx = sx,
    cy = sy;
  let dir = 0;
  out.push([cx, cy]);
  for (let step = 0; step < mask.width * mask.height * 4; step++) {
    let found = false;
    for (let k = 0; k < 8; k++) {
      const di = (dir + 1 + k) % 8;
      const [dx, dy] = dirs[di];
      const nx = cx + dx,
        ny = cy + dy;
      if (isSolid(mask, nx, ny, threshold)) {
        if (nx === sx && ny === sy && out.length > 1) {
          return out;
        }
        cx = nx;
        cy = ny;
        dir = (di + 4) % 8;
        out.push([cx, cy]);
        found = true;
        break;
      }
    }
    if (!found) return out;
  }
  return out;
}

function floodComponent(
  mask: MaskLike,
  sx: number,
  sy: number,
  threshold: number,
  visited: Uint8Array,
): number {
  const { width, height } = mask;
  const stack: number[] = [sy * width + sx];
  let count = 0;
  while (stack.length) {
    const idx = stack.pop()!;
    if (visited[idx]) continue;
    visited[idx] = 1;
    const x = idx % width;
    const y = Math.floor(idx / width);
    if (!isSolid(mask, x, y, threshold)) continue;
    count++;
    if (x > 0) stack.push(idx - 1);
    if (x < width - 1) stack.push(idx + 1);
    if (y > 0) stack.push(idx - width);
    if (y < height - 1) stack.push(idx + width);
  }
  return count;
}

function findComponents(
  mask: MaskLike,
  threshold: number,
): Array<{ sx: number; sy: number; count: number }> {
  const { width, height } = mask;
  const visited = new Uint8Array(width * height);
  const out: Array<{ sx: number; sy: number; count: number }> = [];
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = y * width + x;
      if (visited[idx]) continue;
      if (!isSolid(mask, x, y, threshold)) {
        visited[idx] = 1;
        continue;
      }
      const count = floodComponent(mask, x, y, threshold, visited);
      out.push({ sx: x, sy: y, count });
    }
  }
  return out;
}

function countBackgroundHoles(mask: MaskLike, threshold: number): number {
  const { width, height } = mask;
  const visited = new Uint8Array(width * height);
  const stack: number[] = [];

  const enqueueBackground = (x: number, y: number) => {
    const idx = y * width + x;
    if (!visited[idx] && !isSolid(mask, x, y, threshold)) stack.push(idx);
  };
  const floodBackground = () => {
    while (stack.length) {
      const idx = stack.pop()!;
      if (visited[idx]) continue;
      visited[idx] = 1;
      const x = idx % width;
      const y = Math.floor(idx / width);
      if (isSolid(mask, x, y, threshold)) continue;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (dx === 0 && dy === 0) continue;
          const nx = x + dx;
          const ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          const nextIdx = ny * width + nx;
          if (!visited[nextIdx] && !isSolid(mask, nx, ny, threshold)) stack.push(nextIdx);
        }
      }
    }
  };

  for (let x = 0; x < width; x++) {
    enqueueBackground(x, 0);
    if (height > 1) enqueueBackground(x, height - 1);
  }
  for (let y = 1; y < height - 1; y++) {
    enqueueBackground(0, y);
    if (width > 1) enqueueBackground(width - 1, y);
  }
  floodBackground();

  let holes = 0;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = y * width + x;
      if (visited[idx] || isSolid(mask, x, y, threshold)) continue;
      holes++;
      stack.push(idx);
      floodBackground();
    }
  }
  return holes;
}

export function maskToPolygon(
  mask: MaskLike,
  opts: MaskToPolygonOptions = {},
): MaskToPolygonResult {
  const threshold = opts.threshold ?? 128;
  const epsilon = opts.simplifyEpsilon ?? 1;

  const components = findComponents(mask, threshold);
  if (components.length === 0) {
    return { points: [], multipleComponents: false, lossy: false };
  }
  const multipleComponents = components.length > 1;
  const holeCount = countBackgroundHoles(mask, threshold);
  const lossy = multipleComponents || holeCount > 0;
  const droppedComponents = multipleComponents ? components.length - 1 : undefined;
  const droppedHoles = holeCount > 0 ? holeCount : undefined;
  const lossyReason = lossy ? LOSSY_REASON : undefined;

  const target = components.reduce((acc, c) => (c.count > acc.count ? c : acc), components[0]);
  let ring = traceBoundary(mask, target.sx, target.sy, threshold);
  if (ring.length < 3) {
    return {
      points: ring,
      multipleComponents,
      lossy,
      droppedComponents,
      droppedHoles,
      lossyReason,
    };
  }

  try {
    const closed: [number, number][] = [...ring];
    if (
      closed[0][0] !== closed[closed.length - 1][0] ||
      closed[0][1] !== closed[closed.length - 1][1]
    ) {
      closed.push([closed[0][0], closed[0][1]]);
    }
    const poly: Polygon = [closed];
    const unioned = polygonClipping.union([poly]);
    if (unioned.length > 0 && unioned[0].length > 0) {
      ring = unioned[0][0].map(([x, y]) => [x, y] as [number, number]);
      if (
        ring.length > 1 &&
        ring[0][0] === ring[ring.length - 1][0] &&
        ring[0][1] === ring[ring.length - 1][1]
      ) {
        ring.pop();
      }
    }
  } catch {
    // polygon-clipping 失败时退回原 ring
  }

  if (epsilon > 0 && ring.length > 3) {
    ring = simplifyPolygon(ring, epsilon);
  }
  const dedup: [number, number][] = [];
  for (const p of ring) {
    const last = dedup[dedup.length - 1];
    if (!last || last[0] !== p[0] || last[1] !== p[1]) dedup.push(p);
  }

  return {
    points: dedup,
    multipleComponents,
    lossy,
    droppedComponents,
    droppedHoles,
    lossyReason,
  };
}
