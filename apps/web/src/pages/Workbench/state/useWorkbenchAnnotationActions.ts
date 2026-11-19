// 从 WorkbenchShell 拆出的标注 mutation 接线层。
//
// 集中管理 7 个 handler：
//  - optimisticEnqueueCreate（共用 fallback：tmpId + cache + 离线队列）
//  - handlePickPendingClass（bbox create）
//  - submitPolygon（polygon create）
//  - handleDeleteBox
//  - handleCommitMove / handleCommitResize / handleCommitPolygonGeometry
// 以及 polygon 草稿状态 + PolygonDraftHandle。
//
// 不在这里管的：键盘 dispatch（键位在 useWorkbenchHotkeys）、history undo/redo 本身。

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
import { isCurrentAuthOwner } from "@/stores/authStore";

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
  projectId: string | undefined;
  meUserId: string | null | undefined;
  queryClient: QueryClient;
  history: ReturnType<typeof useAnnotationHistory>;
  s: ReturnType<typeof useWorkbenchState>;
  pushToast: (toast: ToastInput) => void;
  recordRecentClass: (cls: string) => void;
  mutations: AnnotationMutations;
  enqueueOnError: (err: unknown, fallback: () => void) => void;
  /** 由 shell 维护的当前 annotations ref（避免 stale closure）。*/
  annotationsRef: { current: AnnotationResponse[] };
  /** 任务已锁定（review/completed），所有写动作直接 short-circuit + toast。 */
  isLocked?: boolean;
  /** 当前 keypoint 单元 schema 节点数；放满即自动提交一个实例。0 = 未配置 schema。 */
  keypointNodeCount?: number;
  /** 当前工具自身无类别时直接以 unknown 落库，不打开空类别弹层。 */
  activeToolHasOwnClasses?: boolean;
  toolBindings?: ToolBindings;
  createAnnotationAsync?: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
  /**
   * 同步登记提交在途几何 override, 桥接「setDrag(null)」与「onMutate 微任务
   * 回填 cache」之间的一帧空窗, 防松手闪回原尺寸。见 usePendingGeom。
   */
  markPendingGeom?: (id: string, geom: Geometry) => void;
}

export interface UseWorkbenchAnnotationActionsReturn {
  /** 共用 create fallback：分配 tmpId → cache → history → enqueue。*/
  optimisticEnqueueCreate: (payload: AnnotationPayload) => Promise<void>;
  createBboxWithClass: (geom: Geom, cls: string) => boolean;
  beginBboxDrawing: (geom: Geom, cls?: string) => boolean;
  submitManualDrawing: () => void;
  changeManualAttributes: (id: string, next: Record<string, unknown>) => void;
  cancelManualDrawing: () => boolean;
  hasManualDraft: boolean;
  /** 旋转框：由轴对齐矩形生成 angle=0 草稿，再统一选择类别。 */
  createRotatedBbox: (geom: Geom) => boolean;
  /** 旋转框: 旋转 / 缩放手柄落定时更新 OBB geometry (走 update mutation + history)。 */
  handleCommitRotateBbox: (
    id: string,
    before: RotatedBboxGeometry,
    after: RotatedBboxGeometry,
  ) => void;
  handlePickPendingClass: (cls: string) => void;
  submitPolygon: (points: [number, number][]) => void;
  /** 提交折线（不闭合，≥2 顶点）。*/
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
  /** keypoint 节点几何/可见性变更。 */
  handleCommitKeypointGeometry: (id: string, before: Keypoint[], after: Keypoint[]) => void;
  /** shape 状态位 (z_order / is_locked / is_hidden) 字段级 PATCH。*/
  handlePatchShapeFlag: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  /** polygon 草稿点集（由 hotkeys hook 借用一份引用做 Enter/Esc/Backspace 处理）。*/
  polygonDraftPoints: [number, number][];
  setPolygonDraftPoints: React.Dispatch<React.SetStateAction<[number, number][]>>;
  /** 给 ImageStage 用的 PolygonDraftHandle，已 memoize。*/
  polygonHandle: PolygonDraftHandle;
  /** 折线草稿 handle（closed:false，复用同一 polygonDraftPoints）。*/
  polylineHandle: PolygonDraftHandle;
  /** 给 ImageStage 用的 KeypointDraftHandle，已 memoize。*/
  keypointHandle: KeypointDraftHandle;
}

export function useWorkbenchAnnotationActions({
  taskId,
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
  const enqueueOwnedDurably = useCallback(
    (op: OfflineOp) => {
      if (!meUserId) return Promise.reject(new Error("当前账号已退出，无法接收离线操作"));
      return enqueueDurably(op, { userId: meUserId, projectId });
    },
    [meUserId, projectId],
  );
  const owner = useMemo(
    () => ({ taskId, projectId, meUserId, isLocked }),
    [taskId, projectId, meUserId, isLocked],
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

  /** 锁定时 short-circuit；返回 true 表示已被拦截。 */
  const blockIfLocked = useCallback((): boolean => {
    if (isLocked) {
      pushToast({ msg: "任务已锁定", sub: "撤回提交或继续编辑后再操作", kind: "warning" });
      return true;
    }
    return false;
  }, [isLocked, pushToast]);

  /** 共用：写入 annotations cache 中的某条 geometry（bbox 移动 / resize / polygon 编辑都用）。 */
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

  /** 共用：从 annotations cache 中删除一条（delete fallback）。 */
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

  /** create 失败兜底（共用 bbox / polygon）。*/
  const optimisticEnqueueCreate = useCallback(
    async (payload: AnnotationPayload) => {
      if (!taskId || !meUserId) throw new Error("当前账号无法接收离线操作");
      const tmpId = `tmp_${randomId()}`;
      const optimistic: AnnotationResponse = {
        id: tmpId,
        task_id: taskId,
        project_id: projectId ?? null,
        user_id: meUserId ?? null,
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
        payload,
        ts: Date.now(),
      });
      if (currentOwner.current !== owner || !isCurrentAuthOwner(meUserId)) return;
      setQ<AnnotationResponse[]>(["annotations", taskId], (prev) => [...(prev ?? []), optimistic]);
      s.setSelectedId(tmpId);
      history.push({ kind: "create", annotationId: tmpId, payload });
    },
    [taskId, projectId, meUserId, setQ, s, history, owner, enqueueOwnedDurably],
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
        // Business rejections keep the draft; transport failures and temporary server failures
        // are accepted only after the offline queue durably stores the operation.
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

  // ── polygon / polyline 草稿（共用顶点累积 state）──────────────────────
  const {
    points: polygonDraftPoints,
    setPoints: setPolygonDraftPoints,
    getPoints: getPolygonDraftPoints,
  } = usePolygonDraftPoints();
  const polygonBeforeKey = useRef<(() => void) | null>(null);
  const polygonBeforeInput = useRef<((event: KeyboardEvent) => boolean) | null>(null);
  const currentTool = useRef(s.tool);
  currentTool.current = s.tool;
  // Each tool owns its unfinished vertices, including Polygon ↔ Polyline switches.
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

  // ── polyline 提交 ──────────────────────────────────────
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

  // ── keypoint 草稿 ──────────────────────────────────────────
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
  // schema 节点数变化 (切类别 → 不同 schema) 时清空半成品草稿。
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

  // 放满 nodeCount 个点 → 自动提交一个实例。
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
      // 见 usePendingGeom: 同步 mark 目标几何以桥接一帧闪回。
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

  // ── handlers ───────────────────────────────────────────────────────

  const createBboxWithClass = useCallback(
    (geom: Geom, cls: string): boolean => {
      if (s.tool === "box") return beginBboxDrawing(geom, cls);
      if (blockIfLocked()) return false;
      if (!cls) return false;
      const isUnknown = cls === UNKNOWN_CLASS;
      const payload: AnnotationPayload = {
        client_request_id: randomId(),
        annotation_type: "bbox",
        // 工具维度: bbox / smart-box / magic-box 各自映射的 unit.
        tool_unit_id: toolUnitForTool(s.tool),
        class_name: cls,
        geometry: bboxGeom(geom),
        confidence: 1,
      };
      // unknown 是「画完未选类」的兜底，不应污染 activeClass / 最近使用类。
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

  // 旋转框: 轴对齐矩形 → angle=0 的 rotated_bbox，完成后统一弹类别选择。
  const createRotatedBbox = useCallback(
    (geom: Geom): boolean => {
      return beginManualDrawing({ kind: "rotated_bbox", geom });
    },
    [beginManualDrawing],
  );

  // 旋转框: 旋转 / 缩放手柄落定时更新 rotated_bbox geometry。
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
      // 见 usePendingGeom。
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
      // Alt 拖父联动子: 父 + 子的几何更新作为一个 batch 命令进 history (单次 undo 全回退)。
      // 各更新独立 mutate; 失败走同款离线兜底 (乐观写 + enqueue), 但 history 只 pushBatch 一次 (不逐条 push)。
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
        let accepted = 0;
        const finish = (didAccept: boolean) => {
          if (
            currentOwner.current !== owner ||
            !owner.meUserId ||
            !isCurrentAuthOwner(owner.meUserId)
          )
            return;
          if (didAccept) accepted += 1;
          remaining -= 1;
          if (remaining === 0 && accepted === commands.length) history.pushBatch(commands);
        };
        const fire = (annotationId: string, geometry: Geometry) => {
          const p = { geometry };
          // 见 usePendingGeom。
          markPendingGeom?.(annotationId, geometry);
          mutations.update.mutate(
            { annotationId, payload: p },
            {
              onSuccess: () => finish(true),
              onError: (err) =>
                isOfflineCandidate(err)
                  ? enqueueOnError(err, async () => {
                      await enqueueOwnedDurably({
                        kind: "update",
                        id: randomId(),
                        taskId,
                        annotationId,
                        payload: p,
                        ts: Date.now(),
                      });
                      if (
                        currentOwner.current !== owner ||
                        !owner.meUserId ||
                        !isCurrentAuthOwner(owner.meUserId)
                      )
                        return;
                      optimisticUpdateGeom(annotationId, geometry);
                      finish(true);
                    })
                  : (enqueueOnError(err, () => undefined), finish(false)),
            },
          );
        };
        fire(id, afterG);
        for (const c of childMoves) fire(c.id, c.after);
        pushToast({ msg: `联动搬动 ${childMoves.length} 个子框`, kind: "" });
        return;
      }
      const payload = { geometry: afterG };
      // 见 usePendingGeom。
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
      // 见 usePendingGeom。
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
      // 折线 (polyline) 复用同一顶点编辑路径，但不闭合 → 跳过 polygon 专属校验。
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
      // 见 usePendingGeom。
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

  // shape 状态位字段级 PATCH。
  // `flag` ∈ { z_order, is_locked, is_hidden }；value 直传。
  // 失败时仍 enqueue 离线 op（与 handleCommitMove 一致）。
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
