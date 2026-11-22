import type { ToolUnitId } from "@/constants/toolUnits";
import type { ToolId } from "./index";

export const TOOL_TO_UNIT: Record<ToolId, ToolUnitId> = {
  box: "bbox",
  "rotated-box": "rotated_bbox",
  polygon: "region",
  polyline: "polyline",
  mask: "region",
  "smart-point": "region",
  "smart-box": "region",
  "smart-scribble": "region",
  exemplar: "region",
  "text-prompt": "bbox",
  "magic-box": "bbox",
  keypoint: "keypoint",
  select: "bbox",
  hand: "bbox",
  canvas: "bbox",
};

export function toolUnitForTool(toolId: ToolId): ToolUnitId {
  return TOOL_TO_UNIT[toolId] ?? "bbox";
}

export function toolUnitForGeometryType(type: string): ToolUnitId {
  switch (type) {
    case "rotated_bbox":
    case "video_rotated_bbox":
      return "rotated_bbox";
    case "polyline":
    case "video_polyline":
    case "video_track_polyline":
      return "polyline";
    case "keypoint":
      return "keypoint";
    case "polygon":
    case "multi_polygon":
    case "raster_mask":
    case "video_mask":
    case "video_polygon":
    case "video_track_polygon":
    case "video_track_mask":
      return "region";
    case "box_3d":
      return "lidar_box_3d";
    case "point_mask_3d":
      return "point_mask_3d";
    default:
      return "bbox";
  }
}
