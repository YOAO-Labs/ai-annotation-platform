import type { Geometry, Keypoint } from "@/types";

/**
 * 选中信息卡 · 单条几何指标。
 * label 为短中文名(次要色),value 为主数据(主色),hint 为可选补充(更次要)。
 */
export interface Metric {
  label: string;
  value: string;
  hint?: string;
}

/** 千分位分组(整数四舍五入),供大像素面积/周长读数。 */
function group(n: number): string {
  return Math.round(n)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 把归一化分量渲染为百分比文本;<1% 时保留两位小数,避免被舍成 0.0%。 */
function pctText(frac: number): string {
  const v = frac * 100;
  if (v > 0 && v < 1) return `${v.toFixed(2)}%`;
  return `${v.toFixed(1)}%`;
}

/** 单维分量(宽 / 高 / 坐标)在缺像素维度时的相对值文本。 */
function dim(frac: number): string {
  return `${(frac * 100).toFixed(1)}%`;
}

/** 宽高比,统一约成「长边 : 1」或「1 : 长边」,避免出现 0.56 : 1 这类难读形式。 */
function aspectText(wpx: number, hpx: number): string {
  if (hpx === 0) return "—";
  const r = wpx / hpx;
  return r >= 1 ? `${r.toFixed(2)} : 1` : `1 : ${(1 / r).toFixed(2)}`;
}

/** 多边形归一化面积(鞋带公式,返回 [0,1] 占图比,坐标已归一化)。 */
export function shoelaceArea(points: [number, number][]): number {
  const n = points.length;
  if (n < 3) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % n];
    sum += x1 * y2 - x2 * y1;
  }
  return Math.abs(sum) / 2;
}

/** 多边形 / 折线像素周长。closed=true 时计入闭合边,折线 false 不闭合。 */
export function polylinePerimeterPx(
  points: [number, number][],
  imgW: number,
  imgH: number,
  closed: boolean,
): number {
  const n = points.length;
  if (n < 2) return 0;
  let p = 0;
  const limit = closed ? n : n - 1;
  for (let i = 0; i < limit; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % n];
    p += Math.hypot((x2 - x1) * imgW, (y2 - y1) * imgH);
  }
  return p;
}

function bboxMetrics(
  x: number,
  y: number,
  w: number,
  h: number,
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  const hasDims = !!(imgW && imgH);
  const metrics: Metric[] = [
    {
      label: "尺寸",
      value: hasDims
        ? `${Math.round(w * imgW!)}×${Math.round(h * imgH!)} px`
        : `${dim(w)}×${dim(h)}`,
    },
    { label: "占图", value: pctText(w * h) },
    {
      label: "位置",
      value: hasDims
        ? `${Math.round(x * imgW!)}, ${Math.round(y * imgH!)}`
        : `${dim(x)}, ${dim(y)}`,
      hint: "左上",
    },
  ];
  if (hasDims) {
    metrics.push({ label: "宽高比", value: aspectText(w * imgW!, h * imgH!) });
  }
  return metrics;
}

function rotatedMetrics(
  cx: number,
  cy: number,
  w: number,
  h: number,
  angle: number,
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  const hasDims = !!(imgW && imgH);
  return [
    {
      label: "尺寸",
      value: hasDims
        ? `${Math.round(w * imgW!)}×${Math.round(h * imgH!)} px`
        : `${dim(w)}×${dim(h)}`,
    },
    { label: "占图", value: pctText(w * h) },
    {
      label: "中心",
      value: hasDims
        ? `${Math.round(cx * imgW!)}, ${Math.round(cy * imgH!)}`
        : `${dim(cx)}, ${dim(cy)}`,
    },
    { label: "旋转角", value: `${Math.round(angle)}°`, hint: "顺时针" },
  ];
}

function polygonMetrics(
  points: [number, number][],
  holes: [number, number][][] | undefined,
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  const hasDims = !!(imgW && imgH);
  const holeArea = (holes ?? []).reduce((s, ring) => s + shoelaceArea(ring), 0);
  const netArea = Math.max(0, shoelaceArea(points) - holeArea);
  const holeVertices = (holes ?? []).reduce((sum, ring) => sum + ring.length, 0);
  const metrics: Metric[] = [
    {
      label: "顶点",
      value: `${points.length + holeVertices}`,
      hint: holes?.length ? `外环 ${points.length} + ${holes.length} 内环` : undefined,
    },
    { label: "占图", value: pctText(netArea) },
  ];
  if (hasDims) {
    metrics.push({ label: "面积", value: `≈ ${group(netArea * imgW! * imgH!)} px²` });
    const perimeter =
      polylinePerimeterPx(points, imgW!, imgH!, true) +
      (holes ?? []).reduce((sum, ring) => sum + polylinePerimeterPx(ring, imgW!, imgH!, true), 0);
    metrics.push({
      label: "周长",
      value: `≈ ${group(perimeter)} px`,
    });
  }
  return metrics;
}

function multiPolygonMetrics(
  polygons: { points: [number, number][]; holes?: [number, number][][] }[],
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  const hasDims = !!(imgW && imgH);
  const ringCount = polygons.reduce((n, p) => n + 1 + (p.holes?.length ?? 0), 0);
  const totalVerts = polygons.reduce(
    (n, p) => n + p.points.length + (p.holes ?? []).reduce((sum, ring) => sum + ring.length, 0),
    0,
  );
  const netArea = polygons.reduce((s, p) => {
    const holeArea = (p.holes ?? []).reduce((hs, ring) => hs + shoelaceArea(ring), 0);
    return s + Math.max(0, shoelaceArea(p.points) - holeArea);
  }, 0);
  const metrics: Metric[] = [
    { label: "环 / 顶点", value: `${ringCount} / ${totalVerts}` },
    { label: "占图", value: pctText(netArea) },
  ];
  if (hasDims) {
    metrics.push({ label: "总面积", value: `≈ ${group(netArea * imgW! * imgH!)} px²` });
  }
  return metrics;
}

function polylineMetrics(
  points: [number, number][],
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  const metrics: Metric[] = [{ label: "点数", value: `${points.length}` }];
  if (imgW && imgH) {
    metrics.push({
      label: "总长",
      value: `≈ ${group(polylinePerimeterPx(points, imgW, imgH, false))} px`,
    });
  }
  return metrics;
}

function keypointMetrics(points: Keypoint[]): Metric[] {
  const visible = points.filter((p) => p.v === 2).length;
  const occluded = points.filter((p) => p.v === 1).length;
  const skipped = points.filter((p) => p.v === 0).length;
  return [
    { label: "关键点", value: `${points.length} 个` },
    {
      label: "可见",
      value: `${visible} / ${points.length}`,
      hint: `${occluded} 遮挡 · ${skipped} 跳过`,
    },
  ];
}

function rasterMaskMetrics(mask: {
  size: [number, number];
  runs: number;
  bytes: number;
}): Metric[] {
  const [height, width] = mask.size;
  const stored =
    mask.bytes < 1024
      ? `${mask.bytes} B`
      : `${(mask.bytes / 1024).toFixed(mask.bytes < 10 * 1024 ? 1 : 0)} KB`;
  return [
    { label: "画布", value: `${group(width)}×${group(height)} px` },
    { label: "编码段", value: group(mask.runs) },
    { label: "存储", value: stored, hint: "COCO RLE" },
  ];
}

/**
 * 把选中几何展开为结构化指标数组,取代旧单行 geometrySummary 字符串。
 * 纯函数,带单测(面积 / 周长 / 占图比)。无 imgW/imgH 时降级为相对值并省略需要像素的指标
 * (面积 px² / 周长 / 宽高比)。video_track_bbox / 3D 几何不在此列(各有专属面板),返回空数组。
 */
export function geometryMetrics(
  geometry: Geometry,
  imgW: number | null,
  imgH: number | null,
): Metric[] {
  switch (geometry.type) {
    // 视频单帧几何 (video_bbox/rotated/polygon/polyline) 与图片同形状几何载荷一致,
    // 直接直落到对应图片 case 复用同一套指标 (case 间无中间语句, 满足 no-fallthrough)。
    case "bbox":
    case "video_bbox":
      return bboxMetrics(geometry.x, geometry.y, geometry.w, geometry.h, imgW, imgH);
    case "rotated_bbox":
    case "video_rotated_bbox":
      return rotatedMetrics(
        geometry.cx,
        geometry.cy,
        geometry.w,
        geometry.h,
        geometry.angle,
        imgW,
        imgH,
      );
    case "polygon":
    case "video_polygon":
      return polygonMetrics(geometry.points, geometry.holes, imgW, imgH);
    case "multi_polygon":
      return multiPolygonMetrics(geometry.polygons, imgW, imgH);
    case "polyline":
    case "video_polyline":
      return polylineMetrics(geometry.points, imgW, imgH);
    case "keypoint":
    case "video_keypoint":
      return keypointMetrics(geometry.points);
    case "raster_mask":
    case "video_mask":
      return rasterMaskMetrics(geometry.mask);
    default:
      return [];
  }
}
