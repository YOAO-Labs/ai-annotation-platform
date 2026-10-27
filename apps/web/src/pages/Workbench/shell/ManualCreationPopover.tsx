import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useId, useLayoutEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import type { AttributeSchema } from "@/api/projects";
import { Button } from "@/components/ui/Button";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import { AttributeForm } from "./AttributeForm";
import { ClassPickerPopover } from "./ClassPickerPopover";

export interface ManualCreationPopoverProps {
  id: string;
  anchor: { left: number; top: number };
  classes: string[];
  recent: string[];
  className: string;
  schema: AttributeSchema;
  attributes: Record<string, unknown>;
  requiredKeys: string[];
  phase: "class" | "attributes" | "saving" | "error";
  error?: string;
  onPickClass: (cls: string) => void;
  onChangeAttributes: (next: Record<string, unknown>) => void;
  onSubmit: () => void;
  onCancel: () => void;
  /** Preserve the existing safe-mode outside action; continuous drafts omit it. */
  onOutside?: () => void;
}

const POPOVER_CLASS =
  "fixed left-[var(--manual-creation-left)] top-[var(--manual-creation-top)] z-overlay-high flex max-h-[calc(100vh-16px)] w-72 max-w-[calc(100vw-16px)] flex-col gap-2 overflow-y-auto overscroll-contain rounded-md border border-border bg-card p-2.5 text-foreground shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function ownsTextInput(target: EventTarget | null): target is HTMLElement {
  return (
    target instanceof HTMLElement &&
    (target.matches("input, textarea, select") || target.isContentEditable)
  );
}

export function ManualCreationPopover({
  id,
  anchor,
  classes,
  recent,
  className,
  schema,
  attributes,
  requiredKeys,
  phase,
  error,
  onPickClass,
  onChangeAttributes,
  onSubmit,
  onCancel,
  onOutside,
}: ManualCreationPopoverProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const latestRef = useRef({ phase, onSubmit, onCancel });
  latestRef.current = { phase, onSubmit, onCancel };
  const titleId = useId();
  const hintId = useId();
  const saving = phase === "saving";
  const visibleSchema = useMemo(() => {
    const revealed = new Set(requiredKeys);
    return { ...schema, fields: (schema.fields ?? []).filter((field) => revealed.has(field.key)) };
  }, [schema, requiredKeys]);

  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const position = () => {
      const margin = 8;
      const rect = panel.getBoundingClientRect();
      const left = Math.max(margin, Math.min(anchor.left, window.innerWidth - rect.width - margin));
      const maxTop = Math.max(margin, window.innerHeight - rect.height - margin);
      const flippedTop = anchor.top - rect.height - 12;
      const preferredTop =
        anchor.top + rect.height > window.innerHeight - margin
          ? flippedTop >= margin
            ? flippedTop
            : maxTop
          : anchor.top;
      const top = Math.max(margin, Math.min(preferredTop, maxTop));
      panel.style.setProperty("--manual-creation-left", `${left}px`);
      panel.style.setProperty("--manual-creation-top", `${top}px`);
    };
    position();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(position);
    observer?.observe(panel);
    window.addEventListener("resize", position);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", position);
    };
  }, [anchor.left, anchor.top, id, phase]);

  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    if (!saving && panel.contains(document.activeElement)) return;
    const firstField = saving
      ? null
      : panel.querySelector<HTMLElement>(
          "input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button[role='switch']:not([disabled])",
        );
    (firstField ?? panel).focus({ preventScroll: true });
  }, [id, phase, saving]);

  useEffect(() => {
    // The existing class picker portals its own root and Workbench renders one
    // pending creation picker at a time. Other phases have an owned panel ref.
    const getPanel = () =>
      panelRef.current ??
      (latestRef.current.phase === "class"
        ? document.querySelector<HTMLElement>('[data-testid="class-picker-popover"]')
        : null);
    const blockOutside = (event: Event) => {
      if (isWorkbenchInteractionBlocked(event)) return;
      // Queue, tool and navigation controls must keep receiving input, including
      // while a save is pending. Only prevent an outside click from drawing on
      // the image canvas; the transaction owner decides when a task can change.
      if (
        !(event.target instanceof Element) ||
        !event.target
          .closest("canvas, .konvajs-content")
          ?.closest('[data-testid="workbench-stage"]')
      )
        return;
      const panel = getPanel();
      if (!panel || panel.contains(event.target)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const onKey = (event: KeyboardEvent) => {
      const current = latestRef.current;
      if (
        current.phase === "class" ||
        event.defaultPrevented ||
        event.isComposing ||
        event.keyCode === 229 ||
        event.repeat ||
        isWorkbenchInteractionBlocked(event)
      )
        return;
      if (event.key !== "Enter" && event.key !== "Escape") return;
      const panel = getPanel();
      const target = event.target;
      if (ownsTextInput(target) && !panel?.contains(target)) return;
      if (event.key === "Enter") {
        if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
        // Native selects and pressable controls consume Enter themselves. Text
        // fields in this draft submit their synchronously updated value.
        if (
          target instanceof HTMLElement &&
          (target.matches("select, textarea") ||
            target.isContentEditable ||
            target.closest('button, a, [role="button"], [role="switch"], [role^="menuitem"]'))
        )
          return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      if (current.phase === "saving") return;
      if (event.key === "Escape") current.onCancel();
      else current.onSubmit();
    };
    const outsideEvents = ["pointerdown", "mousedown", "click", "dblclick"] as const;
    outsideEvents.forEach((type) => document.addEventListener(type, blockOutside, true));
    window.addEventListener("keydown", onKey, true);
    return () => {
      outsideEvents.forEach((type) => document.removeEventListener(type, blockOutside, true));
      window.removeEventListener("keydown", onKey, true);
    };
  }, []);

  if (phase === "class") {
    return (
      <ClassPickerPopover
        position="fixed"
        anchor={anchor}
        classes={classes}
        recent={recent}
        defaultClass={className}
        onPick={onPickClass}
        onCancel={(reason) => {
          if (reason === "escape") onCancel();
          else onOutside?.();
        }}
      />
    );
  }

  return createPortal(
    <div
      ref={panelRef}
      role="dialog"
      aria-labelledby={titleId}
      aria-describedby={hintId}
      aria-busy={saving}
      tabIndex={-1}
      data-testid="manual-creation-popover"
      data-creation-id={id}
      className={POPOVER_CLASS}
      onPointerDown={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex min-w-0 flex-col gap-1">
        <div id={titleId} className="text-xs font-semibold">
          {saving ? "保存对象" : phase === "error" ? "保存未完成" : "补全必填属性"}
        </div>
        <div className="break-words text-xs text-muted-foreground">类别：{className}</div>
      </div>
      {phase === "error" && (
        <div role="alert" className="break-words text-xs text-status-danger">
          <CopyableError message={error || "保存失败，请重试。"} />
        </div>
      )}
      <AttributeForm
        key={id}
        schema={visibleSchema}
        className={className}
        attributes={attributes}
        onChange={onChangeAttributes}
        immediate
        readOnly={saving}
        hideHeading
      />
      <div id={hintId} role="status" className="text-xs text-muted-foreground">
        {saving ? "正在保存，暂时不能取消" : "Enter 保存 · Esc 取消当前对象"}
      </div>
      <div className="flex justify-end gap-1.5">
        <Button type="button" size="xs" onClick={onCancel} disabled={saving}>
          取消
        </Button>
        <Button type="button" size="xs" variant="primary" onClick={onSubmit} disabled={saving}>
          {saving ? "保存中" : phase === "error" ? "重试" : "保存"}
        </Button>
      </div>
    </div>,
    document.body,
  );
}
