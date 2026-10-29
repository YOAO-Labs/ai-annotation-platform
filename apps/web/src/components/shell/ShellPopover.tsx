import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

const OPEN_EVENT = "shell:popover-open";

export const SHELL_POPOVER_HEADER_CLASS =
  "flex h-11 shrink-0 items-center gap-2 border-b border-border px-3.5 text-xs";

/** Common geometry and dismissal for the three top-bar information panels. */
export function ShellPopover({
  id,
  label,
  onClose,
  children,
}: {
  id: "performance" | "jobs" | "notifications";
  label: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);

  useLayoutEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const trigger = () =>
      document.querySelector<HTMLButtonElement>(`[data-shell-popover-trigger="${id}"]`);

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !panel.contains(target) && !trigger()?.contains(target)) {
        closeRef.current();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      closeRef.current();
      trigger()?.focus();
    };
    const onAnotherOpen = (event: Event) => {
      if ((event as CustomEvent<HTMLElement>).detail !== panel) closeRef.current();
    };

    // A keyboard-opened performance panel also replaces the currently open panel.
    document.addEventListener(OPEN_EVENT, onAnotherOpen);
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    document.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: panel }));
    return () => {
      document.removeEventListener(OPEN_EVENT, onAnotherOpen);
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [id]);

  return createPortal(
    <div
      ref={panelRef}
      id={`shell-popover-${id}`}
      role="dialog"
      aria-label={label}
      data-shell-popover={id}
      className="fixed right-3 top-[60px] z-notification flex h-[min(480px,calc(100dvh-72px))] w-[min(420px,calc(100vw-24px))] min-w-0 flex-col overflow-hidden rounded-md border border-border bg-popover text-popover-foreground shadow-lg"
    >
      {children}
    </div>,
    document.body,
  );
}
