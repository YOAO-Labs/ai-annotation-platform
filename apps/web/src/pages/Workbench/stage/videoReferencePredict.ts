import { useSyncExternalStore } from "react";
import { WORKBENCH_LOCAL_DEFAULTS } from "../state/workbenchLocalDefaults";

export const VIDEO_REFERENCE_PREDICT_STORAGE_KEY = "wb:video:referencePredict";

export type VideoReferenceMode = "off" | "linear" | "kalman";
export type VideoReferencePreset = "stable" | "agile";

export interface VideoReferenceConfig {
  mode: VideoReferenceMode;
  preset: VideoReferencePreset;
}

export type VideoReferenceSetting = "off" | "linear" | "kalman-stable" | "kalman-agile";

const DEFAULT_SETTING: VideoReferenceSetting =
  WORKBENCH_LOCAL_DEFAULTS["experiment.videoReferencePredict"];
const OFF_CONFIG: VideoReferenceConfig = { mode: "off", preset: "stable" };
const DEFAULT_CONFIG = parseSetting(DEFAULT_SETTING);

const listeners = new Set<() => void>();
let cachedConfig: VideoReferenceConfig | null = null;

function readSetting(): VideoReferenceSetting {
  if (typeof window === "undefined") return DEFAULT_SETTING;
  try {
    const raw = window.localStorage.getItem(VIDEO_REFERENCE_PREDICT_STORAGE_KEY);
    if (raw === "1" || raw === "true" || raw === "linear") return "linear";
    if (raw === "kalman-stable" || raw === "kalman-agile" || raw === "off") return raw;
    return DEFAULT_SETTING;
  } catch {
    return DEFAULT_SETTING;
  }
}

function parseSetting(setting: VideoReferenceSetting): VideoReferenceConfig {
  switch (setting) {
    case "linear":
      return { mode: "linear", preset: "stable" };
    case "kalman-stable":
      return { mode: "kalman", preset: "stable" };
    case "kalman-agile":
      return { mode: "kalman", preset: "agile" };
    default:
      return OFF_CONFIG;
  }
}

function readConfig(): VideoReferenceConfig {
  if (!cachedConfig) cachedConfig = parseSetting(readSetting());
  return cachedConfig;
}

export function readVideoReferenceSetting(): VideoReferenceSetting {
  return readSetting();
}

export function writeVideoReferenceSetting(value: VideoReferenceSetting): void {
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(VIDEO_REFERENCE_PREDICT_STORAGE_KEY, value);
    } catch {
      /* local device flag is best-effort */
    }
  }
  cachedConfig = null;
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function useVideoReferenceConfig(): VideoReferenceConfig {
  return useSyncExternalStore(subscribe, readConfig, () => DEFAULT_CONFIG);
}
