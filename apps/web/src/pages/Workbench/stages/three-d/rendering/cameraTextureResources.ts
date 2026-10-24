import * as THREE from "three";

import type { SensorCalibration } from "@/types";

import {
  acquireCameraBitmap,
  loadPointCloudDepthRasters,
  type AcquiredCameraBitmap,
} from "../pointCloudAssetCache";
import { markPointCloudStage } from "../pointCloudTiming";

import type { GpuCameraTextureSample } from "./cameraTextureColorNode";

export interface CameraTextureInput {
  imageUrl: string | null | undefined;
  calibration: SensorCalibration;
}

export interface CameraTextureResources {
  samples: GpuCameraTextureSample[];
  dispose(): void;
}

function createImageTexture(bitmap: ImageBitmap): THREE.Texture {
  const texture = new THREE.Texture(bitmap);
  texture.colorSpace = THREE.NoColorSpace;
  texture.flipY = true;
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

function createDepthTexture(depth: Float32Array, width: number, height: number): THREE.DataTexture {
  const texture = new THREE.DataTexture(depth, width, height, THREE.RedFormat, THREE.FloatType);
  texture.flipY = true;
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  texture.generateMipmaps = false;
  texture.needsUpdate = true;
  return texture;
}

/** Decode camera images once and prepare textures without any Canvas pixel readback. */
export async function prepareCameraTextureResources(
  pointCloudUrl: string,
  positions: Float32Array,
  cameras: readonly CameraTextureInput[],
  signal?: AbortSignal,
): Promise<CameraTextureResources> {
  if (cameras.length > 6) {
    throw new Error("WebGPU camera colorization supports at most 6 cameras");
  }
  const bitmapStartedAt = performance.now();
  const acquired = (
    await Promise.all(
      cameras.map(async (camera) => {
        try {
          if (!camera.imageUrl) return null;
          const handle = await acquireCameraBitmap(camera.imageUrl, signal, "active");
          return { camera, handle };
        } catch (error) {
          if (signal?.aborted || (error instanceof DOMException && error.name === "AbortError")) {
            return null;
          }
          console.warn("[pointcloud-camera-texture] skipped camera", camera.imageUrl, error);
          return null;
        }
      }),
    )
  ).filter(
    (
      entry,
    ): entry is {
      camera: CameraTextureInput;
      handle: AcquiredCameraBitmap;
    } => entry !== null,
  );
  const bitmapsReady = acquired.every(({ handle }) => handle.cacheReady);
  markPointCloudStage("camera-bitmaps-ready", pointCloudUrl, bitmapStartedAt, bitmapsReady);

  const releaseAll = () => acquired.forEach(({ handle }) => handle.release());
  if (signal?.aborted) {
    releaseAll();
    throw new DOMException("Camera texture preparation aborted", "AbortError");
  }
  if (acquired.length === 0) return { samples: [], dispose: releaseAll };

  try {
    const depthStartedAt = performance.now();
    const loaded = await loadPointCloudDepthRasters(
      pointCloudUrl,
      positions,
      acquired.map(({ camera, handle }) => ({
        calibration: camera.calibration,
        width: handle.bitmap.width,
        height: handle.bitmap.height,
      })),
      { signal, lane: "active" },
    );
    markPointCloudStage("camera-depth-ready", pointCloudUrl, depthStartedAt, loaded.cacheHit);
    if (signal?.aborted) throw new DOMException("Camera texture preparation aborted", "AbortError");
    const textureStartedAt = performance.now();
    const samples = acquired.map(({ camera, handle }, index) => ({
      texture: createImageTexture(handle.bitmap),
      depthTexture: createDepthTexture(
        loaded.rasters[index].depth,
        loaded.rasters[index].cols,
        loaded.rasters[index].rows,
      ),
      calibration: camera.calibration,
      width: handle.bitmap.width,
      height: handle.bitmap.height,
    }));
    markPointCloudStage("camera-textures-ready", pointCloudUrl, textureStartedAt);
    let disposed = false;
    return {
      samples,
      dispose() {
        if (disposed) return;
        disposed = true;
        for (const sample of samples) {
          sample.texture.dispose();
          sample.depthTexture.dispose();
          // Three's WebGPU backend may retain the disposed Texture wrapper until
          // its render-object cache is pruned. Release the CPU raster immediately.
          sample.depthTexture.image.data = null;
        }
        releaseAll();
      },
    };
  } catch (error) {
    releaseAll();
    throw error;
  }
}
