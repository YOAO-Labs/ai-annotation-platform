import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { aiMasksApi, type AiMaskAcceptResponse, type AiMaskAcceptTarget } from "@/api/aiMasks";
import type { AnnotationResponse } from "@/types";
import { isCurrentAuthOwner } from "@/stores/authStore";
import type { PendingMaskCandidate } from "@/pages/Workbench/state/useInteractiveAI";
import type { AcceptanceCommand } from "@/pages/Workbench/state/useAnnotationHistory";

interface AnnotationHistoryWriter {
  push: (command: AcceptanceCommand) => void;
}

export interface AcceptNativeMaskCandidateInput {
  candidate: PendingMaskCandidate;
  className: string;
  target: AiMaskAcceptTarget;
}

/**
 * Shared image/video native-candidate accept boundary.
 *
 * The same idempotency key is single-flight in the browser. A failed request
 * throws without mutating candidate state; callers consume it only after this
 * function returns a server result.
 */
export function useAcceptNativeMaskCandidate(args: {
  taskId: string | undefined;
  videoSegmentId?: string | null;
  meUserId?: string | null;
  annotationQueryKey?: readonly unknown[];
  queryClient: QueryClient;
  history: AnnotationHistoryWriter;
}) {
  const { taskId, videoSegmentId, meUserId, annotationQueryKey, queryClient, history } = args;
  const owner = useMemo(
    () => ({ taskId, videoSegmentId, meUserId, active: true }),
    [taskId, videoSegmentId, meUserId],
  );
  useLayoutEffect(() => {
    owner.active = true;
    return () => {
      owner.active = false;
    };
  }, [owner]);
  const inFlightRef = useRef(new Set<string>());

  return useCallback(
    async ({
      candidate,
      className,
      target,
    }: AcceptNativeMaskCandidateInput): Promise<AiMaskAcceptResponse | undefined> => {
      const isCurrentOwner = () =>
        owner.active && (!owner.meUserId || isCurrentAuthOwner(owner.meUserId));
      if (!taskId || !isCurrentOwner() || inFlightRef.current.has(candidate.idempotencyKey))
        return undefined;
      inFlightRef.current.add(candidate.idempotencyKey);
      try {
        const accepted = await aiMasksApi.accept(taskId, {
          idempotency_key: candidate.idempotencyKey,
          candidate: {
            candidate: {
              type: "mask",
              value: { rle: candidate.rle, masklabels: [className] },
              score: candidate.score,
              candidate_id: candidate.candidateId,
            },
            candidate_index: candidate.candidateIndex,
            prompt_revision: candidate.promptRevision,
            receipt: candidate.receipt,
          },
          class_name: className,
          video_segment_id: videoSegmentId ?? null,
          target,
          prompt_summary: candidate.promptSummary,
          routing: candidate.routing,
          inference: candidate.inference,
        });
        if (!isCurrentOwner()) return undefined;
        queryClient.setQueryData<AnnotationResponse[]>(
          annotationQueryKey ?? ["annotations", taskId],
          (current) => {
            const existing = current ?? [];
            return existing.some((annotation) => annotation.id === accepted.annotation.id)
              ? existing.map((annotation) =>
                  annotation.id === accepted.annotation.id ? accepted.annotation : annotation,
                )
              : [...existing, accepted.annotation];
          },
        );
        history.push({
          kind: "acceptance",
          acceptanceKey: candidate.idempotencyKey,
          videoSegmentId: videoSegmentId ?? null,
          resultVersions: { [accepted.annotation.id]: accepted.result_version },
        });
        void queryClient.invalidateQueries({ queryKey: ["predictions", taskId] });
        void queryClient.invalidateQueries({ queryKey: ["tasks"] });
        void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
        return accepted;
      } catch (error) {
        if (!isCurrentOwner()) return undefined;
        throw error;
      } finally {
        inFlightRef.current.delete(candidate.idempotencyKey);
      }
    },
    [annotationQueryKey, history, queryClient, taskId, videoSegmentId, owner],
  );
}
