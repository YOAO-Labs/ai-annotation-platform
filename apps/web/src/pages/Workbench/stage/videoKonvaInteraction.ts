import { useCallback, useEffect, useRef, useState } from "react";
import type Konva from "konva";
import type {
  AnnotationResponse,
  VideoBboxGeometry,
  VideoPolygonGeometry,
  VideoPolylineGeometry,
  VideoRotatedBboxGeometry,
  VideoKeypointGeometry,
  Keypoint,
  VideoTrackGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";
import type { Viewport } from "../state/useViewportTransform";
import type { VideoTool } from "../state/useWorkbenchState";
import type { VideoDrawingDraft } from "./videoStageControls";
import { applyResize, applyRotatedResize } from "./ResizeHandles";
import {
  clamp01,
  clampGeom,
  isVideoBbox,
  isVideoPolygon,
  isVideoPolygonTrack,
  isVideoPolyline,
  isVideoPolylineTrack,
  isVideoRotatedBbox,
  isVideoKeypoint,
  isVideoTrack,
  normalizeGeom,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
  upsertKeyframe,
  upsertPointsKeyframe,
} from "./videoStageGeometry";
import { moveVertex } from "./shared/geometry/polygon";
import { pickTopVideoEntryAt, pickTopVideoMaskAt } from "./videoStagePicking";
import type { VideoMaskRenderRecord } from "./videoMaskFrames";
import { clientToVideoNorm, videoNormToClient, type VideoPixelSize } from "./videoKonvaCoordinates";
import type {
  VideoDragState,
  VideoResizeDirection,
  VideoSamPrompt,
  VideoStageGeom,
} from "./videoStageTypes";

export const VIDEO_MIN_BOX = 0.003;
export const SAM_MIN_DRAG = 0.005;

export function isSamProbeTool(t: VideoTool): boolean {
  return t === "smart-point" || t === "smart-box" || t === "exemplar" || t === "magic-box";
}

export function isSamCandidateNavTool(t: VideoTool): boolean {
  return isSamProbeTool(t) && t !== "magic-box";
}

export function samProbeMode(t: VideoTool): "point" | "bbox" | "exemplar" {
  if (t === "smart-point") return "point";
  if (t === "exemplar") return "exemplar";
  return "bbox";
}

type DragModifiers = { shiftKey?: boolean; altKey?: boolean };

function clampRotatedPosition(
  geometry: VideoRotatedBboxGeometry,
  size: VideoPixelSize,
): VideoRotatedBboxGeometry {
  const rad = (geometry.angle * Math.PI) / 180;
  const extentX =
    (Math.abs(Math.cos(rad)) * geometry.w * size.w +
      Math.abs(Math.sin(rad)) * geometry.h * size.h) /
    (2 * size.w);
  const extentY =
    (Math.abs(Math.sin(rad)) * geometry.w * size.w +
      Math.abs(Math.cos(rad)) * geometry.h * size.h) /
    (2 * size.h);
  const minX = Math.min(0.5, extentX);
  const minY = Math.min(0.5, extentY);
  return {
    ...geometry,
    cx: Math.max(minX, Math.min(1 - minX, geometry.cx)),
    cy: Math.max(minY, Math.min(1 - minY, geometry.cy)),
  };
}

function pointerAngle(
  point: { x: number; y: number },
  geometry: VideoRotatedBboxGeometry,
  size: VideoPixelSize,
) {
  return Math.atan2((point.y - geometry.cy) * size.h, (point.x - geometry.cx) * size.w);
}

function polygonTranslationBounds(
  points: VideoPolygonGeometry["points"],
  holes: VideoPolygonGeometry["holes"],
) {
  let minX = 1;
  let minY = 1;
  let maxX = 0;
  let maxY = 0;
  const include = (ring: VideoPolygonGeometry["points"]) => {
    for (const [x, y] of ring) {
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
  };
  include(points);
  holes?.forEach(include);
  return { minX: -minX, minY: -minY, maxX: 1 - maxX, maxY: 1 - maxY };
}

export function advanceDrag(
  drag: VideoDragState,
  pt: VideoStageGeom | { x: number; y: number },
  modifiers: DragModifiers = {},
): VideoDragState {
  if (!drag || drag.kind === "pan") return drag;
  if (drag.kind === "draw") return { ...drag, current: { x: pt.x, y: pt.y } };
  if (drag.kind === "samProbe") return { ...drag, current: { x: pt.x, y: pt.y } };
  if (drag.kind === "polyVertex") {
    return { ...drag, current: moveVertex(drag.origin, drag.vidx, [clamp01(pt.x), clamp01(pt.y)]) };
  }
  if (drag.kind === "polyMove") {
    let dx = pt.x - drag.start.x;
    let dy = pt.y - drag.start.y;
    const bounds = drag.translationBounds;
    if (bounds) {
      dx = Math.max(bounds.minX, Math.min(bounds.maxX, dx));
      dy = Math.max(bounds.minY, Math.min(bounds.maxY, dy));
    }
    if (dx === 0 && dy === 0) {
      return { ...drag, current: drag.origin, currentHoles: drag.originHoles };
    }
    const offset = ([x, y]: [number, number]): [number, number] =>
      bounds ? [x + dx, y + dy] : [clamp01(x + dx), clamp01(y + dy)];
    return {
      ...drag,
      current: drag.origin.map(offset),
      currentHoles: drag.originHoles?.map((ring) => ring.map(offset)),
    };
  }
  if (drag.kind === "keypointNode") {
    return {
      ...drag,
      current: drag.origin.map((point, index) =>
        index === drag.nodeIdx ? { ...point, x: clamp01(pt.x), y: clamp01(pt.y) } : point,
      ),
    };
  }
  if (drag.kind === "obbMove") {
    return {
      ...drag,
      current: clampRotatedPosition(
        {
          ...drag.origin,
          cx: drag.origin.cx + (pt.x - drag.start.x),
          cy: drag.origin.cy + (pt.y - drag.start.y),
        },
        drag.imageSize,
      ),
    };
  }
  if (drag.kind === "obbResize") {
    const resized = applyRotatedResize(
      {
        type: "rotated_bbox",
        cx: drag.origin.cx,
        cy: drag.origin.cy,
        w: drag.origin.w,
        h: drag.origin.h,
        angle: drag.origin.angle,
      },
      drag.start,
      pt,
      drag.dir,
      drag.imageSize,
      modifiers,
    );
    return {
      ...drag,
      current: clampRotatedPosition(
        { ...drag.origin, ...resized, type: "video_rotated_bbox" },
        drag.imageSize,
      ),
    };
  }
  if (drag.kind === "obbRotate") {
    const delta = pointerAngle(pt, drag.origin, drag.imageSize) - drag.startPointerAngle;
    const angle = (drag.origin.angle + (delta * 180) / Math.PI + 360) % 360;
    return { ...drag, current: clampRotatedPosition({ ...drag.origin, angle }, drag.imageSize) };
  }
  const next =
    drag.kind === "resize"
      ? applyResize(drag.origin, drag.start, pt, drag.dir, modifiers)
      : clampGeom({
          ...drag.origin,
          x: drag.origin.x + (pt.x - drag.start.x),
          y: drag.origin.y + (pt.y - drag.start.y),
        });
  return { ...drag, current: next };
}

export type VideoDragCommit =
  | { type: "none" }
  | {
      type: "draw";
      kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox";
      geom: VideoStageGeom;
    }
  | { type: "track"; ann: AnnotationResponse; geom: VideoStageGeom }
  | { type: "bbox"; ann: AnnotationResponse; geom: VideoStageGeom }
  | {
      type: "poly";
      ann: AnnotationResponse;
      points: VideoPolygonGeometry["points"];
      holes?: VideoPolygonGeometry["holes"];
    }
  | { type: "rotated"; ann: AnnotationResponse; geometry: VideoRotatedBboxGeometry }
  | { type: "keypoint"; ann: AnnotationResponse; points: Keypoint[] }
  | { type: "samProbe"; mode: "point"; pt: [number, number]; alt: boolean }
  | {
      type: "samProbe";
      mode: "bbox" | "exemplar";
      bbox: [number, number, number, number];
      alt: boolean;
    };

export interface ResolveDragCommitCtx {
  annotations: readonly AnnotationResponse[];
  videoTool: VideoTool;
  selectedTrack: (AnnotationResponse & { geometry: VideoTrackGeometry }) | null;
  lockedTrackIds: Set<string>;
  frameIndex: number;
}

export function resolveDragCommit(
  drag: VideoDragState,
  finalPt: { x: number; y: number },
  ctx: ResolveDragCommitCtx,
): VideoDragCommit {
  if (!drag || drag.kind === "pan") return { type: "none" };
  const { annotations, videoTool, selectedTrack, lockedTrackIds, frameIndex } = ctx;

  if (drag.kind === "samProbe") {
    if (drag.mode === "point") {
      return { type: "samProbe", mode: "point", pt: [drag.start.x, drag.start.y], alt: drag.alt };
    }
    const x1 = Math.min(drag.start.x, finalPt.x);
    const y1 = Math.min(drag.start.y, finalPt.y);
    const x2 = Math.max(drag.start.x, finalPt.x);
    const y2 = Math.max(drag.start.y, finalPt.y);
    if (x2 - x1 <= SAM_MIN_DRAG || y2 - y1 <= SAM_MIN_DRAG) return { type: "none" };
    return { type: "samProbe", mode: drag.mode, bbox: [x1, y1, x2, y2], alt: drag.alt };
  }

  if (drag.kind === "draw") {
    if (videoTool === "select") return { type: "none" };
    const geom = normalizeGeom(drag.start, finalPt);
    if (geom.w < VIDEO_MIN_BOX || geom.h < VIDEO_MIN_BOX) return { type: "none" };
    if (
      videoTool === "track" &&
      selectedTrack &&
      !lockedTrackIds.has(selectedTrack.geometry.track_id)
    ) {
      const hasKeyframeAtFrame = selectedTrack.geometry.keyframes.some(
        (kf) => kf.frame_index === frameIndex,
      );
      if (!hasKeyframeAtFrame) return { type: "track", ann: selectedTrack, geom };
    }
    const kind =
      videoTool === "track"
        ? "video_track_bbox"
        : videoTool === "rotated-box"
          ? "video_rotated_bbox"
          : "video_bbox";
    return { type: "draw", kind, geom };
  }

  if (drag.kind === "polyVertex" || drag.kind === "polyMove") {
    if (drag.frameIndex !== frameIndex) return { type: "none" };
    const polyAnn = annotations.find((a) => a.id === drag.id);
    const isPoly =
      polyAnn &&
      (isVideoPolygon(polyAnn) ||
        isVideoPolyline(polyAnn) ||
        isVideoPolygonTrack(polyAnn) ||
        isVideoPolylineTrack(polyAnn));
    if (!polyAnn || !isPoly) return { type: "none" };
    if (polyAnn.geometry.type === "video_polygon" || polyAnn.geometry.type === "video_polyline") {
      if (polyAnn.geometry.frame_index !== frameIndex) return { type: "none" };
    } else {
      const trackId = trackIdOf(polyAnn);
      if (trackId && lockedTrackIds.has(trackId)) return { type: "none" };
    }
    if (drag.current === drag.origin) return { type: "none" };
    return drag.kind === "polyMove"
      ? { type: "poly", ann: polyAnn, points: drag.current, holes: drag.currentHoles }
      : { type: "poly", ann: polyAnn, points: drag.current };
  }

  if (drag.kind === "obbMove" || drag.kind === "obbResize" || drag.kind === "obbRotate") {
    const ann = annotations.find((item) => item.id === drag.id);
    if (drag.current.w < VIDEO_MIN_BOX || drag.current.h < VIDEO_MIN_BOX) return { type: "none" };
    return ann && isVideoRotatedBbox(ann)
      ? { type: "rotated", ann, geometry: drag.current }
      : { type: "none" };
  }
  if (drag.kind === "keypointNode") {
    const ann = annotations.find((item) => item.id === drag.id);
    return ann && isVideoKeypoint(ann)
      ? { type: "keypoint", ann, points: drag.current }
      : { type: "none" };
  }

  const ann = annotations.find((a) => a.id === drag.id);
  if (!ann) return { type: "none" };
  const geom = drag.current;
  if (drag.kind === "resize" && (geom.w < VIDEO_MIN_BOX || geom.h < VIDEO_MIN_BOX))
    return { type: "none" };
  if (isVideoTrack(ann)) return { type: "track", ann, geom };
  if (isVideoBbox(ann)) return { type: "bbox", ann, geom };
  return { type: "none" };
}

function trackIdOf(ann: AnnotationResponse): string | null {
  if (isVideoTrack(ann) || isVideoPolygonTrack(ann) || isVideoPolylineTrack(ann)) {
    return ann.geometry.track_id;
  }
  return null;
}

function pointsAtFrame(
  ann: AnnotationResponse | undefined,
  frameIndex: number,
): [number, number][] | null {
  if (!ann) return null;
  if (isVideoPolygon(ann) || isVideoPolyline(ann)) return ann.geometry.points;
  if (isVideoPolygonTrack(ann))
    return resolveVideoPolygonTrackAtFrame(ann.geometry, frameIndex)?.points ?? null;
  if (isVideoPolylineTrack(ann))
    return resolveVideoPolylineTrackAtFrame(ann.geometry, frameIndex)?.points ?? null;
  return null;
}

export type VideoPickable = {
  id: string;
  geom: VideoStageGeom;
  rotatedBbox?: VideoRotatedBboxGeometry;
  keypoints?: Keypoint[];
};

export interface UseVideoKonvaInteractionParams {
  containerRef: React.RefObject<HTMLElement | null>;
  vpRef: React.MutableRefObject<Viewport>;
  size: VideoPixelSize;
  annotations: readonly AnnotationResponse[];
  entries: VideoPickable[];
  maskEntries: VideoMaskRenderRecord[];
  ghost: VideoPickable | null;
  carryOverGhosts: VideoPickable[];
  selectedTrack: (AnnotationResponse & { geometry: VideoTrackGeometry }) | null;
  videoTool: VideoTool;
  readOnly: boolean;
  isPlaybackActive: boolean;
  lockedTrackIds: Set<string>;
  creationEnabled: boolean;
  frameIndex: number;
  trackContinueAutoAdvance: boolean;
  onSelect: (id: string | null, opts?: { shift?: boolean }) => void;
  onCreate: (frameIndex: number, geom: VideoStageGeom) => void;
  onPendingDraw?: (
    kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
    frameIndex: number,
    geom: VideoStageGeom,
    anchor: { left: number; top: number },
  ) => void;
  onUpdate: (
    annotation: AnnotationResponse,
    geometry:
      | VideoBboxGeometry
      | VideoTrackGeometry
      | VideoPolygonGeometry
      | VideoPolylineGeometry
      | VideoRotatedBboxGeometry
      | VideoKeypointGeometry
      | VideoTrackPolygonGeometry
      | VideoTrackPolylineGeometry,
  ) => void;
  onSamPrompt?: (prompt: VideoSamPrompt) => void;
  samPolarity?: "positive" | "negative";
}

export interface VideoKonvaInteraction {
  drag: VideoDragState;
  getDrawingDraft: () => VideoDrawingDraft | null;
  discardDrawingDraft: () => void;
  continuingTrack: boolean;
  onStagePointerDown: (e: Konva.KonvaEventObject<PointerEvent>) => void;
  onResizeHandlePointerDown: (
    dir: VideoResizeDirection,
    entryId: string,
    geom: VideoStageGeom,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onVertexPointerDown: (
    entryId: string,
    vidx: number,
    points: [number, number][],
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onObbResizePointerDown: (
    dir: VideoResizeDirection,
    entryId: string,
    geometry: VideoRotatedBboxGeometry,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onObbRotatePointerDown: (
    entryId: string,
    geometry: VideoRotatedBboxGeometry,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  onKeypointPointerDown: (
    entryId: string,
    nodeIdx: number,
    points: Keypoint[],
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
}

export function useVideoKonvaInteraction(
  params: UseVideoKonvaInteractionParams,
): VideoKonvaInteraction {
  const [drag, setDrag] = useState<VideoDragState>(null);
  const dragRef = useRef<VideoDragState>(null);
  dragRef.current = drag;
  const drawingOwnerRef = useRef<
    (Omit<VideoDrawingDraft, "continuingTrack"> & { continuingTrack: boolean }) | null
  >(null);

  const getDrawingDraft = useCallback((): VideoDrawingDraft | null => {
    const owner = drawingOwnerRef.current;
    if (dragRef.current?.kind !== "draw" || !owner) return null;
    return {
      kind: owner.kind,
      tool: owner.tool,
      frameIndex: owner.frameIndex,
      ...(owner.continuingTrack ? { continuingTrack: true as const } : {}),
    };
  }, []);
  const discardDrawingDraft = useCallback(() => {
    if (dragRef.current?.kind !== "draw") return;
    dragRef.current = null;
    drawingOwnerRef.current = null;
    setDrag(null);
  }, []);

  const paramsRef = useRef(params);
  paramsRef.current = params;

  const pointFromClient = useCallback((clientX: number, clientY: number) => {
    const p = paramsRef.current;
    const rect = p.containerRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return clientToVideoNorm(clientX, clientY, rect, p.vpRef.current, p.size);
  }, []);

  const beginDraw = useCallback(
    (native: PointerEvent) => {
      const p = paramsRef.current;
      if (p.readOnly || p.isPlaybackActive) return;
      if (isSamProbeTool(p.videoTool)) {
        const probePt = pointFromClient(native.clientX, native.clientY);
        if (!probePt) return;
        p.onSelect(null);
        setDrag({
          kind: "samProbe",
          mode: samProbeMode(p.videoTool),
          start: probePt,
          current: probePt,
          alt: !!native.altKey || p.samPolarity === "negative",
        });
        return;
      }
      if (!p.creationEnabled) return;
      const trackLocked = p.selectedTrack
        ? p.lockedTrackIds.has(p.selectedTrack.geometry.track_id)
        : false;
      if (p.videoTool === "track" && trackLocked) return;
      const pt = pointFromClient(native.clientX, native.clientY);
      if (!pt) return;
      if (p.videoTool !== "track" || !p.selectedTrack) p.onSelect(null);
      drawingOwnerRef.current = {
        kind: "box",
        tool: p.videoTool,
        frameIndex: p.frameIndex,
        continuingTrack:
          p.videoTool === "track" &&
          !!p.selectedTrack &&
          !p.selectedTrack.geometry.keyframes.some((kf) => kf.frame_index === p.frameIndex),
      };
      const nextDrag = { kind: "draw", start: pt, current: pt } as const;
      dragRef.current = nextDrag;
      setDrag(nextDrag);
    },
    [pointFromClient],
  );

  const beginMove = useCallback(
    (hit: VideoPickable, native: PointerEvent) => {
      const p = paramsRef.current;
      const ann = p.annotations.find((a) => a.id === hit.id);
      const trackId = ann ? trackIdOf(ann) : null;
      const toggle = native.shiftKey || native.metaKey || native.ctrlKey;
      if (toggle) {
        p.onSelect(hit.id, { shift: true });
        return;
      }
      p.onSelect(hit.id);
      if (p.readOnly || p.isPlaybackActive || (trackId && p.lockedTrackIds.has(trackId))) return;
      const pt = pointFromClient(native.clientX, native.clientY);
      if (!pt) return;
      const pts = pointsAtFrame(ann, p.frameIndex);
      if (pts && ann) {
        const holes = isVideoPolygon(ann) ? ann.geometry.holes : undefined;
        const translationBounds =
          isVideoPolygon(ann) || isVideoPolygonTrack(ann)
            ? polygonTranslationBounds(pts, holes)
            : undefined;
        setDrag({
          kind: "polyMove",
          id: hit.id,
          frameIndex: p.frameIndex,
          start: pt,
          origin: pts,
          current: pts,
          originHoles: holes,
          currentHoles: holes,
          translationBounds,
        });
        return;
      }
      if (ann && isVideoRotatedBbox(ann)) {
        setDrag({
          kind: "obbMove",
          id: hit.id,
          start: pt,
          origin: ann.geometry,
          current: ann.geometry,
          imageSize: p.size,
        });
        return;
      }
      if (ann && isVideoKeypoint(ann)) return;
      setDrag({ kind: "move", id: hit.id, start: pt, origin: hit.geom, current: hit.geom });
    },
    [pointFromClient],
  );

  const onResizeHandlePointerDown = useCallback(
    (
      dir: VideoResizeDirection,
      entryId: string,
      geom: VideoStageGeom,
      e: Konva.KonvaEventObject<PointerEvent>,
    ) => {
      e.cancelBubble = true;
      const native = e.evt;
      const p = paramsRef.current;
      const ann = p.annotations.find((a) => a.id === entryId);
      const trackId = ann && isVideoTrack(ann) ? ann.geometry.track_id : null;
      p.onSelect(entryId);
      if (p.readOnly || p.isPlaybackActive || (trackId && p.lockedTrackIds.has(trackId))) return;
      const pt = pointFromClient(native.clientX, native.clientY);
      if (!pt) return;
      setDrag({ kind: "resize", id: entryId, dir, start: pt, origin: geom, current: geom });
    },
    [pointFromClient],
  );

  const onVertexPointerDown = useCallback(
    (
      entryId: string,
      vidx: number,
      points: [number, number][],
      e: Konva.KonvaEventObject<PointerEvent>,
    ) => {
      e.cancelBubble = true;
      if (e.evt.button !== 0) return;
      const p = paramsRef.current;
      p.onSelect(entryId);
      const ann = p.annotations.find((a) => a.id === entryId);
      const trackId = ann ? trackIdOf(ann) : null;
      if (p.readOnly || p.isPlaybackActive || (trackId && p.lockedTrackIds.has(trackId))) return;
      const pt = pointFromClient(e.evt.clientX, e.evt.clientY);
      if (!pt) return;
      setDrag({
        kind: "polyVertex",
        id: entryId,
        frameIndex: p.frameIndex,
        vidx,
        start: pt,
        origin: points,
        current: points,
      });
    },
    [pointFromClient],
  );

  const onObbResizePointerDown = useCallback(
    (
      dir: VideoResizeDirection,
      entryId: string,
      geometry: VideoRotatedBboxGeometry,
      e: Konva.KonvaEventObject<PointerEvent>,
    ) => {
      e.cancelBubble = true;
      if (e.evt.button !== 0) return;
      const p = paramsRef.current;
      p.onSelect(entryId);
      if (p.readOnly || p.isPlaybackActive) return;
      const pt = pointFromClient(e.evt.clientX, e.evt.clientY);
      if (!pt) return;
      setDrag({
        kind: "obbResize",
        id: entryId,
        dir,
        start: pt,
        origin: geometry,
        current: geometry,
        imageSize: p.size,
      });
    },
    [pointFromClient],
  );

  const onObbRotatePointerDown = useCallback(
    (
      entryId: string,
      geometry: VideoRotatedBboxGeometry,
      e: Konva.KonvaEventObject<PointerEvent>,
    ) => {
      e.cancelBubble = true;
      if (e.evt.button !== 0) return;
      const p = paramsRef.current;
      p.onSelect(entryId);
      if (p.readOnly || p.isPlaybackActive) return;
      const pt = pointFromClient(e.evt.clientX, e.evt.clientY);
      if (!pt) return;
      setDrag({
        kind: "obbRotate",
        id: entryId,
        origin: geometry,
        current: geometry,
        startPointerAngle: pointerAngle(pt, geometry, p.size),
        imageSize: p.size,
      });
    },
    [pointFromClient],
  );

  const onKeypointPointerDown = useCallback(
    (
      entryId: string,
      nodeIdx: number,
      points: Keypoint[],
      e: Konva.KonvaEventObject<PointerEvent>,
    ) => {
      e.cancelBubble = true;
      if (e.evt.button !== 0) return;
      const p = paramsRef.current;
      p.onSelect(entryId);
      if (p.readOnly || p.isPlaybackActive) return;
      const point = points[nodeIdx];
      const ann = p.annotations.find((item) => item.id === entryId);
      if (!point || point.v === 0 || !ann || !isVideoKeypoint(ann)) return;
      if (e.evt.altKey) {
        p.onUpdate(ann, {
          ...ann.geometry,
          points: points.map((item, index) =>
            index === nodeIdx ? { ...item, v: (item.v === 1 ? 2 : 1) as Keypoint["v"] } : item,
          ),
        });
        return;
      }
      setDrag({ kind: "keypointNode", id: entryId, nodeIdx, origin: points, current: points });
    },
    [],
  );

  const onStagePointerDown = useCallback(
    (e: Konva.KonvaEventObject<PointerEvent>) => {
      const native = e.evt;
      if (native.button !== 0) return;
      const p = paramsRef.current;
      const pt = pointFromClient(native.clientX, native.clientY);
      if (!pt) return;
      if (isSamProbeTool(p.videoTool)) {
        beginDraw(native);
        return;
      }
      const maskHit = pickTopVideoMaskAt(p.maskEntries, pt);
      if (maskHit) {
        p.onSelect(maskHit.id, { shift: native.shiftKey || native.metaKey || native.ctrlKey });
        return;
      }
      const pickables: VideoPickable[] = [
        ...p.carryOverGhosts,
        ...p.entries,
        ...(p.ghost ? [p.ghost] : []),
      ];
      const hit = pickTopVideoEntryAt(pickables, pt, { size: p.size });
      if (!hit) {
        if (p.videoTool === "select") {
          p.onSelect(null);
          return;
        }
        beginDraw(native);
        return;
      }
      if (p.carryOverGhosts.some((g) => g.id === hit.id)) {
        p.onSelect(hit.id, { shift: native.shiftKey || native.metaKey || native.ctrlKey });
        return;
      }
      beginMove(hit, native);
    },
    [beginDraw, beginMove, pointFromClient],
  );

  const commit = useCallback((finalDrag: VideoDragState, finalPt: { x: number; y: number }) => {
    const p = paramsRef.current;
    const action = resolveDragCommit(finalDrag, finalPt, {
      annotations: p.annotations,
      videoTool: p.videoTool,
      selectedTrack: p.selectedTrack,
      lockedTrackIds: p.lockedTrackIds,
      frameIndex: p.frameIndex,
    });
    if (action.type === "none") return;
    if (action.type === "draw") {
      const { kind, geom } = action;
      if (p.onPendingDraw) {
        const rect = p.containerRef.current?.getBoundingClientRect();
        const anchorPt = rect
          ? videoNormToClient({ x: geom.x, y: geom.y + geom.h }, rect, p.vpRef.current, p.size)
          : { x: 0, y: 0 };
        p.onPendingDraw(kind, p.frameIndex, geom, { left: anchorPt.x, top: anchorPt.y + 6 });
      } else {
        p.onCreate(p.frameIndex, geom);
      }
      return;
    }
    if (action.type === "track") {
      const trackGeom = action.ann.geometry as VideoTrackGeometry;
      const wasContinue = !trackGeom.keyframes.some((kf) => kf.frame_index === p.frameIndex);
      p.onUpdate(action.ann, upsertKeyframe(trackGeom, p.frameIndex, action.geom));
      if (p.trackContinueAutoAdvance && wasContinue) {
        const next = p.carryOverGhosts.find((g) => g.id !== action.ann.id);
        if (next) p.onSelect(next.id);
      }
      return;
    }
    if (action.type === "poly") {
      if (p.readOnly || p.isPlaybackActive) return;
      const g = action.ann.geometry;
      if (g.type === "video_polygon") {
        p.onUpdate(action.ann, {
          ...g,
          points: action.points,
          ...(action.holes ? { holes: action.holes } : {}),
        });
      } else if (g.type === "video_polyline") {
        p.onUpdate(action.ann, { ...g, points: action.points });
      } else if (g.type === "video_track_polygon") {
        p.onUpdate(action.ann, upsertPointsKeyframe(g, p.frameIndex, action.points));
      } else if (g.type === "video_track_polyline") {
        p.onUpdate(action.ann, upsertPointsKeyframe(g, p.frameIndex, action.points));
      }
      return;
    }
    if (action.type === "rotated") {
      p.onUpdate(action.ann, action.geometry);
      return;
    }
    if (action.type === "keypoint") {
      const geometry = action.ann.geometry as VideoKeypointGeometry;
      p.onUpdate(action.ann, { ...geometry, points: action.points });
      return;
    }
    if (action.type === "samProbe") {
      p.onSamPrompt?.(
        action.mode === "point"
          ? { mode: "point", pt: action.pt, alt: action.alt }
          : { mode: action.mode, bbox: action.bbox, alt: action.alt },
      );
      return;
    }
    const bbox = action.ann.geometry as VideoBboxGeometry;
    p.onUpdate(action.ann, { type: "video_bbox", frame_index: bbox.frame_index, ...action.geom });
  }, []);

  const interacting = !!drag && drag.kind !== "pan";
  useEffect(() => {
    if (!interacting) return;
    let lastPt: { x: number; y: number } | null = null;
    const onMove = (ev: PointerEvent) => {
      const pt = pointFromClient(ev.clientX, ev.clientY);
      if (!pt) return;
      lastPt = pt;
      setDrag((cur) => advanceDrag(cur, pt, { shiftKey: ev.shiftKey, altKey: ev.altKey }));
    };
    const onUp = (ev: PointerEvent) => {
      const finalDrag = dragRef.current;
      const pt = pointFromClient(ev.clientX, ev.clientY) ?? lastPt;
      drawingOwnerRef.current = null;
      dragRef.current = null;
      setDrag(null);
      if (!pt) return;
      if (finalDrag?.kind === "polyMove" || finalDrag?.kind === "polyVertex") {
        if (pt.x === finalDrag.start.x && pt.y === finalDrag.start.y) return;
        commit(advanceDrag(finalDrag, pt), pt);
      } else {
        commit(finalDrag, pt);
      }
    };
    const onCancel = () => {
      dragRef.current = null;
      drawingOwnerRef.current = null;
      setDrag(null);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    };
  }, [interacting, pointFromClient, commit]);

  return {
    drag,
    getDrawingDraft,
    discardDrawingDraft,
    continuingTrack: drag?.kind === "draw" && !!drawingOwnerRef.current?.continuingTrack,
    onStagePointerDown,
    onResizeHandlePointerDown,
    onVertexPointerDown,
    onObbResizePointerDown,
    onObbRotatePointerDown,
    onKeypointPointerDown,
  };
}
