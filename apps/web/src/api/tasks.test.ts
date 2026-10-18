import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SensorCalibrationUpdate } from "./generated";

const { patch } = vi.hoisted(() => ({ patch: vi.fn() }));

vi.mock("./client", () => ({ apiClient: { patch } }));

import { tasksApi } from "./tasks";

beforeEach(() => patch.mockClear());

describe("tasksApi spatial mutation intents", () => {
  it("sends calibration idempotency keys as headers", () => {
    const payload: SensorCalibrationUpdate = {
      calibration: {
        extrinsic: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        intrinsic: [1, 1, 1, 1, 1, 1, 1, 1, 1],
        rect: null,
      },
      expected_revision: 1,
      expected_digest: "a".repeat(64),
    };
    const key = "d49be709-2ed6-4db3-8ca4-12c657cce21c";

    tasksApi.updateSensorCalibration("task-1", "camera/front", payload, key);

    expect(patch).toHaveBeenCalledWith(
      "/tasks/task-1/point-cloud/cameras/camera%2Ffront/calibration",
      payload,
      { headers: { "Idempotency-Key": key } },
    );
  });
});
