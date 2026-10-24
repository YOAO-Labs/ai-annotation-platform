import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";
import { PointCloudScene } from "./PointCloudScene";

function retainedGpuFrame() {
  const scene = Object.create(PointCloudScene.prototype) as PointCloudScene;
  const geometry = new THREE.BufferGeometry()
    .setAttribute("position", new THREE.BufferAttribute(new Float32Array([1, 2, 3]), 3))
    .setAttribute("color", new THREE.BufferAttribute(new Float32Array([0.1, 0.2, 0.3]), 3));
  const disposeGeometry = vi.spyOn(geometry, "dispose");
  const root = new THREE.Scene();
  const points = new THREE.Object3D();
  root.add(points);
  const raycastMaterial = new THREE.PointsMaterial();
  const disposeRaycast = vi.spyOn(raycastMaterial, "dispose");
  const layer = { dispose: vi.fn(), setPointCount: vi.fn(), setCameraColorization: vi.fn() };
  const triView = { setGeometry: vi.fn(), setCameraTextureColorization: vi.fn() };
  Object.assign(scene, {
    scene: root,
    points,
    pointGeometry: geometry,
    pointRaycastObject: { material: raycastMaterial },
    webGpuPointLayer: layer,
    baseColors: new Float32Array([0.1, 0.2, 0.3]),
    renderedPointCount: 1,
    sourcePointCount: 1,
    pointIndexStride: 2,
    qualityGroundGrid: { visible: true },
    groundZ: 4,
    triViewPass: triView,
    invalidateMain: vi.fn(),
    invalidateTri: vi.fn(),
  });
  return { scene, root, points, layer, geometry, disposeGeometry, disposeRaycast, triView };
}

describe("point-cloud original owner retirement", () => {
  it("destroys retained GPU geometry, positions, colors and raycast buffers exactly once", () => {
    const frame = retainedGpuFrame();
    frame.scene.retirePointCloud();
    frame.scene.retirePointCloud();
    expect(frame.root.children).not.toContain(frame.points);
    expect(frame.disposeGeometry).toHaveBeenCalledTimes(1);
    expect(frame.disposeRaycast).toHaveBeenCalledTimes(1);
    expect(frame.layer.dispose).toHaveBeenCalledTimes(1);
    expect(frame.scene.getPointPositions()).toBeNull();
    expect(frame.scene.getBaseColors()).toBeNull();
    expect(frame.triView.setGeometry).toHaveBeenLastCalledWith(null);
    expect(frame.triView.setCameraTextureColorization).toHaveBeenLastCalledWith(null);
    expect(frame.scene).toMatchObject({
      pointGeometry: null,
      webGpuPointLayer: null,
      pointRaycastObject: null,
      points: null,
      renderedPointCount: 0,
      sourcePointCount: 0,
    });
  });
  it("preserves the ordinary frame-transition GPU allocation reuse contract", () => {
    const frame = retainedGpuFrame();
    frame.scene.clearPointCloud();
    expect(frame.layer.setPointCount).toHaveBeenCalledWith(0);
    expect(frame.layer.dispose).not.toHaveBeenCalled();
    expect(frame.disposeGeometry).not.toHaveBeenCalled();
    expect(frame.scene).toMatchObject({
      pointGeometry: frame.geometry,
      webGpuPointLayer: frame.layer,
    });
    frame.scene.retirePointCloud();
  });
});
