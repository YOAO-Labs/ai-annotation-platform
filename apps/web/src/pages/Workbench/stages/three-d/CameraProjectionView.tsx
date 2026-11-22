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
  boxes: SceneBox[];
  highlightedIds: Set<string>;
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  bestForSelected?: boolean;
  pointPositions?: Float32Array | null;
  showDepth?: boolean;
  seedMode?: boolean;
  interactionDisabled?: boolean;
  onSeedBox?: (rect: SeedRect, calibration: SensorCalibration) => void;
  editableBox?: SceneBox | null;
  onEditPsr?: (psr: Psr, commit: boolean) => void;
  onCancelEditPsr?: (boxId: string, original: Psr) => void;
  onEditError?: () => void;
  manualBbox?: NormalizedCameraBbox | null;
  manualBboxMode?: boolean;
  manualBboxStale?: boolean;
  onManualBboxCommit?: (bbox: NormalizedCameraBbox) => void;
  expanded?: boolean;
  fitToPanel?: boolean;
  visible?: boolean;
}

const MIN_VISIBLE_FOR_HIT = 3;

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
  const seedStartRef = useRef<{ x: number; y: number } | null>(null);
  const seedRectRef = useRef<SeedRect | null>(null);
  const editHandleRef = useRef<{ x: number; y: number } | null>(null);
  const editDragRef = useRef<CameraEditDrag | null>(null);
  const manualDragRef = useRef<ManualBboxDrag | null>(null);
  const manualDraftRef = useRef<NormalizedCameraBbox | null>(null);
  const manualRectRef = useRef<ManualBboxDisplayRect | null>(null);
  const suppressClickRef = useRef(false);
  const hitBoxesRef = useRef<
    { id: string; x0: number; y0: number; x1: number; y1: number; area: number }[]
  >([]);
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
      if (!visible.some(Boolean)) continue;

      const disp = pixels.map(([u, v]) => [u * sx, v * sy] as [number, number]);
      const hl = highlightedIds.has(b.id);

      ctx.lineWidth = hl ? 2.5 : 1.25;
      ctx.strokeStyle = hexToRgba(b.color, hl ? 1 : 0.8);
      ctx.beginPath();
      for (const [i, j] of BOX_EDGES) {
        if (!visible[i] || !visible[j]) continue;
        ctx.moveTo(disp[i][0], disp[i][1]);
        ctx.lineTo(disp[j][0], disp[j][1]);
      }
      ctx.stroke();

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

  useEffect(() => {
    const img = imgRef.current;
    if (!img || !visible) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(img);
    return () => ro.disconnect();
  }, [draw, visible]);

  const handleImgLoad = useCallback(() => {
    const img = imgRef.current;
    if (img?.naturalWidth) {
      setFailedImageUrl(null);
      setLoadedImageUrl(displayUrl ?? null);
      setNatSize({ w: img.naturalWidth, h: img.naturalHeight });
    }
    draw();
  }, [draw, displayUrl]);

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
      draw();
      if (Math.hypot(x - start.x, y - start.y) < MIN_SEED_DRAG_PX) {
        return;
      }
      const img = imgRef.current;
      if (!img?.naturalWidth || !img.clientWidth) return;
      const sx = img.clientWidth / img.naturalWidth;
      const sy = img.clientHeight / img.naturalHeight;
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
      draw();
    }
  }, [cancelCameraEdit, draw]);

  const handleClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      if (interactionDisabled) return;
      if (suppressClickRef.current) {
        suppressClickRef.current = false;
        return;
      }
      if (seedMode || manualBboxMode) return;
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
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
