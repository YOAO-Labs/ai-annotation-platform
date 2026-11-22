import { WORKBENCH_LOCAL_DEFAULTS } from "../../state/workbenchLocalDefaults";

export const POINT_CLOUD_WEBGPU_STORAGE_KEY = "aap.experiment.pointCloudWebGpuRenderer";

export type PointCloudRendererMode = "legacy" | "webgpu-experimental";

export function readPointCloudWebGpuExperiment(): boolean {
  const fallback = WORKBENCH_LOCAL_DEFAULTS["experiment.pointCloudWebGpuRenderer"];
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(POINT_CLOUD_WEBGPU_STORAGE_KEY);
    return raw === null ? fallback : raw === "1" || raw === "true";
  } catch {
    return fallback;
  }
}

export function writePointCloudWebGpuExperiment(value: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(POINT_CLOUD_WEBGPU_STORAGE_KEY, value ? "1" : "0");
  } catch {
    // Local experimental flags are best-effort.
  }
}

export function pointCloudRendererModeFromExperiment(enabled: boolean): PointCloudRendererMode {
  return enabled ? "webgpu-experimental" : "legacy";
}
