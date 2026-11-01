import { apiClient } from "./client";
import type { PredictionResponse, AnnotationResponse } from "@/types";

/** v0.10.15 · 外部预测导入端点响应 (与后端 ImportResult 对齐). */
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

export type PredictionImportFormat = "aap_json" | "coco" | "yolo";
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

  /**
   * 采纳预测.
   * - shapeIndex 给定: 仅采纳指定 shape (画布单点采纳, 避免波及同 prediction 下其它框).
   * - 不传:           采纳整条 prediction 的所有 shape ("全部采纳"按钮).
   */
  accept: (
    taskId: string,
    predictionId: string,
    shapeIndex?: number,
    overrideClassName?: string,
    attributeOverrides?: Record<string, unknown>,
    videoSegmentId?: string | null,
  ) => {
    const qp = new URLSearchParams();
    if (shapeIndex !== undefined) qp.set("shape_index", String(shapeIndex));
    if (videoSegmentId) qp.set("video_segment_id", videoSegmentId);
    // v0.14.17 · 采纳时选类: 预测类名不在项目标签集 (会 422) 时, 带上人选的项目标签重试.
    if (overrideClassName) qp.set("override_class_name", overrideClassName);
    const qs = qp.toString();
    // v0.18.3 · 采纳前在工作台审阅候选属性时改过的值, 经 body.attribute_overrides 原子落库.
    const body =
      attributeOverrides && Object.keys(attributeOverrides).length > 0
        ? { attribute_overrides: attributeOverrides }
        : undefined;
    return apiClient.post<AnnotationResponse[]>(
      `/tasks/${taskId}/predictions/${predictionId}/accept${qs ? `?${qs}` : ""}`,
      body,
    );
  },

  /**
   * B-37 · 驳回预测 shape, 持久化到后端 (rejected_shape_indexes 数组),
   * 避免刷新后 AI 待审框重新出现.
   * - shapeIndex 给定: 仅驳回指定 shape.
   * - 不传:           驳回整条 prediction 的全部 shape.
   */
  reject: (taskId: string, predictionId: string, shapeIndex?: number) => {
    const qs = shapeIndex !== undefined ? `?shape_index=${shapeIndex}` : "";
    return apiClient.post<void>(`/tasks/${taskId}/predictions/${predictionId}/reject${qs}`);
  },

  /**
   * v0.10.15 · 外部模型预测导入 (AAP JSON v1.0 / COCO / YOLO zip).
   * - 走 multipart/form-data, 不用 apiClient (其默认 Content-Type=application/json).
   * - dryRun=true: 仅校验不入库; 用于 Wizard 第 2 步预览.
   * - overwriteExisting=true: 替换 task 已有 source='external_import' 的 predictions.
   */
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

  /**
   * v0.10.54 · 导入 AAP JSON 的 annotations[] (ADR-0028).
   * - 仅支持 aap_json; geometry 透传内部格式。
   * - overwrite=true: 只清该 task 之前导入的标注 (attributes._imported), 不碰人工标注。
   */
  importAnnotations: async (
    projectId: string,
    file: File,
    options: { overwrite?: boolean } = {},
    dryRun = false,
  ): Promise<PredictionImportResult> => {
    const params = new URLSearchParams({ format: "aap_json", dry_run: String(dryRun) });
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
