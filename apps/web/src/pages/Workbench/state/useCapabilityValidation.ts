import { useMemo } from "react";
import type { MLModelCapability } from "@/api/ml-backends";
import type { AttributeField, AttributeSchema, ToolBindings } from "@/api/projects";
import type { ToolUnitId } from "@/constants/toolUnits";

// 能力声明协议 v2 — active model 与项目配置的兼容性校验 (非阻断, 仅警告).
//
// 两类检查:
//   1) 几何兼容: active model 的 supported_geometric_outputs 是否落在项目启用的 tool_unit 几何里.
//      OCR/doc_layout 不产生标准几何 (text / bbox 版面框), 只校验「项目能否承接该几何」.
//     2) 属性落点：遍历 model.output_attribute_types 声明的每种属性
//      (text / language / orientation; class 跳过), 校验项目某启用 unit 是否有承接位
//      (text→type=text 字段; language→key=language 字段; orientation→旋转框工具或
//      key=orientation 字段), 缺失则警告「采纳后该属性丢失」.
//
// 任一不满足时返回一条 warning; 上层在 InteractiveToolBar / AIInspectorPanel 以非阻断条幅展示.

export interface CapabilityWarning {
  /** 稳定 key, 供 React list + 去重. */
  key: string;
  /** 中文提示文案 (硬编码). */
  message: string;
  /**
   * 该警告可「一键补全」的属性字段 (仅属性落点警告携带)。
   * 取自 active model 自报 output_attribute_schema 中 key 对应的项, 已转成项目 AttributeField;
   * 上层据此在警告条旁渲染「补全」CTA, 把字段补进项目所有启用工具单位。
   * 缺失 = 无对应 schema 项 (无字段可补), 不给 CTA。
   */
  fillable?: AttributeField;
}

// output_attribute_schema item.type → 项目 AttributeField.type 合法集; 越界回落 text。
// 与 Projects/sections itemToField 同口径, 但内联实现避免跨 feature 耦合。
const ALLOWED_ATTR_TYPES = ["text", "number", "boolean", "select", "multiselect", "range"];

// model 几何输出名 → 项目 tool_unit 几何能力的映射.
// model.supported_geometric_outputs 用 grounded-sam2 / 协议 v2 的命名 (bbox/polygon/mask/...),
// 项目侧用 ToolUnitId. 一个几何能被多个 unit 承接, 故值是 unit 集合.
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

// 半开放受控词表 output_attribute_types 中**需校验落点**的属性类型 → 用户可读标签。
// `class` 刻意不入表: 类别 taxonomy 几乎恒在, 校验只会制造噪音 (WS1 范围决策)。
const ATTR_TYPE_LANDING: Record<string, string> = {
  text: "识别文本（text）",
  language: "语言（language）",
  orientation: "方向（orientation）",
};

// 缺落点时给用户的修复提示 (去项目设置加什么)。
const ATTR_TYPE_HINT: Record<string, string> = {
  text: "为对应工具添加 text 属性",
  language: "添加 key 为 language 的属性字段",
  orientation: "启用旋转框工具或添加 key 为 orientation 的属性字段",
};

export interface UseCapabilityValidationArgs {
  /** 当前 active model; 无 model (单模型老 backend 或未拉到能力) 时不产警告. */
  activeModel: MLModelCapability | undefined;
  /** 项目启用的 tool_unit 集合 (来自 enabledToolUnits); null = 老项目无显式启用配置, 跳过几何校验. */
  enabledToolUnits: Set<string> | null;
  /** 项目 tool_bindings, 用来取各 unit 的 attribute_schema 判断文本属性. */
  toolBindings: ToolBindings | undefined;
}

/**
 * 返回 active model 与项目配置的兼容性警告数组 (可能为空).
 * 纯派生, 无副作用; 由 InteractiveToolBar / AIInspectorPanel 展示.
 */
export function useCapabilityValidation({
  activeModel,
  enabledToolUnits,
  toolBindings,
}: UseCapabilityValidationArgs): CapabilityWarning[] {
  return useMemo<CapabilityWarning[]>(() => {
    if (!activeModel) return [];
    const warnings: CapabilityWarning[] = [];
    const modelLabel = activeModel.display_name || activeModel.id;

    // 1) 几何兼容: 仅当项目有显式启用 unit 集合时校验 (老项目 enabledToolUnits=null 跳过).
    if (enabledToolUnits && enabledToolUnits.size > 0) {
      const geoms = activeModel.supported_geometric_outputs ?? [];
      const unmatched = geoms.filter((g) => {
        const units = GEOMETRY_TO_UNITS[g];
        // 未知几何名: 无法判断, 不报警 (避免协议新增几何时误报).
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

    // 2) 属性输出落点: 遍历 model 声明的每种 output_attribute_type, 校验项目是否有承接位,
    //    缺失则非阻断警告「采纳后该属性丢失」。class 不入表 —— 类别 taxonomy 几乎恒在, 警告即噪音。
    const declaredTypes = activeModel.output_attribute_types ?? [];
    const unitsToCheck =
      enabledToolUnits && enabledToolUnits.size > 0
        ? [...enabledToolUnits]
        : Object.keys(toolBindings ?? {});
    const schemaOf = (unit: string) =>
      toolBindings?.[unit as ToolUnitId]?.attribute_schema ?? undefined;
    // 项目某启用 unit 是否有 key=<key> 的属性字段 (OCR 协议写 attributes.language / .orientation)。
    const hasFieldKey = (key: string) =>
      unitsToCheck.some((u) => (schemaOf(u)?.fields ?? []).some((f) => f.key === key));
    for (const type of Object.keys(ATTR_TYPE_LANDING)) {
      if (!declaredTypes.includes(type)) continue;
      // 判据差异是有意的：text 是「类型槽」，任意 text 类型字段
      // 都能装识别文本, 故按 type 匹配; language/orientation 是「具名值槽」——固定取值域的具体
      // 属性, 故按 key 具名匹配。手建字段易把 key 取错, 故项目设置「类别与属性」页提供「推荐属性」
      // 一键填入对齐协议 key (见 AttributeSchemaEditor.recommendedFields)。
      const ok =
        type === "text"
          ? unitsToCheck.some((u) => schemaHasTextField(schemaOf(u)))
          : type === "orientation"
            ? (enabledToolUnits?.has("rotated_bbox") ?? false) || hasFieldKey("orientation")
            : hasFieldKey(type); // language: 项目需有 key=language 的属性字段
      if (ok) continue;
      // 从 model 自报 schema 取 key=type 的字段定义, 内联转 AttributeField 作「一键补全」载荷。
      // 找不到对应 schema 项就不带 fillable (没字段可补, 上层不给 CTA)。
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
