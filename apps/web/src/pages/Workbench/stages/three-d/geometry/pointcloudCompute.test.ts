import { describe, expect, it, vi } from "vitest";

import type { SensorCalibration } from "@/types";
import type { CameraSample } from "./colorize";
import { colorizePointsAsync, colorizePointsOnMainThread } from "./pointcloudCompute";
import { PointCloudComputeSession } from "./pointCloudComputeSession";

const calib: SensorCalibration = {
  extrinsic: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
  intrinsic: [10, 0, 1, 0, 10, 1, 0, 0, 1],
};

function sample(): CameraSample {
  return {
    calib,
    width: 3,
    height: 3,
    data: new Uint8ClampedArray([
      255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 255, 255, 0, 255, 20, 40, 60, 255, 0, 255,
      255, 255, 255, 0, 255, 255, 128, 128, 128, 255, 255, 255, 255, 255,
    ]),
  };
}

describe("colorizePointsAsync", () => {
  it("falls back to the synchronous implementation when workers are unavailable", async () => {
    const positions = new Float32Array([0, 0, 1, 0.1, 0, 1]);
    const base = new Float32Array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]);

    const out = await colorizePointsAsync(positions, base, [sample()], { createWorker: null });
    const expected = colorizePointsOnMainThread(positions, base, [sample()]);

    expect(Array.from(out)).toEqual(Array.from(expected));
  });

  it("falls back when worker construction fails", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const positions = new Float32Array([0, 0, 1]);
    const out = await colorizePointsAsync(positions, null, [sample()], {
      createWorker: () => {
        throw new Error("blocked");
      },
    });
    const expected = colorizePointsOnMainThread(positions, null, [sample()]);

    expect(Array.from(out)).toEqual(Array.from(expected));
    expect(warn).toHaveBeenCalledWith(
      "[pointcloud-worker] fallback to main thread",
      expect.any(Error),
    );
    warn.mockRestore();
  });

  it("切帧取消时终止过期 worker 并拒绝旧上色结果", async () => {
    const controller = new AbortController();
    const worker = {
      postMessage: vi.fn(),
      terminate: vi.fn(),
      onmessage: null,
      onerror: null,
    } as unknown as Worker;
    const pending = colorizePointsAsync(new Float32Array([0, 0, 1]), null, [sample()], {
      createWorker: () => worker,
      timeoutMs: 20,
      signal: controller.signal,
    });

    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(worker.terminate).toHaveBeenCalledTimes(1);
  });
});

describe("PointCloudComputeSession", () => {
  it("retires auxiliary decode without terminating the shared measured-frame worker", async () => {
    const worker = {
      postMessage: vi.fn(),
      terminate: vi.fn(),
      onmessage: null,
      onerror: null,
    } as unknown as Worker;
    const factory = vi.fn(() => worker);
    const session = new PointCloudComputeSession(factory);
    const controller = new AbortController();
    const pending = session.decodeAuxiliaryPcd(
      new Uint8Array([1]).buffer,
      "iso_8855",
      100,
      controller.signal,
    );
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(worker.terminate).not.toHaveBeenCalled();
    expect(session.getDiagnostics().pendingRequests).toBe(0);
    session.dispose();
  });
  it("reuses one worker across requests until the workbench session is disposed", async () => {
    const factory = vi.fn();
    const worker = {
      postMessage: vi.fn((request: { reqId: number; kind: string }) => {
        queueMicrotask(() => {
          worker.onmessage?.({
            data: {
              reqId: request.reqId,
              ok: true,
              kind: request.kind,
              colors: new Float32Array([0.2, 0.3, 0.4]),
            },
          } as MessageEvent);
        });
      }),
      terminate: vi.fn(),
      onmessage: null as ((event: MessageEvent) => void) | null,
      onerror: null,
    };
    factory.mockReturnValue(worker as unknown as Worker);
    const session = new PointCloudComputeSession(factory);

    await session.colorize(new Float32Array([0, 0, 1]), null, [sample()]);
    await session.colorize(new Float32Array([0, 0, 1]), null, [sample()]);

    expect(factory).toHaveBeenCalledTimes(1);
    expect(worker.postMessage).toHaveBeenCalledTimes(2);
    expect(worker.terminate).not.toHaveBeenCalled();
    session.dispose();
    expect(worker.terminate).toHaveBeenCalledTimes(1);
  });
});
