import type { MultiPolygonGeometry, PolygonGeometry } from "@/types";
import { analyzeRasterMaskAlpha } from "../rasterMaskRender";
import { decodeCocoRle, encodeCocoRle, type CocoRle } from "./maskRle";

export type RegionGeometry = PolygonGeometry | MultiPolygonGeometry;

export interface MaskConversionReport {
  sourceType: RegionGeometry["type"] | "raster_mask";
  targetType: RegionGeometry["type"] | "raster_mask";
  sourceAreaPixels: number;
  targetAreaPixels: number;
  areaDeltaPixels: number;
  sourceComponents: number;
  targetComponents: number;
  sourceHoles: number;
  targetHoles: number;
  sourceVertices: number;
  targetVertices: number;
  changedPixels: number;
  droppedPixels: number;
  filteredComponents: number;
  filteredHoles: number;
  tolerance: number;
  lossy: boolean;
  reasons: string[];
}

export interface VectorToRasterPreview {
  rle: CocoRle;
  report: MaskConversionReport;
}

export interface RasterToVectorPreview {
  geometry: RegionGeometry;
  report: MaskConversionReport;
}

type Point = readonly [number, number];
type PixelPoint = [number, number];
type DirectedEdge = { from: PixelPoint; to: PixelPoint };

function ringsOf(geometry: RegionGeometry): Array<{ outer: Point[]; holes: Point[][] }> {
  if (geometry.type === "polygon") {
    return [{ outer: geometry.points, holes: geometry.holes ?? [] }];
  }
  return geometry.polygons.map((polygon) => ({
    outer: polygon.points,
    holes: polygon.holes ?? [],
  }));
}

function pointInRing(x: number, y: number, ring: readonly Point[]): boolean {
  let inside = false;
  for (let index = 0, previous = ring.length - 1; index < ring.length; previous = index++) {
    const [xi, yi] = ring[index];
    const [xj, yj] = ring[previous];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function rasterizeRegionGeometry(
  geometry: RegionGeometry,
  width: number,
  height: number,
): Uint8Array {
  if (!Number.isInteger(width) || width <= 0 || !Number.isInteger(height) || height <= 0) {
    throw new Error("image dimensions must be positive integers");
  }
  const components = ringsOf(geometry);
  const alpha = new Uint8Array(width * height);
  for (let y = 0; y < height; y += 1) {
    const ny = (y + 0.5) / height;
    for (let x = 0; x < width; x += 1) {
      const nx = (x + 0.5) / width;
      let filled = false;
      for (const component of components) {
        if (!pointInRing(nx, ny, component.outer)) continue;
        if (component.holes.some((hole) => pointInRing(nx, ny, hole))) continue;
        filled = true;
        break;
      }
      if (filled) alpha[y * width + x] = 255;
    }
  }
  return alpha;
}

function edgeKey(point: PixelPoint): string {
  return `${point[0]},${point[1]}`;
}

function direction(edge: DirectedEdge): number {
  const dx = edge.to[0] - edge.from[0];
  const dy = edge.to[1] - edge.from[1];
  if (dx === 1) return 0;
  if (dy === 1) return 1;
  if (dx === -1) return 2;
  return 3;
}

function boundaryEdges(alpha: Uint8Array, width: number, height: number): DirectedEdge[] {
  const solid = (x: number, y: number) =>
    x >= 0 && y >= 0 && x < width && y < height && alpha[y * width + x] !== 0;
  const edges: DirectedEdge[] = [];
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (!solid(x, y)) continue;
      if (!solid(x, y - 1)) edges.push({ from: [x, y], to: [x + 1, y] });
      if (!solid(x + 1, y)) edges.push({ from: [x + 1, y], to: [x + 1, y + 1] });
      if (!solid(x, y + 1)) edges.push({ from: [x + 1, y + 1], to: [x, y + 1] });
      if (!solid(x - 1, y)) edges.push({ from: [x, y + 1], to: [x, y] });
    }
  }
  return edges;
}

function traceBoundaryRings(alpha: Uint8Array, width: number, height: number): PixelPoint[][] {
  const edges = boundaryEdges(alpha, width, height);
  const outgoing = new Map<string, number[]>();
  edges.forEach((edge, index) => {
    const key = edgeKey(edge.from);
    outgoing.set(key, [...(outgoing.get(key) ?? []), index]);
  });
  const used = new Uint8Array(edges.length);
  const rings: PixelPoint[][] = [];

  for (let startIndex = 0; startIndex < edges.length; startIndex += 1) {
    if (used[startIndex]) continue;
    const start = edges[startIndex];
    const ring: PixelPoint[] = [start.from];
    let currentIndex = startIndex;
    for (let guard = 0; guard <= edges.length; guard += 1) {
      const current = edges[currentIndex];
      used[currentIndex] = 1;
      if (current.to[0] === start.from[0] && current.to[1] === start.from[1]) break;
      ring.push(current.to);
      const candidates = (outgoing.get(edgeKey(current.to)) ?? []).filter((index) => !used[index]);
      if (candidates.length === 0) throw new Error("Mask boundary is open");
      const currentDirection = direction(current);
      const priority = [1, 0, 3, 2];
      candidates.sort((left, right) => {
        const leftTurn = (direction(edges[left]) - currentDirection + 4) % 4;
        const rightTurn = (direction(edges[right]) - currentDirection + 4) % 4;
        return priority.indexOf(leftTurn) - priority.indexOf(rightTurn);
      });
      currentIndex = candidates[0];
    }
    if (ring.length >= 4) rings.push(ring);
  }
  return rings;
}

function signedArea(ring: readonly Point[]): number {
  let sum = 0;
  for (let index = 0; index < ring.length; index += 1) {
    const [x1, y1] = ring[index];
    const [x2, y2] = ring[(index + 1) % ring.length];
    sum += x1 * y2 - x2 * y1;
  }
  return sum / 2;
}

function normalizeRing(ring: PixelPoint[], width: number, height: number): [number, number][] {
  return ring.map(([x, y]) => [x / width, y / height]);
}

function geometryFromAlpha(alpha: Uint8Array, width: number, height: number): RegionGeometry {
  const pixelRings = traceBoundaryRings(alpha, width, height);
  const outers = pixelRings.filter((ring) => signedArea(ring) > 0);
  const holes = pixelRings.filter((ring) => signedArea(ring) < 0);
  if (outers.length === 0) throw new Error("Mask has no foreground boundary");
  const polygons: PolygonGeometry[] = outers.map((outer) => ({
    type: "polygon",
    points: normalizeRing(outer, width, height),
    holes: [],
  }));
  for (const hole of holes) {
    const normalized = normalizeRing(hole, width, height);
    const [sampleX, sampleY] = normalized[0];
    const owner = polygons.find((polygon) => pointInRing(sampleX, sampleY, polygon.points));
    if (!owner) throw new Error("Mask hole has no containing component");
    owner.holes!.push(normalized);
  }
  for (const polygon of polygons) {
    if (polygon.holes?.length === 0) delete polygon.holes;
  }
  return polygons.length === 1 ? polygons[0] : { type: "multi_polygon", polygons };
}

function geometryStats(geometry: RegionGeometry): { holes: number; vertices: number } {
  return ringsOf(geometry).reduce(
    (result, component) => ({
      holes: result.holes + component.holes.length,
      vertices:
        result.vertices +
        component.outer.length +
        component.holes.reduce((sum, hole) => sum + hole.length, 0),
    }),
    { holes: 0, vertices: 0 },
  );
}

function changedPixelCounts(source: Uint8Array, target: Uint8Array) {
  let changedPixels = 0;
  let droppedPixels = 0;
  for (let index = 0; index < source.length; index += 1) {
    const before = source[index] !== 0;
    const after = target[index] !== 0;
    if (before !== after) changedPixels += 1;
    if (before && !after) droppedPixels += 1;
  }
  return { changedPixels, droppedPixels };
}

export function vectorGeometryToRasterPreview(
  geometry: RegionGeometry,
  width: number,
  height: number,
): VectorToRasterPreview {
  const alpha = rasterizeRegionGeometry(geometry, width, height);
  const analysis = analyzeRasterMaskAlpha(alpha, width, height);
  if (analysis.area === 0) throw new Error("转换结果为空 Mask");
  const vectorStats = geometryStats(geometry);
  return {
    rle: encodeCocoRle(alpha, width, height),
    report: {
      sourceType: geometry.type,
      targetType: "raster_mask",
      sourceAreaPixels: analysis.area,
      targetAreaPixels: analysis.area,
      areaDeltaPixels: 0,
      sourceComponents: ringsOf(geometry).length,
      targetComponents: analysis.componentCount,
      sourceHoles: vectorStats.holes,
      targetHoles: vectorStats.holes,
      sourceVertices: vectorStats.vertices,
      targetVertices: 0,
      changedPixels: 0,
      droppedPixels: 0,
      filteredComponents: 0,
      filteredHoles: 0,
      tolerance: 0,
      lossy: false,
      reasons: [],
    },
  };
}

export function compareRegionToRasterResult(
  geometry: RegionGeometry,
  rle: CocoRle,
): MaskConversionReport {
  const [height, width] = rle.size;
  const source = rasterizeRegionGeometry(geometry, width, height);
  const target = decodeCocoRle(rle);
  const sourceAnalysis = analyzeRasterMaskAlpha(source, width, height);
  const targetAnalysis = analyzeRasterMaskAlpha(target, width, height);
  const sourceStats = geometryStats(geometry);
  const targetGeometry = targetAnalysis.area > 0 ? geometryFromAlpha(target, width, height) : null;
  const targetStats = targetGeometry ? geometryStats(targetGeometry) : { holes: 0, vertices: 0 };
  const pixelDiff = changedPixelCounts(source, target);
  const reasons = pixelDiff.changedPixels > 0 ? ["edited_pixels_changed"] : [];
  return {
    sourceType: geometry.type,
    targetType: "raster_mask",
    sourceAreaPixels: sourceAnalysis.area,
    targetAreaPixels: targetAnalysis.area,
    areaDeltaPixels: targetAnalysis.area - sourceAnalysis.area,
    sourceComponents: sourceAnalysis.componentCount,
    targetComponents: targetAnalysis.componentCount,
    sourceHoles: sourceStats.holes,
    targetHoles: targetStats.holes,
    sourceVertices: sourceStats.vertices,
    targetVertices: 0,
    changedPixels: pixelDiff.changedPixels,
    droppedPixels: pixelDiff.droppedPixels,
    filteredComponents: 0,
    filteredHoles: 0,
    tolerance: 0,
    lossy: pixelDiff.changedPixels > 0,
    reasons,
  };
}

export function rasterMaskToRegionPreview(rle: CocoRle): RasterToVectorPreview {
  const [height, width] = rle.size;
  const source = decodeCocoRle(rle);
  const sourceAnalysis = analyzeRasterMaskAlpha(source, width, height);
  if (sourceAnalysis.area === 0) throw new Error("空 Mask 不能转为矢量几何");
  const geometry = geometryFromAlpha(source, width, height);
  const target = rasterizeRegionGeometry(geometry, width, height);
  const targetAnalysis = analyzeRasterMaskAlpha(target, width, height);
  const pixelDiff = changedPixelCounts(source, target);
  const stats = geometryStats(geometry);
  const reasons = pixelDiff.changedPixels > 0 ? ["pixel_xor_changed"] : [];
  return {
    geometry,
    report: {
      sourceType: "raster_mask",
      targetType: geometry.type,
      sourceAreaPixels: sourceAnalysis.area,
      targetAreaPixels: targetAnalysis.area,
      areaDeltaPixels: targetAnalysis.area - sourceAnalysis.area,
      sourceComponents: sourceAnalysis.componentCount,
      targetComponents: ringsOf(geometry).length,
      sourceHoles: stats.holes,
      targetHoles: stats.holes,
      sourceVertices: 0,
      targetVertices: stats.vertices,
      changedPixels: pixelDiff.changedPixels,
      droppedPixels: pixelDiff.droppedPixels,
      filteredComponents: 0,
      filteredHoles: 0,
      tolerance: 0,
      lossy: pixelDiff.changedPixels > 0,
      reasons,
    },
  };
}

export function formatMaskConversionReport(report: MaskConversionReport): string {
  return [
    `${report.sourceType} → ${report.targetType}`,
    `面积: ${report.sourceAreaPixels} → ${report.targetAreaPixels} px (Δ ${report.areaDeltaPixels})`,
    `组件: ${report.sourceComponents} → ${report.targetComponents}`,
    `孔洞: ${report.sourceHoles} → ${report.targetHoles}`,
    `顶点: ${report.sourceVertices} → ${report.targetVertices}`,
    `XOR 变化: ${report.changedPixels} px · 丢失: ${report.droppedPixels} px`,
    `容差: ${report.tolerance} · ${report.lossy ? "有损" : "无损"}`,
    ...(report.reasons.length ? [`原因: ${report.reasons.join(", ")}`] : []),
  ].join("\n");
}
