import * as THREE from "three";
import { describe, expect, it, vi } from "vitest";

import {
  applyLegacyPointCloudDisplayStyle,
  disposeLegacyPointCloudDisplayStyle,
  pointCloudDisplaySize,
} from "./pointCloudDisplayStyle";

describe("pointCloudDisplayStyle", () => {
  it("applies and removes an alpha-tested surface profile without changing depth policy", () => {
    const material = new THREE.PointsMaterial({
      transparent: true,
      opacity: 0.3,
      depthWrite: false,
    });

    applyLegacyPointCloudDisplayStyle(material, "surface");
    const profile = material.alphaMap;
    expect(profile).toBeInstanceOf(THREE.DataTexture);
    expect(material.alphaTest).toBeGreaterThan(0);
    expect(material.alphaToCoverage).toBe(true);
    expect(material.transparent).toBe(true);
    expect(material.opacity).toBe(0.3);
    expect(material.depthWrite).toBe(false);
    expect(pointCloudDisplaySize(2, "surface")).toBe(3.2);

    applyLegacyPointCloudDisplayStyle(material, "points");
    expect(material.alphaMap).toBeNull();
    expect(material.alphaTest).toBe(0);
    expect(material.alphaToCoverage).toBe(false);
    expect(pointCloudDisplaySize(2, "points")).toBe(2);

    const dispose = vi.spyOn(profile!, "dispose");
    disposeLegacyPointCloudDisplayStyle(material);
    expect(dispose).toHaveBeenCalledOnce();
    material.dispose();
  });
});
