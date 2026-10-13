import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AttributeSchema } from "@/api/projects";
import type { Viewport } from "../state/useViewportTransform";
import { AttributeForm } from "./AttributeForm";
import { ClassPalette, shortcutForIndex } from "./ClassPalette";

const POPOVER_CLASS =
  "top-[var(--class-picker-top)] left-[var(--class-picker-left)] min-w-[220px] max-w-[280px] max-h-[70vh] overflow-y-auto rounded-md border border-border bg-card p-2.5 shadow-lg [pointer-events:auto]";

type Geom = { x: number; y: number; w: number; h: number };
type FixedAnchor = { left: number; top: number };
export type ClassPickerCancelReason = "escape" | "outside";

/** v0.11.28：改类悬浮框内联的属性编辑（与类别选择二合一，单列堆叠）。 */
export type ClassPickerAttrEditing = {
  schema: AttributeSchema;
  attributes: Record<string, unknown>;
  context: "image" | "video";
  readOnly?: boolean;
  onChange: (next: Record<string, unknown>) => void;
};

type CommonProps = {
  classes: string[];
  recent: string[];
  defaultClass: string;
  title?: string;
  onPick: (cls: string) => void;
  onCancel: (reason: ClassPickerCancelReason) => void;
  /** 传入时在类别选择下方渲染属性表单（className 跟随当前 defaultClass 联动刷新可见字段）。 */
  attrEditing?: ClassPickerAttrEditing;
};

type ImagePositionProps = CommonProps & {
  position?: "image";
  geom: Geom;
  imgW: number;
  imgH: number;
  vp: Viewport;
};

type FixedPositionProps = CommonProps & {
  position: "fixed";
  anchor: FixedAnchor;
};

type ClassPickerPopoverProps = ImagePositionProps | FixedPositionProps;

/**
 * 画框完成后的类别选择 popover。
 * - image 模式锚定到框左下角；fixed 模式使用调用方给出的 viewport 坐标
 * - 数字 1-9 + 0 直选前十个类别（搜索框内输入数字仍是文本）；↑/↓ 在过滤结果间移动，
 *   Enter 选中高亮项；查询为空时 Enter 维持默认类别；Esc 取消；点外部取消
 */
export function ClassPickerPopover({
  classes,
  recent,
  defaultClass,
  title = "选择类别",
  onPick,
  onCancel,
  attrEditing,
  ...positionProps
}: ClassPickerPopoverProps) {
  const ref = useRef<HTMLDivElement>(null);
  const resolvedDefaultClass = classes.includes(defaultClass) ? defaultClass : (classes[0] ?? "");
  // 键盘导航状态：filteredRef/queryRef 由 ClassPalette 回调同步；highlightRef 供按键处理读取。
  const filteredRef = useRef(classes);
  const queryRef = useRef("");
  const highlightRef = useRef<number | null>(null);
  const [highlightIndex, setHighlightIndex] = useState<number | null>(null);

  const moveHighlight = (dir: 1 | -1) => {
    const len = filteredRef.current.length;
    if (len === 0) return;
    setHighlightIndex((prev) => {
      if (prev === null) return dir === 1 ? 0 : len - 1;
      return Math.max(0, Math.min(len - 1, prev + dir));
    });
  };

  const isFixed = positionProps.position === "fixed";
  // image 模式：框左下角（容器坐标）；fixed 模式：调用方传 viewport/client 坐标。
  const left = isFixed
    ? positionProps.anchor.left
    : positionProps.geom.x * positionProps.imgW * positionProps.vp.scale + positionProps.vp.tx;
  const top = isFixed
    ? positionProps.anchor.top
    : (positionProps.geom.y + positionProps.geom.h) * positionProps.imgH * positionProps.vp.scale +
      positionProps.vp.ty +
      6;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let l = left;
    let t = top;
    // fixed 模式（改类 / SAM / 批量等由调用方给 viewport 坐标的场景）做视口边界 clamp：
    // 锚点来自列表里的触发按钮时，原始坐标常会让 popover 溢出右侧或底部，需拉回视口内，
    // 必要时翻转到锚点上方。image 模式锚定画布内的框，维持原行为不 clamp。
    if (isFixed && typeof window !== "undefined") {
      const margin = 8;
      const rect = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      if (l + rect.width > vw - margin) l = vw - margin - rect.width;
      if (l < margin) l = margin;
      if (t + rect.height > vh - margin) {
        const flipped = top - rect.height - 12; // 翻到锚点上方
        t = flipped >= margin ? flipped : Math.max(margin, vh - margin - rect.height);
      }
      if (t < margin) t = margin;
    }
    el.style.setProperty("--class-picker-left", `${l}px`);
    el.style.setProperty("--class-picker-top", `${t}px`);
  }, [left, top, isFixed]);

  // keyboard
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      // IME 组合中的按键永不确认类别。
      if (e.isComposing || e.keyCode === 229) return;
      const inEditable =
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLSelectElement ||
        e.target instanceof HTMLTextAreaElement;
      if (
        inEditable &&
        (e.key === "ArrowDown" || e.key === "ArrowUp") &&
        !(e.target instanceof HTMLElement && e.target.hasAttribute("data-class-picker-search"))
      )
        return;
      if (
        inEditable &&
        e.key !== "Escape" &&
        e.key !== "Enter" &&
        e.key !== "ArrowDown" &&
        e.key !== "ArrowUp"
      ) {
        return; // 让搜索框 / 属性表单控件正常输入，不抢数字快捷键
      }
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel("escape");
        return;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        moveHighlight(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        if (queryRef.current.trim()) {
          // 有查询：Enter 确认高亮结果；无结果不选择。
          const idx = highlightRef.current;
          const picked = idx !== null ? filteredRef.current[idx] : undefined;
          if (picked) onPick(picked);
          return;
        }
        if (resolvedDefaultClass) onPick(resolvedDefaultClass);
        return;
      }
      // 数字 1-9 + 0 直选前十槽；搜索框等可编辑控件内数字保持文本输入。
      const isDigit =
        (e.key >= "1" && e.key <= "9" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey) ||
        (e.key === "0" && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey);
      if (isDigit && !inEditable) {
        const idx = e.key === "0" ? 9 : parseInt(e.key, 10) - 1;
        if (classes[idx]) {
          e.preventDefault();
          onPick(classes[idx]);
        }
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [classes, resolvedDefaultClass, onPick, onCancel]);

  // 过滤结果 / 查询变化时同步引用并复位高亮（移动高亮不改变过滤结果）。
  const handleFilteredChange = useCallback((filtered: string[], query: string) => {
    const changed = filtered !== filteredRef.current;
    filteredRef.current = filtered;
    queryRef.current = query;
    if (changed) {
      highlightRef.current = null;
      setHighlightIndex(null);
    }
  }, []);
  useEffect(() => {
    highlightRef.current = highlightIndex;
  }, [highlightIndex]);

  // click outside to cancel
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      if (ref.current && !ref.current.contains(e.target as Node)) onCancel("outside");
    };
    // 用 pointerdown(而非 mousedown):视频 Konva 画布的 pointerdown 处理会 preventDefault,
    // 抑制兼容性 mousedown。且用捕获阶段绑定:画布命中由 Konva/Stage 接管,冒泡阶段可能被
    // cancelBubble/停止传播而收不到;捕获阶段从 document 向下最先触发,谁都拦不住。
    // 延迟绑定,避免捕获到打开弹窗那次 down。
    const t = setTimeout(() => document.addEventListener("pointerdown", onDown, true), 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [onCancel]);

  const content = (
    <div
      ref={ref}
      data-testid="class-picker-popover"
      className={`${POPOVER_CLASS} ${isFixed ? "fixed z-overlay-high" : "absolute z-popover"}`}
      onMouseDown={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs font-semibold">{title}</div>
        <div className="text-2xs text-muted-foreground">Enter ↵ 默认 · Esc 取消</div>
      </div>
      <ClassPalette
        classes={classes}
        recent={recent}
        activeClass={resolvedDefaultClass}
        onPick={onPick}
        dense
        enableSearch={classes.length > 9}
        highlightIndex={highlightIndex ?? undefined}
        onFilteredChange={handleFilteredChange}
      />
      {classes.length === 0 && (
        <div className="p-2 text-center text-xs text-muted-foreground">该项目尚未配置类别</div>
      )}
      {classes.length > 0 && (
        <div className="mt-2 text-center text-2xs text-muted-foreground">
          {classes.length <= 10
            ? `快捷键: 1…${shortcutForIndex(classes.length - 1)} · Enter ↵ 默认`
            : "快捷键: 1-9、0 直选前十类 · 其余用搜索或点击 · ↑↓ 选择 · Enter ↵ 确认"}
        </div>
      )}
      {attrEditing && (
        <div className="mt-1">
          <AttributeForm
            schema={attrEditing.schema}
            className={resolvedDefaultClass}
            attributes={attrEditing.attributes}
            onChange={attrEditing.onChange}
            readOnly={attrEditing.readOnly}
            context={attrEditing.context}
          />
        </div>
      )}
    </div>
  );
  // Viewport anchors must escape the stage's stacking context and selection card.
  return isFixed ? createPortal(content, document.body) : content;
}
