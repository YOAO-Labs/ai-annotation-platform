/**
 * 多 ML Backend 能力路由 (交互线).
 *
 * 一个项目注册多个 backend 时, 工作台 AI 从"单 active 后端驱动一切"改成"按角色 + 能力路由":
 * 交互工具 (point/interactive_box/exemplar) 各自解析到支持该 prompt 的交互后端, 批量线另走 batchBackendId。
 * 本 hook 只负责交互线: 对每个注册后端拉 /setup 建 capIndex, 产出 isPromptSupported (并集) +
 * resolveInteractive (逐 prompt 确定性解析 + preferred 兜底) + preferred 状态/持久化。
 *
 * 设计见 docs/plans/archive/2026-06-09-v0.14.18-ml-backend-capability-routing.md (§3/§4)。
 * 纯路由逻辑抽成下方独立函数, 便于单测 (capIndex 构建 / resolveInteractive 三情形 / 兜底链 / reachable 降级)。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { apiErrorDetailMessage } from "@/api/client";
import { mlBackendsApi, mlBackendSetupQueryKey, type MLBackendCapability } from "@/api/ml-backends";
import { INTERACTIVE_ROUTE_PROMPT_IDS } from "@/api/generated/capabilityVocab.gen";

/**
 * 交互 prompt 集合（不含批量文本 prompt）。单一事实源来自后端
 * capability_registry 的 PROMPTS.interactive_route, 经 codegen 生成 (消除手抄漂移:
 * 后端新增 interactive prompt 前端自动认)。当前含 point/interactive_box/exemplar +
 * 预留 scribble/sketch/mask (无 backend 消费时 candidatesFor 为空, 工具仍灰, 无副作用)。
 */
export const INTERACTIVE_PROMPTS = INTERACTIVE_ROUTE_PROMPT_IDS;
export type InteractivePrompt = (typeof INTERACTIVE_ROUTE_PROMPT_IDS)[number];

function isInteractivePrompt(p: string): p is InteractivePrompt {
  return (INTERACTIVE_PROMPTS as readonly string[]).includes(p);
}

export interface BackendCapEntry {
  /** 该后端支持的交互 prompt (跨其 model 取并集; 仅 is_interactive 的 model 计入)。 */
  prompts: Set<InteractivePrompt>;
  /** 同一 model 内可原子组合的 prompt/input/output；不得跨 model 拼接。 */
  interactiveModels: Array<{
    id: string;
    prompts: Set<InteractivePrompt>;
    inputs: Set<string>;
    outputs: Set<string>;
  }>;
  /** 同一 model 行的视频追踪能力，用于纠错路由，禁止跨 model 拼接。 */
  videoModels: Array<{
    id: string;
    trackers: string[];
    prompts: Set<string>;
    inputs: Set<string>;
    outputs: Set<string>;
    textDrivenTrackers: Set<string>;
    maxWindowFrames: number | null;
  }>;
  /** 是否支持文本 prompt (批量线判定用)。 */
  textCapable: boolean;
  /** 后端整体是否交互 (任一 model 或顶层 is_interactive)。 */
  isInteractive: boolean;
  /** 支持的视频 tracker (本期不消费, 仅入索引)。 */
  trackers: string[];
  /** 其中需要文本 prompt 的 tracker。 */
  textDrivenTrackers: string[];
  /** /setup 拉取成功 (false 时从候选排除, 自动降级)。 */
  reachable: boolean;
}

/** 由单个后端的 /setup 响应派生能力条目。reachable=false 表示拉取失败 (cap 为空)。 */
export function buildCapEntry(cap: MLBackendCapability | undefined): BackendCapEntry {
  const prompts = new Set<InteractivePrompt>();
  const interactiveModels: BackendCapEntry["interactiveModels"] = [];
  let textCapable = false;
  let isInteractive = false;
  const trackers = new Set<string>();
  const textDrivenTrackers = new Set<string>();

  if (!cap) {
    return {
      prompts,
      interactiveModels: [],
      videoModels: [],
      textCapable,
      isInteractive,
      trackers: [],
      textDrivenTrackers: [],
      reachable: false,
    };
  }

  const models = Array.isArray(cap.models) ? cap.models : [];
  const videoModels: BackendCapEntry["videoModels"] = [];
  const addPrompts = (interactive: boolean, supported: string[] | undefined) => {
    for (const p of supported ?? []) {
      if (p === "text") textCapable = true;
      if (interactive && isInteractivePrompt(p)) prompts.add(p);
    }
  };

  if (models.length > 0) {
    for (const m of models) {
      const mInteractive = m.is_interactive === true;
      if (mInteractive) isInteractive = true;
      addPrompts(mInteractive, m.supported_prompts);
      if (mInteractive) {
        interactiveModels.push({
          id: m.id,
          prompts: new Set((m.supported_prompts ?? []).filter(isInteractivePrompt)),
          inputs: new Set(m.supported_inputs ?? []),
          outputs: new Set(m.supported_geometric_outputs ?? []),
        });
      }
      if ((m.supported_trackers?.length ?? 0) > 0) {
        videoModels.push({
          id: m.id,
          trackers: [...(m.supported_trackers ?? [])],
          prompts: new Set(m.supported_prompts ?? []),
          inputs: new Set(m.supported_inputs ?? []),
          outputs: new Set(m.supported_geometric_outputs ?? []),
          textDrivenTrackers: new Set(m.text_driven_trackers ?? []),
          maxWindowFrames:
            typeof m.max_window_frames === "number" &&
            Number.isInteger(m.max_window_frames) &&
            m.max_window_frames > 0
              ? m.max_window_frames
              : null,
        });
      }
      for (const t of m.supported_trackers ?? []) trackers.add(t);
      for (const t of m.text_driven_trackers ?? []) textDrivenTrackers.add(t);
    }
  } else {
    // 老式单 model 后端 (sam3 等): 用顶层字段。is_interactive 缺省视为交互 (向后兼容)。
    const topInteractive = cap.is_interactive !== false;
    if (topInteractive) isInteractive = true;
    addPrompts(topInteractive, cap.supported_prompts);
    if (topInteractive) {
      interactiveModels.push({
        id: "__top__",
        prompts: new Set((cap.supported_prompts ?? []).filter(isInteractivePrompt)),
        inputs: new Set(cap.supported_inputs ?? []),
        outputs: new Set(cap.supported_geometric_outputs ?? []),
      });
    }
    if ((cap.supported_trackers?.length ?? 0) > 0) {
      videoModels.push({
        id: "__top__",
        trackers: [...(cap.supported_trackers ?? [])],
        prompts: new Set(cap.supported_prompts ?? []),
        inputs: new Set(cap.supported_inputs ?? []),
        outputs: new Set(cap.supported_geometric_outputs ?? []),
        textDrivenTrackers: new Set(cap.text_driven_trackers ?? []),
        maxWindowFrames:
          typeof cap.max_window_frames === "number" &&
          Number.isInteger(cap.max_window_frames) &&
          cap.max_window_frames > 0
            ? cap.max_window_frames
            : null,
      });
    }
  }
  for (const t of cap.supported_trackers ?? []) trackers.add(t);
  for (const t of cap.text_driven_trackers ?? []) textDrivenTrackers.add(t);

  return {
    prompts,
    interactiveModels,
    videoModels,
    textCapable,
    isInteractive,
    trackers: [...trackers],
    textDrivenTrackers: [...textDrivenTrackers],
    reachable: true,
  };
}

export type CapIndex = Record<string, BackendCapEntry>;

/**
 * /setup 响应的能力指纹: 只取 buildCapEntry 真正消费的字段 (is_interactive / supported_prompts /
 * supported_trackers), 用于 capSignature 在两次 "ok" 之间内容变化时触发 capIndex 重建。
 * 后端动态宣称能力 (运维改 supported_prompts) 才会变, 否则恒定 → 不引入多余重建。
 */
export function capFingerprint(cap: MLBackendCapability | undefined): string {
  if (!cap) return "";
  const models = Array.isArray(cap.models) ? cap.models : [];
  if (models.length > 0) {
    return models
      .map(
        (m) =>
          `${m.is_interactive ? 1 : 0}/${(m.supported_prompts ?? []).join(",")}/${(m.supported_inputs ?? []).join(",")}/${(m.supported_geometric_outputs ?? []).join(",")}/${(m.supported_trackers ?? []).join(",")}/${(m.text_driven_trackers ?? []).join(",")}/${m.max_window_frames ?? ""}`,
      )
      .join(";");
  }
  return `${cap.is_interactive === false ? 0 : 1}/${(cap.supported_prompts ?? []).join(",")}/${(cap.supported_inputs ?? []).join(",")}/${(cap.supported_geometric_outputs ?? []).join(",")}/${(cap.supported_trackers ?? []).join(",")}/${(cap.text_driven_trackers ?? []).join(",")}/${cap.max_window_frames ?? ""}`;
}

export interface InteractiveRouteRequirement {
  prompt: InteractivePrompt;
  requiredInputs: string[];
  output: string;
}

function entrySupportsRequest(
  entry: BackendCapEntry | undefined,
  requirement: InteractiveRouteRequirement,
): boolean {
  return Boolean(
    entry?.reachable &&
    entry.interactiveModels.some(
      (model) =>
        model.prompts.has(requirement.prompt) &&
        model.outputs.has(requirement.output) &&
        requirement.requiredInputs.every((input) => model.inputs.has(input)),
    ),
  );
}

export function candidatesForRequest(
  capIndex: CapIndex,
  order: string[],
  requirement: InteractiveRouteRequirement,
): string[] {
  return order.filter((id) => entrySupportsRequest(capIndex[id], requirement));
}

export function resolveInteractiveRequest(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
  preferredId: string | null,
  requirement: InteractiveRouteRequirement,
): string | null {
  const candidates = candidatesForRequest(capIndex, order, requirement);
  if (preferredId && candidates.includes(preferredId)) return preferredId;
  if (defaultBackendId && candidates.includes(defaultBackendId)) return defaultBackendId;
  return candidates[0] ?? null;
}

/** reachable 且支持该 prompt 的候选后端 (按注册顺序)。 */
export function candidatesFor(capIndex: CapIndex, order: string[], p: InteractivePrompt): string[] {
  return order.filter((id) => {
    const e = capIndex[id];
    return !!e && e.reachable && e.prompts.has(p);
  });
}

/**
 * 逐 prompt 解析交互后端。顺序: preferred → 项目默认 → 注册序第一个。
 * preferred 初值即项目默认 (见 pickDefaultPreferred), 故未切换时等价"项目默认优先";
 * 用户切过后 preferred 覆盖默认 (否则默认本身交互时选择器失效)。
 */
export function resolveInteractive(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
  preferredId: string | null,
  p: InteractivePrompt,
): string | null {
  const cands = candidatesFor(capIndex, order, p);
  if (cands.length === 0) return null;
  if (preferredId && cands.includes(preferredId)) return preferredId;
  if (defaultBackendId && cands.includes(defaultBackendId)) return defaultBackendId;
  return cands[0];
}

/** preferred 初值: 项目默认 (若它是交互后端) 否则第一个交互后端; 都没有 → null。 */
export function pickDefaultPreferred(
  capIndex: CapIndex,
  order: string[],
  defaultBackendId: string | null,
): string | null {
  if (
    defaultBackendId &&
    capIndex[defaultBackendId]?.reachable &&
    capIndex[defaultBackendId].prompts.size > 0
  ) {
    return defaultBackendId;
  }
  return order.find((id) => capIndex[id]?.reachable && capIndex[id].prompts.size > 0) ?? null;
}

export interface BackendRoutingArgs {
  projectId: string | null | undefined;
  /** 已注册后端 (注册顺序 = list 返回顺序)。 */
  backends: Array<{ id: string; name: string }>;
  /** 项目默认后端 (ml_backend_id)。 */
  defaultBackendId: string | null;
  /**
   * 服务端持久化的交互后端偏好 (按 project, 经 useInteractiveBackendPref 注入)。
   * 替代旧 localStorage `wb:preferred-interactive` (不跨设备 = BUG)。
   */
  savedInteractiveBackendId?: string | null;
  /** 写回服务端偏好 (debounced); 用户切换交互后端时调用。 */
  onSaveInteractiveBackend: (id: string | null) => void;
}

export interface BackendRoutingResult {
  capIndex: CapIndex;
  isLoading: boolean;
  /** True while any enabled setup query is fetching, including retries. */
  isFetching: boolean;
  capabilityErrors: Array<{ backendId: string; backendName: string; message: string }>;
  /** Refetches only failed setup queries without changing backend preferences. */
  retryCapabilities: () => Promise<unknown>;
  /** 工具栏 AI 工具门控: 某 prompt 只要任一交互后端支持就亮 (text → 任一 textCapable)。 */
  isPromptSupported: (type: string) => boolean;
  /** 解析某交互 prompt 实际会跑的后端 (null = 无候选, 工具置灰)。 */
  resolveInteractive: (p: InteractivePrompt) => string | null;
  resolveInteractiveRequest: (requirement: InteractiveRouteRequirement) => string | null;
  /** 某交互 prompt 的候选后端 (按注册序; 选择器只列这些)。 */
  candidatesFor: (p: InteractivePrompt) => string[];
  /** 当前 preferred 交互后端 (用户选定, 缺省 = 项目默认/首个交互)。 */
  preferredInteractiveId: string | null;
  setPreferredInteractiveId: (id: string | null) => void;
}

export function useBackendRouting({
  projectId,
  backends,
  defaultBackendId,
  savedInteractiveBackendId,
  onSaveInteractiveBackend,
}: BackendRoutingArgs): BackendRoutingResult {
  const order = useMemo(() => backends.map((b) => b.id), [backends]);

  const queries = useQueries({
    queries: backends.map((b) => ({
      queryKey: mlBackendSetupQueryKey(projectId, b.id),
      queryFn: () => mlBackendsApi.setup(projectId as string, b.id),
      enabled: !!projectId,
      staleTime: 5 * 60 * 1000,
      retry: 0,
    })),
  });

  const isLoading = !!projectId && queries.some((q) => q.isLoading);
  const isFetching = !!projectId && queries.some((q) => q.isFetching);
  const capabilityErrors = projectId
    ? queries.flatMap((query, index) =>
        query.isError
          ? [
              {
                backendId: backends[index].id,
                backendName: backends[index].name,
                message: apiErrorDetailMessage(query.error) || "模型能力加载失败，请重试",
              },
            ]
          : [],
      )
    : [];
  const retryCapabilities = useCallback(
    () =>
      Promise.all(
        projectId
          ? queries
              .filter((query) => query.isError)
              .map((query) => query.refetch({ cancelRefetch: false }))
          : [],
      ),
    [projectId, queries],
  );

  // capIndex: backendId → 能力条目。query 成功用 data, 失败 (isError) → reachable=false。
  // 注: queries 数组与 backends 同序; 用 join 的稳定签名做依赖, 避免每渲染重建。
  // 签名含 ok 态的能力指纹 → /setup 内容变化 (动态宣称能力) 也会触发 capIndex 重建。
  const capSignature = queries
    .map(
      (q, i) =>
        `${order[i]}:${q.isError ? "err" : q.data ? `ok(${capFingerprint(q.data)})` : "pend"}`,
    )
    .join("|");
  const capIndex = useMemo<CapIndex>(() => {
    const idx: CapIndex = {};
    backends.forEach((b, i) => {
      const q = queries[i];
      idx[b.id] = q?.isError ? buildCapEntry(undefined) : buildCapEntry(q?.data);
    });
    return idx;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capSignature]);

  // preferred: 优先用服务端持久化偏好 (若仍是合法交互候选), 否则按默认规则。
  // 偏好来源从 localStorage 迁到注入的 savedInteractiveBackendId (跨设备)。
  const [preferredOverride, setPreferredOverride] = useState<string | null>(null);
  useEffect(() => {
    setPreferredOverride(savedInteractiveBackendId ?? null);
  }, [savedInteractiveBackendId]);

  const fallbackPreferred = useMemo(
    () => pickDefaultPreferred(capIndex, order, defaultBackendId),
    [capIndex, order, defaultBackendId],
  );
  // override 仅当它仍是某 prompt 的合法交互候选时生效, 否则回落默认规则 (后端被删/不可达自愈)。
  const overrideValid =
    !!preferredOverride &&
    !!capIndex[preferredOverride]?.reachable &&
    capIndex[preferredOverride].prompts.size > 0;
  const preferredInteractiveId = overrideValid ? preferredOverride : fallbackPreferred;

  // useCallback: 引用稳定, 下传到 (可能 memo 化的) InteractiveToolBar 选择器时不致每渲染失效。
  const setPreferredInteractiveId = useCallback(
    (id: string | null) => {
      setPreferredOverride(id);
      onSaveInteractiveBackend(id);
    },
    [onSaveInteractiveBackend],
  );

  return {
    capIndex,
    isLoading,
    isFetching,
    capabilityErrors,
    retryCapabilities,
    isPromptSupported: (type: string) => {
      if (isInteractivePrompt(type)) {
        return candidatesFor(capIndex, order, type).length > 0;
      }
      if (type === "text") {
        return order.some((id) => capIndex[id]?.reachable && capIndex[id].textCapable);
      }
      return false;
    },
    resolveInteractive: (p) =>
      resolveInteractive(capIndex, order, defaultBackendId, preferredInteractiveId, p),
    resolveInteractiveRequest: (requirement) =>
      resolveInteractiveRequest(
        capIndex,
        order,
        defaultBackendId,
        preferredInteractiveId,
        requirement,
      ),
    candidatesFor: (p) => candidatesFor(capIndex, order, p),
    preferredInteractiveId,
    setPreferredInteractiveId,
  };
}
