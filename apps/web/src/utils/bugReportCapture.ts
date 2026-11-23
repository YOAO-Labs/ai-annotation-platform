interface ApiCallEntry {
  method: string;
  url: string;
  status: number;
  ms: number;
  requestId?: string;
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

export function sanitizeApiCalls(calls: ApiCallEntry[]): ApiCallEntry[] {
  return calls.map((c) => ({
    ...c,
    url: c.url.replace(/([?&](?:token|password|secret)=)[^&]+/gi, "$1***"),
  }));
}

export async function captureScreenshot(): Promise<Blob> {
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

export function patchFetchForBugCapture() {
  const orig = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const start = Date.now();
    let status = 0;
    let requestId: string | undefined;
    try {
      const resp = await orig(input, init);
      status = resp.status;
      requestId = resp.headers?.get?.("x-request-id")?.trim() || undefined;
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
          ...(requestId ? { requestId } : {}),
        });
        if (apiCallRing.length > MAX_API_CALLS) apiCallRing.shift();
      }
    }
  };
}
