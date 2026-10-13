import { describe, it, expect } from "vitest";
import { ApiError } from "@/api/client";
import { maskMutationErrorMessage, maskMutationRecovery } from "./maskMutationPolicy";

const reasonError = (status: number, reason: string) =>
  new ApiError(status, "request failed", { reason });

describe("maskMutationRecovery", () => {
  it("服务端权威的版本/锁冲突只允许刷新范围，不允许盲重试", () => {
    for (const reason of [
      "expected_versions_missing",
      "version_mismatch",
      "scope_stale",
      "task_lock_conflict",
      "annotation_locked",
      "segment_lock_conflict",
      "overlap_conflict",
      "idempotency_conflict",
    ]) {
      expect(maskMutationRecovery(reasonError(409, reason))).toEqual({
        retry: false,
        refresh: true,
      });
    }
  });

  it("422 是请求本身无效：重试与刷新都不提供", () => {
    expect(maskMutationRecovery(reasonError(422, "geometry_invalid"))).toEqual({
      retry: false,
      refresh: false,
    });
  });

  it("传输类失败按状态码给出重试/刷新组合", () => {
    expect(maskMutationRecovery(reasonError(500, "internal"))).toEqual({
      retry: true,
      refresh: false,
    });
    expect(maskMutationRecovery(reasonError(429, "rate_limited"))).toEqual({
      retry: true,
      refresh: false,
    });
    expect(maskMutationRecovery(reasonError(408, "timeout"))).toEqual({
      retry: true,
      refresh: false,
    });
    expect(maskMutationRecovery(reasonError(423, "locked"))).toEqual({
      retry: false,
      refresh: true,
    });
    expect(maskMutationRecovery(reasonError(428, "precondition"))).toEqual({
      retry: false,
      refresh: true,
    });
  });

  it("非 ApiError（网络断开/编程错误）默认两者都可用", () => {
    expect(maskMutationRecovery(new TypeError("Failed to fetch"))).toEqual({
      retry: true,
      refresh: true,
    });
    expect(maskMutationRecovery(undefined)).toEqual({ retry: true, refresh: true });
  });

  it("未知 reason 不触发 reason 白名单，按状态码回落", () => {
    expect(maskMutationRecovery(reasonError(409, "unknown_reason"))).toEqual({
      retry: false,
      refresh: true,
    });
    expect(maskMutationRecovery(reasonError(400, "unknown_reason"))).toEqual({
      retry: false,
      refresh: false,
    });
  });
});

describe("maskMutationErrorMessage", () => {
  it("把结构化 reason 映射为面向用户的标签", () => {
    expect(maskMutationErrorMessage(reasonError(409, "version_mismatch"))).toBe(
      "来源 Mask 已变更，草稿已保留",
    );
    expect(maskMutationErrorMessage(reasonError(409, "task_lock_conflict"))).toBe(
      "任务正由其他用户编辑",
    );
  });

  it("未知 reason 依次回落 detail.message、reason、transport message", () => {
    expect(maskMutationErrorMessage(new ApiError(409, "transport", { reason: "new_reason" }))).toBe(
      "new_reason",
    );
    expect(
      maskMutationErrorMessage(
        new ApiError(409, "transport", { reason: "new_reason", message: "细节" }),
      ),
    ).toBe("细节");
    expect(maskMutationErrorMessage(new ApiError(500, "boom"))).toBe("boom");
  });

  it("非 ApiError 沿用 Error message", () => {
    expect(maskMutationErrorMessage(new Error("offline"))).toBe("offline");
    expect(maskMutationErrorMessage(42)).toBe("42");
  });
});
