import { useEffect, useLayoutEffect, useRef } from "react";
import type { CommentCanvasDrawing } from "@/api/comments";
import type { CanvasDraft } from "./useWorkbenchState";
import type { DiscussionDraftStore } from "./useDiscussionDraftStore";
import type { DiscussionOrigin, DiscussionTarget } from "./discussionTypes";
import {
  clearCanvasDraftRecovery,
  readCanvasDraftRecovery,
  writeCanvasDraftRecovery,
} from "./discussionCanvasRecovery";

interface Args {
  taskId: string | undefined;
  projectId?: string | null;
  store?: DiscussionDraftStore | null;
  annotationIds?: readonly string[];
  canvasDraft: CanvasDraft;
  beginCanvasDraft: (
    annotationId: string | null,
    initial?: CommentCanvasDrawing | null,
    origin?: DiscussionOrigin,
  ) => void;
  releaseCanvasDraft?: () => void;
  consumeCanvasResult?: (resultId?: string) => void;
}

function flushActiveDrawing(draft: CanvasDraft, store: DiscussionDraftStore | null | undefined) {
  if (!draft.active || !draft.origin || !store?.isOwned(draft.origin)) return;
  const drawing = { shapes: draft.shapes };
  const active = drawing.shapes.length > 0;
  if (!store.saveDrawing(draft.origin, drawing, { active })) return;
  if (active) writeCanvasDraftRecovery(draft.origin, drawing);
  else clearCanvasDraftRecovery(draft.origin);
}

export function useCanvasDraftPersistence({
  taskId,
  projectId,
  store,
  annotationIds,
  canvasDraft,
  beginCanvasDraft,
  releaseCanvasDraft,
  consumeCanvasResult,
}: Args) {
  const latest = useRef({ canvasDraft, store });
  latest.current = { canvasDraft, store };
  const restoredTaskContext = useRef<string | null>(null);
  const restoredAnnotationContext = useRef<string | null>(null);

  useLayoutEffect(() => {
    const origin = canvasDraft.origin;
    if (!origin) return;
    if (!store?.isOwned(origin)) {
      releaseCanvasDraft?.();
      return;
    }
    if (canvasDraft.active) {
      flushActiveDrawing(canvasDraft, store);
      if (origin.target.taskId !== taskId || origin.target.projectId !== projectId)
        releaseCanvasDraft?.();
    } else if (canvasDraft.pendingResult) {
      if (store.saveDrawing(origin, canvasDraft.pendingResult, { active: false })) {
        clearCanvasDraftRecovery(origin);
      }
      consumeCanvasResult?.(canvasDraft.resultId ?? undefined);
    }
  }, [canvasDraft, store, taskId, projectId, releaseCanvasDraft, consumeCanvasResult]);

  useLayoutEffect(
    () => () => {
      flushActiveDrawing(latest.current.canvasDraft, latest.current.store);
    },
    [],
  );

  useLayoutEffect(() => {
    if (!store || !projectId || !taskId || store.getSnapshot().disposed) return;
    const context = JSON.stringify([store.owner.sessionId, projectId, taskId]);
    if (canvasDraft.active || canvasDraft.pendingResult) return;
    const restoreTask = restoredTaskContext.current !== context;
    const restoreAnnotations =
      annotationIds !== undefined && restoredAnnotationContext.current !== context;
    if (!restoreTask && !restoreAnnotations) return;

    const available = new Set(annotationIds ?? []);
    const memory = Object.values(store.getSnapshot().drafts).filter(
      (draft) =>
        draft.target.projectId === projectId &&
        draft.target.taskId === taskId &&
        ((restoreTask && draft.target.kind === "task") ||
          (restoreAnnotations &&
            annotationIds !== undefined &&
            draft.target.kind === "annotation" &&
            available.has(draft.target.annotationId))) &&
        draft.canvasActive &&
        draft.canvasOrigin &&
        store.isOwned(draft.canvasOrigin) &&
        (draft.canvas_drawing?.shapes?.length ?? 0) > 0,
    );
    const selected = store.getSendTarget(projectId, taskId);
    const saved =
      selected?.kind === "annotation"
        ? memory.find(
            (draft) =>
              draft.target.kind === "annotation" &&
              draft.target.annotationId === selected.annotationId,
          )
        : selected?.kind === "task"
          ? memory.find((draft) => draft.target.kind === "task")
          : memory[memory.length - 1];
    if (saved?.canvasOrigin) {
      restoredTaskContext.current = context;
      restoredAnnotationContext.current = context;
      store.setSendTarget(projectId, taskId, saved.target);
      beginCanvasDraft(
        saved.target.kind === "annotation" ? saved.target.annotationId : null,
        saved.canvas_drawing,
        saved.canvasOrigin,
      );
      return;
    }

    for (const record of readCanvasDraftRecovery({
      userId: store.owner.userId,
      projectId,
      taskId,
    })) {
      if (selected?.kind === "task" && record.targetKind !== "task") continue;
      if (
        selected?.kind === "annotation" &&
        (record.targetKind !== "annotation" || record.annotationId !== selected.annotationId)
      )
        continue;
      if (selected?.kind === "issue") continue;
      if (
        record.targetKind === "annotation" &&
        (annotationIds === undefined || !available.has(record.annotationId ?? ""))
      )
        continue;
      if (record.targetKind === "task" && !restoreTask) continue;
      if (record.targetKind === "annotation" && !restoreAnnotations) continue;
      const target: DiscussionTarget =
        record.targetKind === "annotation"
          ? { kind: "annotation", projectId, taskId, annotationId: record.annotationId! }
          : { kind: "task", projectId, taskId };
      if (store.getDraft(target)) continue;
      const origin = store.makeOrigin(target);
      if (!origin) continue;
      const drawing = { shapes: record.shapes };
      if (!store.saveDrawing(origin, drawing, { active: true })) continue;
      restoredTaskContext.current = context;
      restoredAnnotationContext.current = context;
      store.setSendTarget(projectId, taskId, target);
      beginCanvasDraft(target.kind === "annotation" ? target.annotationId : null, drawing, origin);
      break;
    }
    if (restoreTask) restoredTaskContext.current = context;
    if (restoreAnnotations) restoredAnnotationContext.current = context;
  }, [
    store,
    projectId,
    taskId,
    annotationIds,
    canvasDraft.active,
    canvasDraft.pendingResult,
    beginCanvasDraft,
  ]);

  useEffect(() => {
    if (
      !canvasDraft.active ||
      !canvasDraft.shapes.length ||
      !canvasDraft.origin ||
      !store?.isOwned(canvasDraft.origin)
    )
      return;
    const handler = (event: BeforeUnloadEvent) => {
      flushActiveDrawing(latest.current.canvasDraft, latest.current.store);
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [canvasDraft.active, canvasDraft.shapes.length, canvasDraft.origin, store]);
}
