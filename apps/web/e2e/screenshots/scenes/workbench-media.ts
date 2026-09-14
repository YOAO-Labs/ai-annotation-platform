import { openContextToolbar } from "../../fixtures/context-toolbar";
import type { ScreenshotScene } from "./_types";
import { expect, type Page } from "@playwright/test";
import type { WorkbenchLayoutPreferences } from "../../../src/api/auth";
import {
  recordingLayoutCommand,
  recordingPanelCommand,
  waitForRecordingPanels,
  installRecordingWorkbenchLayout,
  type RecordingWorkbenchOverrides,
  waitForRecordingWorkbenchLayout,
} from "../flows/_workbench-layout";

const DARK_WORKBENCH_MATRIX: NonNullable<ScreenshotScene["matrix"]> = {
  themes: ["dark"],
  primaryTheme: "dark",
};

const MULTI_CAMERA_ROLES = [
  "front",
  "front_left",
  "front_right",
  "back",
  "back_left",
  "back_right",
] as const;
const EXPANDED_MULTI_CAMERA_PANELS = Object.fromEntries(
  MULTI_CAMERA_ROLES.map((role) => [role, { x: null, y: null, collapsed: false }]),
) as WorkbenchLayoutPreferences["cameraPanels"];

async function reloadWithSidebarLayout(
  page: Page,
  mode: "both" | "none",
  overrides: RecordingWorkbenchOverrides = {},
): Promise<void> {
  await installRecordingWorkbenchLayout(page, mode, overrides);
  await page.reload({ waitUntil: "domcontentloaded" });
  if (overrides.workspace) {
    await waitForRecordingPanels(page, ["canvas"]);
  } else {
    await waitForRecordingWorkbenchLayout(page, mode);
  }
}

export const WORKBENCH_MEDIA_SCENES: ScreenshotScene[] = [
  {
    name: "workbench/video-real-scene",
    clock: "live",
    role: "admin",
    fixture: {
      project: "video_demo",
      task: "tracking",
      backend: "video_tracker",
      capabilities: ["task:tracker"],
    },
    route: (catalog) => {
      const project = catalog.projects.video_demo;
      return `/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`;
    },
    prepare: async (page) => {
      await reloadWithSidebarLayout(page, "both", {
        workspace: { context: "annotate:video", preset: "video-tracking" },
      });
      await page.getByRole("tab", { name: "标注详情", exact: true }).click();
      await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 15_000 });
      await page.getByTestId("video-konva-stage").waitFor({ state: "visible", timeout: 10_000 });
      await page.getByText("实时同步", { exact: true }).waitFor({ timeout: 5000 });

      // 后台追踪若在 seed repair 前刚好完成，候选审阅条可能晚到；静态场景只展示干净工作台。
      const trackerReview = page.getByTestId("video-tracker-review-bar");
      if (await page.getByTestId("tracker-review-tool-capsule").isVisible()) {
        await openContextToolbar(page, "tracker-review");
        await page.getByTestId("tracker-review-discard").click();
        await trackerReview.waitFor({ state: "hidden", timeout: 5000 });
        await page
          .getByText("已丢弃 AI 追踪候选", { exact: true })
          .waitFor({ state: "hidden", timeout: 10_000 });
      }

      await page.getByTitle("适应视口（双击空白）").click();
      await page.waitForTimeout(800);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/workbench/video-real-scene.png",
  },
  {
    name: "workbench/video-ai-tracking-panel",
    role: "admin",
    fixture: {
      project: "video_demo",
      task: "tracking",
      backend: "video_tracker",
      capabilities: ["task:tracker"],
    },
    route: (catalog) => {
      const project = catalog.projects.video_demo;
      return `/projects/${project.id}/annotate?task=${project.tasks.tracking.id}`;
    },
    prepare: async (page) => {
      await reloadWithSidebarLayout(page, "both");
      await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 15_000 });
      await page.getByTestId("video-konva-stage").waitFor({ state: "visible", timeout: 10_000 });

      const trackerReview = page.getByTestId("video-tracker-review-bar");
      if (await page.getByTestId("tracker-review-tool-capsule").isVisible()) {
        await openContextToolbar(page, "tracker-review");
        await page.getByTestId("tracker-review-discard").click();
        await trackerReview.waitFor({ state: "hidden", timeout: 5000 });
      }

      await page.getByTitle("适应视口（双击空白）").click();
      await page.getByTestId("workbench-ai-tracker").click();
      await page
        .getByTestId("video-tracker-propagate-dialog")
        .waitFor({ state: "visible", timeout: 5000 });
      await page.waitForTimeout(500);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/video-propagate/ai-tracking-panel.png",
  },
  {
    name: "workbench/pointcloud-real-scene",
    clock: "live",
    role: "admin",
    fixture: { project: "pointcloud_multicam_demo", task: "frame_000" },
    route: (catalog) => {
      const project = catalog.projects.pointcloud_multicam_demo;
      return `/projects/${project.id}/annotate?task=${project.tasks.frame_000.id}`;
    },
    prepare: async (page) => {
      await reloadWithSidebarLayout(page, "both", {
        workspace: { context: "annotate:3d", preset: "standard" },
        layout: { cameraPanels: EXPANDED_MULTI_CAMERA_PANELS },
      });
      await page.getByTestId("pc-viewport").waitFor({ state: "visible", timeout: 20_000 });
      await recordingLayoutCommand(page, "传感器融合");
      await recordingLayoutCommand(page, "全部相机停靠");
      await waitForRecordingPanels(page, ["canvas", "camera-view"]);
      const cameraImages = page.locator("[data-camera-dock-panel] img");
      await expect(cameraImages).toHaveCount(6, { timeout: 10_000 });
      await cameraImages.evaluateAll(async (images: HTMLImageElement[]) => {
        await Promise.all(
          images.map((image) => {
            if (image.complete && image.naturalWidth > 0) return Promise.resolve();
            return new Promise<void>((resolve, reject) => {
              image.addEventListener("load", () => resolve(), { once: true });
              image.addEventListener("error", () => reject(new Error("相机图加载失败")), {
                once: true,
              });
            });
          }),
        );
      });
      await page.waitForTimeout(1800);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/workbench/pointcloud-real-scene.png",
  },
  {
    name: "workbench/ocr-real-scene",
    clock: "live",
    role: "admin",
    fixture: {
      project: "ocr_demo",
      task: "ocr",
      backend: "ocr",
      capabilities: ["task:ocr", "output:polygon", "attribute:text"],
    },
    route: (catalog) => {
      const project = catalog.projects.ocr_demo;
      return `/projects/${project.id}/annotate?task=${project.tasks.ocr.id}`;
    },
    prepare: async (page) => {
      await page.getByTestId("workbench-stage").waitFor({ state: "visible", timeout: 10_000 });
      await reloadWithSidebarLayout(page, "both", {
        workspace: { context: "annotate:image", preset: "ai-review" },
      });
      await recordingPanelCommand(page, "讨论", "隐藏面板");
      const panel = page.getByTestId("ai-prediction-popover");
      await panel.waitFor({ state: "visible", timeout: 5000 });
      await waitForRecordingPanels(page, ["canvas", "ai-task"]);
      await page.waitForTimeout(300);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/workbench/ocr-real-scene.png",
  },
];
