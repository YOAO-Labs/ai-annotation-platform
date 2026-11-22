import { randomId } from "@/utils/id";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import type { AnnotationResponse, VideoTrackKeyframe, VideoTrackMaskKeyframe } from "@/types";
import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import type {
  AnnotationSliceResponse,
  AnnotationSliceRestoreRequest,
} from "@/api/annotationSlices";
import type {
  AcceptanceHistoryRestoreRequest,
  AcceptanceHistoryRestoreResponse,
} from "@/api/acceptanceHistory";

export interface SliceCommand {
  kind: "slice";
  operationId: string;
  resultVersions: Record<string, number>;
  restoreExpiresAt: string;
  pendingRestore?: { target: "before" | "after"; idempotencyKey: string };
}

export interface AcceptanceCommand {
  kind: "acceptance";
  acceptanceKey: string;
  videoSegmentId: string | null;
  resultVersions: Record<string, number>;
  restoreExpiresAt?: string;
  pendingRestore?: AcceptanceHistoryRestoreRequest;
}

export interface VideoMaskFrameState {
  keyframe: VideoTrackMaskKeyframe | null;
  manualOutside: boolean;
}

export type Command =
  | SliceCommand
  | AcceptanceCommand
  | { kind: "create"; annotationId: string; payload: AnnotationPayload }
  | { kind: "delete"; annotation: AnnotationResponse; restoreRequestId?: string }
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
  | { kind: "batch"; commands: LeafCommand[]; batchId?: string };

export type LeafCommand = Exclude<Command, { kind: "batch" | "slice" | "acceptance" }>;

function annotationId(command: LeafCommand): string | undefined {
  if (command.kind === "delete") return command.annotation.id;
  if (command.kind !== "acceptPrediction") return command.annotationId;
}

function remapLeaf(command: LeafCommand, previous: string, next: string) {
  if (command.kind === "acceptPrediction") {
    command.createdAnnotationIds = command.createdAnnotationIds.map((id) =>
      id === previous ? next : id,
    );
  } else if (annotationId(command) === previous) {
    if (command.kind === "delete") command.annotation = { ...command.annotation, id: next };
    else command.annotationId = next;
  }
}

function remapHistory(record: TaskHistory, previous: string, next: string) {
  for (const stack of [record.undo, record.redo]) {
    for (const command of stack) {
      if (command.kind === "batch") {
        for (const leaf of command.commands) remapLeaf(leaf, previous, next);
      } else if (command.kind !== "slice" && command.kind !== "acceptance") {
        remapLeaf(command, previous, next);
      }
    }
  }
}

export async function applyLeaf(cmd: LeafCommand, direction: "undo" | "redo", h: HistoryHandlers) {
  if (cmd.kind === "create") {
    if (direction === "undo") {
      if (cmd.annotationId.startsWith("tmp_") && h.removeLocalCreate) {
        const syncedId = await h.removeLocalCreate(cmd.annotationId);
        if (syncedId) cmd.annotationId = syncedId;
      } else {
        await h.deleteAnnotation(cmd.annotationId);
      }
      cmd.payload = { ...cmd.payload, client_request_id: randomId() };
    } else {
      const fresh = await h.createAnnotation(cmd.payload);
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
        ...(cmd.annotation.video_segment_id
          ? { video_segment_id: cmd.annotation.video_segment_id }
          : {}),
        ...(cmd.restoreRequestId ? { client_request_id: cmd.restoreRequestId } : {}),
        attributes: cmd.annotation.attributes ?? undefined,
      });
      cmd.annotation = { ...cmd.annotation, id: restored.id };
    } else {
      await h.deleteAnnotation(cmd.annotation.id);
      delete cmd.restoreRequestId;
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
    throw new Error("旧的采纳历史缺少恢复凭据，无法安全撤销或重做");
  }
}

export interface HistoryHandlers {
  restoreSlice?: (
    taskId: string,
    operationId: string,
    payload: AnnotationSliceRestoreRequest,
  ) => Promise<AnnotationSliceResponse>;
  restoreAcceptance?: (
    taskId: string,
    acceptanceKey: string,
    payload: AcceptanceHistoryRestoreRequest,
  ) => Promise<AcceptanceHistoryRestoreResponse>;
  onAcceptanceError?: (error: unknown) => void;
  onSliceError?: (error: unknown) => void;
  onReplayError?: (message: string, error: unknown) => void;
  resolveAnnotationId?: (annotationId: string) => Promise<string | undefined>;
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
  removeLocalCreate?: (annotationId: string) => Promise<string | void> | void;
}

const HIST_TTL_MS = 5 * 60 * 1000;
const HIST_KEY_PREFIX = "wb:hist:";

function historyKey(
  taskId: string | undefined,
  userId?: string,
  segmentId?: string | null,
): string {
  const taskKey = `${HIST_KEY_PREFIX}${userId === undefined ? "" : `${userId}:`}${taskId ?? ""}`;
  return segmentId ? `${taskKey}:segment:${segmentId}` : taskKey;
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
  segmentId?: string | null,
): {
  undo: Command[];
  redo: Command[];
} | null {
  if (!taskId) return null;
  const ss = readSessionStorage();
  if (!ss) return null;
  try {
    const key = historyKey(taskId, userId, segmentId);
    const raw = ss.getItem(key);
    if (!raw) return null;
    const data = JSON.parse(raw) as PersistedHistory;
    if (typeof data.ts !== "number") return null;
    if (Date.now() - data.ts > HIST_TTL_MS) {
      ss.removeItem(key);
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
  segmentId?: string | null,
): void {
  if (!taskId) return;
  const ss = readSessionStorage();
  if (!ss) return;
  try {
    const key = historyKey(taskId, userId, segmentId);
    if (undo.length === 0 && redo.length === 0) {
      ss.removeItem(key);
      return;
    }
    const payload: PersistedHistory = { undo, redo, ts: Date.now() };
    ss.setItem(key, JSON.stringify(payload));
  } catch {
    // sessionStorage 写失败（quota / private mode）静默忽略；history 仍在内存可用。
  }
}

interface TaskHistory {
  taskId: string | undefined;
  userId?: string;
  segmentId?: string | null;
  undo: Command[];
  redo: Command[];
  busy: boolean;
  revision: number;
}

export function useAnnotationHistory(
  taskId: string | undefined,
  handlers: HistoryHandlers,
  userId?: string,
  segmentId?: string | null,
) {
  const histories = useRef(new Map<string | undefined, TaskHistory>());
  const getHistory = useCallback(
    (owner: string | undefined, segment = segmentId): TaskHistory => {
      const key = historyKey(owner, userId, segment);
      const existing = histories.current.get(key);
      if (existing) return existing;
      const restored = loadHistoryFromSession(owner, userId, segment);
      const created = {
        taskId: owner,
        userId,
        segmentId: segment,
        undo: restored?.undo ?? [],
        redo: restored?.redo ?? [],
        busy: false,
        revision: 0,
      };
      histories.current.set(key, created);
      return created;
    },
    [segmentId, userId],
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
    if (
      (previous.taskId !== taskId ||
        previous.userId !== userId ||
        previous.segmentId !== segmentId) &&
      !previous.busy
    )
      histories.current.delete(historyKey(previous.taskId, previous.userId, previous.segmentId));
    current.current = getHistory(taskId);
    setSnapshot({ ...current.current });
  }, [taskId, getHistory, userId, segmentId]);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const publish = useCallback((record: TaskHistory) => {
    saveHistoryToSession(record.taskId, record.undo, record.redo, record.userId, record.segmentId);
    if (mounted.current && current.current === record) setSnapshot({ ...record });
  }, []);

  const push = useCallback(
    (cmd: Command, owner = taskId) => {
      const record = getHistory(owner);
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
    [getHistory, publish, taskId],
  );

  const pushBatch = useCallback(
    (commands: LeafCommand[]) => {
      if (!commands.length) return;
      push(commands.length === 1 ? commands[0] : { kind: "batch", commands });
    },
    [push],
  );

  const replaceAnnotationId = useCallback(
    (tmpId: string, realId: string, owner = taskId, segment = segmentId) => {
      const record = getHistory(owner, segment);
      remapHistory(record, tmpId, realId);
      publish(record);
    },
    [getHistory, publish, segmentId, taskId],
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
      let insertionIndex = targetIndex;
      let completed = 0;
      const total = cmd.kind === "batch" ? cmd.commands.length : 1;
      const sessionId = useAuthStore.getState().sessionId;
      const ownsSession = () =>
        record.userId === undefined ||
        (isCurrentAuthOwner(record.userId) && useAuthStore.getState().sessionId === sessionId);
      const assertScope = () => {
        if (!ownsSession() || !mounted.current || current.current !== record)
          throw new Error("操作历史所属账号、任务或片段已切换");
      };
      const apply = async (leaf: LeafCommand) => {
        let previous = annotationId(leaf);
        if (
          previous?.startsWith("tmp_") &&
          h.resolveAnnotationId &&
          !(leaf.kind === "create" && direction === "undo" && h.removeLocalCreate)
        ) {
          const realId = await h.resolveAnnotationId(previous);
          assertScope();
          if (realId) {
            remapHistory(record, previous, realId);
            publish(record);
          }
          previous = annotationId(leaf);
        }
        if (leaf.kind === "delete" && direction === "undo" && !leaf.restoreRequestId) {
          leaf.restoreRequestId = randomId();
          publish(record);
        }
        if (leaf.kind === "create" && direction === "redo" && !leaf.payload.client_request_id) {
          leaf.payload = { ...leaf.payload, client_request_id: randomId() };
          publish(record);
        }
        await applyLeaf(leaf, direction, h);
        const next = annotationId(leaf);
        if (previous && next && previous !== next) remapHistory(record, previous, next);
      };
      const insert = (item: Command) => {
        record[to] = [
          ...record[to].slice(0, insertionIndex),
          item,
          ...record[to].slice(insertionIndex),
        ];
        insertionIndex++;
      };
      record.busy = true;
      publish(record);
      try {
        assertScope();
        if (cmd.kind === "slice") {
          if (!h.restoreSlice || !record.taskId) throw new Error("切割恢复接口不可用");
          const target = direction === "undo" ? "before" : "after";
          if (!cmd.pendingRestore || cmd.pendingRestore.target !== target) {
            cmd.pendingRestore = {
              target,
              idempotencyKey: randomId().replace(/-/g, ""),
            };
          }
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
        } else if (cmd.kind === "acceptance") {
          if (!h.restoreAcceptance || !record.taskId) throw new Error("采纳恢复接口不可用");
          const target = direction === "undo" ? "before" : "after";
          if (!cmd.pendingRestore) {
            cmd.pendingRestore = {
              target,
              expected_versions: { ...cmd.resultVersions },
              idempotency_key: randomId(),
              video_segment_id: cmd.videoSegmentId,
            };
          }
          if (cmd.pendingRestore.target !== target) throw new Error("采纳历史恢复状态不一致");
          publish(record);
          const result = await h.restoreAcceptance(
            record.taskId,
            cmd.acceptanceKey,
            cmd.pendingRestore,
          );
          cmd.resultVersions = result.result_versions;
          cmd.restoreExpiresAt = result.restore_expires_at;
          delete cmd.pendingRestore;
          record[from] = record[from].filter((item) => item !== cmd);
        } else if (cmd.kind === "batch") {
          if (cmd.commands.some((sub) => sub.kind === "acceptPrediction"))
            throw new Error("旧的采纳历史缺少恢复凭据，无法安全撤销或重做");
          cmd.batchId ??= randomId();
          while (cmd.commands.length > 0) {
            assertScope();
            if (record.revision !== revision) throw new Error("编辑内容已变化，剩余历史未执行");
            const index = direction === "undo" ? cmd.commands.length - 1 : 0;
            const leaf = cmd.commands[index];
            await apply(leaf);
            cmd.commands.splice(index, 1);
            completed++;
            if (!cmd.commands.length) record[from] = record[from].filter((item) => item !== cmd);
            if (direction === "redo" || record.revision === revision) {
              const counterpart = record[to][insertionIndex - 1];
              if (counterpart?.kind === "batch" && counterpart.batchId === cmd.batchId) {
                if (direction === "undo") counterpart.commands.unshift(leaf);
                else counterpart.commands.push(leaf);
              } else {
                insert({ kind: "batch", commands: [leaf], batchId: cmd.batchId });
              }
            }
            publish(record);
          }
          return;
        } else {
          await apply(cmd);
          record[from] = record[from].filter((item) => item !== cmd);
        }
        if (direction === "redo" || record.revision === revision) insert(cmd);
      } catch (error) {
        if (mounted.current && current.current === record && ownsSession()) {
          if (cmd.kind === "slice") h.onSliceError?.(error);
          else if (
            cmd.kind === "acceptance" ||
            cmd.kind === "acceptPrediction" ||
            (cmd.kind === "batch" && cmd.commands.some((sub) => sub.kind === "acceptPrediction"))
          )
            h.onAcceptanceError?.(error);
          else {
            const label = direction === "undo" ? "撤销" : "重做";
            const progress = cmd.kind === "batch" ? `（本次已完成 ${completed}/${total} 项）` : "";
            h.onReplayError?.(`${label}未完成${progress}，可重试剩余操作`, error);
          }
        }
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
