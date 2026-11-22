import { useCallback, useEffect, useRef, useState, type MutableRefObject } from "react";
import type { QueryClient } from "@tanstack/react-query";

export interface ConflictResolution {
  conflictOpen: boolean;
  setConflictOpen: (open: boolean) => void;
  handleConflictReload: () => void;
  handleConflictOverwrite: () => void;
}

export function useConflictResolution(
  conflictCbRef: MutableRefObject<(annotationId: string, version: number) => void>,
  queryClient: QueryClient,
  taskId: string | undefined,
): ConflictResolution {
  const conflictIdRef = useRef<string>("");
  const [conflictOpen, setConflictOpen] = useState(false);
  const handleConflict = useCallback((annotationId: string, _currentVersion: number) => {
    conflictIdRef.current = annotationId;
    setConflictOpen(true);
  }, []);
  useEffect(() => {
    conflictCbRef.current = handleConflict;
  }, [handleConflict, conflictCbRef]);

  const handleConflictReload = useCallback(() => {
    setConflictOpen(false);
    queryClient.invalidateQueries({ queryKey: ["annotations", taskId] });
  }, [queryClient, taskId]);

  const handleConflictOverwrite = useCallback(() => {
    setConflictOpen(false);
  }, []);

  return { conflictOpen, setConflictOpen, handleConflictReload, handleConflictOverwrite };
}
