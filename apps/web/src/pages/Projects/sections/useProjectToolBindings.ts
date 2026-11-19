/**
 * ClassesSection / TemplateEditModal 共享 hook.
 *
 * 从 project.tool_bindings (或老 classes_config 派生兜底) 反推出按 ToolUnitId 拆分
 * 的编辑状态; 保存时把当前各 unit 的 classRows / attributeFields 序列化回 tool_bindings.
 */

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
  /** 仅 keypoint 单元用：骨骼模板 (命名节点 + 连线)。 */
  keypointSchema?: import("@/types").KeypointSchema | null;
  /**
   * 仅视频几何单位 (bbox/region/polyline) 用：单帧 / 轨迹变体独立开关。null = 两者均可用。
   * box = 单帧几何, track = 轨迹几何 (跨帧关键帧)。每个视频几何单位各自持有一份。
   */
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
    // 退役的 ai_interactive 伪单位: 不在 TOOL_UNIT_GROUPS 里, 上面的循环会跳过它 (out[k]
    // 不存在)。老项目 tool_bindings 里若仍有该 key, 直接跳过会在保存时静默丢掉它携带的
    // classes/attributes。改为折叠进 geometry 单位 (region/bbox), 与后端迁移 0116 同规则。
    foldRetiredAiInteractive(out, tb);
    return out;
  }

  // 兜底: 老项目从扁平 classes_config / attribute_schema 派生到默认 unit
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

/**
 * 把退役的 ai_interactive 单位携带的 classes / attributes 折叠进已配置的 geometry 单位
 * (region / bbox), 就地改写 `out`。与后端迁移 0116 `merge_ai_interactive_binding` 同规则:
 * 目标 = tool_bindings 里已存在且当前 data_type 可用的 {region, bbox} 子集 (都没有则回落
 * bbox); 同名 class / 同 key 属性冲突时保留目标单位的、跳过来源的。避免保存时静默丢配置。
 */
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

/** 单位是否「无任何配置内容」(纯空)。 */
function isUnitEmpty(ub: UnitBindingState): boolean {
  return ub.classRows.length === 0 && ub.attributeFields.length === 0 && !ub.keypointSchema;
}

/**
 * 把 UnitBindingMap 序列化为后端 PATCH 体的 tool_bindings 字段。
 *
 * 保留「禁用但已配置」的单位 (enabled:false + 原有 classes / attribute_schema)，
 * 只丢弃从未配置过的纯空单位，避免禁用即丢失配置 (后端 derive_* 已会跳过
 * enabled=false 的单位，故禁用单位不会污染工作台扁平投影)。
 */
export function unitBindingsToPayload(bindings: UnitBindingMap): ToolBindings {
  const out: ToolBindings = {};
  for (const k of Object.keys(bindings) as ToolUnitId[]) {
    const ub = bindings[k];
    if (!ub) continue;
    // 纯空且禁用 → 不落库，保持 tool_bindings 精简。
    if (!ub.enabled && isUnitEmpty(ub)) continue;
    out[k] = {
      enabled: ub.enabled,
      // 链接(aliasTo)的类完全继承目标 color/alias, 故 payload 省略自身
      // color/alias 走后端 resolve_class_visual 继承; 未链接的类照旧带显式 color/alias。
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
      // keypoint 单元附带骨骼模板 (后端 ToolBinding.keypoint_schema 就位前不落库)。
      ...(k === "keypoint" && ub.keypointSchema ? { keypoint_schema: ub.keypointSchema } : {}),
      // 视频几何单位 (bbox/region/polyline) 附带单帧/轨迹开关 (仅视频项目设置, null 不落库)。
      ...(ub.videoModes ? { video_modes: ub.videoModes } : {}),
    };
  }
  return out;
}

/** Section / Modal 端共用的状态管理 hook. */
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
