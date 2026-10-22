import { useCallback, useEffect, useState } from "react";
import { apiClient, ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

export function verifiedContentError(error: unknown): string {
  const code =
    error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object"
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  if (
    [
      "content_mismatch",
      "source_changed",
      "content_binding_changed",
      "root_identity_changed",
      "directory_identity_changed",
    ].includes(code)
  )
    return "来源图片已变化，已保留标注并禁止编辑，请联系管理员核对来源";
  if (
    [
      "source_unavailable",
      "location_unavailable",
      "api_read_not_verified",
      "content_source_unavailable",
      "filesystem_unavailable",
      "root_not_configured",
    ].includes(code)
  )
    return "原图暂不可读，请联系管理员检查来源、挂载和节点探测";
  if (error instanceof ApiError && error.status === 429) return "原图读取繁忙，请稍后重试";
  if (error instanceof ApiError && [413, 415].includes(error.status))
    return "图片格式或大小超出当前原地引用支持范围";
  return error instanceof Error ? error.message : "原图读取失败，请重试";
}

/** One decoded Blob belongs to a login session, content locator and attempt. */
export function useVerifiedImage(path: string | null) {
  const userId = useAuthStore((s) => s.user?.id);
  const sessionId = useAuthStore((s) => s.sessionId);
  const token = useAuthStore((s) => s.token);
  const [attempt, setAttempt] = useState(0);
  const owner = JSON.stringify([userId, sessionId, token, path, attempt]);
  const [result, setResult] = useState<{ owner: string; url?: string; error?: string } | null>(
    null,
  );
  useEffect(() => {
    if (!path || !userId || !token) return;
    const controller = new AbortController();
    let retired = false;
    let url: string | undefined;
    let image: HTMLImageElement | undefined;
    const current = () =>
      !retired &&
      useAuthStore.getState().user?.id === userId &&
      useAuthStore.getState().sessionId === sessionId &&
      useAuthStore.getState().token === localStorage.getItem("token");
    void apiClient
      .getBlob(path, { signal: controller.signal })
      .then(async (blob) => {
        if (!current()) return;
        url = URL.createObjectURL(blob);
        image = new Image();
        image.src = url;
        await image.decode();
        if (current()) setResult({ owner, url });
        else {
          URL.revokeObjectURL(url);
          url = undefined;
        }
      })
      .catch((error) => {
        if (current()) setResult({ owner, error: verifiedContentError(error) });
        if (url) URL.revokeObjectURL(url);
        url = undefined;
      });
    return () => {
      retired = true;
      controller.abort();
      if (image) image.src = "";
      if (url) URL.revokeObjectURL(url);
    };
  }, [owner, path, sessionId, token, userId]);
  const owned = result?.owner === owner ? result : null;
  const url =
    path && token && localStorage.getItem("token") === token ? (owned?.url ?? null) : null;
  return {
    url,
    error: owned?.error ?? null,
    ready: !!url,
    retry: useCallback(() => setAttempt((n) => n + 1), []),
  };
}
