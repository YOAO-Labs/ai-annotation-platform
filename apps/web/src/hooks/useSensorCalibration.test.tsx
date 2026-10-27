import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  SensorCalibration,
  SensorCalibrationHistoryOut,
  SensorCalibrationRevisionOut,
  TaskPointCloudManifestResponse,
} from "@/api/generated";
import { tasksApi } from "@/api/tasks";
import { pointCloudManifestQueryKey } from "@/api/pointCloudOriginals";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { sensorCalibrationKey, useSensorCalibration } from "./useSensorCalibration";

const calibration: SensorCalibration = {
  intrinsic: [100, 0, 50, 0, 100, 40, 0, 0, 1],
  extrinsic: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
  rect: null,
};

function makeWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  useAuthStore.getState().logout();
});

describe("useSensorCalibration", () => {
  it("history head 和 PATCH 响应会同步当前 manifest 缓存", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const history: SensorCalibrationHistoryOut = {
      current_revision: 2,
      current_digest: "b".repeat(64),
      items: [
        {
          dataset_item_id: "camera-1",
          revision: 2,
          digest: "b".repeat(64),
          calibration,
          created_at: "2026-08-27T08:00:00Z",
        },
      ],
    };
    const manifest: TaskPointCloudManifestResponse = {
      task_id: "task-1",
      point_cloud_url: "/point-cloud.pcd",
      point_cloud_format: "pcd",
      expires_in: 300,
      cameras: [
        {
          dataset_item_id: "camera-1",
          role: "camera_front",
          name: "CAM_FRONT",
          image_url: "/front.jpg",
          calibration: null,
          calibration_revision: 1,
          calibration_digest: "a".repeat(64),
        },
      ],
    };
    const updated: SensorCalibrationRevisionOut = {
      dataset_item_id: "camera-1",
      revision: 3,
      digest: "c".repeat(64),
      calibration: { ...calibration, intrinsic: [110, 0, 50, 0, 100, 40, 0, 0, 1] },
      created_at: "2026-08-27T09:00:00Z",
    };
    queryClient.setQueryData(sensorCalibrationKey("task-1", "camera_front"), history);
    queryClient.setQueryData(pointCloudManifestQueryKey("task-1"), manifest);
    vi.spyOn(tasksApi, "updateSensorCalibration").mockResolvedValue(updated);

    const view = renderHook(
      () =>
        useSensorCalibration({
          taskId: "task-1",
          cameraRole: "camera_front",
          projectId: "project-1",
          enabled: false,
        }),
      { wrapper: makeWrapper(queryClient) },
    );

    await waitFor(() =>
      expect(
        queryClient.getQueryData<TaskPointCloudManifestResponse>(
          pointCloudManifestQueryKey("task-1"),
        )?.cameras[0]?.calibration_revision,
      ).toBe(2),
    );
    const payload = {
      calibration: updated.calibration,
      expected_revision: 2,
      expected_digest: "b".repeat(64),
    };
    const idempotencyKey = "57d0e28b-fcb5-44ea-8fd2-b85fd0fd960a";
    await act(async () => {
      await view.result.current.update.mutateAsync({ payload, idempotencyKey });
    });

    expect(tasksApi.updateSensorCalibration).toHaveBeenCalledWith(
      "task-1",
      "camera_front",
      payload,
      idempotencyKey,
    );

    expect(
      queryClient.getQueryData<SensorCalibrationHistoryOut>(
        sensorCalibrationKey("task-1", "camera_front"),
      ),
    ).toEqual({
      current_revision: 3,
      current_digest: "c".repeat(64),
      items: [updated, ...history.items],
    });
    expect(
      queryClient.getQueryData<TaskPointCloudManifestResponse>(pointCloudManifestQueryKey("task-1"))
        ?.cameras[0],
    ).toEqual(
      expect.objectContaining({ calibration_revision: 3, calibration_digest: updated.digest }),
    );
  });

  it("does not publish a late calibration response into a new session's manifest", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    useAuthStore.getState().setAuth("same-token", { id: "alice" } as MeResponse);
    let complete!: (value: SensorCalibrationRevisionOut) => void;
    vi.spyOn(tasksApi, "updateSensorCalibration").mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const view = renderHook(
      () =>
        useSensorCalibration({
          taskId: "task-1",
          cameraRole: "camera_front",
          projectId: "project-1",
          enabled: false,
        }),
      { wrapper: makeWrapper(queryClient) },
    );
    let pending!: Promise<unknown>;
    act(() => {
      pending = view.result.current.update.mutateAsync({
        payload: {
          calibration,
          expected_revision: 1,
          expected_digest: "a".repeat(64),
        },
        idempotencyKey: crypto.randomUUID(),
      });
    });
    await waitFor(() => expect(complete).toBeTypeOf("function"));
    act(() => {
      useAuthStore.getState().logout();
      useAuthStore.getState().setAuth("same-token", { id: "alice" } as MeResponse);
    });
    const currentManifest = { cameras: [{ role: "camera_front", calibration_revision: 99 }] };
    queryClient.setQueryData(pointCloudManifestQueryKey("task-1"), currentManifest);
    await act(async () => {
      complete({
        dataset_item_id: "camera-1",
        revision: 2,
        digest: "b".repeat(64),
        calibration,
        created_at: "2026-10-24T00:00:00Z",
      });
      await pending;
    });
    expect(queryClient.getQueryData(pointCloudManifestQueryKey("task-1"))).toEqual(currentManifest);
    expect(
      queryClient.getQueryData(sensorCalibrationKey("task-1", "camera_front")),
    ).toBeUndefined();
    view.unmount();
    queryClient.clear();
  });
});
