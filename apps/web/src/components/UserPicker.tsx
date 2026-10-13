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
  /** 光标顶部坐标（向上翻转时以此为基准）。 */
  top: number;
  /** 光标底部坐标（默认向下展开的基准）。 */
  bottom: number;
  /** 光标左边缘坐标。 */
  left: number;
}

interface UserPickerProps {
  /** 视口绝对坐标（建议传光标处屏幕坐标）。 */
  anchor: UserPickerAnchor;
  /** 候选项；多由调用方按当前 query 过滤后传入。 */
  options: UserPickerOption[];
  /** 当前过滤词，用于显示「无匹配」提示。 */
  query: string;
  onPick: (opt: UserPickerOption) => void;
  onClose: () => void;
}

/** 浮层与光标/视口边缘的间距。 */
const POPOVER_MARGIN = 4;

/** 受控浮层：列表 + ↑↓ Home End + Enter 选中 + Esc 关闭。
 *  与 CommentInput 配合：在 contenteditable 中输入 `@` 触发，输入 query 实时过滤。 */
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

  // 讨论面板位于窗口底部时，向下展开会被视口裁掉。先量出真实高度，再决定向上/向下。
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
      // IME 组合中的按键永不确认提及：否则 Enter / 方向键会被当成选人，
      // 既抢走输入法的定字/翻页，又在组合中途改 DOM，导致下一个拼音首字母
      // 直接定格成第一个候选字。
      // IME 组合中的按键永不确认提及：否则 Enter / 方向键会被当成选人，
      // 既抢走输入法的定字/翻页，又在组合中途改 DOM，导致下一个拼音首字母
      // 直接定格成第一个候选字。
      if (e.isComposing || e.keyCode === 229) return;
      // capture 阶段先于编辑器/React 处理；处理完必须阻止继续冒泡，
      // 否则 Enter 会被 input 的 onKeyDown 当成提交，选择 @ 对象即发送消息。
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
    // capture 阶段：保证比文档上其它 keydown 先处理
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
