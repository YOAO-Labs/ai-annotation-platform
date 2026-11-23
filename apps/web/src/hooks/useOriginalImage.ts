import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  captureOriginalOwner,
  isOriginalOwnerCurrent,
  type OriginalResource,
} from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";
import { createOriginalImageAddress } from "@/utils/originalImageDisplay";
import { readOriginalImage } from "@/utils/originalImageCache";
import {
  recordOriginalManualRetrySuccess,
  recordOriginalReadError,
} from "@/utils/originalReadDiagnostics";
import {
  extractVerifiedContentErrorReceipt,
  verifiedContentError,
  type VerifiedContentErrorReceipt,
} from "./useVerifiedImage";

let addressVersion = 0;

function resourceIdentityTemplate(resource: OriginalResource): string {
  switch (resource.kind) {
    case "task":
      return "/api/v1/files/tasks/{task_id}/original-identity";
    case "camera":
      return "/api/v1/files/tasks/{task_id}/links/{link_id}/original-identity";
    case "item":
      return "/api/v1/datasets/{dataset_id}/items/{item_id}/original-identity";
  }
}

export function useOriginalImage(resource: OriginalResource | null, sourceKey?: string | null) {
  const userId = useAuthStore((state) => state.user?.id);
  const sessionId = useAuthStore((state) => state.sessionId);
  const token = useAuthStore((state) => state.token);
  const [attempt, setAttempt] = useState(0);
  const resourceKey = resource ? JSON.stringify(resource) : null;
  const identity = JSON.stringify([userId, sessionId, resourceKey, sourceKey ?? null, attempt]);
  const fence = useRef({ identity, active: true, epoch: 0 });
  if (fence.current.identity !== identity) {
    fence.current.active = false;
    fence.current = { identity, active: true, epoch: 0 };
  }
  const scope = fence.current;
  const usable = !!token && isOriginalOwnerCurrent(captureOriginalOwner());
  const [result, setResult] = useState<{
    identity: string;
    epoch: number;
    url: string;
    version: number;
    error: string | null;
    errorReceipt: VerifiedContentErrorReceipt | null;
  } | null>(null);
  useEffect(() => {
    const owner = captureOriginalOwner();
    if (!resourceKey || !usable || !owner) return;
    const target = JSON.parse(resourceKey) as OriginalResource;
    const controller = new AbortController();
    scope.active = true;
    const epoch = ++scope.epoch;
    let address: Awaited<ReturnType<typeof createOriginalImageAddress>> | undefined;
    let image: HTMLImageElement | undefined;
    let published = false;
    const current = () =>
      scope.active &&
      epoch === scope.epoch &&
      fence.current === scope &&
      !controller.signal.aborted &&
      isOriginalOwnerCurrent(owner);
    void (async () => {
      try {
        const download = await readOriginalImage(target, { owner, signal: controller.signal });
        if (!download) return;
        if (!current()) return;
        address = await createOriginalImageAddress(download.blob, controller.signal);
        if (!current()) return;
        image = new Image();
        image.src = address.url;
        try {
          await image.decode();
        } catch {
          throw new Error("浏览器无法解码此图片格式；可以下载已核验原件");
        }
        if (current()) {
          published = true;
          if (attempt > 0) {
            recordOriginalManualRetrySuccess({
              attempt,
              resourceKind: target.kind,
              requestPath: resourceIdentityTemplate(target),
              contentId:
                typeof download.identity?.content_id === "string"
                  ? download.identity.content_id
                  : null,
              sha256:
                typeof download.identity?.sha256 === "string" ? download.identity.sha256 : null,
            });
          }
          setResult({
            identity,
            epoch,
            url: address.url,
            version: ++addressVersion,
            error: null,
            errorReceipt: null,
          });
        }
      } catch (error) {
        if (current()) {
          const errorReceipt = extractVerifiedContentErrorReceipt(error);
          if (!(error instanceof DOMException && error.name === "AbortError")) {
            recordOriginalReadError({
              attempt,
              resourceKind: target.kind,
              requestPath:
                error instanceof ApiError && error.requestPath
                  ? error.requestPath
                  : resourceIdentityTemplate(target),
              status: error instanceof ApiError ? error.status : null,
              code: errorReceipt?.code ?? null,
              reason: errorReceipt?.reason ?? null,
              requestId: errorReceipt?.requestId ?? null,
            });
          }
          setResult({
            identity,
            epoch,
            url: "",
            version: 0,
            error:
              (error as { status?: number })?.status === 413
                ? "原件超过完整下载大小限制；请联系管理员处理"
                : verifiedContentError(error),
            errorReceipt,
          });
        }
      } finally {
        if (!published) address?.release();
      }
    })();
    return () => {
      scope.active = false;
      scope.epoch += 1;
      controller.abort();
      if (image) image.src = "";
      address?.release();
      address = undefined;
      setResult((value) => (value?.identity === identity && value.epoch === epoch ? null : value));
    };
  }, [attempt, identity, resourceKey, scope, usable]);
  const owned =
    usable &&
    resource &&
    scope.active &&
    result?.identity === identity &&
    result.epoch === scope.epoch
      ? result
      : null;
  return {
    url: owned?.url || null,
    version: owned?.version ?? 0,
    error: owned?.error ?? null,
    errorReceipt: owned?.errorReceipt ?? null,
    ready: !!owned?.url,
    retry: useCallback(() => setAttempt((value) => value + 1), []),
  };
}
