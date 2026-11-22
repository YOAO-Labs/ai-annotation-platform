import { useState, type CSSProperties } from "react";
import { MAX_NAMED_WORKSPACE_PRESETS, MAX_WORKSPACE_PRESET_NAME_LENGTH } from "@/api/auth";
import type { DropdownItem } from "@/components/ui/DropdownMenu";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";
import type { WorkbenchNamedPreset } from "../state/useWorkbenchNamedPresets";
import {
  createWorkspacePreset,
  WORKSPACE_PRESETS,
  type ActiveWorkspacePreset,
} from "./workbenchLayoutPresets";
import { PANEL_TITLES, type WorkspaceContext, type WorkspaceNode } from "./workbenchLayoutSnapshot";

const PREVIEW_BOUNDS = { width: 1600, height: 900 };

const CONTEXT_LABELS: Record<WorkspaceContext, string> = {
  "annotate:image": "图片标注",
  "annotate:video": "视频标注",
  "annotate:3d": "点云标注",
  "review:image": "图片审核",
  "review:video": "视频审核",
  "review:3d": "点云审核",
};

export interface NamedPresetControls {
  presets: WorkbenchNamedPreset[];
  context: WorkspaceContext;
  full: boolean;
  count: number;
  busy: boolean;
  disabled: boolean;
  onSave: (name: string) => void;
  onApply: (preset: WorkbenchNamedPreset) => void;
  onRename: (id: string, name: string) => void;
  onRemove: (preset: WorkbenchNamedPreset) => void;
}

interface WorkbenchLayoutQuickMenuProps {
  items: DropdownItem[];
  activePreset: ActiveWorkspacePreset;
  namedPresets: NamedPresetControls;
  close: () => void;
  onOpenSettings?: () => void;
}

const SMALL_ACTION_CLASS =
  "inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground active:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";
const TEXT_FIELD_CLASS =
  "min-w-0 flex-1 rounded-md border border-border bg-card px-2 py-1.5 text-sm text-foreground [font:inherit] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const QUICK_MENU_ACTION_CLASS =
  "flex w-full min-w-0 items-center gap-2 rounded-sm px-2.5 py-2 text-left text-sm text-muted-foreground hover:bg-accent hover:text-foreground active:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

export function WorkbenchLayoutQuickMenu({
  items,
  activePreset,
  namedPresets,
  close,
  onOpenSettings,
}: WorkbenchLayoutQuickMenuProps) {
  const [name, setName] = useState("");
  const applicable = namedPresets.presets.filter(
    (preset) => preset.snapshot !== null && preset.context === namedPresets.context,
  );
  const overwrites = namedPresets.presets.some(
    (preset) => preset.snapshot !== null && preset.name.trim() === name.trim(),
  );
  const saveBlocked =
    namedPresets.disabled ||
    namedPresets.busy ||
    (namedPresets.full && !overwrites) ||
    !name.trim();
  const save = () => {
    if (saveBlocked) return;
    namedPresets.onSave(name);
    close();
  };

  return (
    <div className="w-full p-1">
      <div className="space-y-0.5">
        {items.map((item) => {
          const selected = activePreset === item.id;
          return (
            <button
              key={item.id}
              type="button"
              aria-pressed={selected}
              disabled={item.disabled}
              className={cn(
                QUICK_MENU_ACTION_CLASS,
                selected && "bg-accent font-semibold text-foreground",
              )}
              onClick={() => {
                item.onSelect?.();
                close();
              }}
            >
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {selected && <Icon name="check" size={12} className="shrink-0 text-brand" />}
            </button>
          );
        })}
      </div>

      <div className="my-1 h-px bg-border" />
      <div className="space-y-2 px-2.5 py-2">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium text-foreground">我的预设</span>
          <span className="text-2xs text-muted-foreground">
            {namedPresets.count} / {MAX_NAMED_WORKSPACE_PRESETS}
          </span>
        </div>
        {applicable.length > 0 ? (
          <div className="space-y-0.5">
            {applicable.map((preset) => (
              <button
                key={preset.id}
                type="button"
                aria-label={`应用预设 ${preset.name}`}
                disabled={namedPresets.disabled || namedPresets.busy}
                className={cn(QUICK_MENU_ACTION_CLASS, "-mx-2.5 w-[calc(100%+1.25rem)]")}
                onClick={() => {
                  namedPresets.onApply(preset);
                  close();
                }}
              >
                <Icon name="grid" size={13} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate text-foreground">{preset.name}</span>
                <span className="shrink-0 text-2xs">应用</span>
              </button>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">当前工作类型暂无预设</p>
        )}
        <form
          className="flex items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <input
            autoFocus
            value={name}
            aria-label="快速保存预设名称"
            placeholder="预设名称"
            maxLength={MAX_WORKSPACE_PRESET_NAME_LENGTH}
            disabled={namedPresets.disabled}
            className={cn(TEXT_FIELD_CLASS, "h-8 py-1 text-xs")}
            onChange={(event) => setName(event.target.value)}
          />
          <button
            type="submit"
            aria-label="保存当前布局为预设"
            title={overwrites ? "覆盖同名预设" : "保存当前布局为预设"}
            disabled={saveBlocked}
            className="inline-flex h-8 shrink-0 items-center justify-center gap-1 rounded-md bg-brand px-2 text-xs text-primary-foreground hover:bg-brand/90 active:bg-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Icon name="save" size={13} />
            {overwrites ? "覆盖" : "保存"}
          </button>
        </form>
        {namedPresets.full && !overwrites && (
          <p className="text-2xs text-status-caution">已存满；输入已有名称可覆盖。</p>
        )}
        {namedPresets.presets.length > applicable.length && (
          <p className="text-2xs text-muted-foreground">其他工作类型的预设请在更多设置中管理。</p>
        )}
      </div>

      <div className="my-1 h-px bg-border" />
      <button
        type="button"
        className={QUICK_MENU_ACTION_CLASS}
        onClick={() => {
          close();
          onOpenSettings?.();
        }}
      >
        <Icon name="settings" size={13} className="shrink-0" />
        <span className="flex-1">更多布局设置…</span>
      </button>
    </div>
  );
}

function SavedPresetRow({
  preset,
  controls,
}: {
  preset: WorkbenchNamedPreset;
  controls: NamedPresetControls;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const restorable = preset.snapshot !== null;
  const applicable = restorable && preset.context === controls.context;
  if (confirming)
    return (
      <li className="flex flex-wrap items-center gap-2 rounded-lg border border-status-danger bg-status-danger-soft px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-sm">删除「{preset.name}」？</span>
        <button
          type="button"
          className={cn(SMALL_ACTION_CLASS, "text-status-danger")}
          disabled={controls.busy}
          onClick={() => {
            controls.onRemove(preset);
            setConfirming(false);
          }}
        >
          确认删除
        </button>
        <button type="button" className={SMALL_ACTION_CLASS} onClick={() => setConfirming(false)}>
          取消
        </button>
      </li>
    );
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
      {draft === null ? (
        <>
          <span className="min-w-0 flex-1 truncate text-sm">{preset.name}</span>
          <span className="text-xs text-muted-foreground">{CONTEXT_LABELS[preset.context]}</span>
          <button
            type="button"
            className={SMALL_ACTION_CLASS}
            disabled={controls.disabled || controls.busy || !applicable}
            title={
              restorable
                ? applicable
                  ? undefined
                  : `保存于${CONTEXT_LABELS[preset.context]}，切换到该工作类型后可应用`
                : "这份预设无法恢复，只能删除"
            }
            onClick={() => controls.onApply(preset)}
          >
            应用
          </button>
          <button
            type="button"
            className={SMALL_ACTION_CLASS}
            disabled={controls.busy || !restorable}
            title={restorable ? undefined : "这份预设无法恢复，只能删除"}
            onClick={() => setDraft(preset.name)}
          >
            重命名
          </button>
          <button
            type="button"
            aria-label={`删除预设 ${preset.name}`}
            className={cn(SMALL_ACTION_CLASS, "hover:text-status-danger")}
            disabled={controls.busy}
            onClick={() => setConfirming(true)}
          >
            <Icon name="trash" size={13} />
            删除
          </button>
        </>
      ) : (
        <>
          <input
            autoFocus
            value={draft}
            aria-label={`重命名预设 ${preset.name}`}
            maxLength={MAX_WORKSPACE_PRESET_NAME_LENGTH}
            className={TEXT_FIELD_CLASS}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && draft.trim()) {
                controls.onRename(preset.id, draft);
                setDraft(null);
              }
              if (event.key === "Escape") setDraft(null);
            }}
          />
          <button
            type="button"
            className={SMALL_ACTION_CLASS}
            disabled={!draft.trim() || controls.busy}
            onClick={() => {
              controls.onRename(preset.id, draft);
              setDraft(null);
            }}
          >
            保存名称
          </button>
          <button type="button" className={SMALL_ACTION_CLASS} onClick={() => setDraft(null)}>
            取消
          </button>
        </>
      )}
    </li>
  );
}

function SavedPresets({ controls }: { controls: NamedPresetControls }) {
  const [name, setName] = useState("");
  const overwrites = controls.presets.some(
    (preset) => preset.snapshot !== null && preset.name.trim() === name.trim(),
  );
  const blocked = controls.disabled || controls.busy || (controls.full && !overwrites);
  const submit = () => {
    if (blocked || !name.trim()) return;
    controls.onSave(name);
    setName("");
  };
  return (
    <div className="space-y-3 border-t border-border pt-4">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-md font-medium">我的预设</h3>
        <span className="text-xs text-muted-foreground">
          {controls.count} / {MAX_NAMED_WORKSPACE_PRESETS}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        把当前排列另存为可反复套用的布局，随账号保存，最多 {MAX_NAMED_WORKSPACE_PRESETS} 组。
        应用时需要与预设保存时的工作类型一致。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={name}
          aria-label="预设名称"
          placeholder="预设名称"
          maxLength={MAX_WORKSPACE_PRESET_NAME_LENGTH}
          disabled={controls.disabled}
          className={TEXT_FIELD_CLASS}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
        />
        <button
          type="button"
          disabled={blocked || !name.trim()}
          onClick={submit}
          className="inline-flex items-center gap-1.5 rounded-md bg-brand px-3 py-1.5 text-xs text-primary-foreground hover:bg-brand/90 active:bg-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Icon name="save" size={13} />
          {overwrites ? "覆盖同名预设" : "保存当前布局"}
        </button>
      </div>
      {controls.full && !overwrites && (
        <p className="text-xs text-status-caution">
          已保存 {MAX_NAMED_WORKSPACE_PRESETS} 组，请先删除一组再保存新布局。
        </p>
      )}
      {controls.presets.length > 0 && (
        <ul className="space-y-2">
          {controls.presets.map((preset) => (
            <SavedPresetRow key={preset.id} preset={preset} controls={controls} />
          ))}
        </ul>
      )}
    </div>
  );
}

function PreviewNode({ node, horizontal }: { node: WorkspaceNode; horizontal: boolean }) {
  if (node.visible === false) return null;
  if (node.type === "leaf") {
    if (node.data.id === "parking") return null;
    const panel = node.data.activeView ?? node.data.views[0];
    return (
      <span
        // eslint-disable-next-line no-restricted-syntax -- Preview weights come from the preset tree.
        style={{ "--preview-flex": `${node.size || 1} 1 0%` } as CSSProperties}
        className="relative min-h-0 min-w-0 flex-[var(--preview-flex)]"
      >
        <span
          className={`absolute inset-px flex items-center justify-center overflow-hidden rounded-sm border py-0.5 text-center text-2xs leading-tight ${panel === "canvas" ? "border-brand/30 bg-brand/10 text-brand" : "border-border bg-muted text-muted-foreground"}`}
        >
          {PANEL_TITLES[panel]}
        </span>
      </span>
    );
  }
  return (
    <span
      // eslint-disable-next-line no-restricted-syntax -- Preview weights come from the preset tree.
      style={{ "--preview-flex": `${node.size || 1} 1 0%` } as CSSProperties}
      className={`flex flex-[var(--preview-flex)] min-h-0 min-w-0 ${horizontal ? "flex-row" : "flex-col"}`}
    >
      {node.data.map((child, index) => (
        <PreviewNode key={index} node={child} horizontal={!horizontal} />
      ))}
    </span>
  );
}

export function WorkbenchLayoutSettings({
  items,
  activePreset,
  namedPresets,
}: {
  items: DropdownItem[];
  activePreset: ActiveWorkspacePreset;
  namedPresets?: NamedPresetControls;
}) {
  const presets = WORKSPACE_PRESETS.filter((preset) => items.some((item) => item.id === preset.id));
  const panels = items.filter((item) => item.id in PANEL_TITLES);
  const tools = items.filter(
    (item) =>
      item.id.startsWith("3d-") || item.id === "camera-presentation" || item.id === "reset-cameras",
  );
  const reset = items.find((item) => item.id === "reset");
  const card = (selected: boolean) =>
    cn(
      "relative flex min-w-0 flex-col gap-3 rounded-lg border p-3 text-left",
      selected ? "border-brand bg-brand/5 ring-1 ring-brand" : "border-border bg-card",
    );
  const selection = (selected: boolean) => (
    <span
      aria-hidden="true"
      className={cn(
        "flex size-4 shrink-0 items-center justify-center rounded-full border",
        selected ? "border-brand bg-brand text-primary-foreground" : "border-border",
      )}
    >
      {selected && <Icon name="check" size={11} />}
    </span>
  );
  return (
    <section aria-label="工作台布局" className="space-y-5 border-b border-border py-4">
      <div>
        <h3 className="text-md font-medium">布局预设</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          按 16:9 屏幕比例预览，高亮项为当前布局。切换预设后可撤销。
        </p>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {presets.map((preset) => {
          const item = items.find((item) => item.id === preset.id)!;
          const snapshot = createWorkspacePreset(preset.id, PREVIEW_BOUNDS);
          const selected = activePreset === preset.id;
          return (
            <button
              key={preset.id}
              type="button"
              aria-pressed={selected}
              disabled={item.disabled}
              onClick={item.onSelect}
              className={cn(
                card(selected),
                "hover:border-brand active:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              <span aria-hidden="true" className="relative flex aspect-video w-full shrink-0">
                {preset.id === "focus" ? (
                  <span className="absolute inset-px flex items-center justify-center rounded-sm border border-brand/30 bg-brand/10 text-2xs text-brand">
                    画布
                  </span>
                ) : (
                  <PreviewNode
                    node={snapshot.layout.grid.root}
                    horizontal={snapshot.layout.grid.orientation === "HORIZONTAL"}
                  />
                )}
              </span>
              <span className="flex w-full items-center justify-between gap-2 text-sm font-medium">
                {preset.title}
                {selection(selected)}
              </span>
            </button>
          );
        })}
        <div
          aria-label="自定义布局"
          aria-current={activePreset === "custom" ? "true" : undefined}
          className={card(activePreset === "custom")}
        >
          <span
            aria-hidden="true"
            className="flex aspect-video w-full shrink-0 items-center justify-center rounded-md border border-dashed border-border bg-muted/30 text-muted-foreground"
          >
            <Icon name="grid" size={28} />
          </span>
          <span className="flex items-center justify-between gap-2 text-sm font-medium">
            自定义布局{selection(activePreset === "custom")}
          </span>
          <p className="text-xs text-muted-foreground">面板排列或显隐不匹配预设时自动选中</p>
        </div>
      </div>
      {namedPresets && <SavedPresets controls={namedPresets} />}
      <details className="group border-t border-border pt-3">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-md py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
          <span>面板与高级布局</span>
          <span className="flex items-center gap-2 text-xs font-normal text-muted-foreground">
            {panels.filter((item) => item.active).length} / {panels.length} 已显示
            <Icon name="chevDown" size={14} className="group-open:rotate-180" />
          </span>
        </summary>
        <div className="space-y-4 pt-2">
          <div className="grid grid-cols-1 gap-x-5 sm:grid-cols-2">
            {panels.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-label={typeof item.label === "string" ? item.label : undefined}
                aria-pressed={!!item.active}
                disabled={item.disabled}
                onClick={item.onSelect}
                className="flex min-w-0 items-center gap-2.5 rounded-md px-2 py-2.5 text-left text-sm hover:bg-accent active:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                <Icon
                  name={item.active ? "eye" : "eyeOff"}
                  size={15}
                  className={item.active ? "text-brand" : "text-muted-foreground"}
                />
                <span className="flex-1">{item.label}</span>
                <span className="text-xs text-muted-foreground">
                  {item.active ? "已显示" : "已隐藏"}
                </span>
              </button>
            ))}
          </div>
          {tools.length > 0 && (
            <div className="space-y-2 border-t border-border pt-3">
              <p className="text-xs text-muted-foreground">点云视图</p>
              <div className="flex flex-wrap gap-2">
                {tools.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    disabled={item.disabled}
                    onClick={item.onSelect}
                    className="rounded-md bg-muted px-3 py-2 text-xs hover:bg-accent active:bg-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {reset && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
              <p className="text-xs text-muted-foreground">恢复默认面板排列</p>
              <button
                type="button"
                disabled={reset.disabled}
                onClick={reset.onSelect}
                className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground active:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                <Icon name="rotate-ccw" size={13} />
                {reset.label}
              </button>
            </div>
          )}
        </div>
      </details>
    </section>
  );
}
