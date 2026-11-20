// KonvaPolygon holes / multi_polygon 渲染地基 (ADR-0052 D5 / D8)。
//
// transforms.geometryToShape 透传 holes / multiPolygon。
// 本模块用 canvas 2D 子路径和 evenodd 填充处理多环和镂空。
// 路径构造是纯函数，供 KonvaPolygon 的 <Line sceneFunc> 调用。
//
// 设计原则:
//   1. 纯函数, 只依赖一个 duck-typed 的 canvas-like context
//      (具备 beginPath / moveTo / lineTo / closePath 即可), 不耦合 Konva / DOM,
//      便于在 vitest + jsdom 下用 vi.fn() 直接断言路径构造 (见 evenOddFill.test.ts)。
//   2. 归一化坐标 [0,1] * (imgW, imgH) → 像素坐标, 与 KonvaPolygon 其余渲染一致。
//   3. 不负责 fill / stroke 调用本身; 调用方决定用 ctx.fillStrokeShape(shape)
//      (honors Konva shape 的 fillRule="evenodd" 属性) 还是 native ctx.fill('evenodd')。

/** Konva Context 或原生 CanvasRenderingContext2D 的最小子集 (duck-typed)。 */
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

/** 单个外环 + 可选内环 (holes)。归一化坐标 [0,1]。 */
export interface PolygonRing {
  points: ReadonlyArray<readonly [number, number]>;
  holes?: ReadonlyArray<ReadonlyArray<readonly [number, number]>>;
}

/** 归一化顶点 (x, y) → 像素坐标 (x*imgW, y*imgH)。 */
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

/**
 * 在 ctx 上画一个闭合环 (归一化坐标 → 像素)。ring.length < 2 时静默跳过 (空环不污染 path)。
 */
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

/**
 * 用同一组真实边界完成 even-odd 填充与描边。第二遍描边必须继续包含 holes：孔洞边缘也是
 * 实际对象边界，若只描外环，缩放或低透明度下会难以辨认镂空范围。
 */
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

/**
 * 便利封装:把单个 polygon (一个外环 + holes[]) 与可选 multi_polygon 数组合并成
 * buildEvenOddPaths 期望的 outerRings 列表。
 *
 * - 传 multiPolygon 时, 它已包含外环 + 各自 holes, 主 polygon (label/anchor 用) 不重复加入。
 * - 否则, 主 polygon 作为唯一外环, 附带其 holes。
 *
 * 这层合并放在纯函数里 (而非 KonvaPolygon 内联), 是为了:
 *   1. KonvaPolygon 的 sceneFunc 只剩 ~3 行, 易读;
 *   2. 合并逻辑可在 jsdom 下用 vi.fn() 直接断言 (见 evenOddFill.test.ts),
 *      不依赖 Konva 渲染栈;
 *   3. holes / multiPolygon 同时存在时 (理论上不会, 但防御式) 取 multi_polygon 优先。
 */
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
