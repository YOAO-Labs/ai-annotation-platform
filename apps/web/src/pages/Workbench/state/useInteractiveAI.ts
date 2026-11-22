import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  mlBackendsApi,
  type InteractiveAnnotateResponse,
  type InteractiveInferenceLineage,
} from "@/api/ml-backends";
import { useToastStore } from "@/components/ui/Toast";
import { VARIANT_FIELD_KEYS } from "../components/SchemaForm";
import { simplifyPolygon } from "../stage/shared/geometry/simplify";
import { createSamCache, makeSamCacheKey, SAM_CACHE_TTL_MS } from "./useSamCache";
import { validateCocoRle, type CocoRle } from "../stage/shared/geometry/maskRle";

export type TextOutputMode = "box" | "mask" | "both";

interface PendingCandidateBase {
  id: string;
  label: string;
  score: number | null;
  source: "point" | "bbox" | "scribble" | "exemplar";
}

export interface PendingPolygonCandidate extends PendingCandidateBase {
  type: "polygonlabels";
  points: [number, number][];
}

export interface PendingRectangleCandidate extends PendingCandidateBase {
  type: "rectanglelabels";
  bbox: { x: number; y: number; width: number; height: number };
}

export interface PendingMaskPromptSummary {
  family: "point" | "interactive_box" | "scribble" | "exemplar" | "mask" | "correction_frame";
  positive_points: number;
  negative_points: number;
  boxes: number;
  positive_scribbles: number;
  negative_scribbles: number;
  multimask: boolean;
  parameters_digest: string | null;
}

export interface PendingMaskCandidate extends PendingCandidateBase {
  type: "mask";
  rle: CocoRle;
  previewPoints?: [number, number][];
  candidateId: string;
  candidateIndex: number;
  promptRevision: string;
  frameIndex: number | null;
  receipt: string;
  idempotencyKey: string;
  promptSummary: PendingMaskPromptSummary;
  routing: {
    requested_backend_id: string;
    backend_pool_id: string | null;
    backend_instance_id: string;
    model_id: string;
  };
  inference: InteractiveInferenceLineage;
  refineSource?: { annotationId: string; sourceVersion: number };
}

export type PendingCandidate =
  | PendingPolygonCandidate
  | PendingRectangleCandidate
  | PendingMaskCandidate;

export type InteractiveTransport = (args: {
  projectId: string;
  mlBackendId: string;
  taskId: string;
  context: Record<string, unknown>;
  signal: AbortSignal;
}) => Promise<InteractiveAnnotateResponse>;

export interface UseInteractiveAIArgs {
  projectId: string | undefined;
  taskId: string | undefined;
  mlBackendId: string | undefined | null;
  transport?: InteractiveTransport;
  cacheScope?: string | number;
  requestContextDefaults?: Record<string, unknown>;
}

export interface UseInteractiveAIReturn {
  candidates: PendingCandidate[];
  activeIdx: number;
  isRunning: boolean;
  error: string | null;
  canRetry: boolean;
  canAcceptCandidates: boolean;
  retryLast: () => void;
  sessionPoints: { pt: [number, number]; polarity: 1 | 0 }[];
  sessionScribbles: {
    points: [number, number][];
    polarity: 1 | 0;
    width: number;
  }[];
  sessionExemplars: { bbox: [number, number, number, number]; polarity: 1 | 0 }[];
  exemplarText: string;
  setExemplarText: (text: string) => void;
  exemplarThreshold: number | null;
  setExemplarThreshold: (thr: number | null) => void;
  runPoint: (pt: [number, number], polarity: 1 | 0, extraParams?: Record<string, unknown>) => void;
  runBbox: (bbox: [number, number, number, number], extraParams?: Record<string, unknown>) => void;
  runScribble: (
    points: [number, number][],
    polarity: 1 | 0,
    width?: number,
    extraParams?: Record<string, unknown>,
  ) => void;
  runExemplar: (
    bbox: [number, number, number, number],
    polarity: 1 | 0,
    outputMode?: TextOutputMode,
    extraParams?: Record<string, unknown>,
  ) => void;
  rerunExemplar: (outputMode?: TextOutputMode, extraParams?: Record<string, unknown>) => void;
  cycle: (dir: 1 | -1) => void;
  select: (idx: number) => void;
  consume: (idx: number) => void;
  cancel: () => void;
  warmup: () => void;
}

const DEBOUNCE_MS = 80;
const EXEMPLAR_DEBOUNCE_MS = 220;

const defaultTransport: InteractiveTransport = ({
  projectId,
  mlBackendId,
  taskId,
  context,
  signal,
}) =>
  mlBackendsApi.interactiveAnnotate(projectId, mlBackendId, { task_id: taskId, context }, signal);

export function useInteractiveAI(args: UseInteractiveAIArgs): UseInteractiveAIReturn {
  const { projectId, taskId, mlBackendId, cacheScope } = args;
  const transport = args.transport ?? defaultTransport;
  const transportRef = useRef(transport);
  transportRef.current = transport;
  const requestContextDefaultsRef = useRef(args.requestContextDefaults ?? {});
  requestContextDefaultsRef.current = args.requestContextDefaults ?? {};
  const pushToast = useToastStore((s) => s.push);

  const [candidates, setCandidates] = useState<PendingCandidate[]>([]);
  const [activeIdx, setActiveIdx] = useState(0);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canRetry, setCanRetry] = useState(false);
  const [canAcceptCandidates, setCanAcceptCandidates] = useState(false);
  const lastFailedRequestRef = useRef<{
    context: Record<string, unknown>;
    source: PendingCandidate["source"];
  } | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const exemplarDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflightRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const activeCacheKeyRef = useRef<string | null>(null);
  const candidateExpiryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const pointSessionRef = useRef<{ pt: [number, number]; polarity: 1 | 0 }[]>([]);
  const [sessionPoints, setSessionPoints] = useState<{ pt: [number, number]; polarity: 1 | 0 }[]>(
    [],
  );
  const maskInputRef = useRef<string | null>(null);
  const maskInputVariantSigRef = useRef<string | null>(null);
  const activeRefinementFamilyRef = useRef<"point" | "bbox" | "scribble" | null>(null);
  const scribbleSessionRef = useRef<
    {
      points: [number, number][];
      polarity: 1 | 0;
      width: number;
    }[]
  >([]);
  const [sessionScribbles, setSessionScribbles] = useState<
    {
      points: [number, number][];
      polarity: 1 | 0;
      width: number;
    }[]
  >([]);

  const clearPointSession = useCallback(() => {
    pointSessionRef.current = [];
    setSessionPoints([]);
  }, []);
  const clearScribbleSession = useCallback(() => {
    scribbleSessionRef.current = [];
    setSessionScribbles([]);
  }, []);
  const resetRefinementSession = useCallback(() => {
    clearPointSession();
    clearScribbleSession();
    activeRefinementFamilyRef.current = null;
    maskInputRef.current = null;
    maskInputVariantSigRef.current = null;
  }, [clearPointSession, clearScribbleSession]);
  const enterRefinementFamily = useCallback(
    (family: "point" | "bbox" | "scribble") => {
      if (activeRefinementFamilyRef.current === family) return;
      if (family !== "point") clearPointSession();
      if (family !== "scribble") clearScribbleSession();
      activeRefinementFamilyRef.current = family;
    },
    [clearPointSession, clearScribbleSession],
  );

  const exemplarSessionRef = useRef<{ bbox: [number, number, number, number]; polarity: 1 | 0 }[]>(
    [],
  );
  const lastExemplarArgsRef = useRef<{
    outputMode: TextOutputMode;
    extra: Record<string, unknown>;
  }>({ outputMode: "mask", extra: {} });
  const [sessionExemplars, setSessionExemplars] = useState<
    { bbox: [number, number, number, number]; polarity: 1 | 0 }[]
  >([]);
  const [exemplarText, setExemplarTextState] = useState("");
  const [exemplarThreshold, setExemplarThresholdState] = useState<number | null>(null);
  const exemplarTextRef = useRef("");
  const exemplarThresholdRef = useRef<number | null>(null);
  const resetExemplarSession = useCallback(() => {
    exemplarSessionRef.current = [];
    exemplarTextRef.current = "";
    exemplarThresholdRef.current = null;
    lastExemplarArgsRef.current = { outputMode: "mask", extra: {} };
    setSessionExemplars([]);
    setExemplarTextState("");
    setExemplarThresholdState(null);
  }, []);

  const lastAppliedVariantRef = useRef<string | null>(null);
  useEffect(() => {
    lastAppliedVariantRef.current = null;
  }, [mlBackendId]);

  const cache = useMemo(() => createSamCache(), []);
  useEffect(() => {
    cache.clearAll();
  }, [mlBackendId, cache]);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (exemplarDebounceRef.current) clearTimeout(exemplarDebounceRef.current);
      if (candidateExpiryRef.current) clearTimeout(candidateExpiryRef.current);
      abortRef.current?.abort();
      cache.clearAll();
    };
  }, [cache]);

  const retireCandidates = useCallback(
    (deleteCached: boolean) => {
      if (candidateExpiryRef.current) {
        clearTimeout(candidateExpiryRef.current);
        candidateExpiryRef.current = null;
      }
      if (deleteCached && activeCacheKeyRef.current) {
        cache.delete(activeCacheKeyRef.current);
      }
      activeCacheKeyRef.current = null;
      setCandidates([]);
      setActiveIdx(0);
      setCanAcceptCandidates(false);
    },
    [cache],
  );

  const activateCandidates = useCallback(
    (next: PendingCandidate[], cacheKey: string, expiresAt: number) => {
      if (candidateExpiryRef.current) clearTimeout(candidateExpiryRef.current);
      activeCacheKeyRef.current = cacheKey;
      setCandidates(next);
      setActiveIdx(0);
      setCanAcceptCandidates(true);
      candidateExpiryRef.current = setTimeout(
        () => {
          if (activeCacheKeyRef.current !== cacheKey) return;
          cache.delete(cacheKey);
          activeCacheKeyRef.current = null;
          setCandidates([]);
          setActiveIdx(0);
          setCanAcceptCandidates(false);
          resetRefinementSession();
          resetExemplarSession();
        },
        Math.max(0, expiresAt - Date.now()),
      );
    },
    [cache, resetExemplarSession, resetRefinementSession],
  );

  const guard = useCallback((): boolean => {
    if (!projectId || !taskId) return false;
    if (!mlBackendId) {
      pushToast({
        msg: "项目未绑定模型服务",
        sub: "请先在项目设置中绑定 SAM 后端",
        kind: "error",
      });
      return false;
    }
    return true;
  }, [projectId, taskId, mlBackendId, pushToast]);

  const dispatch = useCallback(
    async (context: Record<string, unknown>, source: PendingCandidate["source"]) => {
      if (!projectId || !taskId || !mlBackendId) return;
      abortRef.current?.abort();
      const myInflight = ++inflightRef.current;
      setError(null);
      const normalized = normalizePredictContext({
        ...requestContextDefaultsRef.current,
        ...context,
      });
      let requestContext = normalized.context;
      const variantSig = variantSignature(requestContext);
      if (requestContext.mask_input != null && maskInputVariantSigRef.current !== variantSig) {
        const { mask_input: _staleMaskInput, ...withoutStaleMaskInput } = requestContext;
        requestContext = withoutStaleMaskInput;
        maskInputRef.current = null;
        maskInputVariantSigRef.current = null;
      }
      const ctxKind = (requestContext.type as string | undefined) ?? source;
      const { mask_input: _maskInput, ...ctxForKey } = requestContext;
      const cacheKey = makeSamCacheKey({
        taskId,
        mlBackendId,
        ctxKind,
        ctx: ctxForKey,
        scope: cacheScope,
      });
      const bypassCache =
        requestContext.output_geometry === "mask" && requestContext.mask_input != null;
      const cached = bypassCache ? undefined : cache.get(cacheKey);
      if (cached) {
        lastFailedRequestRef.current = null;
        setCanRetry(false);
        activateCandidates(cached.candidates, cacheKey, cached.expiresAt);
        setIsRunning(false);
        return;
      }
      const isVariantSwitch = variantSig !== null && variantSig !== lastAppliedVariantRef.current;
      if (isVariantSwitch) {
        pushToast({ msg: `正在切换到 ${variantLabel(requestContext)} 模型…` });
      }
      const controller = new AbortController();
      abortRef.current = controller;
      lastFailedRequestRef.current = null;
      setCanRetry(false);
      setCanAcceptCandidates(false);
      setIsRunning(true);
      try {
        const resp = await transportRef.current({
          projectId,
          mlBackendId,
          taskId,
          context: requestContext,
          signal: controller.signal,
        });
        if (myInflight !== inflightRef.current) return;
        setError(null);
        lastFailedRequestRef.current = null;
        setCanRetry(false);
        maskInputRef.current = resp.mask_input_next ?? null;
        maskInputVariantSigRef.current = resp.mask_input_next ? variantSig : null;
        if (isVariantSwitch) {
          lastAppliedVariantRef.current = variantSig;
          pushToast({ msg: `已切换到 ${variantLabel(requestContext)}`, kind: "success" });
        }
        const next: PendingCandidate[] = (resp.result ?? [])
          .map((r, i) => normalizeResult(r, i, source, resp, requestContext))
          .filter((c): c is PendingCandidate => c !== null);
        if (next.length > 0) {
          const stored = bypassCache ? undefined : cache.set(cacheKey, next);
          activateCandidates(next, cacheKey, stored?.expiresAt ?? Date.now() + SAM_CACHE_TTL_MS);
        }
        if (next.length === 0) {
          retireCandidates(false);
          pushToast({
            msg: "SAM 未返回候选",
            sub: source === "exemplar" ? "可调低阈值或多画几个样例框" : "请尝试不同的位置/区域",
            kind: "warning",
          });
        } else {
          pushToast({
            msg: `${next.length} 个候选`,
            sub: source === "point" ? "Tab 切换备选 / Enter 采纳" : "在画布上确认候选",
            kind: "success",
          });
        }
      } catch (err) {
        if (controller.signal.aborted || myInflight !== inflightRef.current) return;
        const formatted = formatPredictError(err);
        const msg = err instanceof Error ? err.message : String(err);
        setError(formatted ? `${formatted.msg}${formatted.sub ? `：${formatted.sub}` : ""}` : msg);
        const sessionReason = apiErrorReason(err);
        const invalidSession =
          sessionReason === "invalid_mask_session" ||
          sessionReason === "mask_session_expired" ||
          sessionReason === "mask_session_mismatch";
        let retryContext = requestContext;
        if (invalidSession) {
          const { mask_input: _invalidMaskInput, ...withoutInvalidMaskInput } = requestContext;
          retryContext = withoutInvalidMaskInput;
          maskInputRef.current = null;
          maskInputVariantSigRef.current = null;
        }
        const retryableWithoutSession = !(
          retryContext.type === "scribble" &&
          !isRecord(retryContext.mask_prompt_source) &&
          Array.isArray(retryContext.scribbles) &&
          retryContext.scribbles.every((stroke) => isRecord(stroke) && stroke.polarity === 0)
        );
        lastFailedRequestRef.current = retryableWithoutSession
          ? { context: retryContext, source }
          : null;
        setCanRetry(retryableWithoutSession);
        pushToast({
          msg: formatted?.msg ?? (isVariantSwitch ? "模型切换失败" : "SAM 推理失败"),
          sub: formatted?.sub ?? msg,
          kind: "error",
        });
      } finally {
        if (myInflight === inflightRef.current) setIsRunning(false);
      }
    },
    [
      projectId,
      taskId,
      mlBackendId,
      cacheScope,
      pushToast,
      cache,
      activateCandidates,
      retireCandidates,
    ],
  );

  const retryLast = useCallback(() => {
    const failed = lastFailedRequestRef.current;
    if (!failed || isRunning) return;
    void dispatch(failed.context, failed.source);
  }, [dispatch, isRunning]);

  const runPoint = useCallback(
    (pt: [number, number], polarity: 1 | 0, extraParams?: Record<string, unknown>) => {
      if (!guard()) return;
      resetExemplarSession();
      enterRefinementFamily("point");
      pointSessionRef.current = [...pointSessionRef.current, { pt, polarity }];
      const session = pointSessionRef.current;
      setSessionPoints(session);
      const points = session.map((s) => s.pt);
      const labels = session.map((s) => s.polarity);
      const hasMaskPrompt = isRecord(requestContextDefaultsRef.current.mask_prompt_source);
      const multimask = session.length === 1 && !hasMaskPrompt;
      const refeed = !multimask && maskInputRef.current ? maskInputRef.current : undefined;
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => {
        dispatch(
          {
            ...(extraParams ?? {}),
            type: "point",
            points,
            labels,
            multimask_output: multimask,
            ...(refeed ? { mask_input: refeed } : {}),
          },
          "point",
        );
      }, DEBOUNCE_MS);
    },
    [guard, dispatch, enterRefinementFamily, resetExemplarSession],
  );

  const runBbox = useCallback(
    (bbox: [number, number, number, number], extraParams?: Record<string, unknown>) => {
      if (!guard()) return;
      enterRefinementFamily("bbox");
      resetExemplarSession();
      dispatch(
        {
          ...(extraParams ?? {}),
          type: "interactive_box",
          bbox,
          multimask_output: false,
          ...(maskInputRef.current ? { mask_input: maskInputRef.current } : {}),
        },
        "bbox",
      );
    },
    [guard, dispatch, enterRefinementFamily, resetExemplarSession],
  );

  const runScribble = useCallback(
    (
      points: [number, number][],
      polarity: 1 | 0,
      width = 0.008,
      extraParams?: Record<string, unknown>,
    ) => {
      if (!guard() || points.length < 2) return;
      const totalPoints = scribbleSessionRef.current.reduce(
        (total, stroke) => total + stroke.points.length,
        0,
      );
      if (scribbleSessionRef.current.length >= 64 || totalPoints + points.length > 8192) {
        pushToast({
          msg: "笔迹上限已达到",
          sub: "每轮最多 64 条、8,192 个采样点；可先接纳当前候选再继续",
          kind: "warning",
        });
        return;
      }
      resetExemplarSession();
      enterRefinementFamily("scribble");
      const stroke = { points, polarity, width };
      scribbleSessionRef.current = [...scribbleSessionRef.current, stroke];
      setSessionScribbles(scribbleSessionRef.current);
      dispatch(
        {
          ...(extraParams ?? {}),
          type: "scribble",
          scribbles: scribbleSessionRef.current,
          multimask_output: false,
          ...(maskInputRef.current ? { mask_input: maskInputRef.current } : {}),
        },
        "scribble",
      );
    },
    [dispatch, enterRefinementFamily, guard, pushToast, resetExemplarSession],
  );

  const dispatchExemplar = useCallback(() => {
    const session = exemplarSessionRef.current;
    if (session.length === 0) return;
    const { outputMode, extra } = lastExemplarArgsRef.current;
    const exemplars = session.map((s) => ({ bbox: s.bbox, label: s.polarity === 1 }));
    const text = exemplarTextRef.current.trim();
    const thr = exemplarThresholdRef.current;
    dispatch(
      {
        ...extra,
        type: "exemplar",
        exemplars,
        output: outputMode,
        ...(outputMode === "box" ? { output_geometry: "polygon" } : {}),
        ...(text ? { text } : {}),
        ...(thr != null ? { score_threshold: thr } : {}),
      },
      "exemplar",
    );
  }, [dispatch]);

  const dispatchExemplarDebounced = useCallback(() => {
    if (exemplarDebounceRef.current) clearTimeout(exemplarDebounceRef.current);
    exemplarDebounceRef.current = setTimeout(() => {
      dispatchExemplar();
    }, EXEMPLAR_DEBOUNCE_MS);
  }, [dispatchExemplar]);

  const runExemplar = useCallback(
    (
      bbox: [number, number, number, number],
      polarity: 1 | 0,
      outputMode: TextOutputMode = "mask",
      extraParams?: Record<string, unknown>,
    ) => {
      if (!guard()) return;
      resetRefinementSession();
      exemplarSessionRef.current = [...exemplarSessionRef.current, { bbox, polarity }];
      setSessionExemplars(exemplarSessionRef.current);
      lastExemplarArgsRef.current = { outputMode, extra: extraParams ?? {} };
      dispatchExemplar();
    },
    [guard, resetRefinementSession, dispatchExemplar],
  );

  const rerunExemplar = useCallback(
    (outputMode?: TextOutputMode, extraParams?: Record<string, unknown>) => {
      if (!guard()) return;
      if (exemplarSessionRef.current.length === 0) return;
      lastExemplarArgsRef.current = {
        outputMode: outputMode ?? lastExemplarArgsRef.current.outputMode,
        extra: extraParams ?? lastExemplarArgsRef.current.extra,
      };
      dispatchExemplar();
    },
    [guard, dispatchExemplar],
  );

  const setExemplarText = useCallback(
    (text: string) => {
      exemplarTextRef.current = text;
      setExemplarTextState(text);
      if (exemplarSessionRef.current.length > 0) dispatchExemplarDebounced();
    },
    [dispatchExemplarDebounced],
  );

  const setExemplarThreshold = useCallback(
    (thr: number | null) => {
      exemplarThresholdRef.current = thr;
      setExemplarThresholdState(thr);
      if (exemplarSessionRef.current.length > 0) dispatchExemplarDebounced();
    },
    [dispatchExemplarDebounced],
  );

  const cycleStable = useCallback(
    (dir: 1 | -1) => {
      setActiveIdx((i) => {
        const n = candidates.length;
        if (n === 0) return 0;
        return (((i + dir) % n) + n) % n;
      });
    },
    [candidates.length],
  );

  const select = useCallback(
    (idx: number) => {
      setActiveIdx(candidates.length === 0 ? 0 : Math.min(Math.max(0, idx), candidates.length - 1));
    },
    [candidates.length],
  );

  const consume = useCallback(
    (idx: number) => {
      const c = candidates[idx];
      const activeCacheKey = activeCacheKeyRef.current;
      if (activeCacheKey) cache.delete(activeCacheKey);
      if (c && (c.source === "point" || c.source === "bbox" || c.source === "scribble")) {
        resetRefinementSession();
        retireCandidates(false);
        return;
      }
      const remaining = candidates.filter((_, i) => i !== idx);
      if (remaining.length === 0) {
        retireCandidates(false);
      } else {
        const stored = activeCacheKey ? cache.set(activeCacheKey, remaining) : undefined;
        if (activeCacheKey) {
          activateCandidates(
            remaining,
            activeCacheKey,
            stored?.expiresAt ?? Date.now() + SAM_CACHE_TTL_MS,
          );
        } else {
          setCandidates(remaining);
        }
      }
      setActiveIdx((i) => Math.max(0, i >= idx ? i - 1 : i));
    },
    [candidates, resetRefinementSession, cache, activateCandidates, retireCandidates],
  );

  const cancel = useCallback(() => {
    resetRefinementSession();
    resetExemplarSession();
    retireCandidates(false);
    cache.clearAll();
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    if (exemplarDebounceRef.current) {
      clearTimeout(exemplarDebounceRef.current);
      exemplarDebounceRef.current = null;
    }
    abortRef.current?.abort();
    inflightRef.current++;
    lastFailedRequestRef.current = null;
    setError(null);
    setCanRetry(false);
    setIsRunning(false);
  }, [resetRefinementSession, resetExemplarSession, cache, retireCandidates]);

  const warmedRef = useRef<string | null>(null);
  const warmup = useCallback(() => {
    if (!projectId || !taskId || !mlBackendId) return;
    const key = `${taskId}|${mlBackendId}`;
    if (warmedRef.current === key) return;
    warmedRef.current = key;
    const ctx = { type: "point", points: [[0.5, 0.5]], labels: [1] };
    transportRef
      .current({
        projectId,
        mlBackendId,
        taskId,
        context: ctx,
        signal: new AbortController().signal,
      })
      .then(() => undefined)
      .catch(() => {
        warmedRef.current = null;
      });
  }, [projectId, taskId, mlBackendId]);

  useEffect(() => {
    warmedRef.current = null;
    cancel();
  }, [projectId, taskId, mlBackendId, cacheScope, cancel]);

  return {
    candidates,
    activeIdx,
    isRunning,
    error,
    canRetry,
    canAcceptCandidates,
    retryLast,
    sessionPoints,
    sessionScribbles,
    sessionExemplars,
    exemplarText,
    setExemplarText,
    exemplarThreshold,
    setExemplarThreshold,
    runPoint,
    runBbox,
    runScribble,
    runExemplar,
    rerunExemplar,
    cycle: cycleStable,
    select,
    consume,
    cancel,
    warmup,
  };
}

function variantSignature(context: Record<string, unknown>): string | null {
  const variants = readModelVariants(context);
  const parts = Object.keys(variants)
    .sort()
    .map((key) => `${key}=${variants[key]}`);
  return parts.length > 0 ? parts.join("|") : null;
}

function variantLabel(context: Record<string, unknown>): string {
  const variants = readModelVariants(context);
  const sam = variants.sam_variant ?? null;
  const dino = variants.dino_variant ?? null;
  const segs: string[] = [];
  if (sam) segs.push(`SAM ${sam}`);
  if (dino) segs.push(`DINO ${dino}`);
  for (const [key, value] of Object.entries(variants)) {
    if (key === "sam_variant" || key === "dino_variant") continue;
    segs.push(`${key}=${value}`);
  }
  return segs.join("/");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function readModelVariants(context: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  const nested = context.model_variants;
  if (isRecord(nested)) {
    for (const [key, value] of Object.entries(nested)) {
      if (typeof value === "string") out[key] = value;
    }
  }
  for (const key of VARIANT_FIELD_KEYS) {
    const value = context[key];
    if (typeof value === "string" && out[key] == null) out[key] = value;
  }
  return out;
}

function normalizePredictContext(context: Record<string, unknown>): {
  context: Record<string, unknown>;
  modelVariants: Record<string, string>;
} {
  const modelVariants = readModelVariants(context);
  const next: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(context)) {
    if (
      key === "model_variants" ||
      VARIANT_FIELD_KEYS.includes(key as (typeof VARIANT_FIELD_KEYS)[number])
    ) {
      continue;
    }
    next[key] = value;
  }
  if (Object.keys(modelVariants).length > 0) {
    next.model_variants = modelVariants;
  }
  return { context: next, modelVariants };
}

function apiErrorReason(err: unknown): string | null {
  if (!(err instanceof ApiError) || !isRecord(err.detailRaw)) return null;
  return typeof err.detailRaw.reason === "string" ? err.detailRaw.reason : null;
}

function formatPredictError(err: unknown): { msg: string; sub?: string } | null {
  if (!(err instanceof ApiError)) return null;
  if (err.status === 422) {
    return { msg: "参数错误，请检查输入", sub: err.message };
  }
  if (err.status === 503) {
    const retryAfter = err.headers?.["retry-after"];
    return {
      msg: "模型暂不可用",
      sub: retryAfter ? `${retryAfter} 秒后重试\n${err.message}` : err.message,
    };
  }
  if (err.status >= 500) {
    return { msg: "服务异常", sub: `HTTP ${err.status}\n${err.message}` };
  }
  return null;
}

interface BackendResult {
  type?: string;
  value?: {
    points?: [number, number][];
    polygons?: { points: [number, number][]; holes?: [number, number][][] }[];
    polygonlabels?: string[];
    x?: number;
    y?: number;
    width?: number;
    height?: number;
    rectanglelabels?: string[];
    rle?: CocoRle;
    masklabels?: string[];
    preview?: {
      points?: [number, number][];
    };
  };
  score?: number;
  candidate_id?: string;
}

let idempotencySequence = 0;

export function newMaskIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return `mask:${globalThis.crypto.randomUUID()}`;
  }
  idempotencySequence += 1;
  return `mask:fallback:${Date.now().toString(36)}:${idempotencySequence.toString(36)}`;
}

function normalizeResult(
  raw: unknown,
  idx: number,
  source: PendingCandidate["source"],
  response: InteractiveAnnotateResponse,
  context: Record<string, unknown>,
): PendingCandidate | null {
  const r = raw as BackendResult;
  const score = typeof r.score === "number" ? r.score : null;
  const id = `sam-${Date.now()}-${idx}`;

  if (r.type === "mask") {
    const candidateId = r.candidate_id;
    const promptRevision = response.prompt_revision;
    const receipt = candidateId ? response.accept_receipts?.[candidateId] : undefined;
    const routing = response.routing;
    const promptSummary = response.prompt_summary;
    if (
      typeof candidateId !== "string" ||
      response.output_geometry !== "mask" ||
      typeof promptRevision !== "string" ||
      typeof receipt !== "string" ||
      !routing ||
      typeof routing.requested_backend_id !== "string" ||
      typeof routing.backend_instance_id !== "string" ||
      typeof routing.model_id !== "string" ||
      !promptSummary ||
      !r.value?.rle
    ) {
      return null;
    }
    let rle: CocoRle;
    try {
      rle = validateCocoRle(r.value.rle);
    } catch {
      return null;
    }
    const previewPoints = pickPrimaryRing(r.value.preview);
    return {
      id: candidateId,
      type: "mask",
      rle,
      ...(previewPoints
        ? { previewPoints: previewPoints.map(([x, y]) => [x, y] as [number, number]) }
        : {}),
      candidateId,
      candidateIndex: idx,
      promptRevision,
      frameIndex: Number.isInteger(response.frame_index) ? (response.frame_index as number) : null,
      receipt,
      idempotencyKey: newMaskIdempotencyKey(),
      promptSummary,
      routing: {
        requested_backend_id: routing.requested_backend_id,
        backend_pool_id: routing.backend_pool_id,
        backend_instance_id: routing.backend_instance_id,
        model_id: routing.model_id,
      },
      inference: {
        model_version: response.model_version ?? null,
        inference_time_ms: response.inference_time_ms ?? null,
        cache_hit: response.cache_hit ?? null,
        model_load_ms: response.model_load_ms ?? null,
      },
      refineSource: readMaskPromptSource(context),
      label: r.value.masklabels?.[0] ?? "object",
      score,
      source,
    };
  }

  if (r.type === "rectanglelabels") {
    const v = r.value;
    if (
      !v ||
      typeof v.x !== "number" ||
      typeof v.y !== "number" ||
      typeof v.width !== "number" ||
      typeof v.height !== "number"
    ) {
      return null;
    }
    return {
      id,
      type: "rectanglelabels",
      bbox: { x: v.x, y: v.y, width: v.width, height: v.height },
      label: v.rectanglelabels?.[0] ?? "object",
      score,
      source,
    };
  }

  if (r.type != null && r.type !== "polygonlabels") return null;
  const pts = pickPrimaryRing(r?.value);
  if (!Array.isArray(pts) || pts.length < 3) return null;
  const ring = pts.map(([x, y]) => [x, y]) as [number, number][];
  return {
    id,
    type: "polygonlabels",
    points: simplifyCandidateRing(ring),
    label: r.value?.polygonlabels?.[0] ?? "object",
    score,
    source,
  };
}

function readMaskPromptSource(
  context: Record<string, unknown>,
): PendingMaskCandidate["refineSource"] {
  const source = context.mask_prompt_source;
  if (!isRecord(source)) return undefined;
  const annotationId = source.annotation_id;
  const sourceVersion = source.source_version;
  if (typeof annotationId !== "string" || !Number.isInteger(sourceVersion)) return undefined;
  return { annotationId, sourceVersion: sourceVersion as number };
}

export const SAM_SIMPLIFY_RATIO = 0.003;

export function simplifyCandidateRing(ring: [number, number][]): [number, number][] {
  if (ring.length < 4) return ring;
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (const [x, y] of ring) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  const diagonal = Math.hypot(maxX - minX, maxY - minY);
  if (!(diagonal > 0)) return ring;
  return simplifyPolygon(ring, diagonal * SAM_SIMPLIFY_RATIO) as [number, number][];
}

function ringArea(ring: [number, number][]): number {
  let a = 0;
  for (let i = 0, n = ring.length; i < n; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % n];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

function pickPrimaryRing(value: BackendResult["value"]): [number, number][] | null {
  if (Array.isArray(value?.points) && value.points.length >= 3) return value.points;
  const polys = value?.polygons;
  if (!Array.isArray(polys) || polys.length === 0) return null;
  let best: [number, number][] | null = null;
  let bestArea = -1;
  for (const p of polys) {
    const ring = p?.points;
    if (!Array.isArray(ring) || ring.length < 3) continue;
    const area = ringArea(ring);
    if (area > bestArea) {
      bestArea = area;
      best = ring;
    }
  }
  return best;
}
