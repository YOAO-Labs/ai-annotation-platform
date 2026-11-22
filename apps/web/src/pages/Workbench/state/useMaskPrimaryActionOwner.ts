import { useCallback, useEffect, useRef, useState } from "react";
import { resolveMaskPrimaryActions, type MaskPrimaryActionsInput } from "./maskPrimaryActions";

interface MaskPrimaryActionOwnerOptions {
  owner: object;
  state: MaskPrimaryActionsInput;
  busyRef: { current: boolean };
  onSave: () => Promise<boolean>;
  onCommitInstances: () => Promise<boolean>;
  onApplyRegion: () => boolean;
  onCancelPreview: () => void;
  onRecoverSession: () => void;
  onRefreshInstances: () => Promise<void>;
  onExit: () => Promise<void>;
  onError: (error: unknown) => void;
}

export function useMaskPrimaryActionOwner(options: MaskPrimaryActionOwnerOptions) {
  const [pending, setPending] = useState(false);
  const [emptyConfirmation, setEmptyConfirmation] = useState<{
    owner: object;
    previewId: number;
    revision: number;
  } | null>(null);
  const latest = useRef(options);
  latest.current = options;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const actions = resolveMaskPrimaryActions({ ...options.state, savePending: pending });
  const runPrimary = useCallback(async (): Promise<boolean> => {
    const current = latest.current;
    if (!mounted.current || current.busyRef.current) return false;
    const action = resolveMaskPrimaryActions(current.state).primary;
    if (action.disabled) return false;
    if (action.kind === "apply_region" && current.state.operationPreview?.report.afterArea === 0) {
      setEmptyConfirmation({
        owner: current.owner,
        previewId: current.state.operationPreview.id,
        revision: current.state.revision,
      });
      return false;
    }
    current.busyRef.current = true;
    setPending(true);
    try {
      switch (action.kind) {
        case "save":
          return await current.onSave();
        case "commit_instances":
        case "retry_instances":
          return await current.onCommitInstances();
        case "apply_region":
          current.onApplyRegion();
          return false;
        case "recover_session":
          current.onRecoverSession();
          return false;
        case "refresh_instances":
          await current.onRefreshInstances();
          return false;
        case "recover_operation":
        case "cancel_preview":
          current.onCancelPreview();
          return false;
        case "none":
          return false;
      }
    } catch (error: unknown) {
      if (mounted.current && latest.current.owner === current.owner) current.onError(error);
      return false;
    } finally {
      current.busyRef.current = false;
      if (mounted.current) setPending(false);
    }
  }, []);

  const runSecondary = useCallback(async () => {
    const current = latest.current;
    if (!mounted.current || current.busyRef.current) return;
    const action = resolveMaskPrimaryActions(current.state).secondary;
    if (action.disabled) return;
    if (action.kind === "cancel_preview") current.onCancelPreview();
    else if (action.kind === "exit") await current.onExit();
  }, []);

  const saveBeforeLeave = useCallback(async () => {
    const current = latest.current;
    const action = resolveMaskPrimaryActions(current.state).primary;
    if (!["save", "commit_instances", "retry_instances"].includes(action.kind)) return false;
    return runPrimary();
  }, [runPrimary]);

  const preview = options.state.operationPreview;
  const emptyConfirmationOpen = !!(
    emptyConfirmation &&
    emptyConfirmation.owner === options.owner &&
    preview?.id === emptyConfirmation.previewId &&
    options.state.revision === emptyConfirmation.revision &&
    actions.primary.kind === "apply_region" &&
    !actions.primary.disabled
  );
  const confirmEmptyRegion = useCallback(() => {
    const current = latest.current;
    const currentPreview = current.state.operationPreview;
    const action = resolveMaskPrimaryActions(current.state).primary;
    if (
      !current.busyRef.current &&
      emptyConfirmation?.owner === current.owner &&
      currentPreview?.id === emptyConfirmation.previewId &&
      current.state.revision === emptyConfirmation.revision &&
      action.kind === "apply_region" &&
      !action.disabled
    ) {
      current.onApplyRegion();
    }
    setEmptyConfirmation(null);
  }, [emptyConfirmation]);

  return {
    actions,
    runPrimary,
    runSecondary,
    saveBeforeLeave,
    emptyConfirmationOpen,
    confirmEmptyRegion,
    closeEmptyConfirmation: () => setEmptyConfirmation(null),
  };
}
