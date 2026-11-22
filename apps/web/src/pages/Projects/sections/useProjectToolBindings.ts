import { useEffect, useMemo, useState } from "react";
import type {
  AttributeField,
  AttributeSchema,
  ClassConfigEntry,
  ClassesConfig,
  ProjectResponse,
  ToolBindings,
} from "@/api/projects";
import { defaultColorFor, type ClassRow } from "./ClassEditor";
import {
  TOOL_UNIT_GROUPS,
  dataTypeFromLegacy,
  toolUnitFromLegacy,
  type ProjectDataType,
  type ToolUnitId,
} from "@/constants/toolUnits";

export interface UnitBindingState {
  enabled: boolean;
  classRows: ClassRow[];
  attributeFields: AttributeField[];
  keypointSchema?: import("@/types").KeypointSchema | null;
  videoModes?: { box: boolean; track: boolean } | null;
}

export type UnitBindingMap = Partial<Record<ToolUnitId, UnitBindingState>>;

export function buildUnitBindings(project: {
  data_type?: string | null;
  type_key?: string;
  classes?: string[];
  classes_config?: ClassesConfig | null;
  attribute_schema?: AttributeSchema | null;
  tool_bindings?: ToolBindings | null;
}): UnitBindingMap {
  const dataType = projectDataType(project);
  const out: UnitBindingMap = {};
  for (const g of TOOL_UNIT_GROUPS) {
    if (!g.available) continue;
    if (!g.dataTypes.includes(dataType)) continue;
    out[g.id] = { enabled: false, classRows: [], attributeFields: [] };
  }

  const tb = (project.tool_bindings ?? {}) as ToolBindings;
  if (Object.keys(tb).length > 0) {
    for (const k of Object.keys(tb) as ToolUnitId[]) {
      const b = tb[k];
      if (!b) continue;
      if (!out[k]) continue;
      const classRows: ClassRow[] = (b.classes ?? [])
        .slice()
        .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
        .map((c) => ({
          name: c.name,
          color: c.color ?? defaultColorFor(c.name),
          ...(c.alias ? { alias: c.alias } : {}),
          ...(c.alias_to ? { aliasTo: c.alias_to } : {}),
        }));
      out[k] = {
        enabled: !!b.enabled,
        classRows,
        attributeFields: (b.attribute_schema as AttributeSchema | undefined)?.fields ?? [],
        keypointSchema: b.keypoint_schema ?? null,
        videoModes: b.video_modes
          ? { box: b.video_modes.box ?? true, track: b.video_modes.track ?? true }
          : null,
      };
    }
    foldRetiredAiInteractive(out, tb);
    return out;
  }

  const legacyUnit = toolUnitFromLegacy(project.type_key ?? "image-det");
  const defaultUnit = out[legacyUnit]
    ? legacyUnit
    : (Object.keys(out)[0] as ToolUnitId | undefined);
  if (!defaultUnit) return out;
  const cfg = (project.classes_config ?? {}) as ClassesConfig;
  const ordered = [...(project.classes ?? [])].sort((a, b) => {
    const oa = cfg[a]?.order ?? 0;
    const ob = cfg[b]?.order ?? 0;
    return oa - ob;
  });
  const classRows: ClassRow[] = ordered.map((name) => {
    const c: ClassConfigEntry | undefined = cfg[name];
    return {
      name,
      color: c?.color ?? defaultColorFor(name),
      ...(c?.alias ? { alias: c.alias } : {}),
    } as ClassRow;
  });
  out[defaultUnit] = {
    enabled: true,
    classRows,
    attributeFields: project.attribute_schema?.fields ?? [],
  };
  return out;
}

const AI_INTERACTIVE_KEY = "ai_interactive";

function foldRetiredAiInteractive(out: UnitBindingMap, tb: ToolBindings): void {
  const src = (tb as Record<string, ToolBindings[ToolUnitId]>)[AI_INTERACTIVE_KEY];
  if (!src) return;
  const srcClasses = src.classes ?? [];
  const srcFields = (src.attribute_schema as AttributeSchema | undefined)?.fields ?? [];
  if (srcClasses.length === 0 && srcFields.length === 0) return;

  const tbAny = tb as Record<string, unknown>;
  let targets = (["region", "bbox"] as ToolUnitId[]).filter((t) => tbAny[t] && out[t]);
  if (targets.length === 0 && out.bbox) targets = ["bbox"];

  for (const t of targets) {
    const ub = out[t];
    if (!ub) continue;
    const seenNames = new Set(ub.classRows.map((c) => c.name));
    for (const c of srcClasses) {
      if (seenNames.has(c.name)) continue;
      ub.classRows.push({
        name: c.name,
        color: c.color ?? defaultColorFor(c.name),
        ...(c.alias ? { alias: c.alias } : {}),
        ...(c.alias_to ? { aliasTo: c.alias_to } : {}),
      });
      seenNames.add(c.name);
    }
    const seenKeys = new Set(ub.attributeFields.map((f) => f.key));
    for (const f of srcFields) {
      if (seenKeys.has(f.key)) continue;
      ub.attributeFields.push(f);
      seenKeys.add(f.key);
    }
  }
}

function projectDataType(project: {
  data_type?: string | null;
  type_key?: string;
}): ProjectDataType {
  if (
    project.data_type === "image" ||
    project.data_type === "video" ||
    project.data_type === "lidar"
  ) {
    return project.data_type;
  }
  return dataTypeFromLegacy(project.type_key ?? "image-det");
}

function isUnitEmpty(ub: UnitBindingState): boolean {
  return ub.classRows.length === 0 && ub.attributeFields.length === 0 && !ub.keypointSchema;
}

export function unitBindingsToPayload(bindings: UnitBindingMap): ToolBindings {
  const out: ToolBindings = {};
  for (const k of Object.keys(bindings) as ToolUnitId[]) {
    const ub = bindings[k];
    if (!ub) continue;
    if (!ub.enabled && isUnitEmpty(ub)) continue;
    out[k] = {
      enabled: ub.enabled,
      classes: ub.classRows.map((r, i) =>
        r.aliasTo
          ? { name: r.name, order: i, alias_to: r.aliasTo }
          : {
              name: r.name,
              color: r.color,
              order: i,
              ...(r.alias ? { alias: r.alias } : {}),
            },
      ),
      attribute_schema:
        ub.attributeFields.length > 0 ? { fields: ub.attributeFields } : { fields: [] },
      ...(k === "keypoint" && ub.keypointSchema ? { keypoint_schema: ub.keypointSchema } : {}),
      ...(ub.videoModes ? { video_modes: ub.videoModes } : {}),
    };
  }
  return out;
}

export function useProjectToolBindings(project: ProjectResponse) {
  const initial = useMemo(() => buildUnitBindings(project), [project]);
  const [bindings, setBindings] = useState<UnitBindingMap>(initial);
  const [activeUnit, setActiveUnit] = useState<ToolUnitId>(() => firstActiveUnit(initial));

  useEffect(() => {
    setBindings(initial);
    setActiveUnit((cur) => (initial[cur] ? cur : firstActiveUnit(initial)));
  }, [initial]);

  const dirty = JSON.stringify(bindings) !== JSON.stringify(initial);

  return {
    bindings,
    setBindings,
    activeUnit,
    setActiveUnit,
    dirty,
    initial,
  };
}

function firstActiveUnit(bindings: UnitBindingMap): ToolUnitId {
  const firstEnabled = (Object.keys(bindings) as ToolUnitId[]).find((k) => bindings[k]?.enabled);
  return firstEnabled ?? (Object.keys(bindings)[0] as ToolUnitId | undefined) ?? "bbox";
}
