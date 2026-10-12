/**
 * 平台自适应按键检测与快捷键展示增强。
 *
 * 自动识别用户操作系统（macOS vs Windows/Linux）：
 * - macOS: 将 Ctrl 映射为 ⌘ (Command)，Alt 映射为 ⌥ (Option)；
 * - Windows/Linux: 保持 Ctrl 与 Alt。
 *
 * 同时支持在文档快捷键速查表上方提供即时平台切换，满足多平台切换工作的标注员与质检员。
 */

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
  updateHotkeyElements(plat);
}

function applyPlatformClass(plat: PlatformKind): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.remove("is-platform-mac", "is-platform-win");
  root.classList.add(plat === "mac" ? "is-platform-mac" : "is-platform-win");
}

function updateHotkeyElements(plat: PlatformKind): void {
  if (typeof document === "undefined") return;

  // 1. 更新所有显式声明了 data-platform-key 的按键
  document.querySelectorAll<HTMLElement>("[data-key-mac][data-key-win]").forEach((el) => {
    const text = plat === "mac" ? el.dataset.keyMac : el.dataset.keyWin;
    if (text && el.textContent !== text) {
      el.textContent = text;
    }
  });

  // 2. 增强文档正文和表格中的快捷键代码与 kbd
  document.querySelectorAll<HTMLElement>(".vp-doc table td code, .vp-doc kbd").forEach((el) => {
    if (el.dataset.hotkeyParsed) {
      // 重新渲染已标记的按键
      const raw = el.dataset.rawKey;
      if (raw) {
        el.textContent = formatKeyText(raw, plat);
      }
      return;
    }

    const text = el.textContent || "";
    if (isHotkeyString(text)) {
      el.dataset.hotkeyParsed = "1";
      el.dataset.rawKey = text;
      el.classList.add("doc-keycap");
      el.textContent = formatKeyText(text, plat);
    }
  });
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

export function setupPlatformKeys(): void {
  if (typeof window === "undefined") return;

  const current = getActivePlatform();
  applyPlatformClass(current);

  const observer = new MutationObserver(() => {
    updateHotkeyElements(getActivePlatform());
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
    updateHotkeyElements(current);
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      observer.observe(document.body, { childList: true, subtree: true });
      updateHotkeyElements(current);
    });
  }
}
