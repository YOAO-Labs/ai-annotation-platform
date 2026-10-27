import { apiClient } from "./client";
import type { MLBackendResponse } from "@/types";
import type { OutputAttributeSchemaItem } from "./mlCapabilities";
import type { MLBackendUnloadResponse } from "./generated/types.gen";
import type { CocoRle } from "@/pages/Workbench/stage/shared/geometry/maskRle";

// v0.19.0 · ADR-0044 全局 ML 注册表: backend 上提为全局表, 项目层退化为「启用关联 + 变体覆盖」。
// `available` 端点列出全部全局 backend + 本项目启用态/覆盖; 一行 = 一个全局 backend。
// backend 快照与 MLBackendResponse 同构 (全局项 project_id=null, 额外带 health_meta)。
// 注: per-backend 阈值覆盖已退役 (推理参数运行时按 /setup.params 通用渲染)。
export interface ProjectMLBackendItem {
  backend: MLBackendResponse;
  enabled: boolean;
  default_variants: Record<string, unknown> | null;
}

// 切换项目启用 + 写项目级变体覆盖。覆盖项缺省 = 不改动。
export interface ProjectMLBackendEnablementPayload {
  enabled: boolean;
  default_variants?: Record<string, unknown> | null;
}

export interface InteractiveRequest {
  task_id: string;
  context: Record<string, unknown>;
}

/** 交互式推理响应。图片走 interactiveAnnotate、视频当前帧走 interactiveAnnotateFrame，同形状。 */
export interface InteractiveNativeMaskCandidate {
  type: "mask";
  value: {
    rle: CocoRle;
    masklabels: [string];
    preview?: {
      points: [number, number][];
    };
  };
  score: number | null;
  candidate_id: string;
}

export interface InteractiveRoutingLineage {
  requested_backend_id: string;
  backend_pool_id: string | null;
  backend_instance_id: string | null;
  model_id: string | null;
}

export interface InteractiveInferenceLineage {
  model_version: string | null;
  inference_time_ms: number | null;
  cache_hit: boolean | null;
  model_load_ms: number | null;
}

export interface InteractiveMaskDiagnostic {
  reason: string;
  retryable: boolean;
  message?: string | null;
  supported_geometric_outputs?: string[] | null;
}

export interface InteractivePromptSummary {
  family: "point" | "interactive_box" | "exemplar" | "mask" | "scribble" | "correction_frame";
  positive_points: number;
  negative_points: number;
  boxes: number;
  positive_scribbles: number;
  negative_scribbles: number;
  multimask: boolean;
  parameters_digest: string | null;
}

export interface InteractiveAnnotateResponse {
  result: unknown[];
  score: number | null;
  model_version?: string | null;
  inference_time_ms: number | null;
  cache_hit?: boolean | null;
  model_load_ms?: number | null;
  // v0.18.18 · 交互精修 low-res logits 回灌 (base64, 不透明); 前端原样存储、
  // 下次点击经 context.mask_input 回传。仅 multimask=False 的单 mask 精修阶段非空。
  mask_input_next?: string | null;
  diagnostic?: InteractiveMaskDiagnostic | null;
  prompt_revision?: string | null;
  output_geometry?: "polygon" | "mask";
  frame_index?: number | null;
  routing?: InteractiveRoutingLineage;
  prompt_summary?: InteractivePromptSummary | null;
  accept_receipts?: Record<string, string>;
}

export type MLBackendVariant = Record<string, string>;

export interface MLBackendSupportedVariantOption {
  value: string;
  label?: string;
  vram_gb?: number;
  tier?: "fast" | "balanced" | "accurate" | string;
  recommended?: boolean;
  note?: string;
}

export interface MLBackendSupportedVariantGroup {
  key: string;
  title?: string;
  description?: string;
  variants?: MLBackendSupportedVariantOption[];
}

// v0.14.9 · 能力声明协议 v2 — 单个 model 条目 (一个 backend 暴露 N 个).
// `task` 是条目边界 (det/seg/pose/obb/cls/ocr/doc_layout/...), 决定输出几何与项目兼容性;
// `infra` 缺省继承 backend 默认; series/size 复用 supported_variants 多轴.
export interface MLModelCapability {
  id: string;
  display_name?: string;
  task?: string;
  model_family?: string;
  // 原子 vs 内部编排 (协议 v2.2): atom=单次推理原子; composite=一个 model 内部串多原子.
  // 缺省/老 backend = atom. 模型市场据此打「原子/内置流程」徽标; 编排下游 stage 只收 atom.
  composition?: "atom" | "composite";
  infra?: string;
  is_interactive?: boolean;
  supported_prompts?: string[];
  // v0.18.15 · 一等输入契约 (与 supported_prompts 解耦): 模型能吃哪些投递形态
  // (full_image | crop | bbox_prompt | point_prompt | video). 老 backend 缺字段时由平台合成默认.
  // 模型市场「可接受输入」行 + 多阶段编排父子兼容过滤据此判定.
  supported_inputs?: string[];
  default_input_type?: string | null;
  supported_geometric_outputs?: string[];
  output_attribute_types?: string[];
  // v0.18.0 · backend 自报输出属性 schema (含 select options); 二阶段 backend (onnxtools
  // 车辆属性) 声明 /predict 会写哪些 attributes。`/capabilities` 经 ModelCapability 透传。
  // 多阶段预标 StageCard 的「写回属性键」多选据此列选项。老 backend 缺字段 = 无属性输出。
  output_attribute_schema?: OutputAttributeSchemaItem[];
  supported_text_outputs?: string[];
  supported_trackers?: string[];
  // v0.21.19 · text-driven tracker (sam3_video) 子集; propagate 需 text/exemplars。
  text_driven_trackers?: string[];
  max_window_frames?: number | null;
  supported_variants?: MLBackendSupportedVariantGroup[];
  // v0.14.12 · 显式合法组合 (可选): backend 多 axis 非真笛卡尔积时使用. yolo 的
  // (series, size) 受 MODEL_MATRIX 约束 (rtdetr 只有 l/x; v9 detect 仅 t/s/m/c/e),
  // 必须列举合法组合避免目录展示虚假权重. 字段缺省时前端按 axes 笛卡尔积处理.
  // 每条 inner array 与 supported_variants 轴顺序一致, 即 [axis0_value, axis1_value, ...].
  variant_combinations?: string[][];
  // v0.14.12 · True 表示同 backend 内多 task 共享同一份物理权重 (gsam2 风格);
  // False/缺省表示每 task 独立权重 (yolo 风格). 前端列表据此切换渲染策略。
  variants_shared_across_tasks?: boolean;
  // v0.14.13 · backend 自报的默认 variant 组合 (dict[axis_key, value]).
  // 前端 VariantSelector 在用户未选时取此作初值; 优先级:
  // 项目级 project.default_variants[backend_id] > 本字段 > backend 启动 env 默认.
  default_variants?: Record<string, string>;
  default_thresholds?: Record<string, unknown>;
  resource_profile?: Record<string, unknown>;
  params?: { type?: string; properties?: Record<string, unknown> };
  modality?: string;
  // Legacy declarations may be static. Use catalog metadata for checkpoint verification.
  classes?: { index: number; name: string }[];
  // v0.18.23 · exemplar 模型能力声明; 工作台 AI 面板据此渲染 exemplar 控件
  // (negative_box=false → 隐藏负极性按钮; text_combination=false → 隐藏 text 输入)。
  // 缺省 = 全支持 (向后兼容 sam3 现状)。
  exemplar_capabilities?: {
    multi_box?: boolean;
    negative_box?: boolean;
    text_combination?: boolean;
    threshold_refilter?: boolean;
  };
}

// v0.18.29 · backend /setup 上报值的受控词表校验诊断 (越界 task/infra/prompt/geometry).
// 仅 /capabilities (health_meta 派生) 端点带; 原始 /setup 代理不带. 空 = 全合法.
export interface CapabilityWarning {
  level: string; // warning | info
  model_id?: string | null;
  field: string;
  value: string;
  message: string;
}

// v0.10.1 · /setup 协议自描述响应 (与后端 sam3/grounded-sam2 main.py 同构).
// `params` 为 JSON Schema (Draft-07 子集), M2 schema-form 据此渲染参数面板.
export interface MLBackendCapability {
  name: string;
  version?: string;
  protocol_version?: string;
  compat_protocol_versions?: string[];
  model_version?: string;
  // v0.21.x · 视频追踪权重展示名 (sam3.1_multiplex → "SAM 3.1"); 前端「视频权重」条目用。
  video_model_version?: string;
  is_interactive?: boolean;
  labels?: string[];
  supported_prompts: string[];
  supported_inputs?: string[];
  supported_text_outputs?: string[];
  supported_geometric_outputs?: string[];
  // v0.10.36 · 支持的视频 tracker 列表 (如 ["sam2_video"]); 空/缺 = 不支持视频追踪.
  supported_trackers?: string[];
  // v0.21.19 · text-driven tracker (sam3_video) 子集; 前端选中该 tracker 时显 text 框、
  // 未在 supported_trackers 声明时该 tracker 灰置。空/缺 = 无 text-driven tracker。
  text_driven_trackers?: string[];
  max_window_frames?: number | null;
  // v0.10.40 · 变体富元数据; 缺失时前端回落 params.*_variant.enum.
  supported_variants?: MLBackendSupportedVariantGroup[];
  params?: {
    type?: string;
    properties?: Record<string, unknown>;
  };
  // v0.14.9 · 能力声明协议 v2: backend 默认 infra + 多模型目录.
  // 老 backend 缺省时由平台合成「隐式单 model」(见 capabilities 端点).
  infra?: string;
  // v0.14.14 · backend 自报是否支持 POST /warmup (协议 §4.4); 老 backend 缺字段 = false.
  warmup_endpoint?: boolean;
  models?: MLModelCapability[];
  // capabilities 端点 (health_meta 派生) 会带派生模态; 原始 /setup 不带.
  modalities?: string[];
  // v0.18.29 · 受控词表校验诊断 (仅 /capabilities 带); 空/缺 = 全合法.
  warnings?: CapabilityWarning[];
}

/**
 * v0.18.31 · `/setup` 统一 query key。此前全仓三套命名 (ml-backends…setup / ml-backend-setup /
 * ml-capabilities) 打同一个 `setup` 端点 → 同 backend 被缓存成多份 + 重复请求, 且 invalidate
 * 漏失效。统一为 `ml-backends` 前缀, 让 `useMLBackends` 的 `["ml-backends", projectId]` 前缀
 * invalidate 能命中全部 setup 缓存。
 */
export function mlBackendSetupQueryKey(
  projectId: string | null | undefined,
  backendId: string | null | undefined,
) {
  return ["ml-backends", projectId, backendId, "setup"] as const;
}

export const mlBackendsApi = {
  list: (projectId: string) =>
    apiClient.get<MLBackendResponse[]>(`/projects/${projectId}/ml-backends`),

  // v0.19.0 · ADR-0044 · 列出全部全局 backend + 本项目启用态/覆盖 (项目设置启用清单数据源)。
  listAvailable: (projectId: string) =>
    apiClient.get<{ items: ProjectMLBackendItem[] }>(
      `/projects/${projectId}/ml-backends/available`,
    ),

  // v0.19.0 · ADR-0044 · 切换某全局 backend 在本项目的启用态 (+ 可选变体覆盖)。
  setEnablement: (
    projectId: string,
    registryId: string,
    payload: ProjectMLBackendEnablementPayload,
  ) =>
    apiClient.put<ProjectMLBackendItem>(
      `/projects/${projectId}/ml-backends/${registryId}/enablement`,
      payload,
    ),

  setup: (projectId: string, backendId: string) =>
    // 探测式调用：不弹全局 toast（调用方自带错误展示，例如能力目录的黄色降级提示），
    // 避免一个不可达 backend 容器触发 N 个堆叠 toast。
    apiClient.silentGet<MLBackendCapability>(
      `/projects/${projectId}/ml-backends/${backendId}/setup`,
    ),

  // v0.14.9 · 能力目录 (health_meta 派生视图, 含 models[] + infra + modalities).
  capabilities: (projectId: string, backendId: string) =>
    apiClient.silentGet<MLBackendCapability>(
      `/projects/${projectId}/ml-backends/${backendId}/capabilities`,
    ),

  // v0.14.9 · 强制重探 /setup 并刷新能力目录缓存.
  refreshCapabilities: (projectId: string, backendId: string) =>
    apiClient.silentPost<MLBackendCapability>(
      `/projects/${projectId}/ml-backends/${backendId}/capabilities/refresh`,
    ),

  get: (projectId: string, backendId: string) =>
    apiClient.get<MLBackendResponse>(`/projects/${projectId}/ml-backends/${backendId}`),

  health: (projectId: string, backendId: string) =>
    apiClient.post<{ status: string; backend_id: string; backend_name: string }>(
      `/projects/${projectId}/ml-backends/${backendId}/health`,
    ),

  unload: (projectId: string, backendId: string) =>
    apiClient.post<MLBackendUnloadResponse>(
      `/projects/${projectId}/ml-backends/${backendId}/unload`,
    ),

  // v0.10.26 · 可选 variant body 预热指定变体 (模型市场单变体预热); 缺省回退默认变体.
  // v0.10.36 · 可选 taskType ("image" | "video"): "video" 预热独立 video tracker 池 (仅认 sam_variant, 无 dino).
  reload: (
    projectId: string,
    backendId: string,
    variant?: MLBackendVariant,
    modality?: "image" | "video",
  ) =>
    apiClient.post<{
      ok: boolean;
      loaded: boolean;
      reloaded: boolean;
      sam_variant?: string;
      dino_variant?: string;
      modality?: "image" | "video";
    }>(
      `/projects/${projectId}/ml-backends/${backendId}/reload`,
      variant || modality ? { ...(variant ?? {}), ...(modality ? { modality } : {}) } : undefined,
    ),

  predictTest: (projectId: string, backendId: string, taskId: string) =>
    apiClient.post(
      `/projects/${projectId}/ml-backends/${backendId}/predict-test?task_id=${taskId}`,
    ),

  // v0.18.x · 可选 signal: 交互阈值/文本连发时, 新请求 abort 掉被取代的旧请求, 避免后端
  // GPU 洪泛 (见 issue 0002)。被 abort 的请求在前端 fetch 层抛 AbortError, 调用方静默忽略。
  interactiveAnnotate: (
    projectId: string,
    backendId: string,
    payload: InteractiveRequest,
    signal?: AbortSignal,
  ) =>
    apiClient.post<InteractiveAnnotateResponse>(
      `/projects/${projectId}/ml-backends/${backendId}/interactive-annotating`,
      payload,
      signal ? { signal } : undefined,
    ),

  // v0.14.14 协议 §4.4 · POST /warmup 代理. body 各 backend 自定义:
  //   yolo:  { task: "detect", variants: { series: "yolo11", size: "s" } }
  //   gsam2: { variants: { sam_variant: "small", dino_variant: "B" } }
  //   sam3:  {} 或 { variants: { model_variant: "sam3" } }
  warmup: (projectId: string, backendId: string, body?: Record<string, unknown>) =>
    apiClient.post<{
      ok: boolean;
      model_load_ms: number | null;
      cache_hit: boolean;
      evicted: string | null;
    }>(`/projects/${projectId}/ml-backends/${backendId}/warmup`, body ?? {}),

  // v0.21.4 · 视频单题 AI: 对客户端传入的当前帧 JPEG 跑图像 backend, 落单帧 video_bbox 候选。
  // 走 multipart/form-data(不用 apiClient, 其默认 Content-Type=application/json), config 是
  // PreannotateConfig.buildArgs 产出的字段袋(task/model_id/model_variants/prompt/params/
  // class_filter 等), 后端据此构造 predict context。
  predictFrame: async (
    projectId: string,
    backendId: string,
    params: {
      blob: Blob;
      taskId: string;
      frameIndex: number;
      config: Record<string, unknown>;
    },
    signal?: AbortSignal,
  ): Promise<{ prediction_id: string; candidate_count: number; frame_index: number }> => {
    const form = new FormData();
    form.append("frame", params.blob, "frame.jpg");
    form.append("task_id", params.taskId);
    form.append("frame_index", String(params.frameIndex));
    form.append("config", JSON.stringify(params.config));
    const token = localStorage.getItem("token");
    const res = await fetch(
      `/api/v1/projects/${projectId}/ml-backends/${backendId}/predict-frame`,
      {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
        signal,
      },
    );
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `HTTP ${res.status}`);
    }
    return res.json();
  },

  // v0.21.23 · 视频当前帧的**交互式** SAM 提示 (point / interactive_box / exemplar)。
  // 与 interactiveAnnotate 同 context 契约、同响应形状; 差别只在图从哪来 —— 视频 task 的
  // file_path 是整段 mp4, 服务端取不到帧, 故前端把当前帧 JPEG 随 multipart 传入。
  // 走 fetch 而非 apiClient (后者默认 Content-Type=application/json)。
  interactiveAnnotateFrame: async (
    projectId: string,
    backendId: string,
    params: {
      blob: Blob;
      taskId: string;
      frameIndex: number;
      context: Record<string, unknown>;
    },
    signal?: AbortSignal,
  ): Promise<InteractiveAnnotateResponse> => {
    const form = new FormData();
    form.append("frame", params.blob, "frame.jpg");
    form.append("task_id", params.taskId);
    form.append("frame_index", String(params.frameIndex));
    form.append("context", JSON.stringify(params.context));
    const token = localStorage.getItem("token");
    const res = await fetch(
      `/api/v1/projects/${projectId}/ml-backends/${backendId}/interactive-annotating-frame`,
      {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: form,
        ...(signal ? { signal } : {}),
      },
    );
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `HTTP ${res.status}`);
    }
    return res.json();
  },
};
