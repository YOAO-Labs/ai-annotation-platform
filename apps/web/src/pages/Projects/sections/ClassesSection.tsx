import { useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { DropdownMenu, type DropdownItem } from "@/components/ui/DropdownMenu";
import { Icon } from "@/components/ui/Icon";
import { Switch } from "@/components/ui/Switch";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useToastStore } from "@/components/ui/Toast";
import { useUpdateProject, useRenameClass } from "@/hooks/useProjects";
import { useUnsavedWarning } from "@/hooks/useUnsavedWarning";
import {
  projectsApi,
  type ProjectResponse,
  type AttributeField,
  type AttributeSchema,
} from "@/api/projects";
import { AttributeSchemaEditor, validateAttributeFields } from "./AttributeSchemaEditor";
import {
  PrefillFromBackendDialog,
  type PrefillPicked,
  itemToField,
} from "./PrefillFromBackendDialog";
import { ClassEditor, defaultColorFor, type ClassRow } from "./ClassEditor";
import { useCapabilityInstances } from "@/api/mlCapabilities";
import { KeypointSchemaEditor } from "./KeypointSchemaEditor";
import { ToolUnitTabs } from "./ToolUnitTabs";
import { resolveClassVisual, type ClassRefLite } from "./resolveClassVisual";
import type { KeypointSchema } from "@/types";
import { unitBindingsToPayload, useProjectToolBindings } from "./useProjectToolBindings";
import {
  dataTypeFromLegacy,
  getToolUnitGroup,
  type ProjectDataType,
  type ToolUnitId,
} from "@/constants/toolUnits";

export function ClassesSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const update = useUpdateProject(project.id);
  const rename = useRenameClass(project.id);
  const { bindings, setBindings, activeUnit, setActiveUnit, dirty } =
    useProjectToolBindings(project);
  const dataType = projectDataType(project);
  const isVideoGeoUnit =
    dataType === "video" &&
    (activeUnit === "bbox" || activeUnit === "region" || activeUnit === "polyline");
  const videoModeLabels: Record<string, { box: string; track: string }> = {
    bbox: { box: "单帧矩形框", track: "轨迹矩形框" },
    region: { box: "单帧多边形", track: "多边形轨迹" },
    polyline: { box: "单帧折线", track: "折线轨迹" },
  };
  const [renameAllUnits, setRenameAllUnits] = useState(false);
  const [prefillOpen, setPrefillOpen] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);

  useUnsavedWarning(dirty);

  const hasSharedClass = useMemo(() => {
    const counts = new Map<string, number>();
    for (const ub of Object.values(bindings)) {
      if (!ub?.enabled) continue;
      for (const name of new Set(ub.classRows.map((r) => r.name))) {
        counts.set(name, (counts.get(name) ?? 0) + 1);
      }
    }
    for (const c of counts.values()) if (c >= 2) return true;
    return false;
  }, [bindings]);

  const handleRename = (oldName: string, newName: string) => {
    const allUnits = renameAllUnits && hasSharedClass;
    rename.mutate(
      {
        old_name: oldName,
        new_name: newName,
        tool_unit_id: allUnits ? undefined : activeUnit,
      },
      {
        onSuccess: () =>
          pushToast({
            msg: `已重命名「${oldName}」→「${newName}」`,
            sub: allUnits
              ? "已在所有启用工具单位内同步同名类与历史标注"
              : `已同步迁移 ${activeUnit} 工具单位历史标注`,
            kind: "success",
          }),
        onError: (err) =>
          pushToast({
            msg: "重命名失败",
            sub: (err as Error).message,
            kind: "error",
          }),
      },
    );
  };

  const activeBinding = bindings[activeUnit];

  const { data: capInstances } = useCapabilityInstances();
  const recommendedAttrFields = useMemo(() => {
    const LANDING = new Set(["text", "language", "orientation"]);
    const byKey = new Map<string, AttributeField>();
    for (const inst of capInstances?.instances ?? [])
      for (const m of inst.models)
        for (const item of m.output_attribute_schema ?? [])
          if (LANDING.has(item.key) && !byKey.has(item.key)) byKey.set(item.key, itemToField(item));
    const existing = new Set(
      (activeBinding?.attributeFields ?? []).map((f) => f.key).filter(Boolean),
    );
    return [...byKey.values()].filter((f) => !existing.has(f.key));
  }, [capInstances, activeBinding]);

  const linkTargets = useMemo(
    () =>
      (Object.keys(bindings) as ToolUnitId[])
        .filter(
          (u) =>
            u !== activeUnit && bindings[u]?.enabled && (bindings[u]?.classRows.length ?? 0) > 0,
        )
        .map((u) => ({
          unitId: u,
          unitLabel: getToolUnitGroup(u)?.label ?? u,
          classNames: bindings[u]!.classRows.map((r) => r.name),
        })),
    [bindings, activeUnit],
  );

  const resolveLinked = (ref: ClassRefLite) => resolveClassVisual(bindings, { aliasTo: ref });

  const onLink = (rowName: string, ref: ClassRefLite | null) => {
    setBindings((b) => ({
      ...b,
      [activeUnit]: {
        enabled: b[activeUnit]?.enabled ?? true,
        classRows: (b[activeUnit]?.classRows ?? []).map((r) =>
          r.name === rowName ? { ...r, aliasTo: ref ?? undefined } : r,
        ),
        attributeFields: b[activeUnit]?.attributeFields ?? [],
        keypointSchema: b[activeUnit]?.keypointSchema ?? null,
        videoModes: b[activeUnit]?.videoModes ?? null,
      },
    }));
  };

  const onChange = (next: ClassRow[]) => {
    setBindings((b) => ({
      ...b,
      [activeUnit]: {
        enabled: b[activeUnit]?.enabled ?? true,
        classRows: next,
        attributeFields: b[activeUnit]?.attributeFields ?? [],
        keypointSchema: b[activeUnit]?.keypointSchema ?? null,
        videoModes: b[activeUnit]?.videoModes ?? null,
      },
    }));
  };

  const onAttributeChange = (next: AttributeField[]) => {
    setBindings((b) => ({
      ...b,
      [activeUnit]: {
        enabled: b[activeUnit]?.enabled ?? true,
        classRows: b[activeUnit]?.classRows ?? [],
        attributeFields: next,
        keypointSchema: b[activeUnit]?.keypointSchema ?? null,
        videoModes: b[activeUnit]?.videoModes ?? null,
      },
    }));
  };

  const confirmClassDelete = async (row: ClassRow) => {
    try {
      const usage = await projectsApi.classUsage(project.id);
      const count = usage.classes[row.name] ?? 0;
      return await confirmDialog({
        tone: "danger",
        title: `删除类别「${row.name}」`,
        description:
          count > 0
            ? "删除后引用该类别的标注将变为孤儿；暂不影响标注数据，加回同名类别即可恢复。工作台可隐藏孤儿标注，如需彻底清除请运维执行清理。"
            : "该类别暂无标注引用，可放心删除。",
        details: count > 0 ? `已被 ${count} 条标注使用` : undefined,
        confirmLabel: "删除",
      });
    } catch (err) {
      pushToast({
        msg: "删除前用量统计失败",
        sub: (err as Error).message,
        kind: "error",
      });
      return false;
    }
  };

  const confirmAttributeDelete = async (field: AttributeField) => {
    const key = field.key.trim();
    if (!key) return true;
    try {
      const usage = await projectsApi.classUsage(project.id);
      const count = usage.attributes[key] ?? 0;
      return await confirmDialog({
        tone: "danger",
        title: `删除属性「${key}」`,
        description:
          count > 0
            ? "删除后引用该属性的标注值将变为孤儿；暂不影响标注数据，加回相同属性键即可恢复。工作台可隐藏孤儿标注，如需彻底清除请运维执行清理。"
            : "该属性暂无标注引用，可放心删除。",
        details: count > 0 ? `已被 ${count} 条标注使用` : undefined,
        confirmLabel: "删除",
      });
    } catch (err) {
      pushToast({
        msg: "删除前用量统计失败",
        sub: (err as Error).message,
        kind: "error",
      });
      return false;
    }
  };

  const onKeypointSchemaChange = (next: KeypointSchema) => {
    setBindings((b) => ({
      ...b,
      keypoint: {
        enabled: b.keypoint?.enabled ?? true,
        classRows: b.keypoint?.classRows ?? [],
        attributeFields: b.keypoint?.attributeFields ?? [],
        keypointSchema: next,
      },
    }));
  };

  const onToggle = (unit: ToolUnitId, enabled: boolean) => {
    setBindings((b) => ({
      ...b,
      [unit]: {
        enabled,
        classRows: b[unit]?.classRows ?? [],
        attributeFields: b[unit]?.attributeFields ?? [],
        keypointSchema: b[unit]?.keypointSchema ?? null,
        videoModes: b[unit]?.videoModes ?? null,
      },
    }));
  };

  const onToggleVideoMode = (key: "box" | "track", next: boolean) => {
    setBindings((b) => {
      const prev = b[activeUnit];
      const cur = prev?.videoModes ?? { box: true, track: true };
      const updated = { ...cur, [key]: next };
      if (!updated.box && !updated.track) return b;
      return {
        ...b,
        [activeUnit]: {
          enabled: prev?.enabled ?? true,
          classRows: prev?.classRows ?? [],
          attributeFields: prev?.attributeFields ?? [],
          keypointSchema: prev?.keypointSchema ?? null,
          videoModes: updated,
        },
      };
    });
  };

  const onSave = () => {
    for (const k of Object.keys(bindings) as (keyof typeof bindings)[]) {
      const ub = bindings[k];
      if (!ub) continue;
      const willPersist =
        ub.enabled ||
        ub.classRows.length > 0 ||
        ub.attributeFields.length > 0 ||
        !!ub.keypointSchema;
      if (!willPersist) continue;
      const err = validateAttributeFields(ub.attributeFields);
      if (err) {
        pushToast({ msg: `[${k}] ${err}`, kind: "error" });
        return;
      }
    }
    const tool_bindings = unitBindingsToPayload(bindings);
    update.mutate(
      { tool_bindings },
      {
        onSuccess: () => pushToast({ msg: "类别与属性配置已保存", kind: "success" }),
        onError: (err) =>
          pushToast({
            msg: "保存失败",
            sub: (err as Error).message,
            kind: "error",
          }),
      },
    );
  };

  const onExportJson = () => {
    const fields = activeBinding?.attributeFields ?? [];
    const blob = new Blob([JSON.stringify({ fields }, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${project.display_id}-${activeUnit}-attribute-schema.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const onImportJson = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result ?? "")) as AttributeSchema;
        if (!Array.isArray(parsed.fields)) throw new Error("缺少 fields 数组");
        onAttributeChange(parsed.fields);
        pushToast({ msg: `已导入到 ${activeUnit} 工具单位`, kind: "success" });
      } catch (err) {
        pushToast({
          msg: "JSON 格式错误",
          sub: (err as Error).message,
          kind: "error",
        });
      }
    };
    reader.readAsText(f);
    e.target.value = "";
  };

  const onPrefillFromBackend = ({ classes, attributes }: PrefillPicked) => {
    if (attributes.length > 0) {
      const cur = activeBinding?.attributeFields ?? [];
      const importedByKey = new Map(attributes.filter((f) => f.key).map((f) => [f.key, f]));
      const merged = cur.map((f) =>
        f.key && importedByKey.has(f.key) ? importedByKey.get(f.key)! : f,
      );
      const existingKeys = new Set(cur.filter((f) => f.key).map((f) => f.key));
      const additions = attributes.filter((f) => !existingKeys.has(f.key));
      onAttributeChange([...merged, ...additions]);
    }
    let addedClasses = 0;
    if (classes.length > 0) {
      const cur = activeBinding?.classRows ?? [];
      const existing = new Set(cur.map((r) => r.name));
      const newRows = classes
        .filter((n) => !existing.has(n))
        .map((n) => ({ name: n, color: defaultColorFor(n) }));
      addedClasses = newRows.length;
      if (newRows.length > 0) onChange([...cur, ...newRows]);
    }
    const parts: string[] = [];
    if (addedClasses > 0) parts.push(`${addedClasses} 个类别`);
    if (attributes.length > 0) parts.push(`${attributes.length} 个属性`);
    pushToast({
      msg: `已预填${parts.join(" + ")}到 ${activeUnit} 工具单位`,
      sub: "预填仅修改当前编辑内容，请点击「保存」生效",
      kind: "success",
    });
  };

  return (
    <Card>
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3.5">
        <h3 className="text-sm font-semibold">类别与属性</h3>
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          <Button size="sm" variant="ghost" onClick={() => setPrefillOpen(true)}>
            <Icon name="sparkles" />
            从模型服务预填
          </Button>
          <input
            ref={importInputRef}
            type="file"
            accept="application/json"
            onChange={onImportJson}
            className="hidden"
          />
          <DropdownMenu
            panelAriaLabel="属性 JSON 导入导出"
            minWidth={228}
            items={
              [
                {
                  id: "export-attributes",
                  icon: "download",
                  label: "导出属性配置 (JSON)",
                  onSelect: onExportJson,
                },
                {
                  id: "import-attributes",
                  icon: "upload",
                  label: "导入属性配置 (JSON)",
                  onSelect: () => importInputRef.current?.click(),
                },
              ] satisfies DropdownItem[]
            }
            footer={
              <div className="text-2xs leading-snug text-muted-foreground">
                导出当前工具单位的属性编辑内容；导入会替换该单位的全部属性，保存后生效。类别不包含在内。
              </div>
            }
            trigger={({ open, toggle, ref }) => (
              <Button
                ref={ref}
                size="sm"
                variant="ghost"
                onClick={toggle}
                aria-haspopup="menu"
                aria-expanded={open}
              >
                属性 JSON
                <Icon name="chevDown" size={12} />
              </Button>
            )}
          />
        </div>
      </div>
      <div className="flex flex-col gap-2.5 p-4">
        <p className="m-0 text-xs leading-normal text-muted-foreground">
          {dataType === "video"
            ? "点击工具单位后，维护该几何的类别、颜色、排序和属性；单帧与轨迹共用同一单位的属性，不同工具单位（矩形框 / 多边形 / 折线）相互隔离。"
            : "点击工具单位后，直接维护该工具的类别、颜色、排序和属性；同名类在不同工具单位下相互隔离。"}
        </p>
        <ToolUnitTabs
          bindings={bindings}
          activeUnit={activeUnit}
          onSelect={setActiveUnit}
          dataType={dataType}
        />
        {activeBinding && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <Switch
                checked={activeBinding.enabled}
                onChange={(next) => onToggle(activeUnit, next)}
                label={unitSwitchLabel(activeBinding.enabled)}
                data-testid="unit-enabled-switch"
              />
              {!activeBinding.enabled && (
                <span className="text-xs leading-normal text-muted-foreground">
                  {isVideoGeoUnit
                    ? "禁用后单帧与轨迹变体都不可新增；配置仍会保留，需要修改请先启用。"
                    : "禁用后配置仍会保留，但工作台不会使用；需要修改请先启用。"}
                </span>
              )}
            </div>
            {isVideoGeoUnit &&
              activeBinding.enabled &&
              (() => {
                const vm = activeBinding.videoModes ?? { box: true, track: true };
                const onlyBox = vm.box && !vm.track;
                const onlyTrack = !vm.box && vm.track;
                const labels = videoModeLabels[activeUnit] ?? videoModeLabels.bbox;
                return (
                  <div className="mt-2 flex flex-wrap items-center gap-4">
                    <span className="text-sm text-muted-foreground">可用工具</span>
                    <Switch
                      checked={vm.box}
                      onChange={(next) => onToggleVideoMode("box", next)}
                      label={labels.box}
                      disabled={onlyBox}
                      title={onlyBox ? "至少保留一个可用工具" : undefined}
                      data-testid="video-mode-box-switch"
                    />
                    <Switch
                      checked={vm.track}
                      onChange={(next) => onToggleVideoMode("track", next)}
                      label={labels.track}
                      disabled={onlyTrack}
                      title={onlyTrack ? "至少保留一个可用工具" : undefined}
                      data-testid="video-mode-track-switch"
                    />
                  </div>
                );
              })()}
            <fieldset
              className="m-0 min-w-0 border-0 p-0 disabled:pointer-events-none disabled:opacity-[0.55]"
              disabled={!activeBinding.enabled}
              aria-disabled={!activeBinding.enabled}
            >
              <div className="flex flex-col gap-3.5">
                <section className="min-w-0">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                    <h4 className="m-0 text-xs font-semibold text-muted-foreground">类别</h4>
                    {hasSharedClass && (
                      <Switch
                        checked={renameAllUnits}
                        onChange={setRenameAllUnits}
                        label="重命名时同步所有工具单位的同名类"
                        title="开启后重命名会改动所有启用工具单位中的同名类；关闭则仅改当前工具单位（强隔离默认）。"
                        data-testid="rename-all-units-switch"
                      />
                    )}
                  </div>
                  <ClassEditor
                    value={activeBinding.classRows}
                    onChange={onChange}
                    onRename={handleRename}
                    renaming={rename.isPending}
                    onConfirmDelete={confirmClassDelete}
                    linkTargets={linkTargets}
                    resolveLinked={resolveLinked}
                    onLink={onLink}
                  />
                </section>
                {activeUnit === "keypoint" && (
                  <section className="min-w-0">
                    <h4 className="mb-2 mt-0 text-xs font-semibold text-muted-foreground">
                      关键点骨骼
                    </h4>
                    <KeypointSchemaEditor
                      value={activeBinding.keypointSchema}
                      onChange={onKeypointSchemaChange}
                    />
                  </section>
                )}
                <section className="min-w-0">
                  <h4 className="mb-2 mt-0 text-xs font-semibold text-muted-foreground">
                    属性 schema
                  </h4>
                  <AttributeSchemaEditor
                    value={activeBinding.attributeFields}
                    onChange={onAttributeChange}
                    onConfirmDelete={confirmAttributeDelete}
                    recommendedFields={recommendedAttrFields}
                  />
                </section>
              </div>
            </fieldset>
          </>
        )}
        <div className="flex items-center justify-end gap-3">
          {dirty && (
            <span
              className="inline-flex items-center gap-1.5 text-xs font-medium text-status-caution"
              data-testid="unsaved-indicator"
            >
              <span className="size-1.5 rounded-full bg-amber-500" />
              有未保存的修改
            </span>
          )}
          <Button variant="primary" disabled={!dirty || update.isPending} onClick={onSave}>
            {update.isPending ? "保存中..." : "保存"}
          </Button>
        </div>
      </div>
      <PrefillFromBackendDialog
        open={prefillOpen}
        onClose={() => setPrefillOpen(false)}
        projectId={project.id}
        onPrefill={onPrefillFromBackend}
        targetUnitLabel={getToolUnitGroup(activeUnit)?.label ?? activeUnit}
        existingClassNames={(activeBinding?.classRows ?? []).map((r) => r.name)}
        existingAttrKeys={(activeBinding?.attributeFields ?? []).map((f) => f.key).filter(Boolean)}
      />
    </Card>
  );
}

function projectDataType(project: ProjectResponse): ProjectDataType {
  if (
    project.data_type === "image" ||
    project.data_type === "video" ||
    project.data_type === "lidar"
  ) {
    return project.data_type;
  }
  return dataTypeFromLegacy(project.type_key);
}

function unitSwitchLabel(enabled: boolean): string {
  return `${enabled ? "已启用" : "已禁用"}此工具单位`;
}
