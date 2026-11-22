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
      ) => string;
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
