import { describe, expect, it, vi } from "vitest";
import type { ToolBindings } from "@/api/projects";
import {
  resolveVideoScopeTransition,
  videoToolScopeForTool,
  videoToolUnit,
  videoToolEnabled,
  videoTrackSelectionTool,
} from "./videoToolUnits";

describe("video scope transitions", () => {
  it.each([
    ["box", "track"],
    ["polygon", "polygon-track"],
    ["polyline", "polyline-track"],
    ["mask", "mask-track"],
  ] as const)("keeps the exact %s / %s geometry in both directions", (frame, track) => {
    expect(
      resolveVideoScopeTransition({ tool: frame, scope: "frame" }, "track", () => true),
    ).toEqual({
      tool: track,
      scope: "track",
    });
    expect(
      resolveVideoScopeTransition({ tool: track, scope: "track" }, "frame", () => true),
    ).toEqual({
      tool: frame,
      scope: "frame",
    });
    expect(videoToolScopeForTool(frame)).toBe("frame");
    expect(videoToolScopeForTool(track)).toBe("track");
  });

  it("keeps select neutral and honors an explicit scope even when all creation is disabled", () => {
    const disabled = vi.fn(() => false);
    expect(
      resolveVideoScopeTransition({ tool: "select", scope: "frame" }, "track", disabled),
    ).toEqual({
      tool: "select",
      scope: "track",
    });
    expect(
      resolveVideoScopeTransition({ tool: "select", scope: "track" }, "frame", disabled),
    ).toEqual({
      tool: "select",
      scope: "frame",
    });
    expect(disabled).not.toHaveBeenCalled();
    expect(videoToolScopeForTool("select")).toBeNull();
  });

  it.each([
    "rotated-box",
    "keypoint",
    "smart-point",
    "smart-box",
    "exemplar",
    "magic-box",
  ] as const)("%s has no implicit track or AI substitute", (tool) => {
    const enabled = vi.fn(() => true);
    expect(resolveVideoScopeTransition({ tool, scope: "frame" }, "track", enabled)).toEqual({
      tool: "select",
      scope: "track",
      reason: expect.stringContaining("没有轨迹版本"),
    });
    expect(enabled).not.toHaveBeenCalled();
    expect(resolveVideoScopeTransition({ tool, scope: "frame" }, "frame", enabled)).toEqual({
      tool,
      scope: "frame",
    });
    expect(videoToolScopeForTool(tool)).toBe("frame");
  });

  it("does not substitute another region tool when the exact counterpart is unavailable", () => {
    const enabled = vi.fn(
      (tool: Parameters<typeof videoToolEnabled>[0]) => tool !== "polygon-track",
    );
    expect(
      resolveVideoScopeTransition({ tool: "polygon", scope: "frame" }, "track", enabled),
    ).toEqual({
      tool: "select",
      scope: "track",
      reason: expect.stringContaining("轨迹工具当前不可用"),
    });
    expect(enabled).toHaveBeenCalledTimes(1);
    expect(enabled).toHaveBeenCalledWith("polygon-track");
  });

  it("preserves the requested scope when the existing unit or frame variant gate rejects it", () => {
    const bindings: ToolBindings = {
      region: { enabled: true, classes: [], video_modes: { box: false, track: true } },
    };
    const enabled = (tool: Parameters<typeof videoToolEnabled>[0]) =>
      videoToolEnabled(tool, bindings);
    expect(resolveVideoScopeTransition({ tool: "mask", scope: "frame" }, "track", enabled)).toEqual(
      { tool: "mask-track", scope: "track" },
    );
    expect(
      resolveVideoScopeTransition({ tool: "mask-track", scope: "track" }, "frame", enabled),
    ).toMatchObject({ tool: "select", scope: "frame", reason: expect.any(String) });
    expect(
      resolveVideoScopeTransition({ tool: "box", scope: "frame" }, "track", enabled),
    ).toMatchObject({ tool: "select", scope: "track", reason: expect.any(String) });
  });

  it.each([null, undefined, {}])("retains legacy availability for %s bindings", (bindings) => {
    expect(
      resolveVideoScopeTransition({ tool: "polyline", scope: "frame" }, "track", (tool) =>
        videoToolEnabled(tool, bindings),
      ),
    ).toEqual({ tool: "polyline-track", scope: "track" });
  });
});

describe("videoTrackSelectionTool", () => {
  it.each([
    ["video_track_bbox", "track"],
    ["video_track_polygon", "polygon-track"],
    ["video_track_polyline", "polyline-track"],
    ["video_track_mask", "mask-track"],
  ] as const)("maps the stored %s identity to %s", (geometry, tool) => {
    expect(videoTrackSelectionTool(geometry)).toBe(tool);
  });

  it.each([
    "video_bbox",
    "video_polygon",
    "video_polyline",
    "video_mask",
    "box_3d",
    "video_track_rotated_bbox",
  ])("does not invent a track tool for %s", (geometry) =>
    expect(videoTrackSelectionTool(geometry)).toBeNull(),
  );
});

describe("videoToolUnit", () => {
  it("几何工具映射到各自单位, select 无单位", () => {
    expect(videoToolUnit("box")).toBe("bbox");
    expect(videoToolUnit("track")).toBe("bbox");
    expect(videoToolUnit("polygon")).toBe("region");
    expect(videoToolUnit("polygon-track")).toBe("region");
    expect(videoToolUnit("polyline")).toBe("polyline");
    expect(videoToolUnit("polyline-track")).toBe("polyline");
    expect(videoToolUnit("mask")).toBe("region");
    expect(videoToolUnit("mask-track")).toBe("region");
    expect(videoToolUnit("select")).toBeNull();
  });
});

describe("videoToolEnabled", () => {
  it("select 恒可用; 空 tool_bindings 保守放行", () => {
    expect(videoToolEnabled("select", null)).toBe(true);
    expect(videoToolEnabled("polygon", {})).toBe(true);
  });

  it("单位未启用 → 工具不可用", () => {
    const tb: ToolBindings = { bbox: { enabled: true, classes: [] } };
    // region 未配置 → 多边形不可用
    expect(videoToolEnabled("polygon", tb)).toBe(false);
    expect(videoToolEnabled("box", tb)).toBe(true);
  });

  it("单位已启用但 video_modes 关掉对应变体 → 不可用", () => {
    const tb: ToolBindings = {
      region: { enabled: true, classes: [], video_modes: { box: false, track: true } },
    };
    expect(videoToolEnabled("polygon", tb)).toBe(false); // 单帧关
    expect(videoToolEnabled("polygon-track", tb)).toBe(true); // 轨迹开
    expect(videoToolEnabled("mask", tb)).toBe(false); // Mask 是当前帧工具
    expect(videoToolEnabled("mask-track", tb)).toBe(true); // Mask 轨迹走轨迹变体
  });

  it("video_modes 为 null → 单帧/轨迹均可用", () => {
    const tb: ToolBindings = { polyline: { enabled: true, classes: [] } };
    expect(videoToolEnabled("polyline", tb)).toBe(true);
    expect(videoToolEnabled("polyline-track", tb)).toBe(true);
  });
});

// 交互式 SAM 工具必须登记进 VIDEO_TOOL_TARGET，否则 videoToolEnabled 的
// 「未知工具 → true」会静默放行、绕过全部 tool_bindings 门控（本 epic 头号陷阱）。
describe("videoToolUnits · 交互式 SAM 工具", () => {
  it("smart-point / smart-box / exemplar 按产出几何归 region 单位", () => {
    expect(videoToolUnit("smart-point")).toBe("region");
    expect(videoToolUnit("smart-box")).toBe("region");
    expect(videoToolUnit("exemplar")).toBe("region");
  });

  it("magic-box 归 bbox 单位（它收紧成矩形，不产多边形）", () => {
    expect(videoToolUnit("magic-box")).toBe("bbox");
  });

  it("只启用 bbox 单位 → magic-box 可用而 smart-* / exemplar 不可用（按产出几何分家）", () => {
    const tb: ToolBindings = {
      bbox: { enabled: true, classes: [], attribute_schema: { fields: [] } },
      region: { enabled: false, classes: [], attribute_schema: { fields: [] } },
    } as unknown as ToolBindings;
    expect(videoToolEnabled("magic-box", tb)).toBe(true);
    expect(videoToolEnabled("smart-point", tb)).toBe(false);
    expect(videoToolEnabled("exemplar", tb)).toBe(false);
  });

  it("region 单位未启用 → smart-* 不可用（不得被当作未知工具放行）", () => {
    const tb: ToolBindings = {
      bbox: { enabled: true, classes: [], attribute_schema: { fields: [] } },
      region: { enabled: false, classes: [], attribute_schema: { fields: [] } },
    } as unknown as ToolBindings;
    expect(videoToolEnabled("smart-point", tb)).toBe(false);
    expect(videoToolEnabled("smart-box", tb)).toBe(false);
    expect(videoToolEnabled("exemplar", tb)).toBe(false);
    expect(videoToolEnabled("box", tb)).toBe(true);
  });

  it("region 启用但单帧变体关闭 → smart-* 不可用（它们产单帧几何）", () => {
    const tb: ToolBindings = {
      region: {
        enabled: true,
        classes: [],
        attribute_schema: { fields: [] },
        video_modes: { box: false, track: true },
      },
    } as unknown as ToolBindings;
    expect(videoToolEnabled("smart-point", tb)).toBe(false);
    expect(videoToolEnabled("polygon-track", tb)).toBe(true);
  });

  it("region 启用且单帧变体开启 → smart-* 可用", () => {
    const tb: ToolBindings = {
      region: {
        enabled: true,
        classes: [],
        attribute_schema: { fields: [] },
        video_modes: { box: true, track: false },
      },
    } as unknown as ToolBindings;
    expect(videoToolEnabled("smart-point", tb)).toBe(true);
    expect(videoToolEnabled("smart-box", tb)).toBe(true);
    expect(videoToolEnabled("exemplar", tb)).toBe(true);
  });
});
