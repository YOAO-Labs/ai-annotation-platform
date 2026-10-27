import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { bugReportsApi, type BugAttachment } from "@/api/bug-reports";
import { getAuthSessionRevision } from "@/stores/authSession";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { useSessionRevision } from "./useSessionGuard";

export function captureBugReportOwner() {
  const auth = useAuthStore.getState();
  return {
    userId: auth.user?.id ?? null,
    token: auth.token,
    sessionId: auth.sessionId,
    revision: getAuthSessionRevision(),
  };
}

export function isCurrentBugReportOwner(owner: ReturnType<typeof captureBugReportOwner>) {
  const auth = useAuthStore.getState();
  return Boolean(
    owner.userId &&
    owner.token &&
    owner.token === auth.token &&
    owner.sessionId === auth.sessionId &&
    owner.revision === getAuthSessionRevision() &&
    isCurrentAuthOwner(owner.userId),
  );
}

function downloadError(error: unknown) {
  const status = (error as { status?: number } | null)?.status;
  if (status === 403) return "无权下载该附件";
  if (status === 404) return "附件不存在，请联系管理员";
  if (status === 410) return "附件已过期";
  if (status === 409) return "附件当前不可下载，请联系管理员";
  return "下载失败，请重试";
}

/** Keep downloaded bytes and UI feedback inside their report and login lifetime. */
export function useBugAttachmentDownload(reportId: string | null, lifecycle?: unknown) {
  const revision = useSessionRevision();
  const generation = useRef(0);
  const pending = useRef(new Map<string, AbortController>());
  const [pendingKeys, setPendingKeys] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});

  const invalidate = useCallback(() => {
    generation.current += 1;
    pending.current.forEach((controller) => controller.abort());
    pending.current.clear();
    setPendingKeys(new Set());
    setErrors({});
  }, []);

  useLayoutEffect(() => {
    invalidate();
    const controllers = pending.current;
    return () => {
      generation.current += 1;
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
    };
  }, [reportId, lifecycle, revision, invalidate]);

  const download = async (attachment: BugAttachment) => {
    const owner = captureBugReportOwner();
    if (!reportId || !isCurrentBugReportOwner(owner) || pending.current.has(attachment.storageKey))
      return;
    const scope = generation.current;
    const current = () => scope === generation.current && isCurrentBugReportOwner(owner);
    const controller = new AbortController();
    pending.current.set(attachment.storageKey, controller);
    setPendingKeys(new Set(pending.current.keys()));
    setErrors((previous) => {
      const next = { ...previous };
      delete next[attachment.storageKey];
      return next;
    });
    let objectUrl: string | undefined;
    let anchor: HTMLAnchorElement | undefined;
    try {
      const blob = await bugReportsApi.downloadAttachment(
        reportId,
        attachment.storageKey,
        controller.signal,
      );
      if (!current()) return;
      objectUrl = URL.createObjectURL(blob);
      anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = attachment.fileName;
      anchor.hidden = true;
      document.body.appendChild(anchor);
      anchor.click();
    } catch (error) {
      if (current()) {
        setErrors((previous) => ({
          ...previous,
          [attachment.storageKey]: downloadError(error),
        }));
      }
    } finally {
      anchor?.remove();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      if (current()) {
        pending.current.delete(attachment.storageKey);
        setPendingKeys(new Set(pending.current.keys()));
      }
    }
  };

  return { download, pendingKeys, errors, invalidate };
}
