export type { Role, MatrixAxis, ScreenshotScene } from "./_types";
import type { ScreenshotScene } from "./_types";

import { AUTH_SCENES } from "./auth";
import { PROJECT_SCENES } from "./projects";
import { REVIEW_SCENES } from "./review";
import { EXPORT_SCENES } from "./export";
import { AI_PRE_SCENES } from "./ai-pre";
import { PLATFORM_SCENES } from "./platform";
import { WORKFLOW_SCENES } from "./workflows";
import { WORKBENCH_AI_SCENES } from "./workbench-ai";
import { WORKBENCH_MEDIA_SCENES } from "./workbench-media";
import { DATASET_SCENES } from "./datasets";
import { SETTINGS_SCENES } from "./settings";
import { SUPERADMIN_SCENES } from "./superadmin";

export type ResolvedScreenshotScene = ScreenshotScene & { source: string };

function fromSource(source: string, scenes: ScreenshotScene[]): ResolvedScreenshotScene[] {
  return scenes.map((scene) => ({ ...scene, source }));
}

export const SCENES: ResolvedScreenshotScene[] = [
  ...fromSource("apps/web/e2e/screenshots/scenes/auth.ts", AUTH_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/projects.ts", PROJECT_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/review.ts", REVIEW_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/export.ts", EXPORT_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/ai-pre.ts", AI_PRE_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/platform.ts", PLATFORM_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/workflows.ts", WORKFLOW_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/workbench-ai.ts", WORKBENCH_AI_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/workbench-media.ts", WORKBENCH_MEDIA_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/datasets.ts", DATASET_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/settings.ts", SETTINGS_SCENES),
  ...fromSource("apps/web/e2e/screenshots/scenes/superadmin.ts", SUPERADMIN_SCENES),
];

export function isMLScreenshotScene(scene: ScreenshotScene): boolean {
  return (
    scene.requiresML === true ||
    Boolean(scene.fixture?.backend) ||
    (scene.fixture?.capabilities?.length ?? 0) > 0
  );
}

export function selectScreenshotScenes(profile: string | undefined) {
  if (profile === undefined) return { scenes: SCENES, excluded: [] as string[] };
  if (profile !== "non-ml") {
    throw new Error(
      `Unsupported SCREENSHOT_STATIC_PROFILE "${profile}"; use "non-ml" or unset it.`,
    );
  }

  const scenes = SCENES.filter((scene) => !isMLScreenshotScene(scene));
  const excluded = SCENES.filter(isMLScreenshotScene).map((scene) => scene.name);
  if (!scenes.length || scenes.some(isMLScreenshotScene)) {
    throw new Error("The non-ML static screenshot selection is empty or includes an ML scene.");
  }
  return { scenes, excluded };
}
