import { apiClient } from "./client";
import type { AnnotationResponse, CocoRleMaskRef } from "@/types";

export type VideoTrackerDirection = "forward" | "backward" | "bidirectional";
export type VideoTrackerJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  // 候选/接受流。pending_review = 追踪完、结果暂存待审; accepted = 已落库;
  // discarded = 已丢弃。
  | "pending_review"
  | "partially_reviewed"
  | "accepted"
  | "discarded";

export interface VideoTrackerJob {
  id: string;
  task_id: string;
  dataset_item_id: string;
  annotation_id: string | null;
  segment_id: string | null;
  created_by: string | null;
  status: VideoTrackerJobStatus;
  job_kind?: "tracking" | "correction";
  track_id_snapshot?: string | null;
  correction_frame?: number | null;
  revision?: number;
  review_replayed?: boolean;
  model_key: string;
  direction: VideoTrackerDirection;
  from_frame: number;
  to_frame: number;
  prompt: Record<string, unknown>;
  event_channel: string;
  celery_task_id: string | null;
  cancel_requested_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface VideoMaskCorrectionPayload {
  correction_frame: number;
  from_frame: number;
  to_frame: number;
  model_key: string;
  model_id: string;
  backend_id: string;
  direction: VideoTrackerDirection;
  segment_id?: string | null;
  source_annotation_version: number;
  corrected_mask_digest: string;
  allow_bbox_fallback?: boolean;
  text?: string;
  sam_variant?: string;
}

export interface VideoSegment {
  id: string;
  start_frame: number;
  end_frame: number;
  work_start_frame: number;
  work_end_frame: number;
  segment_index: number;
  status: "open" | "assigned" | "locked" | "completed";
  assignee_id?: string | null;
  locked_by?: string | null;
  locked_at?: string | null;
  lock_expires_at?: string | null;
}

export interface VideoSegmentsResponse {
  task_id: string | null;
  dataset_item_id: string;
  segment_size_frames: number;
  collaboration_enabled: boolean;
  overlap_frames: number;
  segments: VideoSegment[];
}

export interface VideoTrackQualityIssue {
  id: string;
  left_annotation_id: string | null;
  right_annotation_id: string | null;
  code: string;
  frame_start: number;
  frame_end: number;
  metric: Record<string, number>;
}

export interface VideoTrackQualityRun {
  id: string;
  task_id: string;
  left_segment_id: string;
  right_segment_id: string;
  async_job_id: string | null;
  status: string;
  progress_pct: number;
  input_digest: string;
  sampling_digest: string;
  fragments: Array<{
    annotation_id: string;
    segment_id: string;
    class_name: string;
    tool_unit_id: string | null;
    track_id: string | null;
    geometry_family: string | null;
  }>;
  metrics: Record<string, unknown>;
  pairs: Array<{
    left_annotation_id: string;
    right_annotation_id: string;
    class_name: string;
    geometry_family: string;
    matched_frames: number;
    suggestion: "same_track" | "different_track";
    decision: "same_track" | "different_track";
  }>;
  error_message: string | null;
  accepted_by_id: string | null;
  accepted_at: string | null;
  stale_at: string | null;
  created_at: string;
  issues: VideoTrackQualityIssue[];
}

/** text-driven 追踪的视觉示例框 (归一化 xyxy)。复用 sam3 图片侧 Exemplar 形状。 */
export interface VideoTrackerExemplar {
  bbox: [number, number, number, number];
  label?: boolean;
}

export interface VideoTrackerPropagatePayload {
  from_frame: number;
  to_frame: number;
  model_key: string;
  direction: VideoTrackerDirection;
  segment_id?: string | null;
  prompt?: Record<string, unknown>;
  // SAM 模型尺寸 (tiny/small/base_plus/large); 省略时后端回退 tiny。
  sam_variant?: string;
  // text-driven 追踪 (sam3_video) 的文本 query + 可选视觉示例框。
  text?: string;
  exemplars?: VideoTrackerExemplar[];
  output_geometry?: "bbox" | "polygon" | "mask";
  // 无源检测 (画布级发起): source_annotation_id 缺省 = 无源, 新建轨迹类别
  // 由 target_class_name/target_tool_unit_id 显式指定。有源延展时留空 (从 path / 继承源)。
  source_annotation_id?: string | null;
  target_class_name?: string | null;
  target_tool_unit_id?: string | null;
  // M2 · 多选批量: ≥2 条已有轨迹一次延展 (单 job 多源)。后端逐源读当前帧几何构
  // prompt.seeds[] (obj_id + source_annotation_id), 各回填各自源。给出时 track 走多源分支。
  source_annotation_ids?: string[] | null;
}

/** 候选预览: job 暂存的逐帧结果, 供接受前渲染候选叠加。 */
export type VideoTrackerPreviewGeometry =
  | { type: "bbox"; x: number; y: number; w: number; h: number }
  | { type: "polygon"; points: [number, number][] }
  | {
      type: "mask";
      mask: CocoRleMaskRef;
      bbox?: { x: number; y: number; w: number; h: number };
    };

export interface VideoTrackerPreviewResult {
  frame_index: number;
  geometry: VideoTrackerPreviewGeometry;
  confidence?: number | null;
  outside?: boolean;
  instance_id?: string | null;
  primary?: boolean;
  candidate_key?: string;
  geometry_digest?: string;
  source_annotation_id?: string | null;
  target_annotation_id?: string | null;
  manual_protected?: boolean;
}

export interface VideoTrackerJobPreview {
  job_id: string;
  status: VideoTrackerJobStatus;
  annotation_id: string | null;
  job_kind?: "tracking" | "correction";
  correction_frame?: number | null;
  direction?: VideoTrackerDirection | null;
  from_frame?: number;
  to_frame?: number;
  fallback_reason?: string | null;
  seed_mode?: "native_mask" | "bbox" | null;
  protect_manual?: boolean;
  results: VideoTrackerPreviewResult[];
  grid_step: number;
  output_geometry: string;
  job_revision?: number;
  expected_source_versions?: Record<string, number>;
  candidate_total?: number;
  candidate_pending?: number;
  candidate_accepted?: number;
  candidate_rejected?: number;
}

interface VideoTrackerDecisionCommon {
  decision: "accept" | "reject";
  expected_source_versions: Record<string, number>;
  job_revision: number;
  override_manual?: boolean;
}

export type VideoTrackerDecisionPayload = VideoTrackerDecisionCommon &
  (
    | {
        instance_ids: string[];
        from_frame: number;
        to_frame: number;
        qc_issue_id?: never;
        candidate_digest?: never;
      }
    | {
        qc_issue_id: string;
        candidate_digest: string;
        instance_ids?: never;
        from_frame?: never;
        to_frame?: never;
      }
  );

export const videoTrackerApi = {
  segments: (taskId: string, init?: RequestInit) =>
    apiClient.get<VideoSegmentsResponse>(`/tasks/${taskId}/video/segments`, init),
  claimSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:claim`, {}),
  heartbeatSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:heartbeat`, {}),
  releaseSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:release`, {}),
  submitSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:submit`, {}),
  reopenSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:reopen`, {}),
  unassignSegment: (taskId: string, segmentId: string) =>
    apiClient.post<VideoSegment>(`/tasks/${taskId}/video/segments/${segmentId}:unassign`, {}),
  trackQuality: (taskId: string, init?: RequestInit) =>
    apiClient.get<VideoTrackQualityRun[]>(`/tasks/${taskId}/video/track-quality`, init),
  trackQualityDetail: (taskId: string, runId: string, init?: RequestInit) =>
    apiClient.get<VideoTrackQualityRun>(`/tasks/${taskId}/video/track-quality/${runId}`, init),
  runTrackQuality: (taskId: string, leftSegmentId: string, rightSegmentId: string) =>
    apiClient.post<VideoTrackQualityRun>(`/tasks/${taskId}/video/track-quality/run`, {
      left_segment_id: leftSegmentId,
      right_segment_id: rightSegmentId,
    }),
  acceptTrackQuality: (
    taskId: string,
    runId: string,
    payload: {
      input_digest: string;
      pairs: Array<{
        left_annotation_id: string;
        right_annotation_id: string;
        decision: "same_track" | "different_track";
      }>;
    },
  ) =>
    apiClient.post<VideoTrackQualityRun>(
      `/tasks/${taskId}/video/track-quality/${runId}:accept`,
      payload,
    ),
  saveMaskKeyframe: (
    taskId: string,
    annotationId: string,
    frameIndex: number,
    mask: CocoRleMaskRef,
    sourceVersion: number,
    metadata?: {
      source?: "manual" | "prediction";
      occluded?: boolean;
      attributes?: Record<string, unknown> | null;
    },
  ) =>
    apiClient.put<AnnotationResponse>(
      `/tasks/${taskId}/video/tracks/${annotationId}/mask-keyframes/${frameIndex}`,
      {
        mask,
        source: metadata?.source ?? "manual",
        occluded: metadata?.occluded ?? false,
        ...(metadata?.attributes !== undefined ? { attributes: metadata.attributes } : {}),
      },
      { headers: { "If-Match": `W/"${sourceVersion}"` } },
    ),
  operateMaskKeyframe: (
    taskId: string,
    annotationId: string,
    frameIndex: number,
    operation: "delete_keyframe" | "mark_outside" | "restore_held",
    sourceVersion: number,
  ) =>
    apiClient.patch<AnnotationResponse>(
      `/tasks/${taskId}/video/tracks/${annotationId}/mask-keyframes/${frameIndex}`,
      { operation },
      { headers: { "If-Match": `W/"${sourceVersion}"` } },
    ),
  correct: (taskId: string, annotationId: string, payload: VideoMaskCorrectionPayload) =>
    apiClient.post<VideoTrackerJob>(
      `/tasks/${taskId}/video/tracks/${annotationId}/correction-jobs`,
      payload,
    ),
  propagate: (taskId: string, annotationId: string, payload: VideoTrackerPropagatePayload) =>
    apiClient.post<VideoTrackerJob>(
      `/tasks/${taskId}/video/tracks/${annotationId}:propagate`,
      payload,
    ),
  // 任务级追踪 (画布级入口, 源可选): payload.source_annotation_id 给出即延展
  // 该轨迹, 缺省则为无源检测。
  track: (taskId: string, payload: VideoTrackerPropagatePayload) =>
    apiClient.post<VideoTrackerJob>(`/tasks/${taskId}/video:track`, payload),
  get: (jobId: string) => apiClient.get<VideoTrackerJob>(`/video-tracker-jobs/${jobId}`),
  reviewable: (taskId: string) =>
    apiClient.get<VideoTrackerJob[]>(`/tasks/${taskId}/video/tracker-jobs/reviewable`),
  // 刷新后重连: 该 task 下仍在运行 (queued/running) 的追踪任务。
  active: (taskId: string) =>
    apiClient.get<VideoTrackerJob[]>(`/tasks/${taskId}/video/tracker-jobs/active`),
  cancel: (jobId: string) => apiClient.delete<VideoTrackerJob>(`/video-tracker-jobs/${jobId}`),
  // 候选/接受流。
  preview: (jobId: string) =>
    apiClient.get<VideoTrackerJobPreview>(`/video-tracker-jobs/${jobId}/preview`),
  maskContent: (jobId: string, sha256: string) =>
    apiClient.get<import("@/pages/Workbench/stage/shared/geometry/maskRle").CocoRle>(
      `/video-tracker-jobs/${jobId}/mask-content/${sha256}`,
    ),
  accept: (jobId: string) =>
    apiClient.post<VideoTrackerJob>(`/video-tracker-jobs/${jobId}/accept`, {}),
  discard: (jobId: string) =>
    apiClient.post<VideoTrackerJob>(`/video-tracker-jobs/${jobId}/discard`, {}),
  decide: (jobId: string, payload: VideoTrackerDecisionPayload) =>
    apiClient.post<VideoTrackerJob>(`/video-tracker-jobs/${jobId}/decisions`, payload),
};
