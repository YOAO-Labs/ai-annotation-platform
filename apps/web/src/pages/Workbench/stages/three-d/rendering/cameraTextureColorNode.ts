import * as THREE from "three";
import type Node from "three/src/nodes/core/Node.js";
import type UniformNode from "three/src/nodes/core/UniformNode.js";
import { clamp, float, mat3, mat4, texture, uniform, vec2, vec3, vec4 } from "three/tsl";

import type { SensorCalibration } from "@/types";

import type { ColorAdjust } from "../geometry/colorize";

export interface GpuCameraTextureSample {
  texture: THREE.Texture;
  depthTexture: THREE.DataTexture;
  calibration: SensorCalibration;
  width: number;
  height: number;
}

export interface CameraColorAdjustUniforms {
  contrast: UniformNode<"float", number>;
  brightness: UniformNode<"float", number>;
  inverseGamma: UniformNode<"float", number>;
  update(adjust: ColorAdjust): void;
}

export interface CameraTextureColorNodeState {
  node: Node<"vec3">;
  update(samples: readonly GpuCameraTextureSample[] | null): void;
  dispose(): void;
}

interface CameraTextureSlot {
  colorTextureNode: Node<"vec4"> & { value: THREE.Texture };
  depthTextureNode: Node<"vec4"> & { value: THREE.Texture };
  extrinsic: UniformNode<"mat4", THREE.Matrix4>;
  rect: UniformNode<"mat4", THREE.Matrix4>;
  intrinsic: UniformNode<"mat3", THREE.Matrix3>;
  width: UniformNode<"float", number>;
  height: UniformNode<"float", number>;
  enabled: UniformNode<"float", number>;
}

export function createCameraColorAdjustUniforms(adjust: ColorAdjust): CameraColorAdjustUniforms {
  const uniforms = {
    contrast: uniform(adjust.contrast),
    brightness: uniform(adjust.brightness),
    inverseGamma: uniform(1 / adjust.gamma),
    update(next: ColorAdjust) {
      uniforms.contrast.value = next.contrast;
      uniforms.brightness.value = next.brightness;
      uniforms.inverseGamma.value = 1 / next.gamma;
    },
  };
  return uniforms;
}

function matrix4FromRowMajor(values: readonly number[]): THREE.Matrix4 {
  return new THREE.Matrix4().set(
    values[0],
    values[1],
    values[2],
    values[3],
    values[4],
    values[5],
    values[6],
    values[7],
    values[8],
    values[9],
    values[10],
    values[11],
    values[12],
    values[13],
    values[14],
    values[15],
  );
}

function matrix3FromRowMajor(values: readonly number[]): THREE.Matrix3 {
  return new THREE.Matrix3().set(
    values[0],
    values[1],
    values[2],
    values[3],
    values[4],
    values[5],
    values[6],
    values[7],
    values[8],
  );
}

export function createCameraTextureColorNode(
  positionNode: Node<"vec3">,
  baseColorNode: Node<"vec3">,
  adjust: CameraColorAdjustUniforms,
): CameraTextureColorNodeState {
  const emptyColorTexture = new THREE.DataTexture(
    new Uint8Array([0, 0, 0, 255]),
    1,
    1,
    THREE.RGBAFormat,
  );
  emptyColorTexture.needsUpdate = true;
  const emptyDepthTexture = new THREE.DataTexture(
    new Float32Array([0]),
    1,
    1,
    THREE.RedFormat,
    THREE.FloatType,
  );
  emptyDepthTexture.needsUpdate = true;
  const colorizationEnabled = uniform(0);
  const slots: CameraTextureSlot[] = [];
  let bestColor = baseColorNode;
  let bestScore = float(1e9) as unknown as Node<"float">;

  for (let index = 0; index < 6; index += 1) {
    const extrinsic = uniform(new THREE.Matrix4());
    const rect = uniform(new THREE.Matrix4());
    const intrinsic = uniform(new THREE.Matrix3());
    const width = uniform(1);
    const height = uniform(1);
    const enabled = uniform(0);
    const cameraPoint = mat4(rect).mul(mat4(extrinsic).mul(vec4(positionNode, 1)));
    const projected = mat3(intrinsic).mul(cameraPoint.xyz);
    const depth = projected.z;
    const pixel = projected.xy.div(depth);
    const normalizedUv = vec2(pixel.x.div(width), float(1).sub(pixel.y.div(height)));
    const slot: CameraTextureSlot = {
      colorTextureNode: texture(
        emptyColorTexture,
        normalizedUv,
      ) as unknown as CameraTextureSlot["colorTextureNode"],
      depthTextureNode: texture(
        emptyDepthTexture,
        normalizedUv,
      ) as unknown as CameraTextureSlot["depthTextureNode"],
      extrinsic,
      rect,
      intrinsic,
      width,
      height,
      enabled,
    };
    const rasterDepth = slot.depthTextureNode.r;
    const inside = depth
      .greaterThan(0)
      .and(pixel.x.greaterThanEqual(0))
      .and(pixel.x.lessThan(slot.width))
      .and(pixel.y.greaterThanEqual(0))
      .and(pixel.y.lessThan(slot.height));
    const visible = inside
      .and(depth.sub(rasterDepth).lessThanEqual(0.1))
      .and(slot.enabled.greaterThan(0.5));
    const centered = pixel
      .sub(vec2(slot.width.div(2), slot.height.div(2)))
      .div(vec2(slot.width, slot.height));
    const score = centered.dot(centered);
    const better = visible.and(score.lessThan(bestScore));
    bestColor = better.select(slot.colorTextureNode.rgb, bestColor);
    bestScore = better.select(score, bestScore);
    slots.push(slot);
  }

  const clampedColor = clamp(bestColor, 0, 1) as unknown as Node<"vec3">;
  const gammaAdjusted = clampedColor.pow(vec3(adjust.inverseGamma));
  const adjustedColor = clamp(
    gammaAdjusted.sub(0.5).mul(adjust.contrast).add(0.5).add(adjust.brightness),
    0,
    1,
  ) as unknown as Node<"vec3">;
  const node = colorizationEnabled
    .greaterThan(0.5)
    .select(adjustedColor, baseColorNode) as unknown as Node<"vec3">;

  const updateSlot = (slot: CameraTextureSlot, sample?: GpuCameraTextureSample) => {
    slot.colorTextureNode.value = sample?.texture ?? emptyColorTexture;
    slot.depthTextureNode.value = sample?.depthTexture ?? emptyDepthTexture;
    slot.extrinsic.value.copy(
      sample ? matrix4FromRowMajor(sample.calibration.extrinsic) : new THREE.Matrix4(),
    );
    slot.rect.value.copy(
      sample?.calibration.rect ? matrix4FromRowMajor(sample.calibration.rect) : new THREE.Matrix4(),
    );
    slot.intrinsic.value.copy(
      sample ? matrix3FromRowMajor(sample.calibration.intrinsic) : new THREE.Matrix3(),
    );
    slot.width.value = sample?.width ?? 1;
    slot.height.value = sample?.height ?? 1;
    slot.enabled.value = sample ? 1 : 0;
  };

  return {
    node,
    update(samples) {
      if (samples && samples.length > slots.length) {
        throw new Error(`WebGPU camera colorization supports at most ${slots.length} cameras`);
      }
      colorizationEnabled.value = samples && samples.length > 0 ? 1 : 0;
      slots.forEach((slot, index) => updateSlot(slot, samples?.[index]));
    },
    dispose() {
      emptyColorTexture.dispose();
      emptyDepthTexture.dispose();
    },
  };
}
