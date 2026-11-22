import { useCallback, useEffect, useRef, useState } from "react";
import type { AnnotationResponse } from "@/types";
import type { useUpdateAnnotation } from "@/hooks/useTasks";
import {
  geometryConvention,
  parsePsrForm,
  psrFormToGeometry,
  type PsrField,
} from "./ThreeDWorkbench.helpers";
import type { LidarAxisConvention } from "./geometry/axisConvention";
import type { useThreeDHistory } from "./useThreeDHistory";

interface UsePsrPatchPipelineParams {
  selectedId: string | null;
  selectedAnn: AnnotationResponse | null;
  axisConvention: LidarAxisConvention;
  updateAnnotation: ReturnType<typeof useUpdateAnnotation>;
  history: ReturnType<typeof useThreeDHistory>;
  form?: Record<PsrField, string> | null;
}

export function usePsrPatchPipeline({
  selectedId,
  selectedAnn,
  axisConvention,
  updateAnnotation,
  history,
  form,
}: UsePsrPatchPipelineParams) {
  const patchTimer = useRef<number | null>(null);
  const pendingCommitRef = useRef<(() => void) | null>(null);
  const [hasPendingPatch, setHasPendingPatch] = useState(false);
  const [invalidDraft, setInvalidDraft] = useState(false);

  const flushPatch = useCallback(() => {
    if (patchTimer.current) {
      window.clearTimeout(patchTimer.current);
      patchTimer.current = null;
    }
    const commit = pendingCommitRef.current;
    pendingCommitRef.current = null;
    setHasPendingPatch(false);
    commit?.();
  }, []);

  useEffect(() => {
    setInvalidDraft(false);
    return flushPatch;
  }, [selectedId, flushPatch]);

  const schedulePatch = useCallback(
    (f: Record<PsrField, string>) => {
      if (!selectedId) return;
      const { values: v, valid } = parsePsrForm(f);
      if (patchTimer.current) window.clearTimeout(patchTimer.current);
      patchTimer.current = null;
      pendingCommitRef.current = null;
      setHasPendingPatch(valid);
      setInvalidDraft(!valid);
      if (!valid) return;
      const commit = () => {
        const geometry = psrFormToGeometry(
          v,
          geometryConvention(selectedAnn?.geometry, axisConvention),
        );
        if (selectedAnn?.geometry?.type === "box_3d") {
          void history.commit([
            {
              kind: "update",
              annotationId: selectedId,
              before: { geometry: selectedAnn.geometry },
              after: { geometry },
            },
          ]);
        } else {
          updateAnnotation.mutate({ annotationId: selectedId, payload: { geometry } });
        }
      };
      pendingCommitRef.current = commit;
      patchTimer.current = window.setTimeout(() => {
        patchTimer.current = null;
        pendingCommitRef.current = null;
        setHasPendingPatch(false);
        commit();
      }, 250);
    },
    [selectedId, selectedAnn?.geometry, axisConvention, history.commit, updateAnnotation],
  );

  return {
    schedulePatch,
    hasPendingPatch,
    hasInvalidDraft: form === undefined ? invalidDraft : !!form && !parsePsrForm(form).valid,
    isSaving: updateAnnotation.isPending,
    saveError: updateAnnotation.error,
  };
}
