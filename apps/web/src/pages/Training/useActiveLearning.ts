import { randomId } from "@/utils/id";
import { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { useTrainingSessionRevision } from "./trainingViewOwner";
import { activeLearningApi, type ActiveRound, type SelectionTask } from "@/api/activeLearning";
import type { AssertTrainingOwner } from "./trainingViewOwner";
import { normalizeTask } from "@/api/generated/capabilityVocab.gen";

export function activeLearningTask(task?: unknown): SelectionTask | null {
  const value = normalizeTask(String(task ?? "detect"));
  return value === "detect" || value === "segment" ? value : null;
}

export function activeLearningReason(reason: string, task: SelectionTask) {
  if (reason === "overlapping_classes" && task === "segment") return "跨类别实例掩码重叠";
  if (reason === "low_confidence" && task === "segment") return "实例置信度较低";
  if (reason === "random_fallback" && task === "segment") return "零实例信号随机补位";
  return reasonLabel[reason] ?? reason;
}

export const roundStatus: Record<ActiveRound["status"], string> = {
  queued: "等待执行",
  running: "推理中",
  cancelling: "正在确认停止",
  lost: "执行失联",
  succeeded: "选样完成",
  failed: "执行失败",
  cancelled: "已取消",
};
export const armLabel = { active: "主动建议", random_control: "随机对照" };
export const reasonLabel: Record<string, string> = {
  random_control: "随机对照",
  random_exploration: "随机探索",
  random_fallback: "无信号随机补位",
  overlapping_classes: "类别重叠",
  low_confidence: "低置信度",
};
export const runningStates = new Set(["queued", "running", "cancelling"]);
export function useActiveLearningScope(projectId: string, roundId?: string) {
  const userId = useAuthStore((state) => state.user?.id);
  const token = useAuthStore((state) => state.token);
  const revision = useTrainingSessionRevision();
  const { key } = useLocation();
  return useMemo(
    () => ({ value: randomId(), projectId, roundId, userId, token, revision, key }),
    [projectId, roundId, userId, token, revision, key],
  ).value;
}
export function reportOptions(
  projectId: string,
  round: ActiveRound | undefined,
  attemptId: string,
  scope: string,
  assert: AssertTrainingOwner,
) {
  return {
    queryKey: [
      "active-learning-report",
      projectId,
      scope,
      round?.id,
      attemptId,
      round?.spec_digest,
    ],
    retry: false,
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      assert(signal);
      const report = await activeLearningApi.report(projectId, round!.id, attemptId, { signal });
      assert(signal);
      if (
        report.round_id !== round!.id ||
        report.attempt_id !== attemptId ||
        report.spec_digest !== round!.spec_digest
      )
        throw new Error("选样报告身份不符，请重新加载");
      return report;
    },
  };
}
export function loadPending<T>(key: string): T | null {
  try {
    const value = sessionStorage.getItem(key);
    return value ? (JSON.parse(value) as T) : null;
  } catch {
    return null;
  }
}
export function savePending(key: string, value: unknown) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}
export function definitiveRejection(error: unknown) {
  const status = (error as { status?: number })?.status;
  return (
    typeof status === "number" &&
    status >= 400 &&
    status < 500 &&
    ![401, 403, 408, 425, 429].includes(status)
  );
}
