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
// v0.18.27 · 项目级「已保存的编排」复用预标注阶段载荷类型 (与后端 PipelineStage 同构).
import type { PipelineStagePayload } from "@/hooks/usePreannotation";

// ── 类型再导出（向后兼容旧 import 名） ─────────────────────────────
//
// v0.6.4 起后端 Pydantic JSONB 字段已结构化，OpenAPI codegen 直接出强类型，
// 不再需要 `Omit + 富类型` workaround。下面只是把生成出来的类型按旧导出
// 名重新导出，避免 30+ 调用方被迫一起改。

export type AttributeField = GenAttributeField;
export type AttributeFieldOption = GenAttributeFieldOption;
export type AttributeFieldType = GenAttributeField["type"];
export type AttributeSchema = GenAttributeSchema;
export type ClassConfigEntry = GenClassConfigEntry;
export type ClassesConfig = Record<string, ClassConfigEntry>;

// v0.10.29 · 项目级视频帧采样配置 (逻辑采样, 见 docs/plans/.../video-frame-sampling.md §2).
export type VideoSamplingConfig = GenVideoSamplingConfig;

// v0.10.17 · 工具维度类别 / 属性绑定. codegen 派生 dict<string, ToolBinding>, 这里
// 用 ToolUnitId Literal 收窄 key, 供前端编辑器强类型.
export type ToolClassEntry = GenToolClassEntry;
// v0.10.28 · keypoint 单元骨骼模板. 后端 ToolBinding.keypoint_schema 就位前, 前端先在
// 此处扩展可选字段贯通配置 UI ↔ 工作台渲染. 注意: 当前 codegen ToolBinding 无此字段,
// 故 PATCH 时后端会忽略 keypoint_schema (不落库) — 详见提交说明的「未竟部分」.
export type ToolBinding = GenToolBinding & {
  keypoint_schema?: import("@/types").KeypointSchema | null;
};
export type ToolBindings = Partial<Record<ToolUnitId, ToolBinding>>;

// v0.10.10 · I17.3 · ProjectRenderingConfig — 与后端 ProjectRenderingConfig 同形；
// 字段 = null/undefined 表示「项目不覆盖该字段」，沿用用户级 preferences。
export interface ProjectRenderingConfig {
  smoothImage?: boolean | null;
  cssImageFilter?: string | null;
  controlPointsSize?: number | null;
  snapToGrid?: boolean | null;
  box3dDefaultSize?: [number, number, number] | null;
  propagateOverwrite?: boolean | null;
  trackerDefaultModel?: string | null;
}

/** v0.6.4 起 ProjectOut 已强类型，ProjectResponse 仅作为旧导出名保留。 */
// v0.10.10 · 待 codegen 重跑前手动扩 rendering_config 字段。
// v0.18.27 · codegen 已含 preannotate_pipeline (Array<{[k]:unknown}>), Omit 后用富类型覆盖.
export type ProjectResponse = Omit<ProjectOut, "preannotate_pipeline"> & {
  rendering_config?: ProjectRenderingConfig;
  // v0.18.27 · 项目级「已保存的编排」(方案 A); null/缺 = 未配编排.
  preannotate_pipeline?: PipelineStagePayload[] | null;
};
export type ProjectStatsResponse = ProjectStats;
export type ProjectMemberResponse = ProjectMemberOut;
export type ProjectAccessResponse = ProjectAccessOut;
export type { PlatformRole, ProjectRole };
export type ProjectMemberRolePreviewResponse = ProjectMemberRolePreviewOut;
export type ProjectMemberRoleChangePayload = ProjectMemberRoleChangeRequest;
export type ProjectMemberRolePreviewPayload = ProjectMemberRolePreviewRequest;

/** Fixed capability set returned by `GET /projects/{id}/access`. */
export type ProjectCapability = NonNullable<ProjectAccessOut["capabilities"]>[number];

/** @ 提及候选：项目负责人 / 平台超管 / 项目成员，`kind` 用于输入区展示标签。 */
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
// v0.9.7 · 加 ml_backend_source_id (Wizard step 4 复用全局 backend), 待 codegen 重跑.
// v0.10.11 · 加 source_project_id (从已有项目复制配置), 同样待 codegen 重跑.
export type ProjectCreatePayload = ProjectCreate & {
  ml_backend_source_id?: string | null;
  source_project_id?: string | null;
  // v0.10.13 · E1 · 同时复制源项目 annotation_guide + guide_assets (storage key 共享).
  copy_annotation_guide?: boolean;
  // v0.10.14 · E2 · 从 ProjectTemplate 应用模板创建项目; 与 source_project_id 互斥.
  template_id?: string | null;
};
// v0.10.10 · I17.3 · 加 rendering_config 字段；待 codegen 重跑。
// v0.10.13 · E1 · 加 annotation_guide / guide_assets; 待 codegen 重跑。
// v0.18.27 · codegen 已含 preannotate_pipeline (Array<{[k]:unknown}>), Omit 后用富类型覆盖
// 避免与 PipelineStagePayload[] 交叉时报缺索引签名。
export type ProjectUpdatePayload = Omit<ProjectUpdate, "preannotate_pipeline"> & {
  rendering_config?: ProjectRenderingConfig | null;
  annotation_guide?: string | null;
  guide_assets?: GuideAssetEntry[] | null;
  // v0.14.13 · 项目级 variant 偏好 (按 backend_id 分桶); 待 codegen 重跑后从 ProjectUpdate 自带.
  // 形状: {"<backend_uuid>": {"<axis_key>": "<axis_value>", ...}, ...}
  default_variants?: Record<string, Record<string, string>> | null;
  // v0.18.27 · 项目级「已保存的编排」(方案 A); 显式 null = 清除, 不传 = 不动.
  preannotate_pipeline?: PipelineStagePayload[] | null;
};

// v0.10.13 · E1 · 项目标注指引图片资源 entry, 与后端 guide_asset.py 同构.
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

// v0.10.31 · Phase 4.7 · 视频项目导出格式 video_json/mot/kitti（aap_json 图像视频共用）。
export type ExportFormat =
  | "coco"
  | "voc"
  | "yolo"
  | "aap_json"
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
// v0.10.43 · 多目标导出：YOLO 拆 det/obb/seg；一次导出可多选目标（voc 仅可单选）。
export type ExportTarget =
  | "coco"
  | "yolo-det"
  | "yolo-obb"
  | "yolo-seg"
  | "label-studio-brush"
  | "binary-png"
  | "indexed-png"
  | "aap_json"
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
  /** v0.7.2 · 高级筛选 */
  type_key?: string[];
  /** v0.10.28 · 媒体维度筛选 (image / video / lidar) */
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

  /**
   * B1 · resolved project access (platform + project role, membership version and
   * fixed capability set).  Server authoritative; local guards are presentation.
   */
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

  // B-13 · 重命名类别 (后端原子更新 classes / classes_config / annotations.class_name)
  // v0.10.17 · 加可选 tool_unit_id 限定工具单位; 不传时跨所有 unit 同名一起改 (兼容旧客户端).
  renameClass: (id: string, old_name: string, new_name: string, tool_unit_id?: string) =>
    apiClient.post<ProjectResponse>(`/projects/${id}/classes/rename`, {
      old_name,
      new_name,
      ...(tool_unit_id ? { tool_unit_id } : {}),
    }),

  listMembers: (id: string) => apiClient.get<ProjectMemberResponse[]>(`/projects/${id}/members`),

  /** 讨论区 @ 候选：项目负责人 + 平台超管 + 项目成员（按 user_id 去重）。 */
  mentionCandidates: (id: string) =>
    apiClient.get<MentionCandidateResponse[]>(`/projects/${id}/mention-candidates`),

  addMember: (id: string, payload: { user_id: string; role: ProjectRole }) =>
    apiClient.post<ProjectMemberResponse>(`/projects/${id}/members`, payload),

  // B1 · member role change is a preview → CAS write flow (see plan §6).
  previewMemberRole: (id: string, memberId: string, payload: ProjectMemberRolePreviewPayload) =>
    apiClient.post<ProjectMemberRolePreviewResponse>(
      `/projects/${id}/members/${memberId}/role/preview`,
      payload,
    ),

  changeMemberRole: (id: string, memberId: string, payload: ProjectMemberRoleChangePayload) =>
    apiClient.patch<ProjectMemberResponse>(`/projects/${id}/members/${memberId}/role`, payload),

  removeMember: (id: string, memberId: string) =>
    apiClient.delete<void>(`/projects/${id}/members/${memberId}`),

  // v0.10.13 · E1 · 标注指引图片资源
  guideAssets: {
    uploadInit: (
      projectId: string,
      payload: { filename: string; content_type: string; size: number },
    ) =>
      apiClient.post<GuideAssetUploadInitResponse>(
        `/projects/${projectId}/guide-assets/upload-init`,
        payload,
      ),
    uploadComplete: (
      projectId: string,
      payload: { key: string; original_name: string; content_type: string },
    ) =>
      apiClient.post<GuideAssetEntry>(
        `/projects/${projectId}/guide-assets/upload-complete`,
        payload,
      ),
    remove: (projectId: string, key: string) =>
      apiClient.delete<{ deleted: string }>(
        `/projects/${projectId}/guide-assets?key=${encodeURIComponent(key)}`,
      ),
    signUrl: (projectId: string, key: string) =>
      apiClient.get<GuideAssetSignedUrlResponse>(
        `/projects/${projectId}/guide-assets/sign-url?key=${encodeURIComponent(key)}`,
      ),
  },

  // v0.10.27 · 导出异步化：POST 创建 async_job(kind=export)，返回 {job_id}。
  // v0.10.43 · 多目标：targets 可多选，一次导出产一个 zip。不再直接 blob 下载；
  // 产物完成后在 JobsBell 里用预签名 URL 下载。
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
