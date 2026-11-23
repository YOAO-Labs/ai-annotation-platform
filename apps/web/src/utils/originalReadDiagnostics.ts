import { useAuthStore } from "@/stores/authStore";

const MAX_DIAGNOSTIC_EVENTS = 12;
const MAX_DIAGNOSTIC_JSON_CHARS = 6_000;
const UUID_SEGMENT = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const GENERIC_UUID_RE = new RegExp(UUID_SEGMENT, "gi");
const TASK_ORIGINAL_IDENTITY_RE = new RegExp(
  `^/api/v1/files/tasks/${UUID_SEGMENT}/original-identity$`,
  "i",
);
const TASK_ORIGINAL_RE = new RegExp(`^/api/v1/files/tasks/${UUID_SEGMENT}/original$`, "i");
const CAMERA_ORIGINAL_IDENTITY_RE = new RegExp(
  `^/api/v1/files/tasks/${UUID_SEGMENT}/links/${UUID_SEGMENT}/original-identity$`,
  "i",
);
const CAMERA_ORIGINAL_RE = new RegExp(
  `^/api/v1/files/tasks/${UUID_SEGMENT}/links/${UUID_SEGMENT}/original$`,
  "i",
);
const ITEM_ORIGINAL_IDENTITY_RE = new RegExp(
  `^/api/v1/datasets/${UUID_SEGMENT}/items/${UUID_SEGMENT}/original-identity$`,
  "i",
);
const ITEM_ORIGINAL_RE = new RegExp(
  `^/api/v1/datasets/${UUID_SEGMENT}/items/${UUID_SEGMENT}/original$`,
  "i",
);
const TASK_CONTENT_RE = new RegExp(`^/api/v1/files/tasks/${UUID_SEGMENT}/content$`, "i");
const ITEM_CONTENT_RE = new RegExp(`^/api/v1/dataset-items/${UUID_SEGMENT}/content$`, "i");

export type OriginalReadResourceKind = "task" | "item" | "camera" | "verified_content";

export interface OriginalReadErrorEvent {
  kind: "error";
  at: string;
  attempt: number;
  manualRetry: boolean;
  resourceKind: OriginalReadResourceKind;
  requestPath: string;
  status: number | null;
  code: string | null;
  reason: string | null;
  requestId: string | null;
}

export interface OriginalManualRetryRecoveredEvent {
  kind: "manual_retry_recovered";
  at: string;
  attempt: number;
  resourceKind: OriginalReadResourceKind;
  requestPath: string;
  contentId: string | null;
  sha256Prefix: string | null;
}

export type OriginalReadDiagnosticEvent =
  | OriginalReadErrorEvent
  | OriginalManualRetryRecoveredEvent;

export interface OriginalReadDiagnosticsSnapshot {
  route: string;
  updatedAt: string;
  events: OriginalReadDiagnosticEvent[];
}

interface OriginalReadDiagnosticsStore extends OriginalReadDiagnosticsSnapshot {
  ownerKey: string;
}

function diagnosticsWindow(): {
  __originalReadDiagnostics?: OriginalReadDiagnosticsStore;
} | null {
  if (typeof window === "undefined") return null;
  return window as unknown as { __originalReadDiagnostics?: OriginalReadDiagnosticsStore };
}

function currentRoute(): string {
  if (typeof window === "undefined") return "";
  return `${window.location.pathname}${window.location.search}`;
}

function currentOwnerKey(): string {
  const auth = useAuthStore.getState();
  return JSON.stringify([auth.user?.id ?? null, auth.sessionId]);
}

function stableStringify(value: unknown): string {
  const json = JSON.stringify(value, null, 2);
  return json.length <= MAX_DIAGNOSTIC_JSON_CHARS
    ? json
    : `${json.slice(0, MAX_DIAGNOSTIC_JSON_CHARS)}\n... truncated`;
}

export function sanitizeOriginalReadPath(rawPath: string): string {
  let pathname = rawPath.trim();
  if (!pathname) return "/api/v1/unknown";
  if (/^https?:\/\//i.test(pathname)) {
    try {
      pathname = new URL(pathname).pathname;
    } catch {
      pathname = pathname.split(/[?#]/, 1)[0] ?? pathname;
    }
  } else {
    pathname = pathname.split(/[?#]/, 1)[0] ?? pathname;
  }
  if (!pathname.startsWith("/")) pathname = `/${pathname}`;
  if (!pathname.startsWith("/api/v1/")) {
    pathname = pathname.startsWith("/api/") ? pathname : `/api/v1${pathname}`;
  }
  if (TASK_ORIGINAL_IDENTITY_RE.test(pathname)) {
    return "/api/v1/files/tasks/{task_id}/original-identity";
  }
  if (TASK_ORIGINAL_RE.test(pathname)) {
    return "/api/v1/files/tasks/{task_id}/original";
  }
  if (CAMERA_ORIGINAL_IDENTITY_RE.test(pathname)) {
    return "/api/v1/files/tasks/{task_id}/links/{link_id}/original-identity";
  }
  if (CAMERA_ORIGINAL_RE.test(pathname)) {
    return "/api/v1/files/tasks/{task_id}/links/{link_id}/original";
  }
  if (ITEM_ORIGINAL_IDENTITY_RE.test(pathname)) {
    return "/api/v1/datasets/{dataset_id}/items/{item_id}/original-identity";
  }
  if (ITEM_ORIGINAL_RE.test(pathname)) {
    return "/api/v1/datasets/{dataset_id}/items/{item_id}/original";
  }
  if (TASK_CONTENT_RE.test(pathname)) {
    return "/api/v1/files/tasks/{task_id}/content";
  }
  if (ITEM_CONTENT_RE.test(pathname)) {
    return "/api/v1/dataset-items/{item_id}/content";
  }
  return pathname.replace(GENERIC_UUID_RE, "{id}");
}

function appendEvent(event: OriginalReadDiagnosticEvent): void {
  const target = diagnosticsWindow();
  if (!target) return;
  const route = currentRoute();
  const ownerKey = currentOwnerKey();
  const existing = target.__originalReadDiagnostics;
  const events =
    existing && existing.route === route && existing.ownerKey === ownerKey
      ? [...existing.events, event].slice(-MAX_DIAGNOSTIC_EVENTS)
      : [event];
  target.__originalReadDiagnostics = {
    ownerKey,
    route,
    updatedAt: event.at,
    events,
  };
}

export function recordOriginalReadError(input: {
  attempt: number;
  resourceKind: OriginalReadResourceKind;
  requestPath: string;
  status: number | null;
  code: string | null;
  reason: string | null;
  requestId: string | null;
}): void {
  appendEvent({
    kind: "error",
    at: new Date().toISOString(),
    attempt: input.attempt,
    manualRetry: input.attempt > 0,
    resourceKind: input.resourceKind,
    requestPath: sanitizeOriginalReadPath(input.requestPath),
    status: input.status,
    code: input.code,
    reason: input.reason,
    requestId: input.requestId,
  });
}

export function recordOriginalManualRetrySuccess(input: {
  attempt: number;
  resourceKind: OriginalReadResourceKind;
  requestPath: string;
  contentId?: string | null;
  sha256?: string | null;
}): void {
  const sha = typeof input.sha256 === "string" && input.sha256 ? input.sha256.slice(0, 12) : null;
  appendEvent({
    kind: "manual_retry_recovered",
    at: new Date().toISOString(),
    attempt: input.attempt,
    resourceKind: input.resourceKind,
    requestPath: sanitizeOriginalReadPath(input.requestPath),
    contentId: typeof input.contentId === "string" && input.contentId ? input.contentId : null,
    sha256Prefix: sha,
  });
}

export function clearOriginalReadDiagnostics(): void {
  const target = diagnosticsWindow();
  if (target) delete target.__originalReadDiagnostics;
}

const unbindAuth = useAuthStore.subscribe(() => {
  const target = diagnosticsWindow();
  if (
    target?.__originalReadDiagnostics &&
    target.__originalReadDiagnostics.ownerKey !== currentOwnerKey()
  ) {
    delete target.__originalReadDiagnostics;
  }
});

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    unbindAuth();
    clearOriginalReadDiagnostics();
  });
}

export function getOriginalReadDiagnosticsSnapshot(): OriginalReadDiagnosticsSnapshot | null {
  const store = diagnosticsWindow()?.__originalReadDiagnostics;
  if (
    !store ||
    store.route !== currentRoute() ||
    store.ownerKey !== currentOwnerKey() ||
    store.events.length === 0
  ) {
    return null;
  }
  return {
    route: store.route,
    updatedAt: store.updatedAt,
    events: store.events.map((event) => ({ ...event })),
  };
}

export function appendOriginalReadDiagnostics(
  description: string,
  snapshot: OriginalReadDiagnosticsSnapshot | null,
): string {
  if (!snapshot) return description;
  return `${description}\n\n---\n\n### Original Read Diagnostics\n\n\`\`\`json\n${stableStringify(snapshot)}\n\`\`\``;
}

export function originalReadDiagnosticsConsoleEntry(
  snapshot: OriginalReadDiagnosticsSnapshot | null,
): { msg: string; stack: string } | null {
  if (!snapshot) return null;
  return {
    msg: "[original-read-diagnostics]",
    stack: stableStringify(snapshot),
  };
}
