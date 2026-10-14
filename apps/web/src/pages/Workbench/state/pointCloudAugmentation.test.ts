import { describe, expect, it } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import {
  buildPointCloudAugmentationPatch,
  normalizePointCloudPreferencePatch,
  resolvePointCloudAugmentation,
} from "./pointCloudAugmentation";

describe("point-cloud augmentation intent", () => {
  const defaults = DEFAULT_WORKBENCH_PREFERENCES.pointcloud;
  it("preserves legacy surface geometry without migrating or mutating saved settings", () => {
    const saved = {
      ...defaults,
      displayStyle: "surface" as const,
      neighborPointOverlayK: 3 as const,
    };
    expect(resolvePointCloudAugmentation(saved)).toEqual({
      source: "neighbors",
      legacySurface: true,
      neighborPlan: { frameCount: 1, displayMode: "surface" },
    });
    expect(saved.augmentationSource).toBeNull();
    expect(
      resolvePointCloudAugmentation({ ...saved, neighborPointOverlay: true }).neighborPlan,
    ).toEqual({ frameCount: 3, displayMode: "diagnostic" });
  });
  it.each(["none", "vggt_omega"] as const)(
    "explicit %s never enables neighbors for surface appearance or an old boolean",
    (source) => {
      expect(
        resolvePointCloudAugmentation({
          ...defaults,
          augmentationSource: source,
          displayStyle: "surface",
          neighborPointOverlay: true,
        }).neighborPlan.frameCount,
      ).toBe(0);
    },
  );
  it("explicit neighbors use the preserved radius even if the compatibility boolean is absent", () => {
    expect(
      resolvePointCloudAugmentation({
        ...defaults,
        augmentationSource: "neighbors",
        neighborPointOverlay: false,
        neighborPointOverlayK: 2,
      }).neighborPlan,
    ).toEqual({ frameCount: 2, displayMode: "diagnostic" });
  });
  it("compound writes preserve appearance, radius and motion settings", () => {
    expect(buildPointCloudAugmentationPatch("vggt_omega")).toEqual({
      pointcloud: { augmentationSource: "vggt_omega", neighborPointOverlay: false },
    });
    expect(buildPointCloudAugmentationPatch("neighbors")).toEqual({
      pointcloud: { augmentationSource: "neighbors", neighborPointOverlay: true },
    });
    const previous = {
      ...defaults,
      augmentationSource: "vggt_omega" as const,
      neighborPointOverlayK: 3 as const,
      neighborPointCull: "align" as const,
    };
    const next = {
      ...previous,
      ...normalizePointCloudPreferencePatch({ neighborPointOverlay: true }),
    };
    expect(next).toMatchObject({
      augmentationSource: null,
      neighborPointOverlay: true,
      neighborPointOverlayK: 3,
      neighborPointCull: "align",
    });
  });
  it("an explicit source wins over contradictory legacy flags; unrelated patches preserve intent", () => {
    expect(
      normalizePointCloudPreferencePatch({
        augmentationSource: "none",
        neighborPointOverlay: true,
      }),
    ).toEqual({ augmentationSource: "none", neighborPointOverlay: false });
    expect(normalizePointCloudPreferencePatch({ pointSize: 0.08 })).toEqual({ pointSize: 0.08 });
  });
});
