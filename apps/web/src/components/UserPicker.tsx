import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { clsx } from "clsx";

import { useElementStyle } from "@/components/ui/useElementStyle";
import styles from "./UserPicker.module.css";

export interface UserPickerOption {
  id: string;
  name: string;
  email?: string | null;
  hint?: string;
}

interface UserPickerAnchor {
  top: number;
  bottom: number;
  left: number;
}

interface UserPickerProps {
  anchor: UserPickerAnchor;
  options: UserPickerOption[];
  query: string;
  onPick: (opt: UserPickerOption) => void;
  onClose: () => void;
}

const POPOVER_MARGIN = 4;

export function UserPicker({ anchor, options, query, onPick, onClose }: UserPickerProps) {
  const [active, setActive] = useState(0);
  const [measuredHeight, setMeasuredHeight] = useState(0);
  const listRef = useRef<HTMLDivElement | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options.slice(0, 8);
    return options
      .filter((o) => o.name.toLowerCase().includes(q) || (o.email ?? "").toLowerCase().includes(q))
      .slice(0, 8);
  }, [options, query]);

  const { left, top } = useMemo(() => {
    if (typeof window === "undefined") return { left: anchor.left, top: anchor.top };
    const viewportHeight = window.innerHeight;
    let resolvedTop = anchor.bottom + POPOVER_MARGIN;
    const spaceBelow = viewportHeight - anchor.bottom;
    const spaceAbove = anchor.top;
    if (
      measuredHeight > 0 &&
      spaceBelow < measuredHeight + POPOVER_MARGIN &&
      spaceAbove > spaceBelow
    ) {
      resolvedTop = anchor.top - measuredHeight - POPOVER_MARGIN;
    }
    if (measuredHeight > 0) {
      const maxTop = Math.max(POPOVER_MARGIN, viewportHeight - measuredHeight - POPOVER_MARGIN);
      resolvedTop = Math.min(Math.max(POPOVER_MARGIN, resolvedTop), maxTop);
    }
    return { left: anchor.left, top: resolvedTop };
  }, [anchor.bottom, anchor.left, anchor.top, measuredHeight]);

  const popoverRef = useElementStyle<HTMLDivElement>(
    {
      "--user-picker-left": left,
      "--user-picker-top": top,
    } as React.CSSProperties,
    listRef,
  );

  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    setMeasuredHeight(el.offsetHeight);
  }, [filtered.length, query, options.length]);

  useEffect(() => {
    setActive(0);
  }, [query, options.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229) return;
      const stop = () => {
        e.preventDefault();
        e.stopPropagation();
      };
      if (e.key === "ArrowDown") {
        stop();
        setActive((i) => Math.min(filtered.length - 1, i + 1));
      } else if (e.key === "ArrowUp") {
        stop();
        setActive((i) => Math.max(0, i - 1));
      } else if (e.key === "Home") {
        stop();
        setActive(0);
      } else if (e.key === "End") {
        stop();
        setActive(Math.max(0, filtered.length - 1));
      } else if (e.key === "Enter" || e.key === "Tab") {
        if (filtered[active]) {
          stop();
          onPick(filtered[active]);
        }
      } else if (e.key === "Escape") {
        stop();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [filtered, active, onPick, onClose]);

  return createPortal(
    <div ref={popoverRef} role="listbox" aria-label="选择用户" className={styles.popover}>
      {filtered.length === 0 ? (
        <div className={styles.empty}>{query ? `无匹配 "${query}"` : "无项目成员"}</div>
      ) : (
        filtered.map((o, i) => (
          <div
            key={o.id}
            role="option"
            aria-selected={i === active}
            onMouseEnter={() => setActive(i)}
            onMouseDown={(e) => {
              e.preventDefault();
              onPick(o);
            }}
            className={clsx(styles.option, i === active && styles.optionActive)}
          >
            <span className={styles.name}>
              {o.name}
              {o.hint && <span className={styles.role}>{o.hint}</span>}
            </span>
            {o.email && <span className={styles.hint}>{o.email}</span>}
          </div>
        ))
      )}
    </div>,
    document.body,
  );
}
