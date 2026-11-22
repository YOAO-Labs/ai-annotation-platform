import { useMemo } from "react";
import type { MLModelCapability } from "@/api/ml-backends";
import type { AttributeField, AttributeSchema, ToolBindings } from "@/api/projects";
import type { ToolUnitId } from "@/constants/toolUnits";

export interface CapabilityWarning {
  key: string;
  message: string;
  fillable?: AttributeField;
}

const ALLOWED_ATTR_TYPES = ["text", "number", "boolean", "select", "multiselect", "range"];

const GEOMETRY_TO_UNITS: Record<string, ToolUnitId[]> = {
  bbox: ["bbox"],
  rectangle: ["bbox"],
  rotated_bbox: ["rotated_bbox"],
  obb: ["rotated_bbox"],
  polygon: ["region"],
  mask: ["region"],
  segmentation: ["region"],
  polyline: ["polyline"],
  line: ["polyline"],
  keypoint: ["keypoint"],
  pose: ["keypoint"],
  box_3d: ["lidar_box_3d"],
  point_mask: ["point_mask_3d"],
};

const GEOMETRY_LABEL: Record<string, string> = {
  bbox: "矩形框",
  rectangle: "矩形框",
  rotated_bbox: "旋转框",
  obb: "旋转框",
  polygon: "多边形",
  mask: "掩码",
  segmentation: "分割",
  polyline: "折线",
  line: "折线",
  keypoint: "关键点",
  pose: "姿态",
  box_3d: "3D 框",
  point_mask: "点云掩码",
};

function schemaHasTextField(schema: AttributeSchema | undefined): boolean {
  return (schema?.fields ?? []).some((f) => f.type === "text");
}

const ATTR_TYPE_LANDING: Record<string, string> = {
  text: "识别文本（text）",
  language: "语言（language）",
  orientation: "方向（orientation）",
};

const ATTR_TYPE_HINT: Record<string, string> = {
  text: "为对应工具添加 text 属性",
  language: "添加 key 为 language 的属性字段",
  orientation: "启用旋转框工具或添加 key 为 orientation 的属性字段",
};

export interface UseCapabilityValidationArgs {
  activeModel: MLModelCapability | undefined;
  enabledToolUnits: Set<string> | null;
  toolBindings: ToolBindings | undefined;
}

export function useCapabilityValidation({
  activeModel,
  enabledToolUnits,
  toolBindings,
}: UseCapabilityValidationArgs): CapabilityWarning[] {
  return useMemo<CapabilityWarning[]>(() => {
    if (!activeModel) return [];
    const warnings: CapabilityWarning[] = [];
    const modelLabel = activeModel.display_name || activeModel.id;

    if (enabledToolUnits && enabledToolUnits.size > 0) {
      const geoms = activeModel.supported_geometric_outputs ?? [];
      const unmatched = geoms.filter((g) => {
        const units = GEOMETRY_TO_UNITS[g];
        if (!units) return false;
        return !units.some((u) => enabledToolUnits.has(u));
      });
      for (const g of unmatched) {
        warnings.push({
          key: `geom-${g}`,
          message: `模型「${modelLabel}」输出${GEOMETRY_LABEL[g] ?? g}，但当前项目未启用对应的标注工具，采纳后可能无法落库。`,
        });
      }
    }

    const declaredTypes = activeModel.output_attribute_types ?? [];
    const unitsToCheck =
      enabledToolUnits && enabledToolUnits.size > 0
        ? [...enabledToolUnits]
        : Object.keys(toolBindings ?? {});
    const schemaOf = (unit: string) =>
      toolBindings?.[unit as ToolUnitId]?.attribute_schema ?? undefined;
    const hasFieldKey = (key: string) =>
      unitsToCheck.some((u) => (schemaOf(u)?.fields ?? []).some((f) => f.key === key));
    for (const type of Object.keys(ATTR_TYPE_LANDING)) {
      if (!declaredTypes.includes(type)) continue;
      const ok =
        type === "text"
          ? unitsToCheck.some((u) => schemaHasTextField(schemaOf(u)))
          : type === "orientation"
            ? (enabledToolUnits?.has("rotated_bbox") ?? false) || hasFieldKey("orientation")
            : hasFieldKey(type);
      if (ok) continue;
      const schemaItem = (activeModel.output_attribute_schema ?? []).find((s) => s.key === type);
      let fillable: AttributeField | undefined;
      if (schemaItem) {
        const fieldType = (
          ALLOWED_ATTR_TYPES.includes(schemaItem.type) ? schemaItem.type : "text"
        ) as AttributeField["type"];
        fillable = {
          key: schemaItem.key,
          label: schemaItem.label || schemaItem.key,
          type: fieldType,
          required: false,
          ...(schemaItem.options?.length
            ? { options: schemaItem.options.map((o) => ({ value: o.value, label: o.label })) }
            : {}),
        };
      }
      warnings.push({
        key: `attr-${type}`,
        message: `模型「${modelLabel}」会输出${ATTR_TYPE_LANDING[type]}，但当前项目未配置对应承接位（${ATTR_TYPE_HINT[type]}），采纳后该属性将丢失。`,
        ...(fillable ? { fillable } : {}),
      });
    }

    return warnings;
  }, [activeModel, enabledToolUnits, toolBindings]);
}
