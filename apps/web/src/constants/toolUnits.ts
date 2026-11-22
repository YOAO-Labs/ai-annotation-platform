import type { IconName } from "@/components/ui/Icon";

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
  tools: ReadonlyArray<string>;
  dataTypes: ReadonlyArray<ProjectDataType>;
  available: boolean;
}

export type ProjectDataType = "image" | "video" | "lidar";

export interface ProjectDataTypeSpec {
  id: ProjectDataType;
  label: string;
  hint: string;
  icon: IconName;
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
  {
    id: "lidar_box_3d",
    label: "3D 立体框",
    hint: "点云 7-DoF 立体框;车辆 / 行人 / 障碍物",
    icon: "cube",
    tools: ["lidar_box"],
    dataTypes: ["lidar"],
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

export function defaultEnabledUnits(dt: ProjectDataType): ToolUnitId[] {
  if (dt === "video") return ["bbox"];
  if (dt === "lidar") return ["lidar_box_3d", "point_mask_3d"];
  return ["bbox", "region"];
}

export function dataTypeFromLegacy(typeKey: string): ProjectDataType {
  if (typeKey.startsWith("video")) return "video";
  if (typeKey === "lidar") return "lidar";
  return "image";
}

export function toolUnitFromLegacy(typeKey: string): ToolUnitId {
  if (typeKey === "image-seg") return "region";
  if (typeKey === "lidar") return "lidar_box_3d";
  return "bbox";
}
