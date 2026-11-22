import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { choiceDialog } from "@/components/ui/decisionDialog";
import {
  useMaskEditor,
  type UseMaskEditorOptions,
  type UseMaskEditorReturn,
} from "./useMaskEditor";
import type { CocoRle } from "../stage/shared/geometry/maskRle";
import type { MaskOperationSpec } from "../stage/shared/geometry/maskOperations";
import type { MaskInstanceOperationSpec } from "../stage/shared/geometry/maskInstanceOperations";
import type { MaskEditorPhase } from "./canEditMask";

export interface MaskSessionKey {
  taskId: string | undefined;
  frameIndex: number;
  toolKey?: string;
  routeKey?: string;
  selectionKey: string;
  annotationVersion: number | undefined;
}

export type MaskSessionGuardChoice = "save" | "discard" | "continue";

export async function promptMaskLeaveChoice(): Promise<MaskSessionGuardChoice> {
  const key = await choiceDialog({
    title: "Mask 尚未保存",
    description: "是否保存后离开？也可以丢弃稿件，或继续编辑。",
    options: [
      { key: "save", label: "保存后离开" },
      { key: "discard", label: "丢弃并离开", tone: "danger" },
      { key: "continue", label: "继续编辑" },
    ],
  });
  return key === "save" || key === "discard" ? key : "continue";
}

export interface UseMaskEditorSessionOptions extends UseMaskEditorOptions {
  sessionKey: MaskSessionKey;
  onLeaveDirty?: (key: MaskSessionKey, nextKey: MaskSessionKey) => Promise<MaskSessionGuardChoice>;
}

export interface MaskSaveResult {
  ok: boolean;
  retryable: boolean;
  error?: unknown;
}

export interface UseMaskEditorSessionReturn extends UseMaskEditorReturn {
  phase: MaskEditorPhase;
  sessionId: string;
  acceptedSessionId: string | null;
  generation: number;
  lastSaveError: unknown;
  saveInFlight: boolean;
  loadRle: (gen: number, rle: CocoRle) => void;
  loadBlank: (gen: number) => void;
  failLoad: (gen: number, error: unknown) => void;
  markReady: (gen: number) => void;
  save: (commit: () => Promise<MaskSaveResult>) => Promise<MaskSaveResult>;
  recoverFromError: () => void;
  rebaseSession: (nextKey: MaskSessionKey) => number;
}

function serializeKey(key: MaskSessionKey): string {
  return `${key.taskId ?? "?"}|f${key.frameIndex}|t${key.toolKey ?? "?"}|r${key.routeKey ?? "?"}|s${key.selectionKey}|v${key.annotationVersion ?? "?"}`;
}

export function useMaskEditorSession({
  sessionKey,
  onLeaveDirty,
  ...editorOpts
}: UseMaskEditorSessionOptions): UseMaskEditorSessionReturn {
  const editor = useMaskEditor(editorOpts);
  const editorBeginBlank = editor.beginBlank;
  const editorCancel = editor.cancel;
  const editorInitFromPolygon = editor.initFromPolygon;
  const editorInitFromRle = editor.initFromRle;
  const editorMaterializeFromRle = editor.materializeFromRle;
  const editorRunOperation = editor.runOperation;
  const editorRunInstanceOperation = editor.runInstanceOperation;
  const [phase, setPhase] = useState<MaskEditorPhase>("idle");
  const [generation, setGeneration] = useState(0);
  const [lastSaveError, setLastSaveError] = useState<unknown>(undefined);
  const [saveInFlight, setSaveInFlight] = useState(false);
  const [acceptedSessionId, setAcceptedSessionId] = useState<string | null>(null);

  const sessionId = useMemo(() => serializeKey(sessionKey), [sessionKey]);
  const generationRef = useRef(0);
  const savePromiseRef = useRef<Promise<MaskSaveResult> | null>(null);
  const transitionTokenRef = useRef(0);
  const lastKeyRef = useRef<string | null>(null);
  const phaseRef = useRef<MaskEditorPhase>(phase);
  const updatePhase = useCallback((next: MaskEditorPhase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);
  const onLeaveDirtyRef = useRef(onLeaveDirty);
  useEffect(() => {
    onLeaveDirtyRef.current = onLeaveDirty;
  }, [onLeaveDirty]);
  const editorActiveRef = useRef(editor.active);
  editorActiveRef.current = editor.active;
  const hasPendingDraft =
    editor.dirty || editor.operationPreview !== null || editor.instanceOperationPreview !== null;
  const editorDirtyRef = useRef(hasPendingDraft);
  editorDirtyRef.current = hasPendingDraft;
  const requestedKeyRef = useRef(sessionKey);
  requestedKeyRef.current = sessionKey;
  const requestedSessionIdRef = useRef(sessionId);
  requestedSessionIdRef.current = sessionId;
  const acceptedKeyRef = useRef<MaskSessionKey | null>(null);

  const acceptTransition = useCallback(
    (serialized: string, key: MaskSessionKey) => {
      lastKeyRef.current = serialized;
      setAcceptedSessionId(serialized);
      acceptedKeyRef.current = key;
      generationRef.current += 1;
      setGeneration(generationRef.current);
      setLastSaveError(undefined);
      setSaveInFlight(false);
      savePromiseRef.current = null;
      updatePhase("loading");
    },
    [updatePhase],
  );

  useEffect(() => {
    const serialized = sessionId;
    if (lastKeyRef.current === serialized) return;
    const nextKey = requestedKeyRef.current;
    if (lastKeyRef.current === null) {
      acceptTransition(serialized, nextKey);
      return;
    }
    const previousPhase = phaseRef.current;
    const needsGuard =
      editorActiveRef.current &&
      (editorDirtyRef.current || previousPhase === "saving" || previousPhase === "error");
    if (!needsGuard) {
      acceptTransition(serialized, nextKey);
      return;
    }

    const token = ++transitionTokenRef.current;
    const previousKey = acceptedKeyRef.current ?? nextKey;
    const guard = onLeaveDirtyRef.current;
    void (guard ? guard(previousKey, nextKey) : Promise.resolve("discard" as const))
      .catch(() => "continue" as const)
      .then((choice) => {
        if (token !== transitionTokenRef.current) return;
        if (requestedSessionIdRef.current !== serialized) return;
        if (choice === "continue") return;
        if (choice === "discard") editorCancel();
        acceptTransition(serialized, requestedKeyRef.current);
      });
  }, [acceptTransition, editorCancel, sessionId]);

  const loadRle = useCallback(
    (gen: number, rle: CocoRle) => {
      if (gen !== generationRef.current) return;
      editorInitFromRle(rle);
      updatePhase("ready");
    },
    [editorInitFromRle, updatePhase],
  );

  const loadBlank = useCallback(
    (gen: number) => {
      if (gen !== generationRef.current) return;
      editorBeginBlank();
      updatePhase("ready");
    },
    [editorBeginBlank, updatePhase],
  );

  const failLoad = useCallback(
    (gen: number, error: unknown) => {
      if (gen !== generationRef.current) return;
      setLastSaveError(error);
      updatePhase("error");
    },
    [updatePhase],
  );

  const markReady = useCallback(
    (gen: number) => {
      if (gen !== generationRef.current || !editorActiveRef.current) return;
      updatePhase(editorDirtyRef.current ? "dirty" : "ready");
    },
    [updatePhase],
  );

  useEffect(() => {
    const cur = phaseRef.current;
    if (cur === "idle" || cur === "loading" || cur === "saving" || cur === "error") return;
    updatePhase(hasPendingDraft ? "dirty" : "ready");
  }, [hasPendingDraft, updatePhase]);

  const beginBlank = useCallback(() => {
    editorBeginBlank();
    updatePhase("ready");
  }, [editorBeginBlank, updatePhase]);

  const initFromPolygon = useCallback(
    (points: ReadonlyArray<readonly [number, number]>) => {
      editorInitFromPolygon(points);
      updatePhase("ready");
    },
    [editorInitFromPolygon, updatePhase],
  );

  const initFromRle = useCallback(
    (rle: CocoRle) => {
      editorInitFromRle(rle);
      updatePhase("ready");
    },
    [editorInitFromRle, updatePhase],
  );

  const materializeFromRle = useCallback(
    (rle: CocoRle) => {
      editorMaterializeFromRle(rle);
      updatePhase("dirty");
    },
    [editorMaterializeFromRle, updatePhase],
  );

  const runOperation = useCallback(
    (name: string, operation: MaskOperationSpec) =>
      editorRunOperation(name, operation, {
        sessionId: requestedSessionIdRef.current,
        generation: generationRef.current,
      }),
    [editorRunOperation],
  );

  const runInstanceOperation = useCallback(
    (name: string, operation: MaskInstanceOperationSpec) =>
      editorRunInstanceOperation(name, operation, {
        sessionId: requestedSessionIdRef.current,
        generation: generationRef.current,
      }),
    [editorRunInstanceOperation],
  );

  const cancel = useCallback(() => {
    transitionTokenRef.current += 1;
    editorCancel();
    setLastSaveError(undefined);
    setSaveInFlight(false);
    savePromiseRef.current = null;
    updatePhase("idle");
  }, [editorCancel, updatePhase]);

  const save = useCallback(
    (commit: () => Promise<MaskSaveResult>): Promise<MaskSaveResult> => {
      if (savePromiseRef.current) return savePromiseRef.current;
      updatePhase("saving");
      setSaveInFlight(true);
      const gen = generationRef.current;
      let commitPromise: Promise<MaskSaveResult>;
      try {
        commitPromise = commit();
      } catch (error: unknown) {
        commitPromise = Promise.reject(error);
      }
      const promise = commitPromise
        .then((result) => {
          if (gen !== generationRef.current) {
            if (savePromiseRef.current === promise) savePromiseRef.current = null;
            return result;
          }
          if (savePromiseRef.current === promise) savePromiseRef.current = null;
          setSaveInFlight(false);
          if (result.ok) {
            setLastSaveError(undefined);
            updatePhase(editorActiveRef.current ? "ready" : "idle");
          } else {
            setLastSaveError(result.error);
            updatePhase("error");
          }
          return result;
        })
        .catch((error: unknown) => {
          if (gen !== generationRef.current) {
            if (savePromiseRef.current === promise) savePromiseRef.current = null;
            throw error;
          }
          if (savePromiseRef.current === promise) savePromiseRef.current = null;
          setSaveInFlight(false);
          setLastSaveError(error);
          updatePhase("error");
          throw error;
        });
      savePromiseRef.current = promise;
      return promise;
    },
    [updatePhase],
  );

  const recoverFromError = useCallback(() => {
    setLastSaveError(undefined);
    updatePhase(hasPendingDraft ? "dirty" : "ready");
  }, [hasPendingDraft, updatePhase]);

  const rebaseSession = useCallback(
    (nextKey: MaskSessionKey) => {
      const serialized = serializeKey(nextKey);
      transitionTokenRef.current += 1;
      if (lastKeyRef.current !== serialized) {
        acceptTransition(serialized, nextKey);
      }
      return generationRef.current;
    },
    [acceptTransition],
  );

  const publicPhase: MaskEditorPhase = editor.commitInFlight ? "saving" : phase;

  return {
    ...editor,
    beginBlank,
    initFromPolygon,
    initFromRle,
    materializeFromRle,
    runOperation,
    runInstanceOperation,
    cancel,
    phase: publicPhase,
    sessionId,
    acceptedSessionId,
    generation,
    lastSaveError,
    saveInFlight,
    loadRle,
    loadBlank,
    failLoad,
    markReady,
    save,
    recoverFromError,
    rebaseSession,
  };
}
