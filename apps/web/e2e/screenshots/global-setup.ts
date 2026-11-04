import { spawnSync } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { FullConfig } from "@playwright/test";
import type { ScreenshotSeedCatalog } from "../fixtures/seed";
import { closeFixtureTransport, getScreenshotCatalog } from "../helpers/seed-owner-transport.mjs";
import { cleanupScreenshotRun } from "./fixture-run.mjs";
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
  const runId = process.env.SCREENSHOT_FIXTURE_RUN_ID ?? "";
  const catalogPath = screenshotCatalogPath(runId, catalogScope);
  const root = realpathSync(process.env.AAP_WORKTREE_RESOURCE_ROOT ?? "");
  const identity = JSON.parse(readFileSync(path.join(root, ".worktree/identity.json"), "utf8"));
  if (identity.root !== root || !/^[a-f0-9]{16}$/.test(identity.id)) {
    throw new Error("Screenshot worker identity is not the worktree identity.");
  }
  const expected = `worktree-${identity.id}-${process.env.AAP_WORKTREE_MODE}`;
  const healthResponse = await fetch(`${apiBase}/health`, { signal: AbortSignal.timeout(15_000) });
  if (!healthResponse.ok) throw new Error(`Capture API health failed: ${healthResponse.status}`);
  const health = await healthResponse.json();
  const workers: { name?: string }[] = health?.checks?.celery?.workers ?? [];
  if (
    !workers.some((worker) => worker.name === `${expected}@local`) ||
    workers.some(
      (worker) =>
        ![`${expected}@local`, `${expected}-maintenance@local`].includes(worker.name ?? ""),
    )
  ) {
    throw new Error("Screenshot capture requires the verified isolated worktree workers.");
  }
  const apiRoot = fileURLToPath(new URL("../../../api/", import.meta.url));
  const args = [
    "scripts/seed.py",
    "--profile",
    "screenshots",
    "--run-id",
    runId,
    "--ml-backend-mode",
    process.env.SCREENSHOT_ML_BACKEND_MODE ?? "live",
    ...(process.env.SCREENSHOT_ASSET_OFFLINE === "1" ? ["--offline"] : []),
    ...(process.env.SCREENSHOT_ML_BACKEND_URL
      ? ["--ml-backend-url", process.env.SCREENSHOT_ML_BACKEND_URL]
      : []),
    ...(catalogScope === undefined ? [] : ["--backend-requirements", catalogScope]),
  ];
  try {
    const seed = spawnSync(path.join(apiRoot, ".venv/bin/python"), args, {
      cwd: apiRoot,
      env: { ...process.env, PYTHONPATH: "." },
      stdio: "inherit",
    });
    if (seed.error) throw seed.error;
    if (seed.status !== 0) throw new Error(`Screenshot seed failed: ${seed.signal ?? seed.status}`);
    const response = await getScreenshotCatalog(`${apiBase}${catalogPath}`);
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
      catalog.run_id !== runId ||
      (catalogScope !== undefined &&
        catalog.backend_requirements?.join(",") !== (catalogScope === "none" ? "" : catalogScope))
    ) {
      throw new Error("Capture catalog did not honor the selected run and backend scope.");
    }
    storeScreenshotCatalog(catalog);
    await closeFixtureTransport();
  } catch (failure) {
    try {
      await cleanupScreenshotRun(runId, { allowMissing: true });
    } catch (cleanupFailure) {
      throw new AggregateError([failure, cleanupFailure], "Screenshot setup and cleanup failed");
    }
    throw failure;
  }
}
