import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import { PointCloudScene } from "./PointCloudScene";

describe("PointCloudScene main interaction coordinates", () => {
  it("uses the main panel for ray picking, rectangle and polygon selection on a shared surface", () => {
    const scene = Object.create(PointCloudScene.prototype) as PointCloudScene;
    const camera = new THREE.PerspectiveCamera(60, 2, 0.1, 100);
    camera.position.set(0, 0, 10);
    camera.updateMatrixWorld();
    const geometry = new THREE.BufferGeometry().setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array([0, 0, 0]), 3),
    );
    const raycaster = new THREE.Raycaster();
    const setFromCamera = vi.spyOn(raycaster, "setFromCamera");
    Object.assign(scene, {
      camera,
      container: {
        getBoundingClientRect: () => ({ left: 400, top: 200, width: 400, height: 200 }),
      },
      renderer: {
        domElement: {
          getBoundingClientRect: () => ({ left: 0, top: 0, width: 1600, height: 900 }),
        },
      },
      raycaster,
      boxLayer: new THREE.Group(),
      boxGroups: new Map(),
      pointGeometry: geometry,
      pointRaycastObject: null,
      points: null,
      pointIndexStride: 1,
      sourcePointCount: 1,
      renderedPointCount: 1,
      groundZ: 0,
    });
    scene.pickBox(600, 300);
    expect(setFromCamera).toHaveBeenLastCalledWith(new THREE.Vector2(0, 0), camera);
    scene.pickPoint(600, 300);
    expect(setFromCamera).toHaveBeenLastCalledWith(new THREE.Vector2(0, 0), camera);
    expect(scene.placeOnGround(600, 300)).toEqual([0, 0, 0]);
    expect(scene.selectPointMaskInScreenRect(590, 290, 610, 310)?.pointIndices).toEqual([0]);
    expect(
      scene.selectPointMaskInScreenPolygon([
        { x: 590, y: 290 },
        { x: 610, y: 290 },
        { x: 610, y: 310 },
        { x: 590, y: 310 },
      ])?.pointIndices,
    ).toEqual([0]);
    geometry.dispose();
  });
});
