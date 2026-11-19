/**
 * 通用 popover 状态 hook。
 *
 * 之前 ExportSection / TopBar 主题切换 / 智能切题菜单 / AttributeForm
 * DescriptionPopover / CanvasToolbar 各自手写 click-outside + ESC-close 逻辑。
 * 本 hook 统一行为，并暴露 anchorRef / popoverRef 以便组件自管样式与定位。
 *
 * 使用：
 *   const pop = usePopover();
 *   <button ref={pop.anchorRef} onClick={pop.toggle}>菜单</button>
 *   {pop.open && (
 *     <div ref={pop.popoverRef}>...</div>
 *   )}
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface UsePopoverOptions {
  /** 关闭时回调（非状态变化，用于上层副作用） */
  onClose?: () => void;
  /** 初始是否打开 */
  initialOpen?: boolean;
}

export interface UsePopoverResult {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
  close: () => void;
  anchorRef: React.MutableRefObject<HTMLElement | null>;
  popoverRef: React.MutableRefObject<HTMLElement | null>;
}

export function usePopover(opts: UsePopoverOptions = {}): UsePopoverResult {
  const { onClose, initialOpen = false } = opts;
  const [open, setOpenState] = useState(initialOpen);
  const anchorRef = useRef<HTMLElement | null>(null);
  const popoverRef = useRef<HTMLElement | null>(null);

  const setOpen = useCallback(
    (v: boolean) => {
      setOpenState(v);
      if (!v) onClose?.();
    },
    [onClose],
  );

  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);
  const close = useCallback(() => setOpen(false), [setOpen]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (anchorRef.current?.contains(target)) return;
      if (popoverRef.current?.contains(target)) return;
      close();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, close]);

  return { open, setOpen, toggle, close, anchorRef, popoverRef };
}
