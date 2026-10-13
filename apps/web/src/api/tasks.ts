import { apiClient } from "./client";
import type {
  TaskResponse,
  AnnotationResponse,
  AnnotationPageResponse,
  TaskLockResponse,
  ReviewClaimResponse,
  Geometry,
  TaskVideoFrameTimetableResponse,
  TaskVideoManifestResponse,
  VideoManifestV2Response,
  TaskPointCloudManifestResponse,
  NeighborsResponse,
  NeighborAnnotationsResponse,
  VideoFrameOut,
  VideoFramePrefetchResponse,
} from "@/types";
import type {
  CameraAnnotationMemberCreate,
  CameraAnnotationMemberDelete,
  CameraAnnotationMemberList,
  CameraAnnotationMemberOut,
  CameraAnnotationMemberRestore,
  CameraAnnotationMemberUpdate,
  ImagePyramidRetryResponse,
  SceneTimelineResponse,
  SensorCalibrationHistoryOut,
  SensorCalibrationRevisionOut,
  SensorCalibrationUpdate,
  TaskMaskCapabilitiesResponse,
} from "./generated";
import type {
  ImagePyramidAssetRequest,
  ImagePyramidAssetUrlsResponse,
  ImagePyramidResponse,
} from "@/pages/Workbench/stage/imagePyramid";

/** v0.20.11 · 选中框单框二次推理请求: 在选中框 ROI 上跑一个能力。 */
export interface SecondaryInferenceRequest {
  ml_backend_id: string;
  /** attributes → 写回原框; geometry → 建子框。 */
  write_target?: "attributes" | "geometry";
  write_keys?: string[] | null;
  label?: string | null;
  model_id?: string | null;
  model_variants?: Record<string, string> | null;
  params?: Record<string, unknown> | null;
  task_type?: string | null;
  prompt?: string | null;
  class_filter?: number[] | null;
  pad?: number;
}

/** v0.20.11 · 二次推理响应: 更新后的原框 + 新建子框(几何型)。 */
export interface SecondaryInferenceResponse {
  annotation: AnnotationResponse;
  created_children: AnnotationResponse[];
}

/** v0.14.1 · 跨帧 propagate 响应: 复制到目标 task 的新 annotation。 */
export interface PropagateResponse {
  annotation: AnnotationResponse;
  /** v0.15.1 · true=已按 ego pose 做运动补偿; false=原样复制(scene 无轨迹等)。 */
  motion_compensated: boolean;
}

/** v0.15.1 · 批量跨帧延续响应。 */
export interface PropagateBatchResponse {
  items: { source_annotation_id: string; annotation: AnnotationResponse }[];
  motion_compensated: boolean;
}

/** v0.15.1 · 区间插值响应。 */
export interface InterpolateRangeResponse {
  annotations: AnnotationResponse[];
  motion_compensated: boolean;
  /** 已有同 group 标注而被幂等跳过的中间帧。 */
  skipped_frames: number[];
}

export interface TaskListResponse {
  items: TaskResponse[];
  // v0.11.30 · 仅首页返回精确总数；cursor 翻页时为 null（前端复用首页值）。
  total: number | null;
  limit: number;
  offset: number;
  next_cursor?: string | null;
}

export interface TaskListParams {
  status?: string;
  assignee_id?: string;
  batch_id?: string;
  // v0.12.0 · true = 只返回 batch_id IS NULL（未归类）任务
  unbatched?: boolean;
  // v0.12.6 (A3) · 绩效页 reject/类别维度下钻过滤
  reject_reason_type?: string;
  class_name?: string;
  limit?: number;
  offset?: number;
  cursor?: string;
}

export interface AnnotationPayload {
  /** Offline mutation identity; stripped from JSON and sent as Idempotency-Key. */
  client_request_id?: string;
  video_segment_id?: string | null;
  annotation_type?: string;
  /** v0.10.17 · 工具维度绑定; service 层据此校验 class_name 在对应 unit 类别集内. */
  tool_unit_id?: string;
  class_name: string;
  geometry: Geometry;
  confidence?: number;
  parent_prediction_id?: string;
  lead_time?: number;
  attributes?: Record<string, unknown>;
}

export interface AnnotationUpdatePayload {
  geometry?: Geometry;
  class_name?: string;
  confidence?: number;
  attributes?: Record<string, unknown>;
  // v0.10.5 M4-β · shape 状态位（I15）。
  z_order?: number;
  is_locked?: boolean;
  is_hidden?: boolean;
}

export interface VideoTrackConvertToBboxesPayload {
  operation: "copy" | "split";
  scope: "frame" | "track";
  frame_index?: number;
  frame_mode?: "keyframes" | "all_frames";
}

export interface VideoTrackConvertToBboxesResponse {
  source_annotation: AnnotationResponse | null;
  created_annotations: AnnotationResponse[];
  deleted_source: boolean;
  removed_frame_indexes: number[];
}

export interface VideoTrackCompositionPayload {
  operation: "aggregate_bboxes" | "split_track" | "merge_tracks" | "join_tracks";
  annotation_ids: string[];
  frame_index?: number;
  delete_sources?: boolean;
  // v0.10.30 · 2.5 join: gap 填充模式 (interpolate 线性过渡 / outside 标记 gap 区为消失)。
  gap_mode?: "interpolate" | "outside";
}

export interface VideoTrackCompositionResponse {
  operation: "aggregate_bboxes" | "split_track" | "merge_tracks" | "join_tracks";
  updated_annotations: AnnotationResponse[];
  created_annotations: AnnotationResponse[];
  deleted_annotation_ids: string[];
}

export interface PointCloudTrackSummary {
  track_id: string;
  class_name: string;
  member_count: number;
  first_frame: number;
  last_frame: number;
}

export type PointCloudTrackOperationRequest =
  | {
      operation: "split";
      primary_track_id: string;
      split_after_frame: number;
    }
  | {
      operation: "merge";
      primary_track_id: string;
      secondary_track_id: string;
    };

export interface PointCloudTrackOperationCandidates {
  contract_version: 1;
  primary: PointCloudTrackSummary;
  candidates: PointCloudTrackSummary[];
  truncated: boolean;
}

export interface PointCloudTrackOperationPreview {
  contract_version: 1;
  operation: "split" | "merge";
  scene_id: string;
  scene_name: string | null;
  primary: PointCloudTrackSummary;
  secondary: PointCloudTrackSummary | null;
  survivor_track_id: string;
  affected_member_count: number;
  rewritten_member_count: number;
  snapshot_token: string;
}

export interface PointCloudTrackOperationResult extends PointCloudTrackOperationPreview {
  created_track_id: string | null;
  updated_member_count: number;
}

export interface SceneTrackInterval {
  id: string;
  start_frame: number;
  end_frame: number | null;
  source: "legacy_envelope" | "manual" | "imported" | "derived";
  version: number;
}

export interface SceneTrackDetail {
  contract_version: 1;
  id: string;
  project_id: string;
  scene_id: string;
  scene_name: string | null;
  track_id: string;
  class_name: string;
  presence_mode: "inferred" | "explicit";
  attributes: Record<string, unknown>;
  attributes_meta: Record<string, unknown>;
  revision: number;
  retired_at: string | null;
  current_frame: number;
  intervals: SceneTrackInterval[];
  members: {
    total: number;
    by_temporal_role: Record<string, number>;
    by_source: Record<string, number>;
    keyframe_frames: number[];
    derived_frames: number[];
    sample_frames: number[];
  };
  available_commands: Array<"split" | "merge" | "mark_absent" | "resume" | "terminate" | "revert">;
}

export type SceneTrackCommandKind =
  | "split"
  | "merge"
  | "mark_absent"
  | "resume"
  | "terminate"
  | "revert";

export interface SceneTrackCommandRequest {
  kind: Exclude<SceneTrackCommandKind, "revert">;
  track_id: string;
  secondary_track_id?: string;
  frame_index?: number;
  resume_frame?: number;
  source_annotation_id?: string;
  confirm_member_deactivation?: boolean;
}

export interface SceneTrackCommandPreview {
  contract_version: 1;
  kind: SceneTrackCommandKind;
  scene_id: string;
  scene_name: string | null;
  track_id: string;
  secondary_track_id: string | null;
  frame_index: number | null;
  resume_frame: number | null;
  source_revisions: Record<string, number>;
  before_intervals: Record<string, SceneTrackInterval[]>;
  after_intervals: Record<string, SceneTrackInterval[]>;
  affected_members: {
    total: number;
    by_temporal_role: Record<string, number>;
    frames: number[];
    requires_confirmation: boolean;
  };
  snapshot_token: string;
}

export interface SceneTrackCommandResult extends SceneTrackCommandPreview {
  operation_id: string;
  status: "committed" | "reverted";
  created_track_id: string | null;
  result_revisions: Record<string, number>;
}

export interface SceneTrackOperationItem {
  id: string;
  kind: SceneTrackCommandKind;
  status: "committed" | "reverted";
  created_at: string;
  completed_at: string;
  response: SceneTrackCommandResult;
}

export interface VideoFrameTimetableParams {
  from?: number;
  to?: number;
}

export interface VideoFrameParams {
  format?: "webp" | "jpeg";
  width?: number;
}

export interface SubmitResponse {
  status: string;
  task_id: string;
}

export const tasksApi = {
  listByProject: (projectId: string, params?: TaskListParams, init?: RequestInit) => {
    const q = new URLSearchParams({ project_id: projectId });
    if (params?.status) q.set("status", params.status);
    if (params?.assignee_id) q.set("assignee_id", params.assignee_id);
    if (params?.batch_id) q.set("batch_id", params.batch_id);
    if (params?.unbatched) q.set("unbatched", "true");
    if (params?.reject_reason_type) q.set("reject_reason_type", params.reject_reason_type);
    if (params?.class_name) q.set("class_name", params.class_name);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    if (params?.cursor) q.set("cursor", params.cursor);
    return apiClient.get<TaskListResponse>(`/tasks?${q}`, init);
  },

  getNext: (projectId: string, batchId?: string) => {
    const q = new URLSearchParams({ project_id: projectId });
    if (batchId) q.set("batch_id", batchId);
    return apiClient.get<TaskResponse | null>(`/tasks/next?${q}`);
  },

  get: (id: string, init?: RequestInit) => apiClient.get<TaskResponse>(`/tasks/${id}`, init),

  getMaskCapabilities: (id: string, init?: RequestInit) =>
    apiClient.get<TaskMaskCapabilitiesResponse>(`/tasks/${id}/mask-capabilities`, init),

  getImagePyramid: (id: string, init?: RequestInit) =>
    apiClient.silentGet<ImagePyramidResponse>(`/tasks/${id}/image-pyramid`, init),

  getImagePyramidAssetUrls: (id: string, items: ImagePyramidAssetRequest[], init?: RequestInit) =>
    apiClient.post<ImagePyramidAssetUrlsResponse>(
      `/tasks/${id}/image-pyramid/asset-urls`,
      { items },
      init,
    ),

  retryImagePyramid: (id: string) =>
    apiClient.post<ImagePyramidRetryResponse>(`/tasks/${id}/image-pyramid/retry`, {}),

  getVideoManifest: (id: string, init?: RequestInit) =>
    apiClient.get<TaskVideoManifestResponse>(`/tasks/${id}/video/manifest`, init),

  // manifest v2:精确帧 pipeline(WebCodecs)激活时增量查询 chunk_size_frames / dataset_item_id。
  // 实验链路失败会静默回退,不弹全局 toast;signal 让切 task / 卸载能取消旧请求。
  getVideoManifestV2: (id: string, init?: RequestInit) =>
    apiClient.silentGet<VideoManifestV2Response>(`/tasks/${id}/video/manifest-v2`, init),

  getPointCloudManifest: (id: string, init?: RequestInit) =>
    apiClient.get<TaskPointCloudManifestResponse>(`/tasks/${id}/point-cloud/manifest`, init),

  getCameraAnnotationMembers: (
    taskId: string,
    sceneTrackId: string,
    projectionCameraRole?: string | null,
    includeInactive = false,
    init?: RequestInit,
  ) => {
    const query = new URLSearchParams({ scene_track_id: sceneTrackId });
    if (projectionCameraRole) query.set("projection_camera_role", projectionCameraRole);
    if (includeInactive) query.set("include_inactive", "true");
    return apiClient.get<CameraAnnotationMemberList>(
      `/tasks/${taskId}/point-cloud/camera-members?${query}`,
      init,
    );
  },

  createCameraAnnotationMember: (taskId: string, payload: CameraAnnotationMemberCreate) =>
    apiClient.post<CameraAnnotationMemberOut>(
      `/tasks/${taskId}/point-cloud/camera-members`,
      payload,
    ),

  updateCameraAnnotationMember: (
    taskId: string,
    memberId: string,
    payload: CameraAnnotationMemberUpdate,
  ) =>
    apiClient.patch<CameraAnnotationMemberOut>(
      `/tasks/${taskId}/point-cloud/camera-members/${memberId}`,
      payload,
    ),

  deleteCameraAnnotationMember: (
    taskId: string,
    memberId: string,
    payload: CameraAnnotationMemberDelete,
  ) =>
    apiClient.delete<CameraAnnotationMemberOut>(
      `/tasks/${taskId}/point-cloud/camera-members/${memberId}`,
      payload,
    ),

  restoreCameraAnnotationMember: (
    taskId: string,
    memberId: string,
    payload: CameraAnnotationMemberRestore,
  ) =>
    apiClient.post<CameraAnnotationMemberOut>(
      `/tasks/${taskId}/point-cloud/camera-members/${memberId}/restore`,
      payload,
    ),

  getSensorCalibrationHistory: (taskId: string, cameraRole: string, init?: RequestInit) =>
    apiClient.get<SensorCalibrationHistoryOut>(
      `/tasks/${taskId}/point-cloud/cameras/${encodeURIComponent(cameraRole)}/calibration`,
      init,
    ),

  updateSensorCalibration: (taskId: string, cameraRole: string, payload: SensorCalibrationUpdate) =>
    apiClient.patch<SensorCalibrationRevisionOut>(
      `/tasks/${taskId}/point-cloud/cameras/${encodeURIComponent(cameraRole)}/calibration`,
      payload,
    ),

  // v0.14.0 · scene 内前后 k 个邻居 task(跨帧导航 backing)。
  getNeighbors: (id: string, k = 1, init?: RequestInit) =>
    apiClient.get<NeighborsResponse>(`/tasks/${id}/neighbors?k=${k}`, init),

  getSceneTimeline: (
    id: string,
    startFrame: number,
    endFrame: number,
    trackId?: string | null,
    init?: RequestInit,
  ) => {
    const query = new URLSearchParams({
      start_frame: String(startFrame),
      end_frame: String(endFrame),
    });
    if (trackId) query.set("track_id", trackId);
    return apiClient.silentGet<SceneTimelineResponse>(`/tasks/${id}/scene-timeline?${query}`, init);
  },

  // v0.15.17 · 一次性拉 ±k 帧邻帧标注(替代 2k 条并发 getAnnotations + client 过滤)。
  // v0.21.2 · trackId 给定 → 服务端只回该 track(scope=selected);省略 → 回全部(scope=all)。
  getNeighborAnnotations: (id: string, k = 1, trackId?: string | null, init?: RequestInit) => {
    const q = trackId != null ? `?k=${k}&track_id=${encodeURIComponent(trackId)}` : `?k=${k}`;
    return apiClient.get<NeighborAnnotationsResponse>(
      `/tasks/${id}/neighbor-annotations${q}`,
      init,
    );
  },

  // v0.20.11 · 选中框单框二次推理: 在选中框 ROI 上同步跑一个能力, 产物落库
  // (属性写回原框 / 几何建子框, 带 AI 溯源)。
  secondaryInference: (taskId: string, annotationId: string, body: SecondaryInferenceRequest) =>
    apiClient.post<SecondaryInferenceResponse>(
      `/tasks/${taskId}/annotations/${annotationId}/secondary-inference`,
      body,
    ),

  // v0.14.1 · 把源 annotation 跨帧 propagate 到目标 task(同 project 同 scene)。
  propagateToTask: (
    taskId: string,
    annotationId: string,
    targetTaskId: string,
    overridePsr?: Record<string, unknown> | null,
  ) =>
    apiClient.post<PropagateResponse>(
      `/tasks/${taskId}/annotations/${annotationId}/propagate-to-task`,
      { target_task_id: targetTaskId, override_psr: overridePsr ?? null },
    ),

  // v0.15.1 · 批量跨帧延续: 源 task 的多个(annotationIds 给定)或全部
  // (undefined → 全部 active box_3d)运动补偿 propagate 到目标 task。
  propagateBatch: (taskId: string, targetTaskId: string, annotationIds?: string[]) =>
    apiClient.post<PropagateBatchResponse>(`/tasks/${taskId}/annotations/propagate-batch`, {
      target_task_id: targetTaskId,
      annotation_ids: annotationIds ?? null,
    }),

  // v0.15.1 · 关键帧区间插值: 路径 task = 起点帧, 同 track 链两端框之间的
  // 中间帧自动生成插值框(source="interpolated")。v0.21.2 · ADR-0045 · 按 track_id。
  interpolateRange: (taskId: string, trackId: string, toTaskId: string) =>
    apiClient.post<InterpolateRangeResponse>(`/tasks/${taskId}/annotations/interpolate-range`, {
      track_id: trackId,
      to_task_id: toTaskId,
    }),

  getVideoFrameTimetable: (id: string, params?: VideoFrameTimetableParams, init?: RequestInit) => {
    const q = new URLSearchParams();
    if (params?.from !== undefined) q.set("from", String(params.from));
    if (params?.to !== undefined) q.set("to", String(params.to));
    const suffix = q.toString() ? `?${q}` : "";
    return apiClient.get<TaskVideoFrameTimetableResponse>(
      `/tasks/${id}/video/frame-timetable${suffix}`,
      init,
    );
  },

  getVideoFrame: (
    id: string,
    frameIndex: number,
    params?: VideoFrameParams,
    init?: RequestInit,
  ) => {
    const q = new URLSearchParams();
    if (params?.format) q.set("format", params.format);
    if (params?.width !== undefined) q.set("w", String(params.width));
    const suffix = q.toString() ? `?${q}` : "";
    return apiClient.get<VideoFrameOut>(`/tasks/${id}/video/frames/${frameIndex}${suffix}`, init);
  },

  prefetchVideoFrames: (
    id: string,
    frameIndices: number[],
    params?: VideoFrameParams,
    init?: RequestInit,
  ) =>
    apiClient.post<VideoFramePrefetchResponse>(
      `/tasks/${id}/video/frames:prefetch`,
      {
        frame_indices: frameIndices,
        width: params?.width ?? 320,
        format: params?.format ?? "webp",
      },
      init,
    ),

  getAnnotations: (id: string, videoSegmentId?: string | null, init?: RequestInit) => {
    const query = videoSegmentId ? `?video_segment_id=${videoSegmentId}` : "";
    return apiClient.get<AnnotationResponse[]>(`/tasks/${id}/annotations${query}`, init);
  },

  /**
   * Keyset-paged saved annotations (200 per page). Read-only surfaces such as
   * the Data Manager preview use this instead of the one-shot list so large
   * tasks can render progressively; `init` carries the AbortSignal.
   */
  getAnnotationsPage: (
    id: string,
    params: { cursor?: string | null; limit?: number } = {},
    init?: RequestInit,
  ) => {
    const search = new URLSearchParams();
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.cursor) search.set("cursor", params.cursor);
    const query = search.toString();
    return apiClient.get<AnnotationPageResponse>(
      `/tasks/${id}/annotations/page${query ? `?${query}` : ""}`,
      init,
    );
  },

  createAnnotation: (id: string, payload: AnnotationPayload, idempotencyKey?: string) => {
    const { client_request_id, ...body } = payload;
    const key = idempotencyKey ?? client_request_id;
    return apiClient.post<AnnotationResponse>(
      `/tasks/${id}/annotations`,
      body,
      key === undefined ? undefined : { headers: { "Idempotency-Key": key } },
    );
  },

  updateAnnotation: (
    taskId: string,
    annotationId: string,
    payload: AnnotationUpdatePayload,
    etag?: string,
  ) =>
    apiClient.patch<AnnotationResponse>(
      `/tasks/${taskId}/annotations/${annotationId}`,
      payload,
      etag ? { headers: { "If-Match": etag } } : undefined,
    ),

  deleteAnnotation: (taskId: string, annotationId: string) =>
    apiClient.delete<void>(`/tasks/${taskId}/annotations/${annotationId}`),

  convertVideoTrackToBboxes: (
    taskId: string,
    annotationId: string,
    payload: VideoTrackConvertToBboxesPayload,
  ) =>
    apiClient.post<VideoTrackConvertToBboxesResponse>(
      `/tasks/${taskId}/annotations/${annotationId}/video/convert-to-bboxes`,
      payload,
    ),

  composeVideoTracks: (taskId: string, payload: VideoTrackCompositionPayload) =>
    apiClient.post<VideoTrackCompositionResponse>(
      `/tasks/${taskId}/annotations/video/track-compositions`,
      payload,
    ),

  listPointCloudTrackOperationCandidates: (taskId: string, trackId: string, init?: RequestInit) =>
    apiClient.get<PointCloudTrackOperationCandidates>(
      `/tasks/${taskId}/track-operations/candidates?track_id=${encodeURIComponent(trackId)}`,
      init,
    ),

  getSceneTrack: (taskId: string, trackId: string, init?: RequestInit) =>
    apiClient.get<SceneTrackDetail>(
      `/tasks/${taskId}/scene-tracks/${encodeURIComponent(trackId)}`,
      init,
    ),

  previewSceneTrackCommand: (taskId: string, payload: SceneTrackCommandRequest) =>
    apiClient.post<SceneTrackCommandPreview>(
      `/tasks/${taskId}/scene-track-commands/preview`,
      payload,
    ),

  executeSceneTrackCommand: (
    taskId: string,
    payload: SceneTrackCommandRequest & {
      snapshot_token: string;
      idempotency_key: string;
    },
  ) =>
    apiClient.post<SceneTrackCommandResult>(
      `/tasks/${taskId}/scene-track-commands/execute`,
      payload,
    ),

  listSceneTrackOperations: (taskId: string, trackId: string, init?: RequestInit) =>
    apiClient.get<{ contract_version: 1; operations: SceneTrackOperationItem[] }>(
      `/tasks/${taskId}/scene-track-commands?track_id=${encodeURIComponent(trackId)}`,
      init,
    ),

  revertSceneTrackOperation: (taskId: string, operationId: string, idempotencyKey: string) =>
    apiClient.post<SceneTrackCommandResult>(
      `/tasks/${taskId}/scene-track-commands/${operationId}/revert`,
      { idempotency_key: idempotencyKey },
    ),

  previewPointCloudTrackOperation: (taskId: string, payload: PointCloudTrackOperationRequest) =>
    apiClient.post<PointCloudTrackOperationPreview>(
      `/tasks/${taskId}/track-operations/preview`,
      payload,
    ),

  executePointCloudTrackOperation: (
    taskId: string,
    payload: PointCloudTrackOperationRequest & { snapshot_token: string },
  ) => apiClient.post<PointCloudTrackOperationResult>(`/tasks/${taskId}/track-operations`, payload),

  submit: (id: string) => apiClient.post<SubmitResponse>(`/tasks/${id}/submit`),

  // v0.8.7 F7 · 任务跳过
  skip: (
    id: string,
    body: {
      reason: "image_corrupt" | "no_target" | "unclear" | "other";
      note?: string;
    },
  ) =>
    apiClient.post<{
      status: "skipped";
      task_id: string;
      skip_reason: string;
    }>(`/tasks/${id}/skip`, body),

  withdraw: (id: string) => apiClient.post<SubmitResponse>(`/tasks/${id}/withdraw`),

  reopen: (id: string) =>
    apiClient.post<SubmitResponse & { reopened_count: number }>(`/tasks/${id}/reopen`),

  acceptRejection: (id: string) => apiClient.post<SubmitResponse>(`/tasks/${id}/accept-rejection`),

  reviewClaim: (id: string) => apiClient.post<ReviewClaimResponse>(`/tasks/${id}/review/claim`),

  approve: (id: string) =>
    apiClient.post<{ status: string; task_id: string }>(`/tasks/${id}/review/approve`),

  reject: (
    id: string,
    payload: {
      reason_type: "missing" | "extra" | "wrong_label" | "wrong_geometry";
      reason?: string;
    },
  ) =>
    apiClient.post<{
      status: string;
      task_id: string;
      reason_type: string;
      reason: string | null;
    }>(`/tasks/${id}/review/reject`, payload),

  acquireLock: (taskId: string) => apiClient.post<TaskLockResponse>(`/tasks/${taskId}/lock`),

  heartbeatLock: (taskId: string) =>
    apiClient.post<{ status: string }>(`/tasks/${taskId}/lock/heartbeat`),

  releaseLock: (taskId: string) => apiClient.delete<void>(`/tasks/${taskId}/lock`),

  /**
   * v0.6.7 B-13：unmount / 页面跳转期间 release 必须在请求被取消前送达。
   * 用 fetch keepalive 而非常规 client（client 走 fetch 但未开 keepalive，浏览器会在 unload 时取消）。
   * sendBeacon 不支持 Bearer header，所以走 keepalive。
   */
  releaseLockKeepalive: (taskId: string) => {
    const token = localStorage.getItem("token");
    return fetch(`/api/v1/tasks/${taskId}/lock`, {
      method: "DELETE",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    }).catch(() => {});
  },
};
