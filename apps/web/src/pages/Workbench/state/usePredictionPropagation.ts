import { useCallback, useEffect, useRef } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { tasksApi } from "@/api/tasks";
import { useToastStore } from "@/components/ui/Toast";
import { resolveCrossFrameTarget } from "./crossFrameTarget";

type PushToast = ReturnType<typeof useToastStore.getState>["push"];

interface UsePredictionPropagationParams {
  taskId: string | undefined;
  selectedId: string | null;
  navigateToCrossFrameTask: (targetTaskId: string) => Promise<boolean>;
  pushToast: PushToast;
  queryClient: QueryClient;
}

export function usePredictionPropagation({
  taskId,
  selectedId,
  navigateToCrossFrameTask,
  pushToast,
  queryClient,
}: UsePredictionPropagationParams) {
  const pendingCrossFrameSelectRef = useRef<{
    taskId: string;
    annotationId: string;
  } | null>(null);
  const crossFrameInFlightRef = useRef(false);
  const motionCompWarnedRef = useRef(false);
  const latestTaskIdRef = useRef(taskId);
  useEffect(() => {
    latestTaskIdRef.current = taskId;
  }, [taskId]);

  const settleCrossFrameTarget = useCallback(
    async (
      startTaskId: string,
      target: { taskId: string; annotationId?: string },
    ): Promise<boolean> => {
      if (latestTaskIdRef.current !== startTaskId) return false;
      const allowed = await navigateToCrossFrameTask(target.taskId);
      if (!allowed || latestTaskIdRef.current !== startTaskId) return false;
      if (target.annotationId) {
        pendingCrossFrameSelectRef.current = {
          taskId: target.taskId,
          annotationId: target.annotationId,
        };
      }
      return true;
    },
    [navigateToCrossFrameTask],
  );
  const warnNoMotionCompensation = useCallback(
    (compensated: boolean) => {
      if (compensated || motionCompWarnedRef.current) return;
      motionCompWarnedRef.current = true;
      pushToast({
        msg: "该 scene 无 ego 轨迹,跨帧未做运动补偿(原样复制)",
        kind: "warning",
      });
    },
    [pushToast],
  );
  const crossFramePropagate = useCallback(
    async (direction: "next" | "prev") => {
      if (!taskId) return;
      if (crossFrameInFlightRef.current) return;
      crossFrameInFlightRef.current = true;
      try {
        const selId = selectedId;
        if (!selId) {
          pushToast({ msg: "请先选中一个目标框", kind: "" });
          return;
        }
        let neighbors;
        try {
          neighbors = await tasksApi.getNeighbors(taskId, 1);
        } catch {
          pushToast({ msg: "获取邻帧失败", kind: "error" });
          return;
        }
        const resolution = resolveCrossFrameTarget(neighbors, direction);
        if (resolution.kind === "no-scene") {
          pushToast({ msg: "当前 task 不属于任何 scene, 无法跨帧延续", kind: "warning" });
          return;
        }
        if (resolution.kind === "boundary") {
          pushToast({
            msg: direction === "next" ? "已是该 scene 最后一帧" : "已是该 scene 首帧",
            kind: "",
          });
          return;
        }
        try {
          const { annotation, motion_compensated } = await tasksApi.propagateToTask(
            taskId,
            selId,
            resolution.taskId,
          );
          queryClient.invalidateQueries({
            queryKey: ["annotations", resolution.taskId],
          });
          queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
          queryClient.invalidateQueries({ queryKey: ["scene-timeline"] });
          await settleCrossFrameTarget(taskId, {
            taskId: resolution.taskId,
            annotationId: annotation.id,
          });
          pushToast({
            msg: `已延续到帧 ${resolution.frameIndex}`,
            kind: "success",
          });
          warnNoMotionCompensation(motion_compensated);
        } catch {
          pushToast({ msg: "跨帧延续失败", kind: "error" });
        }
      } finally {
        crossFrameInFlightRef.current = false;
      }
    },
    [taskId, selectedId, settleCrossFrameTarget, pushToast, queryClient, warnNoMotionCompensation],
  );

  const crossFramePropagateBatch = useCallback(
    async (direction: "next" | "prev") => {
      if (!taskId) return;
      if (crossFrameInFlightRef.current) return;
      crossFrameInFlightRef.current = true;
      try {
        let neighbors;
        try {
          neighbors = await tasksApi.getNeighbors(taskId, 1);
        } catch {
          pushToast({ msg: "获取邻帧失败", kind: "error" });
          return;
        }
        const resolution = resolveCrossFrameTarget(neighbors, direction);
        if (resolution.kind === "no-scene") {
          pushToast({ msg: "当前 task 不属于任何 scene, 无法跨帧延续", kind: "warning" });
          return;
        }
        if (resolution.kind === "boundary") {
          pushToast({
            msg: direction === "next" ? "已是该 scene 最后一帧" : "已是该 scene 首帧",
            kind: "",
          });
          return;
        }
        try {
          const { items, motion_compensated } = await tasksApi.propagateBatch(
            taskId,
            resolution.taskId,
          );
          queryClient.invalidateQueries({
            queryKey: ["annotations", resolution.taskId],
          });
          queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
          queryClient.invalidateQueries({ queryKey: ["scene-timeline"] });
          await settleCrossFrameTarget(taskId, { taskId: resolution.taskId });
          pushToast({
            msg: `${items.length} 个目标已延续到帧 ${resolution.frameIndex}`,
            kind: "success",
          });
          warnNoMotionCompensation(motion_compensated);
        } catch (e) {
          const msg = e instanceof Error ? e.message : "";
          pushToast({
            msg: msg.includes("box_3d") ? "当前帧没有可延续的 3D 框" : "批量延续失败",
            kind: "error",
          });
        }
      } finally {
        crossFrameInFlightRef.current = false;
      }
    },
    [taskId, settleCrossFrameTarget, pushToast, queryClient, warnNoMotionCompensation],
  );

  const crossFramePropagateToTask = useCallback(
    async (targetTaskId: string, targetFrameIndex: number) => {
      if (!taskId) return;
      const selId = selectedId;
      if (!selId) {
        pushToast({ msg: "请先选中一个目标框", kind: "" });
        return;
      }
      if (crossFrameInFlightRef.current) return;
      crossFrameInFlightRef.current = true;
      try {
        const { annotation, motion_compensated } = await tasksApi.propagateToTask(
          taskId,
          selId,
          targetTaskId,
        );
        queryClient.invalidateQueries({ queryKey: ["annotations", targetTaskId] });
        queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
        queryClient.invalidateQueries({ queryKey: ["scene-timeline"] });
        await settleCrossFrameTarget(taskId, {
          taskId: targetTaskId,
          annotationId: annotation.id,
        });
        pushToast({ msg: `已延续到帧 ${targetFrameIndex}, 微调后可插值填充`, kind: "success" });
        warnNoMotionCompensation(motion_compensated);
      } catch {
        pushToast({ msg: "跨帧延续失败", kind: "error" });
      } finally {
        crossFrameInFlightRef.current = false;
      }
    },
    [taskId, selectedId, settleCrossFrameTarget, pushToast, queryClient, warnNoMotionCompensation],
  );

  const crossFrameInterpolate = useCallback(
    async (trackId: string, toTaskId: string) => {
      if (!taskId) return;
      if (crossFrameInFlightRef.current) return;
      crossFrameInFlightRef.current = true;
      try {
        const { annotations, motion_compensated, skipped_frames } = await tasksApi.interpolateRange(
          taskId,
          trackId,
          toTaskId,
        );
        const affectedTasks = new Set(annotations.map((a) => a.task_id));
        for (const tid of affectedTasks) {
          queryClient.invalidateQueries({ queryKey: ["annotations", tid] });
        }
        queryClient.invalidateQueries({ queryKey: ["scene-timeline"] });
        if (annotations.length === 0) {
          pushToast({
            msg: `区间内中间帧均已有该目标的框(跳过 ${skipped_frames.length} 帧)`,
            kind: "",
          });
          return;
        }
        const first = annotations[0];
        await settleCrossFrameTarget(taskId, {
          taskId: first.task_id,
          annotationId: first.id,
        });
        pushToast({
          msg:
            `已插值填充 ${annotations.length} 帧` +
            (skipped_frames.length > 0 ? `(跳过已有 ${skipped_frames.length} 帧)` : ""),
          kind: "success",
        });
        warnNoMotionCompensation(motion_compensated);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "";
        pushToast({
          msg: msg ? `插值失败: ${msg}` : "插值失败",
          kind: "error",
        });
      } finally {
        crossFrameInFlightRef.current = false;
      }
    },
    [taskId, settleCrossFrameTarget, pushToast, queryClient, warnNoMotionCompensation],
  );

  return {
    pendingCrossFrameSelectRef,
    crossFramePropagate,
    crossFramePropagateBatch,
    crossFramePropagateToTask,
    crossFrameInterpolate,
  };
}
