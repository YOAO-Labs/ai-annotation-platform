import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import {
  appendOriginalReadDiagnostics,
  clearOriginalReadDiagnostics,
  getOriginalReadDiagnosticsSnapshot,
  originalReadDiagnosticsConsoleEntry,
  recordOriginalManualRetrySuccess,
  recordOriginalReadError,
  sanitizeOriginalReadPath,
} from "./originalReadDiagnostics";

describe("originalReadDiagnostics", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/projects/p1/annotate?task=t1");
    useAuthStore.getState().setAuth("token-a", { id: "user-a" } as MeResponse);
    clearOriginalReadDiagnostics();
  });

  afterEach(() => {
    clearOriginalReadDiagnostics();
    useAuthStore.getState().logout();
  });

  it("sanitizes UUIDs and strips query parameters from original paths", () => {
    const taskId = "11111111-1111-1111-1111-111111111111";
    const linkId = "22222222-2222-2222-2222-222222222222";
    const datasetId = "33333333-3333-3333-3333-333333333333";
    const itemId = "44444444-4444-4444-4444-444444444444";

    expect(sanitizeOriginalReadPath(`/files/tasks/${taskId}/original-identity`)).toBe(
      "/api/v1/files/tasks/{task_id}/original-identity",
    );
    expect(
      sanitizeOriginalReadPath(
        `/api/v1/files/tasks/${taskId}/original?content_id=${itemId}&sha256=${"a".repeat(64)}&token=secret`,
      ),
    ).toBe("/api/v1/files/tasks/{task_id}/original");
    expect(
      sanitizeOriginalReadPath(`/files/tasks/${taskId}/links/${linkId}/original-identity`),
    ).toBe("/api/v1/files/tasks/{task_id}/links/{link_id}/original-identity");
    expect(
      sanitizeOriginalReadPath(`/datasets/${datasetId}/items/${itemId}/original?content_id=x`),
    ).toBe("/api/v1/datasets/{dataset_id}/items/{item_id}/original");
    expect(
      sanitizeOriginalReadPath(`/files/tasks/${taskId}/content?sha256=${"a".repeat(64)}`),
    ).toBe("/api/v1/files/tasks/{task_id}/content");
  });

  it("records sanitized error and manual retry recovery events scoped to route and session", () => {
    const taskId = "11111111-1111-1111-1111-111111111111";
    const contentId = "44444444-4444-4444-4444-444444444444";
    const sha256 = "abcdef012345" + "9".repeat(52);

    recordOriginalReadError({
      attempt: 0,
      resourceKind: "task",
      requestPath: `/files/tasks/${taskId}/original-identity?token=leak`,
      status: 409,
      code: "storage_original_busy",
      reason: null,
      requestId: "req-diag-1",
    });
    recordOriginalManualRetrySuccess({
      attempt: 1,
      resourceKind: "task",
      requestPath: `/files/tasks/${taskId}/original-identity`,
      contentId,
      sha256,
    });

    const snapshot = getOriginalReadDiagnosticsSnapshot();
    expect(snapshot).not.toBeNull();
    expect(snapshot?.events).toHaveLength(2);
    expect(snapshot?.events[0]).toMatchObject({
      kind: "error",
      requestPath: "/api/v1/files/tasks/{task_id}/original-identity",
      status: 409,
      code: "storage_original_busy",
      requestId: "req-diag-1",
    });
    expect(snapshot?.events[1]).toMatchObject({
      kind: "manual_retry_recovered",
      attempt: 1,
      contentId,
      sha256Prefix: "abcdef012345",
    });

    const appended = appendOriginalReadDiagnostics("用户描述", snapshot);
    expect(appended).toContain("### Original Read Diagnostics");
    expect(appended).toContain("req-diag-1");
    expect(appended).not.toContain("leak");
    expect(appended).not.toContain(sha256);

    const entry = originalReadDiagnosticsConsoleEntry(snapshot);
    expect(entry?.msg).toBe("[original-read-diagnostics]");
    expect(entry?.stack).toContain("storage_original_busy");

    window.history.replaceState({}, "", "/projects/p2/annotate");
    expect(getOriginalReadDiagnosticsSnapshot()).toBeNull();
    window.history.replaceState({}, "", "/projects/p1/annotate?task=t1");
    expect(getOriginalReadDiagnosticsSnapshot()).not.toBeNull();

    useAuthStore.getState().logout();
    expect(getOriginalReadDiagnosticsSnapshot()).toBeNull();
  });
});
