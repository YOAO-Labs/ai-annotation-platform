import { randomId } from "@/utils/id";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { isCurrentAuthOwner } from "@/stores/authStore";
import type { AnnotationResponse, VideoTrackKeyframe, VideoTrackMaskKeyframe } from "@/types";
import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import type {
  AnnotationSliceResponse,
  AnnotationSliceRestoreRequest,
} from "@/api/annotationSlices";

export interface SliceCommand {
  kind: "slice";
  operationId: string;
  resultVersions: Record<string, number>;
  restoreExpiresAt: string;
  pendingRestore?: { target: "before" | "after"; idempotencyKey: string };
}

export interface VideoMaskFrameState {
  keyframe: VideoTrackMaskKeyframe | null;
  manualOutside: boolean;
}

/**
 * 标注操作命令栈。每条命令记录足够 redo / undo 的状态。
 * 切任务清栈，避免误撤销另一题。
 */
export type Command =
  | SliceCommand
  | { kind: "create"; annotationId: string; payload: AnnotationPayload }
  | { kind: "delete"; annotation: AnnotationResponse }
  | {
      kind: "update";
      annotationId: string;
      before: AnnotationUpdatePayload;
      after: AnnotationUpdatePayload;
    }
  | {
      kind: "videoKeyframe";
      annotationId: string;
      frameIndex: number;
      before: VideoTrackKeyframe | null;
      after: VideoTrackKeyframe | null;
    }
  | {
      kind: "videoMaskFrame";
      annotationId: string;
      frameIndex: number;
      before: VideoMaskFrameState;
      after: VideoMaskFrameState;
    }
  | { kind: "acceptPrediction"; predictionId: string; createdAnnotationIds: string[] }
  /** 批量命令：undo 时反序应用、redo 时正序应用。子命令必须不含 batch（一层）。 */
  | { kind: "batch"; commands: LeafCommand[] };

export type LeafCommand = Exclude<Command, { kind: "batch" | "slice" }>;

/** v0.6.3 P1：单条非 batch 命令的实际执行。导出为纯函数便于单测。
 *  注意 cmd 在 redo / delete-undo 路径会被就地 mutate（annotationId / annotation.id），与 hook 内栈引用相同。 */
export async function applyLeaf(cmd: LeafCommand, direction: "undo" | "redo", h: HistoryHandlers) {
  if (cmd.kind === "create") {
    if (direction === "undo") {
      // v0.6.3 P0：tmpId 走纯本地分支，避免对未入库的 id 调 DELETE → 404
      if (cmd.annotationId.startsWith("tmp_") && h.removeLocalCreate) {
        await h.removeLocalCreate(cmd.annotationId);
      } else {
        await h.deleteAnnotation(cmd.annotationId);
      }
      // A redo is a new creation. Keep its identity on the command so retries
      // replay that creation, while the next undo/redo cycle gets another key.
      cmd.payload = { ...cmd.payload, client_request_id: randomId() };
    } else {
      const fresh = await h.createAnnotation(cmd.payload);
      // redo 重新创建会拿到新 id；后续 undo 还得知道这个 id
      cmd.annotationId = fresh.id;
    }
  } else if (cmd.kind === "delete") {
    if (direction === "undo") {
      const restored = await h.createAnnotation({
        annotation_type: cmd.annotation.annotation_type,
        tool_unit_id: cmd.annotation.tool_unit_id ?? undefined,
        class_name: cmd.annotation.class_name,
        geometry: cmd.annotation.geometry,
        confidence: cmd.annotation.confidence ?? undefined,
        parent_prediction_id: cmd.annotation.parent_prediction_id ?? undefined,
        lead_time: cmd.annotation.lead_time ?? undefined,
        attributes: cmd.annotation.attributes ?? undefined,
      });
      // 反向时新 id；后续 redo 删除以新 id 执行
      cmd.annotation = { ...cmd.annotation, id: restored.id };
    } else {
      await h.deleteAnnotation(cmd.annotation.id);
    }
  } else if (cmd.kind === "update") {
    const target = direction === "undo" ? cmd.before : cmd.after;
    await h.updateAnnotation(cmd.annotationId, target);
  } else if (cmd.kind === "videoKeyframe") {
    if (!h.updateVideoKeyframe) throw new Error("updateVideoKeyframe handler is required");
    const target = direction === "undo" ? cmd.before : cmd.after;
    await h.updateVideoKeyframe(cmd.annotationId, cmd.frameIndex, target);
  } else if (cmd.kind === "videoMaskFrame") {
    if (!h.updateVideoMaskFrame) throw new Error("updateVideoMaskFrame handler is required");
    const target = direction === "undo" ? cmd.before : cmd.after;
    await h.updateVideoMaskFrame(cmd.annotationId, cmd.frameIndex, target);
  } else if (cmd.kind === "acceptPrediction") {
    // accept 的 undo：删掉那一批由 prediction 派生的 annotation；redo 走批量删除策略不实现，避免重复采纳引发 ID 漂移。
    if (direction === "undo") {
      for (const id of cmd.createdAnnotationIds) {
        // v0.20.22 · 防御过滤: 只删 parent_prediction_id === cmd.predictionId 的标注,
        // 防未来别处误把全量 id 塞进 createdAnnotationIds (原因: 后端曾返回整题全量,
        // 前端把返回值 map 出 id 入历史栈, 撤销会误删他人标注)。
        // handler 未注入或 cache miss (ann 为空/undefined) → 保持旧行为直删, 不 regress。
        const ann = h.getAnnotation?.(id);
        if (ann && ann.parent_prediction_id !== cmd.predictionId) continue;
        try {
          await h.deleteAnnotation(id);
        } catch {
          /* ignore */
        }
      }
    }
    // redo 不再触发后端 accept（对方端点是幂等的但 id 不复用），仅消费 redo 栈无副作用
  }
}

export interface HistoryHandlers {
  restoreSlice?: (
    taskId: string,
    operationId: string,
    payload: AnnotationSliceRestoreRequest,
  ) => Promise<AnnotationSliceResponse>;
  onSliceError?: (error: unknown) => void;
  createAnnotation: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
  deleteAnnotation: (annotationId: string) => Promise<unknown>;
  updateAnnotation: (annotationId: string, payload: AnnotationUpdatePayload) => Promise<unknown>;
  updateVideoKeyframe?: (
    annotationId: string,
    frameIndex: number,
    keyframe: VideoTrackKeyframe | null,
  ) => Promise<unknown>;
  updateVideoMaskFrame?: (
    annotationId: string,
    frameIndex: number,
    state: VideoMaskFrameState,
  ) => Promise<unknown>;
  /** v0.6.3 P0：tmpId 上的 create undo 不能走远端（必 404）。
   *  调用方在工作台闭包内提供：从 react-query cache 删 tmpId + 从离线队列删对应 create op。 */
  removeLocalCreate?: (annotationId: string) => Promise<void> | void;
  /** v0.20.22 · accept 的 undo 过滤: 按 id 查当前 annotation, 用于校验
   *  parent_prediction_id === cmd.predictionId 才删 (防未来别处再往数组塞脏 id)。
   *  未注入 / 读不到时 fallback 到无过滤直删, 保持旧行为不 regress。 */
  getAnnotation?: (annotationId: string) => AnnotationResponse | null | undefined;
}

// v0.8.7 F8 · sessionStorage 持久化（5min TTL，避免误用过期 prediction id）
const HIST_TTL_MS = 5 * 60 * 1000;
const HIST_KEY_PREFIX = "wb:hist:";

function historyKey(taskId: string | undefined, userId?: string): string {
  return `${HIST_KEY_PREFIX}${userId === undefined ? "" : `${userId}:`}${taskId ?? ""}`;
}

interface PersistedHistory {
  undo: Command[];
  redo: Command[];
  ts: number;
}

function readSessionStorage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export function loadHistoryFromSession(
  taskId: string | undefined,
  userId?: string,
): {
  undo: Command[];
  redo: Command[];
} | null {
  if (!taskId) return null;
  const ss = readSessionStorage();
  if (!ss) return null;
  try {
    const raw = ss.getItem(historyKey(taskId, userId));
    if (!raw) return null;
    const data = JSON.parse(raw) as PersistedHistory;
    if (typeof data.ts !== "number") return null;
    if (Date.now() - data.ts > HIST_TTL_MS) {
      ss.removeItem(historyKey(taskId, userId));
      return null;
    }
    return { undo: data.undo ?? [], redo: data.redo ?? [] };
  } catch {
    return null;
  }
}

export function saveHistoryToSession(
  taskId: string | undefined,
  undo: Command[],
  redo: Command[],
  userId?: string,
): void {
  if (!taskId) return;
  const ss = readSessionStorage();
  if (!ss) return;
  try {
    if (undo.length === 0 && redo.length === 0) {
      ss.removeItem(historyKey(taskId, userId));
      return;
    }
    const payload: PersistedHistory = { undo, redo, ts: Date.now() };
    ss.setItem(historyKey(taskId, userId), JSON.stringify(payload));
  } catch {
    // sessionStorage 写失败（quota / private mode）静默忽略；history 仍在内存可用。
  }
}

interface TaskHistory {
  taskId: string | undefined;
  userId?: string;
  undo: Command[];
  redo: Command[];
  busy: boolean;
  revision: number;
}

/** Each in-flight operation settles the history of the task that started it. */
export function useAnnotationHistory(
  taskId: string | undefined,
  handlers: HistoryHandlers,
  userId?: string,
) {
  const histories = useRef(new Map<string | undefined, TaskHistory>());
  const getHistory = useCallback(
    (owner: string | undefined): TaskHistory => {
      const key = historyKey(owner, userId);
      const existing = histories.current.get(key);
      if (existing) return existing;
      const restored = loadHistoryFromSession(owner, userId);
      const created = {
        taskId: owner,
        userId,
        undo: restored?.undo ?? [],
        redo: restored?.redo ?? [],
        busy: false,
        revision: 0,
      };
      histories.current.set(key, created);
      return created;
    },
    [userId],
  );
  const current = useRef(getHistory(taskId));
  const [snapshot, setSnapshot] = useState(() => ({ ...current.current }));
  const handlersRef = useRef(handlers);
  const mounted = useRef(true);
  useLayoutEffect(() => {
    handlersRef.current = handlers;
  });
  useLayoutEffect(() => {
    const previous = current.current;
    if ((previous.taskId !== taskId || previous.userId !== userId) && !previous.busy)
      histories.current.delete(historyKey(previous.taskId, previous.userId));
    current.current = getHistory(taskId);
    setSnapshot({ ...current.current });
  }, [taskId, getHistory, userId]);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const publish = useCallback((record: TaskHistory) => {
    saveHistoryToSession(record.taskId, record.undo, record.redo, record.userId);
    if (mounted.current && current.current === record) setSnapshot({ ...record });
  }, []);

  const push = useCallback(
    (cmd: Command, owner = current.current.taskId) => {
      const record = getHistory(owner);
      // A response retry may be delivered after the original receipt was handled.
      if (
        cmd.kind === "slice" &&
        [...record.undo, ...record.redo].some(
          (item) => item.kind === "slice" && item.operationId === cmd.operationId,
        )
      )
        return;
      record.undo = [...record.undo, cmd];
      record.redo = [];
      record.revision += 1;
      publish(record);
    },
    [getHistory, publish],
  );

  const pushBatch = useCallback(
    (commands: LeafCommand[]) => {
      if (!commands.length) return;
      push(commands.length === 1 ? commands[0] : { kind: "batch", commands });
    },
    [push],
  );

  const replaceAnnotationId = useCallback(
    (tmpId: string, realId: string) => {
      const swapLeaf = (c: LeafCommand): LeafCommand => {
        if (c.kind === "create" && c.annotationId === tmpId) return { ...c, annotationId: realId };
        if (c.kind === "update" && c.annotationId === tmpId) return { ...c, annotationId: realId };
        if (c.kind === "delete" && c.annotation.id === tmpId)
          return { ...c, annotation: { ...c.annotation, id: realId } };
        if (c.kind === "videoKeyframe" && c.annotationId === tmpId)
          return { ...c, annotationId: realId };
        if (c.kind === "videoMaskFrame" && c.annotationId === tmpId)
          return { ...c, annotationId: realId };
        if (c.kind === "acceptPrediction" && c.createdAnnotationIds.includes(tmpId))
          return {
            ...c,
            createdAnnotationIds: c.createdAnnotationIds.map((id) => (id === tmpId ? realId : id)),
          };
        return c;
      };
      const swap = (c: Command): Command => {
        if (c.kind === "slice") return c;
        if (c.kind === "batch") return { ...c, commands: c.commands.map(swapLeaf) };
        return swapLeaf(c);
      };
      const record = current.current;
      record.undo = record.undo.map(swap);
      record.redo = record.redo.map(swap);
      publish(record);
    },
    [publish],
  );

  const execute = useCallback(
    async (direction: "undo" | "redo") => {
      const record = current.current;
      if (record.busy) return;
      const from = direction === "undo" ? "undo" : "redo";
      const to = direction === "undo" ? "redo" : "undo";
      const cmd = record[from][record[from].length - 1];
      if (!cmd) return;
      const revision = record.revision;
      const targetIndex = record[to].length;
      const h = handlersRef.current;
      record.busy = true;
      if (cmd.kind !== "slice") record[from] = record[from].slice(0, -1);
      publish(record);
      try {
        const assertOwner = () => {
          if (record.userId !== undefined && !isCurrentAuthOwner(record.userId))
            throw new Error("操作历史所属账号已切换");
        };
        assertOwner();
        if (cmd.kind === "slice") {
          if (!h.restoreSlice || !record.taskId) throw new Error("切割恢复接口不可用");
          const target = direction === "undo" ? "before" : "after";
          if (!cmd.pendingRestore || cmd.pendingRestore.target !== target) {
            cmd.pendingRestore = {
              target,
              idempotencyKey: randomId().replace(/-/g, ""),
            };
          }
          // Persist the retry key before making the request, including across reloads.
          publish(record);
          const result = await h.restoreSlice(record.taskId, cmd.operationId, {
            target,
            expected_versions: cmd.resultVersions,
            idempotency_key: cmd.pendingRestore.idempotencyKey,
          });
          cmd.resultVersions = result.result_versions;
          cmd.restoreExpiresAt = result.restore_expires_at;
          delete cmd.pendingRestore;
          record[from] = record[from].filter((item) => item !== cmd);
        } else if (cmd.kind === "batch") {
          const ordered = direction === "undo" ? [...cmd.commands].reverse() : cmd.commands;
          for (const sub of ordered) {
            try {
              assertOwner();
              await applyLeaf(sub, direction, h);
            } catch {
              /* Existing batch best effort behavior. */
            }
          }
        } else {
          await applyLeaf(cmd, direction, h);
        }
        if (cmd.kind !== "slice" || record.revision === revision) {
          record[to] = [...record[to], cmd];
        } else if (direction === "redo") {
          // New edits made during this request stay after the initiating redo.
          record.undo = [
            ...record.undo.slice(0, targetIndex),
            cmd,
            ...record.undo.slice(targetIndex),
          ];
        }
        // A new edit clears redo, even if the preceding undo settles later.
      } catch (error) {
        if (cmd.kind !== "slice" && !record[from].includes(cmd))
          record[from] = [...record[from], cmd];
        if (cmd.kind === "slice" && mounted.current && current.current === record)
          h.onSliceError?.(error);
      } finally {
        record.busy = false;
        publish(record);
      }
    },
    [publish],
  );
  const undo = useCallback(() => execute("undo"), [execute]);
  const redo = useCallback(() => execute("redo"), [execute]);

  return {
    push,
    pushBatch,
    undo,
    redo,
    replaceAnnotationId,
    canUndo: snapshot.undo.length > 0 && !snapshot.busy,
    canRedo: snapshot.redo.length > 0 && !snapshot.busy,
    busy: snapshot.busy,
  };
}
