import { useCallback, useEffect, useRef, useState } from "react";

export interface UsePopoverOptions {
  onClose?: () => void;
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
