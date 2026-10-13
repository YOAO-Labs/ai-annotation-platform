/**
 * E2E 共享 fixtures：调用后端 _test_seed router 造数与跳登录。
 *
 * 两条造数路径：
 *   - `seed.reset()`：破坏性重建**共享**固定命名空间（收敛所有 E2E 数据）。
 *     仅限确属全局操作的套件，属已记录的串行例外。
 *   - `own("命名空间")`（推荐）：每个测试私有命名空间的标准工作台 fixture，
 *     不触碰其他命名空间；测试结束后自动精确清理，重试可独立重建。
 *
 * 后端要求（`_test_seed.py`）：
 *   - development + E2E_SEED_ENABLED=true，且数据库名以 _e2e / _test 结尾
 *   - POST /api/v1/__test/seed/reset → 清理 + 重建共享 fixture
 *   - POST /api/v1/__test/seed/owned {namespace} → 仅建本命名空间 fixture
 *   - POST /api/v1/__test/seed/owned-cleanup {namespace} → 仅删本命名空间
 *   - POST /api/v1/__test/seed/login {email} → 返回 access_token
 *
 * 用法：
 *   import { test } from "../fixtures/seed";
 *   test("登录后跳默认首页", async ({ page, seed }) => {
 *     const data = await seed.owned("mypage");     // 私有 fixture，测试后自动清理
 *     await seed.loginViaUI(page, data.admin_email, "Test1234", /\/overview/);
 *   });
 */
import {
  test as base,
  expect,
  type Page,
  type Route,
  type APIRequestContext,
  type APIResponse,
  type TestInfo,
} from "@playwright/test";
import { createHash } from "node:crypto";
import { screenshotCatalogPath } from "../screenshots/recording-plan.mjs";

export interface SeedData {
  admin_email: string;
  annotator_email: string;
  reviewer_email: string;
  project_id: string;
  task_ids: string[];
  /** v0.9.4 phase 3: SAM E2E 用 mock backend; url=http://mock-sam.e2e:9999, page.route 拦截 */
  ml_backend_id: string;
}

export interface ProjectRolesSeedUser {
  id: string;
  email: string;
  platform_role: string;
}

export interface ProjectRolesSeedProject {
  project_id: string;
  display_id: string;
  owner_email: string;
  project_role: string | null;
  membership_id: string | null;
  batch_id: string;
  task_ids: string[];
}

/**
 * Multi-project employee-role fixture.  `employee` annotates in `a`, reviews
 * in `b`, has no membership in `c` (despite an assigned task), and is an idle
 * annotator in the open-pool project `d` used for the revocation check.
 */
export interface ProjectRolesSeedData {
  employee_email: string;
  peer_email: string;
  owner_email_a: string;
  owner_email_b: string;
  owner_email_c: string;
  viewer_email: string;
  viewer_unassigned_email: string;
  spare_email: string;
  solo_email: string;
  users: Record<
    | "employee"
    | "peer"
    | "owner_a"
    | "owner_b"
    | "owner_c"
    | "spare"
    | "viewer"
    | "viewer_unassigned"
    | "solo",
    ProjectRolesSeedUser
  >;
  projects: Record<"a" | "b" | "c" | "d", ProjectRolesSeedProject>;
  annotation_task_id: string;
  review_task_id: string;
  c_assigned_task_id: string;
  open_pool_task_id: string;
}

export interface FilteringSeedManifest {
  users: Record<string, string>;
  user_emails: Record<string, string>;
  image: {
    project_id: string;
    task_ids: Record<string, string>;
    object_ids: Record<string, string[]>;
    batch_ids: Record<string, string>;
    schema: Record<string, string>;
    expected: Record<string, string[]>;
    saved_view_ids: Record<string, string>;
  };
  video: {
    project_id: string;
    task_ids: Record<string, string>;
    candidate_ids: Record<string, string>;
    tracker_job_ids: Record<string, string>;
    expected: Record<string, string[]>;
  };
  paging: {
    project_id: string;
    task_id: string;
    object_ids: string[];
    expected_page_one_object_ids: string[];
    expected_page_two_object_ids: string[];
  };
  lidar: {
    project_id: string;
    scene_id: string;
    task_ids: string[];
    track_refs: string[];
    hidden_track_ref: string;
    expected_visible_track_refs: string[];
    saved_view_ids: Record<string, string>;
  };
  operations: {
    project_ids: string[];
    dataset_ids: string[];
    template_ids: string[];
    user_ids: string[];
    user_emails: string[];
    invitation_ids: string[];
    job_ids: string[];
    bug_ids: string[];
    audit_ids: string[];
    /** Namespace-unique display values for global list searches and UI labels. */
    display_names: Record<
      | "active"
      | "inactive"
      | "project_a"
      | "project_b"
      | "dataset_a"
      | "dataset_b"
      | "template_private"
      | "template_public",
      string
    >;
    invitation_emails: Record<"pending" | "accepted" | "expired" | "revoked", string>;
    search_keys: {
      users: string;
      projects: string;
      templates: string;
      invitations: string;
      jobs: string;
    };
    audit_scopes: Record<"alpha" | "beta", string>;
  };
}

export type ScreenshotUserKey = "admin" | "project_admin" | "annotator" | "reviewer";
export type ScreenshotCoreProjectKey =
  | "image_demo"
  | "video_demo"
  | "pointcloud_demo"
  | "pointcloud_multicam_demo"
  | "ocr_demo";
export type ScreenshotProjectKey = ScreenshotCoreProjectKey | "large_image_demo";
export type ScreenshotBackendRequirement = "image_interactive" | "video_tracker" | "ocr";

export interface ScreenshotCatalogUser {
  id: string;
  email: string;
  role: string;
}

export interface ScreenshotCatalogTask {
  id: string;
  display_id: string;
  file_name: string;
  file_path: string;
  status: string;
  recording_anchors?: Record<string, ScreenshotRecordingAnchor>;
}

export interface ScreenshotRecordingAnchor {
  schema_version: 1;
  coordinate_space: "normalized_media";
  label: string;
  frame_index?: number;
  bbox: [number, number, number, number];
  point: [number, number];
  additional_points: Array<[number, number]>;
  polygon: Array<[number, number]>;
  polyline: Array<[number, number]>;
  brush_strokes: Array<Array<[number, number]>>;
  positive_stroke: Array<[number, number]>;
  negative_stroke: Array<[number, number]>;
  negative_point: [number, number] | null;
  provenance: string;
}

export interface ScreenshotCatalogBatch {
  id: string;
  display_id: string;
  status: string;
}

export interface ScreenshotCatalogBackend {
  id: string;
  name: string;
  state: string;
  is_interactive: boolean;
  requirement: ScreenshotBackendRequirement;
  selected_tracker: string | null;
  capabilities: {
    models?: Array<Record<string, unknown>>;
    supported_prompts?: string[];
    supported_trackers?: string[];
    supported_geometric_outputs?: string[];
    [key: string]: unknown;
  };
}

export interface ScreenshotCatalogProject {
  id: string;
  display_id: string;
  name: string;
  data_type: string;
  datasets: Record<string, { id: string; name: string; file_count: number }>;
  tasks: Record<string, ScreenshotCatalogTask>;
  batches: Record<string, ScreenshotCatalogBatch>;
  ml_backend: ScreenshotCatalogBackend | null;
}

export interface ScreenshotSeedCatalog {
  schema_version: 1;
  seed_revision: string;
  backend_requirements?: string[];
  users: Record<ScreenshotUserKey, ScreenshotCatalogUser>;
  projects: Record<ScreenshotCoreProjectKey, ScreenshotCatalogProject> &
    Partial<Record<"large_image_demo", ScreenshotCatalogProject>>;
}

/** 点云 E2E fixture：优先使用 seed 用户缓存中的两个 nuScenes mini 真实 LIDAR_TOP 帧；
 *  CI 未安装外部数据时回落到 32 线、同量级点数的确定性扫描。 */
export interface SeedLidarData {
  lidar_project_id: string;
  lidar_task_ids: string[];
  lidar_fixture_source: "nuscenes_mini" | "nuscenes_profile";
  lidar_point_count: number;
}

export type RasterMaskFixtureVariant =
  | "single"
  | "donut_three"
  | "diagonal_two"
  | "island"
  | "smart_scribble_source"
  | "corrupt";
export type RasterMaskFixtureCanvas = "default" | "media" | "5k" | "8k";

export interface SeedRasterMaskData {
  annotation_id: string;
  variant: RasterMaskFixtureVariant;
  mask: {
    encoding: "coco_rle_ref";
    size: [number, number];
    object_key: string;
    sha256: string;
    runs: number;
    bytes: number;
  };
}

export interface SeedRasterPredictionData {
  prediction_id: string;
  mask: SeedRasterMaskData["mask"];
}

export interface SeedNativeMaskCandidateData {
  response: Record<string, unknown>;
  rle: {
    encoding: "coco_rle";
    size: [number, number];
    counts: number[];
  };
  rles: Array<{
    encoding: "coco_rle";
    size: [number, number];
    counts: number[];
  }>;
}

export interface SeedTrackerReviewData {
  job_id: string;
  source_annotation_ids: string[];
}

export interface SeedProjectPipeline {
  id: string;
  name: string;
  scope: string;
  project_id: string | null;
  is_default: boolean;
  stages: Array<Record<string, unknown>>;
}

export interface SeedTaskAnnotation {
  id: string;
  task_id: string;
  annotation_type: string;
  class_name: string;
  geometry: Record<string, unknown>;
  track_id?: string | null;
  attributes?: Record<string, unknown> | null;
}

/** v0.8.7 F4 · 截图脚本只读窥探：返回首个 super_admin / 首个项目 / 首个任务。
 *  字段允许 null（对应数据不存在时），调用方自行兜底。 */
export interface SeedPeekData {
  admin_email: string | null;
  project_id: string | null;
  task_id: string | null;
}

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

/**
 * Collision-resistant namespace token for one test attempt: a hash of the
 * spec file plus the full test title. Deterministic across retries (the
 * owned route rebuilds the same namespace) and unique across tests, suites
 * and shards, bounded to the backend's `^[a-z0-9]{4,12}$` namespace rule.
 */
export function ownedNamespaceFor(testInfo?: TestInfo): string {
  if (!testInfo) throw new Error("seed.owned() needs a namespace outside a Playwright test");
  const digest = createHash("sha256")
    .update(`${testInfo.file}::${testInfo.titlePath.join("::")}`)
    .digest("base64url")
    .replace(/[^a-zA-Z0-9]/g, "")
    .toLowerCase();
  return digest.slice(0, 12).padStart(4, "0");
}

export class SeedAPI {
  /** Namespaces built by this test via `owned()`; cleaned up in teardown. */
  private ownedNamespaces: string[] = [];

  constructor(
    private request: APIRequestContext,
    private testInfo?: TestInfo,
  ) {}

  /**
   * 录制时 4K 转码会让测试 API 连接空闲数十秒，Uvicorn 已关闭的 keep-alive socket
   * 偶尔会被客户端复用并报 socket hang up。登录是幂等操作，遇到连接复位后用新连接重试。
   */
  private async seedLogin(email: string): Promise<APIResponse> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await this.request.post(`${API_BASE}/api/v1/__test/seed/login`, {
          headers: { Connection: "close" },
          data: { email },
        });
      } catch (error) {
        if (attempt > 0 || !/socket hang up|ECONNRESET/i.test(String(error))) throw error;
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
    }
    throw new Error("seed/login transient retry exhausted");
  }

  async accessToken(email: string): Promise<string> {
    const res = await this.seedLogin(email);
    if (!res.ok()) {
      throw new Error(`seed/login failed: ${res.status()} ${await res.text()}`);
    }
    return ((await res.json()) as { access_token: string }).access_token;
  }

  /** 通过正式 API 准备单条录制夹具；调用方必须在 finally 精确删除。 */
  async createTaskAnnotation(
    taskId: string,
    userEmail: string,
    payload: {
      annotation_type: string;
      tool_unit_id: string;
      class_name: string;
      geometry: Record<string, unknown>;
      attributes?: Record<string, unknown>;
    },
  ): Promise<SeedTaskAnnotation> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.post(`${API_BASE}/api/v1/tasks/${taskId}/annotations`, {
      headers: { Authorization: `Bearer ${token}`, Connection: "close" },
      data: payload,
    });
    if (!res.ok()) {
      throw new Error(`annotations/create failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedTaskAnnotation;
  }

  /** 精确删除录制夹具或流程创建的单条标注。 */
  async deleteTaskAnnotation(
    taskId: string,
    annotationId: string,
    userEmail: string,
  ): Promise<void> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.delete(
      `${API_BASE}/api/v1/tasks/${taskId}/annotations/${annotationId}`,
      { headers: { Authorization: `Bearer ${token}`, Connection: "close" } },
    );
    if (!res.ok() && res.status() !== 404) {
      throw new Error(`annotations/delete failed: ${res.status()} ${await res.text()}`);
    }
  }

  /** 精确删除录制流创建的命名编排，避免在隔离截图库留下可变业务数据。 */
  async deleteProjectPipeline(pipelineId: string, userEmail: string): Promise<void> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.delete(`${API_BASE}/api/v1/project-pipelines/${pipelineId}`, {
      headers: { Authorization: `Bearer ${token}`, Connection: "close" },
    });
    if (!res.ok()) {
      throw new Error(`project-pipelines/delete failed: ${res.status()} ${await res.text()}`);
    }
  }

  /** 通过正式 API 精确删除录制流创建的单个存储连接器。 */
  async deleteStorageConnection(connectionId: string, userEmail: string): Promise<void> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.delete(
      `${API_BASE}/api/v1/storage-connections/${connectionId}`,
      { headers: { Authorization: `Bearer ${token}`, Connection: "close" } },
    );
    if (!res.ok() && res.status() !== 404) {
      throw new Error(`storage-connections/delete failed: ${res.status()} ${await res.text()}`);
    }
  }

  /** 只在隔离录制库内清理与指定名称完全一致的连接器残留。 */
  async deleteStorageConnectionsByExactName(name: string, userEmail: string): Promise<string[]> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.get(`${API_BASE}/api/v1/storage-connections`, {
      headers: { Authorization: `Bearer ${token}`, Connection: "close" },
    });
    if (!res.ok()) {
      throw new Error(`storage-connections/list failed: ${res.status()} ${await res.text()}`);
    }
    const connections = (await res.json()) as Array<{ id: string; name: string }>;
    const exactIds = connections
      .filter((connection) => connection.name === name)
      .map((connection) => connection.id);
    for (const connectionId of exactIds) {
      await this.deleteStorageConnection(connectionId, userEmail);
    }
    return exactIds;
  }

  /** 通过正式级联删除端点精确清理录制流创建的临时项目。 */
  async deleteProject(projectId: string, userEmail: string): Promise<void> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.delete(`${API_BASE}/api/v1/projects/${projectId}`, {
      headers: { Authorization: `Bearer ${token}`, Connection: "close" },
    });
    if (!res.ok() && res.status() !== 404) {
      throw new Error(`projects/delete failed: ${res.status()} ${await res.text()}`);
    }
  }

  /** 只在隔离录制库内删除与指定名称完全一致的残留项目。 */
  async deleteProjectsByExactName(name: string, userEmail: string): Promise<string[]> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.get(`${API_BASE}/api/v1/projects`, {
      headers: { Authorization: `Bearer ${token}`, Connection: "close" },
      params: { search: name },
    });
    if (!res.ok()) {
      throw new Error(`projects/list failed: ${res.status()} ${await res.text()}`);
    }
    const projects = (await res.json()) as Array<{ id: string; name: string }>;
    const exactIds = projects
      .filter((project) => project.name === name)
      .map((project) => project.id);
    for (const projectId of exactIds) await this.deleteProject(projectId, userEmail);
    return exactIds;
  }

  /** 通过正式 API 准备录制所需模板；调用方必须在 finally 精确删除返回的 id。 */
  async createProjectPipeline(
    userEmail: string,
    payload: {
      name: string;
      scope: "private" | "organization" | "public";
      project_id: string | null;
      organization_id?: string | null;
      stages: Array<Record<string, unknown>>;
      is_default: boolean;
    },
  ): Promise<SeedProjectPipeline> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.post(`${API_BASE}/api/v1/project-pipelines`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Connection: "close",
      },
      data: payload,
    });
    if (!res.ok()) {
      throw new Error(`project-pipelines/create failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedProjectPipeline;
  }

  async reset(): Promise<SeedData> {
    // Cleanup may remove WebCodecs objects in MinIO before rebuilding the fixture;
    // do not abandon that idempotent reset while the server is still completing it.
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/reset`, {
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/reset failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedData;
  }

  /**
   * Resolve (and register for post-test cleanup) this test's namespace token.
   * Explicit tokens are for helpers that own their lifecycle; everything else
   * derives the same per-test token the `owned()` fixture uses.
   */
  private ownedNamespace(explicit?: string): string {
    const token = explicit ?? ownedNamespaceFor(this.testInfo);
    if (!this.ownedNamespaces.includes(token)) this.ownedNamespaces.push(token);
    return token;
  }

  /**
   * Build this test's own namespaced image-workbench fixture without touching
   * any other namespace. Without an explicit namespace the token derives
   * deterministically from the spec file and test title, so retries rebuild
   * the same namespace while every other test owns a different one. The
   * namespace is registered on this SeedAPI instance (fresh per test) and
   * `cleanupOwnedFixtures()` removes every registered namespace in the
   * seed-fixture teardown after the test.
   */
  async owned(namespace?: string): Promise<SeedData> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/owned`, {
      data: { namespace: this.ownedNamespace(namespace) },
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/owned failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedData;
  }

  /** Delete exactly one namespace's fixture; idempotent and neighbour-safe. */
  async cleanupOwned(namespace: string): Promise<void> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/owned-cleanup`, {
      data: { namespace },
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/owned-cleanup failed: ${res.status()} ${await res.text()}`);
    }
  }

  /**
   * Remove every namespace this instance built, newest first. Called by the
   * seed fixture teardown after the test body (and its evidence attachments)
   * finished; a failed cleanup must fail the test, so errors propagate.
   */
  async cleanupOwnedFixtures(): Promise<void> {
    for (const namespace of [...this.ownedNamespaces].reverse()) {
      await this.cleanupOwned(namespace);
    }
    this.ownedNamespaces.length = 0;
  }

  /** Build the multi-project employee-role acceptance fixture for this test's namespace. */
  async projectRoles(namespace?: string): Promise<ProjectRolesSeedData> {
    const token = this.ownedNamespace(namespace);
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/project-roles`, {
      data: { namespace: token },
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/project-roles failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as ProjectRolesSeedData;
  }

  /** Build the filtering fixture inside this test's own owned namespace. */
  async filtering(namespace?: string): Promise<FilteringSeedManifest> {
    const token = this.ownedNamespace(namespace);
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/filtering`, {
      data: { namespace: token },
      timeout: 120_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/filtering failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as FilteringSeedManifest;
  }

  /** 截图专用只读 catalog：解析稳定逻辑键，服务端会对完整 profile fail-closed。 */
  async screenshotCatalog(): Promise<ScreenshotSeedCatalog> {
    const res = await this.request.get(`${API_BASE}${screenshotCatalogPath()}`);
    if (!res.ok()) {
      throw new Error(
        `seed/catalog failed: ${res.status()} ${await res.text()}\n` +
          "请运行 `cd apps/api && PYTHONPATH=. uv run python scripts/seed.py " +
          "--profile screenshots --ml-backend-mode live`。",
      );
    }
    return (await res.json()) as ScreenshotSeedCatalog;
  }

  /** 为隔离录制项目启用一个已注册的真实 backend。 */
  async enableMLBackendByName(
    projectId: string,
    userEmail: string,
    backendName: string,
  ): Promise<string> {
    const token = await this.accessToken(userEmail);
    const headers = { Authorization: `Bearer ${token}` };
    const available = await this.request.get(
      `${API_BASE}/api/v1/projects/${projectId}/ml-backends/available`,
      { headers },
    );
    if (!available.ok()) {
      throw new Error(
        `ml-backends/available failed: ${available.status()} ${await available.text()}`,
      );
    }
    const body = (await available.json()) as {
      items: Array<{ backend: { id: string; name: string }; enabled: boolean }>;
    };
    const item = body.items.find((candidate) => candidate.backend.name === backendName);
    if (!item) throw new Error(`未找到 ML backend: ${backendName}`);
    if (item.enabled) return item.backend.id;

    const enabled = await this.request.put(
      `${API_BASE}/api/v1/projects/${projectId}/ml-backends/${item.backend.id}/enablement`,
      { headers, data: { enabled: true } },
    );
    if (!enabled.ok()) {
      throw new Error(`ml-backends/enablement failed: ${enabled.status()} ${await enabled.text()}`);
    }
    return item.backend.id;
  }

  /** 通过正式端点跑一次真实单题推理，用于录制前验证输入可达并预热模型。 */
  async predictTestMLBackend(
    projectId: string,
    backendId: string,
    taskId: string,
    userEmail: string,
  ): Promise<void> {
    const token = await this.accessToken(userEmail);
    const res = await this.request.post(
      `${API_BASE}/api/v1/projects/${projectId}/ml-backends/${backendId}/predict-test`,
      {
        headers: { Authorization: `Bearer ${token}`, Connection: "close" },
        params: { task_id: taskId },
      },
    );
    if (!res.ok()) {
      throw new Error(`ml-backends/predict-test failed: ${res.status()} ${await res.text()}`);
    }
    const body = (await res.json()) as { results?: unknown[] };
    if (!Array.isArray(body.results) || body.results.length === 0) {
      throw new Error("ml-backends/predict-test returned no real result");
    }
  }

  /** v0.16.x · 造点云 E2E fixture(lidar 项目 + 2 帧 point_cloud task)。需先 reset()。 */
  /** Build the namespaced point-cloud fixture (reuses this namespace's users). */
  async seedLidar(namespace?: string): Promise<SeedLidarData> {
    const token = this.ownedNamespace(namespace);
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/lidar`, {
      data: { namespace: token },
    });
    if (!res.ok()) {
      throw new Error(`seed/lidar failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedLidarData;
  }

  /** 只切项目 opt-in；部署级 read/create 总闸由 API 进程环境决定。 */
  async configureRasterMask(projectId: string, enabled: boolean): Promise<void> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/configure-raster-mask`, {
      data: { project_id: projectId, enabled },
    });
    if (!res.ok()) {
      throw new Error(`seed/configure-raster-mask failed: ${res.status()} ${await res.text()}`);
    }
  }

  async videoTask(projectId: string): Promise<{ task_id: string }> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/video-task`, {
      data: { project_id: projectId },
    });
    if (!res.ok()) {
      throw new Error(`seed/video-task failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as { task_id: string };
  }

  /** v0.23.15 · 造确定性 H.264 WebCodecs fixture 任务 + 单 chunk(precise-frame E2E)。 */
  async videoWebCodecs(
    projectId: string,
    options?: {
      fixture?: string;
      chunkStatus?: "ready" | "pending";
      batchId?: string;
    },
  ): Promise<{
    task_id: string;
    dataset_item_id: string;
    chunk_id: number;
    chunk_size_frames: number;
    frame_expectations: Record<string, unknown>;
  }> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/video-webcodecs`, {
      data: {
        project_id: projectId,
        ...(options?.batchId ? { batch_id: options.batchId } : {}),
        fixture: options?.fixture ?? "h264-baseline-gop12",
        chunk_status: options?.chunkStatus ?? "ready",
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/video-webcodecs failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as {
      task_id: string;
      dataset_item_id: string;
      chunk_id: number;
      chunk_size_frames: number;
      frame_expectations: Record<string, unknown>;
    };
  }

  /** Simulate one unknown nested video context version without relaxing public writes. */
  async videoIssueContextHistory(feedbackId: string): Promise<{
    feedback_id: string;
    anchor_position: Record<string, unknown>;
  }> {
    const res = await this.request.post(
      `${API_BASE}/api/v1/__test/seed/video-issue-context-history`,
      {
        data: { feedback_id: feedbackId },
      },
    );
    if (!res.ok()) {
      throw new Error(
        `seed/video-issue-context-history failed: ${res.status()} ${await res.text()}`,
      );
    }
    return res.json();
  }

  /** Deterministically release a pending media chunk without a Celery worker. */
  async videoWebCodecsTransitionReady(
    datasetItemId: string,
    chunkId = 0,
  ): Promise<{ status: "ready" | "pending" | "failed" }> {
    const res = await this.request.post(
      `${API_BASE}/api/v1/__test/seed/video-webcodecs/transition-ready`,
      { data: { dataset_item_id: datasetItemId, chunk_id: chunkId } },
    );
    if (!res.ok()) {
      throw new Error(
        `seed/video-webcodecs/transition-ready failed: ${res.status()} ${await res.text()}`,
      );
    }
    return (await res.json()) as { status: "ready" | "pending" | "failed" };
  }

  async trackerReview(taskId: string, userEmail: string): Promise<SeedTrackerReviewData> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/tracker-review`, {
      data: { task_id: taskId, user_email: userEmail },
    });
    if (!res.ok()) {
      throw new Error(`seed/tracker-review failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedTrackerReviewData;
  }

  async nativeMaskCandidate(
    taskId: string,
    options?: {
      variant?: "default" | "negative_scribble" | "multimask_donut" | "smart_scribble_refined";
      promptFamily?: "point" | "scribble";
      negativeScribbles?: number;
      promptSource?: {
        annotationId: string;
        sourceVersion: number;
        sourceDigest: string;
      };
    },
  ): Promise<SeedNativeMaskCandidateData> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/native-mask-candidate`, {
      data: {
        task_id: taskId,
        variant: options?.variant ?? "default",
        prompt_family: options?.promptFamily ?? "point",
        negative_scribbles: options?.negativeScribbles ?? 0,
        prompt_source: options?.promptSource
          ? {
              annotation_id: options.promptSource.annotationId,
              source_version: options.promptSource.sourceVersion,
              source_digest: options.promptSource.sourceDigest,
            }
          : null,
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/native-mask-candidate failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedNativeMaskCandidateData;
  }

  /** 构造已有 raster annotation，用于只读、损坏内容与锁定矩阵。 */
  async injectRasterMask(opts: {
    taskId: string;
    userEmail: string;
    variant?: RasterMaskFixtureVariant;
    label?: string;
    locked?: boolean;
    canvas?: RasterMaskFixtureCanvas;
  }): Promise<SeedRasterMaskData> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/inject-raster-mask`, {
      data: {
        task_id: opts.taskId,
        user_email: opts.userEmail,
        variant: opts.variant ?? "single",
        label: opts.label ?? "car",
        locked: opts.locked ?? false,
        canvas: opts.canvas ?? "default",
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/inject-raster-mask failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedRasterMaskData;
  }

  /** 构造待接受的 raster prediction，只用于 gate 关闭矩阵。 */
  async injectRasterPrediction(opts: {
    taskId: string;
    userEmail: string;
    label?: string;
  }): Promise<SeedRasterPredictionData> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/inject-raster-prediction`, {
      data: {
        task_id: opts.taskId,
        user_email: opts.userEmail,
        variant: "single",
        label: opts.label ?? "car",
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/inject-raster-prediction failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedRasterPredictionData;
  }

  /** v0.8.7 F4 · 只读窥探现有数据；不破坏 dev 数据。 */
  async peek(): Promise<SeedPeekData> {
    const res = await this.request.get(`${API_BASE}/api/v1/__test/seed/peek`);
    if (!res.ok()) {
      throw new Error(`seed/peek failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as SeedPeekData;
  }

  /** 直接拿 JWT 注入 localStorage（跳过 UI 登录，加快非 auth spec）。 */
  async injectToken(page: Page, email: string, baseURL?: string): Promise<void> {
    const res = await this.seedLogin(email);
    if (!res.ok()) throw new Error(`seed/login failed: ${res.status()} ${await res.text()}`);
    const body = (await res.json()) as { access_token: string; user: unknown };
    await this.setPetEnabled(email, false, body.access_token);
    const target = baseURL ?? process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3001";
    // Establish same-origin storage without booting the previous actor's dashboard.
    // Callers navigate to their test page after injecting the new identity.
    const documentURL = new URL(target).href;
    const emptyDocument = (route: Route) =>
      route.fulfill({
        contentType: "text/html",
        body: "<!doctype html><title>E2E auth setup</title>",
      });
    await page.route(documentURL, emptyDocument, { times: 1 });
    try {
      await page.goto(documentURL);
    } finally {
      await page.unroute(documentURL, emptyDocument);
    }
    await page.evaluate(
      ({ token, user }) => {
        localStorage.setItem("token", token);
        // zustand persist 写入 auth-storage 同步形态
        localStorage.setItem(
          "auth-storage",
          JSON.stringify({ state: { token, user }, version: 0 }),
        );
      },
      { token: body.access_token, user: body.user },
    );
  }

  async setPetEnabled(email: string, enabled: boolean, token?: string): Promise<void> {
    const authToken = token ?? (await this.accessToken(email));
    const res = await this.request.patch(`${API_BASE}/api/v1/auth/me/preferences`, {
      headers: { Authorization: `Bearer ${authToken}`, Connection: "close" },
      data: { workbench: { common: { petEnabled: enabled } } },
    });
    if (!res.ok()) {
      throw new Error(`preferences/pet-enabled failed: ${res.status()} ${await res.text()}`);
    }
  }

  /**
   * UI 路径登录：filling form + click 提交（auth spec 主用）。
   * Issue #123 · 登录后进入角色默认首页：super_admin → /overview，其余角色 → /dashboard。
   * 调用方传入该账号角色对应的期望路径。
   */
  async loginViaUI(
    page: Page,
    email: string,
    password: string,
    expectedHome: RegExp,
  ): Promise<void> {
    await page.goto("/login");
    await page.getByPlaceholder("输入账号或邮箱").fill(email);
    await page.getByPlaceholder("••••••••").fill(password);
    await page.getByRole("button", { name: "登录" }).click();
    await expect(page).toHaveURL(expectedHome, { timeout: 10_000 });
  }

  /**
   * v0.10.10 · I11 e2e 辅助：直接 INSERT 一条 polygon prediction，绕过 ml-backend。
   * 用于 mask 编辑器「AI prediction 精修」入口测试。
   */
  async injectPrediction(opts: {
    taskId: string;
    projectId: string;
    label: string;
    /** 归一化 [0,1] points, ≥3 顶点 */
    polygon: [number, number][];
    score?: number;
  }): Promise<{ prediction_id: string }> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/inject-prediction`, {
      headers: { Connection: "close" },
      data: {
        task_id: opts.taskId,
        project_id: opts.projectId,
        label: opts.label,
        polygon: opts.polygon,
        score: opts.score ?? 0.9,
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/inject-prediction failed: ${res.status()} ${await res.text()}`);
    }
    return (await res.json()) as { prediction_id: string };
  }

  /**
   * v0.8.5 · E2E 辅助：直接置 task 状态，绕过 UI 链路。
   * 多角色串联 spec 用此跳过画框 / 提交流程，专注验证下游交接。
   */
  async advanceTask(opts: {
    taskId: string;
    toStatus: "pending" | "annotating" | "submitted" | "review" | "completed" | "rejected";
    annotatorEmail?: string;
    reviewerEmail?: string;
  }): Promise<void> {
    const res = await this.request.post(`${API_BASE}/api/v1/__test/seed/advance_task`, {
      data: {
        task_id: opts.taskId,
        to_status: opts.toStatus,
        annotator_email: opts.annotatorEmail,
        reviewer_email: opts.reviewerEmail,
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/advance_task failed: ${res.status()} ${await res.text()}`);
    }
  }
}

type Fixtures = {
  seed: SeedAPI;
};

export const test = base.extend<Fixtures>({
  seed: async ({ request }, use, testInfo) => {
    const api = new SeedAPI(request, testInfo);
    // playwright fixture 的 use 不是 React Hook
    // eslint-disable-next-line react-hooks/rules-of-hooks
    await use(api);
    // Deterministic per-test cleanup for every owned namespace the test built.
    await api.cleanupOwnedFixtures();
  },
});

export { expect };
