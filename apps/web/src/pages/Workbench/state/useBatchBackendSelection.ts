import { useCallback, useEffect, useRef, useState } from "react";
import type { MLBackendResponse } from "@/types";

export interface UseBatchBackendSelectionParams {
  projectId: string | null | undefined;
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
