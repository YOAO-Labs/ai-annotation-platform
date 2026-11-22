import { recordApiDuration } from "./_metrics";
import { authSessionToken, type AuthSessionOwner, useAuthStore } from "../stores/authStore";

const BASE = "/api/v1";

class ApiError extends Error {
  detailRaw?: unknown;
  headers?: Record<string, string>;

  constructor(
    public status: number,
    message: string,
    detailRaw?: unknown,
    headers?: Record<string, string>,
  ) {
    super(message);
    this.name = "ApiError";
    this.detailRaw = detailRaw;
    this.headers = headers;
  }
}

const EXPOSED_HEADERS = ["x-login-failed-count", "retry-after"] as const;

function validationIssueMessage(issue: unknown): string | undefined {
  if (!issue || typeof issue !== "object") return undefined;
  const value = issue as { loc?: unknown; msg?: unknown };
  if (typeof value.msg !== "string" || value.msg.length === 0) return undefined;
  const location = Array.isArray(value.loc)
    ? value.loc
        .filter((part) => part !== "body")
        .map(String)
        .join(".")
    : "";
  return location ? `${location}：${value.msg}` : value.msg;
}

export function apiErrorDetailMessage(rawDetail: unknown): string | undefined {
  if (typeof rawDetail === "string") return rawDetail;
  if (Array.isArray(rawDetail)) {
    return rawDetail.map(validationIssueMessage).find(Boolean);
  }
  if (rawDetail && typeof rawDetail === "object" && "message" in rawDetail) {
    const message = String((rawDetail as { message?: unknown }).message ?? "");
    return message || undefined;
  }
  return undefined;
}

async function request<T>(
  path: string,
  init?: RequestInit,
  opts?: {
    anonymous?: boolean;
    silent?: boolean;
    responseType?: "blob";
    session?: AuthSessionOwner;
    expectedEtag?: string;
  },
): Promise<T> {
  const token = opts?.session
    ? authSessionToken(opts.session)
    : opts?.anonymous
      ? null
      : localStorage.getItem("token");
  const owner = useAuthStore.getState();
  if ((opts?.session && !token) || (!opts?.anonymous && owner.user && owner.token !== token)) {
    throw new ApiError(401, "登录账号已在其他页面变更，请等待同步或刷新页面");
  }
  const t0 = performance.now();
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
  try {
    recordApiDuration(performance.now() - t0);
  } catch {
    // 监控不影响业务
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const rawDetail: unknown = (body as { detail?: unknown })?.detail;
    const detail = apiErrorDetailMessage(rawDetail);

    if (res.status === 401 && !opts?.anonymous) {
      const { useAuthStore } = await import("../stores/authStore");
      const current = useAuthStore.getState();
      if (
        (!opts?.session || authSessionToken(opts.session) === token) &&
        localStorage.getItem("token") === token &&
        current.token === owner.token &&
        current.user?.id === owner.user?.id &&
        current.sessionId === owner.sessionId
      )
        current.logout();
    } else if (!opts?.anonymous && !opts?.silent && (res.status === 403 || res.status >= 500)) {
      const { useToastStore } = await import("../components/ui/Toast");
      if (opts?.session && authSessionToken(opts.session) !== token)
        throw new DOMException("登录会话已变更", "AbortError");
      if (res.status === 403) {
        useToastStore.getState().push({
          msg: detail || "没有权限执行该操作",
          kind: "warning",
        });
      } else {
        useToastStore.getState().push({
          msg: detail || "服务器错误，请稍后重试",
          sub: `HTTP ${res.status}`,
          kind: "error",
        });
      }
    }
    const exposedHeaders: Record<string, string> = {};
    for (const k of EXPOSED_HEADERS) {
      const v = res.headers.get(k);
      if (v != null) exposedHeaders[k] = v;
    }
    throw new ApiError(
      res.status,
      detail ?? res.statusText,
      rawDetail,
      Object.keys(exposedHeaders).length ? exposedHeaders : undefined,
    );
  }

  if (opts?.expectedEtag && res.headers.get("ETag") !== opts.expectedEtag) {
    throw new ApiError(409, "请求内容已变化，请刷新后重试");
  }
  if (res.status === 204) return undefined as T;
  return opts?.responseType === "blob" ? (res.blob() as Promise<T>) : res.json();
}

export const apiClient = {
  get: <T>(path: string, init?: RequestInit) => request<T>(path, init),
  getForSession: <T>(
    path: string,
    init: RequestInit,
    session: AuthSessionOwner,
    expectedEtag: string,
  ) => request<T>(path, init, { session, expectedEtag }),
  getBlob: (path: string, init?: RequestInit) =>
    request<Blob>(path, init, { responseType: "blob" }),
  silentGetBlob: (path: string, init?: RequestInit) =>
    request<Blob>(path, init, { responseType: "blob", silent: true }),
  silentGet: <T>(path: string, init?: RequestInit) => request<T>(path, init, { silent: true }),
  post: <T>(path: string, body?: unknown, extra?: RequestInit) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}), ...extra }),
  silentPost: <T>(path: string, body?: unknown, extra?: RequestInit) =>
    request<T>(
      path,
      { method: "POST", body: JSON.stringify(body ?? {}), ...extra },
      { silent: true },
    ),
  put: <T>(path: string, body?: unknown, extra?: RequestInit) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}), ...extra }),
  patch: <T>(path: string, body?: unknown, extra?: RequestInit) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}), ...extra }),
  patchForSession: <T>(path: string, body: unknown, session: AuthSessionOwner) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }, { session, silent: true }),
  delete: <T>(path: string, body?: unknown, extra?: RequestInit) =>
    request<T>(path, {
      method: "DELETE",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      ...extra,
    }),
  silentDelete: <T>(path: string, init?: RequestInit) =>
    request<T>(path, { method: "DELETE", ...init }, { silent: true }),
  publicGet: <T>(path: string) => request<T>(path, undefined, { anonymous: true }),
  publicPost: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }, { anonymous: true }),
};

export { ApiError };
