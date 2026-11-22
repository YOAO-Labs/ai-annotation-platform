import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

import type { AnnotationPayload, AnnotationUpdatePayload } from "@/api/tasks";
import type { AnnotationResponse } from "@/types";
import { useToastStore } from "@/components/ui/Toast";
import { isOfflineMutationQueued } from "@/hooks/useTasks";

import {
  applyLeaf,
  useAnnotationHistory,
  type HistoryHandlers,
  type LeafCommand,
} from "../../state/useAnnotationHistory";

interface CreateMutation {
  mutateAsync: (payload: AnnotationPayload) => Promise<AnnotationResponse>;
}

interface DeleteMutation {
  mutateAsync: (annotationId: string) => Promise<unknown>;
}

interface UpdateMutation {
  mutateAsync: (args: {
    annotationId: string;
    payload: AnnotationUpdatePayload;
  }) => Promise<unknown>;
}

export function useThreeDHistory(
  taskId: string | null,
  mutations: {
    createAnnotation: CreateMutation;
    deleteAnnotation: DeleteMutation;
    updateAnnotation: UpdateMutation;
  },
) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const sessionId = useAuthStore((state) => state.sessionId);
  const pushToast = useToastStore((state) => state.push);
  const activeTask = useRef<string | null>(taskId);
  useLayoutEffect(() => {
    activeTask.current = taskId;
    return () => {
      activeTask.current = null;
    };
  }, [taskId]);
  const handlers = useMemo<HistoryHandlers>(
    () => ({
      createAnnotation: (payload) => mutations.createAnnotation.mutateAsync(payload),
      deleteAnnotation: (annotationId) =>
        mutations.deleteAnnotation.mutateAsync(annotationId).catch((error) => {
          if (!isOfflineMutationQueued(error)) throw error;
        }),
      updateAnnotation: (annotationId, payload) =>
        mutations.updateAnnotation.mutateAsync({ annotationId, payload }).catch((error) => {
          if (!isOfflineMutationQueued(error)) throw error;
        }),
      onReplayError: (msg, error) => pushToast({ msg, sub: String(error), kind: "error" }),
    }),
    [mutations.createAnnotation, mutations.deleteAnnotation, mutations.updateAnnotation, pushToast],
  );

  const history = useAnnotationHistory(taskId ?? undefined, handlers, userId);
  const { pushBatch } = history;
  const commit = useCallback(
    async (commands: Extract<LeafCommand, { kind: "update" | "delete" }>[]) => {
      if (!taskId || !commands.length) return;
      const owns = () =>
        (commands.length === 1 || activeTask.current === taskId) &&
        isCurrentAuthOwner(userId) &&
        useAuthStore.getState().sessionId === sessionId;
      const accepted: LeafCommand[] = [];
      let lastError: unknown;
      for (const command of commands) {
        if (!owns()) break;
        try {
          await applyLeaf(command, "redo", handlers);
          accepted.push(command);
        } catch (error) {
          lastError = error;
        }
      }
      pushBatch(accepted);
      if (accepted.length !== commands.length && owns())
        pushToast({
          msg: `仅保存了 ${accepted.length}/${commands.length} 项 3D 修改`,
          sub: String(lastError),
          kind: "error",
        });
    },
    [handlers, pushBatch, pushToast, sessionId, taskId, userId],
  );
  return { ...history, commit };
}
