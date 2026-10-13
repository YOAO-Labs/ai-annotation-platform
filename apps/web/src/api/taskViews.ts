import { apiClient } from "./client";
import type { TaskResponse } from "@/types";

export type TaskViewVisibility = "private" | "project";
export type DataManagerEntityScope = "tasks" | "objects" | "tracks";
export type TaskFilterOp =
  | "eq"
  | "ne"
  | "in"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "exists"
  | "missing"
  | "contains"
  | "between"
  | "contains_any"
  | "contains_all";

export interface TaskFilterRule {
  field: string;
  op: TaskFilterOp;
  value?: unknown;
}

export interface TaskFilterGroup {
  op: "and" | "or";
  rules: Array<TaskFilterRule | TaskFilterGroup>;
}

export interface TaskSortItem {
  field: string;
  direction: "asc" | "desc";
}

export interface ProjectTaskView {
  id: string | null;
  key: string | null;
  project_id: string;
  owner_id: string | null;
  name: string;
  visibility: TaskViewVisibility;
  entity_scope: DataManagerEntityScope;
  filter_json: Record<string, unknown>;
  sort_json: TaskSortItem[];
  columns_json: string[];
  builtin: boolean;
  task_count: number | null;
  result_count: number | null;
  created_at: string | null;
  updated_at: string | null;
  invalid_fields: string[];
}

export interface ProjectTaskViewPayload {
  name: string;
  visibility: TaskViewVisibility;
  entity_scope: DataManagerEntityScope;
  filter_json: Record<string, unknown>;
  sort_json: TaskSortItem[];
  columns_json: string[];
}

export interface ProjectTaskViewUpdatePayload {
  name?: string;
  visibility?: TaskViewVisibility;
  filter_json?: Record<string, unknown>;
  sort_json?: TaskSortItem[];
  columns_json?: string[];
}

export interface DataManagerTask extends TaskResponse {
  effective_assignee?: TaskResponse["assignee"];
  effective_reviewer?: TaskResponse["reviewer"];
  annotation_count: number;
  prediction_count: number;
  avg_prediction_confidence: number | null;
  unresolved_issue_count: number;
  /** Legacy alias of {@link unresolved_issue_count}. */
  unresolved_feedback_count: number;
  comment_count: number;
  model_versions: string[];
  scene_name: string | null;
  frame_index: number | null;
  last_activity_at: string | null;
  annotation_source_counts: Record<string, number>;
  track_count: number;
  pending_prediction_shape_count: number;
  low_confidence_prediction_shape_count: number;
  pending_tracker_job_count: number;
  keyframe_count: number;
  outside_range_count: number;
  camera_count: number;
  calibration_issue_count: number;
  scene_total_frames: number | null;
}

export interface DataManagerOption {
  value: string;
  label: string;
}

export interface DataManagerFilterField {
  key: string;
  label: string;
  group: string;
  value_type: "text" | "number" | "boolean" | "select" | "multiselect" | "datetime";
  operators: TaskFilterOp[];
  options: DataManagerOption[];
  expensive: boolean;
  tool_unit_id: string | null;
  attribute_key: string | null;
}

export interface DataManagerColumn {
  key: string;
  label: string;
  group: string;
  default: boolean;
  expensive: boolean;
  sortable: boolean;
  sort_field: string | null;
}

export interface DataManagerSchema {
  entity_scope: DataManagerEntityScope;
  available_entity_scopes: DataManagerEntityScope[];
  project_kind: { data_type: string; type_key: string; scene_mode: boolean };
  tool_units: Array<{ id: string; classes: string[] }>;
  filter_fields: DataManagerFilterField[];
  columns: DataManagerColumn[];
  default_columns: string[];
  sort_fields: DataManagerOption[];
  metrics: Array<{ key: string; label: string; group: string }>;
  builtin_views: string[];
}

export interface DataManagerSummary {
  scope: { visible_task_total: number; matched_task_total: number };
  task_status: Record<string, number>;
  annotations: {
    total: number;
    single_frame: number;
    tracked: number;
    distinct_tracks: number;
    imported: number;
    by_source: Record<string, number>;
    by_class: Record<string, number>;
    by_tool_unit: Record<string, number>;
    by_type: Record<string, number>;
  };
  ai_review: {
    prediction_shapes: number;
    low_confidence_prediction_shapes: number;
    tracker_jobs: number;
    confidence_threshold: number;
    by_model_version: Record<string, number>;
    confidence_buckets: Record<string, number>;
  };
  unresolved_feedback: number;
  attributes: Array<{
    tool_unit_id: string;
    key: string;
    label: string;
    eligible: number;
    present: number;
    missing: number;
    values: Record<string, number>;
  }>;
  kind_metrics: Record<string, number | null>;
}

export interface DataManagerMatchItem {
  entity_kind: "annotation" | "prediction_shape" | "tracker_job";
  id: string;
  shape_index: number | null;
  track_id: string | null;
  class_name: string | null;
  tool_unit_id: string | null;
  annotation_type: string | null;
  source: string | null;
  attributes: Record<string, unknown>;
  frame_index: number | null;
}

export interface DataManagerMatchesResponse {
  task_id: string;
  items: DataManagerMatchItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ProjectTaskQueryPayload {
  filter_json: Record<string, unknown>;
  sort_json: TaskSortItem[];
  columns_json: string[];
  limit: number;
  offset: number;
}

export interface ProjectTaskQueryResponse {
  items: DataManagerTask[];
  total: number;
  limit: number;
  offset: number;
}

export interface DataManagerEntityLocation {
  project_id: string;
  task_id: string;
  task_display_id: string;
  batch_id: string | null;
  dataset_item_id: string | null;
  data_type: string;
  focus_kind: "annotation" | "track";
  annotation_id: string | null;
  track_id: string | null;
  scene_id: string | null;
  scene_name: string | null;
  scene_frame_index: number | null;
  video_frame_index: number | null;
}

export interface DataManagerEntityFacets {
  matched_total: number;
  task_total: number;
  by_class: Record<string, number>;
  by_source: Record<string, number>;
  by_tool_unit: Record<string, number>;
  by_type: Record<string, number>;
  by_quality: Record<string, number>;
}

export interface DataManagerObject {
  entity_key: string;
  annotation_id: string;
  task_id: string;
  task_display_id: string;
  file_name: string | null;
  batch_id: string | null;
  class_name: string;
  tool_unit_id: string;
  annotation_type: string;
  source: string;
  imported: boolean;
  confidence: number | null;
  track_id: string | null;
  parent_prediction_id: string | null;
  parent_annotation_id: string | null;
  attributes: Record<string, unknown>;
  attribute_origins: Record<string, "ai" | "human">;
  created_by_id: string | null;
  created_by_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  unresolved_feedback_count: number;
  location: DataManagerEntityLocation;
}

export interface DataManagerTrackSources {
  annotation_sources: Record<string, number>;
  keyframe_sources: Record<string, number>;
}

export interface DataManagerTrack {
  entity_key: string;
  track_ref: string;
  track_kind: "compact_video" | "scene";
  track_id: string;
  compact_annotation_id: string | null;
  class_name: string | null;
  tool_unit_id: string | null;
  annotation_type: string | null;
  start_frame: number | null;
  end_frame: number | null;
  span: number | null;
  occurrence_count: number;
  distinct_task_count: number;
  distinct_frame_count: number;
  missing_frame_count: number;
  duplicate_frame_count: number;
  keyframe_count: number;
  outside_range_count: number;
  occluded_count: number;
  sources: DataManagerTrackSources;
  attributes: Record<string, unknown>;
  attribute_origins: Record<string, "ai" | "human">;
  quality_issues: string[];
  location: DataManagerEntityLocation;
}

export interface DataManagerTrackMember {
  annotation_id: string;
  task_id: string;
  task_display_id: string;
  class_name: string;
  source: string;
  frame_index: number | null;
  keyframe_source: string | null;
  occluded: boolean;
  outside: boolean;
  attributes: Record<string, unknown>;
  attribute_origins: Record<string, "ai" | "human">;
  location: DataManagerEntityLocation;
}

export interface DataManagerEntityQueryPayload {
  filter_json: Record<string, unknown>;
  sort_json: TaskSortItem[];
  columns_json: string[];
  limit: number;
  cursor?: string | null;
}

export interface DataManagerObjectQueryResponse {
  items: DataManagerObject[];
  total: number;
  limit: number;
  next_cursor: string | null;
  facets: DataManagerEntityFacets;
}

export interface DataManagerTrackQueryResponse {
  items: DataManagerTrack[];
  total: number;
  limit: number;
  next_cursor: string | null;
  facets: DataManagerEntityFacets;
}

export interface DataManagerTrackDetail {
  track: DataManagerTrack;
  members: DataManagerTrackMember[];
}

export const taskViewsApi = {
  list: (projectId: string, entityScope: DataManagerEntityScope = "tasks", init?: RequestInit) =>
    apiClient.get<{ items: ProjectTaskView[] }>(
      `/projects/${projectId}/task-views?entity_scope=${entityScope}`,
      init,
    ),

  create: (projectId: string, payload: ProjectTaskViewPayload) =>
    apiClient.post<ProjectTaskView>(`/projects/${projectId}/task-views`, payload),

  update: (projectId: string, viewId: string, payload: ProjectTaskViewUpdatePayload) =>
    apiClient.patch<ProjectTaskView>(`/projects/${projectId}/task-views/${viewId}`, payload),

  remove: (projectId: string, viewId: string) =>
    apiClient.delete<void>(`/projects/${projectId}/task-views/${viewId}`),

  copy: (
    projectId: string,
    viewId: string,
    payload: { name?: string; visibility?: TaskViewVisibility },
  ) => apiClient.post<ProjectTaskView>(`/projects/${projectId}/task-views/${viewId}/copy`, payload),

  query: (projectId: string, payload: ProjectTaskQueryPayload, init?: RequestInit) =>
    apiClient.post<ProjectTaskQueryResponse>(`/projects/${projectId}/tasks/query`, payload, init),

  schema: (projectId: string, entityScope: DataManagerEntityScope = "tasks", init?: RequestInit) =>
    apiClient.get<DataManagerSchema>(
      `/projects/${projectId}/data-manager/schema?entity_scope=${entityScope}`,
      init,
    ),

  summary: (projectId: string, filterJson: Record<string, unknown>, init?: RequestInit) =>
    apiClient.post<DataManagerSummary>(
      `/projects/${projectId}/data-manager/summary`,
      {
        filter_json: filterJson,
      },
      init,
    ),

  matches: (
    projectId: string,
    taskId: string,
    filterJson: Record<string, unknown>,
    init?: RequestInit,
  ) =>
    apiClient.post<DataManagerMatchesResponse>(
      `/projects/${projectId}/tasks/${taskId}/data-manager/matches`,
      { filter_json: filterJson, limit: 100, offset: 0 },
      init,
    ),

  queryObjects: (projectId: string, payload: DataManagerEntityQueryPayload, init?: RequestInit) =>
    apiClient.post<DataManagerObjectQueryResponse>(
      `/projects/${projectId}/data-manager/objects/query`,
      payload,
      init,
    ),

  objectDetail: (projectId: string, annotationId: string, init?: RequestInit) =>
    apiClient.get<{ item: DataManagerObject }>(
      `/projects/${projectId}/data-manager/objects/${annotationId}/detail`,
      init,
    ),

  objectLocation: (projectId: string, annotationId: string, init?: RequestInit) =>
    apiClient.get<DataManagerEntityLocation>(
      `/projects/${projectId}/data-manager/objects/${annotationId}/location`,
      init,
    ),

  queryTracks: (projectId: string, payload: DataManagerEntityQueryPayload, init?: RequestInit) =>
    apiClient.post<DataManagerTrackQueryResponse>(
      `/projects/${projectId}/data-manager/tracks/query`,
      payload,
      init,
    ),

  trackDetail: (projectId: string, trackRef: string, init?: RequestInit) =>
    apiClient.get<DataManagerTrackDetail>(
      `/projects/${projectId}/data-manager/tracks/${encodeURIComponent(trackRef)}/detail`,
      init,
    ),

  queryView: (
    projectId: string,
    viewId: string,
    limit: number,
    offset: number,
    init?: RequestInit,
  ) => {
    const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return apiClient.get<ProjectTaskQueryResponse>(
      `/projects/${projectId}/task-views/${viewId}/tasks?${q}`,
      init,
    );
  },
};
