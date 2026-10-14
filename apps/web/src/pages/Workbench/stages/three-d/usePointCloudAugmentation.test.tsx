import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  pointCloudAugmentationApi,
  type PointCloudAugmentationManifest,
} from "@/api/pointCloudAugmentation";
import { useAuthStore } from "@/stores/authStore";
import { loadAugmentationAsset } from "./pointCloudAugmentationAssets";
import {
  defaultAugmentationCameraRole,
  usePointCloudAugmentation,
} from "./usePointCloudAugmentation";

vi.mock("@/api/pointCloudAugmentation", () => ({
  pointCloudAugmentationApi: { manifest: vi.fn(), asset: vi.fn() },
}));
vi.mock("./pointCloudAugmentationAssets", async (original) => ({
  ...(await original<typeof import("./pointCloudAugmentationAssets")>()),
  loadAugmentationAsset: vi.fn(),
}));
const load = vi.mocked(loadAugmentationAsset);
const getManifest = vi.mocked(pointCloudAugmentationApi.manifest);
const frame = {
  positions: new Float32Array([1, 2, 3]),
  colors: new Float32Array([1, 0, 0]),
  renderedPoints: 1,
  totalPoints: 1,
};
const ready = (taskId: string, generation = 1): PointCloudAugmentationManifest => ({
  task_id: taskId,
  enabled: true,
  status: "ready",
  reason: null,
  asset_id: "a1",
  generation,
  profile: "omega-current-mono-512",
  input_digest: `input-${taskId}`,
  coordinate_frame: "primary_pointcloud",
  axis_convention: "iso_8855",
  cameras: [
    { role: "front", name: "前方", point_count: 1, byte_size: 100, sha256: "a".repeat(64) },
  ],
});
let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const params = {
  taskId: "t1",
  enabled: true,
  measuredReady: true,
  cameraRole: "front",
  convention: "iso_8855" as const,
  targetCount: 100,
};
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  useAuthStore.setState({ user: { id: "u1" } as never, token: "token-1" });
  localStorage.setItem("token", "token-1");
  getManifest.mockReset().mockImplementation(async (taskId) => ready(taskId));
  load.mockReset().mockResolvedValue(frame);
});
afterEach(() => {
  cleanup();
  client.clear();
  useAuthStore.setState({ user: null, token: null });
  localStorage.clear();
});

describe("augmentation display lifecycle", () => {
  it("revalidates disabled availability on re-entry before reusing completed content", async () => {
    let options = params;
    const hook = renderHook(() => usePointCloudAugmentation(options), { wrapper });
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    options = { ...params, enabled: false };
    hook.rerender();
    await waitFor(() =>
      expect(client.getQueryData(["point-cloud-augmentation", "u1", "t1"])).toBeUndefined(),
    );
    getManifest.mockResolvedValue({ ...ready("t1"), enabled: false, status: "disabled" });
    options = params;
    hook.rerender();
    expect(hook.result.current.frame).toBeNull();
    await waitFor(() => expect(hook.result.current.message).toContain("尚未开启"));
    expect(load).toHaveBeenCalledOnce();
  });
  it("does no availability or asset work when not selected, and waits for measured geometry before decode", async () => {
    let options = { ...params, enabled: false, measuredReady: false };
    const hook = renderHook(() => usePointCloudAugmentation(options), { wrapper });
    expect(getManifest).not.toHaveBeenCalled();
    options = { ...options, enabled: true };
    hook.rerender();
    await waitFor(() => expect(hook.result.current.manifest?.status).toBe("ready"));
    expect(load).not.toHaveBeenCalled();
    options = { ...options, measuredReady: true };
    hook.rerender();
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    expect(load).toHaveBeenCalledOnce();
    options = { ...options, enabled: false };
    hook.rerender();
    expect(hook.result.current.frame).toBeNull();
  });
  it("retires old-frame work immediately and rejects late completion", async () => {
    let finish!: (value: typeof frame) => void;
    load.mockImplementation((manifest) =>
      manifest.task_id === "t1"
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : Promise.resolve(frame),
    );
    let options = params;
    const hook = renderHook(() => usePointCloudAugmentation(options), { wrapper });
    await waitFor(() => expect(load).toHaveBeenCalledOnce());
    const oldSignal = load.mock.calls[0][5];
    options = { ...params, taskId: "t2" };
    hook.rerender();
    expect(hook.result.current.frame).toBeNull();
    expect(oldSignal.aborted).toBe(true);
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    await act(async () => finish({ ...frame, positions: new Float32Array([99, 99, 99]) }));
    expect(hook.result.current.frame).toBe(frame);
  });
  it("does not substitute another camera when an explicitly selected direction is missing", async () => {
    let options = params;
    const hook = renderHook(() => usePointCloudAugmentation(options), { wrapper });
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    options = { ...params, cameraRole: "rear" };
    hook.rerender();
    expect(hook.result.current.frame).toBeNull();
    expect(hook.result.current.message).toContain("所选相机");
    expect(load).toHaveBeenCalledOnce();
  });
  it("clears display on generation change, stale results and revocation", async () => {
    const hook = renderHook(() => usePointCloudAugmentation(params), { wrapper });
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    load.mockReturnValueOnce(new Promise(() => undefined));
    act(() => client.setQueryData(["point-cloud-augmentation", "u1", "t1"], ready("t1", 2)));
    await waitFor(() => expect(hook.result.current.frame).toBeNull());
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    act(() =>
      client.setQueryData(["point-cloud-augmentation", "u1", "t1"], {
        ...ready("t1", 2),
        status: "stale",
      }),
    );
    await waitFor(() => expect(hook.result.current.message).toContain("过期"));
    act(() =>
      client.setQueryData(["point-cloud-augmentation", "u1", "t1"], {
        ...ready("t1", 2),
        enabled: false,
        status: "disabled",
      }),
    );
    await waitFor(() => expect(hook.result.current.message).toContain("尚未开启"));
    expect(hook.result.current.frame).toBeNull();
  });
  it("does not reuse a completed auxiliary cache across account changes", async () => {
    const hook = renderHook(() => usePointCloudAugmentation(params), { wrapper });
    await waitFor(() => expect(hook.result.current.frame).toBe(frame));
    load.mockReturnValueOnce(new Promise(() => undefined));
    act(() => {
      localStorage.setItem("token", "token-2");
      useAuthStore.setState({ user: { id: "u2" } as never, token: "token-2" });
    });
    expect(hook.result.current.frame).toBeNull();
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect(hook.result.current.frame).toBeNull();
  });
  it("initializes a stable front direction without requiring camera selection on each frame", () => {
    expect(
      defaultAugmentationCameraRole([
        { role: "CAM_BACK" },
        { role: "CAM_FRONT" },
        { role: "CAM_FRONT_LEFT" },
      ]),
    ).toBe("CAM_FRONT");
    expect(
      defaultAugmentationCameraRole([
        { role: "camera_CAM_BACK" },
        { role: "camera_CAM_FRONT_LEFT" },
        { role: "camera_CAM_FRONT" },
      ]),
    ).toBe("camera_CAM_FRONT");
    expect(defaultAugmentationCameraRole([{ role: "right" }, { role: "left" }])).toBe("left");
  });
});
