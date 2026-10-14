/**
 * 平台自适应按键检测与快捷键展示增强。
 *
 * 自动识别用户操作系统（macOS vs Windows/Linux）：
 * - macOS: 将 Ctrl 映射为 ⌘ (Command)，Alt 映射为 ⌥ (Option)；
 * - Windows/Linux: 保持 Ctrl 与 Alt。
 *
 * 安全约束：
 * - 仅在客户端运行，避免 SSR 异常；
 * - 绑定 VitePress router 路由切换，绝不在 document.body 上挂载无条件 MutationObserver，
 *   杜绝死循环 microtask 导致的页面卡死；
 * - 更新 DOM 前严格比对 textContent，避免无效重绘与事件风暴。
 */

import type { Router } from "vitepress";

export type PlatformKind = "mac" | "win";

const STORAGE_KEY = "aap_docs_hotkey_platform";

export function getDetectedPlatform(): PlatformKind {
  if (typeof navigator === "undefined") return "win";
  const p = (navigator.platform || navigator.userAgent || "").toLowerCase();
  return p.includes("mac") || p.includes("iphone") || p.includes("ipad") ? "mac" : "win";
}

export function getActivePlatform(): PlatformKind {
  if (typeof window === "undefined") return "win";
  try {
    const saved = localStorage.getItem(STORAGE_KEY) as PlatformKind | null;
    if (saved === "mac" || saved === "win") return saved;
  } catch {
    // ignore security/sandbox errors
  }
  return getDetectedPlatform();
}

export function setActivePlatform(plat: PlatformKind): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, plat);
  } catch {
    // ignore
  }
  applyPlatformClass(plat);
  updateHotkeyElements();
}

function applyPlatformClass(plat: PlatformKind): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.remove("is-platform-mac", "is-platform-win");
  root.classList.add(plat === "mac" ? "is-platform-mac" : "is-platform-win");
}

let isUpdating = false;

export function updateHotkeyElements(): void {
  if (typeof document === "undefined" || isUpdating) return;
  isUpdating = true;

  try {
    const plat = getActivePlatform();
    applyPlatformClass(plat);

    // 1. 更新显式声明了 data-key-mac / data-key-win 的专用按键
    document.querySelectorAll<HTMLElement>("[data-key-mac][data-key-win]").forEach((el) => {
      const target = plat === "mac" ? el.dataset.keyMac : el.dataset.keyWin;
      if (target && el.textContent !== target) {
        el.textContent = target;
      }
    });

    // 2. 增强正文表格和正文内的快捷键代码与 kbd
    document.querySelectorAll<HTMLElement>(".vp-doc table td code, .vp-doc kbd").forEach((el) => {
      const currentText = el.textContent || "";

      if (el.dataset.hotkeyParsed) {
        const raw = el.dataset.rawKey;
        if (raw) {
          const target = formatKeyText(raw, plat);
          if (currentText !== target) {
            el.textContent = target;
          }
        }
        return;
      }

      if (isHotkeyString(currentText)) {
        const target = formatKeyText(currentText, plat);
        el.dataset.hotkeyParsed = "1";
        el.dataset.rawKey = currentText;
        if (!el.classList.contains("doc-keycap")) {
          el.classList.add("doc-keycap");
        }
        if (currentText !== target) {
          el.textContent = target;
        }
      }
    });
  } finally {
    isUpdating = false;
  }
}

function isHotkeyString(text: string): boolean {
  return (
    /^(Ctrl|Cmd|Command|Alt|Option|Shift|Enter|Space|Tab|Esc|[A-Z0-9])(\s*\+\s*(Ctrl|Cmd|Command|Alt|Option|Shift|Enter|Space|Tab|Esc|滚轮|拖拽|[A-Z0-9]))+$/i.test(
      text.trim(),
    ) || /^(Ctrl|Cmd|Alt|Option)$/i.test(text.trim())
  );
}

function formatKeyText(raw: string, plat: PlatformKind): string {
  if (plat === "mac") {
    return raw
      .replace(/\bCtrl\b/g, "⌘")
      .replace(/\bAlt\b/g, "⌥")
      .replace(/\bShift\b/g, "⇧");
  } else {
    return raw.replace(/⌘/g, "Ctrl").replace(/⌥/g, "Alt").replace(/⇧/g, "Shift");
  }
}

export function setupPlatformKeys(router?: Router): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // HMR / 多次执行保护
  const win = window as unknown as { __docsPlatformKeysInitialized?: boolean };
  if (win.__docsPlatformKeysInitialized) return;
  win.__docsPlatformKeysInitialized = true;

  const initialPlat = getActivePlatform();
  applyPlatformClass(initialPlat);

  const scheduleUpdate = () => {
    setTimeout(updateHotkeyElements, 60);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scheduleUpdate, { once: true });
  } else {
    scheduleUpdate();
  }

  // 挂载路由切换钩子（VitePress 官方标准 SPA 导航生命周期）
  if (router) {
    const prevHook = router.onAfterRouteChanged;
    router.onAfterRouteChanged = (to) => {
      prevHook?.(to);
      scheduleUpdate();
    };
  }
}
