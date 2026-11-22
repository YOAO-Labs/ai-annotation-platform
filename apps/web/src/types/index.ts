export type PlatformRole = "super_admin" | "project_admin" | "employee" | "viewer";

export type ProjectRole = "annotator" | "reviewer" | "viewer";

export type UserRole = PlatformRole;
export type ProjectStatus = "in_progress" | "completed" | "pending_review" | "archived";
export type TaskStatus =
  | "uploading"
  | "pending"
  | "in_progress"
  | "rejected"
  | "completed"
  | "review";
export type BatchStatus =
  | "draft"
  | "active"
  | "annotating"
  | "reviewing"
  | "approved"
  | "rejected"
  | "archived";

export interface Project {
  id: string;
  displayId: string;
  name: string;
  type: string;
  typeKey: ProjectTypeKey;
  owner: string;
  ownerInitial: string;
  members: number;
  total: number;
  done: number;
  review: number;
  pending: number;
  ai: boolean;
  aiModel: string | null;
  classes: string[];
  updated: string;
  status: ProjectStatus;
  due: string;
}

export type ProjectTypeKey =
  | "image-det"
  | "image-seg"
  | "image-kp"
  | "lidar"
  | "video-mm"
  | "video-track"
  | "mm";

export interface TaskImage {
  id: string;
  name: string;
  tags: string[];
  assignee: string;
  aiBoxes: AIBox[];
}

export interface UserBrief {
  id: string;
  name: string;
  email: string;
  role: string | null;
  avatar_initial: string;
  avatar_ref?: string | null;
}

export interface TaskResponse {
  id: string;
  project_id: string;
  display_id: string;
  file_name: string;
  file_url: string | null;
  original_identity_path?: string | null;
  storage_mode?: "managed" | "reference";
  content_sha256?: string | null;
  file_type: string;
  tags: string[];
  status: TaskStatus;
  assignee_id: string | null;
  assignee: UserBrief | null;
  reviewer: UserBrief | null;
  is_labeled: boolean;
  overlap: number;
  total_annotations: number;
  total_predictions: number;
  batch_id: string | null;
  dataset_item_id?: string | null;
  sequence_order: number | null;
  image_width: number | null;
  image_height: number | null;
  thumbnail_url: string | null;
  blurhash: string | null;
  image_pyramid?: import("@/pages/Workbench/stage/imagePyramid").ImagePyramidSummary | null;
  video_metadata: VideoMetadata | null;
  submitted_at: string | null;
  reviewer_id: string | null;
  reviewer_claimed_at: string | null;
  reviewed_at: string | null;
  reject_reason: string | null;
  reject_reason_type?: string | null;
  skip_reason: string | null;
  skipped_at: string | null;
  reopened_count: number;
  last_reopened_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface ReviewClaimResponse {
  task_id: string;
  reviewer_id: string;
  reviewer_claimed_at: string;
  is_self: boolean;
}

export interface VideoMetadata {
  duration_ms: number | null;
  fps: number | null;
  frame_count: number | null;
  width: number | null;
  height: number | null;
  codec: string | null;
  playback_path: string | null;
  playback_codec: string | null;
  playback_error: string | null;
  poster_frame_path: string | null;
  probe_error: string | null;
  poster_error: string | null;
  frame_timetable_frame_count: number | null;
  frame_timetable_error: string | null;
}

export interface TaskVideoManifestResponse {
  metadata_generation_id?: string | null;
  task_id: string;
  dataset_item_id?: string | null;
  original_identity_path?: string | null;
  video_url: string | null;
  poster_url: string | null;
  metadata: VideoMetadata;
  expires_in: number;
}

export interface VideoFrameTimetableEntry {
  frame_index: number;
  pts_ms: number;
  is_keyframe: boolean;
  pict_type: string | null;
  byte_offset: number | null;
}

export interface TaskVideoFrameTimetableResponse {
  metadata_generation_id?: string | null;
  task_id: string;
  fps: number | null;
  frame_count: number | null;
  source: "ffprobe" | "estimated";
  frames: VideoFrameTimetableEntry[];
}

export interface VideoFrameOut {
  byte_size?: number | null;
  output_id?: string | null;
  sha256?: string | null;
  metadata_generation_id?: string | null;
  frame_index: number;
  width: number;
  format: "webp" | "jpeg";
  status: "pending" | "ready" | "failed";
  url: string | null;
  retry_after: number | null;
  error: string | null;
}

export interface VideoFramePrefetchResponse {
  dataset_item_id: string;
  task_id: string | null;
  frames: VideoFrameOut[];
}

export interface VideoChunkDiagnostics {
  source_codec: string | null;
  output_codec: string | null;
  keyframe_aligned: boolean | null;
  start_byte_offset: number | null;
  end_byte_offset: number | null;
  smart_copy_eligible: boolean | null;
  fallback_reason: string | null;
}

export interface VideoChunkOut {
  output_id?: string | null;
  sha256?: string | null;
  metadata_generation_id?: string | null;
  chunk_id: number;
  start_frame: number;
  end_frame: number;
  status: "pending" | "ready" | "failed";
  url: string | null;
  byte_size: number | null;
  generation_mode: "smart_copy" | "transcode" | null;
  diagnostics: VideoChunkDiagnostics | null;
  retry_after: number | null;
  error: string | null;
}

export interface VideoManifestSegmentOut {
  id: string;
  segment_index: number;
  start_frame: number;
  end_frame: number;
  status: "open" | "assigned" | "locked" | "completed";
  assignee_id: string | null;
  locked_by: string | null;
  locked_at: string | null;
  lock_expires_at: string | null;
}

export interface VideoManifestV2Response {
  metadata_generation_id?: string | null;
  task_id: string | null;
  dataset_item_id: string;
  video_url: string | null;
  poster_url: string | null;
  fps: number | null;
  frame_count: number | null;
  duration_ms: number | null;
  chunks_manifest_url: string;
  frame_timetable_url: string;
  frame_service_base: string;
  chunk_size_frames: number;
  segments: VideoManifestSegmentOut[];
  frame_cache_formats: Array<"webp" | "jpeg">;
  expires_in: number;
}

export interface VideoChunkSampleEntry {
  frame_index: number;
  pts_ms: number;
  duration_ms: number;
  is_keyframe: boolean;
  size_bytes: number;
  offset_in_chunk: number;
}

export interface VideoChunkSamplesResponse {
  output_id?: string | null;
  sha256?: string | null;
  metadata_generation_id?: string | null;
  dataset_item_id: string;
  chunk_id: number;
  codec_string: string;
  description?: string | null;
  width: number;
  height: number;
  samples: VideoChunkSampleEntry[];
}

export type BboxGeometry = { type: "bbox"; x: number; y: number; w: number; h: number };
export type VideoBboxGeometry = {
  type: "video_bbox";
  frame_index: number;
  x: number;
  y: number;
  w: number;
  h: number;
};
export type VideoPolygonGeometry = {
  type: "video_polygon";
  frame_index: number;
  points: [number, number][];
  holes?: [number, number][][];
};
export type VideoPolylineGeometry = {
  type: "video_polyline";
  frame_index: number;
  points: [number, number][];
};
export type VideoRotatedBboxGeometry = {
  type: "video_rotated_bbox";
  frame_index: number;
  cx: number;
  cy: number;
  w: number;
  h: number;
  angle: number;
};
export type VideoKeypointGeometry = {
  type: "video_keypoint";
  frame_index: number;
  points: Keypoint[];
};
export type VideoTrackBbox = { x: number; y: number; w: number; h: number };
export type VideoTrackKeyframe = {
  frame_index: number;
  bbox: VideoTrackBbox;
  source: "manual" | "interpolated" | "prediction";
  occluded?: boolean;
  attributes?: Record<string, unknown> | null;
};
export type VideoTrackOutsideRange = {
  from: number;
  to: number;
  source?: "manual" | "prediction";
};
export type VideoTrackGeometry = {
  type: "video_track_bbox";
  track_id: string;
  semantic_label?: string | null;
  keyframes: VideoTrackKeyframe[];
  outside?: VideoTrackOutsideRange[];
};
export type VideoTrackPolygonKeyframe = {
  frame_index: number;
  points: [number, number][];
  source: "manual" | "interpolated" | "prediction";
  occluded?: boolean;
  attributes?: Record<string, unknown> | null;
};
export type VideoTrackPolygonGeometry = {
  type: "video_track_polygon";
  track_id: string;
  semantic_label?: string | null;
  keyframes: VideoTrackPolygonKeyframe[];
  outside?: VideoTrackOutsideRange[];
};
export type VideoTrackPolylineKeyframe = {
  frame_index: number;
  points: [number, number][];
  source: "manual" | "interpolated" | "prediction";
  occluded?: boolean;
  attributes?: Record<string, unknown> | null;
};
export type VideoTrackPolylineGeometry = {
  type: "video_track_polyline";
  track_id: string;
  semantic_label?: string | null;
  keyframes: VideoTrackPolylineKeyframe[];
  outside?: VideoTrackOutsideRange[];
};
export type CocoRleMaskRef = {
  encoding: "coco_rle_ref";
  storage_encoding?: "identity" | "gzip" | null;
  size: [number, number];
  object_key: string;
  sha256: string;
  runs: number;
  bytes: number;
};
export type VideoMaskGeometry = {
  type: "video_mask";
  frame_index: number;
  mask: CocoRleMaskRef;
};
export type VideoTrackMaskKeyframe = {
  frame_index: number;
  mask: CocoRleMaskRef;
  source: "manual" | "prediction";
  occluded?: boolean;
  attributes?: Record<string, unknown> | null;
};
export type VideoTrackMaskGeometry = {
  type: "video_track_mask";
  track_id: string;
  semantic_label?: string | null;
  keyframes: VideoTrackMaskKeyframe[];
  outside?: VideoTrackOutsideRange[];
};
export type RasterMaskGeometry = {
  type: "raster_mask";
  mask: CocoRleMaskRef;
};
export type PolygonGeometry = {
  type: "polygon";
  points: [number, number][];
  holes?: [number, number][][];
};
export type MultiPolygonGeometry = {
  type: "multi_polygon";
  polygons: PolygonGeometry[];
};
export type RotatedBboxGeometry = {
  type: "rotated_bbox";
  cx: number;
  cy: number;
  w: number;
  h: number;
  angle: number;
};
export type PolylineGeometry = {
  type: "polyline";
  points: [number, number][];
};
export type Keypoint = { x: number; y: number; v: 0 | 1 | 2 };
export type KeypointGeometry = {
  type: "keypoint";
  points: Keypoint[];
};
export type LidarAxisConvention =
  | "iso_8855"
  | "ros_rep103"
  | "kitti_camera"
  | "opencv_camera"
  | "apollo"
  | "y_forward"
  | "sustechpoints_demo"
  | "raw";
export type Box3DGeometry = {
  type: "box_3d";
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  convention_at_create?: LidarAxisConvention | null;
};
export type PointMaskGeometry = {
  type: "point_mask_3d";
  point_indices: number[];
  convention_at_create?: LidarAxisConvention | null;
  decimate_stride?: number | null;
  source_point_count?: number | null;
};
export type Geometry =
  | BboxGeometry
  | VideoBboxGeometry
  | VideoPolygonGeometry
  | VideoPolylineGeometry
  | VideoRotatedBboxGeometry
  | VideoKeypointGeometry
  | VideoTrackGeometry
  | VideoTrackPolygonGeometry
  | VideoTrackPolylineGeometry
  | VideoMaskGeometry
  | VideoTrackMaskGeometry
  | PolygonGeometry
  | MultiPolygonGeometry
  | RotatedBboxGeometry
  | PolylineGeometry
  | KeypointGeometry
  | Box3DGeometry
  | PointMaskGeometry
  | RasterMaskGeometry;

export interface AIBox {
  id: string;
  annotation_type?: string;
  geometry?: Geometry;
  x: number;
  y: number;
  w: number;
  h: number;
  cls: string;
  conf: number;
  polygon?: [number, number][];
  polyline?: [number, number][];
  holes?: [number, number][][];
  multiPolygon?: { points: [number, number][]; holes?: [number, number][][] }[];
  keypoints?: Keypoint[];
}

export type KeypointNode = {
  name: string;
  color?: string | null;
  sublabel?: string | null;
  x?: number | null;
  y?: number | null;
};
export type KeypointSchema = { nodes: KeypointNode[]; edges: [number, number][] };

export interface Annotation extends AIBox {
  source: "manual" | "prediction_based";
  parent_prediction_id?: string | null;
  lead_time?: number | null;
  z_order?: number;
  is_locked?: boolean;
  is_hidden?: boolean;
  version?: number;
  occluded?: boolean;
  render_key?: string;
  parent_annotation_id?: string | null;
  attributes?: Record<string, unknown> | null;
  attributes_meta?: AttributesMeta | null;
}

export type AttributeOrigin = "ai" | "human";
export interface AttributeMetaEntry {
  origin: AttributeOrigin;
  model_ref?: { backend_id?: string; model_id?: string; version?: string } | null;
  confidence?: number | null;
  at?: string | null;
}
export type AttributesMeta = Record<string, AttributeMetaEntry>;

export interface AnnotationResponse {
  id: string;
  task_id: string;
  project_id: string | null;
  video_segment_id?: string | null;
  user_id: string | null;
  source: string;
  annotation_type: string;
  tool_unit_id?: string | null;
  class_name: string;
  geometry: Geometry;
  confidence: number | null;
  parent_prediction_id: string | null;
  parent_annotation_id: string | null;
  lead_time: number | null;
  is_active: boolean;
  ground_truth: boolean;
  attributes?: Record<string, unknown>;
  attributes_meta?: AttributesMeta;
  z_order?: number;
  is_locked?: boolean;
  is_hidden?: boolean;
  track_id?: string | null;
  scene_track_id?: string | null;
  temporal_role?: "keyframe" | "derived" | "sample";
  version?: number;
  created_at: string;
  updated_at: string | null;
  render_key?: string;
}

export interface AnnotationPageResponse {
  items: AnnotationResponse[];
  next_cursor: string | null;
}

import type {
  PredictionShape as GeneratedPredictionShape,
  PredictionOut as GeneratedPredictionOut,
} from "@/api/generated/types.gen";

export type { VideoSamplingConfig } from "@/api/generated/types.gen";

export type {
  TaskPointCloudManifestResponse,
  PointCloudCameraOut,
  SensorCalibration,
} from "@/api/generated/types.gen";

export type { NeighborInfo, NeighborsResponse } from "@/api/generated/types.gen";

export type {
  NeighborFrameAnnotations,
  NeighborAnnotationsResponse,
} from "@/api/generated/types.gen";

export type PredictionShape = Omit<GeneratedPredictionShape, "geometry"> & {
  geometry: Geometry;
  shape_index?: number;
};

export type PredictionSource = "ml_backend" | "external_import";
export type PredictionSourceValue = PredictionSource | (string & {}) | null;

export type PredictionResponse = Omit<GeneratedPredictionOut, "result" | "source"> & {
  source?: PredictionSourceValue;
  result: PredictionShape[];
};

export type MLBackendState = "connected" | "disconnected" | "error" | "predicting";

export interface MLBackendResponse {
  id: string;
  project_id: string;
  name: string;
  url: string;
  state: MLBackendState;
  is_interactive: boolean;
  auth_method: string;
  extra_params: Record<string, unknown>;
  error_message: string | null;
  last_checked_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface TaskLockResponse {
  task_id: string;
  user_id: string;
  expire_at: string;
  unique_id: string;
}

export interface TaskLockConflictDetail {
  reason?: string;
  message?: string;
  user_id?: string | null;
  expire_at?: string | null;
  locked_by?: UserBrief | null;
}

export interface User {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  group: string;
  status: "online" | "offline" | "busy";
  tasks: number;
  accuracy: number | null;
  joined: string;
  initial: string;
}

export interface Role {
  key: string;
  desc: string;
  count: number;
  perms: string[];
}

export type PageKey =
  | "dashboard"
  | "annotate"
  | "review"
  | "users"
  | "datasets"
  | "storage"
  | "ai-pre"
  | "model-market"
  | "training"
  | "audit"
  | "bugs"
  | "settings"
  | "project-templates"
  | "admin-people"
  | "my-performance"
  | "admin-analytics"
  | "admin-health";
