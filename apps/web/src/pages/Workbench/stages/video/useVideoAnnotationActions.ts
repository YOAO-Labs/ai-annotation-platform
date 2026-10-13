import { useCallback, useEffect, useRef } from "react";
import { confirmDialog } from "@/components/ui/decisionDialog";
import type { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { tasksApi, type AnnotationPayload } from "@/api/tasks";
import { rasterMasksApi } from "@/api/rasterMasks";
import { videoTrackerApi } from "@/api/videoTracker";
import { cocoRleBounds, type CocoRle } from "../../stage/shared/geometry/maskRle";
import type {
  AnnotationResponse,
  CocoRleMaskRef,
  Geometry,
  VideoBboxGeometry,
  VideoPolygonGeometry,
  VideoPolylineGeometry,
  VideoRotatedBboxGeometry,
  VideoKeypointGeometry,
  Keypoint,
  VideoTrackGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
  VideoMaskGeometry,
  VideoTrackMaskGeometry,
} from "@/types";
import { UNKNOWN_CLASS } from "../../stage/colors";
import { enqueue as enqueueOffline, type OfflineOp } from "../../state/offlineQueue";
import type { useAnnotationHistory, Command } from "../../state/useAnnotationHistory";
import { randomId } from "@/utils/id";
import type { PendingDrawing, useWorkbenchState } from "../../state/useWorkbenchState";
import {
  buildVideoKeyframeCommand,
  propagateKeyframes,
  type VideoPropagateOptions,
  type VideoTrackKeyframeWithAttrs,
} from "../../state/videoTrackCommands";
import {
  isAnyVideoSingleFrame,
  isAnyVideoTrack,
  nearestTrackBbox,
  upsertKeyframe,
} from "../../stage/videoStageGeometry";
import { removeOutsideFrame } from "../../stage/videoTrackOutside";

type Geom = { x: number; y: number; w: number; h: number };
type VideoGeometry =
  | VideoBboxGeometry
  | VideoTrackGeometry
  | VideoPolygonGeometry
  | VideoPolylineGeometry
  | VideoRotatedBboxGeometry
  | VideoKeypointGeometry
  | VideoTrackPolygonGeometry
  | VideoTrackPolylineGeometry
  | VideoMaskGeometry
  | VideoTrackMaskGeometry;

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

interface VideoAnnotationMutations {
  create: {
    mutate: (
      p: AnnotationPayload,
      opts?: { onSuccess?: (a: AnnotationResponse) => void; onError?: (e: unknown) => void },
    ) => void;
  };
  update: {
    mutate: (
      vars: { annotationId: string; payload: Partial<AnnotationPayload> },
      opts?: { onSuccess?: () => void; onError?: (e: unknown) => void; onSettled?: () => void },
    ) => void;
  };
  delete: {
    mutate: (
      id: string,
      opts?: { onSuccess?: () => void; onError?: (e: unknown) => void; onSettled?: () => void },
    ) => void;
  };
}

interface UseVideoAnnotationActionsArgs {
  taskId: string | undefined;
  meUserId?: string | null;
  queryClient: QueryClient;
  history: ReturnType<typeof useAnnotationHistory>;
  s: ReturnType<typeof useWorkbenchState>;
  annotationsRef: { current: AnnotationResponse[] };
  pushToast: (toast: ToastInput) => void;
  recordRecentClass: (cls: string) => void;
  optimisticEnqueueCreate: (payload: AnnotationPayload) => void;
  enqueueOnError: (err: unknown, fallback: () => void) => void;
  mutations: VideoAnnotationMutations;
  activeToolHasOwnClasses?: boolean;
  annotationQueryKey?: readonly unknown[];
}

export interface VideoConvertOptions {
  operation: "copy" | "split";
  scope: "frame" | "track";
  frameIndex?: number;
  frameMode?: "keyframes" | "all_frames";
}

export interface VideoTrackCompositionOptions {
  operation: "aggregate_bboxes" | "split_track" | "merge_tracks" | "join_tracks";
  annotationIds: string[];
  frameIndex?: number;
  deleteSources?: boolean;
  // v0.10.30 · 2.5 join: gap 填充模式, 仅 join_tracks 透传给后端 gap_mode。
  gapMode?: "interpolate" | "outside";
}

export interface SavedVideoMaskKeyframe {
  annotation: AnnotationResponse;
  mask: CocoRleMaskRef;
  frameIndex: number;
}

export function buildVideoCreatePayload(
  kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
  frameIndex: number,
  geo: Geom,
  cls: string,
): AnnotationPayload {
  const className = cls || UNKNOWN_CLASS;
  if (kind === "video_rotated_bbox") {
    return {
      annotation_type: kind,
      class_name: className,
      tool_unit_id: "rotated_bbox",
      geometry: {
        type: kind,
        frame_index: frameIndex,
        cx: geo.x + geo.w / 2,
        cy: geo.y + geo.h / 2,
        w: geo.w,
        h: geo.h,
        angle: 0,
      },
    };
  }
  if (kind === "video_bbox") {
    return {
      annotation_type: "video_bbox",
      class_name: className,
      geometry: { type: "video_bbox", frame_index: frameIndex, ...geo },
    };
  }

  const trackId = `trk_${randomId()}`;
  const geometry: VideoTrackGeometry = {
    type: "video_track_bbox",
    track_id: trackId,
    keyframes: [
      {
        frame_index: frameIndex,
        bbox: geo,
        source: "manual",
        occluded: false,
      },
    ],
  };

  return {
    annotation_type: "video_track_bbox",
    class_name: className,
    geometry,
  };
}

export function buildVideoKeypointCreatePayload(
  frameIndex: number,
  points: Keypoint[],
  cls: string,
): AnnotationPayload {
  return {
    annotation_type: "video_keypoint",
    class_name: cls || UNKNOWN_CLASS,
    tool_unit_id: "keypoint",
    geometry: { type: "video_keypoint", frame_index: frameIndex, points },
  };
}

/** v0.21.21 · 由绘制的归一化顶点新建单帧 polygon/polyline (当前帧, 非 track)。 */
export function buildVideoPointsCreatePayload(
  type: "video_polygon" | "video_polyline",
  frameIndex: number,
  points: [number, number][],
  cls: string,
): AnnotationPayload {
  const className = cls || UNKNOWN_CLASS;
  return {
    annotation_type: type,
    class_name: className,
    // 多边形归 region 单位、折线归 polyline 单位 (对齐图片, 各自类别/属性隔离)。
    tool_unit_id: type === "video_polyline" ? "polyline" : "region",
    geometry: { type, frame_index: frameIndex, points },
  };
}

/** v0.21.20 · 由绘制的归一化顶点新建 polygon/polyline track (单关键帧于当前帧)。 */
export function buildVideoPointsTrackCreatePayload(
  type: "video_track_polygon" | "video_track_polyline",
  frameIndex: number,
  points: [number, number][],
  cls: string,
): AnnotationPayload {
  const className = cls || UNKNOWN_CLASS;
  const trackId = `trk_${randomId()}`;
  const geometry: VideoTrackPolygonGeometry | VideoTrackPolylineGeometry = {
    type,
    track_id: trackId,
    keyframes: [{ frame_index: frameIndex, points, source: "manual", occluded: false }],
  };
  return {
    annotation_type: type,
    class_name: className,
    // 轨迹多边形归 region、轨迹折线归 polyline (与单帧同单位, 变体不同)。
    tool_unit_id: type === "video_track_polyline" ? "polyline" : "region",
    geometry,
  };
}

export function buildVideoMaskTrackCreatePayload(
  frameIndex: number,
  mask: CocoRleMaskRef,
  cls: string,
): AnnotationPayload {
  const geometry: VideoTrackMaskGeometry = {
    type: "video_track_mask",
    track_id: `trk_${randomId()}`,
    keyframes: [{ frame_index: frameIndex, mask, source: "manual", occluded: false }],
  };
  return {
    annotation_type: "video_track_mask",
    class_name: cls || UNKNOWN_CLASS,
    tool_unit_id: "region",
    geometry,
  };
}

export function buildVideoMaskCreatePayload(
  frameIndex: number,
  mask: CocoRleMaskRef,
  cls: string,
): AnnotationPayload {
  const geometry: VideoMaskGeometry = {
    type: "video_mask",
    frame_index: frameIndex,
    mask,
  };
  return {
    annotation_type: "video_mask",
    class_name: cls || UNKNOWN_CLASS,
    tool_unit_id: "region",
    geometry,
  };
}

export function upsertVideoMaskKeyframe(
  geometry: VideoTrackMaskGeometry,
  frameIndex: number,
  mask: CocoRleMaskRef,
): VideoTrackMaskGeometry {
  const visibleGeometry = removeOutsideFrame(geometry, frameIndex);
  const previous = visibleGeometry.keyframes.find(
    (keyframe) => keyframe.frame_index === frameIndex,
  );
  return {
    ...visibleGeometry,
    keyframes: [
      ...visibleGeometry.keyframes.filter((keyframe) => keyframe.frame_index !== frameIndex),
      {
        frame_index: frameIndex,
        mask,
        source: "manual" as const,
        occluded: previous?.occluded ?? false,
        ...(previous?.attributes !== undefined ? { attributes: previous.attributes } : {}),
      },
    ].sort((a, b) => a.frame_index - b.frame_index),
  };
}

export function buildVideoUpdateCommand(ann: AnnotationResponse, geometry: VideoGeometry): Command {
  if (ann.geometry.type === "video_track_bbox" && geometry.type === "video_track_bbox") {
    const keyframeCommand = buildVideoKeyframeCommand(ann.id, ann.geometry, geometry);
    if (keyframeCommand) return keyframeCommand;
  }
  return {
    kind: "update",
    annotationId: ann.id,
    before: { geometry: ann.geometry },
    after: { geometry },
  };
}

export function buildVideoCompositionCommands(
  beforeAnnotations: AnnotationResponse[],
  result: {
    updated_annotations: AnnotationResponse[];
    created_annotations: AnnotationResponse[];
    deleted_annotation_ids: string[];
  },
): Exclude<Command, { kind: "batch" | "slice" }>[] {
  const beforeById = new Map(beforeAnnotations.map((ann) => [ann.id, ann]));
  const commands: Exclude<Command, { kind: "batch" | "slice" }>[] = [];

  for (const updated of result.updated_annotations) {
    const before = beforeById.get(updated.id);
    if (!before) continue;
    commands.push({
      kind: "update",
      annotationId: updated.id,
      before: { geometry: before.geometry },
      after: { geometry: updated.geometry },
    });
  }

  for (const deletedId of result.deleted_annotation_ids) {
    const before = beforeById.get(deletedId);
    if (before) commands.push({ kind: "delete", annotation: before });
  }

  for (const created of result.created_annotations) {
    commands.push({
      kind: "create",
      annotationId: created.id,
      payload: {
        annotation_type: created.annotation_type,
        class_name: created.class_name,
        geometry: created.geometry,
        confidence: created.confidence ?? undefined,
        attributes: created.attributes,
      },
    });
  }

  return commands;
}

function isConflictError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409;
}

function isVideoPending(pending: PendingDrawing): pending is NonNullable<PendingDrawing> & {
  kind:
    | "video_bbox"
    | "video_rotated_bbox"
    | "video_keypoint"
    | "video_track_bbox"
    | "video_polygon"
    | "video_polyline"
    | "video_track_polygon"
    | "video_track_polyline";
} {
  return !!pending?.kind && pending.kind !== "video_mask" && pending.kind.startsWith("video_");
}

function pointsBounds(points: readonly [number, number][]): Geom {
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  const maxX = Math.max(...xs);
  const maxY = Math.max(...ys);
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function videoClassPickerAnchor(geom: Geom): { left: number; top: number } {
  const rect =
    typeof document === "undefined"
      ? null
      : document.querySelector("[data-video-overlay]")?.getBoundingClientRect();
  if (!rect || rect.width === 0 || rect.height === 0) return { left: 16, top: 16 };
  return {
    left: rect.left + (geom.x + geom.w) * rect.width,
    top: rect.top + (geom.y + geom.h) * rect.height + 6,
  };
}

export function useVideoAnnotationActions({
  taskId,
  meUserId,
  queryClient,
  history,
  s,
  annotationsRef,
  pushToast,
  recordRecentClass,
  optimisticEnqueueCreate,
  enqueueOnError,
  mutations,
  activeToolHasOwnClasses = true,
  annotationQueryKey = ["annotations", taskId],
}: UseVideoAnnotationActionsArgs) {
  const enqueueOwned = useCallback(
    (op: OfflineOp) => {
      if (!meUserId) return;
      void enqueueOffline(op, { userId: meUserId });
    },
    [meUserId],
  );
  const pendingMaskClassResolverRef = useRef<((className: string | null) => void) | null>(null);
  useEffect(
    () => () => {
      pendingMaskClassResolverRef.current?.(null);
      pendingMaskClassResolverRef.current = null;
    },
    [taskId],
  );

  const requestVideoMaskClass = useCallback(
    (rle: CocoRle, frameIndex: number): Promise<string | null> => {
      if (!activeToolHasOwnClasses) return Promise.resolve(UNKNOWN_CLASS);
      pendingMaskClassResolverRef.current?.(null);
      const geom = cocoRleBounds(rle) ?? { x: 0, y: 0, w: 1, h: 1 };
      s.setPendingDrawing({
        kind: "video_mask",
        frameIndex,
        geom,
        anchor: videoClassPickerAnchor(geom),
      });
      return new Promise((resolve) => {
        pendingMaskClassResolverRef.current = resolve;
      });
    },
    [activeToolHasOwnClasses, s],
  );

  const handleCancelVideoMaskPendingClass = useCallback((): boolean => {
    if (s.pendingDrawing?.kind !== "video_mask") return false;
    const resolve = pendingMaskClassResolverRef.current;
    pendingMaskClassResolverRef.current = null;
    s.setPendingDrawing(null);
    resolve?.(null);
    return true;
  }, [s]);

  const optimisticUpdateAnnotation = useCallback(
    (annotationId: string, patch: { geometry?: Geometry; class_name?: string }) => {
      if (!taskId) return;
      queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (prev) =>
        (prev ?? []).map((a) => (a.id === annotationId ? { ...a, ...patch } : a)),
      );
    },
    [annotationQueryKey, queryClient, taskId],
  );

  const handleVideoCreateWithClass = useCallback(
    (
      kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
      frameIndex: number,
      geo: Geom,
      cls: string,
    ) => {
      const payload = buildVideoCreatePayload(kind, frameIndex, geo, cls);
      const className = payload.class_name;
      mutations.create.mutate(payload, {
        onSuccess: (created) => {
          history.push({ kind: "create", annotationId: created.id, payload });
          if (className !== UNKNOWN_CLASS) {
            s.setActiveClass(className);
            recordRecentClass(className);
          }
          s.setSelectedId(created.id);
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
    },
    [enqueueOnError, history, mutations.create, optimisticEnqueueCreate, recordRecentClass, s],
  );

  const handleVideoKeypointCreateWithClass = useCallback(
    (frameIndex: number, points: Keypoint[], cls: string) => {
      const payload = buildVideoKeypointCreatePayload(frameIndex, points, cls);
      const className = payload.class_name;
      mutations.create.mutate(payload, {
        onSuccess: (created) => {
          history.push({ kind: "create", annotationId: created.id, payload });
          if (className !== UNKNOWN_CLASS) {
            s.setActiveClass(className);
            recordRecentClass(className);
          }
          s.setSelectedId(created.id);
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
    },
    [enqueueOnError, history, mutations.create, optimisticEnqueueCreate, recordRecentClass, s],
  );

  const handleVideoKeypointCreate = useCallback(
    (frameIndex: number, points: Keypoint[]) => {
      if (!activeToolHasOwnClasses) {
        handleVideoKeypointCreateWithClass(frameIndex, points, UNKNOWN_CLASS);
        return;
      }
      const geom = pointsBounds(points.map((point) => [point.x, point.y] as [number, number]));
      s.setPendingDrawing({
        kind: "video_keypoint",
        frameIndex,
        geom,
        anchor: videoClassPickerAnchor(geom),
        points,
      });
    },
    [activeToolHasOwnClasses, handleVideoKeypointCreateWithClass, s],
  );

  const handleVideoCreate = useCallback(
    (frameIndex: number, geo: Geom) => {
      handleVideoCreateWithClass(
        "video_track_bbox",
        frameIndex,
        geo,
        s.activeClass || UNKNOWN_CLASS,
      );
    },
    [handleVideoCreateWithClass, s.activeClass],
  );

  // v0.21.20 · 由绘制顶点新建 polygon/polyline track (单关键帧于当前帧)。
  const handleVideoPointsTrackCreateWithClass = useCallback(
    (
      type: "video_track_polygon" | "video_track_polyline",
      frameIndex: number,
      points: [number, number][],
      cls: string,
    ) => {
      const payload = buildVideoPointsTrackCreatePayload(type, frameIndex, points, cls);
      const className = payload.class_name;
      mutations.create.mutate(payload, {
        onSuccess: (created) => {
          history.push({ kind: "create", annotationId: created.id, payload });
          if (className !== UNKNOWN_CLASS) {
            s.setActiveClass(className);
            recordRecentClass(className);
          }
          s.setSelectedId(created.id);
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
    },
    [enqueueOnError, history, mutations.create, optimisticEnqueueCreate, recordRecentClass, s],
  );

  const handleVideoPointsTrackCreate = useCallback(
    (
      type: "video_track_polygon" | "video_track_polyline",
      frameIndex: number,
      points: [number, number][],
    ) => {
      if (!activeToolHasOwnClasses) {
        handleVideoPointsTrackCreateWithClass(type, frameIndex, points, UNKNOWN_CLASS);
        return;
      }
      const geom = pointsBounds(points);
      s.setPendingDrawing({
        kind: type,
        frameIndex,
        geom,
        anchor: videoClassPickerAnchor(geom),
        points,
      });
    },
    [activeToolHasOwnClasses, handleVideoPointsTrackCreateWithClass, s],
  );

  // v0.21.21 · 单帧 polygon/polyline 创建 (非 track), 与 handleVideoPointsTrackCreate 平行。
  const handleVideoPointsCreateWithClass = useCallback(
    (
      type: "video_polygon" | "video_polyline",
      frameIndex: number,
      points: [number, number][],
      cls: string,
    ) => {
      const payload = buildVideoPointsCreatePayload(type, frameIndex, points, cls);
      const className = payload.class_name;
      mutations.create.mutate(payload, {
        onSuccess: (created) => {
          history.push({ kind: "create", annotationId: created.id, payload });
          if (className !== UNKNOWN_CLASS) {
            s.setActiveClass(className);
            recordRecentClass(className);
          }
          s.setSelectedId(created.id);
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
    },
    [enqueueOnError, history, mutations.create, optimisticEnqueueCreate, recordRecentClass, s],
  );

  const handleVideoPointsCreate = useCallback(
    (type: "video_polygon" | "video_polyline", frameIndex: number, points: [number, number][]) => {
      if (!activeToolHasOwnClasses) {
        handleVideoPointsCreateWithClass(type, frameIndex, points, UNKNOWN_CLASS);
        return;
      }
      const geom = pointsBounds(points);
      s.setPendingDrawing({
        kind: type,
        frameIndex,
        geom,
        anchor: videoClassPickerAnchor(geom),
        points,
      });
    },
    [activeToolHasOwnClasses, handleVideoPointsCreateWithClass, s],
  );

  const handleVideoPendingDraw = useCallback(
    (
      kind: "video_bbox" | "video_track_bbox" | "video_rotated_bbox",
      frameIndex: number,
      geom: Geom,
      anchor: { left: number; top: number },
    ) => {
      s.setPendingDrawing({ kind, frameIndex, geom, anchor });
    },
    [s],
  );

  const handlePickVideoPendingClass = useCallback(
    (cls: string): boolean => {
      const pending = s.pendingDrawing;
      if (pending?.kind === "video_mask") {
        const resolve = pendingMaskClassResolverRef.current;
        pendingMaskClassResolverRef.current = null;
        s.setPendingDrawing(null);
        resolve?.(cls);
        return true;
      }
      if (!isVideoPending(pending)) return false;
      s.setPendingDrawing(null);
      if (
        pending.kind === "video_bbox" ||
        pending.kind === "video_track_bbox" ||
        pending.kind === "video_rotated_bbox"
      ) {
        handleVideoCreateWithClass(pending.kind, pending.frameIndex, pending.geom, cls);
      } else if (pending.kind === "video_keypoint") {
        handleVideoKeypointCreateWithClass(pending.frameIndex, pending.points, cls);
      } else {
        const points = pending.points;
        if (!points) return false;
        if (pending.kind === "video_polygon" || pending.kind === "video_polyline") {
          handleVideoPointsCreateWithClass(pending.kind, pending.frameIndex, points, cls);
        } else {
          handleVideoPointsTrackCreateWithClass(pending.kind, pending.frameIndex, points, cls);
        }
      }
      return true;
    },
    [
      handleVideoCreateWithClass,
      handleVideoKeypointCreateWithClass,
      handleVideoPointsCreateWithClass,
      handleVideoPointsTrackCreateWithClass,
      s,
    ],
  );

  const handleVideoUpdate = useCallback(
    (ann: AnnotationResponse, geometry: VideoGeometry) => {
      const after = { geometry };
      const command = buildVideoUpdateCommand(ann, geometry);
      mutations.update.mutate(
        { annotationId: ann.id, payload: after },
        {
          onSuccess: () => history.push(command),
          onError: (err) => {
            if (isConflictError(err)) return;
            enqueueOnError(err, () => {
              optimisticUpdateAnnotation(ann.id, { geometry });
              history.push(command);
              if (taskId)
                enqueueOwned({
                  kind: "update",
                  id: randomId(),
                  taskId,
                  annotationId: ann.id,
                  payload: after,
                  ts: Date.now(),
                });
            });
          },
        },
      );
    },
    [enqueueOnError, enqueueOwned, history, mutations.update, optimisticUpdateAnnotation, taskId],
  );

  const handleVideoMaskCommit = useCallback(
    async (
      rle: CocoRle,
      frameIndex: number,
      selected: AnnotationResponse | null,
      createMode: "frame" | "track" = "frame",
      isCurrent: () => boolean = () => true,
    ) => {
      if (!taskId) throw new Error("Task is not available");
      if (!isCurrent()) return null;
      if (selected?.geometry.type === "video_track_mask") {
        const selectedMaskVersion = selected.version;
        if (selectedMaskVersion == null) {
          throw new Error("Video Mask annotation version is missing");
        }
        const reference = await rasterMasksApi.uploadTaskContent(taskId, rle);
        if (!isCurrent()) return null;
        const geometry = upsertVideoMaskKeyframe(selected.geometry, frameIndex, reference);
        const command = buildVideoUpdateCommand(selected, geometry);
        const updated = await videoTrackerApi.saveMaskKeyframe(
          taskId,
          selected.id,
          frameIndex,
          reference,
          selectedMaskVersion,
        );
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (current) =>
          (current ?? []).map((item) => (item.id === updated.id ? updated : item)),
        );
        if (!isCurrent()) return null;
        history.push(command);
        return {
          annotation: updated,
          mask: reference,
          frameIndex,
        } satisfies SavedVideoMaskKeyframe;
      }
      if (selected?.geometry.type === "video_mask") {
        if (selected.version == null) {
          throw new Error("Video Mask annotation version is missing");
        }
        const reference = await rasterMasksApi.uploadTaskContent(taskId, rle);
        if (!isCurrent()) return null;
        const geometry: VideoMaskGeometry = {
          type: "video_mask",
          frame_index: frameIndex,
          mask: reference,
        };
        const command = buildVideoUpdateCommand(selected, geometry);
        const updated = await tasksApi.updateAnnotation(
          taskId,
          selected.id,
          { geometry },
          `W/"${selected.version}"`,
        );
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (current) =>
          (current ?? []).map((item) => (item.id === updated.id ? updated : item)),
        );
        if (!isCurrent()) return null;
        history.push(command);
        return {
          annotation: updated,
          mask: reference,
          frameIndex,
        } satisfies SavedVideoMaskKeyframe;
      }
      const className = await requestVideoMaskClass(rle, frameIndex);
      if (!className || !isCurrent()) return null;
      const reference = await rasterMasksApi.uploadTaskContent(taskId, rle);
      if (!isCurrent()) return null;
      const payload =
        createMode === "track"
          ? buildVideoMaskTrackCreatePayload(frameIndex, reference, className)
          : buildVideoMaskCreatePayload(frameIndex, reference, className);
      return new Promise<SavedVideoMaskKeyframe>((resolve, reject) => {
        mutations.create.mutate(payload, {
          onSuccess: (created) => {
            if (isCurrent()) history.push({ kind: "create", annotationId: created.id, payload });
            resolve({ annotation: created, mask: reference, frameIndex });
          },
          onError: reject,
        });
      });
    },
    [annotationQueryKey, history, mutations.create, queryClient, requestVideoMaskClass, taskId],
  );

  const handleVideoRename = useCallback(
    (ann: AnnotationResponse, className: string) => {
      const before = { class_name: ann.class_name };
      const after = { class_name: className };
      mutations.update.mutate(
        { annotationId: ann.id, payload: after },
        {
          onSuccess: () => history.push({ kind: "update", annotationId: ann.id, before, after }),
          onError: (err) => {
            if (isConflictError(err)) return;
            enqueueOnError(err, () => {
              optimisticUpdateAnnotation(ann.id, { class_name: className });
              history.push({ kind: "update", annotationId: ann.id, before, after });
              if (taskId)
                enqueueOwned({
                  kind: "update",
                  id: randomId(),
                  taskId,
                  annotationId: ann.id,
                  payload: after,
                  ts: Date.now(),
                });
            });
          },
        },
      );
    },
    [enqueueOnError, enqueueOwned, history, mutations.update, optimisticUpdateAnnotation, taskId],
  );

  const handleVideoBatchRename = useCallback(
    (annotations: AnnotationResponse[], className: string) => {
      const targets = annotations.filter(
        (ann) => isAnyVideoTrack(ann) && ann.class_name !== className,
      );
      if (!className || targets.length === 0) return;

      let pending = targets.length;
      let succeeded = 0;
      let failed = 0;
      const commands: Extract<Command, { kind: "update" }>[] = [];

      targets.forEach((ann) => {
        const before = { class_name: ann.class_name };
        const after = { class_name: className };
        mutations.update.mutate(
          { annotationId: ann.id, payload: after },
          {
            onSuccess: () => {
              succeeded++;
              commands.push({ kind: "update", annotationId: ann.id, before, after });
            },
            onError: () => {
              failed++;
            },
            onSettled: () => {
              pending--;
              if (pending !== 0) return;
              if (commands.length > 0) history.pushBatch(commands);
              if (succeeded > 0) {
                s.setActiveClass(className);
                recordRecentClass(className);
              }
              pushToast({
                msg: `${succeeded} 条轨迹已改为 ${className}`,
                sub: failed ? `${failed} 项失败` : undefined,
                kind: failed ? "error" : "success",
              });
            },
          },
        );
      });
    },
    [history, mutations.update, pushToast, recordRecentClass, s],
  );

  const handleVideoBatchDelete = useCallback(
    (annotations: AnnotationResponse[]) => {
      const targets = annotations.filter(isAnyVideoTrack);
      if (targets.length === 0) return;

      let pending = targets.length;
      let succeeded = 0;
      let failed = 0;
      const commands: Extract<Command, { kind: "delete" }>[] = [];

      targets.forEach((ann) => {
        mutations.delete.mutate(ann.id, {
          onSuccess: () => {
            succeeded++;
            commands.push({ kind: "delete", annotation: ann });
          },
          onError: () => {
            failed++;
          },
          onSettled: () => {
            pending--;
            if (pending !== 0) return;
            if (commands.length > 0) history.pushBatch(commands);
            pushToast({
              msg: `已删除 ${succeeded}/${targets.length} 条轨迹`,
              sub: failed ? `${failed} 项失败` : undefined,
              kind: failed ? "error" : "success",
            });
            s.setSelectedId(null);
          },
        });
      });
    },
    [history, mutations.delete, pushToast, s],
  );

  const handleVideoSetSelectedClass = useCallback(
    (className: string) => {
      if (!s.selectedId) return false;
      const ann = annotationsRef.current.find((a) => a.id === s.selectedId);
      // v0.21.26 · 放宽到全部视频几何 (单帧 bbox/polygon/polyline/rotated + bbox/polygon/polyline track):
      // 改类走 handleVideoRename (仅动 class_name, 类型无关), 快捷改类不再对点集几何静默失效。
      if (!ann || !(isAnyVideoSingleFrame(ann) || isAnyVideoTrack(ann))) return false;
      if (ann.class_name === className) return true;
      handleVideoRename(ann, className);
      recordRecentClass(className);
      return true;
    },
    [annotationsRef, handleVideoRename, recordRecentClass, s.selectedId],
  );

  const handleVideoConvertToBboxes = useCallback(
    async (ann: AnnotationResponse, options: VideoConvertOptions) => {
      if (!taskId || ann.geometry.type !== "video_track_bbox") return;
      if (options.frameMode === "all_frames") {
        const confirmed = await confirmDialog({
          title: "按插值结果展开所有可见帧？",
          description: "长视频可能生成大量独立框。",
          confirmLabel: "继续",
        });
        if (!confirmed) return;
        // 等待决定期间轨迹可能已被删除;目标不存在则放弃本次转换。
        if (!annotationsRef.current.some((item) => item.id === ann.id)) return;
      }
      try {
        const result = await tasksApi.convertVideoTrackToBboxes(taskId, ann.id, {
          operation: options.operation,
          scope: options.scope,
          frame_index: options.frameIndex,
          frame_mode: options.frameMode ?? "keyframes",
        });
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (prev) => {
          const base = (prev ?? []).filter(
            (item) => !result.created_annotations.some((created) => created.id === item.id),
          );
          const withoutSource = result.deleted_source
            ? base.filter((item) => item.id !== ann.id)
            : base;
          const updatedSource = result.source_annotation
            ? withoutSource.map((item) => (item.id === ann.id ? result.source_annotation! : item))
            : withoutSource;
          return [...updatedSource, ...result.created_annotations];
        });
        const commands: Exclude<Command, { kind: "batch" | "slice" }>[] =
          result.created_annotations.map((created) => ({
            kind: "create",
            annotationId: created.id,
            payload: {
              annotation_type: created.annotation_type,
              class_name: created.class_name,
              geometry: created.geometry,
              confidence: created.confidence ?? undefined,
              attributes: created.attributes,
            },
          }));
        if (result.deleted_source) {
          commands.push({ kind: "delete", annotation: ann });
          s.setSelectedId(null);
        } else if (
          result.source_annotation &&
          result.source_annotation.geometry.type === "video_track_bbox"
        ) {
          commands.push({
            kind: "update",
            annotationId: ann.id,
            before: { geometry: ann.geometry },
            after: { geometry: result.source_annotation.geometry },
          });
          s.setSelectedId(result.source_annotation.id);
        }
        history.pushBatch(commands);
        pushToast({ msg: `已生成 ${result.created_annotations.length} 个独立框`, kind: "success" });
      } catch (err) {
        pushToast({ msg: "轨迹转换失败", sub: String(err), kind: "error" });
      }
    },
    [annotationQueryKey, annotationsRef, history, pushToast, queryClient, s, taskId],
  );

  const handleVideoComposeTracks = useCallback(
    async (options: VideoTrackCompositionOptions) => {
      if (!taskId || options.annotationIds.length === 0) return;
      const before = annotationsRef.current.filter((ann) => options.annotationIds.includes(ann.id));
      try {
        const result = await tasksApi.composeVideoTracks(taskId, {
          operation: options.operation,
          annotation_ids: options.annotationIds,
          frame_index: options.frameIndex,
          delete_sources: options.deleteSources,
          gap_mode: options.gapMode,
        });
        queryClient.setQueryData<AnnotationResponse[]>(annotationQueryKey, (prev) => {
          const deleted = new Set(result.deleted_annotation_ids);
          const updatedById = new Map(result.updated_annotations.map((ann) => [ann.id, ann]));
          const createdIds = new Set(result.created_annotations.map((ann) => ann.id));
          const kept = (prev ?? [])
            .filter((ann) => !deleted.has(ann.id) && !createdIds.has(ann.id))
            .map((ann) => updatedById.get(ann.id) ?? ann);
          const present = new Set(kept.map((ann) => ann.id));
          return [
            ...kept,
            ...result.updated_annotations.filter((ann) => !present.has(ann.id)),
            ...result.created_annotations,
          ];
        });
        history.pushBatch(buildVideoCompositionCommands(before, result));
        const nextSelected =
          result.created_annotations[0]?.id ?? result.updated_annotations[0]?.id ?? null;
        s.setSelectedId(nextSelected);
        const label =
          options.operation === "aggregate_bboxes"
            ? "已聚合为轨迹"
            : options.operation === "split_track"
              ? "轨迹已拆分"
              : options.operation === "join_tracks"
                ? "轨迹已跳连"
                : "轨迹已合并";
        pushToast({ msg: label, kind: "success" });
      } catch (err) {
        pushToast({ msg: "轨迹组合失败", sub: String(err), kind: "error" });
      }
    },
    [annotationQueryKey, annotationsRef, history, pushToast, queryClient, s, taskId],
  );

  // v0.10.30 · 2.3 track 级属性默认值: 写入 annotation.attributes (顶层)。复用既有
  // update mutation + offline 回退路径 (同 handleVideoRename 风格), undo 用 update 命令。
  const handleUpdateTrackAttributes = useCallback(
    (ann: AnnotationResponse, attributes: Record<string, unknown>) => {
      const before = { attributes: ann.attributes };
      const after = { attributes };
      mutations.update.mutate(
        { annotationId: ann.id, payload: after },
        {
          onSuccess: () => history.push({ kind: "update", annotationId: ann.id, before, after }),
          onError: (err) => {
            if (isConflictError(err)) return;
            enqueueOnError(err, () => {
              queryClient.setQueryData<AnnotationResponse[]>(
                ["annotations", taskId ?? ""],
                (prev) => (prev ?? []).map((a) => (a.id === ann.id ? { ...a, attributes } : a)),
              );
              history.push({ kind: "update", annotationId: ann.id, before, after });
              if (taskId)
                enqueueOwned({
                  kind: "update",
                  id: randomId(),
                  taskId,
                  annotationId: ann.id,
                  payload: after,
                  ts: Date.now(),
                });
            });
          },
        },
      );
    },
    [enqueueOnError, enqueueOwned, history, mutations.update, queryClient, taskId],
  );

  // v0.10.30 · 2.3 当前帧逐帧覆盖: 写入该帧 keyframe.attributes。复用 upsertKeyframe (保留当前帧框)
  // + handleVideoUpdate (geometry 单条 keyframe 变化 → videoKeyframe undo 命令)。
  const handleUpdateKeyframeAttributes = useCallback(
    (ann: AnnotationResponse, frameIndex: number, attributes: Record<string, unknown>) => {
      if (ann.geometry.type !== "video_track_bbox") return;
      const track = ann.geometry;
      const bbox = nearestTrackBbox(track, frameIndex);
      const patch: Partial<VideoTrackKeyframeWithAttrs> = { attributes };
      const next = upsertKeyframe(track, frameIndex, bbox, patch);
      handleVideoUpdate(ann, next);
    },
    [handleVideoUpdate],
  );

  // v0.10.30 · 2.6 Propagate: 把 fromFrame 处框复制到后续/向前 N 帧, 合成单条 undo 命令。
  const handlePropagateKeyframe = useCallback(
    (
      ann: AnnotationResponse,
      fromFrame: number,
      count: number,
      options: { direction: VideoPropagateOptions["direction"]; overwrite: boolean },
    ) => {
      if (ann.geometry.type !== "video_track_bbox") return;
      const track = ann.geometry;
      const fromBbox = nearestTrackBbox(track, fromFrame);
      const next = propagateKeyframes(track, fromFrame, fromBbox, {
        direction: options.direction,
        count,
        overwrite: options.overwrite,
      });
      if (!next) {
        pushToast({ msg: "无可铺设的帧", kind: "warning" });
        return;
      }
      handleVideoUpdate(ann, next);
      const added = next.keyframes.length - track.keyframes.length;
      pushToast({
        msg: `已传播到 ${count} 帧`,
        sub: added > 0 ? `新增 ${added} 个关键帧` : undefined,
        kind: "success",
      });
    },
    [handleVideoUpdate, pushToast],
  );

  return {
    handleVideoCreate,
    handleVideoCreateWithClass,
    handleVideoPointsTrackCreate,
    handleVideoPointsCreate,
    handleVideoPointsCreateWithClass,
    handleVideoKeypointCreate,
    handleVideoPendingDraw,
    handlePickVideoPendingClass,
    handleVideoUpdate,
    handleVideoMaskCommit,
    handleCancelVideoMaskPendingClass,
    handleVideoRename,
    handleVideoBatchRename,
    handleVideoBatchDelete,
    handleVideoSetSelectedClass,
    handleVideoConvertToBboxes,
    handleVideoComposeTracks,
    handleUpdateTrackAttributes,
    handleUpdateKeyframeAttributes,
    handlePropagateKeyframe,
  };
}
