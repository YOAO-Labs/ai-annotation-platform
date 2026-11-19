import { useEffect, useMemo, useRef, useState } from "react";
import { MILESTONES, pickLine, type PetMood } from "./petLines";

const IDLE_MS = 45_000; // 久坐阈值:无上下文干扰多久后冒泡
const TALK_HOLD_MS = 6_000; // 一句话停留时长
const CELEBRATE_MS = 3_400;
const AI_RUNNING_MIN_MS = 800;

export type PetSelectionSourceKind =
  | "manual"
  | "prediction"
  | "interpolated"
  | "legacy"
  | "unknown";

export interface WorkbenchPetContext {
  selection: {
    count: number;
    title: string | null;
    collapsed: boolean;
    sourceKind: PetSelectionSourceKind;
  };
  ai: {
    running: boolean;
    candidateCount: number;
    backendOnline?: boolean;
  };
  workflow: {
    saving: boolean;
    offline: boolean;
    offlineQueueCount: number;
    readOnly: boolean;
    reviewMode: boolean;
  };
  quality: {
    warningCount: number;
    primaryWarning: string | null;
  };
  counts: {
    annotationCount: number;
  };
}

export interface PetStateResult {
  mood: PetMood;
  message: string | null;
  priority: number;
}

interface UsePetStateArgs {
  context: WorkbenchPetContext;
  /** 点击计数:每 +1 触发一句「戳一下」彩蛋。 */
  poke: number;
}

function selectedMessage(sourceKind: PetSelectionSourceKind): string {
  if (sourceKind === "prediction") return "预测来源";
  if (sourceKind === "interpolated") return "插值帧";
  if (sourceKind === "legacy") return "当前帧";
  return "已选中";
}

function hasWorkContext(context: WorkbenchPetContext, aiRunning: boolean): boolean {
  return (
    context.selection.count > 0 ||
    aiRunning ||
    context.ai.candidateCount > 0 ||
    context.ai.backendOnline === false ||
    context.workflow.saving ||
    context.workflow.offline ||
    context.workflow.offlineQueueCount > 0 ||
    context.workflow.readOnly ||
    context.workflow.reviewMode ||
    context.quality.warningCount > 0
  );
}

function deriveContextState(
  context: WorkbenchPetContext,
  aiRunning: boolean,
  idleTalk: string | null,
): PetStateResult {
  if (context.workflow.offlineQueueCount > 0) {
    return {
      mood: "offline",
      message: `离线队列 ${context.workflow.offlineQueueCount}`,
      priority: 90,
    };
  }
  if (context.workflow.saving) {
    return { mood: "offline", message: "保存中", priority: 90 };
  }
  if (context.workflow.offline) {
    return { mood: "offline", message: "离线待同步", priority: 90 };
  }
  if (context.ai.backendOnline === false) {
    return { mood: "offline", message: "AI 后端未在线", priority: 90 };
  }
  if (context.quality.warningCount > 0) {
    return {
      mood: "warning",
      message: context.quality.primaryWarning ?? "需要检查",
      priority: 80,
    };
  }
  if (aiRunning) {
    return { mood: "aiRunning", message: "AI 推理中", priority: 70 };
  }
  if (context.ai.candidateCount > 0) {
    return {
      mood: "candidateReady",
      message:
        context.ai.candidateCount > 1 ? `${context.ai.candidateCount} 个候选待处理` : "候选待处理",
      priority: 60,
    };
  }
  if (context.selection.count > 1) {
    return {
      mood: "multiSelected",
      message: `已选 ${context.selection.count} 个`,
      priority: 55,
    };
  }
  if (context.selection.count === 1 && context.selection.collapsed) {
    return {
      mood: "holding",
      message: context.selection.title,
      priority: 50,
    };
  }
  if (context.selection.count === 1) {
    return {
      mood: "selected",
      message: selectedMessage(context.selection.sourceKind),
      priority: 40,
    };
  }
  if (context.workflow.reviewMode) {
    return { mood: "review", message: "审核中", priority: 30 };
  }
  if (context.workflow.readOnly) {
    return { mood: "review", message: "只读", priority: 30 };
  }
  if (idleTalk) {
    return { mood: "idleTalk", message: idleTalk, priority: 10 };
  }
  return { mood: "idle", message: null, priority: 0 };
}

/**
 * 工作台桌宠状态机：
 * - 标注里程碑仍是 transient 最高优先级。
 * - 普通标注 +1 不触发气泡/姿态切换,避免高频标注时视觉闪烁。
 * - 常态状态全部从 WorkbenchPetContext 纯前端派生。
 * - 久坐闲聊只在没有工作上下文时出现。
 */
export function usePetState({ context, poke }: UsePetStateArgs): PetStateResult {
  const [transient, setTransient] = useState<{
    mood: "celebrate";
    message: string;
  } | null>(null);
  const [idleTalk, setIdleTalk] = useState<string | null>(null);
  const [heldAiRunning, setHeldAiRunning] = useState(context.ai.running);
  const aiStartedAt = useRef<number | null>(context.ai.running ? Date.now() : null);

  // 标注里程碑 → 短暂庆祝。普通 +1 不再触发气泡/姿态切换,避免连续标注时视觉闪烁。
  const prevCount = useRef(context.counts.annotationCount);
  useEffect(() => {
    const prev = prevCount.current;
    prevCount.current = context.counts.annotationCount;
    if (context.counts.annotationCount === prev + 1) {
      const milestone = (MILESTONES as readonly number[]).includes(context.counts.annotationCount);
      if (milestone) {
        setTransient({
          mood: "celebrate",
          message: pickLine("celebrate", context.counts.annotationCount),
        });
      }
    }
  }, [context.counts.annotationCount]);

  useEffect(() => {
    if (!transient) return;
    const t = window.setTimeout(() => setTransient(null), CELEBRATE_MS);
    return () => window.clearTimeout(t);
  }, [transient]);

  // AI running 至少展示 800ms,避免极快请求导致桌宠状态一闪而过。
  useEffect(() => {
    if (context.ai.running) {
      aiStartedAt.current = Date.now();
      setHeldAiRunning(true);
      return;
    }
    if (!heldAiRunning) return;
    const elapsed =
      aiStartedAt.current == null ? AI_RUNNING_MIN_MS : Date.now() - aiStartedAt.current;
    const wait = Math.max(0, AI_RUNNING_MIN_MS - elapsed);
    const t = window.setTimeout(() => setHeldAiRunning(false), wait);
    return () => window.clearTimeout(t);
  }, [context.ai.running, heldAiRunning]);

  // 戳一下 → 随机彩蛋。若当前有更高优先级状态,彩蛋会被自然压住。
  useEffect(() => {
    if (poke === 0) return;
    setIdleTalk(pickLine("poke"));
    const t = window.setTimeout(() => setIdleTalk(null), TALK_HOLD_MS);
    return () => window.clearTimeout(t);
  }, [poke]);

  const canIdleTalk = useMemo(
    () => !hasWorkContext(context, heldAiRunning),
    [context, heldAiRunning],
  );

  // 久坐检测(仅无上下文时;有选中 / AI / warning / offline 代表在干活,不打扰)。
  useEffect(() => {
    if (!canIdleTalk) {
      setIdleTalk(null);
      return;
    }
    let idleTimer: number | null = null;
    let clearTimer: number | null = null;
    const clearTimers = () => {
      if (idleTimer) window.clearTimeout(idleTimer);
      if (clearTimer) window.clearTimeout(clearTimer);
      idleTimer = null;
      clearTimer = null;
    };
    const schedule = () => {
      idleTimer = window.setTimeout(() => {
        setIdleTalk(pickLine("idleTalk"));
        clearTimer = window.setTimeout(() => {
          setIdleTalk(null);
          schedule();
        }, TALK_HOLD_MS);
      }, IDLE_MS);
    };
    const bump = () => {
      setIdleTalk(null);
      clearTimers();
      schedule();
    };
    window.addEventListener("pointermove", bump, { passive: true });
    window.addEventListener("keydown", bump);
    schedule();
    return () => {
      clearTimers();
      window.removeEventListener("pointermove", bump);
      window.removeEventListener("keydown", bump);
    };
  }, [canIdleTalk]);

  if (transient) {
    return { mood: transient.mood, message: transient.message, priority: 100 };
  }
  return deriveContextState(context, heldAiRunning, idleTalk);
}
