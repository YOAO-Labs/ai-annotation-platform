/**
 * Cloudflare Turnstile loader.
 *
 * `VITE_TURNSTILE_SITE_KEY` 缺省时 noop —— 本地开发不阻断注册流。
 * 启用时动态注入官方 api.js（async defer），并暴露 render/reset 封装。
 */

declare global {
  interface Window {
    turnstile?: {
      render: (
        container: string | HTMLElement,
        options: {
          sitekey: string;
          callback?: (token: string) => void;
          "error-callback"?: () => void;
          "expired-callback"?: () => void;
          theme?: "light" | "dark" | "auto";
          size?: "normal" | "compact" | "invisible";
        },
      ) => string; // widget id
      reset: (widgetId?: string) => void;
      remove: (widgetId?: string) => void;
    };
  }
}

const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";
let _scriptPromise: Promise<void> | null = null;

export function getTurnstileSiteKey(): string | null {
  const key = import.meta.env.VITE_TURNSTILE_SITE_KEY;
  return typeof key === "string" && key.length > 0 ? key : null;
}

/**
 * 读取 index.html 中 `<meta name="csp-nonce">` 的 per-request nonce.
 * 用于动态 script 注入时显式带 `nonce` 属性, 满足 nonce-based CSP.
 * dev 模式下 meta content 仍是 placeholder (Nginx 才替换), 浏览器忽略不影响渲染.
 */
function getCspNonce(): string {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csp-nonce"]');
  return meta?.content ?? "";
}

export function loadTurnstileScript(): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  if (window.turnstile) return Promise.resolve();
  if (_scriptPromise) return _scriptPromise;

  _scriptPromise = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src^="${SCRIPT_SRC}"]`);
    if (existing) {
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () => reject(new Error("turnstile script failed")));
      return;
    }
    const s = document.createElement("script");
    s.src = `${SCRIPT_SRC}?render=explicit`;
    s.async = true;
    s.defer = true;
    // CSP nonce-based 收紧 — 动态注入 script 必须显式设 nonce 才能通过 script-src
    const nonce = getCspNonce();
    if (nonce) s.nonce = nonce;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("turnstile script failed"));
    document.head.appendChild(s);
  });
  return _scriptPromise;
}

export interface RenderOptions {
  onToken: (token: string) => void;
  onError?: () => void;
  onExpired?: () => void;
  theme?: "light" | "dark" | "auto";
}

export async function renderTurnstile(
  container: HTMLElement,
  sitekey: string,
  opts: RenderOptions,
): Promise<string | null> {
  await loadTurnstileScript();
  if (!window.turnstile) return null;
  return window.turnstile.render(container, {
    sitekey,
    theme: opts.theme ?? "auto",
    callback: opts.onToken,
    "error-callback": opts.onError,
    "expired-callback": opts.onExpired,
  });
}

export function resetTurnstile(widgetId: string | null) {
  if (window.turnstile && widgetId) window.turnstile.reset(widgetId);
}

export function removeTurnstile(widgetId: string | null) {
  if (window.turnstile && widgetId) window.turnstile.remove(widgetId);
}
