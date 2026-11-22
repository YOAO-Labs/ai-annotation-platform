import { apiClient } from "./client";
import type {
  ProjectOut,
  ProjectPage as GenProjectPage,
  ProjectCreate,
  ProjectUpdate,
  ProjectStats,
  ProjectMemberOut,
  ProjectAccessOut,
  ProjectMemberRoleChangeRequest,
  ProjectMemberRolePreviewOut,
  ProjectMemberRolePreviewRequest,
  PlatformRole,
  ProjectRole,
  MentionCandidateOut,
  AttributeField as GenAttributeField,
  AttributeFieldOption as GenAttributeFieldOption,
  AttributeSchema as GenAttributeSchema,
  ClassConfigEntry as GenClassConfigEntry,
  ToolBinding as GenToolBinding,
  ToolClassEntry as GenToolClassEntry,
  VideoSamplingConfig as GenVideoSamplingConfig,
} from "./generated/types.gen";
import type { ToolUnitId } from "@/constants/toolUnits";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";

export type AttributeField = GenAttributeField;
export type AttributeFieldOption = GenAttributeFieldOption;
export type AttributeFieldType = GenAttributeField["type"];
export type AttributeSchema = GenAttributeSchema;
export type ClassConfigEntry = GenClassConfigEntry;
export type ClassesConfig = Record<string, ClassConfigEntry>;

export type VideoSamplingConfig = GenVideoSamplingConfig;

export type ToolClassEntry = GenToolClassEntry;
export type ToolBinding = GenToolBinding & {
  keypoint_schema?: import("@/types").KeypointSchema | null;
};
export type ToolBindings = Partial<Record<ToolUnitId, ToolBinding>>;

export interface ProjectRenderingConfig {
  smoothImage?: boolean | null;
  cssImageFilter?: string | null;
  controlPointsSize?: number | null;
  snapToGrid?: boolean | null;
  box3dDefaultSize?: [number, number, number] | null;
  propagateOverwrite?: boolean | null;
  trackerDefaultModel?: string | null;
}

export type ProjectResponse = Omit<ProjectOut, "preannotate_pipeline"> & {
  rendering_config?: ProjectRenderingConfig;
  preannotate_pipeline?: PipelineStagePayload[] | null;
};
export type ProjectStatsResponse = ProjectStats;
export type ProjectMemberResponse = ProjectMemberOut;
export type ProjectAccessResponse = ProjectAccessOut;
export type { PlatformRole, ProjectRole };
export type ProjectMemberRolePreviewResponse = ProjectMemberRolePreviewOut;
export type ProjectMemberRoleChangePayload = ProjectMemberRoleChangeRequest;
export type ProjectMemberRolePreviewPayload = ProjectMemberRolePreviewRequest;

export type ProjectCapability = NonNullable<ProjectAccessOut["capabilities"]>[number];

export type MentionCandidateResponse = MentionCandidateOut;

export interface ProjectReadinessSummary {
  project_id: string;
  guide_ready: boolean;
  classes_ready: boolean;
  linked_dataset_count: number;
  dataset_item_count: number;
  task_count: number;
  linked_task_count: number;
  task_creation_active_jobs: number;
  task_creation_failed_jobs: number;
  latest_task_creation_status: string | null;
  batch_count: number;
  nonempty_batch_count: number;
  executable_batch_count: number;
  assigned_batch_count: number;
  active_annotator_count: number;
  active_reviewer_count: number;
  activated_batch_count: number;
}
export type ProjectCreatePayload = ProjectCreate & {
  ml_backend_source_id?: string | null;
  source_project_id?: string | null;
  copy_annotation_guide?: boolean;
  template_id?: string | null;
};
export type ProjectUpdatePayload = Omit<ProjectUpdate, "preannotate_pipeline"> & {
  rendering_config?: ProjectRenderingConfig | null;
  annotation_guide?: string | null;
  guide_assets?: GuideAssetEntry[] | null;
  default_variants?: Record<string, Record<string, string>> | null;
  preannotate_pipeline?: PipelineStagePayload[] | null;
};

export interface GuideAssetEntry {
  key: string;
  original_name: string;
  content_type: string;
  size: number;
  uploaded_at: string;
}

export interface GuideAssetUploadInitResponse {
  key: string;
  upload_url: string;
  expires_in: number;
}

export interface GuideAssetSignedUrlResponse {
  url: string;
  expires_in: number;
}

export type ExportFormat =
  | "coco"
  | "voc"
  | "yolo"
  | "native_json"
  | "video_json"
  | "mot"
  | "kitti"
  | "coco-multicamera"
  | "nuscenes"
  | "pointmask"
  | "yolo-frames-det"
  | "yolo-frames-seg"
  | "coco-frames-seg"
  | "davis"
  | "youtube-vos"
  | "mots";
export type ExportTarget =
  | "coco"
  | "yolo-det"
  | "yolo-obb"
  | "yolo-seg"
  | "label-studio-brush"
  | "binary-png"
  | "indexed-png"
  | "native_json"
  | "video_json"
  | "mot"
  | "kitti"
  | "coco-multicamera"
  | "nuscenes"
  | "pointmask"
  | "yolo-frames-det"
  | "yolo-frames-seg"
  | "coco-frames-seg"
  | "davis"
  | "youtube-vos"
  | "mots"
  | "voc";
export type VideoFrameMode = "keyframes" | "all_frames";
export type VideoExportScope = {
  task_id: string;
  selection:
    | {
        kind: "segments";
        start_segment_id: string;
        end_segment_id: string;
      }
    | {
        kind: "frames";
        from_frame: number;
        to_frame: number;
      };
};
export interface LidarExportOptions {
  kittiCameraRole?: string;
}
export interface LidarExportIssue {
  code: string;
  message: string;
  task_id: string | null;
  task_display_id: string | null;
  frame_key: string | null;
  camera_role: string | null;
}
export interface LidarExportPreflight {
  ready: boolean;
  camera_roles: string[];
  selected_camera_role: string | null;
  checked_tasks: number;
  issue_count: number;
  issues_truncated: boolean;
  issues: LidarExportIssue[];
}
export interface ExportOptions {
  includeAttributes?: boolean;
  videoFrameMode?: VideoFrameMode;
  indexedOverlapPolicy?: "error" | "z_order" | "larger_area" | "smaller_area";
  videoOverlapPolicy?: "error" | "z_order" | "larger_area" | "smaller_area";
  motsFrameBase?: 0 | 1;
  scope?: VideoExportScope;
  lidar?: LidarExportOptions;
}

export interface ProjectClassUsageResponse {
  classes: Record<string, number>;
  attributes: Record<string, number>;
}

export interface ProjectCleanupOrphansResponse {
  orphan_annotations: number;
  orphan_attribute_keys: Record<string, number>;
}

export interface ProjectListParams {
  status?: string;
  search?: string;
  type_key?: string[];
  data_type?: string[];
  member_id?: string;
  created_from?: string;
  created_to?: string;
}

export interface ProjectPageParams extends ProjectListParams {
  page: number;
  page_size: number;
}

export type ProjectPage = Omit<GenProjectPage, "items"> & {
  items: ProjectResponse[];
};

function projectQueryString(params?: ProjectListParams | ProjectPageParams) {
  const q = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([k, v]) => {
    if (v === undefined || v === null) return;
    if (Array.isArray(v)) {
      v.forEach((vi) => q.append(k, String(vi)));
    } else {
      q.append(k, String(v));
    }
  });
  return q.toString();
}

export const projectsApi = {
  list: (params?: ProjectListParams, init?: RequestInit) => {
    const qs = projectQueryString(params);
    const path = `/projects${qs ? `?${qs}` : ""}`;
    return init
      ? apiClient.get<ProjectResponse[]>(path, init)
      : apiClient.get<ProjectResponse[]>(path);
  },

  page: (params: ProjectPageParams, init?: RequestInit) =>
    apiClient.get<ProjectPage>(`/projects/query?${projectQueryString(params)}`, init),

  stats: () => apiClient.get<ProjectStatsResponse>("/projects/stats"),

  get: (id: string) => apiClient.get<ProjectResponse>(`/projects/${id}`),
  getReadiness: (id: string) => apiClient.get<ProjectReadinessSummary>(`/projects/${id}/readiness`),

  getAccess: (id: string, init?: RequestInit) =>
    init
      ? apiClient.get<ProjectAccessResponse>(`/projects/${id}/access`, init)
      : apiClient.get<ProjectAccessResponse>(`/projects/${id}/access`),

  create: (payload: ProjectCreatePayload) => apiClient.post<ProjectResponse>("/projects", payload),

  update: (id: string, payload: ProjectUpdatePayload) =>
    apiClient.patch<ProjectResponse>(`/projects/${id}`, payload),

  remove: (id: string) => apiClient.delete<void>(`/projects/${id}`),

  transfer: (id: string, new_owner_id: string) =>
    apiClient.post<ProjectResponse>(`/projects/${id}/transfer`, { new_owner_id }),

  classUsage: (id: string) =>
    apiClient.get<ProjectClassUsageResponse>(`/projects/${id}/class-usage`),

  cleanupOrphans: (id: string, dry_run = true) =>
    apiClient.post<ProjectCleanupOrphansResponse>(`/projects/${id}/cleanup-orphans`, { dry_run }),

  renameClass: (id: string, old_name: string, new_name: string, tool_unit_id?: string) =>
    apiClient.post<ProjectResponse>(`/projects/${id}/classes/rename`, {
      old_name,
      new_name,
      ...(tool_unit_id ? { tool_unit_id } : {}),
    }),

  listMembers: (id: string) => apiClient.get<ProjectMemberResponse[]>(`/projects/${id}/members`),

  mentionCandidates: (id: string) =>
    apiClient.get<MentionCandidateResponse[]>(`/projects/${id}/mention-candidates`),

  addMember: (id: string, payload: { user_id: string; role: ProjectRole }) =>
    apiClient.post<ProjectMemberResponse>(`/projects/${id}/members`, payload),

  previewMemberRole: (id: string, memberId: string, payload: ProjectMemberRolePreviewPayload) =>
    apiClient.post<ProjectMemberRolePreviewResponse>(
      `/projects/${id}/members/${memberId}/role/preview`,
      payload,
    ),

  changeMemberRole: (id: string, memberId: string, payload: ProjectMemberRoleChangePayload) =>
    apiClient.patch<ProjectMemberResponse>(`/projects/${id}/members/${memberId}/role`, payload),

  removeMember: (id: string, memberId: string) =>
    apiClient.delete<void>(`/projects/${id}/members/${memberId}`),

  guideAssets: {
    upload: (projectId: string, file: File, init?: RequestInit) =>
      apiClient.silentPost<GuideAssetEntry>(
        `/projects/${projectId}/guide-assets/upload?file_name=${encodeURIComponent(file.name)}`,
        undefined,
        { ...init, body: file, headers: { ...init?.headers, "Content-Type": file.type } },
      ),
    remove: (projectId: string, key: string, init?: RequestInit) =>
      apiClient.silentDelete<{ deleted: string }>(
        `/projects/${projectId}/guide-assets?key=${encodeURIComponent(key)}`,
        init,
      ),
    content: (projectId: string, key: string, init?: RequestInit) =>
      apiClient.silentGetBlob(
        `/projects/${projectId}/guide-assets/content?key=${encodeURIComponent(key)}`,
        init,
      ),
  },

  exportProject: (id: string, targets: ExportTarget[], opts?: ExportOptions) => {
    const includeAttr = opts?.includeAttributes !== false;
    const params = new URLSearchParams({ include_attributes: String(includeAttr) });
    targets.forEach((t) => params.append("targets", t));
    if (opts?.videoFrameMode) params.set("video_frame_mode", opts.videoFrameMode);
    if (opts?.indexedOverlapPolicy) {
      params.set("indexed_overlap_policy", opts.indexedOverlapPolicy);
    }
    if (opts?.videoOverlapPolicy) {
      params.set("video_overlap_policy", opts.videoOverlapPolicy);
    }
    if (opts?.motsFrameBase !== undefined) {
      params.set("mots_frame_base", String(opts.motsFrameBase));
    }
    return apiClient.post<{ job_id: string }>(`/projects/${id}/export?${params.toString()}`, {
      ...(opts?.scope ? { scope: opts.scope } : {}),
      ...(opts?.lidar ? { lidar: { kitti_camera_role: opts.lidar.kittiCameraRole } } : {}),
    });
  },
  preflightLidarExport: (id: string, targets: ExportTarget[], lidar?: LidarExportOptions) =>
    apiClient.post<LidarExportPreflight>(`/projects/${id}/exports/lidar:preflight`, {
      targets,
      ...(lidar ? { lidar: { kitti_camera_role: lidar.kittiCameraRole } } : {}),
    }),
};
