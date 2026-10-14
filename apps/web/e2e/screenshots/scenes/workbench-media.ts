import { openContextToolbar } from "../../fixtures/context-toolbar";
import { waitForScreenshotVideo } from "../environment";
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

async function waitForToastToDisappear(page: Page, message: string): Promise<void> {
  const toast = page.locator("[data-sonner-toast]").filter({ hasText: message });
  await expect(toast).toBeVisible({ timeout: 5_000 });
  await expect(toast).toBeHidden({ timeout: 15_000 });
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
      await waitForScreenshotVideo(page);
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
      await waitForScreenshotVideo(page);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/workbench/video-real-scene.png",
  },
  {
    name: "workbench/video-ai-tracking-panel",
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
      await reloadWithSidebarLayout(page, "both");
      await page.getByTestId("video-timeline-shell").waitFor({ state: "visible", timeout: 15_000 });
      await waitForScreenshotVideo(page);

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
      await page.getByText("实时同步", { exact: true }).waitFor({ timeout: 5000 });
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
      const viewport = page.getByTestId("pc-viewport");
      await viewport.waitFor({ state: "visible", timeout: 20_000 });
      await recordingLayoutCommand(page, "传感器融合");
      await waitForToastToDisappear(page, "已切换到传感器融合");
      await recordingLayoutCommand(page, "全部相机停靠");
      await waitForRecordingPanels(page, ["canvas", "camera-view"]);
      await expect(viewport).toHaveAttribute("data-pointcloud-renderer-count", "1");
      await expect(page.getByTestId("pointcloud-loading")).toBeHidden({ timeout: 20_000 });
      await expect(page.getByTestId("pointcloud-renderer-error")).toBeHidden();
      const pointStats = page.getByTestId("pointcloud-stats");
      await expect(pointStats).toBeVisible({ timeout: 20_000 });
      await expect
        .poll(
          async () => {
            const count = (await pointStats.textContent())?.match(/^\s*([\d,]+)\s*点/)?.[1];
            return count ? Number(count.replace(/,/g, "")) : 0;
          },
          { timeout: 20_000 },
        )
        .toBeGreaterThan(0);

      const taskId = new URL(page.url()).searchParams.get("task");
      if (!taskId) throw new Error("[pointcloud-real-scene] 页面 URL 缺少 task ID");
      const token = await page.evaluate(() => localStorage.getItem("token"));
      if (!token) throw new Error("[pointcloud-real-scene] 缺少登录凭据");
      const api = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
      const annotationResponse = await page.request.get(
        `${api}/api/v1/tasks/${taskId}/annotations`,
        { headers: { Authorization: `Bearer ${token}` } },
      );
      expect(annotationResponse.ok(), "[pointcloud-real-scene] 读取标注列表失败").toBeTruthy();
      expect(await annotationResponse.json()).toEqual([]);
      const cameraGallery = page.locator("[data-camera-dock-panel]");
      await expect(cameraGallery).toBeVisible();
      const cameraImages = cameraGallery.locator("img");
      await expect(cameraImages).toHaveCount(6, { timeout: 10_000 });
      const channelNames = await cameraGallery
        .locator("section[aria-label]")
        .evaluateAll((sections) =>
          sections.map((section) => section.getAttribute("aria-label") ?? ""),
        );
      expect(channelNames).toHaveLength(6);
      expect(channelNames.every((name) => name.trim().length > 0)).toBeTruthy();
      expect(new Set(channelNames).size).toBe(6);
      expect(
        await cameraImages.evaluateAll((images) =>
          images.map((image) => (image as HTMLImageElement).alt),
        ),
      ).toEqual(channelNames);
      await expect
        .poll(
          async () =>
            cameraImages.evaluateAll((images) =>
              images.every((element) => {
                const image = element as HTMLImageElement;
                return image.complete && image.naturalWidth > 0;
              }),
            ),
          { timeout: 10_000 },
        )
        .toBe(true);
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
