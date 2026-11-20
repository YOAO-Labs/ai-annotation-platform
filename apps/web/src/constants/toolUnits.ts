/**
 * 工具维度类别 / 属性绑定 — 前端常量与映射.
 *
 * 与后端 ``app/schemas/_jsonb_types.py`` 中 ``ToolUnitId`` Literal 完全对齐.
 * 新增 unit 时两端同步;codegen 派生的 schema 仍是 dict<string, ToolBinding>,
 * 前端用此模块的 Literal type 收窄校验.
 */

import type { IconName } from "@/components/ui/Icon";

// 「AI 交互」曾作为伪工具单位 "ai_interactive" 列在此处。它不产出独有几何, 已退役为项目级
// 开关 `project.ai_interactive_enabled`(项目设置「ML 模型」); 存量 tool_bindings 里的残留 key
// 由后端迁移 0116 合并进 region/bbox, 字面量随之删除。AI 工具的类别随其产出几何归 region/bbox。
export type ToolUnitId =
  | "bbox"
  | "polyline"
  | "keypoint"
  | "region"
  | "lidar_box_3d"
  | "point_mask_3d"
  | "rotated_bbox";

export const TOOL_UNIT_IDS: ReadonlyArray<ToolUnitId> = [
  "bbox",
  "polyline",
  "keypoint",
  "region",
  "lidar_box_3d",
  "point_mask_3d",
  "rotated_bbox",
];

export interface ToolUnitGroupSpec {
  id: ToolUnitId;
  label: string;
  hint: string;
  icon: IconName;
  /** unit 包含的具体绘制工具 (Workbench ToolId), 仅 UI 文案展示用 */
  tools: ReadonlyArray<string>;
  /** 哪些数据类型可启用 (image / video / lidar) */
  dataTypes: ReadonlyArray<ProjectDataType>;
  /** 本版是否已实现 (false = 占位置灰) */
  available: boolean;
}

export type ProjectDataType = "image" | "video" | "lidar";

export interface ProjectDataTypeSpec {
  id: ProjectDataType;
  label: string;
  hint: string;
  icon: IconName;
  /** 兼容旧 type_key 默认值 (新建项目时 form.typeKey 的初值) */
  legacyTypeKey: string;
}

export const PROJECT_DATA_TYPES: ReadonlyArray<ProjectDataTypeSpec> = [
  {
    id: "image",
    label: "图片",
    hint: "图像 · 单张图片标注",
    icon: "image",
    legacyTypeKey: "image-det",
  },
  {
    id: "video",
    label: "视频",
    hint: "视频 · 帧序列标注",
    icon: "video",
    legacyTypeKey: "video-track",
  },
  {
    id: "lidar",
    label: "3D 点云",
    hint: "激光点云 · 3D 框 / 点云分割标注",
    icon: "cube",
    legacyTypeKey: "lidar",
  },
];

export const TOOL_UNIT_GROUPS: ReadonlyArray<ToolUnitGroupSpec> = [
  {
    id: "bbox",
    label: "矩形框 (bbox)",
    hint: "拖框圈选;基础几何",
    icon: "rect",
    tools: ["box"],
    dataTypes: ["image", "video"],
    available: true,
  },
  {
    id: "polyline",
    label: "折线 (polyline)",
    hint: "开放折线;车道线 / 道路",
    icon: "annotationPolyline",
    tools: ["polyline"],
    dataTypes: ["image", "video"],
    available: true,
  },
  {
    id: "rotated_bbox",
    label: "旋转框 (OBB)",
    hint: "带角度矩形;遥感 / 文本 / 车辆",
    icon: "rotatedBox",
    tools: ["rotated-box"],
    dataTypes: ["image", "video"],
    available: true,
  },
  {
    id: "keypoint",
    label: "关键点 (keypoint)",
    hint: "命名节点 + 骨骼连线; COCO 范式姿态 / 部件标注",
    icon: "keypoints",
    tools: ["keypoint"],
    dataTypes: ["image", "video"],
    available: true,
  },
  {
    id: "region",
    label: "区域 (polygon + mask)",
    hint: "多边形与笔刷掩码打包;实例分割",
    icon: "annotationPolygon",
    tools: ["polygon", "mask"],
    dataTypes: ["image", "video"],
    available: true,
  },
  // 「AI 交互」曾作为一个工具单位列在此处 (带类别 / 属性编辑器)。它不产出独有几何,
  // 已退役为项目级开关 project.ai_interactive_enabled (项目设置「ML 模型」);
  // AI 工具的类别随其产出几何归属 region / bbox 单位 (见 stage/tools/toolUnits.ts)。
  {
    id: "lidar_box_3d",
    label: "3D 立体框",
    hint: "点云 7-DoF 立体框;车辆 / 行人 / 障碍物",
    icon: "cube",
    tools: ["lidar_box"],
    dataTypes: ["lidar"],
    // 解禁: 点云项目可在向导配 3D 框类别 (3D 编辑交互另行实现)
    available: true,
  },
  {
    id: "point_mask_3d",
    label: "点云分割",
    hint: "框选点云索引集合;细粒度 3D 分割",
    icon: "scissors",
    tools: ["point_mask"],
    dataTypes: ["lidar"],
    available: true,
  },
];

export function getToolUnitGroup(id: ToolUnitId): ToolUnitGroupSpec | undefined {
  return TOOL_UNIT_GROUPS.find((g) => g.id === id);
}

/** 给定数据类型, 推荐默认启用的 unit 集合. */
export function defaultEnabledUnits(dt: ProjectDataType): ToolUnitId[] {
  if (dt === "video") return ["bbox"];
  if (dt === "lidar") return ["lidar_box_3d", "point_mask_3d"];
  // image 默认: bbox + region 两个开 (覆盖检测 / 分割 90% 场景)。AI 工具无独立单位,
  // 其类别随产出几何归 region / bbox, 可用性由 project.ai_interactive_enabled 决定。
  return ["bbox", "region"];
}

/** 兼容: 把 legacy type_key 推导到 data_type. */
export function dataTypeFromLegacy(typeKey: string): ProjectDataType {
  if (typeKey.startsWith("video")) return "video";
  if (typeKey === "lidar") return "lidar";
  return "image";
}

/** 兼容: 把 legacy type_key 推导到默认 tool_unit (与后端 migration 0072 同规则). */
export function toolUnitFromLegacy(typeKey: string): ToolUnitId {
  if (typeKey === "image-seg") return "region";
  if (typeKey === "lidar") return "lidar_box_3d";
  return "bbox";
}
