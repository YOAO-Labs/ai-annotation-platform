import type { SensorCalibration } from "@/types";

import { colorizePoints, type CameraSample } from "./colorize";
import { buildDepthRaster, buildGpuDepthRaster } from "./depthmap";
import { decodePointCloudFrame, decodeAuxiliaryPointCloudFrame } from "./pointcloudFrame";
import type { LidarAxisConvention } from "./axisConvention";

type PointcloudWorkerRequest =
  | {
      reqId: number;
      kind: "decode_pcd";
      buffer: ArrayBuffer;
      convention: LidarAxisConvention;
      decimateThreshold: number;
    }
  | {
      reqId: number;
      kind: "decode_auxiliary_pcd";
      buffer: ArrayBuffer;
      convention: LidarAxisConvention;
      decimateThreshold: number;
    }
  | {
      reqId: number;
      kind: "colorize";
      positions: Float32Array;
      baseColors: Float32Array | null;
      samples: CameraSample[];
    }
  | {
      reqId: number;
      kind: "build_depth_rasters";
      positions: Float32Array;
      cameras: Array<{ calib: SensorCalibration; width: number; height: number }>;
    };

type WorkerScope = {
  onmessage: ((event: MessageEvent<PointcloudWorkerRequest>) => void) | null;
  postMessage: (message: unknown, transfer?: Transferable[]) => void;
};

const ctx = self as unknown as WorkerScope;

ctx.onmessage = (event: MessageEvent<PointcloudWorkerRequest>) => {
  const message = event.data;
  try {
    if (message.kind === "decode_auxiliary_pcd") {
      const frame = decodeAuxiliaryPointCloudFrame(
        message.buffer,
        message.convention,
        message.decimateThreshold,
      );
      ctx.postMessage({ reqId: message.reqId, ok: true, kind: message.kind, frame }, [
        frame.positions.buffer,
        frame.colors.buffer,
      ]);
      return;
    }
    if (message.kind === "decode_pcd") {
      const frame = decodePointCloudFrame(
        message.buffer,
        message.convention,
        message.decimateThreshold,
      );
      ctx.postMessage({ reqId: message.reqId, ok: true, kind: message.kind, frame }, [
        frame.positions.buffer,
        frame.heightColors.buffer,
      ]);
      return;
    }
    if (message.kind === "build_depth_rasters") {
      const rasters = message.cameras.map((camera) =>
        buildGpuDepthRaster(message.positions, camera.calib, camera.width, camera.height),
      );
      ctx.postMessage({ reqId: message.reqId, ok: true, kind: message.kind, rasters }, [
        ...rasters.map((raster) => raster.depth.buffer),
      ]);
      return;
    }
    const rasters = message.samples.map((sample) =>
      buildDepthRaster(message.positions, sample.calib, sample.width, sample.height),
    );
    const colors = colorizePoints(message.positions, message.baseColors, message.samples, rasters);
    ctx.postMessage({ reqId: message.reqId, ok: true, kind: message.kind, colors }, [
      colors.buffer,
    ]);
  } catch (error) {
    ctx.postMessage({
      reqId: message.reqId,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
};
