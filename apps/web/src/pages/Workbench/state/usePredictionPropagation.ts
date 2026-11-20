// 第 3 批(B 类)· 从 useWorkbenchShellModel 抽出的跨帧预测传播竞态簇。
// Shift+→/← 把选中框延续到同 scene 邻帧 / 任意帧 / 区间插值,带并发守卫 +
// 运动补偿单次提示。3 个 ref(pending 补选 / in-flight 守卫 / 已提示)跨 4 个回调
// 与 2 个外部 effect(切 task 清理、导航后补选)协作:故 pendingCrossFrameSelectRef
// 作返回值供组件那两处 effect 读写;in-flight / warned 两 ref 全内部。
// navigateToCrossFrameTask(绑组件导航原语)作参数传入。逐字搬运,行为零变化。
//
// 守护手段(诚实标注):jsdom 测不到 ref 竞态时序,靠人工冒烟 ——
// 连按 Shift+→ 验证邻帧不重复建标注(同 track_id)、导航后自动补选新框。
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
  // 并发/重复触发守卫: 按住 Alt+→ auto-repeat 或快速连按时, 防止并发
  // 多个 propagate POST 在目标帧造出共享同一新 track_id 的重复 annotation。
  const crossFrameInFlightRef = useRef(false);
  // "scene 无 ego 轨迹,未做运动补偿" 每会话只轻提示一次,避免逐帧刷 toast。
  const motionCompWarnedRef = useRef(false);
  // 传播是异步的(getNeighbors + propagate),期间用户可能用任务列表/上下帧按钮手动切到
  // 别的 task。latestTaskIdRef 跟踪最新挂载的 taskId,用于在 await 后判断「起始 task 是否
  // 已被用户切走」——若是,则不再抢导航、不预约补选,避免把用户从当前 task 拽回传播目标。
  const latestTaskIdRef = useRef(taskId);
  useEffect(() => {
    latestTaskIdRef.current = taskId;
  }, [taskId]);

  // 传播落库后统一收尾:仅当起始 task 仍是当前 task 时才导航 + 预约补选。
  // 服务端框已建,失效缓存即可呈现;切走后只是不抢用户当前视图。
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
        // 按需直拉邻帧 (非缓存), propagate 才发请求, 避免给每个 task 都预取。
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
          // 失效目标 task 标注缓存, 跳过去后重新拉到含新框的列表。
          queryClient.invalidateQueries({
            queryKey: ["annotations", resolution.taskId],
          });
          // 源 task 框可能刚被分配 track_id, 失效让本帧高亮同步。
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

  // 批量延续: 当前帧全部 box_3d 一次运动补偿 propagate 到邻帧。
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

  // 把选中框延续到 scene 内任意帧(插值工作流: 先把链建到区间终点)。
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

  // 区间插值: 当前 task(起点帧)与 toTask(终点帧)的同 track 框之间,
  // 按 track_id 自动生成中间插值框；完成后跳到首个插值帧预览 (ADR-0045)。
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
