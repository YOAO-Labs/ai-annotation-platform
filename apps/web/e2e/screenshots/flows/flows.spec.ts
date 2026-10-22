import {
  drainRecordingCleanup,
  recoverRecordingAnnotationIds,
} from "../_helpers/recording-cleanup";
/**
 * M3 · 流程录制 spec。
 *
 * 执行：`pnpm screenshots:flows`（流程预演，不产正式素材）
 *
 * 前置条件同 screenshots.spec.ts。
 * Marketing captures archive qualified sources; docs only rehearses. Publish with docs:media:derive.
 */
import { test } from "../../fixtures/seed";
import type { ScreenshotSeedCatalog } from "../../fixtures/seed";
import { expect, type Page } from "@playwright/test";
import { runE2eQuickstart } from "./e2e-quickstart";
import { runAiPredictionImport } from "./ai-prediction-import";
import { runAiPreannotate } from "./ai-preannotate";
import { runDetectionOnlineTraining } from "./detection-online-training";
import { runReviewReject } from "./review-reject";
import { runBatchBulkActions } from "./batch-bulk-actions";
import { runAiPreVariantSelector } from "./ai-pre-variant-selector";
import { runRotatedBbox } from "./rotated-bbox";
import { runBboxDraw } from "./bbox-draw";
import { runWorkspaceLayoutBasics, workspaceLayoutBasicsLayout } from "./workspace-layout-basics";
import { runWorkspaceLayoutPersistence } from "./workspace-layout-persistence";
import { runWorkspaceNamedPreset } from "./workspace-named-preset";
import { runVideoCommentBadges } from "./video-comment-badges";
import { runDiscussionMentionReturn } from "./discussion-mention-return";
import { runMaskSliceInstances } from "./mask-slice-instances";
import { runDiscussionTargetDrafts } from "./discussion-target-drafts";
import { runTaskDrawingComment } from "./task-drawing-comment";
import { runVideoIssueResolution } from "./video-issue-resolution";
import { runProjectGuideAutosave } from "./project-guide-autosave";
import { runProjectInvitationJoin } from "./project-invitation-join";
import { runPolylineDraw } from "./polyline-draw";
import { runPolygonDraw } from "./polygon-draw";
import { runPolygonBoundaryTrace } from "./polygon-boundary-trace";
import { runPolygonSliceUndo } from "./polygon-slice-undo";
import { runMaskDraw } from "./mask-draw";
import { runVideoTrack } from "./video-track";
import { runVideoTimelineZoom } from "./video-timeline-zoom";
import { runVideoTrackerRange } from "./video-tracker-range";
import {
  runVideoTrackBatchPropagate,
  type VideoTrackBatchPropagateCleanupRecord,
} from "./video-track-batch-propagate";
import { runVideoPropagateTrackVsCopy } from "./video-propagate-track-vs-copy";
import { runVideoMaskTrackEdit } from "./video-mask-track-edit";
import { runAiTrackerPanel } from "./ai-tracker-panel";
import { runPointcloudControls } from "./pointcloud-controls";
import { runPointcloudView } from "./pointcloud-view";
import { runPointcloudCameraSeed3dBox } from "./pointcloud-camera-seed-3d-box";
import { runPointcloudCrossframeTrack } from "./pointcloud-crossframe-track";
import { runPointcloudBillboardLabel } from "./pointcloud-billboard-label";
import { runPointcloudPanelLayout } from "./pointcloud-panel-layout";
import {
  runStorageConnectorCreateTest,
  STORAGE_CONNECTOR_RECORDING_NAME,
} from "./storage-connector-create-test";
import { runVideoDraw } from "./video-draw";
import { runVideoChapter, type VideoChapterCleanupRecord } from "./video-chapter";
import { runVideoMultiSeedTracking } from "./video-multi-seed-tracking";
import { runVideoTimelinePredictionNavigation } from "./video-timeline-prediction-navigation";
import { runVideoTrackerTextDiscovery } from "./video-tracker-text-discovery";
import { runVideoTrackerComboDiscovery } from "./video-tracker-combo-discovery";
import { runVideoMaskCorrectionPropagate } from "./video-mask-correction-propagate";
import { runPipelineTemplateCreate } from "./pipeline-template-create";
import { runPipelineApplyProject, type PipelineApplyCleanupRecord } from "./pipeline-apply-project";
import { runJobsRetryRecovery } from "./jobs-retry-recovery";
import {
  runModelMarketGpuResourceOverview,
  runModelMarketRuntimePartialFailure,
  runModelMarketRuntimePool,
  runModelMarketVideoPool,
} from "./model-market-runtime-pool";
import { runProjectMlRouting } from "./project-ml-routing";
import { runBackgroundExportDownload } from "./background-export-download";
import {
  PROJECT_CREATE_RECORDING_NAME,
  runProjectCreateExistingResources,
} from "./project-create-existing-resources";
import { runVideoTrackCarryover } from "./video-track-carryover";
import { runLargeImageProgressive } from "./large-image-progressive";
import { runLargeImagePyramidRecovery } from "./large-image-pyramid-recovery";
import { runLargeImageMaskLimit } from "./large-image-mask-limit";
import { runPlatformOverview } from "./platform-overview";
import { runProjectActionsMenu } from "./project-actions-menu";
import { runJobsBellActive } from "./jobs-bell-active";
import { runVideoTrackerJobStates } from "./video-tracker-job-states";
import { runSmartScribble } from "./smart-scribble";
import { runHotkeyCheatSheet } from "./hotkey-cheatsheet";
import { runSamInteractive, runSamToolRecording, type SamRecordingTool } from "./sam-interactive";
import { runOcrInference, type OcrCleanupRecord } from "./ocr-inference";
import { runCurrentTaskImageInference } from "./current-task-image-inference";
import {
  runCurrentFrameVideoInference,
  type VideoFrameInferenceCleanupRecord,
} from "./current-frame-video-inference";
import {
  runSecondaryInferenceAttribute,
  type SecondaryInferenceCleanupRecord,
} from "./secondary-inference-attribute";
import {
  prepareLiveCandidateReview,
  runCandidateKeyboardReview,
} from "./candidate-keyboard-review";
import {
  runCandidateReviewLifecycle,
  type CandidateReviewCleanupRecord,
} from "./candidate-review-lifecycle";
import { recordingAnchor } from "./_canvas";
import { PROJECT_ONBOARDING_RECORDING_NAME, runProjectOnboarding } from "./project-onboarding";
import { installRecordingWorkbenchLayout } from "./_workbench-layout";
import type { SourceGifVariant } from "../_helpers/flow-manifest";
import {
  recordingPlan,
  recordingInference,
  assertRecordingMode,
  recordingTimeout,
} from "../recording-plan.mjs";
import {
  archiveMarketingMaster,
  clipFromEpochWindow,
  getMarketingRunContext,
  MARKETING_PROJECT_NAME,
} from "../_helpers/marketing-recorder";
import { marketingAssetSpec } from "../_helpers/marketing-assets";
import {
  discardExternalMarketingRecording,
  startExternalMarketingRecording,
  stopExternalMarketingRecording,
} from "../_helpers/marketing-external-recorder";
import { applyScreenshotTheme, installScreenshotEnvironment } from "../environment";
import { loadScreenshotCatalog } from "../catalog-runtime";
import { execFileSync } from "child_process";
import path from "path";

const HERE = decodeURIComponent(new URL(".", import.meta.url).pathname);
const REPO_ROOT = HERE.replace(/\/apps\/web\/e2e\/screenshots\/flows\/?$/, "");
// nuScenes-mini scene-0061 frame 0, truck annotation 83d881a6b3d94ef3a3bc3b585cc514f8.
// Official world box transformed into the LiDAR timestamp ego frame (ISO 8855).
const NUSCENES_RECORDING_BOX = {
  type: "box_3d",
  center: [16.19298371687693, 4.529423348258203, 1.8934624374100686],
  size: [10.201, 2.877, 3.595],
  rotation: [0.021297334519504878, -0.010713029376386173, 0.026594679170021198],
  convention_at_create: "iso_8855",
};
const DOCS_IMAGES = path.join(REPO_ROOT, "docs-site/user-guide/images");
const MARKETING_ARCHIVE_ROOT = path.join(REPO_ROOT, ".artifacts/marketing");
const VALIDATE_ONLY = process.env.SCREENSHOT_VALIDATE_ONLY === "1";
const SKIP_CAPTURE_SEED_REPAIR = process.env.SCREENSHOT_SKIP_SEED_REPAIR === "1";
const SELECTED_CAPTURE = process.env.SCREENSHOT_RECORDING_FLOWS
  ? recordingPlan(
      process.env.SCREENSHOT_RECORDING_FLOWS.split(","),
      process.env.SCREENSHOT_RECORDING_PROFILE,
    )
  : null;
function setFlowTimeout(requested: number): void {
  test.setTimeout(recordingTimeout(requested, test.info().timeout, Boolean(SELECTED_CAPTURE)));
}

const VIDEO_ML_CAPTURE_FLOWS = new Set([
  "video-tracker-range",
  "video-tracker-cross-frame-points",
  "video-tracker-positive-negative",
  "video-tracker-box-seed",
  "video-tracker-text-discovery",
  "video-tracker-combo-discovery",
  "video-timeline-prediction-navigation",
  "video-mask-correction-propagate",
  "video-propagate-track-vs-copy",
  "video-track-batch-propagate",
  "current-frame-video-inference",
  "ai-tracker-panel",
  "video-tracker-job-states",
]);

let cached: ScreenshotSeedCatalog | null = null;
const flowInferenceEvidence: Record<string, unknown> = {};
const flowBehaviorEvidence: Record<string, unknown> = {};
type VideoAnnotationCleanupRecord = Pick<
  VideoFrameInferenceCleanupRecord,
  "projectId" | "taskId" | "annotationIds"
>;
const videoAnnotationCleanupRecords: VideoAnnotationCleanupRecord[] = [];
type VideoTrackerRecordingCleanupRecord = VideoTrackBatchPropagateCleanupRecord & {
  accessToken: string;
  baselineAnnotationIds: string[];
  annotations: VideoAnnotationCleanupRecord;
};
const videoTrackerRecordingCleanupRecords: VideoTrackerRecordingCleanupRecord[] = [];
const videoChapterCleanupRecords: Array<VideoChapterCleanupRecord & { accessToken: string }> = [];
const ocrCleanupRecords: OcrCleanupRecord[] = [];
const videoFrameInferenceCleanupRecords: VideoFrameInferenceCleanupRecord[] = [];
const secondaryInferenceCleanupRecords: SecondaryInferenceCleanupRecord[] = [];
const candidateReviewCleanupRecords: CandidateReviewCleanupRecord[] = [];
const pipelineApplyCleanupRecords: PipelineApplyCleanupRecord[] = [];
const jobsRetryCleanupRecords: Array<{ projectId: string; taskId: string }> = [];
const backgroundExportCleanupRecords: Array<{
  projectId: string;
  taskId: string;
  jobId?: string;
}> = [];

const FLOW_SOURCE_BY_ASSET: Record<string, string> = {
  "project-onboarding": "project-onboarding.ts",
  "ai-assisted-annotation": "sam-interactive.ts",
  "ocr-real-scene": "ocr-inference.ts",
  "secondary-inference-attribute": "secondary-inference-attribute.ts",
  "ai-prediction-import": "ai-prediction-import.ts",
  "review-reject": "review-reject.ts",
  "pipeline-apply-project": "pipeline-apply-project.ts",
  "jobs-retry-recovery": "jobs-retry-recovery.ts",
  "project-ml-routing": "project-ml-routing.ts",
  "ai-preannotate": "ai-preannotate.ts",
  "sam-tools/smart-point": "sam-interactive.ts",
  "sam-tools/smart-box": "sam-interactive.ts",
  "sam-tools/exemplar": "sam-interactive.ts",
  "video-tracker-cross-frame-points": "video-multi-seed-tracking.ts",
  "video-tracker-positive-negative": "video-multi-seed-tracking.ts",
  "video-tracker-box-seed": "video-multi-seed-tracking.ts",
  "video-timeline-prediction-navigation": "video-timeline-prediction-navigation.ts",
  "video-propagate-track-vs-copy": "video-propagate-track-vs-copy.ts",
  "video-tracker-text-discovery": "video-tracker-text-discovery.ts",
  "video-tracker-combo-discovery": "video-tracker-combo-discovery.ts",
  "video-mask-correction-propagate": "video-mask-correction-propagate.ts",
  "pipeline-template-create": "pipeline-template-create.ts",
  "model-market-runtime-pool": "model-market-runtime-pool.ts",
  "model-market-video-pool": "model-market-runtime-pool.ts",
  "model-market-runtime-partial-failure": "model-market-runtime-pool.ts",
  "model-market-gpu-resource-overview": "model-market-runtime-pool.ts",
  "background-export-download": "background-export-download.ts",
  "project-create-existing-resources": "project-create-existing-resources.ts",
  "large-image-mask-limit": "large-image-mask-limit.ts",
  "platform-overview": "platform-overview.ts",
  "project-actions-menu": "project-actions-menu.ts",
  "jobs-bell-active": "jobs-bell-active.ts",
  "video-tracker-job-states": "video-tracker-job-states.ts",
  "workspace-layout-persistence": "workspace-layout-persistence.ts",
  "workspace-named-preset": "workspace-named-preset.ts",
  "polygon-boundary-trace": "polygon-boundary-trace.ts",
  "polygon-slice-undo": "polygon-slice-undo.ts",
  "video-comment-badges": "video-comment-badges.ts",
  "discussion-mention-return": "discussion-mention-return.ts",
  "mask-slice-instances": "mask-slice-instances.ts",
  "discussion-target-drafts": "discussion-target-drafts.ts",
  "task-drawing-comment": "task-drawing-comment.ts",
  "video-issue-resolution": "video-issue-resolution.ts",
  "project-guide-autosave": "project-guide-autosave.ts",
  "project-invitation-join": "project-invitation-join.ts",
};

function flowWatchPaths(assetId: string): string[] {
  const inferred = `${assetId.replace(/^sam-tools\//, "sam-")}.ts`;
  const sourceFile = FLOW_SOURCE_BY_ASSET[assetId] ?? inferred;
  const paths = [
    `apps/web/e2e/screenshots/flows/${sourceFile}`,
    "apps/web/e2e/screenshots/flows/_canvas.ts",
    "apps/web/e2e/screenshots/flows/_workbench-layout.ts",
    "apps/web/e2e/screenshots/environment.ts",
    "apps/web/scripts/media-derivation.mjs",
    "apps/web/e2e/fixtures/seed.ts",
    "apps/api/app/services/screenshot_seed_spec.py",
    "apps/api/app/services/screenshot_seed_backends.py",
  ];
  if (assetId === "project-onboarding")
    paths.push(
      "apps/web/e2e/screenshots/flows/project-create-existing-resources.ts",
      "apps/web/src/pages/Login/LoginPage.tsx",
      "apps/web/src/components/projects/CreateProjectWizard.tsx",
      "apps/web/src/components/projects/steps/Step7Success.tsx",
    );
  if (
    [
      "bbox-draw",
      "rotated-bbox",
      "polyline-draw",
      "polygon-draw",
      "mask-draw",
      "ai-assisted-annotation",
    ].includes(assetId) ||
    assetId.startsWith("sam-tools/")
  ) {
    paths.push("apps/web/e2e/screenshots/flows/_image-drawing.ts");
  }
  if (assetId === "sam-tools/smart-point")
    paths.push("apps/web/e2e/screenshots/flows/_sam-recording-candidates.ts");
  if (["video-draw", "video-track-carryover", "video-mask-track-edit"].includes(assetId))
    paths.push(
      "apps/web/e2e/screenshots/flows/_video-keyframe-recording.ts",
      "apps/web/e2e/screenshots/flows/_video-keyframe-evidence.ts",
    );
  if (["video-timeline-zoom", "video-chapter"].includes(assetId))
    paths.push("apps/web/e2e/screenshots/flows/_video-timeline.ts");
  if (assetId === "smart-scribble")
    paths.push(
      "apps/web/e2e/screenshots/flows/_smart-scribble-evidence.ts",
      "apps/web/e2e/screenshots/flows/_sam-recording-candidates.ts",
    );
  if (assetId === "candidate-review-lifecycle")
    paths.push("apps/web/e2e/screenshots/flows/candidate-keyboard-review.ts");
  if (assetId === "jobs-retry-recovery") {
    paths.push("apps/api/scripts/screenshot_job_recovery_fixture.py");
  }
  if (assetId === "review-reject") {
    paths.push("apps/api/scripts/screenshot_review_reject_fixture.py");
  }
  if (assetId === "background-export-download") {
    paths.push("apps/api/scripts/screenshot_background_export_fixture.py");
  }
  if (assetId === "storage-connector-create-test") {
    paths.push(
      "apps/web/src/components/connections/StorageConnectionsPanel.tsx",
      "apps/api/app/api/v1/storage_connections.py",
      "apps/api/app/services/storage_connection.py",
      "apps/api/app/services/connector_guard.py",
    );
  }
  return paths.filter((candidate, index, all) => all.indexOf(candidate) === index);
}

test.beforeAll(({ browserName: _browserName }, testInfo) => {
  assertRecordingMode(
    testInfo.project.name === MARKETING_PROJECT_NAME ? "marketing" : "docs",
    VALIDATE_ONLY,
  );
  screenshotDatabaseEnv();
  cached = loadScreenshotCatalog();
});

function screenshotDatabaseEnv(): NodeJS.ProcessEnv {
  const databaseUrl = process.env.SCREENSHOT_DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("[flows] 缺少 SCREENSHOT_DATABASE_URL，拒绝在未确认的数据库上录制或清理");
  }
  const databaseName = databaseUrl.replace(/\?.*$/, "").split("/").at(-1) ?? "";
  if (!databaseName.endsWith("_test") && !databaseName.endsWith("_e2e")) {
    throw new Error("[flows] SCREENSHOT_DATABASE_URL 必须指向名称以 _test 或 _e2e 结尾的隔离库");
  }
  return {
    ...process.env,
    DATABASE_URL: databaseUrl,
    MIGRATION_DATABASE_URL: databaseUrl,
    PYTHONPATH: ".",
  };
}

function screenshotBackendMode(catalog: ScreenshotSeedCatalog): "stub" | "live" {
  const backends = Object.values(catalog.projects)
    .map((project) => project.ml_backend?.name)
    .filter((name): name is string => Boolean(name));
  return backends.length > 0 && backends.every((name) => name === "mock-v2-backend")
    ? "stub"
    : "live";
}

function repairScreenshotProfile(mode: "stub" | "live", silent = false): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/seed.py",
      "--profile",
      "screenshots",
      "--offline",
      "--repair",
      "--ml-backend-mode",
      mode,
      ...(process.env.SCREENSHOT_BACKEND_REQUIREMENTS === undefined
        ? []
        : ["--backend-requirements", process.env.SCREENSHOT_BACKEND_REQUIREMENTS]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: silent ? "pipe" : "inherit",
    },
  );
}

// flow 会修改任务状态、标注和预标注作业。结束后由 screenshots profile
// 重建自己管理的固定项目，不再按几何类型猜测并删除数据。
// Playwright 要求 hook 的 fixture 参数使用对象解构；此处确实不消费任何 fixture。
// eslint-disable-next-line no-empty-pattern
test.afterAll(async ({}, testInfo) => {
  if (!cached) return;
  // 推理完成时已清一次；整组结束再幂等清理一次可变业务痕迹，
  // 然后才重建 seed。审计表是平台不可变安全记录，录制器不绕过该约束。
  await cleanupRecordingRecords();
  // Playwright 会在单项失败后重启 worker，并在旧 worker 上执行 afterAll。
  // marketing-master 的 catalog 由 globalSetup 只读取一次；此时重建固定项目会让
  // 后续 worker 继续使用已经失效的项目 / 任务 ID，造成整批录制级联跳回 Dashboard。
  // 营销录制使用隔离库，由外层录制流程在整批开始前准备、结束后统一恢复。
  if (testInfo.project.name === MARKETING_PROJECT_NAME) return;
  if (SKIP_CAPTURE_SEED_REPAIR) return;
  repairScreenshotProfile(screenshotBackendMode(cached));
});

async function cleanupRecordingRecords(): Promise<void> {
  await drainRecordingCleanup(ocrCleanupRecords, cleanupOcrRecording);
  await drainRecordingCleanup(videoFrameInferenceCleanupRecords, cleanupVideoFrameInference);
  await drainRecordingCleanup(videoTrackerRecordingCleanupRecords, cleanupVideoTrackerRecording);
  await drainRecordingCleanup(videoAnnotationCleanupRecords, cleanupVideoFrameInference);
  await drainRecordingCleanup(videoChapterCleanupRecords, cleanupVideoChapter);
  await drainRecordingCleanup(secondaryInferenceCleanupRecords, cleanupSecondaryInference);
  await drainRecordingCleanup(candidateReviewCleanupRecords, cleanupCandidateReview);
  await drainRecordingCleanup(pipelineApplyCleanupRecords, cleanupPipelineApply);
  await drainRecordingCleanup(jobsRetryCleanupRecords, (record) => {
    manageJobsRetryFixture("cleanup", record);
  });
  await drainRecordingCleanup(backgroundExportCleanupRecords, (record) => {
    manageBackgroundExportFixture("cleanup", record);
  });
}

function cleanupOcrRecording(record: OcrCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      "--celery-task-id",
      record.celeryTaskId,
      ...record.annotationIds.flatMap((annotationId) => ["--annotation-id", annotationId]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function registerVideoAnnotationCleanup(
  catalog: ScreenshotSeedCatalog,
): VideoAnnotationCleanupRecord {
  const record = {
    projectId: String(catalog.projects.video_demo.id),
    taskId: String(catalog.projects.video_demo.tasks.tracking.id),
    annotationIds: [] as string[],
  };
  videoAnnotationCleanupRecords.push(record);
  return record;
}

async function cleanupVideoChapter(
  record: VideoChapterCleanupRecord & { accessToken: string },
): Promise<void> {
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const response = await fetch(
    `${apiBase}/api/v1/videos/${record.datasetItemId}/chapters/${record.chapterId}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${record.accessToken}` },
    },
  );
  if (!response.ok && response.status !== 404)
    throw new Error(`Recording chapter cleanup: HTTP ${response.status}`);
}

function cleanupVideoFrameInference(record: VideoAnnotationCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-key",
      "video_demo",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...record.annotationIds.flatMap((annotationId) => ["--annotation-id", annotationId]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function cleanupVideoTrackBatchPropagate(record: VideoTrackBatchPropagateCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-key",
      "video_demo",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...record.videoTrackerJobIds.flatMap((jobId) => ["--video-tracker-job-id", jobId]),
      ...record.sourceAnnotationIds.flatMap((annotationId) => ["--annotation-id", annotationId]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

async function readRecordingAnnotations(taskId: string, accessToken: string): Promise<unknown> {
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const response = await fetch(`${apiBase}/api/v1/tasks/${taskId}/annotations`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`Recording annotation recovery: HTTP ${response.status}`);
  return response.json();
}

async function cleanupVideoTrackerRecording(
  record: VideoTrackerRecordingCleanupRecord,
): Promise<void> {
  // This read is independent of the page, which portable finalization closes.
  // Retain the cleanup record and job lineage if recovery itself is unavailable.
  recoverRecordingAnnotationIds(
    { ...record.annotations, baselineAnnotationIds: record.baselineAnnotationIds },
    await readRecordingAnnotations(record.taskId, record.accessToken),
  );
  try {
    if (record.videoTrackerJobIds.length) cleanupVideoTrackBatchPropagate(record);
  } finally {
    if (record.annotations.annotationIds.length) cleanupVideoFrameInference(record.annotations);
  }
}

async function recordVideoTrackerStory(
  page: Page,
  assetId: string,
  run: (
    onJobCreated: (jobId: string) => void,
    onAnnotationsCreated: (ids: string[]) => void,
  ) => Promise<{ drawStartMs: number; drawEndMs: number; evidence: object }>,
  docsTarget?: string,
): Promise<void> {
  if (!cached) throw new Error("Screenshot catalog is not ready");
  const t0 = Date.now();
  const annotations = registerVideoAnnotationCleanup(cached);
  const accessToken = await page.evaluate(() => localStorage.getItem("token"));
  if (!accessToken) throw new Error("Recording cleanup requires the isolated user's token");
  const baselineAnnotationIds: string[] = [];
  recoverRecordingAnnotationIds(
    { taskId: annotations.taskId, baselineAnnotationIds: [], annotationIds: baselineAnnotationIds },
    await readRecordingAnnotations(annotations.taskId, accessToken),
  );
  const jobs: VideoTrackerRecordingCleanupRecord = {
    projectId: annotations.projectId,
    taskId: annotations.taskId,
    sourceAnnotationIds: [],
    videoTrackerJobIds: [],
    accessToken,
    baselineAnnotationIds,
    annotations,
  };
  videoTrackerRecordingCleanupRecords.push(jobs);
  const errors: string[] = [];
  const onPageError = (error: Error) => errors.push(error.message);
  const onConsole = (message: import("@playwright/test").ConsoleMessage) => {
    if (message.type() === "error") errors.push(message.text());
  };
  const onResponse = (response: import("@playwright/test").Response) => {
    const pathname = new URL(response.url()).pathname;
    if (pathname.startsWith("/api/") && response.status() >= 400)
      errors.push(`${response.status()} ${pathname}`);
  };
  page.on("pageerror", onPageError);
  page.on("console", onConsole);
  page.on("response", onResponse);
  try {
    const win = await run(
      (id) => {
        if (!jobs.videoTrackerJobIds.includes(id)) jobs.videoTrackerJobIds.push(id);
      },
      (ids) => {
        for (const id of ids)
          if (!annotations.annotationIds.includes(id)) annotations.annotationIds.push(id);
      },
    );
    expect(jobs.videoTrackerJobIds).toHaveLength(1);
    expect(annotations.annotationIds.length).toBeGreaterThan(0);
    expect(errors, `${assetId} browser and API errors`).toEqual([]);
    flowInferenceEvidence[assetId] = { ...win.evidence, browserErrors: errors };
    await finalize(page, assetId, docsTarget, {
      fps: 6,
      maxWidth: 680,
      maxColors: 128,
      ...drawTrim(win, t0),
    });
  } finally {
    page.off("pageerror", onPageError);
    page.off("console", onConsole);
    page.off("response", onResponse);
    // Accepted outputs are not propagation sources. Recover missed responses,
    // then delete the owned job and new annotations through independent guards.
    await cleanupVideoTrackerRecording(jobs);
  }
}

function cleanupSecondaryInference(record: SecondaryInferenceCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...record.annotationIds.flatMap((annotationId) => ["--annotation-id", annotationId]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function cleanupCandidateReview(record: CandidateReviewCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-key",
      "image_demo",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...(record.celeryTaskId ? ["--celery-task-id", record.celeryTaskId] : []),
      ...record.predictionIds.flatMap((predictionId) => ["--prediction-id", predictionId]),
      ...record.annotationIds.flatMap((annotationId) => ["--annotation-id", annotationId]),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function cleanupPipelineApply(record: PipelineApplyCleanupRecord): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/cleanup_screenshot_ocr_flow.py",
      "--project-key",
      "image_demo",
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...(record.celeryTaskId ? ["--celery-task-id", record.celeryTaskId] : []),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function manageJobsRetryFixture(
  action: "seed" | "cleanup",
  record: { projectId: string; taskId: string },
  userEmail?: string,
): { backend_id?: string } {
  const output = execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/screenshot_job_recovery_fixture.py",
      action,
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...(userEmail ? ["--user-email", userEmail] : []),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "inherit"],
    },
  );
  const lastLine = output.trim().split("\n").at(-1);
  if (!lastLine) throw new Error(`[jobs-retry-recovery] ${action} 未返回夹具结果`);
  return JSON.parse(lastLine) as { backend_id?: string };
}

function manageReviewRejectFixture(
  action: "prepare" | "cleanup",
  record: { projectId: string; taskId: string },
  reviewerEmail: string,
): void {
  execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/screenshot_review_reject_fixture.py",
      action,
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      "--reviewer-email",
      reviewerEmail,
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      stdio: "inherit",
    },
  );
}

function manageBackgroundExportFixture(
  action: "run" | "cleanup",
  record: { projectId: string; taskId: string; jobId?: string },
): Record<string, unknown> {
  const output = execFileSync(
    path.join(REPO_ROOT, "apps/api/.venv/bin/python"),
    [
      "scripts/screenshot_background_export_fixture.py",
      action,
      "--project-id",
      record.projectId,
      "--task-id",
      record.taskId,
      ...(record.jobId ? ["--job-id", record.jobId] : []),
    ],
    {
      cwd: path.join(REPO_ROOT, "apps/api"),
      env: screenshotDatabaseEnv(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "inherit"],
    },
  );
  const lastLine = output.trim().split("\n").at(-1);
  if (!lastLine) throw new Error(`[background-export-download] ${action} 未返回夹具结果`);
  return JSON.parse(lastLine) as Record<string, unknown>;
}

type GifOptions = {
  fps?: number;
  maxWidth?: number;
  maxColors?: number;
  startSec?: number;
  durationSec?: number;
  captureWindow?: { startEpochMs: number; endEpochMs: number };
};

type MarketingClip =
  | { startSeconds: number; durationSeconds: number }
  | { tailSeconds: number }
  | { startEpochMs: number; endEpochMs: number };

function marketingClipFromVariants(
  assetId: string,
  variants: Array<{ options?: GifOptions }>,
): MarketingClip | undefined {
  marketingAssetSpec(assetId);
  const captureWindows = variants.flatMap(({ options }) =>
    options?.captureWindow ? [options.captureWindow] : [],
  );
  if (captureWindows.length > 0) {
    return {
      startEpochMs: Math.min(...captureWindows.map((window) => window.startEpochMs)),
      endEpochMs: Math.max(...captureWindows.map((window) => window.endEpochMs)),
    };
  }
  const windows = variants.flatMap(({ options }) =>
    options?.startSec !== undefined && options.durationSec !== undefined
      ? [{ start: options.startSec, end: options.startSec + options.durationSec }]
      : [],
  );
  if (windows.length === 0) return undefined;

  const contentStart = Math.min(...windows.map((window) => window.start));
  const contentEnd = Math.max(...windows.map((window) => window.end));
  return {
    startSeconds: contentStart,
    durationSeconds: contentEnd - contentStart,
  };
}

async function archiveMarketingRecording(
  page: Page,
  assetId: string,
  universalClip?: MarketingClip,
  variants: Array<{ target: string; options?: GifOptions }> = [],
): Promise<boolean> {
  const info = test.info();
  if (info.project.name !== MARKETING_PROJECT_NAME) return false;
  const capture = await stopExternalMarketingRecording(page);
  const resolvedClip =
    universalClip && "startEpochMs" in universalClip
      ? clipFromEpochWindow(capture.startedAtEpochMs, universalClip)
      : universalClip;
  const browser = {
    name: page.context().browser()?.browserType().name() ?? "chromium",
    version: page.context().browser()?.version() ?? "unknown",
  };
  await page.close();
  try {
    const archived = await archiveMarketingMaster({
      archiveRoot: MARKETING_ARCHIVE_ROOT,
      run: getMarketingRunContext(REPO_ROOT),
      video: capture,
      captureExtension: capture.extension,
      captureDriver: capture.driver,
      deviceScaleFactor: capture.deviceScaleFactor,
      sourcePhysicalSize: capture.sourcePhysicalSize,
      captureCadence: capture.cadence,
      assetId,
      assetSpec: marketingAssetSpec(assetId),
      source: path.relative(REPO_ROOT, info.file),
      watchPaths: flowWatchPaths(assetId),
      testTitle: info.titlePath.join(" › "),
      projectName: info.project.name,
      seedRevision: cached?.seed_revision ?? null,
      viewport: capture.logicalViewport,
      browser,
      universalClip: resolvedClip,
      gifVariants: gifRecipes(variants, capture.startedAtEpochMs),
      evidence: {
        clock: SELECTED_CAPTURE ? "live" : "unverified",
        backend_requirements: SELECTED_CAPTURE
          ? recordingPlan([info.title.split(" —")[0]]).backendRequirements
          : null,
        inference: SELECTED_CAPTURE ? recordingInference(info.title.split(" —")[0]) : "unverified",
        inference_evidence: flowInferenceEvidence[assetId],
        behavior_evidence: flowBehaviorEvidence[assetId],
      },
    });
    console.log(`[marketing] ✓ 4K60 采集源：${archived.capturePath}`);
    console.log(`[marketing] ✓ 4K60 MP4 母版：${archived.masterPath}`);
    console.log(`[marketing] ✓ manifest：${archived.manifestPath}`);
    return true;
  } finally {
    capture.cleanup();
  }
}

function gifRecipes(
  variants: Array<{ target: string; options?: GifOptions }>,
  recordingStartEpochMs?: number,
): SourceGifVariant[] {
  return variants.map(({ target, options = {} }) => {
    const epochClip =
      options.captureWindow && recordingStartEpochMs !== undefined
        ? clipFromEpochWindow(recordingStartEpochMs, options.captureWindow)
        : undefined;
    return {
      target: path.relative(REPO_ROOT, target).replaceAll("\\", "/"),
      fps: options.fps ?? 10,
      maxWidth: options.maxWidth ?? 1280,
      maxColors: options.maxColors,
      startSec: epochClip?.startSeconds ?? options.startSec,
      durationSec: epochClip?.durationSeconds ?? options.durationSec,
    };
  });
}

async function finalizeVariants(
  page: Page,
  assetId: string,
  variants: Array<{ target: string; options?: GifOptions }>,
  marketingClip?: MarketingClip,
) {
  if (VALIDATE_ONLY) {
    await page.close();
    return;
  }
  if (test.info().project.name === MARKETING_PROJECT_NAME) {
    await archiveMarketingRecording(
      page,
      assetId,
      marketingClip ?? marketingClipFromVariants(assetId, variants),
      variants,
    );
  } else {
    throw new Error(
      "Production media requires a qualified marketing master; rehearse with SCREENSHOT_VALIDATE_ONLY=1.",
    );
  }
}

async function finalize(
  page: Page,
  assetId: string,
  docsTarget?: string,
  gifOpts?: GifOptions,
  marketingClip?: MarketingClip,
) {
  await finalizeVariants(
    page,
    assetId,
    docsTarget ? [{ target: docsTarget, options: gifOpts }] : [],
    marketingClip ??
      (test.info().project.name === MARKETING_PROJECT_NAME && gifOpts
        ? marketingClipFromVariants(assetId, [{ options: gifOpts }])
        : undefined),
  );
}

async function finalizeMarketingBackedHomepageAsset(
  page: Page,
  assetId: string,
  trim: { startSec?: number; durationSec?: number },
  docsGifTarget?: string,
  docsGifOptions?: { fps?: number; maxWidth?: number },
) {
  await finalize(page, assetId, docsGifTarget, {
    ...trim,
    fps: docsGifOptions?.fps ?? 8,
    maxWidth: docsGifOptions?.maxWidth ?? 860,
  });
}

function hasLiveSam3(catalog: ScreenshotSeedCatalog): boolean {
  const backend = catalog.projects.image_demo.ml_backend;
  return Boolean(
    backend?.name.toLowerCase().includes("sam3") ||
    (backend?.capabilities.models ?? []).some((model) =>
      [model.id, model.model_family].some(
        (value) => typeof value === "string" && value.toLowerCase().includes("sam3"),
      ),
    ),
  );
}

test.describe("flow recordings", () => {
  test.beforeEach(async ({ page, seed }, testInfo) => {
    if (SELECTED_CAPTURE) testInfo.setTimeout(recordingTimeout(420_000, testInfo.timeout, true));
    if (SELECTED_CAPTURE && VIDEO_ML_CAPTURE_FLOWS.has(testInfo.title.split(" —")[0]))
      testInfo.setTimeout(1_200_000);
    if (SELECTED_CAPTURE) {
      if (!SELECTED_CAPTURE.flows.includes(testInfo.title.split(" —")[0])) {
        throw new Error("Flow is outside the preflighted recording selection");
      }
      if (SELECTED_CAPTURE.backendRequirements !== process.env.SCREENSHOT_BACKEND_REQUIREMENTS) {
        throw new Error("Recording selection and backend scope differ");
      }
      if (
        SELECTED_CAPTURE.backendRequirements.includes("image_interactive") &&
        (!cached || !hasLiveSam3(cached))
      ) {
        throw new Error(
          "Selected SAM recording requires a live SAM3 backend; no stub or skipped success",
        );
      }
    }
    if (
      (testInfo.project.name !== MARKETING_PROJECT_NAME && !SELECTED_CAPTURE) ||
      testInfo.title.includes("e2e-quickstart")
    ) {
      return;
    }
    if (!cached) throw new Error("screenshot seed catalog 未完成");

    // 每条营销母版拥有独立的固定数据状态。绘图、审核和视频轨迹流程都会写库；
    // 若沿用同一任务，前一条素材会改变后一条素材的画布、状态与命中目标。
    await cleanupRecordingRecords();
    if (!SKIP_CAPTURE_SEED_REPAIR) repairScreenshotProfile(screenshotBackendMode(cached), true);
    cached = await seed.screenshotCatalog();

    // image_demo 主后端保留 SAM3 供交互工具使用；批量预标单项另启用 batchable YOLO。
    if (
      testInfo.title.startsWith("ai-preannotate —") ||
      testInfo.title.startsWith("ai-pre-variant-selector —")
    ) {
      await seed.enableMLBackendByName(
        cached.projects.image_demo.id,
        cached.users.admin.email,
        "yolo-backend",
      );
    }

    const selectedFlow = testInfo.title.split(" —")[0];
    if (selectedFlow === "pipeline-template-create") {
      // Template design needs the actual model catalog, but submits no inference.
      for (const name of ["yolo-backend", "onnxtools-backend"]) {
        await seed.enableMLBackendByName(
          cached.projects.image_demo.id,
          cached.users.admin.email,
          name,
        );
      }
    }
    if (selectedFlow.startsWith("model-market-")) {
      // These management demonstrations deliberately mask private runtime topology.
      // They exercise real UI behavior against deterministic observations, not inference.
      flowBehaviorEvidence[selectedFlow] = {
        kind: "management-observation-snapshot",
        deterministic_runtime_fixture: true,
        real_inference: false,
        fixture_source: "apps/web/e2e/screenshots/flows/model-market-runtime-pool.ts",
      };
    }

    if (!VALIDATE_ONLY && testInfo.project.name === MARKETING_PROJECT_NAME) {
      await startExternalMarketingRecording(page);
    }
  });

  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.project.name === MARKETING_PROJECT_NAME) {
      await discardExternalMarketingRecording(page);
    }
    if (SELECTED_CAPTURE && testInfo.status === "skipped") {
      throw new Error(
        "Explicitly selected recording was skipped; no successful capture was produced",
      );
    }
  });

  test("detection-online-training — 检测在线训练完整闭环", async ({ page, seed }) => {
    test.skip(!SELECTED_CAPTURE, "Private training demo requires an explicit recording selection");
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(1_200_000);
    await installScreenshotEnvironment(page, { clock: "live" });
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both");
    const win = await runDetectionOnlineTraining(page, {
      marketing: test.info().project.name === MARKETING_PROJECT_NAME,
    });
    flowInferenceEvidence["detection-online-training"] = win.evidence;
    if (!VALIDATE_ONLY) {
      await archiveMarketingRecording(page, "detection-online-training", {
        startEpochMs: win.drawStartMs,
        endEpochMs: win.drawEndMs,
      });
    }
  });

  test("e2e-quickstart — 登录→标注→提交", async ({ page, seed }) => {
    test.skip(
      test.info().project.name === MARKETING_PROJECT_NAME,
      "组合教程包含多个目标，不归档为单项营销母版",
    );
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    await installScreenshotEnvironment(page);
    let recordingPage = page;
    if (test.info().project.name === MARKETING_PROJECT_NAME) {
      const context = page.context();
      const project = cached.projects.image_demo;
      await seed.injectToken(page, cached.users.admin.email);
      await installRecordingWorkbenchLayout(page, "none");
      await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.clean.id}`);
      await expect(page.getByTestId("workbench-stage")).toHaveAttribute(
        "data-image-ready",
        "true",
        { timeout: 10_000 },
      );
      await page.evaluate(() => {
        localStorage.clear();
        sessionStorage.clear();
      });
      await page.close();
      recordingPage = await context.newPage();
      await installScreenshotEnvironment(recordingPage);
    }
    await runE2eQuickstart(recordingPage, cached, {
      marketing: test.info().project.name === MARKETING_PROJECT_NAME,
    });
    await finalize(
      recordingPage,
      "e2e-quickstart",
      path.join(DOCS_IMAGES, "getting-started/e2e.gif"),
      {
        fps: 4,
        maxWidth: 640,
        maxColors: 128,
      },
    );
  });

  test("ai-preannotate — AI 预标注发起流程", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runAiPreannotate(page, cached);
    flowInferenceEvidence["ai-preannotate"] = win.evidence;
    await finalize(page, "ai-preannotate", undefined, drawTrim(win, t0));
  });

  test("ai-prediction-import — 导入预标注结果", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both");
    const win = await runAiPredictionImport(page, cached, {
      marketing: test.info().project.name === MARKETING_PROJECT_NAME,
    });
    await finalize(page, "ai-prediction-import", undefined, drawTrim(win, t0));
  });

  const samToolDemos: Array<{
    tool: SamRecordingTool;
    label: string;
    target: string;
    fps?: number;
    maxWidth?: number;
  }> = [
    { tool: "smart-point", label: "智能点", target: "smart-point-interaction.gif" },
    { tool: "smart-box", label: "智能框", target: "smart-box-interaction.gif" },
    {
      tool: "exemplar",
      label: "Exemplar 示例",
      target: "exemplar-interaction.gif",
      fps: 6,
      maxWidth: 760,
    },
  ];

  for (const demo of samToolDemos) {
    test(`sam-tool-${demo.tool} — ${demo.label}真实推理`, async ({ page, seed }) => {
      if (!cached) throw new Error("screenshot seed catalog 未完成");
      test.skip(!hasLiveSam3(cached), "真实 SAM 工具视频需要 live SAM3");
      setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 150_000);
      const t0 = Date.now();
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.admin.email);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "annotate:image", preset: "ai-review" },
      });
      const cleanup: CandidateReviewCleanupRecord = {
        projectId: cached.projects.image_demo.id,
        taskId: cached.projects.image_demo.tasks.annotating.id,
        predictionIds: [],
        annotationIds: [],
      };
      candidateReviewCleanupRecords.push(cleanup);
      try {
        const win = await runSamToolRecording(page, cached, demo.tool, {
          accept: true,
          onCreated: (id, annotation) => {
            cleanup.annotationIds.push(id);
            if (typeof annotation.parent_prediction_id === "string")
              cleanup.predictionIds.push(annotation.parent_prediction_id);
          },
        });
        flowInferenceEvidence[`sam-tools/${demo.tool}`] = win.evidence;
        await finalizeMarketingBackedHomepageAsset(
          page,
          `sam-tools/${demo.tool}`,
          drawTrim(win, t0),
          path.join(DOCS_IMAGES, "sam", demo.target),
          { fps: demo.fps ?? 8, maxWidth: demo.maxWidth ?? 860 },
        );
      } finally {
        cleanupCandidateReview(cleanup);
      }
    });
  }

  test("smart-scribble — 已存 Mask 正负笔迹精修", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(300_000);
    const project = cached.projects.image_demo;
    const task = project.tasks.annotating;
    const cleanup: CandidateReviewCleanupRecord = {
      projectId: project.id,
      taskId: task.id,
      predictionIds: [],
      annotationIds: [],
    };
    candidateReviewCleanupRecords.push(cleanup);
    try {
      await seed.configureRasterMask(project.id, true);
      const source = await seed.injectRasterMask({
        taskId: task.id,
        userEmail: cached.users.admin.email,
        variant: "smart_scribble_source",
        label: "car",
        canvas: "media",
      });
      cleanup.annotationIds.push(source.annotation_id);
      const t0 = Date.now();
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.admin.email);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "annotate:image", preset: "ai-review" },
      });
      const win = await runSmartScribble(page, cached, source, cleanup, (evidence) => {
        flowInferenceEvidence["smart-scribble"] = evidence;
      });
      await finalize(
        page,
        "smart-scribble",
        path.join(DOCS_IMAGES, "sam/smart-scribble-interaction.gif"),
        { fps: 4, maxWidth: 860, maxColors: 96, ...drawTrim(win, t0) },
      );
    } finally {
      cleanupCandidateReview(cleanup);
    }
  });

  test("sam-interactive — Magic Box 候选→人工确认", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    test.skip(!hasLiveSam3(cached), "首页 AI 视频需要 live SAM3");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 150_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "ai-review" },
    });
    const cleanup: CandidateReviewCleanupRecord = {
      projectId: cached.projects.image_demo.id,
      taskId: cached.projects.image_demo.tasks.annotating.id,
      predictionIds: [],
      annotationIds: [],
    };
    candidateReviewCleanupRecords.push(cleanup);
    try {
      const win = await runSamInteractive(page, cached, (id, annotation) => {
        cleanup.annotationIds.push(id);
        if (typeof annotation.parent_prediction_id === "string")
          cleanup.predictionIds.push(annotation.parent_prediction_id);
      });
      flowInferenceEvidence["ai-assisted-annotation"] = win.evidence;
      await finalizeMarketingBackedHomepageAsset(page, "ai-assisted-annotation", drawTrim(win, t0));
    } finally {
      cleanupCandidateReview(cleanup);
    }
  });

  test("review-reject — 审核拒回流程", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 150_000);
    const project = cached.projects.image_demo;
    const task = project.tasks.review;
    const reviewerEmail = cached.users.reviewer.email;
    const t0 = Date.now();
    const cleanupRecord = { projectId: project.id, taskId: task.id };
    manageReviewRejectFixture("prepare", cleanupRecord, reviewerEmail);
    try {
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, reviewerEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        common: { petEnabled: false },
        layout: {
          floatingSelection: {
            collapsed: true,
            x: 1050,
            y: 110,
            w: 300,
            h: 260,
          },
        },
      });
      const win = await runReviewReject(page, cached);
      await finalize(page, "review-reject", path.join(DOCS_IMAGES, "review/reject-flow.gif"), {
        fps: 6,
        maxWidth: 860,
        ...drawTrim(win, t0),
      });
    } finally {
      manageReviewRejectFixture("cleanup", cleanupRecord, reviewerEmail);
    }
  });

  test("batch-bulk-actions — 批次多选批量操作", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    const win = await runBatchBulkActions(page, cached);
    await finalize(
      page,
      "batch-bulk-actions",
      path.join(DOCS_IMAGES, "projects/batch-bulk-actions.gif"),
      { ...drawTrim(win, t0) },
    );
  });

  test("ai-pre-variant-selector — 变体两轴联动", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    const win = await runAiPreVariantSelector(page, cached);
    await finalize(
      page,
      "ai-pre-variant-selector",
      path.join(DOCS_IMAGES, "projects/ai-pre-variant-selector.gif"),
      { ...drawTrim(win, t0) },
    );
  });

  test("ocr-inference — 真实 RapidOCR 当前题推理", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 180_000);
    const t0 = Date.now();
    // 保留推理中 badge / loader 的自然动效，不安装静态 PNG 专用的禁动环境。
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both");
    let cleanupRecord: OcrCleanupRecord | null = null;
    const win = await runOcrInference(page, cached, (record) => {
      cleanupRecord = record;
      ocrCleanupRecords.push(record);
    });
    if (!cleanupRecord) throw new Error("[ocr-inference] 未记录无痕清理标识");
    cleanupOcrRecording(cleanupRecord);
    await finalizeMarketingBackedHomepageAsset(page, "ocr-real-scene", drawTrim(win, t0));
  });

  test("current-task-image-inference — 项目编排推理到人工采纳", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 180_000);
    const t0 = Date.now();
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "ai-review" },
    });
    let cleanupRecord: OcrCleanupRecord | null = null;
    const win = await runCurrentTaskImageInference(
      page,
      cached,
      (record) => {
        cleanupRecord = record;
        ocrCleanupRecords.push(record);
      },
      (evidence) => {
        flowInferenceEvidence["current-task-image-inference"] = evidence;
      },
    );
    if (!cleanupRecord) {
      throw new Error("[current-task-image-inference] 未记录无痕清理标识");
    }
    cleanupOcrRecording(cleanupRecord);
    await finalize(page, "current-task-image-inference", undefined, drawTrim(win, t0));
  });

  test("current-frame-video-inference — 当前帧车辆推理与作用域核对", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 180_000);
    const t0 = Date.now();
    await seed.enableMLBackendByName(
      cached.projects.video_demo.id,
      cached.users.project_admin.email,
      "yolo-backend",
    );
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both");
    let cleanupRecord: VideoFrameInferenceCleanupRecord | null = null;
    const win = await runCurrentFrameVideoInference(page, cached, (record) => {
      cleanupRecord = record;
      videoFrameInferenceCleanupRecords.push(record);
    });
    if (!cleanupRecord) {
      throw new Error("[current-frame-video-inference] 未记录无痕清理标识");
    }
    cleanupVideoFrameInference(cleanupRecord);
    await finalize(page, "current-frame-video-inference", undefined, drawTrim(win, t0));
  });

  test("secondary-inference-attribute — 裁剪 OCR 属性写回与人工校正", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 180_000);
    const t0 = Date.now();
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      common: { petEnabled: false },
      layout: {
        attrPanelCollapsed: false,
        aiSectionCollapsed: true,
        manualSectionCollapsed: false,
        discussionCollapsed: true,
        floatingSelection: { collapsed: true, x: 310, y: 510, w: 320, h: 300 },
      },
      ui: { secondary_bar_hidden: false },
    });
    let cleanupRecord: SecondaryInferenceCleanupRecord | null = null;
    const win = await runSecondaryInferenceAttribute(page, cached, (record) => {
      cleanupRecord = record;
      secondaryInferenceCleanupRecords.push(record);
    });
    if (!cleanupRecord) {
      throw new Error("[secondary-inference-attribute] 未记录无痕清理标识");
    }
    cleanupSecondaryInference(cleanupRecord);
    await finalize(page, "secondary-inference-attribute", undefined, drawTrim(win, t0));
  });

  test("workspace-layout-basics — 布局调整与编辑状态保留", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    const userEmail = cached.users.admin.email;
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, userEmail);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", workspaceLayoutBasicsLayout);
    let created: { taskId: string; annotationId: string } | undefined;
    try {
      const win = await runWorkspaceLayoutBasics(page, cached, (annotation) => {
        created = annotation;
      });
      await finalize(page, "workspace-layout-basics", undefined, drawTrim(win, t0));
    } finally {
      if (created) await seed.deleteTaskAnnotation(created.taskId, created.annotationId, userEmail);
    }
  });

  test("workspace-layout-persistence — 布局偏好跨任务、刷新与紧凑视口持久化", async ({
    page,
    seed,
  }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    // The screenshot seed/repair fixture may take several minutes on a live
    // media worker; keep the per-test budget separate from the 20s UI waits.
    setFlowTimeout(420_000);
    const t0 = Date.now();
    await seed.injectToken(page, cached.users.project_admin.email);
    const result = await runWorkspaceLayoutPersistence(page, cached);
    flowBehaviorEvidence["workspace-layout-persistence"] = result.evidence;
    await finalize(page, "workspace-layout-persistence", undefined, drawTrim(result, t0));
  });

  test("rotated-bbox — 旋转框绘制", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now(); // 录屏起点参照（page 在测试体前创建，t0≈video t=0）
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let createdId: string | undefined;
    try {
      const win = await runRotatedBbox(page, cached, {
        onCreated: (id) => {
          createdId = id;
        },
      });
      await finalize(page, "rotated-bbox", path.join(DOCS_IMAGES, "workbench/rotated-bbox.gif"), {
        fps: 4,
        maxWidth: 640,
        maxColors: 96,
        ...drawTrim(win, t0),
      });
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          createdId,
          cached.users.annotator.email,
        );
    }
  });

  test("bbox-draw — 矩形绘制", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
      ui: { secondary_bar_hidden: true },
    });
    let createdId: string | undefined;
    try {
      const win = await runBboxDraw(page, cached, {
        onCreated: (id) => {
          createdId = id;
        },
        marketing: test.info().project.name === MARKETING_PROJECT_NAME,
      });
      await finalize(page, "bbox-draw", path.join(DOCS_IMAGES, "bbox/draw-in-progress.gif"), {
        fps: 4,
        maxWidth: 640,
        maxColors: 96,
        ...drawTrim(win, t0),
      });
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          createdId,
          cached.users.annotator.email,
        );
    }
  });

  test("polyline-draw — 折线逐点绘制", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let createdId: string | undefined;
    try {
      const win = await runPolylineDraw(page, cached, {
        onCreated: (id) => {
          createdId = id;
        },
      });
      await finalize(
        page,
        "polyline-draw",
        path.join(DOCS_IMAGES, "polyline/draw-in-progress.gif"),
        {
          fps: 4,
          maxWidth: 640,
          maxColors: 96,
          ...drawTrim(win, t0),
        },
      );
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          createdId,
          cached.users.annotator.email,
        );
    }
  });

  test("polygon-draw — 多边形逐点绘制", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let createdId: string | undefined;
    try {
      const win = await runPolygonDraw(page, cached, {
        onCreated: (id) => {
          createdId = id;
        },
      });
      await finalize(page, "polygon-draw", path.join(DOCS_IMAGES, "polygon/draw-in-progress.gif"), {
        fps: 4,
        maxWidth: 640,
        maxColors: 96,
        ...drawTrim(win, t0),
      });
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          createdId,
          cached.users.annotator.email,
        );
    }
  });

  test("mask-draw — Mask 笔刷涂抹", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let createdId: string | undefined;
    try {
      const win = await runMaskDraw(page, cached, {
        onCreated: (id) => {
          createdId = id;
        },
      });
      await finalize(page, "mask-draw", path.join(DOCS_IMAGES, "mask-brush/draw-in-progress.gif"), {
        fps: 4,
        maxWidth: 640,
        maxColors: 96,
        ...drawTrim(win, t0),
      });
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          createdId,
          cached.users.annotator.email,
        );
    }
  });

  test("candidate-keyboard-review — 候选键盘审阅与自动前进", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const project = cached.projects.image_demo;
    const task = project.tasks.annotating;
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 300_000);
    const cleanup: CandidateReviewCleanupRecord = {
      projectId: project.id,
      taskId: task.id,
      predictionIds: [],
      annotationIds: [],
    };
    candidateReviewCleanupRecords.push(cleanup);
    const live = await prepareLiveCandidateReview(
      page,
      cached,
      await seed.accessToken(cached.users.admin.email),
      cleanup,
    );
    flowInferenceEvidence["candidate-keyboard-review"] = live.evidence;
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both");
    const win = await runCandidateKeyboardReview(page, cached, live.candidateIds, cleanup);
    await finalizeVariants(
      page,
      "candidate-keyboard-review",
      [
        {
          target: path.join(DOCS_IMAGES, "workbench/review-auto-advance.gif"),
          options: {
            fps: 8,
            maxWidth: 860,
            ...drawTrim({ drawStartMs: win.autoAdvanceStartMs, drawEndMs: win.drawEndMs }, t0),
          },
        },
      ],
      { startEpochMs: win.drawStartMs, endEpochMs: win.drawEndMs },
    );
  });

  test("candidate-review-lifecycle — 跳过、采纳、驳回与最终计数", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 300_000);
    const project = cached.projects.image_demo;
    const task = project.tasks.annotating;
    const cleanupRecord: CandidateReviewCleanupRecord = {
      projectId: project.id,
      taskId: task.id,
      predictionIds: [],
      annotationIds: [],
    };
    candidateReviewCleanupRecords.push(cleanupRecord);
    try {
      const live = await prepareLiveCandidateReview(
        page,
        cached,
        await seed.accessToken(cached.users.admin.email),
        cleanupRecord,
      );
      flowInferenceEvidence["candidate-review-lifecycle"] = live.evidence;
      const t0 = Date.now();
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.annotator.email);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "annotate:image", preset: "ai-review" },
        common: { petEnabled: false, autoAdvanceOnDecide: true },
        layout: { aiSectionCollapsed: false, manualSectionCollapsed: false },
      });
      const win = await runCandidateReviewLifecycle(page, cached, live.candidateIds, cleanupRecord);
      flowInferenceEvidence["candidate-review-lifecycle"] = {
        ...live.evidence,
        review: win.reviewEvidence,
      };
      await finalize(page, "candidate-review-lifecycle", undefined, drawTrim(win, t0));
    } finally {
      cleanupCandidateReview(cleanupRecord);
    }
  });

  test("video-track — 视频时序工作台", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    let createdId: string | null = null;
    try {
      const win = await runVideoTrack(page, cached, (id) => {
        createdId = id;
      });
      await finalize(page, "video-track", undefined, drawTrim(win, t0));
    } finally {
      if (createdId)
        await seed.deleteTaskAnnotation(
          cached.projects.video_demo.tasks.tracking.id,
          createdId,
          cached.users.admin.email,
        );
    }
  });

  test("video-timeline-zoom — 时间轴锚点缩放与复位", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    const cleanup = registerVideoAnnotationCleanup(cached);
    try {
      const win = await runVideoTimelineZoom(page, cached, (id) => cleanup.annotationIds.push(id));
      flowBehaviorEvidence["video-timeline-zoom"] = win.evidence;
      await finalize(
        page,
        "video-timeline-zoom",
        path.join(DOCS_IMAGES, "video-timeline/horizontal-zoom.gif"),
        { fps: 6, maxWidth: 640, maxColors: 128, ...drawTrim(win, t0) },
      );
    } finally {
      cleanupVideoFrameInference(cleanup);
    }
  });

  test("video-timeline-prediction-navigation — AI 预测密度与帧导航", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 180_000); // 真实双目标视频推理 + 4K H.264 归档转码
    const t0 = Date.now();
    await seed.enableMLBackendByName(
      cached.projects.video_demo.id,
      cached.users.project_admin.email,
      "yolo-backend",
    );
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "none");
    let cleanupRecord: VideoFrameInferenceCleanupRecord | null = null;
    const win = await runVideoTimelinePredictionNavigation(page, cached, (record) => {
      cleanupRecord = record;
      videoFrameInferenceCleanupRecords.push(record);
    });
    if (!cleanupRecord) {
      throw new Error("[video-timeline-prediction-navigation] 未记录无痕清理标识");
    }
    cleanupVideoFrameInference(cleanupRecord);
    await finalize(page, "video-timeline-prediction-navigation", undefined, drawTrim(win, t0));
  });

  test("video-chapter — 时间轴圈选与拖柄调整章节", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    const accessToken = await seed.accessToken(cached.users.admin.email);
    const createdChapters: Array<VideoChapterCleanupRecord & { accessToken: string }> = [];
    try {
      const windows = await runVideoChapter(page, cached, (record) => {
        const cleanup = { ...record, accessToken };
        createdChapters.push(cleanup);
        videoChapterCleanupRecords.push(cleanup);
      });
      flowBehaviorEvidence["video-chapter"] = windows.evidence;
      await finalizeVariants(page, "video-chapter", [
        {
          target: path.join(DOCS_IMAGES, "video-timeline/brush-create-chapter.gif"),
          options: { fps: 4, maxWidth: 600, maxColors: 96, ...drawTrim(windows.create, t0) },
        },
        {
          target: path.join(DOCS_IMAGES, "video-timeline/chapter-resize-hover.gif"),
          options: { fps: 4, maxWidth: 600, maxColors: 96, ...drawTrim(windows.resize, t0) },
        },
      ]);
    } finally {
      for (const record of createdChapters) await cleanupVideoChapter(record);
    }
  });

  test("video-tracker-range — 时间轴刷选追踪范围", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000);
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    await recordVideoTrackerStory(
      page,
      "video-tracker-range",
      (onJobCreated, onAnnotationsCreated) =>
        runVideoTrackerRange(page, cached!, onJobCreated, onAnnotationsCreated),
      path.join(DOCS_IMAGES, "video-propagate/shift-brush-range.gif"),
    );
  });

  test("video-track-batch-propagate — 双轨迹批量延展并复核", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 180_000); // 双轨迹真实视频推理 + 候选审阅 + 4K H.264 归档
    const project = cached.projects.video_demo;
    const task = project.tasks.tracking;
    const userEmail = cached.users.project_admin.email;
    const anchors = [
      recordingAnchor(cached, "video_demo", "tracking", "left_bus_f0", 0),
      recordingAnchor(cached, "video_demo", "tracking", "right_bus_f0", 0),
    ];
    const cleanupRecord: VideoTrackBatchPropagateCleanupRecord = {
      projectId: project.id,
      taskId: task.id,
      sourceAnnotationIds: [],
      videoTrackerJobIds: [],
    };
    const t0 = Date.now();

    try {
      await seed.enableMLBackendByName(project.id, userEmail, "sam3-backend");
      for (const [index, anchor] of anchors.entries()) {
        const [x1, y1, x2, y2] = anchor.bbox;
        const trackId = `trk_marketing_batch_${index === 0 ? "left" : "right"}`;
        const source = await seed.createTaskAnnotation(task.id, userEmail, {
          annotation_type: "video_track_bbox",
          tool_unit_id: "bbox",
          class_name: anchor.label,
          geometry: {
            type: "video_track_bbox",
            track_id: trackId,
            keyframes: [
              {
                frame_index: 0,
                bbox: { x: x1, y: y1, w: x2 - x1, h: y2 - y1 },
                source: "manual",
                occluded: false,
              },
            ],
            outside: [],
          },
        });
        cleanupRecord.sourceAnnotationIds.push(source.id);
      }

      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        common: { petEnabled: false },
        layout: {
          floatingSelection: {
            collapsed: false,
            x: 600,
            y: 150,
            w: 300,
            h: 300,
          },
        },
      });
      const win = await runVideoTrackBatchPropagate(
        page,
        cached,
        cleanupRecord.sourceAnnotationIds,
        (jobId) => cleanupRecord.videoTrackerJobIds.push(jobId),
      );
      if (cleanupRecord.videoTrackerJobIds.length !== 1) {
        throw new Error("[video-track-batch-propagate] 批量延展必须只产生一个追踪作业");
      }
      await finalize(page, "video-track-batch-propagate", undefined, drawTrim(win, t0));
    } finally {
      if (
        cleanupRecord.sourceAnnotationIds.length > 0 ||
        cleanupRecord.videoTrackerJobIds.length > 0
      ) {
        cleanupVideoTrackBatchPropagate(cleanupRecord);
      }
    }
  });

  test("video-propagate-track-vs-copy — 几何复制与 AI 延展对比", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 180_000); // 真实 30 帧 SAM2 追踪 + 候选审阅 + 4K H.264 归档
    const project = cached.projects.video_demo;
    const task = project.tasks.tracking;
    const userEmail = cached.users.project_admin.email;
    const anchor = recordingAnchor(cached, "video_demo", "tracking", "front_truck_f0", 0);
    const cleanupRecord: VideoTrackBatchPropagateCleanupRecord = {
      projectId: project.id,
      taskId: task.id,
      sourceAnnotationIds: [],
      videoTrackerJobIds: [],
    };
    const t0 = Date.now();

    try {
      await seed.enableMLBackendByName(project.id, userEmail, "grounded-sam2");
      const [x1, y1, x2, y2] = anchor.bbox;
      const source = await seed.createTaskAnnotation(task.id, userEmail, {
        annotation_type: "video_track_bbox",
        tool_unit_id: "bbox",
        class_name: anchor.label,
        geometry: {
          type: "video_track_bbox",
          track_id: "trk_marketing_propagate_compare_truck",
          keyframes: [
            {
              frame_index: 0,
              bbox: { x: x1, y: y1, w: x2 - x1, h: y2 - y1 },
              source: "manual",
              occluded: false,
            },
          ],
          outside: [],
        },
      });
      cleanupRecord.sourceAnnotationIds.push(source.id);

      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        common: { petEnabled: false },
        layout: {
          floatingSelection: {
            collapsed: false,
            x: 770,
            y: 115,
            w: 310,
            h: 350,
          },
        },
      });
      const win = await runVideoPropagateTrackVsCopy(page, cached, source.id, (jobId) =>
        cleanupRecord.videoTrackerJobIds.push(jobId),
      );
      if (cleanupRecord.videoTrackerJobIds.length !== 1) {
        throw new Error("[video-propagate-track-vs-copy] AI 对比必须只产生一个追踪作业");
      }
      await finalize(page, "video-propagate-track-vs-copy", undefined, drawTrim(win, t0));
    } finally {
      if (
        cleanupRecord.sourceAnnotationIds.length > 0 ||
        cleanupRecord.videoTrackerJobIds.length > 0
      ) {
        cleanupVideoTrackBatchPropagate(cleanupRecord);
      }
    }
  });

  test("video-tracker-cross-frame-points — 双目标跨帧多正点", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000);
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    await recordVideoTrackerStory(
      page,
      "video-tracker-cross-frame-points",
      (onJobCreated, onAnnotationsCreated) =>
        runVideoMultiSeedTracking(
          page,
          cached!,
          "cross-frame-points",
          onJobCreated,
          onAnnotationsCreated,
        ),
    );
  });

  test("video-tracker-positive-negative — 双目标正负点修正", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000);
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    await recordVideoTrackerStory(
      page,
      "video-tracker-positive-negative",
      (onJobCreated, onAnnotationsCreated) =>
        runVideoMultiSeedTracking(
          page,
          cached!,
          "positive-negative",
          onJobCreated,
          onAnnotationsCreated,
        ),
    );
  });

  test("video-tracker-box-seed — 双目标整车框种子", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000);
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    await recordVideoTrackerStory(
      page,
      "video-tracker-box-seed",
      (onJobCreated, onAnnotationsCreated) =>
        runVideoMultiSeedTracking(page, cached!, "box-seed", onJobCreated, onAnnotationsCreated),
    );
  });

  test("video-tracker-text-discovery — 文本发现双目标并采纳轨迹", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000);
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    await recordVideoTrackerStory(
      page,
      "video-tracker-text-discovery",
      (onJobCreated, onAnnotationsCreated) =>
        runVideoTrackerTextDiscovery(page, cached!, onJobCreated, onAnnotationsCreated),
    );
  });

  test("video-tracker-combo-discovery — 文本发现后逐对象记忆追踪", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000); // 真实两趟视频推理 + 4K H.264 归档转码
    const t0 = Date.now();
    await seed.enableMLBackendByName(
      cached.projects.video_demo.id,
      cached.users.project_admin.email,
      "sam3-backend",
    );
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "none");
    const win = await runVideoTrackerComboDiscovery(page, cached);
    await finalize(page, "video-tracker-combo-discovery", undefined, drawTrim(win, t0));
  });

  test("video-mask-correction-propagate — 错帧加减笔迹后重传播", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 240_000); // 两次 Mask 提交 + 真实视频重传播 + 4K H.264 归档
    const t0 = Date.now();
    await seed.enableMLBackendByName(
      cached.projects.video_demo.id,
      cached.users.project_admin.email,
      "sam3-backend",
    );
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.project_admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      // Keep the selected-object card inside the canvas so its action buttons
      // are physically reachable; the default right-side position is behind
      // the inspector Dockview sash at this recording viewport.
      layout: {
        floatingSelection: { collapsed: false, x: 720, y: 88, w: 340, h: 440 },
      },
    });
    const win = await runVideoMaskCorrectionPropagate(page, cached);
    await finalize(page, "video-mask-correction-propagate", undefined, drawTrim(win, t0));
  });

  test("pipeline-template-create — 创建车辆检测到属性分类公共模板", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(150_000);
    const t0 = Date.now();
    let pipelineId: string | null = null;
    try {
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.admin.email);
      await applyScreenshotTheme(page, "dark");
      const win = await runPipelineTemplateCreate(page, (createdId) => {
        pipelineId = createdId;
      });
      await finalize(page, "pipeline-template-create", undefined, drawTrim(win, t0));
    } finally {
      if (pipelineId) {
        await seed.deleteProjectPipeline(pipelineId, cached.users.admin.email);
      }
    }
  });

  test("pipeline-apply-project — 套用公共模板并运行项目默认编排", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 240_000);
    const project = cached.projects.image_demo;
    const task = project.tasks.clean;
    const userEmail = cached.users.admin.email;
    const t0 = Date.now();
    let sourcePipelineId: string | null = null;
    let appliedPipelineId: string | null = null;
    let cleanupRecord: PipelineApplyCleanupRecord | null = null;

    try {
      cleanupPipelineApply({ projectId: project.id, taskId: task.id });
      const yoloBackendId = await seed.enableMLBackendByName(project.id, userEmail, "yolo-backend");
      const attributeBackendId = await seed.enableMLBackendByName(
        project.id,
        userEmail,
        "onnxtools-backend",
      );
      const sourcePipeline = await seed.createProjectPipeline(userEmail, {
        name: "车辆检测 → 车型与颜色",
        scope: "public",
        project_id: null,
        organization_id: null,
        stages: [
          {
            stage: 0,
            ml_backend_id: yoloBackendId,
            source: { kind: "dataset", data_type: "image" },
            model_id: "detect",
            model_variants: { series: "yolo11", size: "s" },
            class_filter: [2, 5, 7],
          },
          {
            stage: 1,
            ml_backend_id: attributeBackendId,
            model_id: "vehicle-attr-classify",
            task_type: "classification",
            parent_stage: 0,
            parent_class_filter: ["car", "bus", "truck"],
            roi: { mode: "crop", pad: 0.05 },
            input: { mode: "crop" },
            write: { target: "attributes", keys: ["vehicle_type", "color"] },
            on_failure: "keep_parent",
          },
        ],
        is_default: false,
      });
      sourcePipelineId = sourcePipeline.id;

      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both");
      const win = await runPipelineApplyProject(
        page,
        cached,
        sourcePipeline.id,
        (createdId) => {
          appliedPipelineId = createdId;
        },
        (record) => {
          cleanupRecord = record;
          pipelineApplyCleanupRecords.push(record);
        },
      );
      if (!appliedPipelineId) {
        throw new Error("[pipeline-apply-project] 未记录套用后的私有编排 ID");
      }
      if (!cleanupRecord) {
        throw new Error("[pipeline-apply-project] 未记录真实推理清理标识");
      }
      cleanupPipelineApply(cleanupRecord);
      await finalize(page, "pipeline-apply-project", undefined, drawTrim(win, t0));
    } finally {
      if (cleanupRecord) cleanupPipelineApply(cleanupRecord);
      if (appliedPipelineId) await seed.deleteProjectPipeline(appliedPipelineId, userEmail);
      if (sourcePipelineId) await seed.deleteProjectPipeline(sourcePipelineId, userEmail);
    }
  });

  test("jobs-retry-recovery — 失败预测重试后进入结果", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 150_000);
    const project = cached.projects.ocr_demo;
    const task = project.tasks.ocr;
    const userEmail = cached.users.admin.email;
    const record = { projectId: project.id, taskId: task.id };
    const t0 = Date.now();
    jobsRetryCleanupRecords.push(record);

    try {
      manageJobsRetryFixture("cleanup", record);
      const fixture = manageJobsRetryFixture("seed", record, userEmail);
      if (!fixture.backend_id) {
        throw new Error("[jobs-retry-recovery] 夹具未返回 RapidOCR backend id");
      }
      await seed.predictTestMLBackend(project.id, fixture.backend_id, task.id, userEmail);
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both");
      const win = await runJobsRetryRecovery(page, cached);
      await finalize(page, "jobs-retry-recovery", undefined, drawTrim(win, t0));
    } finally {
      manageJobsRetryFixture("cleanup", record);
    }
  });

  test("model-market-runtime-pool — 服务池运行时观测与实例下钻", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runModelMarketRuntimePool(page, cached);
    await finalize(page, "model-market-runtime-pool", undefined, drawTrim(win, t0));
  });

  test("model-market-video-pool — 视频追踪独立显存池与预热入口", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runModelMarketVideoPool(page, cached);
    await finalize(page, "model-market-video-pool", undefined, drawTrim(win, t0));
  });

  test("model-market-runtime-partial-failure — 单数据源失败与可信状态保留", async ({
    page,
    seed,
  }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runModelMarketRuntimePartialFailure(page, cached);
    await finalize(page, "model-market-runtime-partial-failure", undefined, drawTrim(win, t0));
  });

  test("model-market-gpu-resource-overview — GPU 资源就绪性、预算与阻断实例", async ({
    page,
    seed,
  }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runModelMarketGpuResourceOverview(page, cached);
    await finalize(page, "model-market-gpu-resource-overview", undefined, drawTrim(win, t0));
  });

  test("platform-overview — 全局统计、模型成本与近期活动", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runPlatformOverview(page);
    await finalize(page, "platform-overview", undefined, drawTrim(win, t0));
  });

  test("project-actions-menu — 导入、导出与复制入口", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runProjectActionsMenu(page, cached);
    await finalize(page, "project-actions-menu", undefined, drawTrim(win, t0));
  });

  test("jobs-bell-active — 进度、取消与完成产物下载", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runJobsBellActive(page);
    await finalize(page, "jobs-bell-active", undefined, drawTrim(win, t0));
  });

  test("video-tracker-job-states — 四状态、筛选与返回视频工作台", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 1_800_000 : 120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runVideoTrackerJobStates(page, cached);
    await finalize(page, "video-tracker-job-states", undefined, drawTrim(win, t0));
  });

  test("project-ml-routing — 批量主后端与交互能力自动分流", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 900_000 : 120_000);
    const t0 = Date.now();
    try {
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.admin.email);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "none");
      const win = await runProjectMlRouting(page, cached);
      await finalize(page, "project-ml-routing", undefined, drawTrim(win, t0));
    } finally {
      await cleanupRecordingRecords();
      repairScreenshotProfile(screenshotBackendMode(cached), true);
      cached = await seed.screenshotCatalog();
    }
  });

  test("background-export-download — 后台多格式导出与 ZIP 下载", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(150_000);
    const project = cached.projects.image_demo;
    const task = project.tasks.clean;
    const record: { projectId: string; taskId: string; jobId?: string } = {
      projectId: project.id,
      taskId: task.id,
    };
    const t0 = Date.now();
    backgroundExportCleanupRecords.push(record);

    try {
      manageBackgroundExportFixture("cleanup", record);
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, cached.users.admin.email);
      await applyScreenshotTheme(page, "dark");
      const win = await runBackgroundExportDownload(
        page,
        cached,
        (jobId) => manageBackgroundExportFixture("run", { ...record, jobId }),
        (jobId) => {
          record.jobId = jobId;
        },
      );
      await finalize(page, "background-export-download", undefined, drawTrim(win, t0));
    } finally {
      manageBackgroundExportFixture("cleanup", record);
    }
  });

  test("project-onboarding — 登录、创建项目并进入工作台", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 150_000);
    const userEmail = cached.users.admin.email;
    let createdProjectId: string | undefined;
    const t0 = Date.now();
    try {
      await seed.deleteProjectsByExactName(PROJECT_ONBOARDING_RECORDING_NAME, userEmail);
      await installScreenshotEnvironment(page);
      const win = await runProjectOnboarding(page, cached, (projectId) => {
        createdProjectId = projectId;
      });
      await finalize(page, "project-onboarding", undefined, drawTrim(win, t0));
    } finally {
      if (createdProjectId) await seed.deleteProject(createdProjectId, userEmail);
    }
  });

  test("project-create-existing-resources — 创建项目并复用已有资源", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(120_000);
    const userEmail = cached.users.project_admin.email;
    let createdProjectId: string | undefined;
    const t0 = Date.now();

    try {
      await seed.deleteProjectsByExactName(PROJECT_CREATE_RECORDING_NAME, userEmail);
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      const win = await runProjectCreateExistingResources(page, cached, (projectId) => {
        createdProjectId = projectId;
      });
      await finalize(page, "project-create-existing-resources", undefined, drawTrim(win, t0));
    } finally {
      if (createdProjectId) await seed.deleteProject(createdProjectId, userEmail);
    }
  });

  test("video-track-carryover — 跨帧虚影 Tab 续写", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
      video: { trackContinueAutoAdvance: true },
    });
    const cleanup = registerVideoAnnotationCleanup(cached);
    try {
      const win = await runVideoTrackCarryover(page, cached, (id) =>
        cleanup.annotationIds.push(id),
      );
      flowBehaviorEvidence["video-track-carryover"] = win.evidence;
      await finalize(
        page,
        "video-track-carryover",
        path.join(DOCS_IMAGES, "workbench/video-track-carryover-ghost.gif"),
        { fps: 4, maxWidth: 600, maxColors: 96, ...drawTrim(win, t0) },
      );
    } finally {
      cleanupVideoFrameInference(cleanup);
    }
  });

  test("video-mask-track-edit — Mask 轨迹创建与后续帧编辑", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // 视频解码、两次 Mask 提交与归档
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    const cleanup = registerVideoAnnotationCleanup(cached);
    try {
      const win = await runVideoMaskTrackEdit(page, cached, (id) => cleanup.annotationIds.push(id));
      flowBehaviorEvidence["video-mask-track-edit"] = win.evidence;
      await finalize(page, "video-mask-track-edit", undefined, drawTrim(win, t0));
    } finally {
      cleanupVideoFrameInference(cleanup);
    }
  });

  test("ai-tracker-panel — AI 与追踪面板停靠、隐藏恢复与并存", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    if (SELECTED_CAPTURE) setFlowTimeout(1_800_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "standard" },
    });
    const win = await runAiTrackerPanel(page, cached);
    await finalize(page, "ai-tracker-panel", undefined, drawTrim(win, t0));
  });

  test("pointcloud-controls — 点云控件(上色/点大小/深度)", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 180_000); // 点云加载与 4K H.264 归档都较重
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runPointcloudControls(page, cached);
    await finalize(page, "pointcloud-controls", undefined, drawTrim(win, t0));
  });

  test("pointcloud-view — 点云视图导航(拖动旋转)", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 180_000); // 点云加载与 4K H.264 归档都较重
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    const win = await runPointcloudView(page, cached);
    await finalize(page, "pointcloud-view", undefined, drawTrim(win, t0));
  });

  test("pointcloud-billboard-label — 3D 框标签内容与多角度正对", async ({ page, seed }) => {
    test.skip(
      test.info().project.name !== MARKETING_PROJECT_NAME,
      "billboard 多角度核对需要 marketing-master 的硬件 WebGL 与 60Hz 运行面",
    );
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 120_000);
    const userEmail = cached.users.admin.email;
    const task = cached.projects.pointcloud_demo.tasks.frame_000;
    let annotationId: string | null = null;
    const t0 = Date.now();
    try {
      const source = await seed.createTaskAnnotation(task.id, userEmail, {
        annotation_type: "box_3d",
        tool_unit_id: "lidar_box_3d",
        class_name: "object",
        geometry: NUSCENES_RECORDING_BOX,
        attributes: { kind: "车辆", visibility: "清晰可见" },
      });
      annotationId = source.id;

      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        common: {
          labelVisibility: "always",
          labelContent: { single: [], track: ["id", "state"], ai: ["source", "score"] },
        },
      });
      const win = await runPointcloudBillboardLabel(page, cached, {
        taskId: task.id,
        annotationId: source.id,
        geometry: source.geometry,
      });
      await finalize(page, "pointcloud-billboard-label", undefined, drawTrim(win, t0));
    } finally {
      if (annotationId) await seed.deleteTaskAnnotation(task.id, annotationId, userEmail);
    }
  });

  test("storage-connector-create-test — 创建 S3 连接器并测试样本数", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(90_000);
    const userEmail = cached.users.admin.email;
    let connectionId: string | null = null;
    const t0 = Date.now();
    try {
      await seed.deleteStorageConnectionsByExactName(STORAGE_CONNECTOR_RECORDING_NAME, userEmail);
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      const win = await runStorageConnectorCreateTest(page, (createdId) => {
        connectionId = createdId;
      });
      await finalize(page, "storage-connector-create-test", undefined, drawTrim(win, t0));
    } finally {
      if (connectionId) await seed.deleteStorageConnection(connectionId, userEmail);
    }
  });

  test("video-draw — 视频画框轨迹(track 关键帧插值)", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 120_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    const cleanup = registerVideoAnnotationCleanup(cached);
    try {
      const win = await runVideoDraw(page, cached, (id) => cleanup.annotationIds.push(id));
      flowBehaviorEvidence["video-draw"] = win.evidence;
      await finalize(
        page,
        "video-draw",
        path.join(DOCS_IMAGES, "workbench/video-track-trajectory.gif"),
        { fps: 3, maxWidth: 520, maxColors: 80, ...drawTrim(win, t0) },
      );
    } finally {
      cleanupVideoFrameInference(cleanup);
    }
  });

  test("large-image-progressive — 大图渐进式高清切片", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 90_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "focus" },
    });
    const browserErrors: string[] = [];
    const onPageError = (error: Error) => {
      browserErrors.push(error.message);
    };
    page.on("pageerror", onPageError);
    try {
      const win = await runLargeImageProgressive(page, cached);
      expect(browserErrors, "large-image-progressive rendered tile errors").toEqual([]);
      await finalize(page, "large-image-progressive", undefined, drawTrim(win, t0));
    } finally {
      page.off("pageerror", onPageError);
    }
  });

  test("large-image-pyramid-recovery — 单切片失败后自动恢复", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 90_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "focus" },
    });
    const win = await runLargeImagePyramidRecovery(page, cached);
    await finalize(page, "large-image-pyramid-recovery", undefined, drawTrim(win, t0));
  });

  test("large-image-mask-limit — 超大图矢量标注与 Mask 尺寸门禁", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 90_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "focus" },
    });
    const win = await runLargeImageMaskLimit(page, cached);
    await finalize(page, "large-image-mask-limit", undefined, drawTrim(win, t0));
  });

  test("pointcloud-camera-seed-3d-box — 相机图辅助生成并核对 3D 框", async ({ page, seed }) => {
    test.skip(
      test.info().project.name !== MARKETING_PROJECT_NAME,
      "真实点云种框需要 marketing-master 的硬件 WebGL 与 60Hz 运行面",
    );
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 120_000);
    const t0 = Date.now();
    const userEmail = cached.users.admin.email;
    let created: { taskId: string; annotationId: string } | null = null;
    try {
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "annotate:3d", preset: "standard" },
      });
      const win = await runPointcloudCameraSeed3dBox(page, cached, (record) => {
        created = record;
      });
      created = win.created;
      await finalize(page, "pointcloud-camera-seed-3d-box", undefined, drawTrim(win, t0));
    } finally {
      if (created) {
        await seed.deleteTaskAnnotation(created.taskId, created.annotationId, userEmail);
      }
    }
  });

  test("pointcloud-panel-layout — 3D 三视图与相机面板布局", async ({ page, seed }) => {
    test.skip(
      test.info().project.name !== MARKETING_PROJECT_NAME,
      "3D 面板布局需要 marketing-master 的硬件 WebGL 与 60Hz 运行面",
    );
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 120_000);
    const userEmail = cached.users.admin.email;
    const task = cached.projects.pointcloud_demo.tasks.frame_000;
    let annotationId: string | null = null;
    const t0 = Date.now();
    try {
      const source = await seed.createTaskAnnotation(task.id, userEmail, {
        annotation_type: "box_3d",
        tool_unit_id: "lidar_box_3d",
        class_name: "object",
        geometry: NUSCENES_RECORDING_BOX,
      });
      annotationId = source.id;

      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "annotate:3d", preset: "standard" },
        layout: { cameraPanels: {} },
      });
      const win = await runPointcloudPanelLayout(page, cached, source);
      await finalize(page, "pointcloud-panel-layout", undefined, drawTrim(win, t0));
    } finally {
      if (annotationId) await seed.deleteTaskAnnotation(task.id, annotationId, userEmail);
    }
  });

  test("pointcloud-crossframe-track — 3D 目标跨帧延续、修正与邻帧核对", async ({ page, seed }) => {
    test.skip(
      test.info().project.name !== MARKETING_PROJECT_NAME,
      "真实点云跨帧链需要 marketing-master 的硬件 WebGL 与 60Hz 运行面",
    );
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 120_000);
    const userEmail = cached.users.admin.email;
    const frame0 = cached.projects.pointcloud_demo.tasks.frame_000;
    const cleanup: Array<{ taskId: string; annotationId: string }> = [];
    try {
      const source = await seed.createTaskAnnotation(frame0.id, userEmail, {
        annotation_type: "box_3d",
        tool_unit_id: "lidar_box_3d",
        class_name: "object",
        geometry: NUSCENES_RECORDING_BOX,
      });
      cleanup.push({ taskId: frame0.id, annotationId: source.id });

      const t0 = Date.now();
      await installScreenshotEnvironment(page);
      await seed.injectToken(page, userEmail);
      await applyScreenshotTheme(page, "dark");
      await installRecordingWorkbenchLayout(page, "both", {
        common: {
          crossFrameOverlayEnabled: true,
          crossFrameOverlayK: 1,
          crossFrameOverlayScope: "selected",
        },
      });
      const win = await runPointcloudCrossframeTrack(page, cached, source, (created) => {
        cleanup.push(created);
      });
      await finalize(page, "pointcloud-crossframe-track", undefined, drawTrim(win, t0));
    } finally {
      for (const annotation of cleanup.reverse()) {
        await seed.deleteTaskAnnotation(annotation.taskId, annotation.annotationId, userEmail);
      }
    }
  });

  test("hotkey-cheatsheet — 键盘快捷键面板(? 打开)", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
    });
    const win = await runHotkeyCheatSheet(page, cached);
    await finalize(
      page,
      "hotkey-cheatsheet",
      // 面板以文字 + kbd 为主、帧间变化小，沿用画布档 fps8/900 即可压到 5MB 内。
      path.join(DOCS_IMAGES, "workbench/hotkey-cheatsheet.gif"),
      { fps: 8, maxWidth: 900, ...drawTrim(win, t0) },
    );
  });

  test("workspace-named-preset — 命名布局预设的保存、套用与撤销", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    // 与 workspace-layout-persistence 相同：流程自己走真实偏好读写与刷新核验，
    // 隔离库的播种 / 重建可能很慢，测试预算单独给出。
    setFlowTimeout(420_000);
    const t0 = Date.now();
    await seed.injectToken(page, cached.users.annotator.email);
    let annotationId: string | undefined;
    try {
      const result = await runWorkspaceNamedPreset(page, cached);
      annotationId = result.evidence.annotationId;
      flowBehaviorEvidence["workspace-named-preset"] = result.evidence;
      await finalize(page, "workspace-named-preset", undefined, drawTrim(result, t0));
    } finally {
      // 偏好由流程自己恢复；这里只删除本流程画出的那一个对象。
      if (annotationId)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          annotationId,
          cached.users.annotator.email,
        );
    }
  });

  test("polygon-boundary-trace — 沿已有边界追踪相邻多边形", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let created: string[] = [];
    try {
      const win = await runPolygonBoundaryTrace(page, cached);
      created = [win.evidence.sourceId, win.evidence.savedId];
      flowBehaviorEvidence["polygon-boundary-trace"] = win.evidence;
      await finalize(page, "polygon-boundary-trace", undefined, drawTrim(win, t0));
    } finally {
      for (const id of created)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          id,
          cached.users.annotator.email,
        );
    }
  });

  test("mask-slice-instances — Mask 直线切割为两个实例", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    const taskId = cached.projects.image_demo.tasks.annotating.id;
    const userEmail = cached.users.annotator.email;
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, userEmail);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let created: string[] = [];
    try {
      const win = await runMaskSliceInstances(page, cached);
      created = (win.evidence.cleanup_annotation_ids as string[]) ?? [];
      flowBehaviorEvidence["mask-slice-instances"] = win.evidence;
      await finalize(page, "mask-slice-instances", undefined, drawTrim(win, t0));
    } finally {
      // 精确删除流程自己上传并切分的两个实例（像素读回已在母版之外完成）。
      for (const id of created) await seed.deleteTaskAnnotation(taskId, id, userEmail);
    }
  });

  test("polygon-slice-undo — 多边形切割与撤销重做", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.annotator.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      image: { afterBoxCreate: "pick_class" },
    });
    let created: string[] = [];
    try {
      const win = await runPolygonSliceUndo(page, cached);
      created = [win.evidence.sourceId, win.evidence.createdId];
      flowBehaviorEvidence["polygon-slice-undo"] = win.evidence;
      await finalize(page, "polygon-slice-undo", undefined, drawTrim(win, t0));
    } finally {
      for (const id of created)
        await seed.deleteTaskAnnotation(
          cached.projects.image_demo.tasks.annotating.id,
          id,
          cached.users.annotator.email,
        );
    }
  });

  test("video-comment-badges — 视频对象评论角标与对象讨论", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 180_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      common: { petEnabled: false, showAnnotationComments: true },
      workspace: { context: "annotate:video", preset: "standard" },
    });
    const result = await runVideoCommentBadges(page, cached);
    flowBehaviorEvidence["video-comment-badges"] = result.evidence;
    await finalize(page, "video-comment-badges", undefined, drawTrim(result, t0));
  });

  test("video-issue-resolution — 视频问题定位与连续处理", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 420_000 : 180_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:video", preset: "video-tracking" },
    });
    // 两条问题夹具由流程自身在 finally 中通过正式接口按 id 精确清理。
    const win = await runVideoIssueResolution(page, cached, () => undefined);
    flowBehaviorEvidence["video-issue-resolution"] = win.evidence;
    await finalize(page, "video-issue-resolution", undefined, drawTrim(win, t0));
  });

  test("project-guide-autosave — 标注指引表格编辑自动保存", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    // 设置页无需工作台布局预置；流程自己在 finally 中恢复该项目的原始指引。
    const win = await runProjectGuideAutosave(page, cached);
    flowBehaviorEvidence["project-guide-autosave"] = win.evidence;
    await finalize(page, "project-guide-autosave", undefined, drawTrim(win, t0));
  });

  test("discussion-mention-return — 提及成员并从通知返回原评论", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    // 双浏览器上下文（管理员 + 被提及成员）登录与通知往返最慢：单独放宽预算。
    setFlowTimeout(SELECTED_CAPTURE ? 600_000 : 300_000);
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      common: { petEnabled: false },
      workspace: { context: "annotate:image", preset: "standard" },
    });
    const result = await runDiscussionMentionReturn(page, cached);
    flowBehaviorEvidence["discussion-mention-return"] = result.evidence;
    await finalize(page, "discussion-mention-return", undefined, drawTrim(result, t0));
  });

  test("discussion-target-drafts — 讨论草稿按收件人各自保留", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    const taskId = cached.projects.image_demo.tasks.annotating.id;
    const userEmail = cached.users.annotator.email;
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, userEmail);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      // 讨论列写宽：草稿文字在母版中可读（拖拽方案见 _workbench-layout.ts 注释）。
      rightColumnWidth: 460,
      image: { afterBoxCreate: "pick_class" },
    });
    // 流程只选中这个已保存对象并验证它的独立草稿，不修改它。
    const annotation = await seed.createTaskAnnotation(taskId, userEmail, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.58, y: 0.52, w: 0.16, h: 0.14 },
    });
    try {
      const win = await runDiscussionTargetDrafts(page, cached, { annotationId: annotation.id });
      flowBehaviorEvidence["discussion-target-drafts"] = win.evidence;
      await finalize(page, "discussion-target-drafts", undefined, drawTrim(win, t0));
    } finally {
      await seed.deleteTaskAnnotation(taskId, annotation.id, userEmail);
    }
  });

  test("task-drawing-comment — 在题图上圈点并随评论发送", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    const taskId = cached.projects.image_demo.tasks.annotating.id;
    const userEmail = cached.users.annotator.email;
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, userEmail);
    await applyScreenshotTheme(page, "dark");
    await installRecordingWorkbenchLayout(page, "both", {
      workspace: { context: "annotate:image", preset: "standard" },
      // 讨论列写宽：草稿文字在母版中可读（拖拽方案见 _workbench-layout.ts 注释）。
      rightColumnWidth: 460,
      image: { afterBoxCreate: "pick_class" },
    });
    // 一个真实落库的标注：流程读回它证明圈点没有改动已保存标注；
    // 评论由流程自己按 id 删除。
    const kept = await seed.createTaskAnnotation(taskId, userEmail, {
      annotation_type: "bbox",
      tool_unit_id: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.72, y: 0.62, w: 0.13, h: 0.12 },
    });
    try {
      const win = await runTaskDrawingComment(page, cached);
      flowBehaviorEvidence["task-drawing-comment"] = win.evidence;
      await finalize(page, "task-drawing-comment", undefined, drawTrim(win, t0));
    } finally {
      await seed.deleteTaskAnnotation(taskId, kept.id, userEmail);
    }
  });

  test("project-invitation-join — 项目邀请创建与被邀请人加入", async ({ page, seed }) => {
    if (!cached) throw new Error("screenshot seed catalog 未完成");
    setFlowTimeout(SELECTED_CAPTURE ? 300_000 : 180_000); // Include isolated seed and encoding.
    const t0 = Date.now();
    await installScreenshotEnvironment(page);
    await seed.injectToken(page, cached.users.admin.email);
    await applyScreenshotTheme(page, "dark");
    // 流程自身完成退出与被邀请人登录；finally 撤销邀请或移除本次新增的成员关系。
    const win = await runProjectInvitationJoin(page, cached, () => undefined);
    flowBehaviorEvidence["project-invitation-join"] = win.evidence;
    await finalize(page, "project-invitation-join", undefined, drawTrim(win, t0));
  });
});

// 由绘制起止时间戳算 GIF 裁剪窗口：startSec 跳过开头(加载/隐藏预测/选工具)，
// durationSec 只留绘制段(裁掉结尾的删除清理)。win 为 null(工具缺失)时不裁。
// 只保留生产者明确标记的稳定内容；额外首尾 padding 会把加载与 finally 清理重新收进片段。
function drawTrim(
  win: { drawStartMs: number; drawEndMs: number } | null,
  t0: number,
): Pick<GifOptions, "startSec" | "durationSec" | "captureWindow"> {
  if (!win) return {};
  const startSec = Math.max(0, (win.drawStartMs - t0) / 1000);
  const durationSec = (win.drawEndMs - win.drawStartMs) / 1000;
  return {
    startSec,
    durationSec,
    captureWindow: {
      startEpochMs: win.drawStartMs,
      endEpochMs: win.drawEndMs,
    },
  };
}
