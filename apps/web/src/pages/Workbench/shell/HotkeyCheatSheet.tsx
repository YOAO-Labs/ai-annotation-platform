// 工作台快捷键面板（Increment A 参考界面 + Increment B 账号级自定义）。
//
// 与工作台设置 / 标注指引同一套对话框几何（md: 1120×min(820px,85dvh)，220px 导航列，
// 仅右内容区滚动）；分类为用途导向，工作台类型用显式过滤器（当前工作台 / 全部类型）。
// 可编辑命令经 useWorkbenchShortcutPreferences 读写账号偏好；Fixed 命令展示并参与
// 冲突盘点（见 state/hotkeyBindings.ts）。挂载期记住选中分类；查询词关闭即清空，
// 不写入账号偏好。

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, RotateCcw, Search, X } from "lucide-react";
import type { AttributeSchema } from "@/api/projects";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { HighlightText } from "@/components/ui/HighlightText";
import { Button } from "@/components/shadcn/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/shadcn/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import {
  EDITABLE_COMMAND_IDS,
  bindingKeyLabels,
  commandDomain,
  findBindingConflicts,
  getEditableCommand,
  modLabel,
  normalizeRecordedEvent,
  resolveEffectiveCommands,
  type EffectiveCommandState,
  type ShortcutBinding,
  type ShortcutOverridesByDomain,
} from "../state/hotkeyBindings";
import type { WorkbenchShortcutPreferencesState } from "../state/useWorkbenchShortcutPreferences";
import {
  HOTKEYS,
  HOTKEY_CATEGORIES,
  HOTKEY_CATEGORY_LABEL,
  HOTKEY_STAGE_LABEL,
  type HotkeyCategory,
  type HotkeyDef,
  type HotkeyStage,
} from "../state/hotkeys";
import {
  WORKBENCH_DIALOG_CONTENT_CLASS,
  workbenchDialogOverlayClass,
} from "./workbenchDialogClasses";

const KBD_CLASS =
  "whitespace-nowrap rounded-[3px] border border-b-2 border-border bg-muted px-1.5 py-px font-mono text-xs leading-normal text-foreground";
// 每行固定为「主行 + 说明/操作行」两级内容槽，说明行即使为空也占位，
// 且两级统一 min-h-5 / leading-5，使所有命令的行高与分隔线间距完全一致。
const ROW_CLASS = "flex items-start justify-between gap-3 border-b border-border py-2 text-sm";
const PRIMARY_STACK_CLASS = "flex min-h-12 min-w-0 flex-col gap-1";
const PRIMARY_LINE_CLASS =
  "flex min-h-5 min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 leading-5 text-foreground [overflow-wrap:anywhere]";
const NOTE_LINE_CLASS =
  "flex min-h-5 min-w-0 flex-wrap items-center gap-x-1.5 text-xs leading-5 text-muted-foreground";
const KEYS_LINE_CLASS = "flex min-h-5 min-w-0 flex-wrap items-center justify-end gap-1";
const ACTIONS_LINE_CLASS = "flex min-h-5 items-center justify-end gap-1";
const NAME_CLASS = "min-w-0 leading-5 text-foreground [overflow-wrap:anywhere]";
// 跨阶段视图(全部类型 / 搜索)按图片 / 视频 / 点云着色,便于快速区分适用范围。
const STAGE_CHIP_CLASS: Record<HotkeyStage, string> = {
  image: "bg-stage-image-soft text-stage-image",
  video: "bg-stage-video-soft text-stage-video",
  threed: "bg-stage-threed-soft text-stage-threed",
};
const STAGE_TEXT_CLASS: Record<HotkeyStage, string> = {
  image: "text-stage-image",
  video: "text-stage-video",
  threed: "text-stage-threed",
};
const SECTION_TITLE_CLASS =
  "sticky top-0 z-local-1 mb-2 border-b border-border bg-card py-1.5 text-xs font-bold uppercase tracking-[0.04em] text-foreground";

interface HotkeyCheatSheetProps {
  open: boolean;
  onClose: () => void;
  /** 项目级属性 schema：含 hotkey 的字段在「选择与编辑」内以专属分组展示。 */
  attributeSchema?: AttributeSchema;
  /** 当前工作台类型：驱动「当前工作台」过滤与常用视图范围。 */
  stageKind?: "image" | "video" | "3d";
  /** Increment B · 账号级快捷键偏好（生效表 + 写路径）；未提供时仅展示参考。 */
  shortcuts?: WorkbenchShortcutPreferencesState;
}

type StageFilter = "current" | "all";

function stageTypeOf(kind: HotkeyCheatSheetProps["stageKind"]): HotkeyStage {
  return kind === "3d" ? "threed" : (kind ?? "image");
}

function rowStages(h: HotkeyDef): HotkeyStage[] {
  return h.stages ?? ["image", "video", "threed"];
}

/** keys 数组里的修饰键标签（与 bindingKeyLabels / modLabel 一致）。 */
const MODIFIER_KEY_LABELS = new Set(["Ctrl", "Cmd", "⌘", "Shift", "Alt"]);

/** keys 数组的展示键帽（最后一个元素为「或」候选项时按 keysAlt 拆分）。 */
function staticKeyGroups(h: HotkeyDef): string[][] {
  if (h.keysLabel) return [[h.keysLabel]];
  if (h.keysAlt && h.keys.length > 1) {
    const head = h.keys.slice(0, -1);
    const alts = h.keys[-1 + h.keys.length];
    // 共享的前导修饰键要在候选项上重复，否则 [Ctrl, Delete, Backspace] 会写成
    // 「Ctrl+Delete 或 Backspace」，把 Ctrl+Backspace 误述成裸 Backspace。
    const shared: string[] = [];
    for (const key of head) {
      if (!MODIFIER_KEY_LABELS.has(key)) break;
      shared.push(key);
    }
    return [head, [...shared, alts]];
  }
  return [h.keys];
}

function KeyGroup({ group, separator }: { group: string[]; separator?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      {group.map((k, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          {i > 0 && <span className="text-2xs text-muted-foreground">+</span>}
          <kbd className={KBD_CLASS}>{k}</kbd>
        </span>
      ))}
      {separator && <span className="text-2xs text-muted-foreground">或</span>}
    </span>
  );
}

interface BindingCellProps {
  bindings: ShortcutBinding[];
  disabled: boolean;
}

/** 生效组合展示：主组合与备用组合用「或」连接；停用显示专用键帽。 */
function BindingCell({ bindings, disabled }: BindingCellProps) {
  if (disabled) {
    return (
      <span className="inline-flex items-center">
        <kbd className={`${KBD_CLASS} text-muted-foreground`}>已停用</kbd>
      </span>
    );
  }
  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-1">
      {bindings.map((b, i) => (
        <KeyGroup key={i} group={bindingKeyLabels(b)} separator={i < bindings.length - 1} />
      ))}
    </span>
  );
}

interface RecordState {
  commandId: string;
  slot: 0 | 1;
}

const RECORDER_REASON_TEXT: Record<string, string> = {
  ime: "输入法组合中的按键不能录制",
  "modifier-only": "不能只录制修饰键，请加上一个字符或命名键",
  browser: "该组合由浏览器 / 系统保留，无法被工作台接管",
  physical: "该键位是物理键位例外（如反引号 / Tab / Enter），保持固定",
  altgr: "AltGr 字符输入不参与快捷键",
  unsupported: "只支持单字符或方向键 / 删除键 / F1–F11 等命名键",
};

export function HotkeyCheatSheet({
  open,
  onClose,
  attributeSchema,
  stageKind,
  shortcuts,
}: HotkeyCheatSheetProps) {
  const [query, setQuery] = useState("");
  // 挂载期记住选中分类（关闭不清空）；查询词在重新打开时清空。
  const [category, setCategory] = useState<string>("common");
  const [stageFilter, setStageFilter] = useState<StageFilter>("current");
  const [modifiedOnly, setModifiedOnly] = useState(false);
  const [recording, setRecording] = useState<RecordState | null>(null);
  const [candidate, setCandidate] = useState<ShortcutBinding | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const candidateActionRef = useRef<HTMLSpanElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const composingRef = useRef(false);
  const overlayPointerRef = useRef(false);

  const currentStage = stageTypeOf(stageKind);
  const desktop = useMediaQuery("(min-width: 768px)");
  const editing = !!shortcuts?.loaded && !shortcuts.loadError;
  const effective = shortcuts?.previewEffective ?? shortcuts?.effective;
  const modKey = modLabel();

  const cancelRecording = () => {
    setRecording(null);
    setCandidate(null);
    setCandidateError(null);
  };

  useEffect(() => {
    setRecording(null);
    setCandidate(null);
    setCandidateError(null);
    setActionError(null);
  }, [category, stageFilter, query, modifiedOnly, stageKind]);

  useEffect(() => {
    if (!open) {
      setQuery("");
      setRecording(null);
      setCandidate(null);
      setCandidateError(null);
      setModifiedOnly(false);
      composingRef.current = false;
    }
  }, [open]);

  // 录制监听：capture 阶段吞掉按键，录制中的组合不触发任何后台动作。
  useEffect(() => {
    if (!recording || candidate) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (e.key === "Escape") {
        setRecording(null);
        setCandidate(null);
        setCandidateError(null);
        return;
      }
      const { binding, reason } = normalizeRecordedEvent(e);
      if (!binding || reason) {
        setCandidate(null);
        setCandidateError(
          reason ? (RECORDER_REASON_TEXT[reason] ?? "该按键不能录制") : "该按键不能录制",
        );
        return;
      }
      setCandidate(binding);
      setCandidateError(null);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording, candidate]);

  useEffect(() => {
    if (recording && candidate) candidateActionRef.current?.querySelector("button")?.focus();
  }, [recording, candidate]);

  const attributeItems = useMemo(
    () =>
      (attributeSchema?.fields ?? []).filter(
        (f) => !!f.hotkey && (f.type === "boolean" || f.type === "select"),
      ),
    [attributeSchema],
  );

  const matchesStage = (h: HotkeyDef) =>
    stageFilter === "all" || rowStages(h).includes(currentStage);

  const rowSearchText = (h: HotkeyDef) => {
    const bindings = effective?.get(h.id)?.bindings;
    const keysText = bindings
      ? bindings.map((binding) => bindingKeyLabels(binding).join("+")).join(" ")
      : (h.keysLabel ?? h.keys.join(" "));
    const stageText = rowStages(h)
      .map((s) => HOTKEY_STAGE_LABEL[s])
      .join(" ");
    return [
      h.desc,
      h.applies ?? "",
      h.note ?? "",
      keysText,
      stageText,
      HOTKEY_CATEGORY_LABEL[h.category],
    ]
      .join(" ")
      .toLowerCase();
  };

  const effectiveState = (id: string): EffectiveCommandState | undefined => effective?.get(id);

  const isPending = (id: string) => !!shortcuts?.pendingKeys.has(`${commandDomain(id)}:${id}`);
  const failureOf = (id: string) => shortcuts?.failedMap.get(`${commandDomain(id)}:${id}`);

  const q = query.trim().toLowerCase();
  const matchesQuery = (h: HotkeyDef) => !q || rowSearchText(h).includes(q);
  const isModified = (id: string) => {
    const state = effectiveState(id);
    return !!state && (state.customized || state.disabled);
  };

  const visibleRows = (targetCategory: string): HotkeyDef[] => {
    let rows = HOTKEYS.filter((h) => matchesStage(h) && matchesQuery(h));
    // 项目类别数字键在选择分类下由专属「项目类别与属性」分组渲染，避免重复；
    // 搜索模式下回到通用结果流。
    if (!q)
      rows = rows.filter((h) => h.id !== "image.category.digit" && h.id !== "video.category.digit");
    if (targetCategory === "common") rows = rows.filter((h) => h.common);
    else rows = rows.filter((h) => h.category === targetCategory);
    if (modifiedOnly) rows = rows.filter((h) => isModified(h.id));
    return rows;
  };

  const categoryDescriptions: Record<string, string> = {
    common: "精选的高频动作，按当前工作台类型过滤；不代表个人使用频率。",
    draw: "选择与切换标注工具，画布绘制入口。",
    selection: "对象选择、状态位与项目类别 / 属性快捷键。",
    canvas: "缩放、适应视口与视图重置。",
    ai: "AI 工具、候选采纳与置信度。",
    playback: "视频播放、帧与关键帧导航、轨迹状态。",
    task: "切题、提交与系统入口。",
    mouse: "滚轮、拖拽与组合点击手势。",
  };

  const conflictingTargets = (commandId: string, bindings: ShortcutBinding[]) => {
    if (!effective) return [];
    return findBindingConflicts({ commandId, bindings, effective });
  };

  const startRecording = (commandId: string, slot: 0 | 1) => {
    setActionError(null);
    setRecording({ commandId, slot });
    setCandidate(null);
    setCandidateError(null);
  };

  const confirmRecording = () => {
    if (!recording || !candidate || !shortcuts) return;
    const state = effectiveState(recording.commandId);
    const current = state?.bindings ?? getEditableCommand(recording.commandId)?.bindings ?? [];
    const other = recording.slot === 0 ? current[1] : current[0];
    if (
      other &&
      other.key === candidate.key &&
      other.modifiers.join(",") === candidate.modifiers.join(",")
    ) {
      setCandidateError("该组合已分配给本命令的另一组合");
      return;
    }
    const next =
      recording.slot === 0 ? [candidate, ...current.slice(1)] : [...current.slice(0, 1), candidate];
    const conflicts = conflictingTargets(recording.commandId, next);
    if (conflicts.length > 0) return; // 确认按钮已被禁用；保守拦截
    shortcuts.saveCommand({
      domain: commandDomain(recording.commandId),
      commandId: recording.commandId,
      bindings: next,
    });
    setRecording(null);
    setCandidate(null);
  };

  const restoreCommands = (ids: string[]) => {
    if (!shortcuts || !effective) return;
    const overrides: ShortcutOverridesByDomain = { common: {}, image: {}, video: {} };
    for (const [id, state] of effective) overrides[commandDomain(id)][id] = state.bindings;
    for (const id of ids) overrides[commandDomain(id)][id] = null;
    const next = resolveEffectiveCommands(overrides);
    for (const id of ids) {
      const conflicts = findBindingConflicts({
        commandId: id,
        bindings: next.get(id)!.bindings,
        effective: next,
      });
      const overlapping = next.get(id)!.conflictsWith;
      if (conflicts.length || overlapping.length) {
        const labels = conflicts.length
          ? conflicts.map((item) => item.targetLabel)
          : overlapping.map((other) => getEditableCommand(other)?.label ?? other);
        setActionError(
          `无法恢复「${getEditableCommand(id)?.label}」：与「${labels.join("、")}」冲突，请先修改占用组合。`,
        );
        return;
      }
    }
    setActionError(null);
    for (const id of ids)
      shortcuts.saveCommand({ domain: commandDomain(id), commandId: id, bindings: null });
  };

  const setCommandDisabled = (commandId: string, disabled: boolean) => {
    if (!disabled) return restoreCommands([commandId]);
    setActionError(null);
    shortcuts?.saveCommand({ domain: commandDomain(commandId), commandId, bindings: [] });
  };

  const resetDomain = (domain: "common" | "image" | "video") => {
    restoreCommands(
      [...(effective ?? [])]
        .filter(
          ([id, state]) => commandDomain(id) === domain && (state.customized || state.disabled),
        )
        .map(([id]) => id),
    );
  };

  const navigateToCommand = (commandId: string) => {
    const def = getEditableCommand(commandId);
    if (def) setCategory(def.category);
    requestAnimationFrame(() => {
      document.getElementById(`hotkey-row-${commandId}`)?.scrollIntoView({ block: "center" });
    });
  };

  const close = () => {
    setRecording(null);
    onClose();
  };

  // 候选组合的冲突（录制中实时展示；确认按钮据此禁用）。
  const candidateConflicts = useMemo(() => {
    if (!recording || !candidate || !effective) return [];
    const state = effectiveState(recording.commandId);
    const current = state?.bindings ?? getEditableCommand(recording.commandId)?.bindings ?? [];
    const next =
      recording.slot === 0 ? [candidate, ...current.slice(1)] : [...current.slice(0, 1), candidate];
    return findBindingConflicts({ commandId: recording.commandId, bindings: next, effective });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording, candidate, effective]);

  const saveState = (() => {
    if (!shortcuts) return null;
    if (shortcuts.failedMap.size > 0) return { kind: "error" as const, text: "保存失败" };
    if (shortcuts.pendingKeys.size > 0) return { kind: "pending" as const, text: "正在保存…" };
    return { kind: "saved" as const, text: "更改自动保存" };
  })();

  const renderRow = (h: HotkeyDef) => {
    const editable = EDITABLE_COMMAND_IDS.has(h.id);
    const state = editable ? effectiveState(h.id) : undefined;
    const def = editable ? getEditableCommand(h.id) : undefined;
    const disabled = !!state?.disabled;
    const failed = failureOf(h.id);
    const pending = isPending(h.id);
    const isRecordingRow = recording?.commandId === h.id;
    const stageChips =
      stageFilter === "all" && h.stages ? (
        <span className="inline-flex gap-1">
          {h.stages.map((s) => (
            <span
              key={s}
              data-hotkey-stage={s}
              className={`inline-flex items-center gap-1 rounded-full px-1.5 py-px text-2xs ${STAGE_CHIP_CLASS[s]}`}
            >
              <span aria-hidden="true" className="size-1 rounded-full bg-current" />
              {HOTKEY_STAGE_LABEL[s]}
            </span>
          ))}
        </span>
      ) : null;

    return (
      <div key={h.id} id={`hotkey-row-${h.id}`} className={ROW_CLASS} data-hotkey-command={h.id}>
        <div className={PRIMARY_STACK_CLASS}>
          <span className={PRIMARY_LINE_CLASS}>
            <HighlightText text={h.desc} query={query} />
            {state?.customized && (
              <span className="rounded-full bg-brand/10 px-1.5 py-px text-2xs font-medium text-brand">
                已自定义
              </span>
            )}
            {disabled && (
              <span className="rounded-full bg-muted px-1.5 py-px text-2xs text-muted-foreground">
                已停用
              </span>
            )}
            {!editable && (
              <span
                className="text-2xs text-muted-foreground"
                title="该命令由对应监听方固定，暂不支持改绑"
              >
                固定
              </span>
            )}
            {state && state.conflictsWith.length > 0 && (
              <span
                className="inline-flex items-center gap-0.5 rounded-full bg-status-danger-soft px-1.5 py-px text-2xs text-status-danger"
                title="与其他命令争用同一按键，已暂停执行；请在面板中修正"
              >
                <AlertTriangle aria-hidden="true" className="size-3" />
                冲突
              </span>
            )}
            {h.applies && (
              <span className="text-xs text-muted-foreground">
                <HighlightText text={h.applies} query={query} />
              </span>
            )}
          </span>
          <span className={NOTE_LINE_CLASS} data-hotkey-note="">
            {stageChips}
            {h.note && <HighlightText text={h.note} query={query} />}
            {pending && <span>正在保存…</span>}
            {failed && (
              <span className="inline-flex items-center gap-1 text-status-danger">
                保存失败
                <button
                  type="button"
                  className="underline hover:text-foreground"
                  onClick={() => shortcuts?.retrySave(commandDomain(h.id), h.id)}
                >
                  重试
                </button>
                <button
                  type="button"
                  className="underline hover:text-foreground"
                  onClick={() => shortcuts?.discardPending(commandDomain(h.id), h.id)}
                >
                  放弃
                </button>
              </span>
            )}
          </span>
        </div>
        <div className="flex max-w-[46%] flex-none flex-col items-end gap-1">
          {editable && state && def ? (
            isRecordingRow ? (
              <div className="flex flex-col items-end gap-1">
                <span className="inline-flex min-h-6 items-center gap-1 text-xs text-muted-foreground">
                  {candidate ? (
                    <BindingCell bindings={[candidate]} disabled={false} />
                  ) : (
                    "按下新组合…"
                  )}
                  <span className="text-2xs">Esc 取消</span>
                </span>
                {candidateError && (
                  <span className="text-xs text-status-danger">{candidateError}</span>
                )}
                {candidate && (
                  <span ref={candidateActionRef} className="flex gap-1">
                    {candidateConflicts.length === 0 && (
                      <Button size="xs" onClick={confirmRecording}>
                        确认
                      </Button>
                    )}
                    <Button
                      size="xs"
                      variant="outline"
                      onClick={() => {
                        setCandidate(null);
                        setCandidateError(null);
                      }}
                    >
                      重录
                    </Button>
                  </span>
                )}
                {candidate &&
                  candidateConflicts.map((c) => (
                    <span key={c.targetId} className="text-right text-xs text-status-danger">
                      与「{c.targetLabel}」冲突：{c.applies}，两者会同时生效。
                      <button
                        type="button"
                        className="ml-1 underline hover:text-foreground"
                        onClick={() => navigateToCommand(c.targetId)}
                      >
                        查看
                      </button>
                    </span>
                  ))}
              </div>
            ) : (
              <div className="flex flex-col items-end gap-1">
                <span className={KEYS_LINE_CLASS}>
                  <button
                    type="button"
                    className="inline-flex flex-wrap items-center justify-end gap-1 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    title={`点击修改主组合（${modKey} 为平台主修饰键）`}
                    onClick={() => startRecording(h.id, 0)}
                    disabled={!editing}
                  >
                    <BindingCell bindings={state.bindings.slice(0, 1)} disabled={disabled} />
                  </button>
                </span>
                <div className={ACTIONS_LINE_CLASS}>
                  {state.bindings.length > 1 ? (
                    <button
                      type="button"
                      className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      title="点击修改备用组合"
                      onClick={() => startRecording(h.id, 1)}
                      disabled={!editing}
                    >
                      <BindingCell bindings={state.bindings.slice(1)} disabled={disabled} />
                    </button>
                  ) : (
                    !disabled && (
                      <Button
                        size="xs"
                        variant="ghost"
                        className="h-5 px-1.5 text-2xs text-muted-foreground"
                        onClick={() => startRecording(h.id, 1)}
                        disabled={!editing}
                      >
                        添加备用
                      </Button>
                    )
                  )}
                  <Button
                    size="xs"
                    variant="ghost"
                    className="h-5 px-1.5 text-2xs"
                    onClick={() => setCommandDisabled(h.id, !disabled)}
                    disabled={!editing}
                  >
                    {disabled ? "启用" : "停用"}
                  </Button>
                  {(state.customized || disabled) && (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="h-5 px-1.5 text-2xs text-muted-foreground"
                      onClick={() => setCommandDisabled(h.id, false)}
                      disabled={!editing}
                    >
                      恢复默认
                    </Button>
                  )}
                </div>
              </div>
            )
          ) : (
            <span className={`${KEYS_LINE_CLASS} max-w-[220px]`}>
              {h.keysLabel ? (
                <kbd className={KBD_CLASS}>{h.keysLabel}</kbd>
              ) : h.keysAlt ? (
                (() => {
                  const groups = staticKeyGroups(h);
                  return (
                    <>
                      {groups[0].length > 0 && <KeyGroup group={groups[0]} />}
                      <span className="text-2xs text-muted-foreground">或</span>
                      <KeyGroup group={groups[1]} />
                    </>
                  );
                })()
              ) : (
                h.keys.map((k, j) => (
                  <kbd key={j} className={KBD_CLASS}>
                    {k}
                  </kbd>
                ))
              )}
            </span>
          )}
        </div>
      </div>
    );
  };

  const projectSection = (targetCategory: string) => {
    if (targetCategory !== "selection" || modifiedOnly) return null;
    const digitRows = q
      ? []
      : HOTKEYS.filter(
          (h) => h.id === "image.category.digit" || h.id === "video.category.digit",
        ).filter((h) => matchesStage(h) && matchesQuery(h));
    const showAttributes = stageFilter === "all" || currentStage === "image";
    const filteredAttr = attributeItems.filter((f) => {
      if (!q) return true;
      return f.label.toLowerCase().includes(q) || (f.hotkey ?? "").toLowerCase().includes(q);
    });
    if (digitRows.length === 0 && (!showAttributes || filteredAttr.length === 0)) return null;
    return (
      <div className="mt-4 rounded-lg border border-border bg-muted/30 p-3">
        <div className="text-xs font-bold uppercase tracking-[0.04em] text-foreground">
          项目类别与属性
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          数字 1-9 与 0 按项目类别配置顺序直选前十个类别；其余类别用类别面板搜索或点击。
          类别弹层打开时数字键归弹层消费。
        </p>
        <div className="mt-2">{digitRows.map(renderRow)}</div>
        {showAttributes && filteredAttr.length > 0 && (
          <>
            <div className="mt-3 text-xs font-semibold text-foreground">属性快捷键（图片）</div>
            <p className="mt-1 text-xs text-muted-foreground">
              在属性编辑器的「属性快捷键」区域聚焦后按数字键切换 / 循环属性值；
              焦点离开区域立即恢复画布类别键。
            </p>
            <div className="mt-2">
              {filteredAttr.map((f) => (
                <div key={f.key} className={ROW_CLASS}>
                  <span className={NAME_CLASS}>
                    {f.type === "boolean" ? "切换 " : "循环 "}
                    <span className="font-medium">
                      <HighlightText text={f.label} query={query} />
                    </span>
                    <span className="ml-2 text-xs text-muted-foreground">属性快捷键区域聚焦时</span>
                  </span>
                  <kbd className={KBD_CLASS}>{f.hotkey}</kbd>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    );
  };

  const searchNoResult =
    q.length > 0 &&
    HOTKEYS.every(
      (h) => !matchesStage(h) || !matchesQuery(h) || (modifiedOnly && !isModified(h.id)),
    ) &&
    (modifiedOnly ||
      (stageFilter === "current" && currentStage !== "image") ||
      attributeItems.length === 0 ||
      attributeItems.every((f) => {
        const hit = f.label.toLowerCase().includes(q) || (f.hotkey ?? "").toLowerCase().includes(q);
        return !hit;
      }));

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
    >
      <DialogContent
        ref={contentRef}
        showCloseButton={false}
        aria-describedby={undefined}
        data-testid="workbench-hotkeys-dialog"
        data-workbench-hotkeys=""
        className={WORKBENCH_DIALOG_CONTENT_CLASS}
        overlayProps={{
          // 点云(3D)画布不模糊遮罩,避免软件渲染下拖慢合成清理。
          className: workbenchDialogOverlayClass(stageKind !== "3d"),
          "data-testid": "workbench-hotkeys-overlay",
          "data-workbench-hotkeys": "",
          onPointerDown: (event) => {
            overlayPointerRef.current = event.target === event.currentTarget;
          },
          onClick: (event) => {
            if (event.target === event.currentTarget && overlayPointerRef.current) {
              event.preventDefault();
              event.stopPropagation();
              overlayPointerRef.current = false;
              // 未确认的录制先取消，不关闭面板；已确认写入由偏好写路径负责。
              if (recording) {
                setRecording(null);
                setCandidate(null);
                setCandidateError(null);
                return;
              }
              close();
            }
          },
        }}
        onPointerDownCapture={(event) => {
          overlayPointerRef.current = false;
          const row =
            event.target instanceof Element ? event.target.closest("[data-hotkey-command]") : null;
          if (recording && row?.getAttribute("data-hotkey-command") !== recording.commandId)
            cancelRecording();
        }}
        onPointerDownOutside={(event) => event.preventDefault()}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          returnFocusRef.current =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
          searchRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const previous = returnFocusRef.current;
          const trigger =
            previous?.isConnected && previous !== document.body
              ? previous
              : document.querySelector<HTMLElement>('button[aria-label="快捷键"]');
          trigger?.focus();
        }}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={() => {
          composingRef.current = false;
        }}
        onEscapeKeyDown={(event) => {
          if (event.isComposing || composingRef.current) {
            event.preventDefault();
            return;
          }
          // 录制中 Esc 只取消录制，面板保持打开。
          if (recording) {
            event.preventDefault();
            setRecording(null);
            setCandidate(null);
            setCandidateError(null);
          }
        }}
      >
        <DialogTitle className="sr-only">快捷键</DialogTitle>
        <Tabs
          value={category}
          onValueChange={(next) => {
            setCategory(next);
            if (scrollRef.current) scrollRef.current.scrollTop = 0;
          }}
          orientation={desktop ? "vertical" : "horizontal"}
          className="min-h-0 flex-1 gap-0"
        >
          <aside className="flex shrink-0 flex-col gap-3 border-b border-border bg-muted/40 p-4 pt-[max(16px,env(safe-area-inset-top))] md:w-[220px] md:border-b-0 md:border-r md:pt-4">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-semibold">快捷键</span>
              <Button variant="ghost" size="icon" aria-label="关闭快捷键面板" onClick={close}>
                <X />
              </Button>
            </div>
            <div className="relative">
              <Search
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
              />
              {/* React 18 的 Input 组件不转发 ref；搜索框要吃 onOpenAutoFocus 的程序化聚焦，用原生 input。 */}
              <input
                ref={searchRef}
                aria-label="搜索快捷键"
                placeholder="搜索动作 / 按键 / 生效条件…"
                value={query}
                onFocus={cancelRecording}
                className="h-9 w-full appearance-none rounded-md border border-border bg-background pl-9 pr-8 text-sm text-foreground outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                onChange={(event) => {
                  setQuery(event.target.value);
                  if (scrollRef.current) scrollRef.current.scrollTop = 0;
                }}
              />
              {query && (
                <button
                  type="button"
                  aria-label="清空搜索"
                  onClick={() => setQuery("")}
                  className="absolute right-0 top-0 flex h-9 w-8 items-center justify-center rounded-md text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="size-3.5" />
                </button>
              )}
            </div>
            <div
              role="radiogroup"
              aria-label="工作台类型"
              className="flex rounded-md border border-border p-0.5 text-xs"
            >
              {(
                [
                  ["current", `当前工作台（${HOTKEY_STAGE_LABEL[currentStage]}）`],
                  ["all", "全部类型"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-label={
                    value === "current"
                      ? `当前工作台（${HOTKEY_STAGE_LABEL[currentStage]}）`
                      : label
                  }
                  aria-checked={stageFilter === value}
                  onClick={() => setStageFilter(value)}
                  className={`flex-1 rounded-[4px] px-2 py-1 transition-none ${
                    stageFilter === value
                      ? "bg-card font-medium text-foreground shadow-none"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {value === "current" ? (
                    <>
                      {"当前工作台（"}
                      <span className={`font-medium ${STAGE_TEXT_CLASS[currentStage]}`}>
                        {HOTKEY_STAGE_LABEL[currentStage]}
                      </span>
                      {"）"}
                    </>
                  ) : (
                    label
                  )}
                </button>
              ))}
            </div>
            <TabsList
              aria-label="快捷键分类"
              className="h-auto w-full justify-start gap-1 overflow-x-auto bg-transparent p-0 md:flex-col md:items-stretch"
            >
              {HOTKEY_CATEGORIES.map((item) => (
                <TabsTrigger
                  key={item}
                  value={item}
                  onClick={() => {
                    if (q && item === category) setCategory(item);
                  }}
                  className="h-9 flex-none justify-start px-3 transition-none data-[state=active]:bg-card data-[state=active]:shadow-none"
                >
                  {HOTKEY_CATEGORY_LABEL[item]}
                </TabsTrigger>
              ))}
            </TabsList>
            {editing && (
              <label className="flex cursor-pointer select-none items-center gap-1.5 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={modifiedOnly}
                  onChange={(e) => setModifiedOnly(e.target.checked)}
                />
                仅看已修改
              </label>
            )}
          </aside>
          <TabsContent value={category} className="m-0 flex min-h-0 min-w-0 flex-1 flex-col">
            <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-4 md:px-8 md:py-5">
              <div className="flex min-w-0 flex-col gap-1">
                <h2 className="text-lg font-semibold">
                  {q ? "搜索结果" : (HOTKEY_CATEGORY_LABEL[category as HotkeyCategory] ?? category)}
                </h2>
                <p className="text-xs text-muted-foreground">
                  {q ? "在所选类型范围内搜索全部分类。" : (categoryDescriptions[category] ?? "")}
                </p>
              </div>
              <span
                className="hidden shrink-0 text-xs text-muted-foreground md:inline"
                title="录制的组合按 KeyboardEvent.key 匹配；修饰键要求完全一致"
              >
                {modKey} = 平台主修饰键
              </span>
            </header>
            <div
              ref={scrollRef}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3 md:px-8 md:py-4"
            >
              {actionError && (
                <div role="alert" className="mb-3 text-xs text-status-danger">
                  {actionError}
                </div>
              )}
              {shortcuts && shortcuts.opaque && (
                <div className="mb-3 flex items-start gap-2 rounded-lg border border-status-danger/40 bg-status-danger-soft p-3 text-xs text-status-danger">
                  <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 flex-none" />
                  <span>
                    检测到无法识别的快捷键数据（可能来自更新版本）。相关覆盖已暂时停用，
                    原始值已保留，可在修正后再保存。
                  </span>
                </div>
              )}
              {shortcuts && !shortcuts.opaque && shortcuts.issues.length > 0 && (
                <div className="mb-3 flex items-start gap-2 rounded-lg border border-status-danger/40 bg-status-danger-soft p-3 text-xs text-status-danger">
                  <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 flex-none" />
                  <span>
                    {shortcuts.issues.length} 条快捷键覆盖无法执行（未知命令或格式无效），
                    已保留原值并停用，不影响其他快捷键。
                  </span>
                </div>
              )}
              {shortcuts?.loadError ? (
                <div className="py-8 text-center">
                  <p className="text-sm text-muted-foreground">无法加载快捷键偏好</p>
                  <Button variant="outline" className="mt-2" onClick={() => shortcuts.retryLoad()}>
                    重试
                  </Button>
                </div>
              ) : searchNoResult ? (
                <div className="py-8 text-center">
                  <p className="text-sm text-muted-foreground">没有匹配的快捷键</p>
                  <div className="mt-2 flex justify-center gap-2">
                    <Button variant="outline" size="sm" onClick={() => setQuery("")}>
                      清空搜索
                    </Button>
                    {stageFilter === "current" && (
                      <Button variant="outline" size="sm" onClick={() => setStageFilter("all")}>
                        在全部类型中搜索
                      </Button>
                    )}
                  </div>
                </div>
              ) : q ? (
                // 搜索跨所选类型范围内的全部分类，并展示分类上下文；常用为精选聚合，搜索时按各自归属分类去重。
                <>
                  {HOTKEY_CATEGORIES.flatMap((cat) => {
                    if (cat === "common") return [];
                    const rows = visibleRows(cat);
                    if (rows.length === 0) return [];
                    return [
                      <section key={cat} className="mb-4">
                        <div className={SECTION_TITLE_CLASS}>
                          <HighlightText text={HOTKEY_CATEGORY_LABEL[cat]} query={query} />
                        </div>
                        {rows.map(renderRow)}
                      </section>,
                    ];
                  })}
                  {projectSection("selection")}
                </>
              ) : (
                <>
                  {visibleRows(category).map(renderRow)}
                  {projectSection(category)}
                </>
              )}
            </div>
            <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-3 pb-[max(12px,env(safe-area-inset-bottom))] text-xs text-muted-foreground md:px-8">
              <span
                className={
                  saveState?.kind === "error"
                    ? "text-status-danger"
                    : saveState?.kind === "pending"
                      ? "text-foreground"
                      : undefined
                }
                role="status"
              >
                {saveState?.text ?? "仅展示参考，不修改绑定"}
              </span>
              {editing && currentStage !== "threed" && (
                <span className="flex flex-wrap gap-2">
                  <Button
                    variant="ghost"
                    size="xs"
                    className="h-6 px-2 text-2xs text-muted-foreground"
                    onClick={() => resetDomain(currentStage === "video" ? "video" : "image")}
                  >
                    <RotateCcw data-icon="inline-start" className="size-3" />
                    重置{currentStage === "video" ? "视频" : "图片"}快捷键
                  </Button>
                  <Button
                    variant="ghost"
                    size="xs"
                    className="h-6 px-2 text-2xs text-muted-foreground"
                    onClick={() => resetDomain("common")}
                  >
                    <RotateCcw data-icon="inline-start" className="size-3" />
                    重置常用快捷键
                  </Button>
                </span>
              )}
            </footer>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
