import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { QueryClient } from "@tanstack/react-query";
import type { ToolBindings } from "@/api/projects";

import { isSelfIntersecting, type Pt } from "../stage/polygonGeom";
import { UNKNOWN_CLASS } from "../stage/colors";
import type { KeypointDraftHandle, PolygonDraftHandle } from "../stage/tools";
import { toolUnitForTool } from "../stage/tools/toolUnits";
import { isComplexPolygonGeometry } from "../stage/shared/geometry/geometryEditPolicy";
import { bboxGeom, keypointGeom, polygonGeom, polylineGeom } from "../state/transforms";
import type { Geometry, Keypoint } from "@/types";
import { randomId } from "@/utils/id";
import { enqueueDurably, isOfflineCandidate, type OfflineOp } from "../state/offlineQueue";
import { attributeSchemaForUnit, classesForUnit } from "./useToolBindings";
import { getMissingRequired } from "../shell/AttributeForm";
import {
  creationAttributeDefaults,
  manualDrawingPayload,
  manualImageTool,
  type ManualCreationDraft,
} from "./manualImageCreation";
import type { PendingDrawing } from "./useWorkbenchState";
import { usePolygonDraftPoints } from "./usePolygonDraftPoints";
import { POLYGON_AUTO_POINT_LIMIT } from "../stage/polygonAutoPoints";
import type { useWorkbenchState } from "../state/useWorkbenchState";
import type { useAnnotationHistory } from "../state/useAnnotationHistory";
import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import { tasksApi } from "@/api/tasks";
import type { AnnotationResponse, RotatedBboxGeometry } from "@/types";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

type Geom = { x: number; y: number; w: number; h: number };

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

export interface AnnotationMutations {
  create: {
    mutate: (
      p: AnnotationPayload,
      opts?: {
        onSuccess?: (a: AnnotationResponse) => void;
        onError?: (e: unknown) => void;
        onSettled?: () => void;
      },
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

export interface UseWorkbenchAnnotationActionsArgs {
  taskId: string | undefined;
  videoSegmentId?: string | null;
  projectId: string | undefined;
  meUserId: string | null | undefined;
  queryClient: QueryClient;
  history: ReturnType<typeof useAnnotationHistory>;
  s: ReturnType<typeof useWorkbenchState>;
  pushToast: (toast: ToastInput) => void;
  recordRecentClass: (cls: string) => void;
  mutations: AnnotationMutations;
  enqueueOnError: (err: unknown, fallback: () => void) => void;
  annotationsRef: { current: AnnotationResponse[] };
  isLocked?: boolean;
  keypointNodeCount?: number;
  activeToolHasOwnClasses?: boolean;
  toolBindings?: ToolBindings;
  createAnnotationAsync?: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
  markPendingGeom?: (id: string, geom: Geometry) => void;
}

export interface UseWorkbenchAnnotationActionsReturn {
  optimisticEnqueueCreate: (payload: AnnotationPayload) => Promise<void>;
  createBboxWithClass: (geom: Geom, cls: string) => boolean;
  beginBboxDrawing: (geom: Geom, cls?: string) => boolean;
  submitManualDrawing: () => void;
  changeManualAttributes: (id: string, next: Record<string, unknown>) => void;
  cancelManualDrawing: () => boolean;
  hasManualDraft: boolean;
  createRotatedBbox: (geom: Geom) => boolean;
  handleCommitRotateBbox: (
    id: string,
    before: RotatedBboxGeometry,
    after: RotatedBboxGeometry,
  ) => void;
  handlePickPendingClass: (cls: string) => void;
  submitPolygon: (points: [number, number][]) => void;
  submitPolyline: (points: [number, number][]) => void;
  handleDeleteBox: (id: string) => void;
  handleCommitMove: (
    id: string,
    before: Geom,
    after: Geom,
    childMoves?: { id: string; before: Geometry; after: Geometry }[],
  ) => void;
  handleCommitResize: (id: string, before: Geom, after: Geom) => void;
  handleCommitPolygonGeometry: (id: string, before: Pt[], after: Pt[]) => void;
  handleCommitKeypointGeometry: (id: string, before: Keypoint[], after: Keypoint[]) => void;
  handlePatchShapeFlag: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  polygonDraftPoints: [number, number][];
  setPolygonDraftPoints: React.Dispatch<React.SetStateAction<[number, number][]>>;
  polygonHandle: PolygonDraftHandle;
  polylineHandle: PolygonDraftHandle;
  keypointHandle: KeypointDraftHandle;
}

export function useWorkbenchAnnotationActions({
  taskId,
  videoSegmentId,
  projectId,
  meUserId,
  queryClient,
  history,
  s,
  pushToast,
  recordRecentClass,
  mutations,
  enqueueOnError,
  annotationsRef,
  isLocked = false,
  keypointNodeCount = 0,
  activeToolHasOwnClasses = true,
  toolBindings,
  createAnnotationAsync,
  markPendingGeom,
}: UseWorkbenchAnnotationActionsArgs): UseWorkbenchAnnotationActionsReturn {
  const setQ = queryClient.setQueryData.bind(queryClient);
  const sessionId = useAuthStore((state) => state.sessionId);
  const enqueueOwnedDurably = useCallback(
    (op: OfflineOp) => {
      if (!meUserId) return Promise.reject(new Error("当前账号已退出，无法接收离线操作"));
      return enqueueDurably(op, { userId: meUserId, projectId });
    },
    [meUserId, projectId],
  );
  const owner = useMemo(
    () => ({ taskId, projectId, meUserId, isLocked, sessionId, videoSegmentId }),
    [taskId, projectId, meUserId, isLocked, sessionId, videoSegmentId],
  );
  const currentOwner = useRef(owner);
  currentOwner.current = owner;
  useEffect(() => {
    currentOwner.current = owner;
    return () => {
      if (currentOwner.current === owner) currentOwner.current = { ...owner };
    };
  }, [owner]);
  const manualDraftRef = useRef<PendingDrawing>(null);
  const draftOwnerRef = useRef(owner);
  if (draftOwnerRef.current !== owner) {
    draftOwnerRef.current = owner;
    manualDraftRef.current = null;
  }
  const publishManualDraft = useCallback(
    (drawing: PendingDrawing) => {
      manualDraftRef.current = drawing;
      s.setPendingDrawing(drawing);
    },
    [s],
  );
  useEffect(() => {
    if (s.pendingDrawing?.creation && s.pendingDrawing.creation.taskId !== taskId)
      s.setPendingDrawing(null);
  }, [s, taskId]);

  const blockIfLocked = useCallback((): boolean => {
    if (isLocked) {
      pushToast({ msg: "任务已锁定", sub: "撤回提交或继续编辑后再操作", kind: "warning" });
      return true;
    }
    return false;
  }, [isLocked, pushToast]);

  const optimisticUpdateGeom = useCallback(
    (id: string, afterG: Record<string, unknown>) => {
      if (!taskId || !meUserId || !isCurrentAuthOwner(meUserId) || currentOwner.current !== owner)
        return;
      setQ<AnnotationResponse[]>(["annotations", taskId], (prev) =>
        (prev ?? []).map((a) =>
          a.id === id ? { ...a, geometry: afterG as AnnotationResponse["geometry"] } : a,
        ),
      );
    },
    [meUserId, taskId, setQ, owner],
  );

  const optimisticDelete = useCallback(
    (id: string) => {
      if (
        !taskId ||
        !owner.meUserId ||
        !isCurrentAuthOwner(owner.meUserId) ||
        currentOwner.current !== owner
      )
        return;
      setQ<AnnotationResponse[]>(["annotations", taskId], (prev) =>
        (prev ?? []).filter((a) => a.id !== id),
      );
    },
    [taskId, setQ, owner],
  );

  const optimisticEnqueueCreate = useCallback(
    async (payload: AnnotationPayload) => {
      if (!taskId || !meUserId) throw new Error("当前账号无法接收离线操作");
      const tmpId = `tmp_${randomId()}`;
      const scopedPayload = videoSegmentId
        ? { ...payload, video_segment_id: videoSegmentId }
        : payload;
      const optimistic: AnnotationResponse = {
        id: tmpId,
        task_id: taskId,
        project_id: projectId ?? null,
        user_id: meUserId ?? null,
        video_segment_id: videoSegmentId ?? null,
        source: "manual",
        annotation_type: payload.annotation_type ?? "bbox",
        class_name: payload.class_name,
        geometry: payload.geometry,
        confidence: payload.confidence ?? 1,
        parent_prediction_id: null,
        parent_annotation_id: null,
        lead_time: null,
        is_active: true,
        ground_truth: false,
        attributes: payload.attributes ?? {},
        created_at: new Date().toISOString(),
        updated_at: null,
        render_key: tmpId,
      };
      await enqueueOwnedDurably({
        kind: "create",
        id: payload.client_request_id ?? randomId(),
        tmpId,
        taskId,
        videoSegmentId,
        payload: scopedPayload,
        ts: Date.now(),
      });
      history.push({ kind: "create", annotationId: tmpId, payload: scopedPayload });
      if (
        currentOwner.current !== owner ||
        !isCurrentAuthOwner(meUserId) ||
        useAuthStore.getState().sessionId !== sessionId
      )
        return;
      const key = videoSegmentId
        ? ["annotations", taskId, videoSegmentId]
        : ["annotations", taskId];
      setQ<AnnotationResponse[]>(key, (prev) => [...(prev ?? []), optimistic]);
      s.setSelectedId(tmpId);
    },
    [
      taskId,
      projectId,
      meUserId,
      setQ,
      s,
      history,
      owner,
      enqueueOwnedDurably,
      sessionId,
      videoSegmentId,
    ],
  );

  const submitManual = useCallback(
    (drawing: NonNullable<PendingDrawing>) => {
      const draft = drawing.creation;
      if (!draft || !taskId || draft.taskId !== taskId || blockIfLocked()) return;
      if (manualDraftRef.current?.creation?.phase === "saving") return;
      if (!draft.className) return;
      if (toolBindings && !toolBindings[draft.toolUnitId]?.enabled) {
        publishManualDraft({
          ...drawing,
          creation: { ...draft, phase: "error", error: "当前工具已停用，请取消草稿后选择可用工具" },
        });
        return;
      }
      const ownClasses = classesForUnit(toolBindings, draft.toolUnitId);
      if (
        toolBindings &&
        draft.className !== UNKNOWN_CLASS &&
        !ownClasses.includes(draft.className)
      ) {
        publishManualDraft({
          ...drawing,
          creation: {
            ...draft,
            phase: "error",
            error: "类别已不属于当前工具，请取消草稿后重新选类",
          },
        });
        return;
      }
      const schema = attributeSchemaForUnit(toolBindings, draft.toolUnitId);
      const missing = getMissingRequired(schema, draft.className, draft.attributes);
      if (missing.length) {
        publishManualDraft({
          ...drawing,
          creation: {
            ...draft,
            phase: "attributes",
            requiredKeys: [...new Set([...draft.requiredKeys, ...missing])],
            error: undefined,
          },
        });
        return;
      }
      const payload = { ...manualDrawingPayload(drawing, draft), client_request_id: draft.id };
      publishManualDraft({ ...drawing, creation: { ...draft, phase: "saving", error: undefined } });
      const owns = () =>
        currentOwner.current === owner &&
        manualDraftRef.current?.creation?.id === draft.id &&
        !!meUserId &&
        isCurrentAuthOwner(meUserId);
      const accepted = (id: string) => {
        if (!owns()) return;
        history.push({ kind: "create", annotationId: id, payload });
        s.setSelectedId(id);
        if (draft.className !== UNKNOWN_CLASS) {
          s.setActiveClass(draft.className);
          recordRecentClass(draft.className);
        }
        publishManualDraft(null);
      };
      const failed = async (err: unknown) => {
        if (isOfflineCandidate(err)) {
          const tmpId = `tmp_${randomId()}`;
          try {
            if (!meUserId) throw new Error("当前账号已退出，无法接收离线操作");
            await enqueueDurably(
              {
                kind: "create",
                id: payload.client_request_id,
                tmpId,
                taskId,
                payload,
                ts: Date.now(),
              },
              { userId: meUserId, projectId },
            );
            if (!owns()) return;
            const optimistic: AnnotationResponse = {
              id: tmpId,
              task_id: taskId,
              project_id: projectId ?? null,
              user_id: meUserId ?? null,
              source: "manual",
              annotation_type: payload.annotation_type ?? "bbox",
              tool_unit_id: payload.tool_unit_id,
              class_name: payload.class_name,
              geometry: payload.geometry,
              confidence: 1,
              parent_prediction_id: null,
              parent_annotation_id: null,
              lead_time: null,
              is_active: true,
              ground_truth: false,
              attributes: payload.attributes ?? {},
              created_at: new Date().toISOString(),
              updated_at: null,
              render_key: tmpId,
            };
            queryClient.setQueryData<AnnotationResponse[]>(["annotations", taskId], (prev) => [
              ...(prev ?? []),
              optimistic,
            ]);
            accepted(tmpId);
            if (currentOwner.current === owner)
              pushToast({ msg: "已保存到离线队列", sub: "联网后自动同步", kind: "warning" });
            return;
          } catch {
            if (owns())
              publishManualDraft({
                ...drawing,
                creation: {
                  ...draft,
                  phase: "error",
                  error: "无法保存到离线队列，草稿已保留。请恢复存储或网络后重试。",
                },
              });
            return;
          }
        }
        if (owns())
          publishManualDraft({
            ...drawing,
            creation: { ...draft, phase: "error", error: "保存失败，草稿已保留。请重试。" },
          });
      };
      if (createAnnotationAsync)
        void createAnnotationAsync(payload).then((created) => accepted(created.id), failed);
      else
        mutations.create.mutate(payload, {
          onSuccess: (created) => accepted(created.id),
          onError: (error) => {
            void failed(error);
          },
        });
    },
    [
      taskId,
      projectId,
      meUserId,
      blockIfLocked,
      toolBindings,
      publishManualDraft,
      owner,
      history,
      s,
      recordRecentClass,
      queryClient,
      pushToast,
      createAnnotationAsync,
      mutations.create,
    ],
  );

  const beginManualDrawing = useCallback(
    (drawing: NonNullable<PendingDrawing>, cls?: string): boolean => {
      if (blockIfLocked() || !taskId || manualDraftRef.current) return false;
      const unit =
        drawing.kind === "polygon"
          ? "region"
          : drawing.kind === "polyline"
            ? "polyline"
            : drawing.kind === "keypoint"
              ? "keypoint"
              : drawing.kind === "rotated_bbox"
                ? "rotated_bbox"
                : "bbox";
      const intent = s.continuousCreation;
      if (intent && (intent.toolUnitId !== unit || !intent.className)) {
        pushToast({ msg: "请先为当前工具选择连续创建类别", kind: "warning" });
        return false;
      }
      const className =
        intent?.className ?? (!activeToolHasOwnClasses ? UNKNOWN_CLASS : (cls ?? ""));
      const draft: ManualCreationDraft = {
        id: randomId(),
        taskId,
        toolUnitId: unit,
        className,
        attributes: creationAttributeDefaults(
          attributeSchemaForUnit(toolBindings, unit),
          className,
        ),
        requiredKeys: [],
        phase: className ? "attributes" : "class",
      };
      const pending = { ...drawing, creation: draft };
      publishManualDraft(pending);
      if (className) submitManual(pending);
      return true;
    },
    [
      blockIfLocked,
      taskId,
      s.continuousCreation,
      activeToolHasOwnClasses,
      toolBindings,
      publishManualDraft,
      submitManual,
      pushToast,
    ],
  );

  const beginBboxDrawing = useCallback(
    (geom: Geom, cls?: string) => beginManualDrawing({ kind: "bbox", geom }, cls),
    [beginManualDrawing],
  );
  const submitManualDrawing = useCallback(() => {
    const pending = manualDraftRef.current;
    if (pending) submitManual(pending);
  }, [submitManual]);
  const changeManualAttributes = useCallback(
    (id: string, next: Record<string, unknown>) => {
      const pending = manualDraftRef.current;
      const draft = pending?.creation;
      if (!pending || !draft || draft.id !== id || draft.phase === "saving") return;
      const missing = getMissingRequired(
        attributeSchemaForUnit(toolBindings, draft.toolUnitId),
        draft.className,
        next,
      );
      publishManualDraft({
        ...pending,
        creation: {
          ...draft,
          attributes: next,
          requiredKeys: [...new Set([...draft.requiredKeys, ...missing])],
        },
      });
    },
    [publishManualDraft, toolBindings],
  );

  const createGeometryWithClass = useCallback(
    (
      annotationType: AnnotationPayload["annotation_type"],
      toolUnitId: AnnotationPayload["tool_unit_id"],
      geometry: Geometry,
      cls: string,
      success: { msg: string; sub?: string },
    ): boolean => {
      if (blockIfLocked() || !cls) return false;
      const payload: AnnotationPayload = {
        client_request_id: randomId(),
        annotation_type: annotationType,
        tool_unit_id: toolUnitId,
        class_name: cls,
        geometry,
        confidence: 1,
      };
      if (cls !== UNKNOWN_CLASS) {
        s.setActiveClass(cls);
        recordRecentClass(cls);
      }
      mutations.create.mutate(payload, {
        onSuccess: (created) => {
          if (
            currentOwner.current !== owner ||
            !owner.meUserId ||
            !isCurrentAuthOwner(owner.meUserId)
          )
            return;
          history.push({ kind: "create", annotationId: created.id, payload });
          s.setSelectedId(created.id);
          pushToast({
            msg: success.msg,
            sub: success.sub ? `${success.sub} · ${cls}` : cls,
            kind: "success",
          });
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
      return true;
    },
    [
      blockIfLocked,
      enqueueOnError,
      history,
      mutations.create,
      optimisticEnqueueCreate,
      pushToast,
      recordRecentClass,
      s,
      owner,
    ],
  );

  const pointsBounds = useCallback((points: readonly [number, number][]): Geom => {
    const xs = points.map(([x]) => x);
    const ys = points.map(([, y]) => y);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const maxX = Math.max(...xs);
    const maxY = Math.max(...ys);
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }, []);

  const createPolygonWithClass = useCallback(
    (points: [number, number][], cls: string) =>
      createGeometryWithClass("polygon", "region", polygonGeom(points), cls, {
        msg: "已创建多边形",
        sub: `${points.length} 顶点`,
      }),
    [createGeometryWithClass],
  );

  const createPolylineWithClass = useCallback(
    (points: [number, number][], cls: string) =>
      createGeometryWithClass("polyline", "polyline", polylineGeom(points), cls, {
        msg: "已创建折线",
        sub: `${points.length} 顶点`,
      }),
    [createGeometryWithClass],
  );

  const createKeypointWithClass = useCallback(
    (points: Keypoint[], cls: string) => {
      const visible = points.filter((point) => point.v > 0).length;
      return createGeometryWithClass("keypoint", "keypoint", keypointGeom(points), cls, {
        msg: "已创建关键点",
        sub: `${visible}/${points.length} 可见`,
      });
    },
    [createGeometryWithClass],
  );

  const {
    points: polygonDraftPoints,
    setPoints: setPolygonDraftPoints,
    getPoints: getPolygonDraftPoints,
  } = usePolygonDraftPoints();
  const polygonBeforeKey = useRef<(() => void) | null>(null);
  const polygonBeforeInput = useRef<((event: KeyboardEvent) => boolean) | null>(null);
  const currentTool = useRef(s.tool);
  currentTool.current = s.tool;
  useEffect(() => {
    setPolygonDraftPoints([]);
  }, [s.tool, setPolygonDraftPoints]);
  useEffect(() => {
    setPolygonDraftPoints([]);
  }, [taskId, setPolygonDraftPoints]);

  const submitPolygon = useCallback(
    (points: [number, number][]) => {
      if (blockIfLocked()) return;
      if (points.length < 3) {
        pushToast({ msg: "多边形需至少 3 个顶点", kind: "warning" });
        return;
      }
      if (manualImageTool(s.tool)) {
        if (beginManualDrawing({ kind: "polygon", geom: pointsBounds(points), points }))
          setPolygonDraftPoints([]);
        return;
      }
      setPolygonDraftPoints([]);
      if (!activeToolHasOwnClasses) {
        createPolygonWithClass(points, UNKNOWN_CLASS);
        return;
      }
      s.setPendingDrawing({ kind: "polygon", geom: pointsBounds(points), points });
    },
    [
      activeToolHasOwnClasses,
      blockIfLocked,
      beginManualDrawing,
      createPolygonWithClass,
      pointsBounds,
      pushToast,
      s,
      setPolygonDraftPoints,
    ],
  );

  const polygonHandle = useMemo<PolygonDraftHandle>(
    () => ({
      points: polygonDraftPoints,
      addPoint: (pt) => {
        if (!manualDraftRef.current && !isLocked) setPolygonDraftPoints((p) => [...p, pt]);
      },
      close: () => submitPolygon(getPolygonDraftPoints()),
      cancel: () => setPolygonDraftPoints([]),
      closed: true,
      beforeInput: polygonBeforeInput,
      boundaryTrace: {
        getPoints: getPolygonDraftPoints,
        append: (batch, expected) => {
          if (
            currentOwner.current !== owner ||
            currentTool.current !== "polygon" ||
            manualDraftRef.current ||
            isLocked ||
            getPolygonDraftPoints() !== expected
          )
            return false;
          setPolygonDraftPoints([...expected, ...batch]);
          return true;
        },
        readSource: async (id, signal) => {
          if (!taskId || currentOwner.current !== owner || isLocked) return null;
          const annotations = await tasksApi.getAnnotations(taskId, null, { signal });
          if (signal.aborted || currentOwner.current !== owner) return null;
          return (
            annotations.find((annotation) => annotation.id === id && annotation.is_active) ?? null
          );
        },
      },
      autoPoints: {
        getPoints: getPolygonDraftPoints,
        beforeKey: polygonBeforeKey,
        append: (batch, expected) => {
          if (
            manualDraftRef.current ||
            isLocked ||
            getPolygonDraftPoints() !== expected ||
            expected.length + batch.length > POLYGON_AUTO_POINT_LIMIT
          )
            return false;
          setPolygonDraftPoints([...expected, ...batch]);
          return true;
        },
      },
    }),
    [
      polygonDraftPoints,
      submitPolygon,
      isLocked,
      getPolygonDraftPoints,
      setPolygonDraftPoints,
      owner,
      taskId,
    ],
  );

  const submitPolyline = useCallback(
    (points: [number, number][]) => {
      if (blockIfLocked()) return;
      if (points.length < 2) {
        pushToast({ msg: "折线需至少 2 个顶点", kind: "warning" });
        return;
      }
      if (manualImageTool(s.tool)) {
        if (beginManualDrawing({ kind: "polyline", geom: pointsBounds(points), points }))
          setPolygonDraftPoints([]);
        return;
      }
      setPolygonDraftPoints([]);
      if (!activeToolHasOwnClasses) {
        createPolylineWithClass(points, UNKNOWN_CLASS);
        return;
      }
      s.setPendingDrawing({ kind: "polyline", geom: pointsBounds(points), points });
    },
    [
      activeToolHasOwnClasses,
      blockIfLocked,
      beginManualDrawing,
      createPolylineWithClass,
      pointsBounds,
      pushToast,
      s,
      setPolygonDraftPoints,
    ],
  );

  const polylineHandle = useMemo<PolygonDraftHandle>(
    () => ({
      points: polygonDraftPoints,
      addPoint: (pt) => {
        if (!manualDraftRef.current && !isLocked) setPolygonDraftPoints((p) => [...p, pt]);
      },
      close: () => submitPolyline(polygonDraftPoints),
      cancel: () => setPolygonDraftPoints([]),
      closed: false,
    }),
    [polygonDraftPoints, submitPolyline, isLocked, setPolygonDraftPoints],
  );

  const [keypointDraftPoints, setKeypointDraftPoints] = useState<Keypoint[]>([]);
  useEffect(() => {
    if (!isLocked) return;
    setPolygonDraftPoints([]);
    setKeypointDraftPoints([]);
  }, [isLocked, setPolygonDraftPoints]);
  useEffect(() => {
    if (s.tool !== "keypoint") setKeypointDraftPoints([]);
  }, [s.tool]);
  useEffect(() => {
    setKeypointDraftPoints([]);
  }, [taskId]);
  useEffect(() => {
    setKeypointDraftPoints([]);
  }, [keypointNodeCount]);

  const submitKeypoint = useCallback(
    (points: Keypoint[]) => {
      if (blockIfLocked()) return;
      if (points.length === 0) return;
      if (manualImageTool(s.tool)) {
        if (
          beginManualDrawing({
            kind: "keypoint",
            geom: pointsBounds(points.map((point) => [point.x, point.y])),
            points,
          })
        )
          setKeypointDraftPoints([]);
        return;
      }
      setKeypointDraftPoints([]);
      if (!activeToolHasOwnClasses) {
        createKeypointWithClass(points, UNKNOWN_CLASS);
        return;
      }
      s.setPendingDrawing({
        kind: "keypoint",
        geom: pointsBounds(points.map((point) => [point.x, point.y] as [number, number])),
        points,
      });
    },
    [
      activeToolHasOwnClasses,
      blockIfLocked,
      beginManualDrawing,
      createKeypointWithClass,
      pointsBounds,
      s,
    ],
  );

  useEffect(() => {
    if (keypointNodeCount > 0 && keypointDraftPoints.length >= keypointNodeCount) {
      submitKeypoint(keypointDraftPoints.slice(0, keypointNodeCount));
    }
  }, [keypointDraftPoints, keypointNodeCount, submitKeypoint]);

  const keypointHandle = useMemo<KeypointDraftHandle>(
    () => ({
      points: keypointDraftPoints,
      nodeCount: keypointNodeCount,
      addPoint: (kp) => {
        if (!manualDraftRef.current && !isLocked) setKeypointDraftPoints((p) => [...p, kp]);
      },
      cancel: () => setKeypointDraftPoints([]),
    }),
    [keypointDraftPoints, keypointNodeCount, isLocked],
  );

  const cancelManualDrawing = useCallback((): boolean => {
    if (manualDraftRef.current?.creation?.phase === "saving") {
      pushToast({ msg: "标注正在保存", sub: "保存完成后再继续", kind: "warning" });
      return true;
    }
    if (manualDraftRef.current) {
      publishManualDraft(null);
      return true;
    }
    if (polygonDraftPoints.length) {
      setPolygonDraftPoints([]);
      return true;
    }
    if (keypointDraftPoints.length) {
      setKeypointDraftPoints([]);
      return true;
    }
    return false;
  }, [
    pushToast,
    publishManualDraft,
    polygonDraftPoints.length,
    keypointDraftPoints.length,
    setPolygonDraftPoints,
  ]);

  const handleCommitKeypointGeometry = useCallback(
    (id: string, before: Keypoint[], after: Keypoint[]) => {
      if (blockIfLocked()) return;
      if (!taskId) return;
      const beforeG = keypointGeom(before);
      const afterG = keypointGeom(after);
      const payload = { geometry: afterG };
      markPendingGeom?.(id, afterG);
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { geometry: beforeG },
              after: { geometry: afterG },
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticUpdateGeom(id, afterG);
              history.push({
                kind: "update",
                annotationId: id,
                before: { geometry: beforeG },
                after: { geometry: afterG },
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      taskId,
      enqueueOnError,
      optimisticUpdateGeom,
      markPendingGeom,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const createBboxWithClass = useCallback(
    (geom: Geom, cls: string): boolean => {
      if (s.tool === "box") return beginBboxDrawing(geom, cls);
      if (blockIfLocked()) return false;
      if (!cls) return false;
      const isUnknown = cls === UNKNOWN_CLASS;
      const payload: AnnotationPayload = {
        client_request_id: randomId(),
        annotation_type: "bbox",
        tool_unit_id: toolUnitForTool(s.tool),
        class_name: cls,
        geometry: bboxGeom(geom),
        confidence: 1,
      };
      if (!isUnknown) {
        s.setActiveClass(cls);
        recordRecentClass(cls);
      }
      mutations.create.mutate(payload, {
        onSuccess: (newAnnotation) => {
          if (
            currentOwner.current !== owner ||
            !owner.meUserId ||
            !isCurrentAuthOwner(owner.meUserId)
          )
            return;
          s.setSelectedId(newAnnotation.id);
          history.push({ kind: "create", annotationId: newAnnotation.id, payload });
        },
        onError: (err) => enqueueOnError(err, () => optimisticEnqueueCreate(payload)),
      });
      return true;
    },
    [
      blockIfLocked,
      beginBboxDrawing,
      s,
      mutations,
      history,
      recordRecentClass,
      enqueueOnError,
      optimisticEnqueueCreate,
      owner,
    ],
  );

  const createRotatedBboxWithClass = useCallback(
    (geom: Geom, cls: string): boolean => {
      const geometry: RotatedBboxGeometry = {
        type: "rotated_bbox",
        cx: geom.x + geom.w / 2,
        cy: geom.y + geom.h / 2,
        w: geom.w,
        h: geom.h,
        angle: 0,
      };
      return createGeometryWithClass("rotated_bbox", "rotated_bbox", geometry, cls, {
        msg: "已创建旋转框",
      });
    },
    [createGeometryWithClass],
  );

  const createRotatedBbox = useCallback(
    (geom: Geom): boolean => {
      return beginManualDrawing({ kind: "rotated_bbox", geom });
    },
    [beginManualDrawing],
  );

  const handleCommitRotateBbox = useCallback(
    (id: string, before: RotatedBboxGeometry, after: RotatedBboxGeometry) => {
      if (blockIfLocked()) return;
      if (!taskId) return;
      if (
        before.cx === after.cx &&
        before.cy === after.cy &&
        before.w === after.w &&
        before.h === after.h &&
        before.angle === after.angle
      )
        return;
      const payload = { geometry: after };
      markPendingGeom?.(id, after);
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { geometry: before },
              after: { geometry: after },
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticUpdateGeom(id, after as unknown as Record<string, unknown>);
              history.push({
                kind: "update",
                annotationId: id,
                before: { geometry: before },
                after: { geometry: after },
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      taskId,
      enqueueOnError,
      optimisticUpdateGeom,
      markPendingGeom,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const handlePickPendingClass = useCallback(
    (cls: string) => {
      const pending = s.pendingDrawing;
      if (!pending || !cls) return;
      if (pending.kind?.startsWith("video_")) return;
      if (pending.creation) {
        if (
          manualDraftRef.current?.creation?.id !== pending.creation.id ||
          pending.creation.phase === "saving"
        )
          return;
        const draft = {
          ...pending.creation,
          className: cls,
          attributes: creationAttributeDefaults(
            attributeSchemaForUnit(toolBindings, pending.creation.toolUnitId),
            cls,
          ),
        };
        submitManual({ ...pending, creation: draft });
        return;
      }
      s.setPendingDrawing(null);
      switch (pending.kind) {
        case "rotated_bbox":
          createRotatedBboxWithClass(pending.geom, cls);
          break;
        case "polygon":
          createPolygonWithClass(pending.points, cls);
          break;
        case "polyline":
          createPolylineWithClass(pending.points, cls);
          break;
        case "keypoint":
          createKeypointWithClass(pending.points, cls);
          break;
        default:
          createBboxWithClass(pending.geom, cls);
      }
    },
    [
      createBboxWithClass,
      createKeypointWithClass,
      createPolygonWithClass,
      createPolylineWithClass,
      createRotatedBboxWithClass,
      s,
      toolBindings,
      submitManual,
    ],
  );

  const handleDeleteBox = useCallback(
    (id: string) => {
      if (blockIfLocked()) return;
      const target = annotationsRef.current.find((a) => a.id === id);
      if (target?.is_locked) {
        pushToast({ msg: "对象已锁定", sub: "请先解锁再删除", kind: "warning" });
        return;
      }
      if (target && taskId) {
        mutations.delete.mutate(id, {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({ kind: "delete", annotation: target });
            pushToast({ msg: "已删除标注", kind: "success" });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "delete",
                id: randomId(),
                taskId,
                annotationId: id,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticDelete(id);
              history.push({ kind: "delete", annotation: target });
            }),
        });
      }
      s.setSelectedId(null);
    },
    [
      blockIfLocked,
      mutations,
      history,
      pushToast,
      s,
      taskId,
      enqueueOnError,
      optimisticDelete,
      annotationsRef,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const handleCommitMove = useCallback(
    (
      id: string,
      before: Geom,
      after: Geom,
      childMoves?: { id: string; before: Geometry; after: Geometry }[],
    ) => {
      if (blockIfLocked()) return;
      if (!taskId) return;
      const beforeG = bboxGeom(before);
      const afterG = bboxGeom(after);
      if (childMoves && childMoves.length > 0) {
        const commands: {
          kind: "update";
          annotationId: string;
          before: { geometry: Geometry };
          after: { geometry: Geometry };
        }[] = [
          {
            kind: "update",
            annotationId: id,
            before: { geometry: beforeG },
            after: { geometry: afterG },
          },
          ...childMoves.map((c) => ({
            kind: "update" as const,
            annotationId: c.id,
            before: { geometry: c.before },
            after: { geometry: c.after },
          })),
        ];
        let remaining = commands.length;
        const accepted = new Set<string>();
        const finish = (annotationId: string, didAccept: boolean) => {
          if (didAccept) accepted.add(annotationId);
          remaining -= 1;
          if (remaining !== 0) return;
          history.pushBatch(commands.filter((command) => accepted.has(command.annotationId)));
          if (
            currentOwner.current === owner &&
            owner.meUserId &&
            isCurrentAuthOwner(owner.meUserId)
          )
            pushToast({
              msg: `已移动 ${accepted.size}/${commands.length} 个标注`,
              kind: accepted.size === commands.length ? "success" : "warning",
            });
        };
        const fire = (annotationId: string, geometry: Geometry) => {
          const p = { geometry };
          markPendingGeom?.(annotationId, geometry);
          mutations.update.mutate(
            { annotationId, payload: p },
            {
              onSuccess: () => finish(annotationId, true),
              onError: async (err) => {
                let didAccept = false;
                try {
                  await enqueueOnError(err, async () => {
                    await enqueueOwnedDurably({
                      kind: "update",
                      id: randomId(),
                      taskId,
                      annotationId,
                      payload: p,
                      ts: Date.now(),
                    });
                    didAccept = true;
                    optimisticUpdateGeom(annotationId, geometry);
                  });
                } finally {
                  finish(annotationId, didAccept);
                }
              },
            },
          );
        };
        fire(id, afterG);
        for (const c of childMoves) fire(c.id, c.after);
        return;
      }
      const payload = { geometry: afterG };
      markPendingGeom?.(id, afterG);
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { geometry: beforeG },
              after: { geometry: afterG },
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticUpdateGeom(id, afterG);
              history.push({
                kind: "update",
                annotationId: id,
                before: { geometry: beforeG },
                after: { geometry: afterG },
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      taskId,
      enqueueOnError,
      optimisticUpdateGeom,
      pushToast,
      markPendingGeom,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const handleCommitResize = useCallback(
    (id: string, before: Geom, after: Geom) => {
      if (blockIfLocked()) return;
      if (after.w < 0.005 || after.h < 0.005) {
        pushToast({ msg: "框太小未保存", sub: "拖动到至少 0.5% × 0.5%", kind: "error" });
        return;
      }
      if (!taskId) return;
      const beforeG = bboxGeom(before);
      const afterG = bboxGeom(after);
      const payload = { geometry: afterG };
      markPendingGeom?.(id, afterG);
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { geometry: beforeG },
              after: { geometry: afterG },
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticUpdateGeom(id, afterG);
              history.push({
                kind: "update",
                annotationId: id,
                before: { geometry: beforeG },
                after: { geometry: afterG },
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      pushToast,
      taskId,
      enqueueOnError,
      optimisticUpdateGeom,
      markPendingGeom,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const handleCommitPolygonGeometry = useCallback(
    (id: string, before: Pt[], after: Pt[]) => {
      if (blockIfLocked()) return;
      const target = annotationsRef.current.find((a) => a.id === id);
      if (isComplexPolygonGeometry(target?.geometry)) {
        pushToast({
          msg: "复杂多边形暂不支持直接编辑",
          sub: "已保留全部外环与内环，未提交本次变更",
          kind: "warning",
        });
        return;
      }
      const isPolyline = target?.geometry.type === "polyline";
      if (isPolyline) {
        if (after.length < 2) {
          pushToast({ msg: "折线至少需要 2 顶点", kind: "error" });
          return;
        }
      } else {
        if (after.length < 3) {
          pushToast({ msg: "多边形至少需要 3 顶点", kind: "error" });
          return;
        }
        if (!isSelfIntersecting(after).ok) {
          pushToast({ msg: "多边形自相交，已撤销", kind: "error" });
          return;
        }
      }
      if (!taskId) return;
      const beforeG = isPolyline ? polylineGeom(before) : polygonGeom(before);
      const afterG = isPolyline ? polylineGeom(after) : polygonGeom(after);
      const payload = { geometry: afterG };
      markPendingGeom?.(id, afterG);
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { geometry: beforeG },
              after: { geometry: afterG },
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              optimisticUpdateGeom(id, afterG);
              history.push({
                kind: "update",
                annotationId: id,
                before: { geometry: beforeG },
                after: { geometry: afterG },
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      pushToast,
      taskId,
      enqueueOnError,
      optimisticUpdateGeom,
      annotationsRef,
      markPendingGeom,
      enqueueOwnedDurably,
      owner,
    ],
  );

  const handlePatchShapeFlag = useCallback(
    (id: string, flag: "z_order" | "is_locked" | "is_hidden", value: number | boolean) => {
      if (blockIfLocked()) return;
      if (!taskId) return;
      const target = annotationsRef.current.find((a) => a.id === id);
      const before = target ? (target as unknown as Record<string, unknown>)[flag] : undefined;
      const payload = { [flag]: value } as AnnotationUpdatePayload;
      mutations.update.mutate(
        { annotationId: id, payload },
        {
          onSuccess: () => {
            if (
              currentOwner.current !== owner ||
              !owner.meUserId ||
              !isCurrentAuthOwner(owner.meUserId)
            )
              return;
            history.push({
              kind: "update",
              annotationId: id,
              before: { [flag]: before } as AnnotationUpdatePayload,
              after: payload,
            });
          },
          onError: (err) =>
            enqueueOnError(err, async () => {
              await enqueueOwnedDurably({
                kind: "update",
                id: randomId(),
                taskId,
                annotationId: id,
                payload,
                ts: Date.now(),
              });
              if (
                currentOwner.current !== owner ||
                !owner.meUserId ||
                !isCurrentAuthOwner(owner.meUserId)
              )
                return;
              history.push({
                kind: "update",
                annotationId: id,
                before: { [flag]: before } as AnnotationUpdatePayload,
                after: payload,
              });
            }),
        },
      );
    },
    [
      blockIfLocked,
      mutations,
      history,
      taskId,
      enqueueOnError,
      annotationsRef,
      enqueueOwnedDurably,
      owner,
    ],
  );

  return {
    optimisticEnqueueCreate,
    beginBboxDrawing,
    submitManualDrawing,
    changeManualAttributes,
    cancelManualDrawing,
    hasManualDraft:
      !!s.pendingDrawing?.creation ||
      polygonDraftPoints.length > 0 ||
      keypointDraftPoints.length > 0,
    createBboxWithClass,
    createRotatedBbox,
    handleCommitRotateBbox,
    handlePickPendingClass,
    submitPolygon,
    submitPolyline,
    handleDeleteBox,
    handleCommitMove,
    handleCommitResize,
    handleCommitPolygonGeometry,
    handleCommitKeypointGeometry,
    handlePatchShapeFlag,
    polygonDraftPoints,
    setPolygonDraftPoints,
    polygonHandle,
    polylineHandle,
    keypointHandle,
  };
}
