import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import { PointCloudScene } from "./PointCloudScene";

describe("PointCloudScene display style", () => {
  it("updates visual materials in place without changing raycast geometry", () => {
    const scene = Object.create(PointCloudScene.prototype) as PointCloudScene;
    const geometry = new THREE.BufferGeometry().setAttribute(
      "position",
      new THREE.Float32BufferAttribute([0, 0, 0], 3),
    );
    const visualMaterial = new THREE.PointsMaterial({ size: 0.1 });
    const raycastMaterial = new THREE.PointsMaterial({ size: 0.1 });
    const points = new THREE.Points(geometry, visualMaterial);
    const raycastPoints = new THREE.Points(geometry, raycastMaterial);
    const triViewPass = { setDisplayStyle: vi.fn(() => true) };

    Object.assign(scene, {
      displayStyle: "points",
      pointSize: 0.1,
      points,
      pointGeometry: geometry,
      pointRaycastObject: raycastPoints,
      webGpuPointLayer: null,
      neighborPoints: [],
      triViewPass,
      disposed: false,
      renderScheduler: null,
    });

    scene.setDisplayStyle("surface");
    expect(points.geometry).toBe(geometry);
    expect(raycastPoints.geometry).toBe(geometry);
    expect(points.material).toBe(visualMaterial);
    expect(visualMaterial.alphaMap).toBeInstanceOf(THREE.DataTexture);
    expect(visualMaterial.size).toBeCloseTo(0.16);
    expect(raycastMaterial.alphaMap).toBeNull();
    expect(raycastMaterial.size).toBe(0.1);
    expect(triViewPass.setDisplayStyle).toHaveBeenCalledWith("surface");

    scene.setDisplayStyle("surface");
    expect(triViewPass.setDisplayStyle).toHaveBeenCalledOnce();

    visualMaterial.alphaMap?.dispose();
    visualMaterial.dispose();
    raycastMaterial.dispose();
    geometry.dispose();
  });

  it("keeps diagnostic direction colors separate from neutral surface neighbors", () => {
    const scene = Object.create(PointCloudScene.prototype) as PointCloudScene;
    const neighborLayer = new THREE.Group();
    Object.assign(scene, {
      rendererStatus: { actualBackend: "legacy-webgl2" },
      pointSize: 0.1,
      displayStyle: "surface",
      neighborLayer,
      neighborPoints: [],
      disposed: false,
      renderScheduler: null,
    });

    const matrix = new THREE.Matrix4();
    scene.setNeighborPoints([
      {
        positions: new Float32Array([0, 0, 0]),
        matrix,
        dir: "past",
        distance: 1,
        displayMode: "surface",
      },
      {
        positions: new Float32Array([1, 0, 0]),
        matrix,
        dir: "future",
        distance: 1,
        displayMode: "diagnostic",
      },
    ]);

    const layers = (scene as unknown as { neighborPoints: { material: THREE.PointsMaterial }[] })
      .neighborPoints;
    expect(layers).toHaveLength(2);
    expect(layers[0].material.color.getHex()).toBe(0xb8c0cc);
    expect(layers[0].material.opacity).toBe(0.22);
    expect(layers[0].material.alphaMap).toBeInstanceOf(THREE.DataTexture);
    expect(layers[1].material.color.getHex()).toBe(0xd98a4a);
    expect(layers[1].material.opacity).toBe(0.5);

    scene.setNeighborPoints([]);
    expect(neighborLayer.children).toHaveLength(0);
  });
});
