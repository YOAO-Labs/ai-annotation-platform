/** 自动捕获工具：API 调用 ring buffer + console 错误 ring buffer + 脱敏。 */

interface ApiCallEntry {
  method: string;
  url: string;
  status: number;
  ms: number;
}

interface ConsoleErrorEntry {
  msg: string;
  stack?: string;
}

const MAX_API_CALLS = 10;
const MAX_CONSOLE_ERRORS = 5;

const apiCallRing: ApiCallEntry[] = [];
const consoleErrorRing: ConsoleErrorEntry[] = [];

export function initBugReportCapture() {
  // Capture console errors
  const origOnerror = window.onerror;
  window.onerror = (message, source, lineno, colno, error) => {
    consoleErrorRing.push({
      msg: String(message),
      stack: error instanceof Error ? error.stack?.slice(0, 500) : undefined,
    });
    if (consoleErrorRing.length > MAX_CONSOLE_ERRORS) consoleErrorRing.shift();
    if (origOnerror) origOnerror(message, source, lineno, colno, error);
  };

  const origUnhandled = window.onunhandledrejection;
  window.onunhandledrejection = ((event: PromiseRejectionEvent) => {
    consoleErrorRing.push({
      msg: event.reason instanceof Error ? event.reason.message : String(event.reason),
      stack: event.reason instanceof Error ? event.reason.stack?.slice(0, 500) : undefined,
    });
    if (consoleErrorRing.length > MAX_CONSOLE_ERRORS) consoleErrorRing.shift();
    if (origUnhandled) origUnhandled.call(window, event);
  }) as typeof window.onunhandledrejection;
}

export function getRecentApiCalls(): ApiCallEntry[] {
  return [...apiCallRing];
}

export function getRecentConsoleErrors(): ConsoleErrorEntry[] {
  return [...consoleErrorRing];
}

/**
 * Sanitize API calls: strip auth headers, password fields.
 */
export function sanitizeApiCalls(calls: ApiCallEntry[]): ApiCallEntry[] {
  return calls.map((c) => ({
    ...c,
    url: c.url.replace(/([?&](?:token|password|secret)=)[^&]+/gi, "$1***"),
  }));
}

/**
 * 用 html2canvas 截当前可视区。返回 PNG Blob。
 *
 * 注意：被 BugReportDrawer 自身遮挡的部分不会被截到（截图前应先关闭 drawer
 * 或把 drawer 设为 ignore）。这里使用 ignoreElements 排除带 data-bug-drawer 的节点。
 */
export async function captureScreenshot(): Promise<Blob> {
  // 用 html2canvas-pro 替代 html2canvas：原版 1.4.1 不支持 oklch()，
  // 而本项目设计 token 全是 oklch，会直接抛 "Attempting to parse an unsupported
  // color function 'oklch'" → 截图失败。html2canvas-pro 是 drop-in fork，
  // 支持 oklch / oklab / lab / color() 等新色彩函数。
  const { default: html2canvas } = await import("html2canvas-pro");
  const canvas = await html2canvas(document.body, {
    backgroundColor: "#ffffff",
    scale: Math.min(window.devicePixelRatio || 1, 2),
    useCORS: true,
    logging: false,
    ignoreElements: (el) => {
      return Boolean(
        el.closest?.("[data-bug-drawer]") ||
        el.closest?.("[data-bug-fab]") ||
        el.closest?.("[data-toast-rack]"),
      );
    },
  });
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("toBlob failed"))), "image/png");
  });
}

/**
 * Hook into the app's fetch to record API calls. Call once at app init.
 */
export function patchFetchForBugCapture() {
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const start = Date.now();
    let status = 0;
    try {
      const resp = await orig(input, init);
      status = resp.status;
      return resp;
    } finally {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v1/")) {
        apiCallRing.push({
          method: init?.method ?? "GET",
          url,
          status,
          ms: Date.now() - start,
        });
        if (apiCallRing.length > MAX_API_CALLS) apiCallRing.shift();
      }
    }
  };
}
