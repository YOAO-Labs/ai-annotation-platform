import { apiClient } from "./client";
import type { PredictionResponse, AnnotationResponse } from "@/types";

export interface PredictionImportError {
  task_match: Record<string, unknown>;
  reason: string;
}

export interface PredictionImportResult {
  imported: number;
  skipped: number;
  errors: PredictionImportError[];
  dry_run: boolean;
}

export type PredictionImportFormat = "native_json" | "coco" | "yolo";
export type YoloImportVariant = "det" | "obb" | "seg";

export interface PredictionImportOptions {
  modelVersion?: string;
  overwriteExisting?: boolean;
  imageWidth?: number;
  imageHeight?: number;
  yoloVariant?: YoloImportVariant;
}

export type PredictionPurgeSourceScope = "ml_backend" | "external_import" | "all";

export interface PredictionPurgeCounts {
  ml_backend: number;
  external_import: number;
  unknown: number;
  total: number;
}

export interface PredictionPurgeResult {
  source_scope: PredictionPurgeSourceScope;
  task_ids: string[] | null;
  dry_run: boolean;
  counts: PredictionPurgeCounts;
}

export const predictionsApi = {
  listByTask: (
    taskId: string,
    modelVersion?: string,
    minConfidence?: number,
    limit?: number,
    offset?: number,
    init?: RequestInit,
  ) => {
    const params = new URLSearchParams();
    if (modelVersion) params.set("model_version", modelVersion);
    if (minConfidence !== undefined) params.set("min_confidence", String(minConfidence));
    if (limit !== undefined) params.set("limit", String(limit));
    if (offset !== undefined && offset > 0) params.set("offset", String(offset));
    const qs = params.size ? `?${params}` : "";
    return apiClient.get<PredictionResponse[]>(`/tasks/${taskId}/predictions${qs}`, init);
  },

  accept: (
    taskId: string,
    predictionId: string,
    shapeIndex?: number,
    overrideClassName?: string,
    attributeOverrides?: Record<string, unknown>,
    videoSegmentId?: string | null,
    idempotencyKey?: string,
  ) => {
    const qp = new URLSearchParams();
    if (shapeIndex !== undefined) qp.set("shape_index", String(shapeIndex));
    if (videoSegmentId) qp.set("video_segment_id", videoSegmentId);
    if (overrideClassName) qp.set("override_class_name", overrideClassName);
    const qs = qp.toString();
    const body =
      attributeOverrides && Object.keys(attributeOverrides).length > 0
        ? { attribute_overrides: attributeOverrides }
        : undefined;
    return apiClient.post<AnnotationResponse[]>(
      `/tasks/${taskId}/predictions/${predictionId}/accept${qs ? `?${qs}` : ""}`,
      body,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
  },

  reject: (taskId: string, predictionId: string, shapeIndex?: number) => {
    const qs = shapeIndex !== undefined ? `?shape_index=${shapeIndex}` : "";
    return apiClient.post<void>(`/tasks/${taskId}/predictions/${predictionId}/reject${qs}`);
  },

  import: async (
    projectId: string,
    format: PredictionImportFormat,
    file: File | File[],
    options: PredictionImportOptions = {},
    dryRun = false,
  ): Promise<PredictionImportResult> => {
    const params = new URLSearchParams({ format, dry_run: String(dryRun) });
    if (format === "yolo" && options.yoloVariant) {
      params.set("yolo_variant", options.yoloVariant);
    }
    const form = new FormData();
    const files = Array.isArray(file) ? file : [file];
    files.forEach((item) => form.append("file", item));
    if (options.modelVersion) form.append("model_version", options.modelVersion);
    if (options.overwriteExisting !== undefined) {
      form.append("overwrite_existing", String(options.overwriteExisting));
    }
    if (options.imageWidth !== undefined) {
      form.append("image_width", String(options.imageWidth));
    }
    if (options.imageHeight !== undefined) {
      form.append("image_height", String(options.imageHeight));
    }

    const token = localStorage.getItem("token");
    const res = await fetch(`/api/v1/projects/${projectId}/predictions/import?${params}`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `HTTP ${res.status}`);
    }
    return res.json();
  },

  purge: (
    projectId: string,
    payload: {
      source_scope: PredictionPurgeSourceScope;
      task_ids?: string[] | null;
      dry_run?: boolean;
    },
  ) => apiClient.post<PredictionPurgeResult>(`/projects/${projectId}/predictions/purge`, payload),

  importAnnotations: async (
    projectId: string,
    file: File,
    options: { overwrite?: boolean } = {},
    dryRun = false,
  ): Promise<PredictionImportResult> => {
    const params = new URLSearchParams({ format: "native_json", dry_run: String(dryRun) });
    const form = new FormData();
    form.append("file", file);
    if (options.overwrite) form.append("overwrite", "true");

    const token = localStorage.getItem("token");
    const res = await fetch(`/api/v1/projects/${projectId}/annotations/import?${params}`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail ?? `HTTP ${res.status}`);
    }
    return res.json();
  },
};
