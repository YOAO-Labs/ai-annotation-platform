import { describe, it, expect, beforeEach, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  document.head.innerHTML = "";
  delete (window as { turnstile?: unknown }).turnstile;
});

describe("getTurnstileSiteKey", () => {
  it("env 缺省时返回 null", async () => {
    vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "");
    const { getTurnstileSiteKey } = await import("../turnstile");
    expect(getTurnstileSiteKey()).toBeNull();
    vi.unstubAllEnvs();
  });

  it("env 配置时返回 site key", async () => {
    vi.stubEnv("VITE_TURNSTILE_SITE_KEY", "0xAAAA...key");
    const { getTurnstileSiteKey } = await import("../turnstile");
    expect(getTurnstileSiteKey()).toBe("0xAAAA...key");
    vi.unstubAllEnvs();
  });
});

describe("loadTurnstileScript", () => {
  it("window.turnstile 已存在时立即 resolve，不写 script", async () => {
    (window as { turnstile?: unknown }).turnstile = {
      render: vi.fn(),
      reset: vi.fn(),
      remove: vi.fn(),
    };
    const { loadTurnstileScript } = await import("../turnstile");
    await loadTurnstileScript();
    expect(document.head.querySelectorAll("script").length).toBe(0);
  });

  it("第一次调用注入 <script> 且后续调用复用同一个 promise", async () => {
    const { loadTurnstileScript } = await import("../turnstile");
    const p1 = loadTurnstileScript();
    const p2 = loadTurnstileScript();
    expect(p1).toBe(p2);
    const scripts = document.head.querySelectorAll(
      "script[src^='https://challenges.cloudflare.com/turnstile/v0/api.js']",
    );
    expect(scripts.length).toBe(1);
    (scripts[0] as HTMLScriptElement).onload?.(new Event("load"));
    await expect(p1).resolves.toBeUndefined();
    await expect(p2).resolves.toBeUndefined();
  });

  it("script 标签已在 DOM 中时 attach load 监听而不是再创建", async () => {
    const existing = document.createElement("script");
    existing.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
    document.head.appendChild(existing);

    const { loadTurnstileScript } = await import("../turnstile");
    const p = loadTurnstileScript();
    expect(document.head.querySelectorAll("script").length).toBe(1);
    existing.dispatchEvent(new Event("load"));
    await expect(p).resolves.toBeUndefined();
  });

  it("script load 失败时 reject", async () => {
    const { loadTurnstileScript } = await import("../turnstile");
    const p = loadTurnstileScript();
    const s = document.head.querySelector("script") as HTMLScriptElement;
    s.onerror?.(new Event("error"));
    await expect(p).rejects.toThrow(/turnstile script failed/);
  });

  it("注入 script 时从 meta[name=csp-nonce] 读取 nonce 设到 script.nonce", async () => {
    const meta = document.createElement("meta");
    meta.setAttribute("name", "csp-nonce");
    meta.setAttribute("content", "test-nonce-abc123");
    document.head.appendChild(meta);

    const { loadTurnstileScript } = await import("../turnstile");
    loadTurnstileScript();
    const s = document.head.querySelector(
      "script[src^='https://challenges.cloudflare.com']",
    ) as HTMLScriptElement;
    expect(s.nonce).toBe("test-nonce-abc123");
  });

  it("meta 缺省时不设 nonce (dev 模式无 Nginx 替换占位符)", async () => {
    const { loadTurnstileScript } = await import("../turnstile");
    loadTurnstileScript();
    const s = document.head.querySelector(
      "script[src^='https://challenges.cloudflare.com']",
    ) as HTMLScriptElement;
    expect(s.nonce).toBe("");
  });
});
