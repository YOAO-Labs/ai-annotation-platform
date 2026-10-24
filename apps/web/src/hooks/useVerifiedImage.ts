import { useCallback, useEffect, useRef, useState } from "react";
import { apiClient, ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

// Grid previews share the bounded snapshot service with foreground originals.
const pendingPreviews = new Set<() => void>();
let activePreviews = 0;

function previewDelay(signal: AbortSignal, milliseconds: number) {
  return new Promise<void>((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("预览请求已取消", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

async function fetchPreview(path: string, signal: AbortSignal) {
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      return await apiClient.silentGetBlob(path, { signal });
    } catch (error) {
      const detail = error instanceof ApiError ? error.detailRaw : null;
      if (
        attempt >= 2 ||
        !(error instanceof ApiError) ||
        error.status !== 429 ||
        !detail ||
        typeof detail !== "object" ||
        !("code" in detail) ||
        detail.code !== "snapshot_capacity_exceeded"
      )
        throw error;
      await previewDelay(signal, 500 * (attempt + 1));
    }
  }
}

function imageBlob(path: string, signal: AbortSignal): Promise<Blob> {
  if (!path.endsWith("/preview")) return apiClient.silentGetBlob(path, { signal });
  return new Promise((resolve, reject) => {
    const abort = () => {
      pendingPreviews.delete(start);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("预览请求已取消", "AbortError"));
    };
    const start = () => {
      pendingPreviews.delete(start);
      signal.removeEventListener("abort", abort);
      if (signal.aborted) return abort();
      activePreviews++;
      void fetchPreview(path, signal)
        .then(resolve, reject)
        .finally(() => {
          activePreviews--;
          pendingPreviews.values().next().value?.();
        });
    };
    if (signal.aborted) abort();
    else if (activePreviews < 2) start();
    else {
      pendingPreviews.add(start);
      signal.addEventListener("abort", abort, { once: true });
    }
  });
}

export function verifiedContentError(error: unknown): string {
  const code =
    error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object"
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  if (code === "dataset_archived") return "数据集已归档；请由创建者或超级管理员恢复后再执行此操作";
  if (code === "dataset_lifecycle_changed")
    return "数据集生命周期已变化，请刷新并在当前权限下创建新操作";
  if (code === "dataset_busy") return "数据集正在处理其他操作，请稍后重新核对";
  if (
    [
      "content_unverified",
      "no_readable_original",
      "original_location_ambiguous",
      "original_relation_conflict",
      "content_link_missing",
      "original_link_invalid",
    ].includes(code)
  )
    return "原件缺少可用的核验归属或关联，请刷新并联系管理员核对";
  if (code === "content_source_missing") return "原件对象不存在，请联系管理员核对保存位置";
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
  if (
    [
      "source_revision_changed",
      "revision_preflight_required",
      "revision_selection_changed",
    ].includes(code)
  )
    return "候选或当前修订已变化，请重新扫描和预检后核对";
  if (code === "rules_impact_changed") return "规则或接纳路径已变化，请重新预览影响后确认";
  if (code === "source_busy") return "来源有扫描或登记正在执行，请稍后重新核对";
  if (code === "accepted_revision_item_deleted")
    return "已接纳的新样本已被删除；本次确认不会重新创建，请重新扫描";
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
  const owner = JSON.stringify([userId, sessionId, path, attempt]);
  const ownerRef = useRef(owner);
  const generation = useRef(0);
  ownerRef.current = owner;
  const usable = !!token && localStorage.getItem("token") === token;
  const [result, setResult] = useState<{
    owner: string;
    epoch: number;
    url?: string;
    error?: string;
  } | null>(null);
  useEffect(() => {
    if (!path || !userId || !usable) return;
    const controller = new AbortController();
    const epoch = ++generation.current;
    let retired = false;
    let url: string | undefined;
    let image: HTMLImageElement | undefined;
    const current = () =>
      !retired &&
      ownerRef.current === owner &&
      useAuthStore.getState().user?.id === userId &&
      useAuthStore.getState().sessionId === sessionId &&
      useAuthStore.getState().token === localStorage.getItem("token");
    const load = async () => {
      for (let refresh = 0; ; refresh++) {
        const presented = useAuthStore.getState().token;
        try {
          const blob = await imageBlob(path, controller.signal);
          if (!current()) throw new DOMException("Image owner retired", "AbortError");
          if (presented === useAuthStore.getState().token) return blob;
        } catch (error) {
          if (!current() || presented === useAuthStore.getState().token) throw error;
        }
        if (refresh >= 1) throw new DOMException("Image credentials changed", "AbortError");
      }
    };
    void load()
      .then(async (blob) => {
        if (!current()) return;
        url = URL.createObjectURL(blob);
        image = new Image();
        image.src = url;
        await image.decode();
        if (current()) setResult({ owner, epoch, url });
        else {
          URL.revokeObjectURL(url);
          url = undefined;
        }
      })
      .catch((error) => {
        if (current()) setResult({ owner, epoch, error: verifiedContentError(error) });
        if (url) URL.revokeObjectURL(url);
        url = undefined;
      });
    return () => {
      retired = true;
      generation.current += 1;
      controller.abort();
      if (image) image.src = "";
      if (url) URL.revokeObjectURL(url);
    };
  }, [owner, path, sessionId, usable, userId]);
  const owned = result?.owner === owner && result.epoch === generation.current ? result : null;
  const url =
    path && token && localStorage.getItem("token") === token ? (owned?.url ?? null) : null;
  return {
    url,
    error: owned?.error ?? null,
    ready: !!url,
    retry: useCallback(() => setAttempt((n) => n + 1), []),
  };
}
