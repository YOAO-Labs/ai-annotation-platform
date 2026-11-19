/**
 * WorkbenchShell 第三刀。
 *
 * 从 WorkbenchShell.tsx 抽出切题 + 提交流程：
 *   - navigateTask(direction)        切上/下一题，距末页 10 条预加载
 *   - smartNext(mode)                N=未标注 / U=不确定，无目标自动加载下一页 + toast
 *   - hasMissingRequired             所有 annotation 必填属性巡检
 *   - handleSubmitTask()             提交质检（必填巡检拦截 + 成功后自动切下一题）
 *
 * 本 hook 将 task flow 与 ImageStage 渲染逻辑解耦，便于单测。
 */
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { TaskResponse, AnnotationResponse } from "@/types";
import type { AttributeSchema } from "@/api/projects";
import { getMissingRequired } from "../shell/AttributeForm";

interface ToastFn {
  (toast: { msg: string; sub?: string; kind?: "success" | "warning" | "error" | "" }): void;
}

export interface UseWorkbenchTaskFlowParams {
  taskId: string | undefined;
  task: TaskResponse | undefined;
  tasks: TaskResponse[];
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  annotationsRef: React.MutableRefObject<AnnotationResponse[]>;
  /** 当 annotations 列表变化时触发 hasMissingRequired 重算（外部传入 query data 即可） */
  annotationsData: unknown;
  /** Project 对象（仅读 attribute_schema），保持松类型以兼容 generated client */
  currentProject: { attribute_schema?: unknown } | undefined;
  /** 当前工具绑定派生出的真实 schema；优先于已废弃的 project.attribute_schema。 */
  attributeSchema?: AttributeSchema | null;
  /** 跨工具标注时按 annotation.tool_unit_id 解析其绑定 schema。 */
  getAttributeSchema?: (annotation: AnnotationResponse) => AttributeSchema | undefined;
  userBoxesCount: number;
  /** 提交前由 shell 汇总现有写入/Mask/离线队列 owner 状态。 */
  submitBlockedReason?: string | null;
  /** 当前用户/任务上下文仍然有效时才允许成功回调导航或写 UI。 */
  isCurrentContext?: () => boolean;
  currentUserId?: string | null;
  setCurrentTaskId: (id: string) => void | Promise<boolean>;
  setSelectedId: (id: string | null) => void;
  /** 选中首个缺失必填属性，并在属性卡刷新后把焦点移到对应控件。 */
  focusRequiredAttribute?: (annotationId: string, fieldKey?: string) => void;
  pushToast: ToastFn;
  submitTaskMut: {
    mutate: (id: string, opts?: { onSuccess?: () => void; onError?: (e: unknown) => void }) => void;
  };
}

export interface MissingRequiredAttribute {
  annotationId: string;
  className: string;
  fieldKey: string;
}

export interface SubmitBlockInputs {
  pendingWrites: number;
  maskSaving: boolean;
  maskDraft: boolean;
  localDraft: boolean;
  queueCount: number;
  syncError?: string | null;
}

/** Keep the submit gate derived from existing owners; this does not create another save state. */
export function resolveSubmitBlockedReason(input: SubmitBlockInputs): string | null {
  if (input.pendingWrites > 0) return "工作台写入尚未完成";
  if (input.maskSaving) return "Mask 正在保存";
  if (input.maskDraft) return "Mask 草稿尚未保存";
  if (input.localDraft) return "当前标注草稿尚未保存";
  if (input.queueCount > 0) return "离线队列仍有操作待同步";
  if (input.syncError) return input.syncError;
  return null;
}

export interface UseWorkbenchTaskFlowResult {
  navigateTask: (direction: "next" | "prev") => void;
  smartNext: (mode: "open" | "uncertain") => void;
  hasMissingRequired: boolean;
  missingRequired: MissingRequiredAttribute[];
  handleSubmitTask: () => void;
}

export function relativeTaskTargetId(
  taskIds: readonly string[],
  committedTaskId: string | undefined,
  pendingTaskId: string | null,
  direction: "next" | "prev",
): string | null {
  const baseTaskId = pendingTaskId ?? committedTaskId;
  const index = taskIds.indexOf(baseTaskId ?? "");
  if (index < 0) return null;
  const targetIndex =
    direction === "next" ? Math.min(index + 1, taskIds.length - 1) : Math.max(0, index - 1);
  return taskIds[targetIndex] === baseTaskId ? null : taskIds[targetIndex];
}

export function useWorkbenchTaskFlow(p: UseWorkbenchTaskFlowParams): UseWorkbenchTaskFlowResult {
  const {
    taskId,
    task,
    tasks,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    annotationsRef,
    annotationsData,
    currentProject,
    attributeSchema,
    getAttributeSchema,
    userBoxesCount,
    submitBlockedReason,
    isCurrentContext,
    currentUserId,
    setCurrentTaskId,
    setSelectedId,
    focusRequiredAttribute,
    pushToast,
    submitTaskMut,
  } = p;
  const pendingRelativeTaskIdRef = useRef<string | null>(null);
  const submitSequenceRef = useRef(0);
  const contextKey = `${currentUserId ?? ""}:${taskId ?? ""}`;
  const currentContextKeyRef = useRef(contextKey);
  currentContextKeyRef.current = contextKey;

  useEffect(() => {
    const pendingTaskId = pendingRelativeTaskIdRef.current;
    if (
      !pendingTaskId ||
      pendingTaskId === taskId ||
      !tasks.some((item) => item.id === pendingTaskId)
    ) {
      pendingRelativeTaskIdRef.current = null;
    }
  }, [taskId, tasks]);

  const navigateTask = useCallback(
    (direction: "next" | "prev") => {
      if (tasks.length === 0) return;
      const targetTaskId = relativeTaskTargetId(
        tasks.map((item) => item.id),
        taskId,
        pendingRelativeTaskIdRef.current,
        direction,
      );
      if (!targetTaskId) return;
      const newIdx = tasks.findIndex((item) => item.id === targetTaskId);
      // 距末页 10 条时预加载下一页
      if (
        direction === "next" &&
        newIdx >= tasks.length - 10 &&
        hasNextPage &&
        !isFetchingNextPage
      ) {
        fetchNextPage();
      }
      pendingRelativeTaskIdRef.current = targetTaskId;
      void Promise.resolve(setCurrentTaskId(targetTaskId)).then((allowed) => {
        if (allowed === false && pendingRelativeTaskIdRef.current === targetTaskId) {
          pendingRelativeTaskIdRef.current = null;
        }
      });
      setSelectedId(null);
    },
    [
      tasks,
      taskId,
      hasNextPage,
      isFetchingNextPage,
      fetchNextPage,
      setCurrentTaskId,
      setSelectedId,
    ],
  );

  /** N（下一未标注）/ U（下一最不确定，total_predictions desc）。 */
  const smartNext = useCallback(
    (mode: "open" | "uncertain") => {
      if (tasks.length === 0) return;
      const idx = tasks.findIndex((t) => t.id === taskId);
      const after = tasks.slice(idx + 1);
      const target =
        mode === "open"
          ? after.find((t) => t.status !== "completed" && t.total_annotations === 0)
          : [...after]
              .filter((t) => t.total_predictions > 0 && t.total_annotations === 0)
              .sort((a, b) => b.total_predictions - a.total_predictions)[0];
      if (!target) {
        if (hasNextPage && !isFetchingNextPage) {
          fetchNextPage();
          pushToast({ msg: "正在加载下一页任务…", kind: "warning" });
        } else {
          pushToast({
            msg: mode === "open" ? "前方已无未标注题目" : "前方已无不确定题目",
            kind: "warning",
          });
        }
        return;
      }
      setCurrentTaskId(target.id);
      setSelectedId(null);
    },
    [
      tasks,
      taskId,
      hasNextPage,
      isFetchingNextPage,
      fetchNextPage,
      setCurrentTaskId,
      setSelectedId,
      pushToast,
    ],
  );

  /** 计算所有 annotation 中是否有 required 属性未填（驱动提交按钮 disabled）。 */
  const missingRequired = useMemo<MissingRequiredAttribute[]>(() => {
    const fallbackSchema =
      attributeSchema ?? (currentProject?.attribute_schema as AttributeSchema | null | undefined);
    const missing: MissingRequiredAttribute[] = [];
    for (const annotation of annotationsRef.current) {
      const schema = getAttributeSchema?.(annotation) ?? fallbackSchema;
      for (const fieldKey of getMissingRequired(
        schema ?? undefined,
        annotation.class_name,
        annotation.attributes,
      )) {
        missing.push({ annotationId: annotation.id, className: annotation.class_name, fieldKey });
      }
    }
    return missing;
    // 当 annotations 列表变化时重算
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [annotationsData, attributeSchema, currentProject?.attribute_schema, getAttributeSchema]);
  const hasMissingRequired = missingRequired.length > 0;

  const handleSubmitTask = useCallback(() => {
    if (!taskId) return;
    if (submitBlockedReason) {
      pushToast({
        msg: "还有修改正在保存，暂时无法提交",
        sub: submitBlockedReason,
        kind: "warning",
      });
      return;
    }
    if (hasMissingRequired) {
      const first = missingRequired[0];
      if (first) {
        setSelectedId(first.annotationId);
        focusRequiredAttribute?.(first.annotationId, first.fieldKey);
      }
      pushToast({
        msg: "存在必填属性未填，无法提交",
        sub: first
          ? `对象 ${first.annotationId.slice(0, 8)}… · 字段 ${first.fieldKey}`
          : "请检查右侧标注属性表单",
        kind: "error",
      });
      return;
    }
    const sequence = ++submitSequenceRef.current;
    const submittedContextKey = currentContextKeyRef.current;
    submitTaskMut.mutate(taskId, {
      onSuccess: () => {
        if (
          submitSequenceRef.current !== sequence ||
          currentContextKeyRef.current !== submittedContextKey ||
          isCurrentContext?.() === false
        )
          return;
        pushToast({
          msg: `已提交 ${task?.display_id} 至质检`,
          sub: `共 ${userBoxesCount} 个标注`,
          kind: "success",
        });
        navigateTask("next");
      },
    });
  }, [
    taskId,
    submitTaskMut,
    pushToast,
    task?.display_id,
    userBoxesCount,
    navigateTask,
    hasMissingRequired,
    missingRequired,
    submitBlockedReason,
    isCurrentContext,
    focusRequiredAttribute,
    setSelectedId,
  ]);

  return { navigateTask, smartNext, hasMissingRequired, missingRequired, handleSubmitTask };
}
