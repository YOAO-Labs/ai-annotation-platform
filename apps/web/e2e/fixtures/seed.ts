/// <reference lib="es2021.promise" />

import {
  test as base,
  expect,
  type Page,
  type Route,
  type APIRequestContext,
  type TestInfo,
} from "@playwright/test";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { worktreeMode as readWorktreeMode } from "../helpers/worktree-mode.mjs";
import {
  closeFixtureTransport,
  postFixture,
  runMaskSliceLifecycle,
  type SeedResponse,
} from "../helpers/seed-owner-transport.mjs";

export interface SeedData {
  admin_email: string;
  annotator_email: string;
  reviewer_email: string;
  project_id: string;
  task_ids: string[];
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
  | "fault_source";
export type RasterMaskFixtureCanvas = "default" | "5k" | "8k";

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

type RasterFaultCase = "diagnosis" | "retry";
type PreparedRasterFaultEntry = {
  namespace: string;
  run_id: string;
  seed: SeedData;
  task_id: string;
  healthy_annotation_id: string;
  corrupt_annotation_id: string;
};
type PreparedRasterFaultManifest = {
  schema: "aap-e2e-raster-fault/v1";
  suite_id: string;
  cases: Record<RasterFaultCase, PreparedRasterFaultEntry[]>;
};

function preparedRasterFaultEntry(
  caseName: RasterFaultCase,
  attempt: number,
): PreparedRasterFaultEntry {
  const path = process.env.PLAYWRIGHT_RASTER_FAULT_MANIFEST;
  const suiteId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
  if (!path || !suiteId || !["test", "e2e"].includes(readWorktreeMode() ?? "")) {
    throw new Error(
      "Raster fault cases require an independently prepared isolated fixture manifest",
    );
  }
  const value = JSON.parse(readFileSync(path, "utf8")) as PreparedRasterFaultManifest;
  if (
    typeof value !== "object" ||
    value === null ||
    value.schema !== "aap-e2e-raster-fault/v1" ||
    value.suite_id !== suiteId ||
    typeof value.cases !== "object" ||
    value.cases === null ||
    !Array.isArray(value.cases.diagnosis) ||
    !Array.isArray(value.cases.retry)
  ) {
    throw new Error("Raster fault manifest has an invalid suite or schema");
  }
  const seen = new Set<string>();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  for (const item of [...value.cases.diagnosis, ...value.cases.retry]) {
    if (
      typeof item !== "object" ||
      item === null ||
      typeof item.seed !== "object" ||
      item.seed === null
    )
      throw new Error("Invalid raster fault entry");
    const data = item.seed;
    const identities = [
      item.run_id,
      data.project_id,
      item.task_id,
      item.healthy_annotation_id,
      item.corrupt_annotation_id,
    ];
    if (
      typeof item.namespace !== "string" ||
      !/^[a-z0-9]{4,12}$/.test(item.namespace) ||
      identities.some((identity) => typeof identity !== "string" || !uuid.test(identity)) ||
      typeof data.admin_email !== "string" ||
      typeof data.annotator_email !== "string" ||
      typeof data.reviewer_email !== "string" ||
      typeof data.ml_backend_id !== "string" ||
      !uuid.test(data.ml_backend_id) ||
      !Array.isArray(data.task_ids) ||
      data.task_ids.some((id) => typeof id !== "string" || !uuid.test(id)) ||
      !data.task_ids.includes(item.task_id)
    ) {
      throw new Error("Invalid raster fault entry");
    }
    for (const identity of [item.namespace, ...identities]) {
      const key = String(identity);
      if (seen.has(key)) throw new Error("Raster fault runs must use distinct resources");
      seen.add(key);
    }
  }
  const entry = value.cases[caseName][attempt];
  if (!entry) throw new Error("Raster fault manifest has no independently prepared retry");
  return entry;
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

export interface SeedTaskAnnotation {
  id: string;
  task_id: string;
  annotation_type: string;
  class_name: string;
  geometry: Record<string, unknown>;
  track_id?: string | null;
  attributes?: Record<string, unknown> | null;
}

const API_BASE = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";

function namespaceForKey(key: string): string {
  const runId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
  if (!runId) throw new Error("Fixture run identity is required");
  const digest = createHash("sha256")
    .update(`${runId}::${key}`)
    .digest("base64url")
    .replace(/[^a-zA-Z0-9]/g, "")
    .toLowerCase();
  return digest.slice(0, 12).padStart(4, "0");
}

export function ownedNamespaceFor(testInfo?: TestInfo): string {
  if (!testInfo) throw new Error("seed.owned() needs a namespace outside a Playwright test");
  return namespaceForKey(`${testInfo.file}::${testInfo.titlePath.join("::")}`);
}

const OWNED_SETUP_TIMEOUT = 60_000;

type OwnedRun = { id: string; provisional: boolean };

function isUnadmittedScopeRefusal(status: number, body: string): boolean {
  if (status !== 409) return false;
  try {
    return JSON.parse(body)?.detail?.code === "fixture_scope_busy";
  } catch {
    return false;
  }
}

export class SeedAPI {
  private explicitNamespaces = new Map<string, string>();
  private ownedRuns = new Map<string, OwnedRun>();
  private retiredRunIds = new Map<string, string>();
  private workbenchBuilt = new Set<string>();
  private projectNamespaces = new Map<string, string>();
  private taskRunIds = new Map<string, string>();
  private boundPages = new Set<Page>();

  constructor(
    private request: APIRequestContext,
    private testInfo?: TestInfo,
  ) {}

  private seedLogin(email: string): Promise<SeedResponse> {
    return postFixture(`${API_BASE}/api/v1/__test/seed/login`, {
      data: { email },
    });
  }

  async accessToken(email: string): Promise<string> {
    const res = await this.seedLogin(email);
    if (!res.ok()) {
      throw new Error(`seed/login failed: ${res.status()} ${await res.text()}`);
    }
    return ((await res.json()) as { access_token: string }).access_token;
  }

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

  private ownedNamespace(explicit?: string): string {
    if (explicit !== undefined && !/^[a-z0-9]{4,12}$/.test(explicit)) {
      throw new Error("Fixture namespace must contain 4–12 lowercase letters or digits");
    }
    const defaultToken = this.testInfo ? ownedNamespaceFor(this.testInfo) : undefined;
    const token =
      explicit !== undefined
        ? explicit === defaultToken
          ? explicit
          : namespaceForKey(`explicit::${explicit}`)
        : defaultToken;
    if (!token) throw new Error("seed.owned() needs a namespace outside a Playwright test");
    if (explicit !== undefined) this.explicitNamespaces.set(explicit, token);
    return token;
  }

  private ownedScope(token: string, includeRetired = false) {
    const suiteId = process.env.PLAYWRIGHT_FIXTURE_RUN_ID;
    const runId =
      this.ownedRuns.get(token)?.id ?? (includeRetired ? this.retiredRunIds.get(token) : undefined);
    if (!suiteId || !runId) throw new Error("Fixture suite and run identities are required");
    return { namespace: token, suite_id: suiteId, run_id: runId };
  }

  private async postOwnedSeed(
    path: "owned" | "owned-prepare" | "project-roles" | "filtering" | "lidar",
    token: string,
    { data, timeout }: { data?: Record<string, unknown>; timeout?: number } = {},
  ): Promise<SeedResponse> {
    let run = this.ownedRuns.get(token);
    if (run) {
      run.provisional = false;
    } else {
      run = { id: randomUUID(), provisional: true };
      this.ownedRuns.set(token, run);
      this.retiredRunIds.delete(token);
    }
    try {
      const res = await postFixture(`${API_BASE}/api/v1/__test/seed/${path}`, {
        data: { ...data, ...this.ownedScope(token) },
        timeout,
      });
      if (!res.ok()) {
        const status = res.status();
        const body = await res.text();
        if (
          run.provisional &&
          this.ownedRuns.get(token) === run &&
          isUnadmittedScopeRefusal(status, body)
        ) {
          this.ownedRuns.delete(token);
        }
        throw new Error(`seed/${path} failed: ${status} ${body}`);
      }
      return res;
    } finally {
      run.provisional = false;
    }
  }

  async prepareRasterOwned(): Promise<void> {
    await this.postOwnedSeed("owned-prepare", this.ownedNamespace(), { timeout: 60_000 });
  }

  async owned(
    namespace?: string,
    options?: { imageCanvas?: "default" | "720p" | "5k" | "8k" },
  ): Promise<SeedData> {
    const token = this.ownedNamespace(namespace);
    if (this.workbenchBuilt.has(token)) {
      await this.cleanupOwned(token);
    }
    const res = await this.postOwnedSeed("owned", token, {
      data: { image_canvas: options?.imageCanvas ?? "default" },
      timeout: OWNED_SETUP_TIMEOUT,
    });
    this.workbenchBuilt.add(token);
    const data = (await res.json()) as SeedData;
    this.projectNamespaces.set(data.project_id, token);
    const { run_id: runId } = this.ownedScope(token);
    for (const taskId of data.task_ids) this.taskRunIds.set(taskId, runId);
    return data;
  }

  async preparedRasterFault(caseName: RasterFaultCase): Promise<{
    data: SeedData;
    taskId: string;
    healthy: { annotation_id: string };
    corrupt: { annotation_id: string };
  }> {
    if (!this.testInfo) throw new Error("Raster fault fixtures require a Playwright test");
    const entry = preparedRasterFaultEntry(caseName, this.testInfo.retry);
    const response = await postFixture(`${API_BASE}/api/v1/__test/seed/raster-fault-verify`, {
      data: {
        namespace: entry.namespace,
        suite_id: process.env.PLAYWRIGHT_FIXTURE_RUN_ID,
        run_id: entry.run_id,
        project_id: entry.seed.project_id,
        task_id: entry.task_id,
        user_email: entry.seed.annotator_email,
        healthy_annotation_id: entry.healthy_annotation_id,
        corrupt_annotation_id: entry.corrupt_annotation_id,
      },
    });
    if (!response.ok()) {
      throw new Error(
        `Raster fault scope verification failed: ${response.status()} ${await response.text()}`,
      );
    }
    if (this.ownedRuns.has(entry.namespace)) {
      throw new Error("Raster fault namespace is already registered");
    }
    this.ownedRuns.set(entry.namespace, { id: entry.run_id, provisional: false });
    this.workbenchBuilt.add(entry.namespace);
    this.projectNamespaces.set(entry.seed.project_id, entry.namespace);
    for (const taskId of entry.seed.task_ids) this.taskRunIds.set(taskId, entry.run_id);
    return {
      data: entry.seed,
      taskId: entry.task_id,
      healthy: { annotation_id: entry.healthy_annotation_id },
      corrupt: { annotation_id: entry.corrupt_annotation_id },
    };
  }

  async cleanupOwned(namespace: string): Promise<void> {
    const token = this.explicitNamespaces.get(namespace) ?? namespace;
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/owned-cleanup`, {
      data: this.ownedScope(token, true),
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/owned-cleanup failed: ${res.status()} ${await res.text()}`);
    }
    this.workbenchBuilt.delete(token);
    const run = this.ownedRuns.get(token);
    if (run) this.retiredRunIds.set(token, run.id);
    this.ownedRuns.delete(token);
    for (const [projectId, owner] of this.projectNamespaces) {
      if (owner === token) this.projectNamespaces.delete(projectId);
    }
  }

  async cleanupOwnedFixtures(): Promise<void> {
    for (const namespace of [...this.ownedRuns.keys()].reverse()) {
      await this.cleanupOwned(namespace);
    }
  }

  async retireOwnedPages(): Promise<void> {
    const errors: unknown[] = [];
    for (const page of this.boundPages) {
      if (page.isClosed()) continue;
      try {
        await page.close();
      } catch (error) {
        errors.push(error);
      }
    }
    this.boundPages.clear();
    if (errors.length) throw new AggregateError(errors, "Owned page retirement failed");
  }

  async projectRoles(namespace?: string): Promise<ProjectRolesSeedData> {
    const token = this.ownedNamespace(namespace);
    const res = await this.postOwnedSeed("project-roles", token, { timeout: 60_000 });
    return (await res.json()) as ProjectRolesSeedData;
  }

  async filtering(
    options: { namespace?: string; videoReady?: boolean } = {},
  ): Promise<FilteringSeedManifest> {
    const token = this.ownedNamespace(options.namespace);
    const res = await this.postOwnedSeed("filtering", token, {
      data: { video_ready: options.videoReady === true },
      timeout: 120_000,
    });
    return (await res.json()) as FilteringSeedManifest;
  }

  async withOwnedInvitation<T>(
    projectId: string,
    inviterId: string,
    email: string,
    perform: () => Promise<T>,
  ): Promise<T> {
    const namespace = this.projectNamespaces.get(projectId);
    if (!namespace) throw new Error("Invitation project has no owned fixture run");
    const { run_id: runId } = this.ownedScope(namespace);
    const intentId = randomUUID();
    let confirmed = false;
    let failed = false;
    let failure: unknown;
    let result!: T;
    try {
      const response = await postFixture(`${API_BASE}/api/v1/__test/seed/invitation-intent`, {
        data: {
          run_id: runId,
          intent_id: intentId,
          project_id: projectId,
          inviter_id: inviterId,
          email,
        },
      });
      if (!response.ok()) {
        throw new Error(`Invitation intent failed: ${response.status()} ${await response.text()}`);
      }
      const intent = (await response.json()) as { intent_id: string };
      if (intent.intent_id !== intentId) throw new Error("Invitation intent identity changed");
      confirmed = true;
      result = await perform();
    } catch (error) {
      failed = true;
      failure = error;
    }
    try {
      const response = await postFixture(`${API_BASE}/api/v1/__test/seed/invitation-finish`, {
        data: { run_id: runId, intent_id: intentId },
      });
      if (!response.ok() && (confirmed || response.status() !== 404)) {
        throw new Error(`Invitation finish failed: ${response.status()} ${await response.text()}`);
      }
    } catch (cleanupError) {
      failure = failed
        ? new AggregateError([failure, cleanupError], "Invitation and ownership settlement failed")
        : cleanupError;
      failed = true;
    }
    if (failed) throw failure;
    return result;
  }

  async seedLidar(namespace?: string): Promise<SeedLidarData> {
    const token = this.ownedNamespace(namespace);
    const res = await this.postOwnedSeed("lidar", token);
    return (await res.json()) as SeedLidarData;
  }

  async prepareRasterImages(data: SeedData): Promise<void> {
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/prepare-raster-images`, {
      data: {
        run_id: this.projectScope(data.project_id).run_id,
        project_id: data.project_id,
        user_email: data.admin_email,
      },
      timeout: 60_000,
    });
    if (!res.ok()) {
      throw new Error(`seed/prepare-raster-images failed: ${res.status()} ${await res.text()}`);
    }
  }

  async configureRasterMask(projectId: string, enabled: boolean): Promise<void> {
    const runId = this.projectScope(projectId).run_id;
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/configure-raster-mask`, {
      data: { run_id: runId, project_id: projectId, enabled },
    });
    if (!res.ok()) {
      throw new Error(`seed/configure-raster-mask failed: ${res.status()} ${await res.text()}`);
    }
  }

  maskSliceLifecycle(
    taskId: string,
    action: "gc" | "drop_revision" | "expire_revision",
    operationId: string,
  ) {
    const runId = this.taskRunIds.get(taskId);
    if (!runId) throw new Error("Task is not owned by this fixture scope");
    return runMaskSliceLifecycle({
      run_id: runId,
      task_id: taskId,
      action,
      operation_id: operationId,
    });
  }

  private projectScope(projectId: string) {
    const token = this.projectNamespaces.get(projectId);
    if (!token) throw new Error("Project is not owned by this fixture scope");
    return this.ownedScope(token);
  }
  async providerControl(
    projectId: string,
    registryId: string,
    action: "attach" | "detach" | "hold" | "release" | "fail_all" | "fail_next" | "requests",
    value?: boolean | number,
  ): Promise<{
    requests: Array<{
      tasks: Array<{ id: string }>;
      context: Record<string, unknown>;
      receivedAt: number;
      responseStatus?: number;
    }>;
  }> {
    const response = await postFixture(`${API_BASE}/api/v1/__test/seed/provider-control`, {
      data: { ...this.projectScope(projectId), registry_id: registryId, action, value },
    });
    if (!response.ok()) {
      throw new Error(
        `Fixture provider control failed: ${response.status()} ${await response.text()}`,
      );
    }
    return response.json();
  }

  async videoTask(projectId: string): Promise<{ task_id: string }> {
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/video-task`, {
      data: { ...this.projectScope(projectId), project_id: projectId },
    });
    if (!res.ok()) {
      throw new Error(`seed/video-task failed: ${res.status()} ${await res.text()}`);
    }
    const result = (await res.json()) as { task_id: string };
    this.taskRunIds.set(result.task_id, this.projectScope(projectId).run_id);
    return result;
  }

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
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/video-webcodecs`, {
      data: {
        ...this.projectScope(projectId),
        project_id: projectId,
        ...(options?.batchId ? { batch_id: options.batchId } : {}),
        fixture: options?.fixture ?? "h264-baseline-gop12",
        chunk_status: options?.chunkStatus ?? "ready",
      },
    });
    if (!res.ok()) {
      throw new Error(`seed/video-webcodecs failed: ${res.status()} ${await res.text()}`);
    }
    const result = (await res.json()) as {
      task_id: string;
      dataset_item_id: string;
      chunk_id: number;
      chunk_size_frames: number;
      frame_expectations: Record<string, unknown>;
    };
    this.taskRunIds.set(result.task_id, this.projectScope(projectId).run_id);
    return result;
  }

  async videoIssueContextHistory(
    projectId: string,
    feedbackId: string,
  ): Promise<{
    feedback_id: string;
    anchor_position: Record<string, unknown>;
  }> {
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/video-issue-context-history`, {
      data: { ...this.projectScope(projectId), feedback_id: feedbackId },
    });
    if (!res.ok()) {
      throw new Error(
        `seed/video-issue-context-history failed: ${res.status()} ${await res.text()}`,
      );
    }
    return res.json();
  }

  async videoWebCodecsTransitionReady(
    datasetItemId: string,
    chunkId = 0,
  ): Promise<{ status: "ready" | "pending" | "failed" }> {
    const res = await postFixture(
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
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/tracker-review`, {
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
    const runId = this.taskRunIds.get(taskId);
    if (!runId) throw new Error("Native mask task has no fixture run");
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/native-mask-candidate`, {
      data: {
        run_id: runId,
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

  async injectRasterMask(opts: {
    taskId: string;
    userEmail: string;
    variant?: RasterMaskFixtureVariant;
    label?: string;
    locked?: boolean;
    canvas?: RasterMaskFixtureCanvas;
  }): Promise<SeedRasterMaskData> {
    const runId = this.taskRunIds.get(opts.taskId);
    if (!runId) throw new Error("Raster task has no fixture run");
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/inject-raster-mask`, {
      data: {
        run_id: runId,
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

  async injectRasterPrediction(opts: {
    taskId: string;
    userEmail: string;
    label?: string;
  }): Promise<SeedRasterPredictionData> {
    const runId = this.taskRunIds.get(opts.taskId);
    if (!runId) throw new Error("Raster task has no fixture run");
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/inject-raster-prediction`, {
      data: {
        run_id: runId,
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

  async injectToken(page: Page, email: string, baseURL?: string): Promise<void> {
    this.boundPages.add(page);
    const res = await this.seedLogin(email);
    if (!res.ok()) throw new Error(`seed/login failed: ${res.status()} ${await res.text()}`);
    const body = (await res.json()) as { access_token: string; user: unknown };
    await this.setPetEnabled(email, false, body.access_token);
    const target = baseURL ?? process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3001";
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

  async injectPrediction(opts: {
    taskId: string;
    projectId: string;
    label: string;
    polygon: [number, number][];
    score?: number;
  }): Promise<{ prediction_id: string }> {
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/inject-prediction`, {
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

  async advanceTask(opts: {
    taskId: string;
    toStatus: "pending" | "annotating" | "submitted" | "review" | "completed" | "rejected";
    annotatorEmail?: string;
    reviewerEmail?: string;
  }): Promise<void> {
    const res = await postFixture(`${API_BASE}/api/v1/__test/seed/advance_task`, {
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
  owned: SeedData;
};

export const test = base.extend<Fixtures, { fixtureOwner: void }>({
  fixtureOwner: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      try {
        await use(undefined);
      } finally {
        await closeFixtureTransport();
      }
    },
    { scope: "worker", auto: true },
  ],
  seed: [
    async ({ request }, use, testInfo) => {
      const api = new SeedAPI(request, testInfo);
      const failures: unknown[] = [];
      try {
        if (process.env.PLAYWRIGHT_RASTER_OFFLINE === "1") {
          await api.prepareRasterOwned();
        }
        await use(api);
      } catch (error) {
        failures.push(error);
      }
      try {
        await api.retireOwnedPages();
      } catch (error) {
        failures.push(error);
      }
      try {
        await api.cleanupOwnedFixtures();
      } catch (error) {
        failures.push(error);
      }
      if (failures.length === 1) {
        throw failures[0];
      }
      if (failures.length > 1) {
        throw new AggregateError(failures, "Seed fixture failed");
      }
    },
    { timeout: 120_000 },
  ],
  owned: [
    async ({ seed }, use) => {
      await use(await seed.owned());
    },
    { timeout: OWNED_SETUP_TIMEOUT },
  ],
});

export { expect };
