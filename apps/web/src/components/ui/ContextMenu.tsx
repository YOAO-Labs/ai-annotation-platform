import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

import { Icon } from "./Icon";
import type { DropdownItem } from "./DropdownMenu";

/**
 * module.css → Tailwind。坐标定位/翻转/键盘/a11y 逻辑不动;坐标锚点沿用 --context-menu-x/y
 * CSS 变量(经 Tailwind 任意值消费)。item 是 portal 内原生 <button>,加 appearance-none 防 UA 漏样。
 */

interface ContextMenuProps {
  open: boolean;
  x: number;
  y: number;
  items: DropdownItem[];
  onClose: () => void;
}

export function ContextMenu({ open, x, y, items, onClose }: ContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [flip, setFlip] = useState({ x: false, y: false });

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (evt: MouseEvent) => {
      if (!menuRef.current?.contains(evt.target as Node)) onClose();
    };
    const onKeyDown = (evt: KeyboardEvent) => {
      if (evt.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose, open]);

  useLayoutEffect(() => {
    if (!open) return;
    const menu = menuRef.current;
    if (!menu) return;
    const rect = menu.getBoundingClientRect();
    setFlip({
      x: x + rect.width > window.innerWidth,
      y: y + rect.height > window.innerHeight,
    });
  }, [items, open, x, y]);

  if (!open) return null;

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-orientation="vertical"
      tabIndex={-1}
      className={cn(
        "fixed left-[var(--context-menu-x)] top-[var(--context-menu-y)] z-overlay-high min-w-[190px] rounded-md border border-border bg-popover p-1 shadow-md outline-none",
        flip.x && "-translate-x-full",
        flip.y && "-translate-y-full",
      )}
      // eslint-disable-next-line no-restricted-syntax -- 坐标锚点是一次性动态值，经 CSS custom property 注入
      style={
        {
          "--context-menu-x": `${x}px`,
          "--context-menu-y": `${y}px`,
        } as CSSProperties
      }
    >
      {items.map((item, index) => {
        if (item.divider) {
          return (
            <div key={item.id || `div-${index}`} role="separator" className="my-1 h-px bg-border" />
          );
        }
        return (
          <button
            key={item.id}
            type="button"
            role="menuitem"
            disabled={item.disabled}
            onClick={() => {
              if (item.disabled) return;
              item.onSelect?.();
              onClose();
            }}
            className={cn(
              "flex w-full appearance-none items-center gap-2 whitespace-nowrap rounded-sm border-0 bg-transparent px-2.5 py-2 text-left text-sm font-normal text-muted-foreground",
              item.active ? "font-semibold text-foreground" : "hover:bg-accent",
              item.active && "bg-accent",
              item.disabled &&
                "cursor-not-allowed text-muted-foreground/60 opacity-60 hover:bg-transparent",
            )}
          >
            {item.icon && <Icon name={item.icon} size={13} />}
            <span className="flex-1">{item.label}</span>
            {item.kbd && (
              <span className="mono rounded border border-b-2 border-border bg-muted px-1.5 py-px text-2xs text-muted-foreground">
                {item.kbd}
              </span>
            )}
            {item.active && !item.kbd && <Icon name="check" size={12} className="text-brand" />}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}
