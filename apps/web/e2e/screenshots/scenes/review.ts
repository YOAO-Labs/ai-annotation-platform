import type { ScreenshotScene } from "./_types";
import { waitForScreenshotImage } from "../environment";

import type { Page } from "@playwright/test";
import {
  installRecordingWorkbenchLayout,
  waitForRecordingPanels,
} from "../flows/_workbench-layout";

const DARK_WORKBENCH_MATRIX: NonNullable<ScreenshotScene["matrix"]> = {
  themes: ["dark"],
  primaryTheme: "dark",
};

async function waitForReviewWorkbench(page: Page): Promise<void> {
  await page.getByTestId("review-reject").waitFor({ state: "visible", timeout: 10_000 });
  await waitForScreenshotImage(page);
}

const reviewWorkbenchRoute = (catalog: Parameters<ScreenshotScene["route"]>[0]) => {
  const project = catalog.projects.image_demo;
  const params = new URLSearchParams({
    batch: project.batches.review.id,
    task: project.tasks.review.id,
    returnTo: `/review?project=${project.id}&batch=${project.batches.review.id}`,
  });
  return `/projects/${project.id}/review?${params.toString()}`;
};

export const REVIEW_SCENES: ScreenshotScene[] = [
  {
    name: "review/workbench",
    clock: "live",
    role: "reviewer",
    fixture: { project: "image_demo", task: "review", batch: "review" },
    route: reviewWorkbenchRoute,
    prepare: async (page) => {
      await installRecordingWorkbenchLayout(page, "both", {
        workspace: { context: "review:image", preset: "review" },
      });
      await page.reload();
      await waitForReviewWorkbench(page);
      await waitForRecordingPanels(page, ["canvas", "inspector", "discussion"]);
    },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/review/workbench.png",
  },
  {
    name: "review/reject-form",
    clock: "live",
    role: "reviewer",
    fixture: { project: "image_demo", task: "review", batch: "review" },
    route: reviewWorkbenchRoute,
    prepare: async (page) => {
      await waitForReviewWorkbench(page);
      await page.getByTestId("review-reject").click();
      const reason = page.getByRole("alertdialog", { name: "退回原因（1 个任务）" });
      await reason.getByRole("button", { name: "位置或尺寸不准", exact: true }).click();
      const comment = page.getByRole("alertdialog", { name: "补充说明", exact: true });
      await comment.getByRole("button", { name: "位置与边界不准", exact: true }).click();
      await comment.getByRole("textbox", { name: "补充说明（可选）" }).waitFor();
    },
    // 第二步的真实补充说明与快捷标签，不提交退回，也不修改待审任务。
    capture: { kind: "locator", selector: '[role="alertdialog"]', padding: 0 },
    matrix: DARK_WORKBENCH_MATRIX,
    target: "docs-site/user-guide/images/review/reject-form.png",
  },
  {
    // ReviewPage 落地态：左侧批次树 + 右侧按项目分组的批次卡片网格。
    name: "review/review-list-page",
    role: "reviewer",
    fixture: { project: "image_demo", batch: "review" },
    route: () => "/review",
    prepare: async (page, catalog) => {
      await page.waitForLoadState("networkidle");
      await page
        .getByRole("button")
        .filter({ hasText: catalog.projects.image_demo.batches.review.display_id })
        .filter({ hasText: "审核中" })
        .waitFor({ state: "visible", timeout: 10_000 });
    },
    capture: { kind: "fullPage" },
    target: "docs-site/user-guide/images/review/review-list-page.png",
  },
];
