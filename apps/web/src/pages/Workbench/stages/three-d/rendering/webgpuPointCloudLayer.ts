import * as THREE from "three";
import { ClippingGroup, PointsNodeMaterial } from "three/webgpu";
import type Node from "three/src/nodes/core/Node.js";
import {
  float,
  instancedBufferAttribute,
  instancedDynamicBufferAttribute,
  length,
  mix,
  smoothstep,
  uniform,
  uv,
  vec3,
} from "three/tsl";

import { NEUTRAL_ADJUST, type ColorAdjust } from "../geometry/colorize";

import {
  createCameraColorAdjustUniforms,
  createCameraTextureColorNode,
  type GpuCameraTextureSample,
} from "./cameraTextureColorNode";
import {
  POINT_CLOUD_SURFACE_SIZE_MULTIPLIER,
  pointCloudDisplaySize,
  type PointCloudDisplayStyle,
} from "./pointCloudDisplayStyle";

const SELECTION_ATTRIBUTE = "pointSelection";

export interface WebGpuPointCloudLayer {
  object: THREE.Object3D;
  updatePointData(positions: Float32Array, colors: Float32Array): boolean;
  setPointCount(count: number): void;
  setPointSize(size: number): void;
  setDisplayStyle(style: PointCloudDisplayStyle): void;
  setClippingPlanes(planes: THREE.Plane[]): void;
  setSelection(indices: readonly number[] | null, pointIndexStride: number): void;
  setCameraColorization(samples: readonly GpuCameraTextureSample[] | null): void;
  setColorAdjust(adjust: ColorAdjust): void;
  dispose(): void;
}

interface WebGpuPointCloudLayerOptions {
  pointSize: number;
  sizeAttenuation: boolean;
  color?: THREE.ColorRepresentation;
  opacity?: number;
  depthWrite?: boolean;
  displayStyle?: PointCloudDisplayStyle;
  selection?: boolean;
  /** WebGPURenderer (含 WebGL2 fallback) 只通过 ClippingGroup 消费局部裁剪面。 */
  clipping?: boolean;
}

function ensureInstancedAttribute(
  geometry: THREE.BufferGeometry,
  name: string,
): THREE.InstancedBufferAttribute {
  const existing = geometry.getAttribute(name);
  if (existing instanceof THREE.InstancedBufferAttribute) return existing;
  const attribute = new THREE.InstancedBufferAttribute(
    existing.array,
    existing.itemSize,
    existing.normalized,
    1,
  );
  if (existing instanceof THREE.BufferAttribute) attribute.setUsage(existing.usage);
  geometry.setAttribute(name, attribute);
  return attribute;
}

function ensureSelectionAttribute(geometry: THREE.BufferGeometry, count: number) {
  const existing = geometry.getAttribute(SELECTION_ATTRIBUTE) as
    | THREE.InstancedBufferAttribute
    | undefined;
  if (existing && existing.count === count) return existing;
  const attribute = new THREE.InstancedBufferAttribute(new Float32Array(count), 1);
  attribute.setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute(SELECTION_ATTRIBUTE, attribute);
  return attribute;
}

/**
 * WebGPU point primitives are fixed to one pixel. A counted Sprite renders one
 * camera-facing quad per input point while keeping the source BufferGeometry as
 * the CPU truth shared by the main and orthographic renderers.
 */
export function createWebGpuPointCloudLayer(
  geometry: THREE.BufferGeometry,
  options: WebGpuPointCloudLayerOptions,
): WebGpuPointCloudLayer {
  const position = ensureInstancedAttribute(geometry, "position");
  const sourceColorAttribute = geometry.getAttribute("color");
  const colorAttribute = sourceColorAttribute
    ? ensureInstancedAttribute(geometry, "color")
    : undefined;
  const selectionAttribute = options.selection
    ? ensureSelectionAttribute(geometry, position.count)
    : null;

  let pointSize = options.pointSize;
  let displayStyle = options.displayStyle ?? "points";
  const sizeNode = uniform(pointSize);
  const displayStyleNode = uniform(displayStyle === "surface" ? 1 : 0);
  const surfaceSizeNode = mix(
    float(1),
    float(POINT_CLOUD_SURFACE_SIZE_MULTIPLIER),
    displayStyleNode,
  );
  const radialDistance = length(uv().mul(2).sub(1));
  const surfaceAlpha = smoothstep(float(0.45), float(1), radialDistance).oneMinus();
  const material = new PointsNodeMaterial({
    size: pointCloudDisplaySize(pointSize, displayStyle),
    sizeAttenuation: options.sizeAttenuation,
    transparent: (options.opacity ?? 1) < 1,
    opacity: options.opacity ?? 1,
    depthWrite: options.depthWrite ?? true,
    alphaToCoverage: true,
  });
  const positionNode = instancedBufferAttribute(position) as unknown as Node<"vec3">;
  material.positionNode = positionNode;
  material.sizeNode = sizeNode.mul(surfaceSizeNode);
  // The style uniform preserves one material and shader topology. In surface
  // mode, the radial alpha is tested before depth writes; points mode remains
  // the original full quad.
  material.opacityNode = mix(float(1), surfaceAlpha, displayStyleNode);
  material.alphaTestNode = displayStyleNode.mul(0.08);

  let baseColorNode: Node<"vec3">;
  let selectionNode: Node<"float"> | null = null;
  if (colorAttribute) {
    baseColorNode = instancedDynamicBufferAttribute(colorAttribute) as unknown as Node<"vec3">;
    selectionNode = selectionAttribute
      ? (instancedDynamicBufferAttribute(selectionAttribute) as unknown as Node<"float">)
      : null;
  } else {
    const color = new THREE.Color(options.color ?? 0xffffff);
    material.color.copy(color);
    baseColorNode = vec3(color.r, color.g, color.b) as unknown as Node<"vec3">;
  }
  const adjustUniforms = createCameraColorAdjustUniforms(NEUTRAL_ADJUST);
  const applySelection = (source: Node<"vec3">) =>
    selectionNode
      ? (mix(source, vec3(1, 0.12, 0.12), selectionNode) as unknown as Node<"vec3">)
      : source;
  const cameraColorNode = createCameraTextureColorNode(positionNode, baseColorNode, adjustUniforms);
  material.colorNode = applySelection(cameraColorNode.node);

  const sprite = new THREE.Sprite(material as unknown as THREE.SpriteMaterial);
  sprite.count = Number.isFinite(geometry.drawRange.count)
    ? Math.min(position.count, geometry.drawRange.count)
    : position.count;
  sprite.frustumCulled = false;
  const clippingGroup = options.clipping ? new ClippingGroup() : null;
  if (clippingGroup) clippingGroup.add(sprite);
  const object = clippingGroup ?? sprite;
  object.userData.pointCloudDisplayStyle = displayStyle;

  return {
    object,
    updatePointData(positions, colors) {
      const pointCount = positions.length / 3;
      if (
        !Number.isInteger(pointCount) ||
        pointCount > position.count ||
        !colorAttribute ||
        colors.length !== positions.length
      ) {
        return false;
      }
      (position.array as Float32Array).set(positions);
      (colorAttribute.array as Float32Array).set(colors);
      position.needsUpdate = true;
      colorAttribute.needsUpdate = true;
      if (selectionAttribute) {
        (selectionAttribute.array as Float32Array).fill(0);
        selectionAttribute.needsUpdate = true;
      }
      sprite.count = pointCount;
      geometry.setDrawRange(0, pointCount);
      return true;
    },
    setPointCount(count) {
      sprite.count = Math.max(0, Math.min(position.count, Math.floor(count)));
      geometry.setDrawRange(0, sprite.count);
    },
    setPointSize(size) {
      pointSize = size;
      sizeNode.value = size;
      material.size = pointCloudDisplaySize(size, displayStyle);
    },
    setDisplayStyle(style) {
      if (style === displayStyle) return;
      displayStyle = style;
      displayStyleNode.value = style === "surface" ? 1 : 0;
      material.size = pointCloudDisplaySize(pointSize, displayStyle);
      object.userData.pointCloudDisplayStyle = displayStyle;
    },
    setClippingPlanes(planes) {
      if (clippingGroup) {
        clippingGroup.clippingPlanes = planes;
      } else {
        material.clippingPlanes = planes;
        material.needsUpdate = true;
      }
    },
    setSelection(indices, pointIndexStride) {
      if (!selectionAttribute) return;
      const values = selectionAttribute.array as Float32Array;
      values.fill(0);
      if (indices && indices.length > 0) {
        const selected = new Set(indices);
        for (let i = 0; i < sprite.count; i += 1) {
          if (selected.has(i * pointIndexStride)) values[i] = 1;
        }
      }
      selectionAttribute.needsUpdate = true;
    },
    setCameraColorization(samples) {
      cameraColorNode.update(samples);
    },
    setColorAdjust(adjust) {
      adjustUniforms.update(adjust);
    },
    dispose() {
      cameraColorNode.dispose();
      material.dispose();
    },
  };
}
