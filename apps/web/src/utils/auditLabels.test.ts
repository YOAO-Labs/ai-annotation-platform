/**
 * v0.8.3 · auditLabels 工具单测。
 */
import { describe, it, expect } from "vitest";
import {
  AUDIT_ACTION_LABELS,
  auditActionLabel,
  AUDIT_BUSINESS_ACTIONS,
  AUDIT_TARGET_TYPES,
} from "./auditLabels";

describe("auditActionLabel", () => {
  it("已知 action → 中文标签", () => {
    expect(auditActionLabel("auth.login")).toBe("登录");
    expect(auditActionLabel("user.invite")).toBe("邀请用户");
    expect(auditActionLabel("project.delete")).toBe("删除项目");
    expect(auditActionLabel("video_mask.keyframe_operate")).toBe("操作视频 Mask 关键帧");
    expect(auditActionLabel("storage.fault")).toBe("存储访问故障");
  });

  it("未知 action → 原 action 串", () => {
    expect(auditActionLabel("custom.unknown")).toBe("custom.unknown");
  });
});

describe("AUDIT_BUSINESS_ACTIONS / AUDIT_TARGET_TYPES", () => {
  it("AUDIT_BUSINESS_ACTIONS 不包含 http.* 动作", () => {
    for (const a of AUDIT_BUSINESS_ACTIONS) {
      expect(a.startsWith("http.")).toBe(false);
    }
    // 仍包含核心业务动作
    expect(AUDIT_BUSINESS_ACTIONS).toContain("auth.login");
    expect(AUDIT_BUSINESS_ACTIONS).toContain("user.invite");
    expect(AUDIT_BUSINESS_ACTIONS).toContain("video_tracker_job.decision");
    expect(AUDIT_BUSINESS_ACTIONS).toContain("storage.fault");
  });

  it("AUDIT_ACTION_LABELS 包含 http.post / http.patch / http.put / http.delete", () => {
    expect(AUDIT_ACTION_LABELS["http.post"]).toBe("HTTP·写");
    expect(AUDIT_ACTION_LABELS["http.patch"]).toBe("HTTP·改");
    expect(AUDIT_ACTION_LABELS["http.put"]).toBe("HTTP·改");
    expect(AUDIT_ACTION_LABELS["http.delete"]).toBe("HTTP·删");
  });

  it("AUDIT_TARGET_TYPES 含核心 6 类 + v0.9.9 新增 ml_backend / bug_report", () => {
    expect(AUDIT_TARGET_TYPES).toEqual([
      "user",
      "project",
      "task",
      "dataset",
      "storage_location",
      "annotation",
      "system",
      "ml_backend",
      "bug_report",
      "video_tracker_job",
    ]);
  });
});
