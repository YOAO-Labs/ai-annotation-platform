/**
 * 视频 VideoTool → 工具单位 / 单帧·轨迹变体 的集中映射 (对齐图片 stage/tools/toolUnits.ts)。
 *
 * 纠偏后视频每个几何是独立工具单位 (各自类别/属性 schema):
 *   矩形框 → bbox, 多边形 → region, 折线 → polyline。
 * 每单位的 `video_modes:{box(单帧), track}` 决定单帧/轨迹变体是否可用 (复刻 bbox 现有模式)。
 * 工具可用 ⇔ 单位已启用 且 该变体开关开 (video_modes 为 null 时两者均可用, 兼容老项目)。
 */

import type { ToolUnitId } from "@/constants/toolUnits";
import type { ToolBindings } from "@/api/projects";
import type { VideoTool } from "../state/useWorkbenchState";

/** box = 单帧几何, track = 轨迹几何 (跨帧关键帧)。 */
export type VideoVariant = "box" | "track";
export type VideoToolScope = "frame" | "track";

export interface VideoToolSelection {
  tool: VideoTool;
  scope: VideoToolScope;
}

export const VIDEO_TOOL_TARGET: Partial<
  Record<VideoTool, { unit: ToolUnitId; variant: VideoVariant }>
> = {
  box: { unit: "bbox", variant: "box" },
  "rotated-box": { unit: "rotated_bbox", variant: "box" },
  keypoint: { unit: "keypoint", variant: "box" },
  track: { unit: "bbox", variant: "track" },
  mask: { unit: "region", variant: "box" },
  "mask-track": { unit: "region", variant: "track" },
  polygon: { unit: "region", variant: "box" },
  "polygon-track": { unit: "region", variant: "track" },
  polyline: { unit: "polyline", variant: "box" },
  "polyline-track": { unit: "polyline", variant: "track" },
  // 交互式 SAM 工具按**产出几何**归属单位 (对齐图片侧 TOOL_TO_UNIT):
  // smart-point / smart-box 的候选采纳后落单帧 video_polygon → region 单位、box 变体。
  // **新增 AI 工具必须在此登记**, 否则 videoToolEnabled 的「未知工具 → true」(见下)
  // 会静默放行、绕过全部 tool_bindings 门控, 且 videoToolUnit 返回 null 会污染类选择器。
  "smart-point": { unit: "region", variant: "box" },
  "smart-box": { unit: "region", variant: "box" },
  exemplar: { unit: "region", variant: "box" },
  // magic-box 把候选收紧成外接矩形落 video_bbox, 故归 bbox 单位 (与 smart-* / exemplar 不同)。
  "magic-box": { unit: "bbox", variant: "box" },
};

/** Selection has no creating scope; all geometry tools keep their existing payload variant. */
export function videoToolScopeForTool(tool: VideoTool): VideoToolScope | null {
  const variant = VIDEO_TOOL_TARGET[tool]?.variant;
  return variant === "track" ? "track" : variant === "box" ? "frame" : null;
}

// Pair exact geometries: polygon and Mask share a unit, but cannot substitute for each other.
const VIDEO_SCOPE_PAIRS: ReadonlyArray<Record<VideoToolScope, VideoTool>> = [
  { frame: "box", track: "track" },
  { frame: "polygon", track: "polygon-track" },
  { frame: "polyline", track: "polyline-track" },
  { frame: "mask", track: "mask-track" },
];

/** Resolve an admitted scope request without choosing a different geometry or an AI tool. */
export function resolveVideoScopeTransition(
  current: VideoToolSelection,
  targetScope: VideoToolScope,
  isEnabled: (tool: VideoTool) => boolean,
): VideoToolSelection & { reason?: string } {
  if (current.tool === "select") return { tool: "select", scope: targetScope };
  const targetTool =
    videoToolScopeForTool(current.tool) === targetScope
      ? current.tool
      : VIDEO_SCOPE_PAIRS.find(
          (pair) => pair.frame === current.tool || pair.track === current.tool,
        )?.[targetScope];
  const scopeLabel = targetScope === "frame" ? "单帧" : "轨迹";
  if (!targetTool) {
    return {
      tool: "select",
      scope: targetScope,
      reason: `当前工具没有${scopeLabel}版本，已保留${scopeLabel}范围并切换到选择工具`,
    };
  }
  if (!isEnabled(targetTool)) {
    return {
      tool: "select",
      scope: targetScope,
      reason: `对应的${scopeLabel}工具当前不可用，已保留${scopeLabel}范围并切换到选择工具`,
    };
  }
  return { tool: targetTool, scope: targetScope };
}

/** Only existing manual video track geometries select a creating track tool. */
export function videoTrackSelectionTool(geometryType: string): VideoTool | null {
  switch (geometryType) {
    case "video_track_bbox":
      return "track";
    case "video_track_polygon":
      return "polygon-track";
    case "video_track_polyline":
      return "polyline-track";
    case "video_track_mask":
      return "mask-track";
    default:
      return null;
  }
}

/** 该视频工具落在哪个工具单位; select 等非几何工具返回 null。 */
export function videoToolUnit(t: VideoTool): ToolUnitId | null {
  return VIDEO_TOOL_TARGET[t]?.unit ?? null;
}

/**
 * 视频工具在给定 tool_bindings 下是否可用。
 * select 恒可用; 几何工具需其单位已启用且该变体 (单帧/轨迹) 开关开。
 * tool_bindings 为空 (老项目未 backfill) 时保守放行, 交由工作台其它兜底。
 */
export function videoToolEnabled(t: VideoTool, tb: ToolBindings | null | undefined): boolean {
  const target = VIDEO_TOOL_TARGET[t];
  if (!target) return true; // select / 未知工具
  if (!tb || Object.keys(tb).length === 0) return true; // 老项目兜底
  const binding = tb[target.unit];
  if (!binding?.enabled) return false;
  const vm = binding.video_modes;
  if (!vm) return true; // null = 单帧/轨迹均可用
  return target.variant === "box" ? (vm.box ?? true) : (vm.track ?? true);
}
