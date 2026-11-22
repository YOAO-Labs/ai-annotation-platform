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
  keypointSchema: import("@/types").KeypointSchema | null;
  hasOwnClasses: boolean;
}

export function useToolBindings(
  project: ProjectResponse | null | undefined,
  activeToolId: ToolId,
  overrideUnit?: ToolUnitId,
): ToolBindingsView {
  const toolUnitId = overrideUnit ?? toolUnitForTool(activeToolId);
  return useMemo(() => {
    const tb = (project?.tool_bindings ?? {}) as ToolBindings;
    const view = _materialize(tb, toolUnitId);
    const hasOwnClasses = view.classes.length > 0;
    if (hasOwnClasses || overrideUnit) {
      return view;
    }
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

export function classesForUnit(tb: ToolBindings | null | undefined, unit: ToolUnitId): string[] {
  return _materialize((tb ?? {}) as ToolBindings, unit).classes;
}

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
