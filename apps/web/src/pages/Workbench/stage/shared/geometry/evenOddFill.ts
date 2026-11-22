export interface PathCanvasContext {
  beginPath(): void;
  moveTo(x: number, y: number): void;
  lineTo(x: number, y: number): void;
  closePath(): void;
}

export interface FillStrokePathContext<TShape> extends PathCanvasContext {
  fillShape(shape: TShape): void;
  strokeShape(shape: TShape): void;
}

export interface PolygonRing {
  points: ReadonlyArray<readonly [number, number]>;
  holes?: ReadonlyArray<ReadonlyArray<readonly [number, number]>>;
}

function moveToPx(
  ctx: PathCanvasContext,
  px: number,
  py: number,
  imgW: number,
  imgH: number,
): void {
  ctx.moveTo(px * imgW, py * imgH);
}

function lineToPx(
  ctx: PathCanvasContext,
  px: number,
  py: number,
  imgW: number,
  imgH: number,
): void {
  ctx.lineTo(px * imgW, py * imgH);
}

function traceRing(
  ctx: PathCanvasContext,
  ring: ReadonlyArray<readonly [number, number]>,
  imgW: number,
  imgH: number,
): void {
  const n = ring.length;
  if (n < 2) return;
  const first = ring[0];
  moveToPx(ctx, first[0], first[1], imgW, imgH);
  for (let i = 1; i < n; i++) {
    const p = ring[i];
    lineToPx(ctx, p[0], p[1], imgW, imgH);
  }
  ctx.closePath();
}

/**
 * 把一组外环 (multi_polygon 的每个分量或单个 polygon 的外环) + 各自的 holes 全部画成
 * 子路径, 供后续单次 evenodd 填充。多分量时外环之间天然 evenodd 互不抵消 (分量按定义
 * 互不相交), 与 geometryMetrics.multiPolygonMetrics 的「逐环面积求和」语义一致。
 *
 * 调用方需在调用前 ctx.beginPath() 清空当前路径。本函数只负责追加子路径, 不闭合整条 path。
 *
 * @param ctx        duck-typed canvas context (Konva Context 或原生)
 * @param outerRings 每个 multi_polygon 分量的 { points, holes? }。单 polygon 调用方
 *                   传 [{ points, holes }] 即可。
 * @param imgW       图像像素宽 (归一化 → 像素换算)
 * @param imgH       图像像素高
 * @returns 追加的子路径数 (外环 + holes), 供测试 / 诊断用。
 */
export function buildEvenOddPaths(
  ctx: PathCanvasContext,
  outerRings: ReadonlyArray<PolygonRing>,
  imgW: number,
  imgH: number,
): number {
  let subpaths = 0;
  for (const ring of outerRings) {
    if (ring.points.length < 2) continue;
    traceRing(ctx, ring.points, imgW, imgH);
    subpaths++;
    if (ring.holes) {
      for (const hole of ring.holes) {
        if (hole.length < 2) continue;
        traceRing(ctx, hole, imgW, imgH);
        subpaths++;
      }
    }
  }
  return subpaths;
}

export function drawEvenOddShape<TShape>(
  ctx: FillStrokePathContext<TShape>,
  shape: TShape,
  outerRings: ReadonlyArray<PolygonRing>,
  imgW: number,
  imgH: number,
): { fillSubpaths: number; strokeSubpaths: number } {
  ctx.beginPath();
  const fillSubpaths = buildEvenOddPaths(ctx, outerRings, imgW, imgH);
  ctx.fillShape(shape);

  ctx.beginPath();
  const strokeSubpaths = buildEvenOddPaths(ctx, outerRings, imgW, imgH);
  ctx.strokeShape(shape);
  return { fillSubpaths, strokeSubpaths };
}

export function collectOuterRings(opts: {
  primaryPoints?: ReadonlyArray<readonly [number, number]>;
  holes?: ReadonlyArray<ReadonlyArray<readonly [number, number]>>;
  multiPolygon?: ReadonlyArray<PolygonRing>;
}): PolygonRing[] {
  const { primaryPoints, holes, multiPolygon } = opts;
  if (multiPolygon && multiPolygon.length > 0) {
    return multiPolygon.map((p) => ({
      points: p.points,
      holes: p.holes,
    }));
  }
  if (primaryPoints && primaryPoints.length >= 2) {
    return [{ points: primaryPoints, holes }];
  }
  return [];
}
