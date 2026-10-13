import { useEffect, useMemo, useRef, useState } from "react";
import type { ClassesConfig } from "@/api/projects";
import { classColor } from "../stage/colors";

const RECENT_BUTTON_CLASS =
  "flex cursor-pointer appearance-none items-center gap-1 rounded-full border border-border bg-background px-2 py-1 text-xs text-foreground";
const CLASS_ROW_BASE =
  "flex cursor-pointer items-center gap-2 rounded-sm border border-transparent px-2 py-1.5 text-sm";
const SHORTCUT_CLASS =
  "inline-block rounded-[3px] border border-b-2 border-border bg-muted px-1.5 py-px font-mono text-2xs leading-none text-muted-foreground";

interface ClassPaletteProps {
  classes: string[];
  recent?: string[];
  activeClass?: string | null;
  /** v0.5.4：项目级 classes_config，决定每个类别的颜色覆盖。空时回落 hash。 */
  classesConfig?: ClassesConfig;
  /** readOnly 时点击无效，仅作为图例 + 快捷键速查（左侧常驻面板用）。 */
  onPick?: (cls: string) => void;
  /** 是否启用搜索框（默认：类别 > 9 时自动启用） */
  enableSearch?: boolean;
  /** 高亮第 N 个（用于键盘导航；undefined = 跟随 activeClass） */
  highlightIndex?: number;
  /** 紧凑模式（popover 内使用） */
  dense?: boolean;
  /** 纯预览模式：行不响应点击；hover 不变色；鼠标 default。 */
  readOnly?: boolean;
  /** 键盘导航接入：过滤结果（与查询原文）变化时回调（popover 用它驱动 Up/Down/Enter）。 */
  onFilteredChange?: (filtered: string[], query: string) => void;
}

/** 类别直选只有十个数字槽：1-9 + 0（按工具绑定单元配置顺序取前十个）。 */
export const CLASS_SHORTCUT_SLOTS = 10;

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(" ");
}

export function shortcutForIndex(idx: number): string {
  if (idx < 0 || idx >= CLASS_SHORTCUT_SLOTS) return "";
  return idx === 9 ? "0" : String(idx + 1);
}

export function ClassPalette({
  classes,
  recent = [],
  activeClass,
  classesConfig,
  onPick,
  enableSearch,
  highlightIndex,
  dense = false,
  readOnly = false,
  onFilteredChange,
}: ClassPaletteProps) {
  const handlePick = (c: string) => {
    if (readOnly) return;
    onPick?.(c);
  };
  const [query, setQuery] = useState("");
  const showSearch = enableSearch ?? classes.length > 9;
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setQuery("");
  }, [classes]);

  const filtered = useMemo(() => {
    if (!query.trim()) return classes;
    const q = query.toLowerCase();
    return classes.filter((c) => c.toLowerCase().includes(q));
  }, [classes, query]);

  useEffect(() => {
    onFilteredChange?.(filtered, query);
  }, [filtered, query, onFilteredChange]);

  // 键盘高亮行保持在可视范围内（类别多时 popover 内部滚动）。
  useEffect(() => {
    if (typeof highlightIndex !== "number") return;
    const row = listRef.current?.children[highlightIndex] as HTMLElement | undefined;
    row?.scrollIntoView({ block: "nearest" });
  }, [highlightIndex]);

  // recent 只展示当前项目存在的类别
  const visibleRecent = useMemo(
    () => recent.filter((c) => classes.includes(c)).slice(0, 5),
    [recent, classes],
  );

  return (
    <div className={cn("flex flex-col", dense ? "gap-1.5" : "gap-2")}>
      {showSearch && (
        <input
          data-class-picker-search=""
          ref={inputRef}
          autoFocus={dense}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索类别..."
          className="appearance-none rounded-sm border border-border bg-background px-2 py-1.5 text-xs text-foreground outline-none"
        />
      )}

      {visibleRecent.length > 0 && !query.trim() && (
        <div>
          <div className="mb-1 text-2xs tracking-[0.5px] text-muted-foreground">最近使用</div>
          <div className="flex flex-wrap gap-1">
            {visibleRecent.map((c) => (
              <button
                key={`recent-${c}`}
                type="button"
                onClick={readOnly ? undefined : () => handlePick(c)}
                disabled={readOnly}
                className={cn(RECENT_BUTTON_CLASS, readOnly && "cursor-default")}
              >
                <svg className="size-2 flex-[0_0_auto]" viewBox="0 0 8 8" aria-hidden="true">
                  <rect width="8" height="8" rx="2" fill={classColor(c, classesConfig)} />
                </svg>
                {c}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col gap-px" ref={listRef}>
        {filtered.map((c) => {
          const idx = classes.indexOf(c);
          const sk = shortcutForIndex(idx);
          const isActive = activeClass === c;
          const isHighlighted =
            typeof highlightIndex === "number" && filtered.indexOf(c) === highlightIndex;
          return (
            <div
              key={c}
              onClick={readOnly ? undefined : () => handlePick(c)}
              className={cn(
                CLASS_ROW_BASE,
                dense && "gap-1.5 py-1",
                readOnly && "cursor-default opacity-[0.92]",
                !readOnly && isActive && "bg-muted",
                isHighlighted && "border-brand/40 bg-brand/10",
              )}
            >
              <svg className="size-2.5 flex-[0_0_auto]" viewBox="0 0 10 10" aria-hidden="true">
                <rect width="10" height="10" rx="2" fill={classColor(c, classesConfig)} />
              </svg>
              <span className="flex-1 truncate">{c}</span>
              {sk && <span className={SHORTCUT_CLASS}>{sk}</span>}
            </div>
          );
        })}
        {filtered.length === 0 && (
          <div className="px-1 py-2 text-center text-xs text-muted-foreground">没有匹配的类别</div>
        )}
      </div>
    </div>
  );
}
