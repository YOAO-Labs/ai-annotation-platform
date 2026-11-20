import { useEffect, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLocation } from "react-router-dom";

import { useElementStyle } from "../ui/useElementStyle";

interface SidebarDrawerProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /** 抽屉宽度，默认 240px。 */
  width?: number;
}

/**
 * 窄屏 hamburger 抽屉：
 * - 从左侧滑入；
 * - 遮罩点击 / Esc / 路由变化自动关闭；
 * - body 滚动锁定；
 * - 通过 Portal 渲染到 document.body，避免 grid 布局影响。
 */
export function SidebarDrawer({ open, onClose, children, width = 240 }: SidebarDrawerProps) {
  const location = useLocation();
  const panelRef = useElementStyle<HTMLElement>({
    "--sidebar-drawer-width": width,
  } as CSSProperties);

  // 路由变化关闭抽屉
  useEffect(() => {
    if (open) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // body 滚动锁
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  return createPortal(
    <>
      {/* 遮罩 */}
      <div
        onClick={onClose}
        aria-hidden="true"
        className={`fixed inset-0 z-app-drawer-backdrop bg-black/40 transition-opacity duration-[220ms] ease-out ${
          open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
        }`}
      />
      {/* 抽屉本体 */}
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="导航菜单"
        className={`fixed left-0 top-0 z-app-drawer h-screen w-[var(--sidebar-drawer-width)] overflow-auto border-r border-border bg-card transition-[transform,box-shadow] duration-[220ms] ease-out ${
          open ? "translate-x-0 shadow-xl" : "-translate-x-full"
        }`}
      >
        {children}
      </aside>
    </>,
    document.body,
  );
}
