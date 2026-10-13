// v0.23.5 · WS-B · 图片 / 视频统一的 mask 编辑会话状态机。
//
// 在 useMaskEditor (buffer + undo/redo + 模式) 之上叠加 ADR-0052 D7 冻结的会话语义:
//
//   idle → loading → ready → dirty → saving → error
//
// - sessionId = hash(taskId, frameIndex, selectionKey, annotationVersion) + 单调 generation;
//   过期请求 (旧 GET / 404 / mutation 回包 generation 不匹配) 不得回写 Buffer。
// - loading / saving 禁止 pointer 写入 (canEditMask 据此判定)。
// - 保存走单飞 Promise: 重复 Enter / 双击只触发一次 mutation; 成功才清 Buffer,
//   失败保留 history / label / refine lineage, 暴露 retry。
// - 离开 dirty session (切 task / frame / tool / route) 触发 guard 回调, 由调用方
//   决定保存 / 丢弃 / 继续编辑。
//
// 设计取舍: 不破坏 useMaskEditor 现有签名 (被图片 / 视频共用, 大量调用点), 而是在外层
// 包一层 session; 调用方按需迁移到 session API。本版先让视频侧 (迟到 GET 覆盖主战场)
// 接入 session 的 generation 隔离与单飞保存; 图片侧逐步跟进。

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

/** 会话键输入: 标识一段唯一的编辑上下文。 */
export interface MaskSessionKey {
  taskId: string | undefined;
  frameIndex: number;
  /** 当前图片/视频工具；切工具也属于离开编辑上下文。 */
  toolKey?: string;
  /** 当前路由；用于工作台内部路由切换的 dirty guard。 */
  routeKey?: string;
  /** 选中对象标识 (annotation id 或 "blank"); 不同对象互不干扰。 */
  selectionKey: string;
  /** annotation version (服务端乐观锁), 刷新后变化 → 新 session。 */
  annotationVersion: number | undefined;
}

export type MaskSessionGuardChoice = "save" | "discard" | "continue";

/**
 * 应用内三选一决策对话框 (decision-dialog 计划 Group C): 一次呈现保存 / 丢弃 / 继续编辑,
 * 取代原先两段连续 window.confirm; 取消 / Esc / 点遮罩 = 继续编辑。
 */
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
  /** 当前会话键; 变化 → 开新 session (自增 generation)。 */
  sessionKey: MaskSessionKey;
  /**
   * 离开 dirty session 时的 guard。返回 Promise<choice>; 调用方据此 save/discard/continue。
   * 未提供时默认 "discard" (保持旧行为, 但暴露 dirty 让调用方在迁移期自行接管)。
   */
  onLeaveDirty?: (key: MaskSessionKey, nextKey: MaskSessionKey) => Promise<MaskSessionGuardChoice>;
}

export interface MaskSaveResult {
  ok: boolean;
  /** 409 / 网络错误等可重试错误; 调用方决定是否暴露 retry。 */
  retryable: boolean;
  error?: unknown;
}

export interface UseMaskEditorSessionReturn extends UseMaskEditorReturn {
  /** 当前会话相位。 */
  phase: MaskEditorPhase;
  /** 稳定会话 id (key 的序列化)。 */
  sessionId: string;
  /** 已通过 dirty guard、可以接收加载回包的会话 id。 */
  acceptedSessionId: string | null;
  /** 单调递增代次; 每次 sessionKey 变化自增。 */
  generation: number;
  /** 上一次保存的错误 (error 相位时非空)。 */
  lastSaveError: unknown;
  /** 是否有进行中的保存 (单飞)。 */
  saveInFlight: boolean;
  /**
   * 按 generation 隔离的 RLE 加载: 仅当 generation 仍匹配当前 session 才 initFromRle。
   * 迟到 GET / 404 回包对旧 generation 调用 → 静默丢弃, 不覆盖 Buffer (A1)。
   */
  loadRle: (gen: number, rle: CocoRle) => void;
  /** 按 generation 隔离的 blank 加载 (404 → beginBlank 场景)。 */
  loadBlank: (gen: number) => void;
  /** 当前 generation 的加载失败；旧请求失败不得取消新会话。 */
  failLoad: (gen: number, error: unknown) => void;
  /** 已由精修入口初始化 Buffer 时，把当前 generation 从 loading 推进到 ready。 */
  markReady: (gen: number) => void;
  /**
   * 单飞保存: 同一 session 内重复调用合并为一次 Promise。
   * 成功 → phase=ready (或 idle, 由调用方 cancel); 失败 → phase=error, 保留 Buffer, 可 retry。
   */
  save: (commit: () => Promise<MaskSaveResult>) => Promise<MaskSaveResult>;
  /** 从 error 恢复到 dirty (用户继续编辑)。 */
  recoverFromError: () => void;
  /** 外部刷新已确认新版本时同步接纳会话键，避免再启动一轮常规加载。 */
  rebaseSession: (nextKey: MaskSessionKey) => number;
}

function serializeKey(key: MaskSessionKey): string {
  return `${key.taskId ?? "?"}|f${key.frameIndex}|t${key.toolKey ?? "?"}|r${key.routeKey ?? "?"}|s${key.selectionKey}|v${key.annotationVersion ?? "?"}`;
}

/**
 * mask 编辑会话状态机 hook。包裹 useMaskEditor, 在其上叠加 phase / generation / 单飞保存。
 */
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
  // 最近一次 sessionKey 序列化值, 用于检测变化并自增 generation。
  const lastKeyRef = useRef<string | null>(null);
  // refs 让 sessionKey-change effect 只依赖 sessionId, 避免每次 render 重跑。
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

  // sessionKey 变化 → 自增 generation, 进入 loading (调用方随后 loadRle/loadBlank)。
  // 若旧 session 处于 dirty/saving/error, 触发 onLeaveDirty guard。
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
        // save 表示 guard 已完成保存；discard 才由 session 主动清 Buffer。
        if (choice === "discard") editorCancel();
        acceptTransition(serialized, requestedKeyRef.current);
      });
  }, [acceptTransition, editorCancel, sessionId]);

  const loadRle = useCallback(
    (gen: number, rle: CocoRle) => {
      if (gen !== generationRef.current) return; // 迟到回包, 丢弃
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

  // editor 内部 dirty 变化 → 同步 phase (ready ↔ dirty)。
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
      // 单飞: 同 session 内重复 Enter / 双击合并为同一 Promise。
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
          // 迟到回包 (session 已切换): 不改当前 phase, 只清自己的引用。
          if (gen !== generationRef.current) {
            if (savePromiseRef.current === promise) savePromiseRef.current = null;
            return result;
          }
          if (savePromiseRef.current === promise) savePromiseRef.current = null;
          setSaveInFlight(false);
          if (result.ok) {
            setLastSaveError(undefined);
            // 成功: 调用方在 commit 内已决定是否 cancel; 这里回到 ready (buffer 仍可能在,
            // 但 dirty 已被 editor 在 commit 成功路径清掉)。若 editor 已 cancel → active=false,
            // 调用方应再切 idle; 简化: 只要还 active 就回 ready。
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
    // 回到 dirty: buffer/history 仍在, 用户继续编辑或手动 retry。
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
