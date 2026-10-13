/**
 * Batch-line AI backend selection for the workbench ("运行当前题 AI" /
 * batch preannotation entry points).
 *
 * Ownership rule: the workbench is a long-lived session. Once the user picks
 * a batch backend manually in the AI panel, that choice must not be silently
 * reset by external changes (another tab promoting a different default
 * backend, or list-order changes). Switching project resets the manual flag
 * and re-initializes from that project's default (ml_backend_id, falling
 * back to the first available backend); within a project the default is only
 * followed while the user has not picked manually.
 *
 * The interactive line (point/bbox/exemplar tools) routes through
 * useBackendRouting/useInteractiveBackendPref instead.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { MLBackendResponse } from "@/types";

export interface UseBatchBackendSelectionParams {
  projectId: string | null | undefined;
  /** 项目默认后端 (currentProject.ml_backend_id)。 */
  projectDefaultBackendId: string | null | undefined;
  backends: MLBackendResponse[];
}

export function useBatchBackendSelection({
  projectId,
  projectDefaultBackendId,
  backends,
}: UseBatchBackendSelectionParams) {
  const firstBackendId = backends[0]?.id ?? null;
  const [batchBackendId, setBatchBackendId] = useState<string | null>(null);
  const batchManuallyPickedRef = useRef(false);
  const batchProjectRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (batchProjectRef.current !== projectId) {
      batchProjectRef.current = projectId;
      batchManuallyPickedRef.current = false;
      setBatchBackendId(projectDefaultBackendId ?? firstBackendId);
      return;
    }
    if (batchManuallyPickedRef.current) return;
    setBatchBackendId(projectDefaultBackendId ?? firstBackendId);
  }, [projectId, projectDefaultBackendId, firstBackendId]);
  const selectBatchBackend = useCallback((id: string | null) => {
    batchManuallyPickedRef.current = true;
    setBatchBackendId(id);
  }, []);
  const selectedBackend = backends.find((b) => b.id === batchBackendId) ?? null;
  return { batchBackendId, selectBatchBackend, selectedBackend };
}
