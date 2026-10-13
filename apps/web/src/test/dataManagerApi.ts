import { http, HttpResponse } from "msw";

import type { ProjectAccessResponse, ProjectCapability, ProjectResponse } from "@/api/projects";
import type {
  DataManagerSchema,
  DataManagerSummary,
  DataManagerTask,
  ProjectTaskQueryPayload,
  ProjectTaskView,
} from "@/api/taskViews";
import { server } from "@/mocks/server";
import { TEST_USER_ID } from "@/test/auth";

export interface DataManagerApiOptions {
  capabilities?: ProjectCapability[];
  views?: ProjectTaskView[];
  tasks?: DataManagerTask[];
  schemaStatus?: number;
  createdView?: ProjectTaskView;
  createdViewGate?: Promise<void>;
}

export interface CapturedRequests {
  taskQueries: ProjectTaskQueryPayload[];
  createdViewPayloads: unknown[];
  schemaRequests: number;
}

export const PROJECT_ID = "p1";

/** Platform employee holding project-scoped read capabilities, not a global role. */
export const READ_CAPABILITIES: ProjectCapability[] = ["project.read", "task.read"];

const PROJECT = {
  id: PROJECT_ID,
  name: "测试项目",
  display_id: "P-1",
  owner_id: TEST_USER_ID,
} as unknown as ProjectResponse;

export function accessResponse(capabilities: ProjectCapability[]): ProjectAccessResponse {
  const manages = capabilities.includes("project.manage");
  return {
    access_kind: manages ? "owner" : "member",
    capabilities,
    is_manager: manages,
    membership_id: "membership-1",
    membership_version: 3,
    platform_role: "employee",
    project_id: PROJECT_ID,
    project_role: "annotator",
    user_id: TEST_USER_ID,
  };
}

export const SCHEMA: DataManagerSchema = {
  entity_scope: "tasks",
  available_entity_scopes: ["tasks"],
  project_kind: { data_type: "image", type_key: "image", scene_mode: false },
  tool_units: [],
  filter_fields: [
    {
      key: "ai.low_confidence_prediction_shape_count",
      label: "低置信候选",
      group: "AI",
      value_type: "number",
      operators: ["gt"],
      options: [],
      expensive: false,
      tool_unit_id: null,
      attribute_key: null,
    },
    {
      key: "task.status",
      label: "任务状态",
      group: "任务",
      value_type: "select",
      operators: ["eq", "in"],
      options: [],
      expensive: false,
      tool_unit_id: null,
      attribute_key: null,
    },
    {
      key: "task.assignee",
      label: "标注员",
      group: "人员",
      value_type: "text",
      operators: ["eq"],
      options: [],
      expensive: false,
      tool_unit_id: null,
      attribute_key: null,
    },
  ],
  columns: [
    {
      key: "display_id",
      label: "任务",
      group: "任务",
      default: true,
      expensive: false,
      sortable: false,
      sort_field: null,
    },
    {
      key: "unresolved_issue_count",
      label: "未解决问题",
      group: "质量",
      default: true,
      expensive: true,
      sortable: true,
      sort_field: "unresolved_issue_count",
    },
    {
      key: "comment_count",
      label: "评论",
      group: "讨论",
      default: true,
      expensive: true,
      sortable: true,
      sort_field: "comment_count",
    },
  ],
  default_columns: ["display_id"],
  sort_fields: [{ value: "task.created_at", label: "创建时间" }],
  metrics: [],
  builtin_views: ["all"],
};

export function createTaskView(overrides: Partial<ProjectTaskView>): ProjectTaskView {
  return {
    id: null,
    key: null,
    project_id: PROJECT_ID,
    owner_id: TEST_USER_ID,
    name: "视图",
    visibility: "private",
    entity_scope: "tasks",
    filter_json: {},
    sort_json: [{ field: "task.created_at", direction: "asc" }],
    columns_json: ["display_id"],
    builtin: false,
    task_count: 1,
    result_count: 1,
    created_at: null,
    updated_at: null,
    invalid_fields: [],
    ...overrides,
  };
}

export const DEFAULT_VIEWS: ProjectTaskView[] = [
  createTaskView({
    key: "all",
    owner_id: null,
    name: "全部任务",
    visibility: "project",
    builtin: true,
  }),
  createTaskView({ id: "v1", name: "组合视图" }),
  createTaskView({
    id: "v2",
    name: "第二视图",
    filter_json: { field: "task.status", op: "eq", value: "pending" },
  }),
];

export function createDataManagerTask(overrides: Partial<DataManagerTask> = {}): DataManagerTask {
  return {
    id: "task-1",
    project_id: PROJECT_ID,
    display_id: "T-1",
    file_name: "one.png",
    file_url: null,
    file_type: "image",
    tags: [],
    status: "pending",
    assignee_id: null,
    assignee: null,
    reviewer: null,
    is_labeled: false,
    overlap: 0,
    total_annotations: 0,
    total_predictions: 0,
    batch_id: null,
    sequence_order: null,
    image_width: 100,
    image_height: 100,
    thumbnail_url: null,
    blurhash: null,
    video_metadata: null,
    submitted_at: null,
    reviewer_id: null,
    reviewer_claimed_at: null,
    reviewed_at: null,
    reject_reason: null,
    skip_reason: null,
    skipped_at: null,
    reopened_count: 0,
    last_reopened_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: null,
    annotation_count: 0,
    prediction_count: 0,
    avg_prediction_confidence: null,
    unresolved_issue_count: 0,
    unresolved_feedback_count: 0,
    comment_count: 0,
    model_versions: [],
    scene_name: null,
    frame_index: null,
    last_activity_at: null,
    annotation_source_counts: { manual: 0, prediction_based: 0, ai_tracker: 0, interpolated: 0 },
    track_count: 0,
    pending_prediction_shape_count: 0,
    low_confidence_prediction_shape_count: 0,
    pending_tracker_job_count: 0,
    keyframe_count: 0,
    outside_range_count: 0,
    camera_count: 0,
    calibration_issue_count: 0,
    scene_total_frames: null,
    ...overrides,
  };
}

export const DEFAULT_TASKS = [createDataManagerTask()];

export const SUMMARY: DataManagerSummary = {
  scope: { visible_task_total: 1, matched_task_total: 1 },
  task_status: { pending: 1 },
  annotations: {
    total: 0,
    single_frame: 0,
    tracked: 0,
    distinct_tracks: 0,
    imported: 0,
    by_source: {},
    by_class: {},
    by_tool_unit: {},
    by_type: {},
  },
  ai_review: {
    prediction_shapes: 0,
    low_confidence_prediction_shapes: 0,
    tracker_jobs: 0,
    confidence_threshold: 0.5,
    by_model_version: {},
    confidence_buckets: {},
  },
  unresolved_feedback: 0,
  attributes: [],
  kind_metrics: {},
};

/**
 * Describe the data-manager responses at the API boundary the page already
 * uses. Register with `installDataManagerApi()` in a `beforeEach`; the global
 * afterEach resets these runtime handlers.
 */
export function installDataManagerApi(options: DataManagerApiOptions = {}): CapturedRequests {
  const capabilities = options.capabilities ?? READ_CAPABILITIES;
  const views = options.views ?? DEFAULT_VIEWS;
  const tasks = options.tasks ?? DEFAULT_TASKS;
  const captured: CapturedRequests = {
    taskQueries: [],
    createdViewPayloads: [],
    schemaRequests: 0,
  };

  server.use(
    http.get("*/api/v1/projects/p1", () => HttpResponse.json(PROJECT)),
    http.get("*/api/v1/projects/p1/access", () => HttpResponse.json(accessResponse(capabilities))),
    http.get("*/api/v1/projects/p1/data-manager/schema", () => {
      captured.schemaRequests += 1;
      if (options.schemaStatus && options.schemaStatus >= 400) {
        return new HttpResponse(null, { status: options.schemaStatus });
      }
      return HttpResponse.json(SCHEMA);
    }),
    http.get("*/api/v1/projects/p1/task-views", () => HttpResponse.json({ items: views })),
    http.post("*/api/v1/projects/p1/tasks/query", async ({ request }) => {
      const payload = (await request.json()) as ProjectTaskQueryPayload;
      captured.taskQueries.push(payload);
      return HttpResponse.json({ items: tasks, total: tasks.length, limit: 50, offset: 0 });
    }),
    http.post("*/api/v1/projects/p1/tasks/:taskId/data-manager/matches", ({ params }) =>
      HttpResponse.json({
        task_id: String(params.taskId),
        items: [],
        total: 0,
        limit: 100,
        offset: 0,
      }),
    ),
    http.post("*/api/v1/projects/p1/data-manager/summary", () => HttpResponse.json(SUMMARY)),
    http.get("*/api/v1/tasks/:taskId", ({ params }) =>
      HttpResponse.json(
        createDataManagerTask({
          id: String(params.taskId),
          display_id: String(params.taskId) === "task-deep" ? "T-deep" : "T-task",
          file_name: `${String(params.taskId)}.png`,
        }),
      ),
    ),
    http.post("*/api/v1/projects/p1/task-views", async ({ request }) => {
      captured.createdViewPayloads.push(await request.json());
      if (options.createdViewGate) await options.createdViewGate;
      return HttpResponse.json(
        options.createdView ?? createTaskView({ id: "v-created", name: "新视图" }),
      );
    }),
  );

  return captured;
}
