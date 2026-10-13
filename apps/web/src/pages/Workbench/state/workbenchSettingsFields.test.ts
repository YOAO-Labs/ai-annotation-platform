// v0.15.3 · 字段注册表完整性:key 与 category 一致、默认值可解析、patch 构造正确。
import { describe, expect, it } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import {
  WORKBENCH_SETTING_CATEGORY_LABELS,
  WORKBENCH_SETTING_FIELDS,
  WORKBENCH_SETTING_SECTIONS,
  WORKBENCH_SETTING_GROUPS,
  groupWorkbenchSettings,
  getVisibleWorkbenchSettingFields,
  filterWorkbenchSettings,
  buildFieldPatch,
  getFieldValue,
  lockableFieldName,
} from "./workbenchSettingsFields";

describe("workbenchSettingsFields 注册表", () => {
  it("每个字段 key 前缀与 category 一致,且分类有展示标签", () => {
    for (const field of WORKBENCH_SETTING_FIELDS) {
      expect(field.key.startsWith(`${field.category}.`)).toBe(true);
      expect(WORKBENCH_SETTING_CATEGORY_LABELS[field.category]).toBeTruthy();
    }
  });

  it("每个字段都能从 DEFAULT_WORKBENCH_PREFERENCES 解析出默认值(注册表与类型不漂移)", () => {
    for (const field of WORKBENCH_SETTING_FIELDS) {
      const value = getFieldValue(DEFAULT_WORKBENCH_PREFERENCES, field);
      expect(value).not.toBeUndefined();
    }
  });

  it("透明度设置明确区分通用填充与重复 AI 候选，不再暴露独立 Mask 控件", () => {
    expect(
      WORKBENCH_SETTING_FIELDS.find((field) => field.key === "image.fadedOpacity")?.label,
    ).toBe("重复 AI 候选透明度");
    expect(
      WORKBENCH_SETTING_FIELDS.find((field) => field.key === "image.maskOverlayOpacity"),
    ).toBeUndefined();
  });

  it("评论提示说明覆盖图片和视频画布", () => {
    expect(
      WORKBENCH_SETTING_FIELDS.find((field) => field.key === "common.showAnnotationComments")
        ?.description,
    ).toContain("图片和视频画布");
  });

  it("注册表默认值与现状一致", () => {
    expect(WORKBENCH_SETTING_FIELDS).toHaveLength(49);
    const byKey = Object.fromEntries(
      WORKBENCH_SETTING_FIELDS.map((f) => [f.key, getFieldValue(DEFAULT_WORKBENCH_PREFERENCES, f)]),
    );
    expect(byKey).toEqual({
      "common.leftWidthPct": 15,
      "common.rightWidthPct": 15,
      "common.longTaskSampleRate": 0.05,
      "common.confirmDelete": "never",
      "common.recentClassesLimit": 5,
      "common.petEnabled": true,
      "common.focusSelectionEnabled": false,
      "common.autoAdvanceOnDecide": true,
      "common.showAnnotationComments": true,
      "common.crossFrameOverlayEnabled": false,
      "common.crossFrameOverlayK": 1,
      "common.crossFrameOverlayScope": "selected",
      "common.performanceTier": "standard",
      "common.labelFontSize": 12,
      "common.labelVisibility": "always",
      "common.labelContent": { single: [], track: ["id", "state"], ai: ["source", "score"] },
      "common.strokeWidth": 1.5,
      "common.fillOpacity": 0.07,
      "common.fillOpacitySelected": 0.12,
      "image.smoothImage": true,
      "image.cssImageFilter": "",
      "image.controlPointsSize": 6,
      "image.autoFitOnResize": true,
      "image.snapToGrid": false,
      "image.afterBoxCreate": "pick_class",
      "image.snapThresholdPx": 8,
      "image.zoomStepFactor": 1.1,
      "image.fadedOpacity": 0.35,
      "video.defaultPlaybackRate": 1,
      "video.largeFrameStep": 10,
      "video.autoFitOnResize": true,
      "video.trackContinueAutoAdvance": false,
      "pointcloud.pointSize": 0.06,
      "pointcloud.persistCameraView": false,
      "pointcloud.colorizeWithCamera": false,
      "pointcloud.colorizeContrast": 1,
      "pointcloud.colorizeBrightness": 0,
      "pointcloud.colorizeGamma": 1,
      "pointcloud.showDepthHint": false,
      "pointcloud.pointMaskSelectMode": "rect",
      "pointcloud.showGrid": true,
      "pointcloud.showAxisGizmo": true,
      "pointcloud.cameraDamping": 0.1,
      "pointcloud.neighborPointOverlay": false,
      "pointcloud.neighborPointOverlayK": 1,
      "pointcloud.neighborPointCull": "keep",
      "experiment.pointCloudWebGpuRenderer": false,
      "experiment.webcodecs": true,
      "experiment.videoReferencePredict": "off",
    });
  });

  it("buildFieldPatch 产出子树级 patch", () => {
    const field = WORKBENCH_SETTING_FIELDS.find((f) => f.key === "image.controlPointsSize")!;
    expect(buildFieldPatch(field, 12)).toEqual({ image: { controlPointsSize: 12 } });
    const common = WORKBENCH_SETTING_FIELDS.find((f) => f.key === "common.longTaskSampleRate")!;
    expect(buildFieldPatch(common, 0.2)).toEqual({ common: { longTaskSampleRate: 0.2 } });
    const pointcloud = WORKBENCH_SETTING_FIELDS.find((f) => f.key === "pointcloud.showGrid")!;
    expect(buildFieldPatch(pointcloud, false)).toEqual({ pointcloud: { showGrid: false } });
    const video = WORKBENCH_SETTING_FIELDS.find((f) => f.key === "video.largeFrameStep")!;
    expect(buildFieldPatch(video, "grid")).toEqual({ video: { largeFrameStep: "grid" } });
  });

  it("local fields do not build preference patches", () => {
    const field = WORKBENCH_SETTING_FIELDS.find((f) => f.key === "experiment.webcodecs")!;
    expect(() => buildFieldPatch(field, true)).toThrow(/Local workbench setting/);
  });

  it("lockableFieldName:image 4 个可锁字段映射平铺名,其余 null", () => {
    const names = WORKBENCH_SETTING_FIELDS.map((f) => lockableFieldName(f));
    expect(names.filter(Boolean).sort()).toEqual([
      "controlPointsSize",
      "cssImageFilter",
      "smoothImage",
      "snapToGrid",
    ]);
    const common = WORKBENCH_SETTING_FIELDS.find((f) => f.category === "common")!;
    expect(lockableFieldName(common)).toBeNull();
  });
});

describe("settings presentation", () => {
  it("every registered field has a section and children stay with their parent", () => {
    for (const field of WORKBENCH_SETTING_FIELDS) {
      expect(field.section && WORKBENCH_SETTING_SECTIONS[field.section]).toBeTruthy();
      if (field.parentKey)
        expect(
          WORKBENCH_SETTING_FIELDS.find((parent) => parent.key === field.parentKey)?.section,
        ).toBe(field.section);
    }
  });
  it("all settings share the field-backed purpose groups without missing or duplicating fields", () => {
    const fields = getVisibleWorkbenchSettingFields();
    expect(fields).toHaveLength(48);
    expect(fields.some((field) => field.hidden)).toBe(false);
    const groups = groupWorkbenchSettings(fields);
    // notifications 分组由共享通知偏好面板渲染，不承载偏好字段
    expect(groups.map((group) => group.key)).toEqual(
      Object.keys(WORKBENCH_SETTING_GROUPS).filter((key) => key !== "notifications"),
    );
    const groupedKeys = groups.flatMap((group) =>
      group.sections.flatMap((section) => section.fields.map((field) => field.key)),
    );
    expect(groupedKeys.sort()).toEqual(fields.map((field) => field.key).sort());
    expect(new Set(groupedKeys).size).toBe(48);
    const canvas = groups.find((group) => group.key === "canvas")!;
    expect(
      new Set(canvas.sections.flatMap((section) => section.fields.map((field) => field.category))),
    ).toEqual(new Set(["image", "video", "pointcloud"]));
    expect(filterWorkbenchSettings(fields, "  ")).toEqual(fields);
    expect(filterWorkbenchSettings(fields, "画布与视角")).toEqual(
      fields.filter((field) => WORKBENCH_SETTING_SECTIONS[field.section].group === "canvas"),
    );
  });
  it("search matches options and case-insensitive words without bringing back hidden fields", () => {
    const fields = getVisibleWorkbenchSettingFields();
    expect(filterWorkbenchSettings(fields, "  GAMMA 上色 ").map((field) => field.key)).toEqual([
      "pointcloud.colorizeWithCamera",
      "pointcloud.colorizeGamma",
    ]);
    expect(filterWorkbenchSettings(fields, "矩形").map((field) => field.key)).toEqual([
      "pointcloud.pointMaskSelectMode",
    ]);
    expect(filterWorkbenchSettings(getVisibleWorkbenchSettingFields(), "网格吸附")).toEqual([]);
    expect(filterWorkbenchSettings(fields, "相机上色").map((field) => field.key)).toEqual([
      "pointcloud.colorizeWithCamera",
      "pointcloud.colorizeContrast",
      "pointcloud.colorizeBrightness",
      "pointcloud.colorizeGamma",
    ]);
  });
});
