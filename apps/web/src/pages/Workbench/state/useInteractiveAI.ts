import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  mlBackendsApi,
  type InteractiveAnnotateResponse,
  type InteractiveInferenceLineage,
} from "@/api/ml-backends";
import { useToastStore } from "@/components/ui/Toast";
import { VARIANT_FIELD_KEYS } from "../components/SchemaForm";
import { recordPredictCacheHit } from "./sessionVariantCache";
import { simplifyPolygon } from "../stage/shared/geometry/simplify";
import { createSamCache, makeSamCacheKey, SAM_CACHE_TTL_MS } from "./useSamCache";
import { validateCocoRle, type CocoRle } from "../stage/shared/geometry/maskRle";

/**
 * v0.9.2 · 工作台 SAM 交互式 hook。
 *
 * 各 prompt 全部走 `POST /projects/{pid}/ml-backends/{bid}/interactive-annotating`：
 *   point           : ctx { type:"point", points:[[x,y],...], labels:[1|0,...], multimask_output }
 *                     (v0.18.17 · 正/负点累加, 每次重发全量点; 首点 multimask 出候选)
 *   interactive_box : ctx { type:"interactive_box", bbox:[x1,y1,x2,y2] } (v0.18.17 · 旧 "bbox" 改名)
 *   exemplar        : ctx { type:"exemplar", bbox:[x1,y1,x2,y2] } (SAM 3 PCS 全图相似)
 * 后端返回 `result[]`，每条形如：
 *   { type:"polygonlabels", value:{ points:[[x,y]...], polygonlabels:[label] }, score }
 *
 * 候选以「待确认紫虚线」叠加到 Konva canvas，由 `<PendingPolygonsOverlay>` 消费。
 *
 * 防抖：runPoint 80ms（轻击 / 多点同图场景）；runBbox 不防抖（一次完整动作）。
 * 文本召回 (type=text) 自 v0.14.18 起归批量 AI 面板，不再走本 hook 的交互浮层。
 */
/** v0.9.4 phase 2 · text 模式输出形态. point/bbox 模式恒为 "mask"(协议默认). */
export type TextOutputMode = "box" | "mask" | "both";

interface PendingCandidateBase {
  /** 仅用于 React key / 选中态定位 */
  id: string;
  /** backend 给的标签（DINO 短语 / SAM 默认 "object"） */
  label: string;
  score: number | null;
  /** 触发该候选的 prompt 类型 */
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
  /** Simplified display-only outline; the RLE remains authoritative on accept. */
  previewPoints?: [number, number][];
  candidateId: string;
  candidateIndex: number;
  promptRevision: string;
  /** 服务端签名的推理帧；视频接受不得改用播放过程中漂移的当前帧。 */
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
  /** 候选由已存 Mask 精修得到时，接纳必须原位更新该版本。 */
  refineSource?: { annotationId: string; sourceVersion: number };
}

/**
 * 候选几何判别联合。原生 Mask 保留 COCO RLE 和服务端签名的接纳血缘，
 * 不经过环提取、多边形简化或几何近似。
 */
export type PendingCandidate =
  | PendingPolygonCandidate
  | PendingRectangleCandidate
  | PendingMaskCandidate;

/**
 * v0.21.23 · 交互式推理的**投递方式**。默认走图片链路（只传 task_id，服务端按 task URL 取图）。
 *
 * 视频当前帧要传入 transport：视频 task 的 file_path 是整段 mp4，服务端取不到帧，需前端把
 * 当前帧解成 JPEG 走 multipart（见 `interactiveAnnotateFrame`）。抽成注入点是为了让整套
 * abort 取代 / 候选缓存 / 点会话累加 / multimask / mask_input 回灌 / 防抖 / consume 语义
 * 被两条链原样复用，而不是复刻一份。
 */
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
  /** 省略 = 图片链路（interactive-annotating，服务端自取图）。 */
  transport?: InteractiveTransport;
  /**
   * v0.21.23 · 候选缓存 / 点会话的额外作用域（视频传当前 frameIndex）。
   * 进 cacheKey 防跨帧串候选；变化时连同点会话与 mask_input 回灌一起失效——
   * low-res logits 绑定具体图像，切帧后回传给下一帧是错的。
   */
  cacheScope?: string | number;
  /** 合并到每次 prompt 的安全默认值，调用方字段优先。 */
  requestContextDefaults?: Record<string, unknown>;
}

export interface UseInteractiveAIReturn {
  candidates: PendingCandidate[];
  activeIdx: number;
  isRunning: boolean;
  /** Current inference failure, independent of whether that request can be retried. */
  error: string | null;
  canRetry: boolean;
  canAcceptCandidates: boolean;
  retryLast: () => void;
  /**
   * v0.18.18 · §5.5 当前点交互会话已落的正/负点 (归一化坐标 + 极性), 供画布 overlay 渲染。
   * 提交 / 取消 / 切 prompt / 切 task·backend 时清空。空数组 = 无进行中的多点精修会话。
   */
  sessionPoints: { pt: [number, number]; polarity: 1 | 0 }[];
  /** 当前 Mask 精修会话已落的正 / 负归一化笔迹。 */
  sessionScribbles: {
    points: [number, number][];
    polarity: 1 | 0;
    width: number;
  }[];
  /**
   * v0.18.19 · PCS exemplar refine 会话已落的正/负框 (归一化 xyxy + 极性), 供画布 overlay 渲染。
   * 正框=扩召回 / 负框=排误检; 每次操作重发全量。Esc / 切 prompt / 切 task·backend 时清空。
   */
  sessionExemplars: { bbox: [number, number, number, number]; polarity: 1 | 0 }[];
  /** v0.18.19 · exemplar 会话叠加的 text 概念 (PCS text + 几何组合); 改动即重跑当前会话。 */
  exemplarText: string;
  setExemplarText: (text: string) => void;
  /** v0.18.19 · exemplar 会话 per-request 阈值; null=用 backend 默认。拖动即重过滤当前会话。 */
  exemplarThreshold: number | null;
  setExemplarThreshold: (thr: number | null) => void;
  /** v0.10.2 · 各 run* 接受可选 extraParams; 由 InteractiveToolBar 注入 (box_threshold 等). */
  runPoint: (pt: [number, number], polarity: 1 | 0, extraParams?: Record<string, unknown>) => void;
  runBbox: (bbox: [number, number, number, number], extraParams?: Record<string, unknown>) => void;
  runScribble: (
    points: [number, number][],
    polarity: 1 | 0,
    width?: number,
    extraParams?: Record<string, unknown>,
  ) => void;
  /**
   * v0.18.19 · SAM 3 exemplar refine 会话: 拖框累加到正/负框集 (polarity 1=正 / 0=负), 每次
   * 重发全量 exemplars[] + text + 阈值 + output。与 bbox 同手势, context.type="exemplar"。
   */
  runExemplar: (
    bbox: [number, number, number, number],
    polarity: 1 | 0,
    outputMode?: TextOutputMode,
    extraParams?: Record<string, unknown>,
  ) => void;
  /** v0.18.19 · 不加新框, 用当前会话 (含最新 text/阈值/output) 重跑; outputMode 变更时由 shell 调。 */
  rerunExemplar: (outputMode?: TextOutputMode, extraParams?: Record<string, unknown>) => void;
  cycle: (dir: 1 | -1) => void;
  /** 画布 alpha picking 直接选中某个候选。 */
  select: (idx: number) => void;
  /** 接受一个候选；调用方拿到 candidate 后落库（创建 polygon annotation），随后调 consume(idx) 清除该条。 */
  consume: (idx: number) => void;
  /** 清空所有候选（Esc） */
  cancel: () => void;
  /**
   * v0.10.4 I6.2 · 异步触发 backend embed 预热 (image encoder + 缓存)。
   * 每个 (taskId, mlBackendId) 仅触发一次；结果丢弃但写入 cache，下次真实点击命中。
   * 用 dummy point @ image center；后端不支持 point (sam3-only exemplar) 时静默忽略错误。
   */
  warmup: () => void;
}

const DEBOUNCE_MS = 80;
// v0.18.x · exemplar 阈值滑块/文本框连续变更的防抖窗口 (拖一次滑块约 20 个 tick → 仅末位
// 生效一次 dispatch), 比 point 的 80ms 长, 因 exemplar 单次后端推理更重 (见 issue 0002)。
const EXEMPLAR_DEBOUNCE_MS = 220;

/** 默认投递：图片链路，只传 task_id，服务端按 task URL 取图。 */
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
  // transport 常是内联闭包（捕获 frameIndex / 取帧函数），放 ref 免得每次渲染都让
  // dispatch 的 useCallback 失效、进而级联重建 runPoint/runBbox/... 的引用。
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
  // v0.18.x · exemplar text/阈值连发的独立防抖 timer (与 point 的 debounceRef 互不干扰)。
  const exemplarDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflightRef = useRef(0);
  // v0.18.x · 当前 in-flight 交互请求的 AbortController: 新请求发起前 abort 掉上一个, 避免被
  // 取代的旧请求仍在后端跑到完成 (GPU 洪泛, 见 issue 0002)。
  const abortRef = useRef<AbortController | null>(null);
  const activeCacheKeyRef = useRef<string | null>(null);
  const candidateExpiryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // v0.18.17 · 点交互会话: 累加同一对象的正/负点, 每次重发全量点 (无状态后端). 单点首击
  // multimask 出候选; ≥2 点转单 mask 精修. 会话在 提交(consume point 候选) / Esc(cancel) /
  // 切 task·backend / 切 prompt 模式(bbox/text/exemplar) 时重置。
  const pointSessionRef = useRef<{ pt: [number, number]; polarity: 1 | 0 }[]>([]);
  // v0.18.18 · §5.5 会话点位可视化: pointSessionRef 是同步真源 (runPoint 立即读它拼全量点),
  // 这里镜像出可订阅 state 驱动画布 overlay 渲染已落的正/负点。
  const [sessionPoints, setSessionPoints] = useState<{ pt: [number, number]; polarity: 1 | 0 }[]>(
    [],
  );
  // mask_input 存上一轮单 mask 的受签名 low-res logits token。在同一已存 Mask
  // 精修会话中可跨 point / box / scribble 回灌；接受、取消、切题 / 帧 / 模型时释放。
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

  // v0.18.19 · exemplar refine 会话: 累加同一概念的正/负框, 每次重发全量 (无状态后端). 拖正框
  // 扩召回 / 拖负框去误检 / 拖阈值实时增减 / 叠 text 概念。会话在 Esc(cancel) / 切 task·backend /
  // 切到其它 prompt 模式 (point/bbox/text) 时重置。lastExemplarArgsRef 存上次 dispatch 的
  // outputMode + extra, 供「不加新框」的 text/阈值/output 变更重跑复用。
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
  // dispatchExemplar 读这两个 ref 拿最新 text/阈值, 避免把它们塞进 useCallback 依赖导致闭包过期。
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

  // v0.10.23 · 会话级模型变体切换反馈。变体切换是惰性的：用户在 AI 面板下拉选了新变体后,
  // 直到下一次预测才把它发给 backend (首次冷启 1-3s+)。lastAppliedVariantRef 记「上次成功
  // 应用的变体签名」, 切换后首次预测期间弹「切换中…→ 成功/失败」三态通知; 同变体后续预测不弹。
  // 切 backend → 清空 (新 backend 的变体语义独立)。
  const lastAppliedVariantRef = useRef<string | null>(null);
  useEffect(() => {
    lastAppliedVariantRef.current = null;
  }, [mlBackendId]);

  // v0.10.4 I6.1 · 按 (taskId, mlBackendId, ctx) 缓存候选；切 backend 时全清。
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
        msg: "项目未绑定 ML Backend",
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
      // v0.18.x · 新请求发起即 abort 上一个仍在跑的交互请求, 并自增 inflight 让旧请求结果失效
      // (含 cache-hit 路径, 防止旧 in-flight 完成后覆盖刚命中的缓存; 见 issue 0002)。
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
      // v0.18.18 · mask_input 是上一轮 logits 的不透明回灌, 不进缓存键 (同一点序的 mask_input
      // 是确定的, 排除它避免巨串塞键 / 误判 miss)。
      const { mask_input: _maskInput, ...ctxForKey } = requestContext;
      const cacheKey = makeSamCacheKey({
        taskId,
        mlBackendId,
        ctxKind,
        ctx: ctxForKey,
        scope: cacheScope,
      });
      // 命中前端缓存：直接复用候选，跳过 HTTP。
      const bypassCache =
        requestContext.output_geometry === "mask" && requestContext.mask_input != null;
      const cached = bypassCache ? undefined : cache.get(cacheKey);
      if (cached) {
        lastFailedRequestRef.current = null;
        setCanRetry(false);
        activateCandidates(cached.candidates, cacheKey, cached.expiresAt);
        // 上面 L212-213 已经 abort 旧请求 + 自增 inflightRef; 旧请求 finally 守卫不再通过
        // → 旋转图标永不清除 (issue claude[bot] P1)。此处显式复位。
        setIsRunning(false);
        return;
      }
      // 请求在途和网络失败时保留上一个已签名候选，便于继续修改或重试。
      // 新请求成功后由 activateCandidates 原子替换；成功空结果才退役旧候选。
      // v0.10.23 · 本次请求携带的变体是否与上次成功应用的不同 → 切换后首次预测, 弹三态通知。
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
        // 只接受最新一次请求的结果（防止防抖窗口外的旧请求覆盖新候选）
        if (myInflight !== inflightRef.current) return;
        setError(null);
        lastFailedRequestRef.current = null;
        setCanRetry(false);
        // v0.18.18 · 存本轮回灌 token: 单 mask 精修阶段非空, 多候选 / 框 / text / exemplar 为 null
        // (后端按 multimask 自动决定), 下次 ≥2 点点击经 context.mask_input 回传。
        maskInputRef.current = resp.mask_input_next ?? null;
        maskInputVariantSigRef.current = resp.mask_input_next ? variantSig : null;
        if (isVariantSwitch) {
          lastAppliedVariantRef.current = variantSig;
          pushToast({ msg: `已切换到 ${variantLabel(requestContext)}`, kind: "success" });
        }
        recordPredictCacheHit(
          mlBackendId,
          normalized.modelVariants,
          Object.keys(normalized.modelVariants).length > 0 ? resp.cache_hit : null,
        );
        const next: PendingCandidate[] = (resp.result ?? [])
          .map((r, i) => normalizeResult(r, i, source, resp, requestContext))
          .filter((c): c is PendingCandidate => c !== null);
        // 仅缓存非空结果，避免后端瞬时返空被钉死。
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
          // v0.18.26 · 出候选也给提示, 与「无候选」行为对齐: 右上角告知本轮候选数。
          // 候选是画布上的待确认浮层 (紫虚线), 不在右栏; point 多候选为同一对象的备选 mask。
          pushToast({
            msg: `${next.length} 个候选`,
            sub: source === "point" ? "Tab 切换备选 / Enter 采纳" : "在画布上确认候选",
            kind: "success",
          });
        }
      } catch (err) {
        // abort 掉的旧请求 (被新请求取代) 静默忽略, 不弹错误 toast (见 issue 0002)。
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
        // 切换后首次预测失败 → 不更新 lastAppliedVariant (下次重试仍视为切换), 落到切换失败态;
        // backend 503 (变体 checkpoint 未预置) 的 detail 经 ApiError.message 透出。
        pushToast({
          msg: formatted?.msg ?? (isVariantSwitch ? "模型切换失败" : "SAM 推理失败"),
          sub: formatted?.sub ?? msg,
          kind: "error",
        });
      } finally {
        if (myInflight === inflightRef.current) setIsRunning(false);
      }
    },
    // transport 走 ref，不进依赖（内联闭包每渲染新建，会级联重建所有 run* 引用）。
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
      // v0.18.19 · 切到点模式 → 重置 exemplar 会话 (互斥)。
      resetExemplarSession();
      enterRefinementFamily("point");
      // v0.18.17 · 累加到当前点会话, 每次重发全量点 (正/负点精修同一对象).
      pointSessionRef.current = [...pointSessionRef.current, { pt, polarity }];
      const session = pointSessionRef.current;
      // v0.18.18 · §5.5 镜像到可订阅 state, 驱动画布会话点 overlay。
      setSessionPoints(session);
      const points = session.map((s) => s.pt);
      const labels = session.map((s) => s.polarity);
      // 单点首击 multimask 出 3 候选 (top-1 + Tab 切换); 累加 ≥2 点转单 mask 精修.
      const hasMaskPrompt = isRecord(requestContextDefaultsRef.current.mask_prompt_source);
      const multimask = session.length === 1 && !hasMaskPrompt;
      // v0.18.18 · §5.4 仅多点精修阶段 (≥2 点) 回灌上一轮 low-res logits; 首点候选阶段不回灌
      // (多候选 index 歧义)。首个非候选点 (第 2 点) 时 maskInputRef 通常还空 (首点是 multimask),
      // 回灌自然从第 3 点起生效, 与协议约定一致。
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
      // 框提示清掉当前点 / 笔迹显示，但保留同一 Mask 会话的 logits token。
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

  // v0.18.19 · 用当前会话 (全量正/负框 + text + 阈值) 重发一次 exemplar 请求 (无状态后端).
  // outputMode / extra 取自 lastExemplarArgsRef (上次 run/rerun 写入); text/阈值取自 ref 实时值。
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
        // Box recall uses the legacy geometry envelope; keep the saved Mask
        // preference for the next segmentation request instead of validating boxes as Masks.
        ...(outputMode === "box" ? { output_geometry: "polygon" } : {}),
        ...(text ? { text } : {}),
        ...(thr != null ? { score_threshold: thr } : {}),
      },
      "exemplar",
    );
  }, [dispatch]);

  // v0.18.x · text/阈值连续变更走防抖: 拖一次滑块约 20 个 tick 只在末位触发一次 dispatch
  // (见 issue 0002)。runExemplar (拖新框) / rerunExemplar (切 output) 是单次明确动作, 即时 dispatch。
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
      // v0.18.19 · 切到 exemplar 模式 → 重置点会话; 拖框累加到正/负框集, 每次重发全量。
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
      // 会话进行中即重跑 (叠/改 text 概念, 防抖合并逐字符输入); 无会话时仅暂存, 下次拖框带上。
      if (exemplarSessionRef.current.length > 0) dispatchExemplarDebounced();
    },
    [dispatchExemplarDebounced],
  );

  const setExemplarThreshold = useCallback(
    (thr: number | null) => {
      exemplarThresholdRef.current = thr;
      setExemplarThresholdState(thr);
      // 拖阈值重过滤 (会话进行中, 防抖合并连续 tick); backbone 缓存命中下只重跑 grounding head。
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
      // v0.18.17 · point / interactive_box 的多候选是「同一对象的备选 mask」, 接受一个即
      // 清空全部 + 重置点会话 (开始下一个对象); text / exemplar 是「多实例」, 仅移除被接受的那条.
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
    // v0.18.x · abort 在跑的请求 + 自增 inflight, 取消会话后无结果再回来覆盖 (见 issue 0002)。
    // abort 后旧请求的 finally 因 inflight 已变不会复位 isRunning, 这里显式收尾。
    abortRef.current?.abort();
    inflightRef.current++;
    lastFailedRequestRef.current = null;
    setError(null);
    setCanRetry(false);
    setIsRunning(false);
  }, [resetRefinementSession, resetExemplarSession, cache, retireCandidates]);

  // v0.10.4 I6.2 · 预热去重：同 (taskId, mlBackendId) 只发一次。
  const warmedRef = useRef<string | null>(null);
  const warmup = useCallback(() => {
    if (!projectId || !taskId || !mlBackendId) return;
    const key = `${taskId}|${mlBackendId}`;
    if (warmedRef.current === key) return;
    warmedRef.current = key;
    // 静默发一个图中心 point，结果丢；只是为了让后端 image encoder 加载 + cache。
    // 绕过 dispatch 的 setCandidates / inflightRef，失败完全静默。
    // v0.21.23 · 走 transport：视频侧必须带当前帧图，直连图片端点会把整段 mp4 喂给 SAM。
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
        // backend 不支持 point (如 sam3 exemplar-only) 或其它失败 → 静默，下次真实点击会重试。
        warmedRef.current = null;
      });
  }, [projectId, taskId, mlBackendId]);

  // 切 project / task / backend / 帧 → 重置预热记忆, 并 cancel 掉整个会话。
  // v0.21.23 · cacheScope (视频 frameIndex) 也纳入同一重置入口。这里必须走 cancel 而不是只
  // reset 会话: 候选是**当前帧**的分割结果, 留着它跨帧会被采纳到新帧上 (提示发生在 F20、
  // 采纳落到 F28, 于是标注在当前帧不可见)。cancel 一并 abort 在飞请求, 避免旧帧的响应回来
  // 覆盖新帧候选。此外 mask_input 的 low-res logits 绑定具体图像, 跨帧回传必错。
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

// ── helpers ───────────────────────────────────────────────────────────────────

/**
 * v0.10.23 · 从 context 抽出变体字段 (sam_variant/dino_variant) 拼一个稳定签名,
 * 用于判定「是否切换了变体」。无任何变体字段时返回 null (该 backend 不暴露变体,
 * 或用户从未选过 → 不弹切换通知)。
 */
function variantSignature(context: Record<string, unknown>): string | null {
  const variants = readModelVariants(context);
  const parts = Object.keys(variants)
    .sort()
    .map((key) => `${key}=${variants[key]}`);
  return parts.length > 0 ? parts.join("|") : null;
}

/** v0.10.23 · 通知文案: 「SAM tiny/DINO base」; 其它轴显示 axis=value。 */
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
    // polygonlabels 字段
    points?: [number, number][];
    // 后端 _rings_to_polygon_label 在「多连通区域」时改用 polygons[] 承载各外环 (单环时才用 points)。
    polygons?: { points: [number, number][]; holes?: [number, number][][] }[];
    polygonlabels?: string[];
    // rectanglelabels 字段 (v0.9.4 phase 2)
    x?: number;
    y?: number;
    width?: number;
    height?: number;
    rectanglelabels?: string[];
    // 原生 Mask 字段
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

  // 仅历史缺省 type 或显式 polygonlabels 走多边形兼容；未知新类型不得被误解释。
  if (r.type != null && r.type !== "polygonlabels") return null;
  // 后端按环数分发: 单环 → value.points; 多连通区域 → value.polygons[].points。
  // 候选/预览/落库均为单环模型, 故多环时取「面积最大」外环 (主体), 丢弃碎屑噪点 —
  // 此前只读 value.points, 多环结果被静默丢弃 → "同位置时好时坏 / 没有候选区域"。
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

/**
 * 候选多边形的简化容差 —— 顶点偏移不超过该候选**外接框对角线**的 0.3%。
 *
 * SAM 的 mask 轮廓是逐像素台阶: 大面积目标能吐出近千个顶点(实测一条占半屏的路面 965 个),
 * 渲染成一片顶点句柄、编辑无从下手、落库也臃肿。用**相对**容差而非固定像素阈值, 因为后者
 * 对大小目标一视同仁 —— 能压平路面的阈值(≈5px)会同时把一辆宽 40px 的车改形。
 *
 * 实测(该视频 1920×1080, 见 docs/research/13-simplify-tolerance-eval.md 的方法):
 *   路面(965 顶点, 对角线 2629px) → 208 顶点, 面积保真 0.9999
 *   车  (24 顶点,  对角线  122px) →  23 顶点, 面积保真 0.9995
 */
export const SAM_SIMPLIFY_RATIO = 0.003;

/** 按候选自身尺度简化: epsilon = 外接框对角线 × SAM_SIMPLIFY_RATIO。 */
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

/** 多边形面积 (shoelace 绝对值; 归一化坐标, 仅用于比较取大). */
function ringArea(ring: [number, number][]): number {
  let a = 0;
  for (let i = 0, n = ring.length; i < n; i++) {
    const [x1, y1] = ring[i];
    const [x2, y2] = ring[(i + 1) % n];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

/** 单环取 points; 多环 (value.polygons) 取面积最大外环。无有效环返回 null。 */
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
