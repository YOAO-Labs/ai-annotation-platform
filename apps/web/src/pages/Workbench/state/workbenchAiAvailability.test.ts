import { describe, expect, it } from "vitest";
import { buildCapEntry } from "./useBackendRouting";
import {
  resolveWorkbenchAiAvailability,
  resolveWorkbenchAiRunUnavailableReason,
} from "./workbenchAiAvailability";

const base = {
  backends: [],
  capIndex: {},
  backendsLoading: false,
  backendsError: false,
  capabilitiesLoading: false,
  hasTrackers: false,
};

describe("workbench AI execution backend availability", () => {
  const backends = [
    { id: "default", state: "disconnected" },
    { id: "catalog-placement", state: "connected" },
  ];
  const capIndex = { "catalog-placement": { ...buildCapEntry(undefined), reachable: true } };

  it("allows the catalog model's actual placement even when the original backend is offline", () => {
    expect(
      resolveWorkbenchAiRunUnavailableReason({
        backendId: "catalog-placement",
        backends,
        capIndex,
      }),
    ).toBeUndefined();
  });

  it.each(["default", "removed", null])(
    "blocks unavailable execution target %s despite another online backend",
    (backendId) => {
      expect(resolveWorkbenchAiRunUnavailableReason({ backendId, backends, capIndex })).toContain(
        "当前 AI 后端不可用",
      );
    },
  );

  it("keeps the project loading reason while the execution backend is unavailable", () => {
    expect(
      resolveWorkbenchAiRunUnavailableReason({
        backendId: "catalog-placement",
        backends: [],
        capIndex,
        unavailableReason: "正在加载项目 AI 后端",
      }),
    ).toBe("正在加载项目 AI 后端");
  });
});

describe("workbench AI entry availability", () => {
  it("distinguishes loading, list failure, missing configuration and unreachable backends", () => {
    expect(
      resolveWorkbenchAiAvailability({ ...base, backendsLoading: true }).aiUnavailableReason,
    ).toContain("正在加载");
    expect(
      resolveWorkbenchAiAvailability({ ...base, backendsError: true }).aiUnavailableReason,
    ).toContain("加载失败");
    const missing = resolveWorkbenchAiAvailability(base);
    expect(missing.aiUnavailableReason).toContain("尚未接入");
    expect(missing.trackerUnavailableReason).toBe(missing.aiUnavailableReason);
    const backend = { id: "a", state: "connected" };
    expect(
      resolveWorkbenchAiAvailability({ ...base, backends: [backend], capabilitiesLoading: true })
        .aiUnavailableReason,
    ).toContain("正在检查");
    expect(
      resolveWorkbenchAiAvailability({ ...base, backends: [backend] }).aiUnavailableReason,
    ).toContain("连接或能力检查失败");
  });

  it("allows another usable project backend while one backend fails or is still loading", () => {
    const result = resolveWorkbenchAiAvailability({
      ...base,
      backends: [
        { id: "a", state: "disconnected" },
        { id: "b", state: "connected" },
      ],
      capIndex: { b: { ...buildCapEntry(undefined), reachable: true } },
      capabilitiesLoading: true,
      hasTrackers: true,
    });
    expect(result.aiUnavailableReason).toBeUndefined();
    expect(result.trackerUnavailableReason).toBeUndefined();
  });

  it("requires both a connected backend and reachable capabilities; stale capabilities cannot enable it", () => {
    expect(
      resolveWorkbenchAiAvailability({
        ...base,
        backends: [{ id: "a", state: "error" }],
        capIndex: { a: { ...buildCapEntry(undefined), reachable: true } },
        hasTrackers: true,
      }).aiUnavailableReason,
    ).toBeDefined();
    expect(
      resolveWorkbenchAiAvailability({
        ...base,
        capIndex: { removed: { ...buildCapEntry(undefined), reachable: true } },
        hasTrackers: true,
      }).trackerUnavailableReason,
    ).toContain("尚未接入");
  });

  it("keeps single-task configuration available when the connected backend has no trackers", () => {
    const result = resolveWorkbenchAiAvailability({
      ...base,
      backends: [{ id: "a", state: "connected" }],
      capIndex: { a: { ...buildCapEntry(undefined), reachable: true } },
    });
    expect(result.aiUnavailableReason).toBeUndefined();
    expect(result.trackerUnavailableReason).toContain("不支持视频追踪");
  });
});
