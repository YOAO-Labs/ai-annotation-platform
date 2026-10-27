/**
 * v0.13.4 · 单相机视图 = 只读图 + 投影 overlay(canvas)。
 *
 * 把各 3D 框经该相机标定(SensorCalibration)投影成 12 边线框,叠在图上;点投影框可反选对应
 * 3D 框(命中测试 → onSelectBox)。投影**实时**:消费同一份 boxes + highlightedIds,框 PSR / 选中
 * 变化即重绘(useUpdateAnnotation 乐观更新会即时把新几何写入 annotations,故面板 / gizmo / 列表
 * 改框后 overlay 立刻跟随)。无标定的相机降级:不画投影、不报错。
 *
 * 缩放约定:intrinsic 基于图像**原始分辨率**,投影出的像素是原图坐标;overlay 按
 *   显示尺寸 / 自然尺寸(clientWidth/naturalWidth)比例缩放后绘制,`ResizeObserver` + onLoad 重算。
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { hexToRgba } from "@/pages/Workbench/stage/colors";
import type { NormalizedCameraBbox } from "@/api/generated";
import type { SensorCalibration } from "@/types";

import { useCameraOriginalImage } from "./useCameraOriginalImage";

import { psrToCorners } from "./geometry/box3d";
import { BOX_EDGES, projectPoints, unprojectPixelAtDepth } from "./geometry/projection";
import { normalizeRect, type SeedRect } from "./geometry/frustum";
import { buildDepthRaster, sampleDepth, type DepthRaster } from "./geometry/depthmap";
import type { Psr } from "./geometry/triview";
import type { SceneBox } from "./PointCloudScene";

// v0.17.6 · Tailwind class constants (was ThreeDWorkbench.module.css).
const CAMERA_ITEM = "m-0 shrink-0";
const CAMERA_ITEM_EXPANDED = "w-fit max-w-full";
const CAMERA_VIEW = "relative inline-block min-h-24 bg-muted leading-none";
const CAMERA_VIEW_THUMBNAIL = "w-[190px]";
const CAMERA_VIEW_EXPANDED = "w-fit max-w-full";
const CAMERA_CANVAS = "absolute inset-0 cursor-pointer touch-none";
const CAMERA_CANVAS_SEED = "cursor-crosshair";
const CAMERA_CANVAS_BLOCKED = "cursor-wait";
const CAMERA_IMG = "block";
const CAMERA_IMG_THUMBNAIL = "h-auto w-[190px] object-cover";
const CAMERA_IMG_EXPANDED = "h-[70vh] w-auto max-w-full object-contain";
const CAMERA_LOADING =
  "absolute inset-0 flex items-center justify-center text-xs text-muted-foreground";
const CAMERA_FIGCAPTION = "mt-1 text-xs text-muted-foreground text-center";

interface CameraProjectionViewProps {
  name: string;
  imageUrl: string | null | undefined;
  calibration?: SensorCalibration | null;
  /** 非隐藏的 3D 框(PSR + 类别色),与主视图同源。 */
  boxes: SceneBox[];
  /** 需高亮的框 id(选中框 + 同 track_id 成员);决定描边粗细 / 填充。 */
  highlightedIds: Set<string>;
  /** 点投影框反选(命中最小面积框,前景优先)。 */
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  /** 该相机是否最正对当前选中框(可见角点最多者);用于 figcaption 角标。 */
  bestForSelected?: boolean;
  /** v0.13.6 · 点云坐标(N*3,lidar/world 系);深度提示开启时建相机深度栅格。 */
  pointPositions?: Float32Array | null;
  /** v0.13.6 · 深度提示开关:开 → 画深度热力图 + 图上 hover 读出最近点深度/3D。 */
  showDepth?: boolean;
  /**
   * v0.15.24 · 种框模式:在相机图上拖一个 2D 矩形 → mouseup 回调 onSeedBox(natural 像素系)。
   * 上层据此选视锥内点拟合 box_3d。开则禁用反选点击、光标 crosshair。无标定时不生效。
   */
  seedMode?: boolean;
  /** 上层正在保存种框时阻止新的 pointer，不把请求排成并发队列。 */
  interactionDisabled?: boolean;
  /** v0.15.24 · 种框完成回调:rect 为 natural 像素系,calibration 为本相机标定(必非空)。 */
  onSeedBox?: (rect: SeedRect, calibration: SensorCalibration) => void;
  /** 放大相机视图中允许直接移动中心的单个 3D 框；缩略视图与不可编辑态传 null。 */
  editableBox?: SceneBox | null;
  /** 拖动中 commit=false 更新共享草稿，pointerup 时 commit=true 只提交一次。 */
  onEditPsr?: (psr: Psr, commit: boolean) => void;
  /** Escape、pointer cancel 或拖出画布时恢复原 PSR，不提交。 */
  onCancelEditPsr?: (boxId: string, original: Psr) => void;
  /** 标定不可逆等异常只提示，不改变当前草稿。 */
  onEditError?: () => void;
  /** 当前相机上的持久化 2D 成员；坐标为图像归一化坐标。 */
  manualBbox?: NormalizedCameraBbox | null;
  /** 开启后拖框创建，或用八向手柄/框内拖动编辑现有 2D 成员。 */
  manualBboxMode?: boolean;
  /** 标定版本落后时用警示色和虚线提示。 */
  manualBboxStale?: boolean;
  onManualBboxCommit?: (bbox: NormalizedCameraBbox) => void;
  /** 放大浮层使用内容宽度，并受父级可用空间约束；缩略图保持固定 190px。 */
  expanded?: boolean;
  /** 停靠图库按可用宽度等比显示，不使用浮层的固定尺寸。 */
  fitToPanel?: boolean;
  /** 停靠面板隐藏或标签未激活时暂停投影和深度绘制。 */
  visible?: boolean;
}

// 一个框至少有这么多可见角点才参与命中测试(避免擦边框误选)。
const MIN_VISIBLE_FOR_HIT = 3;

// v0.15.24 · 种框拖拽最小位移(显示像素);低于此视为误点,不种框。
const MIN_SEED_DRAG_PX = 5;

const EDIT_HANDLE_RADIUS_PX = 7;
const EDIT_HANDLE_HIT_RADIUS_PX = 20;
const EDIT_DRAG_THRESHOLD_PX = 0.5;
const MANUAL_HANDLE_SIZE_PX = 7;
const MANUAL_HANDLE_HIT_PX = 12;
const MIN_MANUAL_BBOX_RATIO = 0.005;

type ManualHandle = "nw" | "n" | "ne" | "e" | "se" | "s" | "sw" | "w";

interface ManualBboxDisplayRect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

interface ManualBboxDrag {
  pointerId: number;
  kind: "create" | "move" | "resize";
  handle?: ManualHandle;
  start: { x: number; y: number };
  original: NormalizedCameraBbox | null;
  latest: NormalizedCameraBbox | null;
  moved: boolean;
}

function manualHandlePoints(rect: ManualBboxDisplayRect) {
  const cx = (rect.x0 + rect.x1) / 2;
  const cy = (rect.y0 + rect.y1) / 2;
  return {
    nw: { x: rect.x0, y: rect.y0 },
    n: { x: cx, y: rect.y0 },
    ne: { x: rect.x1, y: rect.y0 },
    e: { x: rect.x1, y: cy },
    se: { x: rect.x1, y: rect.y1 },
    s: { x: cx, y: rect.y1 },
    sw: { x: rect.x0, y: rect.y1 },
    w: { x: rect.x0, y: cy },
  } satisfies Record<ManualHandle, { x: number; y: number }>;
}

function hitManualHandle(
  rect: ManualBboxDisplayRect,
  point: { x: number; y: number },
): ManualHandle | null {
  let best: { handle: ManualHandle; distance: number } | null = null;
  for (const [handle, candidate] of Object.entries(manualHandlePoints(rect)) as Array<
    [ManualHandle, { x: number; y: number }]
  >) {
    const distance = Math.hypot(point.x - candidate.x, point.y - candidate.y);
    if (distance <= MANUAL_HANDLE_HIT_PX && (!best || distance < best.distance)) {
      best = { handle, distance };
    }
  }
  return best?.handle ?? null;
}

function normalizedBboxFromPoints(
  start: { x: number; y: number },
  end: { x: number; y: number },
  width: number,
  height: number,
): NormalizedCameraBbox | null {
  const x0 = Math.max(0, Math.min(1, Math.min(start.x, end.x) / width));
  const y0 = Math.max(0, Math.min(1, Math.min(start.y, end.y) / height));
  const x1 = Math.max(0, Math.min(1, Math.max(start.x, end.x) / width));
  const y1 = Math.max(0, Math.min(1, Math.max(start.y, end.y) / height));
  if (x1 - x0 < MIN_MANUAL_BBOX_RATIO || y1 - y0 < MIN_MANUAL_BBOX_RATIO) return null;
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

function moveManualBbox(bbox: NormalizedCameraBbox, dx: number, dy: number): NormalizedCameraBbox {
  return {
    ...bbox,
    x: Math.max(0, Math.min(1 - bbox.w, bbox.x + dx)),
    y: Math.max(0, Math.min(1 - bbox.h, bbox.y + dy)),
  };
}

function resizeManualBbox(
  bbox: NormalizedCameraBbox,
  handle: ManualHandle,
  dx: number,
  dy: number,
): NormalizedCameraBbox {
  let x0 = bbox.x;
  let y0 = bbox.y;
  let x1 = bbox.x + bbox.w;
  let y1 = bbox.y + bbox.h;
  if (handle.includes("w")) x0 = Math.min(x1 - MIN_MANUAL_BBOX_RATIO, Math.max(0, x0 + dx));
  if (handle.includes("e")) x1 = Math.max(x0 + MIN_MANUAL_BBOX_RATIO, Math.min(1, x1 + dx));
  if (handle.includes("n")) y0 = Math.min(y1 - MIN_MANUAL_BBOX_RATIO, Math.max(0, y0 + dy));
  if (handle.includes("s")) y1 = Math.max(y0 + MIN_MANUAL_BBOX_RATIO, Math.min(1, y1 + dy));
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

interface CameraEditDrag {
  pointerId: number;
  boxId: string;
  cameraName: string;
  calibration: SensorCalibration;
  depth: number;
  pixelOffset: readonly [number, number];
  start: { x: number; y: number };
  original: Psr;
  latest: Psr;
  moved: boolean;
  errorShown: boolean;
}

export function CameraProjectionView({
  name,
  imageUrl,
  calibration,
  boxes,
  highlightedIds,
  onSelectBox,
  bestForSelected = false,
  pointPositions = null,
  showDepth = false,
  seedMode = false,
  interactionDisabled = false,
  onSeedBox,
  editableBox = null,
  onEditPsr,
  onCancelEditPsr,
  onEditError,
  manualBbox = null,
  manualBboxMode = false,
  manualBboxStale = false,
  onManualBboxCommit,
  expanded = false,
  fitToPanel = false,
  visible = true,
}: CameraProjectionViewProps) {
  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // v0.15.24 · 种框拖拽:起点(显示坐标)+ 当前矩形(显示坐标),用 ref 即时重绘不进 state。
  const seedStartRef = useRef<{ x: number; y: number } | null>(null);
  const seedRectRef = useRef<SeedRect | null>(null);
  const editHandleRef = useRef<{ x: number; y: number } | null>(null);
  const editDragRef = useRef<CameraEditDrag | null>(null);
  const manualDragRef = useRef<ManualBboxDrag | null>(null);
  const manualDraftRef = useRef<NormalizedCameraBbox | null>(null);
  const manualRectRef = useRef<ManualBboxDisplayRect | null>(null);
  const suppressClickRef = useRef(false);
  // 命中测试用:每框可见投影角点的显示坐标包围盒(id + 矩形 + 面积),draw 时同步。
  const hitBoxesRef = useRef<
    { id: string; x0: number; y0: number; x1: number; y1: number; area: number }[]
  >([]);
  // v0.13.6 · 深度提示:相机深度栅格(state,变化时驱动一次重绘)+ hover 读数(不入 draw 依赖,不触发重绘)。
  const [raster, setRaster] = useState<DepthRaster | null>(null);
  const [natSize, setNatSize] = useState<{ w: number; h: number } | null>(null);
  const [loadedImageUrl, setLoadedImageUrl] = useState<string | null>(null);
  const [failedImageUrl, setFailedImageUrl] = useState<string | null>(null);
  const [hover, setHover] = useState<{ depth: number; point: [number, number, number] } | null>(
    null,
  );
  const originalImage = useCameraOriginalImage(imageUrl);
  const displayUrl = originalImage.url;
  const imageFailed = !imageUrl || originalImage.failed || failedImageUrl === displayUrl;
  const imageReady = !!displayUrl && loadedImageUrl === displayUrl && !imageFailed;

  useEffect(() => {
    setNatSize(null);
    setHover(null);
  }, [displayUrl]);

  // 深度栅格:开关开 + 有点 + 有标定 + 知道原图尺寸时建一次(换帧/换相机重建);否则清空。
  useEffect(() => {
    if (!visible) return;
    if (imageReady && showDepth && pointPositions && calibration && natSize) {
      setRaster(buildDepthRaster(pointPositions, calibration, natSize.w, natSize.h));
    } else {
      setRaster(null);
      setHover(null);
    }
  }, [visible, imageReady, showDepth, pointPositions, calibration, natSize]);

  const draw = useCallback(() => {
    if (!visible || !imageReady) return;
    const img = imgRef.current;
    const canvas = canvasRef.current;
    if (!img || !canvas) return;
    const cssW = img.clientWidth;
    const cssH = img.clientHeight;
    const natW = img.naturalWidth;
    const natH = img.naturalHeight;
    hitBoxesRef.current = [];
    editHandleRef.current = null;
    manualRectRef.current = null;
    if (!cssW || !cssH || !natW || !natH) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    const sx = cssW / natW;
    const sy = cssH / natH;

    // v0.13.6 · 深度热力图:遍历栅格非空格,在投影像素画按深度着色的点(近→远 = 红→蓝)。
    // 画在框线之下。深度归一化到 [minDepth, maxDepth],hue 0(红,近)→ 240(蓝,远)。
    if (calibration && showDepth && raster && isFinite(raster.minDepth)) {
      const span = raster.maxDepth - raster.minDepth || 1;
      const cellsN = raster.cols * raster.rows;
      for (let c = 0; c < cellsN; c++) {
        const d = raster.depth[c];
        if (!isFinite(d)) continue;
        const t = (d - raster.minDepth) / span;
        ctx.fillStyle = `hsl(${240 * t}, 90%, 55%)`;
        ctx.fillRect(raster.u[c] * sx - 1, raster.v[c] * sy - 1, 2, 2);
      }
    }

    if (
      calibration &&
      editableBox &&
      onEditPsr &&
      onCancelEditPsr &&
      !seedMode &&
      !manualBboxMode
    ) {
      const projected = projectPoints([editableBox.center], calibration);
      const [u, v] = projected.pixels[0];
      const x = u * sx;
      const y = v * sy;
      if (
        projected.visible[0] &&
        Number.isFinite(x) &&
        Number.isFinite(y) &&
        x >= 0 &&
        x <= cssW &&
        y >= 0 &&
        y <= cssH
      ) {
        editHandleRef.current = { x, y };
        const styles = getComputedStyle(canvas);
        const accent = styles.getPropertyValue("--sc-brand").trim() || "#3b82f6";
        const surface = styles.getPropertyValue("--sc-card").trim() || "#ffffff";
        ctx.save();
        ctx.beginPath();
        ctx.arc(x, y, EDIT_HANDLE_RADIUS_PX + 2, 0, Math.PI * 2);
        ctx.fillStyle = surface;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, EDIT_HANDLE_RADIUS_PX, 0, Math.PI * 2);
        ctx.fillStyle = accent;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = surface;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, 1.75, 0, Math.PI * 2);
        ctx.fillStyle = surface;
        ctx.fill();
        ctx.restore();
      }
    }

    // 高亮框最后画(描边置顶);同序更新命中包围盒。
    const projectionCalibration = calibration;
    const ordered = projectionCalibration
      ? [...boxes].sort(
          (a, b) => Number(highlightedIds.has(a.id)) - Number(highlightedIds.has(b.id)),
        )
      : [];

    for (const b of ordered) {
      if (!projectionCalibration) break;
      const corners = psrToCorners(b.center, b.size, b.rotation);
      const { pixels, visible } = projectPoints(corners, projectionCalibration);
      if (!visible.some(Boolean)) continue; // 全角点在相机后方 / 不可见 → 该相机不画此框

      const disp = pixels.map(([u, v]) => [u * sx, v * sy] as [number, number]);
      const hl = highlightedIds.has(b.id);

      // 12 边线:两端都可见才连(MVP:出画角点的边不画,不做画面裁剪)。
      ctx.lineWidth = hl ? 2.5 : 1.25;
      ctx.strokeStyle = hexToRgba(b.color, hl ? 1 : 0.8);
      ctx.beginPath();
      for (const [i, j] of BOX_EDGES) {
        if (!visible[i] || !visible[j]) continue;
        ctx.moveTo(disp[i][0], disp[i][1]);
        ctx.lineTo(disp[j][0], disp[j][1]);
      }
      ctx.stroke();

      // 可见角点包围盒:既作淡填充(高亮时),也作命中区。
      let x0 = Infinity;
      let y0 = Infinity;
      let x1 = -Infinity;
      let y1 = -Infinity;
      let visCount = 0;
      for (let k = 0; k < disp.length; k++) {
        if (!visible[k]) continue;
        visCount++;
        x0 = Math.min(x0, disp[k][0]);
        y0 = Math.min(y0, disp[k][1]);
        x1 = Math.max(x1, disp[k][0]);
        y1 = Math.max(y1, disp[k][1]);
      }
      if (hl && visCount >= 2) {
        ctx.fillStyle = hexToRgba(b.color, 0.14);
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      }
      if (visCount >= MIN_VISIBLE_FOR_HIT) {
        hitBoxesRef.current.push({
          id: b.id,
          x0,
          y0,
          x1,
          y1,
          area: Math.max(1, (x1 - x0) * (y1 - y0)),
        });
      }
    }

    // v0.15.24 · 种框橡皮筋矩形(显示坐标,虚线 + 淡填充)。从 shadcn token 取品牌色。
    const seed = seedRectRef.current;
    if (seedMode && seed) {
      const accent = getComputedStyle(canvas).getPropertyValue("--sc-brand").trim() || "#3b82f6";
      const w = seed.x1 - seed.x0;
      const h = seed.y1 - seed.y0;
      ctx.save();
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = accent;
      ctx.fillRect(seed.x0, seed.y0, w, h);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = accent;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 3]);
      ctx.strokeRect(seed.x0, seed.y0, w, h);
      ctx.restore();
    }

    const manual = manualDraftRef.current ?? manualBbox;
    if (manual) {
      const rect = {
        x0: manual.x * cssW,
        y0: manual.y * cssH,
        x1: (manual.x + manual.w) * cssW,
        y1: (manual.y + manual.h) * cssH,
      };
      manualRectRef.current = rect;
      const styles = getComputedStyle(canvas);
      const color = manualBboxStale
        ? styles.getPropertyValue("--sc-warning").trim() || "#f59e0b"
        : styles.getPropertyValue("--sc-success").trim() || "#10b981";
      ctx.save();
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.12;
      ctx.fillRect(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      if (manualBboxStale) ctx.setLineDash([7, 4]);
      ctx.strokeRect(rect.x0, rect.y0, rect.x1 - rect.x0, rect.y1 - rect.y0);
      ctx.setLineDash([]);
      if (manualBboxMode) {
        const handles = manualHandlePoints(rect);
        ctx.fillStyle = color;
        for (const point of Object.values(handles)) {
          ctx.fillRect(
            point.x - MANUAL_HANDLE_SIZE_PX / 2,
            point.y - MANUAL_HANDLE_SIZE_PX / 2,
            MANUAL_HANDLE_SIZE_PX,
            MANUAL_HANDLE_SIZE_PX,
          );
        }
      }
      ctx.restore();
    }
  }, [
    visible,
    imageReady,
    boxes,
    calibration,
    editableBox,
    highlightedIds,
    onCancelEditPsr,
    onEditPsr,
    showDepth,
    raster,
    seedMode,
    manualBbox,
    manualBboxMode,
    manualBboxStale,
  ]);

  // 数据 / 标定变化重绘。
  useEffect(() => {
    draw();
  }, [draw]);

  useEffect(() => {
    if (!interactionDisabled) return;
    seedStartRef.current = null;
    seedRectRef.current = null;
    manualDragRef.current = null;
    manualDraftRef.current = null;
    draw();
  }, [draw, interactionDisabled]);

  useEffect(() => {
    manualDragRef.current = null;
    manualDraftRef.current = null;
    draw();
  }, [draw, manualBboxMode, name]);

  // 图尺寸变化(响应式布局 / 懒加载)重算缩放并重绘。
  useEffect(() => {
    const img = imgRef.current;
    if (!img || !visible) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(img);
    return () => ro.disconnect();
  }, [draw, visible]);

  // 图加载后记下原图分辨率(深度栅格 / 投影都基于 intrinsic 原图坐标)+ 重绘。
  const handleImgLoad = useCallback(() => {
    const img = imgRef.current;
    if (img?.naturalWidth) {
      setFailedImageUrl(null);
      setLoadedImageUrl(displayUrl ?? null);
      setNatSize({ w: img.naturalWidth, h: img.naturalHeight });
    }
    draw();
  }, [draw, displayUrl]);

  // 光标相对 canvas 的显示坐标。
  const localXY = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }, []);

  const naturalPixel = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>): [number, number] | null => {
      const img = imgRef.current;
      if (!img?.naturalWidth || !img.naturalHeight || !img.clientWidth || !img.clientHeight) {
        return null;
      }
      const { x, y } = localXY(e);
      return [(x / img.clientWidth) * img.naturalWidth, (y / img.clientHeight) * img.naturalHeight];
    },
    [localXY],
  );

  const releasePointer = useCallback((canvas: HTMLCanvasElement, pointerId: number) => {
    if (canvas.hasPointerCapture?.(pointerId)) canvas.releasePointerCapture(pointerId);
    canvas.style.removeProperty("cursor");
  }, []);

  const cancelCameraEdit = useCallback(
    (canvas: HTMLCanvasElement | null) => {
      const drag = editDragRef.current;
      if (!drag) return;
      editDragRef.current = null;
      suppressClickRef.current = false;
      if (canvas) releasePointer(canvas, drag.pointerId);
      onCancelEditPsr?.(drag.boxId, drag.original);
    },
    [onCancelEditPsr, releasePointer],
  );

  const cancelManualEdit = useCallback(
    (canvas: HTMLCanvasElement | null) => {
      const drag = manualDragRef.current;
      if (!drag) return;
      manualDragRef.current = null;
      manualDraftRef.current = null;
      suppressClickRef.current = false;
      if (canvas) releasePointer(canvas, drag.pointerId);
      draw();
    },
    [draw, releasePointer],
  );

  useEffect(() => {
    const drag = editDragRef.current;
    if (
      drag &&
      (drag.boxId !== editableBox?.id ||
        drag.cameraName !== name ||
        drag.calibration !== calibration)
    ) {
      cancelCameraEdit(canvasRef.current);
    }
  }, [calibration, cancelCameraEdit, editableBox?.id, name]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || (!editDragRef.current && !manualDragRef.current)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (manualDragRef.current) cancelManualEdit(canvasRef.current);
      else cancelCameraEdit(canvasRef.current);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [cancelCameraEdit, cancelManualEdit]);

  // v0.13.6 / v0.15.24 · mousemove:种框模式下更新橡皮筋矩形(ref + 直接重绘);否则深度 hover。
  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (interactionDisabled) return;
      const manualDrag = manualDragRef.current;
      if (manualBboxMode && manualDrag && manualDrag.pointerId === e.pointerId) {
        const point = localXY(e);
        const width = e.currentTarget.clientWidth;
        const height = e.currentTarget.clientHeight;
        const dx = (point.x - manualDrag.start.x) / width;
        const dy = (point.y - manualDrag.start.y) / height;
        let next: NormalizedCameraBbox | null = null;
        if (manualDrag.kind === "create") {
          next = normalizedBboxFromPoints(manualDrag.start, point, width, height);
        } else if (manualDrag.kind === "move" && manualDrag.original) {
          next = moveManualBbox(manualDrag.original, dx, dy);
        } else if (manualDrag.handle && manualDrag.original) {
          next = resizeManualBbox(manualDrag.original, manualDrag.handle, dx, dy);
        }
        manualDrag.latest = next;
        manualDrag.moved ||=
          Math.hypot(point.x - manualDrag.start.x, point.y - manualDrag.start.y) >=
          MIN_SEED_DRAG_PX;
        manualDraftRef.current = next;
        draw();
        return;
      }
      if (seedMode) {
        if (!seedStartRef.current) return;
        const { x, y } = localXY(e);
        const s = seedStartRef.current;
        seedRectRef.current = normalizeRect(s.x, s.y, x, y);
        draw();
        return;
      }
      const drag = editDragRef.current;
      if (drag && drag.pointerId === e.pointerId && calibration && onEditPsr) {
        const bounds = e.currentTarget.getBoundingClientRect();
        if (
          e.clientX < bounds.left ||
          e.clientX > bounds.right ||
          e.clientY < bounds.top ||
          e.clientY > bounds.bottom
        ) {
          cancelCameraEdit(e.currentTarget);
          return;
        }
        const pixel = naturalPixel(e);
        if (!pixel) return;
        const center = unprojectPixelAtDepth(
          [pixel[0] + drag.pixelOffset[0], pixel[1] + drag.pixelOffset[1]],
          drag.depth,
          calibration,
        );
        if (!center) {
          if (!drag.errorShown) {
            drag.errorShown = true;
            onEditError?.();
          }
          return;
        }
        const { x, y } = localXY(e);
        const next: Psr = {
          center,
          size: [...drag.original.size],
          rotation: [...drag.original.rotation],
        };
        drag.latest = next;
        drag.moved ||= Math.hypot(x - drag.start.x, y - drag.start.y) >= EDIT_DRAG_THRESHOLD_PX;
        if (drag.moved) onEditPsr(next, false);
        return;
      }

      if (manualBboxMode) {
        const point = localXY(e);
        const rect = manualRectRef.current;
        if (!rect) e.currentTarget.style.cursor = "crosshair";
        else if (hitManualHandle(rect, point)) e.currentTarget.style.cursor = "nwse-resize";
        else if (
          point.x >= rect.x0 &&
          point.x <= rect.x1 &&
          point.y >= rect.y0 &&
          point.y <= rect.y1
        )
          e.currentTarget.style.cursor = "move";
        else e.currentTarget.style.cursor = "default";
        return;
      }
      const handle = editHandleRef.current;
      if (handle) {
        const { x, y } = localXY(e);
        e.currentTarget.style.cursor =
          Math.hypot(x - handle.x, y - handle.y) <= EDIT_HANDLE_HIT_RADIUS_PX ? "grab" : "pointer";
      }
      if (!showDepth || !raster) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const u = ((e.clientX - rect.left) / rect.width) * raster.width;
      const v = ((e.clientY - rect.top) / rect.height) * raster.height;
      setHover(sampleDepth(raster, u, v));
    },
    [
      seedMode,
      manualBboxMode,
      calibration,
      onEditPsr,
      cancelCameraEdit,
      naturalPixel,
      localXY,
      onEditError,
      draw,
      showDepth,
      raster,
      interactionDisabled,
    ],
  );

  // v0.15.24 · 种框:按下记起点(需有标定);松手换算回 natural 像素发 onSeedBox;微小位移视为误点。
  const handlePointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (e.button !== 0 || editDragRef.current || manualDragRef.current) return;
      if (interactionDisabled) {
        e.preventDefault();
        return;
      }
      if (seedMode) {
        if (!calibration) return;
        seedStartRef.current = localXY(e);
        seedRectRef.current = null;
        return;
      }
      if (manualBboxMode && onManualBboxCommit) {
        const point = localXY(e);
        const rect = manualRectRef.current;
        let kind: ManualBboxDrag["kind"] = "create";
        let handle: ManualHandle | undefined;
        if (rect && manualBbox) {
          handle = hitManualHandle(rect, point) ?? undefined;
          if (handle) kind = "resize";
          else if (
            point.x >= rect.x0 &&
            point.x <= rect.x1 &&
            point.y >= rect.y0 &&
            point.y <= rect.y1
          )
            kind = "move";
          else return;
        }
        manualDragRef.current = {
          pointerId: e.pointerId,
          kind,
          handle,
          start: point,
          original: manualBbox,
          latest: manualBbox,
          moved: false,
        };
        e.currentTarget.setPointerCapture(e.pointerId);
        e.currentTarget.style.cursor = kind === "move" ? "grabbing" : "crosshair";
        e.preventDefault();
        return;
      }
      if (!editableBox || !calibration || !onEditPsr || !onCancelEditPsr) return;
      const handle = editHandleRef.current;
      if (!handle) return;
      const local = localXY(e);
      if (Math.hypot(local.x - handle.x, local.y - handle.y) > EDIT_HANDLE_HIT_RADIUS_PX) return;

      const projected = projectPoints([editableBox.center], calibration);
      const depth = projected.depths[0];
      const pixel = naturalPixel(e);
      if (
        !projected.visible[0] ||
        !pixel ||
        !unprojectPixelAtDepth(projected.pixels[0], depth, calibration)
      ) {
        onEditError?.();
        return;
      }

      const original: Psr = {
        center: [...editableBox.center],
        size: [...editableBox.size],
        rotation: [...editableBox.rotation],
      };
      editDragRef.current = {
        pointerId: e.pointerId,
        boxId: editableBox.id,
        cameraName: name,
        calibration,
        depth,
        pixelOffset: [projected.pixels[0][0] - pixel[0], projected.pixels[0][1] - pixel[1]],
        start: local,
        original,
        latest: original,
        moved: false,
        errorShown: false,
      };
      e.currentTarget.setPointerCapture(e.pointerId);
      e.currentTarget.style.cursor = "grabbing";
      e.preventDefault();
    },
    [
      seedMode,
      manualBboxMode,
      manualBbox,
      onManualBboxCommit,
      calibration,
      editableBox,
      onEditPsr,
      onCancelEditPsr,
      localXY,
      naturalPixel,
      onEditError,
      name,
      interactionDisabled,
    ],
  );

  const handlePointerUp = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (interactionDisabled) {
        seedStartRef.current = null;
        seedRectRef.current = null;
        draw();
        return;
      }
      const manualDrag = manualDragRef.current;
      if (manualBboxMode && manualDrag && manualDrag.pointerId === e.pointerId) {
        manualDragRef.current = null;
        releasePointer(e.currentTarget, e.pointerId);
        suppressClickRef.current = manualDrag.moved;
        const next = manualDrag.latest;
        manualDraftRef.current = null;
        if (manualDrag.moved && next) onManualBboxCommit?.(next);
        draw();
        return;
      }
      const drag = editDragRef.current;
      if (!seedMode && drag && drag.pointerId === e.pointerId) {
        editDragRef.current = null;
        releasePointer(e.currentTarget, e.pointerId);
        suppressClickRef.current = drag.moved;
        if (drag.moved) onEditPsr?.(drag.latest, true);
        return;
      }

      const start = seedStartRef.current;
      seedStartRef.current = null;
      seedRectRef.current = null;
      if (!seedMode || !start || !calibration || !onSeedBox) {
        draw();
        return;
      }
      const { x, y } = localXY(e);
      draw(); // 清掉橡皮筋
      // 对角线位移小于阈值才算误点(与 ThreeDWorkbench 的 DRAG_CLICK_TOL 同口径);
      // 用 hypot 而非「任一边 < 阈值」,避免吃掉细长矩形(行人/杆子/路灯侧影)。
      if (Math.hypot(x - start.x, y - start.y) < MIN_SEED_DRAG_PX) {
        return; // 误点,不种框
      }
      const img = imgRef.current;
      if (!img?.naturalWidth || !img.clientWidth) return;
      const sx = img.clientWidth / img.naturalWidth;
      const sy = img.clientHeight / img.naturalHeight;
      // 显示坐标 → natural 像素(与 projectPoints 的原图 intrinsic 对齐)。
      const rect = normalizeRect(start.x / sx, start.y / sy, x / sx, y / sy);
      onSeedBox(rect, calibration);
    },
    [
      seedMode,
      manualBboxMode,
      calibration,
      onSeedBox,
      onEditPsr,
      onManualBboxCommit,
      releasePointer,
      localXY,
      draw,
      interactionDisabled,
    ],
  );

  const handlePointerLeave = useCallback(() => {
    setHover(null);
    if (manualDragRef.current) return;
    if (editDragRef.current) {
      cancelCameraEdit(canvasRef.current);
      return;
    }
    canvasRef.current?.style.removeProperty("cursor");
    if (seedStartRef.current || seedRectRef.current) {
      seedStartRef.current = null;
      seedRectRef.current = null;
      draw(); // 拖出画布外 → 取消种框
    }
  }, [cancelCameraEdit, draw]);

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      if (interactionDisabled) return;
      if (suppressClickRef.current) {
        suppressClickRef.current = false;
        return;
      }
      if (seedMode || manualBboxMode) return; // 编辑模式禁用反选
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      // 命中含点的最小面积框(前景 / 近处框面积小,优先选中)。
      let hitId: string | null = null;
      let hitArea = Infinity;
      for (const hb of hitBoxesRef.current) {
        if (x >= hb.x0 && x <= hb.x1 && y >= hb.y0 && y <= hb.y1 && hb.area < hitArea) {
          hitArea = hb.area;
          hitId = hb.id;
        }
      }
      if (hitId) onSelectBox(hitId, { shift: e.shiftKey });
    },
    [interactionDisabled, manualBboxMode, seedMode, onSelectBox],
  );

  return (
    <figure
      className={`${CAMERA_ITEM} ${fitToPanel ? "min-w-0 w-full" : expanded ? CAMERA_ITEM_EXPANDED : ""}`}
    >
      <div
        className={`${CAMERA_VIEW} ${fitToPanel ? "w-full" : expanded ? CAMERA_VIEW_EXPANDED : CAMERA_VIEW_THUMBNAIL}`}
      >
        <img
          key={displayUrl ?? "pending-original"}
          ref={imgRef}
          src={displayUrl}
          alt={name}
          className={`${CAMERA_IMG} ${
            fitToPanel
              ? "h-auto w-full object-contain"
              : expanded
                ? CAMERA_IMG_EXPANDED
                : CAMERA_IMG_THUMBNAIL
          } ${imageReady ? "" : "opacity-0"}`}
          loading="eager"
          decoding="async"
          onLoad={handleImgLoad}
          onError={() => setFailedImageUrl(displayUrl ?? null)}
        />
        {!imageReady && (
          <span className={CAMERA_LOADING} role="status">
            {imageFailed ? (imageUrl ? "相机图像加载失败" : "当前帧无图像") : "加载相机…"}
          </span>
        )}
        <canvas
          ref={canvasRef}
          className={[
            CAMERA_CANVAS,
            (!imageReady || !visible) && "pointer-events-none opacity-0",
            (seedMode || manualBboxMode) && CAMERA_CANVAS_SEED,
            interactionDisabled && CAMERA_CANVAS_BLOCKED,
          ]
            .filter(Boolean)
            .join(" ")}
          aria-label={`${name} 相机投影${
            manualBboxMode
              ? manualBbox
                ? "，拖动框体或手柄编辑 2D 成员"
                : "，拖动创建 2D 成员"
              : editableBox
                ? "，拖动中心手柄微调 3D 框"
                : ""
          }`}
          aria-disabled={interactionDisabled || undefined}
          onClick={handleClick}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerLeave}
          onPointerLeave={handlePointerLeave}
          onLostPointerCapture={() => cancelCameraEdit(canvasRef.current)}
        />
      </div>
      <figcaption className={`${CAMERA_FIGCAPTION} ${fitToPanel ? "break-words" : ""}`}>
        {name}
        {bestForSelected && " · 正对"}
        {calibration ? "" : " · 无标定"}
        {seedMode && calibration && (interactionDisabled ? " · 正在保存" : " · 拖框种 3D 框")}
        {manualBbox && ` · 2D 人工框${manualBboxStale ? "（标定已变更）" : ""}`}
        {manualBboxMode &&
          (interactionDisabled ? " · 正在保存" : manualBbox ? " · 可移动/缩放" : " · 拖框创建")}
        {showDepth && !seedMode && hover && ` · ${hover.depth.toFixed(1)}m`}
      </figcaption>
    </figure>
  );
}

export default CameraProjectionView;
