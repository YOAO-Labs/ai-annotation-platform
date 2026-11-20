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

/**
 * 视频 Konva 栈交互状态机(画框/移动/缩放/平移分流的 draw/move/resize 部分)。
 *
 * 镜像旧 SVG 栈 VideoStage 的 beginDraw/beginMove/beginResize/onPointerMove/finishDrag,
 * 但坐标源从 SVG CTM 换成 Konva 像素空间(videoKonvaCoordinates.clientToVideoNorm)。
 * **拖拽计算与提交语义复用既有纯函数**(applyResize/clampGeom/normalizeGeom/upsertKeyframe),
 * 命中复用 pickTopVideoEntryAt(同一 z 序 + padding),避免行为漂移。
 *
 * 平移(右键 / Space+左键)仍由 VideoKonvaStage 容器层处理,不在本模块。
 */

/** 画框/缩放的最小有效尺寸(归一化);与旧栈 finishDrag 的 0.003 阈值一致。 */
export const VIDEO_MIN_BOX = 0.003;
/** SAM 提示框的最小拖拽边长 (图幅比例)，与图片侧 ImageStage 同值。 */
export const SAM_MIN_DRAG = 0.005;

/** 交互式 SAM 提示工具 —— 不画几何, 松手派发 onSamPrompt 请求候选。 */
export function isSamProbeTool(t: VideoTool): boolean {
  return t === "smart-point" || t === "smart-box" || t === "exemplar" || t === "magic-box";
}

/**
 * 候选需要用户逐个挑选 (Tab 切换 / Enter 采纳) 的 AI 工具。
 * magic-box 例外: 单候选、候选一到就自动弹类选择器, 键盘导航对它无意义 (与图片侧一致)。
 */
export function isSamCandidateNavTool(t: VideoTool): boolean {
  return isSamProbeTool(t) && t !== "magic-box";
}

/** 提示形态: point 是零位移点击; bbox(interactive_box) 与 exemplar(视觉示例框) 都是拖框。 */
export function samProbeMode(t: VideoTool): "point" | "bbox" | "exemplar" {
  if (t === "smart-point") return "point";
  if (t === "exemplar") return "exemplar";
  // magic-box 骑 interactive_box prompt; 差异只在采纳时收紧成外接框。
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

/** 拖拽过程推进:给定当前 drag + 新归一化点,返回下一 drag(纯函数,与 onPointerMove 对齐)。 */
export function advanceDrag(
  drag: VideoDragState,
  pt: VideoStageGeom | { x: number; y: number },
  modifiers: DragModifiers = {},
): VideoDragState {
  if (!drag || drag.kind === "pan") return drag;
  if (drag.kind === "draw") return { ...drag, current: { x: pt.x, y: pt.y } };
  // SAM 提示框的实时预览 (point 模式下 current 恒等于 start)。
  if (drag.kind === "samProbe") return { ...drag, current: { x: pt.x, y: pt.y } };
  // 单帧 polygon/polyline 编辑: 拖顶点 / 整体平移 (origin 是 points 数组)。
  if (drag.kind === "polyVertex") {
    return { ...drag, current: moveVertex(drag.origin, drag.vidx, [clamp01(pt.x), clamp01(pt.y)]) };
  }
  if (drag.kind === "polyMove") {
    const dx = pt.x - drag.start.x;
    const dy = pt.y - drag.start.y;
    return {
      ...drag,
      current: drag.origin.map(
        ([px, py]) => [clamp01(px + dx), clamp01(py + dy)] as [number, number],
      ),
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

/** 提交动作(纯描述,由 hook 落到回调);不含 DOM/anchor 计算,便于单测。 */
export type VideoDragCommit =
  | { type: "none" }
  | {
      type: "draw";
      kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox";
      geom: VideoStageGeom;
    }
  | { type: "track"; ann: AnnotationResponse; geom: VideoStageGeom }
  | { type: "bbox"; ann: AnnotationResponse; geom: VideoStageGeom }
  // 单帧 polygon/polyline 顶点拖拽 / 整体平移的提交。
  | { type: "poly"; ann: AnnotationResponse; points: [number, number][] }
  | { type: "rotated"; ann: AnnotationResponse; geometry: VideoRotatedBboxGeometry }
  | { type: "keypoint"; ann: AnnotationResponse; points: Keypoint[] }
  // 交互式 SAM 提示: 不建标注, 交给 onSamPrompt 请求候选 (采纳时才落库)。
  // 坐标归一化 [0,1]; bbox 形如 [x1,y1,x2,y2] (与图片侧 onSamPrompt 同契约)。
  | { type: "samProbe"; mode: "point"; pt: [number, number]; alt: boolean }
  // bbox = interactive_box 提示; exemplar = 视觉示例框 (同为拖框, 但派发到 runExemplar)。
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
  /** 当前帧号:用于「选中轨迹该帧已有关键帧时,同帧再画框判为新建而非覆盖」的分流。 */
  frameIndex: number;
}

/** 松手提交:复刻 finishDrag 分支(draw → 建框/落关键帧;move/resize → 更新 track/bbox)。 */
export function resolveDragCommit(
  drag: VideoDragState,
  finalPt: { x: number; y: number },
  ctx: ResolveDragCommitCtx,
): VideoDragCommit {
  if (!drag || drag.kind === "pan") return { type: "none" };
  const { annotations, videoTool, selectedTrack, lockedTrackIds, frameIndex } = ctx;

  // 交互式 SAM 提示 (复刻图片侧 ImageStage 的 samProbe 派发)。
  if (drag.kind === "samProbe") {
    if (drag.mode === "point") {
      return { type: "samProbe", mode: "point", pt: [drag.start.x, drag.start.y], alt: drag.alt };
    }
    const x1 = Math.min(drag.start.x, finalPt.x);
    const y1 = Math.min(drag.start.y, finalPt.y);
    const x2 = Math.max(drag.start.x, finalPt.x);
    const y2 = Math.max(drag.start.y, finalPt.y);
    // 最小拖拽阈值 (图幅的 0.5%): 误点当框会让后端拿到退化 bbox。与图片侧同值。
    if (x2 - x1 <= SAM_MIN_DRAG || y2 - y1 <= SAM_MIN_DRAG) return { type: "none" };
    return { type: "samProbe", mode: drag.mode, bbox: [x1, y1, x2, y2], alt: drag.alt };
  }

  if (drag.kind === "draw") {
    if (videoTool === "select") return { type: "none" };
    const geom = normalizeGeom(drag.start, finalPt);
    if (geom.w < VIDEO_MIN_BOX || geom.h < VIDEO_MIN_BOX) return { type: "none" };
    // track 工具且选中轨迹未锁:画框落到该轨迹关键帧(而非建独立框)——但仅当当前帧「尚无关键帧」时。
    // 断吞框:同帧再画框(该帧已有关键帧)几乎总是「标第二个物体」,而非重做本帧关键帧。
    // 此时跳过 track 分支、落到下方 draw 新建分支,避免 upsertKeyframe 静默替换第一个框。
    // 跨帧画框(该帧无关键帧)仍延展当前轨迹,保留插值这一轨迹标注的核心价值。
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

  // polygon/polyline 顶点/整体编辑: 落新 points (self-intersection 不阻断, 与图片侧一致仅作视觉提示)。
  // 单帧 → 替换 points; 轨迹 → 由 commit 在当前帧 upsert 关键帧 (据几何类型分流)。
  if (drag.kind === "polyVertex" || drag.kind === "polyMove") {
    const polyAnn = annotations.find((a) => a.id === drag.id);
    const isPoly =
      polyAnn &&
      (isVideoPolygon(polyAnn) ||
        isVideoPolyline(polyAnn) ||
        isVideoPolygonTrack(polyAnn) ||
        isVideoPolylineTrack(polyAnn));
    if (!polyAnn || !isPoly) return { type: "none" };
    return { type: "poly", ann: polyAnn, points: drag.current };
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

/** 任意视频轨迹几何(bbox / polygon / polyline)的 track_id;非轨迹返回 null。 */
function trackIdOf(ann: AnnotationResponse): string | null {
  if (isVideoTrack(ann) || isVideoPolygonTrack(ann) || isVideoPolylineTrack(ann)) {
    return ann.geometry.track_id;
  }
  return null;
}

/**
 * polygon/polyline 几何在某帧的可编辑顶点(整体平移的 origin);非点集几何返回 null。
 * 单帧取几何自身 points;轨迹解析当前帧(精确关键帧 / 插值),outside 帧无解析 → null。
 */
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

/** 命中候选(轻量视图,仅命中所需的 id + geom)。 */
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
  /** 当前帧可命中的框(含 track 解析帧 + legacy bbox),归一化 geom。 */
  entries: VideoPickable[];
  /** 已解码的提交态掩码；命中按 alpha，而不是外接框。 */
  maskEntries: VideoMaskRenderRecord[];
  /** 选中轨迹当前帧无框时的参考 ghost(可拖出关键帧),归一化 geom。 */
  ghost: VideoPickable | null;
  /**
   * 跨网格帧续写参考框(非选中待续轨迹);点击 → 仅选中该轨迹(随后画框 / 拖其 ghost 续写),
   * 不直接落关键帧。优先级最低(实框 / 选中 ghost 覆盖时让位)。
   */
  carryOverGhosts: VideoPickable[];
  selectedTrack: (AnnotationResponse & { geometry: VideoTrackGeometry }) | null;
  videoTool: VideoTool;
  readOnly: boolean;
  isPlaybackActive: boolean;
  lockedTrackIds: Set<string>;
  creationEnabled: boolean;
  frameIndex: number;
  /** 续写后自动前进(video.trackContinueAutoAdvance);续写完一条 → 选中下一条待续 carryOver。 */
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
  /**
   * 交互式 SAM 提示松手回调 (smart-point / smart-box)。
   * 坐标归一化 [0,1]，与图片侧 onSamPrompt 同契约；由 shell 取当前帧图请求候选。
   */
  onSamPrompt?: (prompt: VideoSamPrompt) => void;
  /** 工具条上的正/负切换; 与 Alt 等价 (图片侧 SmartPointTool / ExemplarTool 同语义)。 */
  samPolarity?: "positive" | "negative";
}

export interface VideoKonvaInteraction {
  drag: VideoDragState;
  getDrawingDraft: () => VideoDrawingDraft | null;
  discardDrawingDraft: () => void;
  continuingTrack: boolean;
  /** 接到 Konva Stage 的 onPointerDown:空白拖→画框,命中→移动。 */
  onStagePointerDown: (e: Konva.KonvaEventObject<PointerEvent>) => void;
  /** 接到 resize 句柄的 onPointerDown(需 cancelBubble 防冒泡到 Stage)。 */
  onResizeHandlePointerDown: (
    dir: VideoResizeDirection,
    entryId: string,
    geom: VideoStageGeom,
    e: Konva.KonvaEventObject<PointerEvent>,
  ) => void;
  /** 接到 polygon/polyline 顶点句柄的 onPointerDown(拖顶点)。 */
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

/**
 * 视频 Konva 交互 hook:持有 drag 状态,分流 pointerdown,拖拽过程/松手挂 window 跟踪。
 * 易变输入用 ref 快照,使 window 监听只在「是否拖拽中」切换时装卸一次,避免逐帧重装。
 */
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
    // Clear the event owner before React renders, so a trailing pointerup cannot commit it.
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
      // 交互式 SAM 工具不画几何, 起 samProbe 拖拽 (point 零位移 / bbox 拖框)。
      // 放在 creationEnabled 之前: 提示不创建标注, 其可用性已由工具栏三层门控裁决。
      if (isSamProbeTool(p.videoTool)) {
        const probePt = pointFromClient(native.clientX, native.clientY);
        if (!probePt) return;
        p.onSelect(null);
        setDrag({
          kind: "samProbe",
          mode: samProbeMode(p.videoTool),
          start: probePt,
          current: probePt,
          // 负点 = Alt 按住 或 工具条切到「负向」(与图片侧 SmartPointTool 同式)。
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
      // polygon/polyline: 命中框内 → 整体平移 (origin 是 points, 非 bbox geom)。
      // 单帧取几何自身 points; 轨迹取当前帧解析后的多边形/折线 (插值帧也可整体移动 → 物化关键帧)。
      const pts = pointsAtFrame(ann, p.frameIndex);
      if (pts) {
        setDrag({ kind: "polyMove", id: hit.id, start: pt, origin: pts, current: pts });
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

  // 单帧 polygon/polyline 顶点句柄按下: 拖该顶点。cancelBubble 防冒泡到 Stage 触发平移/选择。
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
      if (native.button !== 0) return; // 右键/中键平移由容器层处理
      const p = paramsRef.current;
      const pt = pointFromClient(native.clientX, native.clientY);
      if (!pt) return;
      // AI 工具下不做命中拾取: 点在已有标注上也应发 SAM 提示 (对齐图片侧)。
      if (isSamProbeTool(p.videoTool)) {
        beginDraw(native);
        return;
      }
      const maskHit = pickTopVideoMaskAt(p.maskEntries, pt);
      if (maskHit) {
        p.onSelect(maskHit.id, { shift: native.shiftKey || native.metaKey || native.ctrlKey });
        return;
      }
      // z 序:carryOver(最低) → entries → 选中 ghost(最高);pickTop 逆序取「最后命中」为 top,
      // 故 carryOver 置首,让实框 / 选中 ghost 覆盖时优先。
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
      // 点中待续轨迹 ghost → 仅选中(不落关键帧);续写交给随后画框(WS1)或拖其 ghost。
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
      // 「续写」判定:此前当前帧无关键帧(延展),而非移动/缩放已有帧。仅续写才触发自动前进。
      const wasContinue = !trackGeom.keyframes.some((kf) => kf.frame_index === p.frameIndex);
      p.onUpdate(action.ann, upsertKeyframe(trackGeom, p.frameIndex, action.geom));
      // 续写后自动前进:选中下一条待续轨迹(carryOverGhosts 已排除当前选中,取首个)。
      if (p.trackContinueAutoAdvance && wasContinue) {
        const next = p.carryOverGhosts.find((g) => g.id !== action.ann.id);
        if (next) p.onSelect(next.id);
      }
      return;
    }
    if (action.type === "poly") {
      const g = action.ann.geometry;
      // 单帧: 替换 points, 保留 frame_index / holes。
      if (g.type === "video_polygon") {
        p.onUpdate(action.ann, { ...g, points: action.points });
      } else if (g.type === "video_polyline") {
        p.onUpdate(action.ann, { ...g, points: action.points });
        // 轨迹: 在当前帧 upsert 一个 manual 关键帧 (精确帧替换 / 插值帧物化)。
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
    // 交互式 SAM 提示: 不建标注, 交给 shell 请求候选 (采纳时才落库)。
    if (action.type === "samProbe") {
      p.onSamPrompt?.(
        action.mode === "point"
          ? { mode: "point", pt: action.pt, alt: action.alt }
          : { mode: action.mode, bbox: action.bbox, alt: action.alt },
      );
      return;
    }
    // bbox:替换该帧几何。
    const bbox = action.ann.geometry as VideoBboxGeometry;
    p.onUpdate(action.ann, { type: "video_bbox", frame_index: bbox.frame_index, ...action.geom });
  }, []);

  // 拖拽中(draw/move/resize)挂 window 跟踪;pan 不经此(容器层处理)。
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
      // Commit callbacks may select the next track; the completed drag no longer owns a draft.
      drawingOwnerRef.current = null;
      dragRef.current = null;
      setDrag(null);
      if (pt) commit(finalDrag, pt);
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
