import { request, type FullConfig } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../fixtures/seed";
import { storeScreenshotCatalog } from "./catalog-runtime";
import { recordingPlan, screenshotCatalogPath } from "./recording-plan.mjs";
import { SCREENSHOT_MATRIX_PROJECTS } from "./matrix";

export default async function screenshotGlobalSetup(config: FullConfig): Promise<void> {
  const scope = process.env.SCREENSHOT_BACKEND_REQUIREMENTS;
  const staticProfile = process.env.SCREENSHOT_STATIC_PROFILE;
  if (staticProfile !== undefined) {
    if (staticProfile !== "non-ml") {
      throw new Error(`Unsupported SCREENSHOT_STATIC_PROFILE "${staticProfile}"; use "non-ml".`);
    }
    if (
      scope !== undefined ||
      process.env.SCREENSHOT_RECORDING_FLOWS !== undefined ||
      process.env.SCREENSHOT_RECORDING_PROFILE !== undefined
    ) {
      throw new Error("Static screenshot selection cannot be combined with a recording scope.");
    }
    if (
      config.projects.some(
        (project) =>
          !SCREENSHOT_MATRIX_PROJECTS.includes(
            project.name as (typeof SCREENSHOT_MATRIX_PROJECTS)[number],
          ),
      ) ||
      SCREENSHOT_MATRIX_PROJECTS.some(
        (name) => !config.projects.some((project) => project.name === name),
      )
    ) {
      throw new Error(
        "The non-ML static profile requires only desktop-light, desktop-dark, and mobile projects.",
      );
    }
  }
  if (scope !== undefined) {
    const plan = recordingPlan(
      (process.env.SCREENSHOT_RECORDING_FLOWS ?? "").split(","),
      process.env.SCREENSHOT_RECORDING_PROFILE,
    );
    if (
      scope !== plan.backendRequirements ||
      config.projects.some((project) => !["flows", "marketing-master"].includes(project.name))
    ) {
      throw new Error(
        "Scoped backend validation is only supported for the matching recording selection.",
      );
    }
  }

  const catalogScope = staticProfile === "non-ml" ? "none" : scope;
  const apiBase = process.env.PLAYWRIGHT_API_BASE ?? "http://127.0.0.1:8010";
  const context = await request.newContext({ baseURL: apiBase });
  try {
    const response = await context.get(screenshotCatalogPath(catalogScope));
    if (!response.ok()) {
      const readiness =
        staticProfile === "non-ml"
          ? "请先运行 screenshots seed，并确保静态项目、任务和真实媒体夹具可用。"
          : "请先运行 screenshots seed，并确保 live backend 或 protocol stub 已启动。";
      throw new Error(
        `seed/catalog failed: ${response.status()} ${await response.text()}\n${readiness}`,
      );
    }
    const catalog = (await response.json()) as ScreenshotSeedCatalog;
    if (
      catalogScope !== undefined &&
      catalog.backend_requirements?.join(",") !== (catalogScope === "none" ? "" : catalogScope)
    ) {
      throw new Error(
        "Capture API did not honor the selected backend scope; refresh the API runtime.",
      );
    }
    storeScreenshotCatalog(catalog);
  } finally {
    await context.dispose();
  }
}
