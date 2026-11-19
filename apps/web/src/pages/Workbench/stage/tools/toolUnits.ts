/**
 * 工作台 ToolId → ToolUnitId 映射.
 *
 * 与后端 ``app/schemas/_jsonb_types.py`` 中的 ToolUnitId Literal 严格对齐.
 * 区域工具 (polygon / mask) 共享 region; bbox 单独. hand / canvas 是辅助工具不持有
 * 类别 (返回 bbox 占位用于 fallback).
 *
 * AI 交互工具**按其产出几何归属单位**, 不再共享一个 ai_interactive 伪单位:
 * smart-point / smart-box / exemplar 产 polygon → region; magic-box 把 SAM 多边形
 * 收紧成外接矩形 → bbox; text-prompt 批量召回框 → bbox。此前它们全归 ai_interactive,
 * 导致 AI 画的与手画的同物落入两个互不相通的类别域 (采纳落库 tool_unit_id 取自本表)。
 * 「能否使用 AI 工具」是能力维度, 由 project.ai_interactive_enabled + 后端 supported_prompts
 * 决定, 归项目设置「ML 模型」, 与几何类别无关。
 */

import type { ToolUnitId } from "@/constants/toolUnits";
import type { ToolId } from "./index";

export const TOOL_TO_UNIT: Record<ToolId, ToolUnitId> = {
  box: "bbox",
  "rotated-box": "rotated_bbox",
  polygon: "region",
  polyline: "polyline",
  mask: "region",
  // SAM 点 / 框 / 示例交互产出多边形 → 与手画 polygon 同归 region 单位.
  "smart-point": "region",
  "smart-box": "region",
  "smart-scribble": "region",
  exemplar: "region",
  // 文本召回与 Magic Box 产出矩形框 → 与手画 box 同归 bbox 单位.
  "text-prompt": "bbox",
  "magic-box": "bbox",
  // 关键点工具归 keypoint 单位.
  keypoint: "keypoint",
  // select / hand / canvas 是选择 / 视图 / 批注工具, 不归任何 unit; 给个占位 (兜底 bbox)
  select: "bbox",
  hand: "bbox",
  canvas: "bbox",
};

export function toolUnitForTool(toolId: ToolId): ToolUnitId {
  return TOOL_TO_UNIT[toolId] ?? "bbox";
}

/** 已落库标注改类时，按几何类型恢复其类别/属性所属工具单位。 */
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
