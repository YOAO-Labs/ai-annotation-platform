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
  } catch {}
  return getDetectedPlatform();
}

export function setActivePlatform(plat: PlatformKind): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, plat);
  } catch {}
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

    document.querySelectorAll<HTMLElement>("[data-key-mac][data-key-win]").forEach((el) => {
      const target = plat === "mac" ? el.dataset.keyMac : el.dataset.keyWin;
      if (target && el.textContent !== target) {
        el.textContent = target;
      }
    });

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

  if (router) {
    const prevHook = router.onAfterRouteChanged;
    router.onAfterRouteChanged = (to) => {
      prevHook?.(to);
      scheduleUpdate();
    };
  }
}
