import type { ToolUnitId } from "@/constants/toolUnits";
import type { ToolBindings } from "@/api/projects";
import type { VideoTool } from "../state/useWorkbenchState";

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
  "smart-point": { unit: "region", variant: "box" },
  "smart-box": { unit: "region", variant: "box" },
  exemplar: { unit: "region", variant: "box" },
  "magic-box": { unit: "bbox", variant: "box" },
};

export function videoToolScopeForTool(tool: VideoTool): VideoToolScope | null {
  const variant = VIDEO_TOOL_TARGET[tool]?.variant;
  return variant === "track" ? "track" : variant === "box" ? "frame" : null;
}

const VIDEO_SCOPE_PAIRS: ReadonlyArray<Record<VideoToolScope, VideoTool>> = [
  { frame: "box", track: "track" },
  { frame: "polygon", track: "polygon-track" },
  { frame: "polyline", track: "polyline-track" },
  { frame: "mask", track: "mask-track" },
];

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

export function videoToolUnit(t: VideoTool): ToolUnitId | null {
  return VIDEO_TOOL_TARGET[t]?.unit ?? null;
}

export function videoToolEnabled(t: VideoTool, tb: ToolBindings | null | undefined): boolean {
  const target = VIDEO_TOOL_TARGET[t];
  if (!target) return true;
  if (!tb || Object.keys(tb).length === 0) return true;
  const binding = tb[target.unit];
  if (!binding?.enabled) return false;
  const vm = binding.video_modes;
  if (!vm) return true;
  return target.variant === "box" ? (vm.box ?? true) : (vm.track ?? true);
}
