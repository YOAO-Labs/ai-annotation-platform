import { apiClient } from "./client";
import type { MLBackendResponse } from "@/types";
import type { OutputAttributeSchemaItem } from "./mlCapabilities";
import type { MLBackendUnloadResponse } from "./generated/types.gen";
import type { CocoRle } from "@/pages/Workbench/stage/shared/geometry/maskRle";

export interface ProjectMLBackendItem {
  backend: MLBackendResponse;
  enabled: boolean;
  default_variants: Record<string, unknown> | null;
}

export interface ProjectMLBackendEnablementPayload {
  enabled: boolean;
  default_variants?: Record<string, unknown> | null;
}

export interface InteractiveRequest {
  task_id: string;
  context: Record<string, unknown>;
}

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

export interface MLModelCapability {
  id: string;
  display_name?: string;
  task?: string;
  model_family?: string;
  composition?: "atom" | "composite";
  infra?: string;
  is_interactive?: boolean;
  supported_prompts?: string[];
  supported_inputs?: string[];
  default_input_type?: string | null;
  supported_geometric_outputs?: string[];
  output_attribute_types?: string[];
  output_attribute_schema?: OutputAttributeSchemaItem[];
  supported_text_outputs?: string[];
  supported_trackers?: string[];
  text_driven_trackers?: string[];
  max_window_frames?: number | null;
  supported_variants?: MLBackendSupportedVariantGroup[];
  variant_combinations?: string[][];
  variants_shared_across_tasks?: boolean;
  default_variants?: Record<string, string>;
  default_thresholds?: Record<string, unknown>;
  resource_profile?: Record<string, unknown>;
  params?: { type?: string; properties?: Record<string, unknown> };
  modality?: string;
  classes?: { index: number; name: string }[];
  exemplar_capabilities?: {
    multi_box?: boolean;
    negative_box?: boolean;
    text_combination?: boolean;
    threshold_refilter?: boolean;
  };
}

export interface CapabilityWarning {
  level: string;
  model_id?: string | null;
  field: string;
  value: string;
  message: string;
}

export interface MLBackendCapability {
  name: string;
  version?: string;
  protocol_version?: string;
  compat_protocol_versions?: string[];
  model_version?: string;
  video_model_version?: string;
  is_interactive?: boolean;
  labels?: string[];
  supported_prompts: string[];
  supported_inputs?: string[];
  supported_text_outputs?: string[];
  supported_geometric_outputs?: string[];
  supported_trackers?: string[];
  text_driven_trackers?: string[];
  max_window_frames?: number | null;
  supported_variants?: MLBackendSupportedVariantGroup[];
  params?: {
    type?: string;
    properties?: Record<string, unknown>;
  };
  infra?: string;
  warmup_endpoint?: boolean;
  models?: MLModelCapability[];
  modalities?: string[];
  warnings?: CapabilityWarning[];
}

export function mlBackendSetupQueryKey(
  projectId: string | null | undefined,
  backendId: string | null | undefined,
) {
  return ["ml-backends", projectId, backendId, "setup"] as const;
}

export const mlBackendsApi = {
  list: (projectId: string) =>
    apiClient.get<MLBackendResponse[]>(`/projects/${projectId}/ml-backends`),

  listAvailable: (projectId: string) =>
    apiClient.get<{ items: ProjectMLBackendItem[] }>(
      `/projects/${projectId}/ml-backends/available`,
    ),

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
    apiClient.silentGet<MLBackendCapability>(
      `/projects/${projectId}/ml-backends/${backendId}/setup`,
    ),

  capabilities: (projectId: string, backendId: string) =>
    apiClient.silentGet<MLBackendCapability>(
      `/projects/${projectId}/ml-backends/${backendId}/capabilities`,
    ),

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

  warmup: (projectId: string, backendId: string, body?: Record<string, unknown>) =>
    apiClient.post<{
      ok: boolean;
      model_load_ms: number | null;
      cache_hit: boolean;
      evicted: string | null;
    }>(`/projects/${projectId}/ml-backends/${backendId}/warmup`, body ?? {}),

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
