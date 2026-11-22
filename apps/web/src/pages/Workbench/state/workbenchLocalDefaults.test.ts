import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import {
  detectWebCodecsSupport,
  isWebCodecsExperimentEnabled,
  WEBCODECS_FLAG_STORAGE_KEY,
} from "../stage/useVideoChunkDecoder";
import {
  useVideoReferenceConfig,
  VIDEO_REFERENCE_PREDICT_STORAGE_KEY,
} from "../stage/videoReferencePredict";
import {
  POINT_CLOUD_WEBGPU_STORAGE_KEY,
  readPointCloudWebGpuExperiment,
} from "../stages/three-d/pointCloudExperiment";
import { getFieldValue, getVisibleWorkbenchSettingFields } from "./workbenchSettingsFields";

vi.mock("./workbenchLocalDefaults", () => ({
  WORKBENCH_LOCAL_DEFAULTS: {
    "experiment.pointCloudWebGpuRenderer": true,
    "experiment.webcodecs": false,
    "experiment.videoReferencePredict": "kalman-agile",
  },
}));

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

function visibleLocalValues() {
  return Object.fromEntries(
    getVisibleWorkbenchSettingFields()
      .filter((field) => field.storage === "local")
      .map((field) => [field.key, getFieldValue(DEFAULT_WORKBENCH_PREFERENCES, field)]),
  );
}

it("设置界面和运行时共同采用更改后的本机默认值", () => {
  expect(visibleLocalValues()).toEqual({
    "experiment.pointCloudWebGpuRenderer": true,
    "experiment.webcodecs": false,
    "experiment.videoReferencePredict": "kalman-agile",
  });
  expect(readPointCloudWebGpuExperiment()).toBe(true);
  expect(isWebCodecsExperimentEnabled("", localStorage)).toBe(false);
  const { result } = renderHook(useVideoReferenceConfig);
  expect(result.current).toEqual({ mode: "kalman", preset: "agile" });
});

it("保留本机存储、WebCodecs URL 优先级和能力检测", () => {
  localStorage.setItem(POINT_CLOUD_WEBGPU_STORAGE_KEY, "0");
  localStorage.setItem(WEBCODECS_FLAG_STORAGE_KEY, "1");
  localStorage.setItem(VIDEO_REFERENCE_PREDICT_STORAGE_KEY, "off");
  expect(visibleLocalValues()).toEqual({
    "experiment.pointCloudWebGpuRenderer": false,
    "experiment.webcodecs": true,
    "experiment.videoReferencePredict": "off",
  });
  expect(readPointCloudWebGpuExperiment()).toBe(false);
  expect(isWebCodecsExperimentEnabled("", localStorage)).toBe(true);
  expect(isWebCodecsExperimentEnabled("?webcodecs=0", localStorage)).toBe(false);
  vi.stubGlobal("VideoDecoder", undefined);
  expect(detectWebCodecsSupport()).toBe(false);
});
