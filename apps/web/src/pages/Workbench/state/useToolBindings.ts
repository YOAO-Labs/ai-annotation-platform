/**
 * 工作台读取 project.tool_bindings 派生当前激活工具的类别与属性 schema.
 *
 * 给定 project + activeToolId, 返回:
 *   - classes: string[]              当前 tool_unit 内类别名列表 (按 order)
 *   - classesConfig: ClassesConfig  扁平形 (name → {color, alias, order}), 兼容 ClassPalette
 *   - attributeSchema: AttributeSchema  当前 tool_unit 的属性 schema
 *   - toolUnitId: ToolUnitId        派生出的工具单位 (供创建标注时 POST tool_unit_id)
 *
 * tool_bindings 是唯一真值 (全部项目已 backfill). 当前 unit 未配置类时,
 * 借 bbox / region 默认 unit 的类名展示; 都为空则返回空视图.
 */

import { useMemo } from "react";
import type { AttributeSchema, ClassesConfig, ProjectResponse, ToolBindings } from "@/api/projects";
import type { ToolUnitId } from "@/constants/toolUnits";
import type { ToolId } from "../stage/tools";
import { toolUnitForTool } from "../stage/tools/toolUnits";

export interface ToolBindingsView {
  classes: string[];
  classesConfig: ClassesConfig;
  attributeSchema: AttributeSchema;
  toolUnitId: ToolUnitId;
  /** keypoint 单元的骨骼模板 (仅 toolUnitId === "keypoint" 时有意义)。 */
  keypointSchema: import("@/types").KeypointSchema | null;
  /**
   * 当前激活工具「自身的 unit」是否定义了类别 (借 bbox/region 兜底之前判断)。
   * 工作台据此决定: 落框提交时若工具本身没有类别定义, 直接以 __unknown 落库而非弹选类别窗
   * (修复老项目用无类别工具仍弹窗的 BUG)。
   */
  hasOwnClasses: boolean;
}

export function useToolBindings(
  project: ProjectResponse | null | undefined,
  activeToolId: ToolId,
  // 3D 点云台无对应 2D ToolId,直接指定工具单位(lidar_box_3d),绕过 toolUnitForTool。
  overrideUnit?: ToolUnitId,
): ToolBindingsView {
  const toolUnitId = overrideUnit ?? toolUnitForTool(activeToolId);
  return useMemo(() => {
    const tb = (project?.tool_bindings ?? {}) as ToolBindings;
    const view = _materialize(tb, toolUnitId);
    const hasOwnClasses = view.classes.length > 0;
    // overrideUnit(如 3D 的 lidar_box_3d)显式指定单位,不借 bbox/region 兜底(强隔离)。
    if (hasOwnClasses || overrideUnit) {
      return view;
    }
    // 兜底: 当前 unit 未配置或类集合为空 (尤其是
    // ai_interactive — 历史项目升级后曾因迁移漏配导致 AI 调色板清空).
    // 退到 bbox / region 默认 unit 借类名; toolUnitId 仍保持当前激活工具的 unit,
    // 让 POST 写入时仍按工具维度落 tool_unit_id, 仅 UI 借用类名展示.
    for (const fallbackUnit of ["bbox", "region"] as const) {
      if (fallbackUnit === toolUnitId) continue;
      const fb = _materialize(tb, fallbackUnit);
      if (fb.classes.length > 0) {
        return { ...fb, toolUnitId, hasOwnClasses };
      }
    }
    return { ...view, toolUnitId, hasOwnClasses };
  }, [project, toolUnitId, overrideUnit]);
}

/**
 * B-57 · 纯函数: 取某 tool_unit 自身定义的类名 (按 order), 无 bbox/region 兜底。
 * 镜像后端 lookup_classes_for_tool_unit (强隔离), 供采纳预测时按预测单位列出可选类别。
 * 未启用 / 未配置类时返回空数组。
 */
export function classesForUnit(tb: ToolBindings | null | undefined, unit: ToolUnitId): string[] {
  return _materialize((tb ?? {}) as ToolBindings, unit).classes;
}

/** 已落库标注改类/改属性时读取其自身工具单位 schema，不借当前激活工具兜底。 */
export function attributeSchemaForUnit(
  tb: ToolBindings | null | undefined,
  unit: ToolUnitId,
): AttributeSchema {
  return _materialize((tb ?? {}) as ToolBindings, unit).attributeSchema;
}

function _materialize(tb: ToolBindings, unit: ToolUnitId): ToolBindingsView {
  const binding = tb[unit];
  if (!binding || !binding.enabled) {
    return {
      classes: [],
      classesConfig: {} as ClassesConfig,
      attributeSchema: { fields: [] } as AttributeSchema,
      toolUnitId: unit,
      keypointSchema: null,
      hasOwnClasses: false,
    };
  }
  const ordered = (binding.classes ?? []).slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
  const classes = ordered.map((c) => c.name);
  const classesConfig: ClassesConfig = {};
  ordered.forEach((c, i) => {
    classesConfig[c.name] = {
      color: c.color ?? null,
      order: c.order ?? i,
      alias: c.alias ?? null,
    };
  });
  const attributeSchema: AttributeSchema = (binding.attribute_schema as
    | AttributeSchema
    | undefined) ?? {
    fields: [],
  };
  return {
    classes,
    classesConfig,
    attributeSchema,
    toolUnitId: unit,
    keypointSchema: binding.keypoint_schema ?? null,
    hasOwnClasses: classes.length > 0,
  };
}
