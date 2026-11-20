// canEditMask / canCommitMask 单测 (A4 锁定绕过的纯函数层)。

import { describe, expect, it } from "vitest";
import { canCommitMask, canEditMask, maskEditBlockReason } from "./canEditMask";

const OPEN = {
  taskReadOnly: false,
  annotationLocked: false,
  trackLocked: false,
  segmentLocked: false,
};

describe("canEditMask · 锁与只读门", () => {
  it("ready 态 + 无锁 → 允许", () => {
    expect(canEditMask({ ...OPEN, editorPhase: "ready" })).toBe(true);
  });
  it("dirty 态 + 无锁 → 允许", () => {
    expect(canEditMask({ ...OPEN, editorPhase: "dirty" })).toBe(true);
  });
  it("taskReadOnly → 拒绝 (review/completed 锁)", () => {
    expect(canEditMask({ ...OPEN, taskReadOnly: true, editorPhase: "ready" })).toBe(false);
  });
  it("annotationLocked → 拒绝 (per-annotation is_locked)", () => {
    expect(canEditMask({ ...OPEN, annotationLocked: true, editorPhase: "dirty" })).toBe(false);
  });
  it("trackLocked → 拒绝 (视频轨迹 lock)", () => {
    expect(canEditMask({ ...OPEN, trackLocked: true, editorPhase: "ready" })).toBe(false);
  });
  it("segmentLocked → 拒绝 (assignment/segment lock)", () => {
    expect(canEditMask({ ...OPEN, segmentLocked: true, editorPhase: "ready" })).toBe(false);
  });
  it("loading 态 → 拒绝 (迟到 GET 写入窗口)", () => {
    expect(canEditMask({ ...OPEN, editorPhase: "loading" })).toBe(false);
  });
  it("saving 态 → 拒绝 (单飞保存期间禁止 pointer)", () => {
    expect(canEditMask({ ...OPEN, editorPhase: "saving" })).toBe(false);
  });
  it("idle / error 态 → 拒绝", () => {
    expect(canEditMask({ ...OPEN, editorPhase: "idle" })).toBe(false);
    expect(canEditMask({ ...OPEN, editorPhase: "error" })).toBe(false);
  });
  it("返回稳定且按安全优先级排序的禁用原因", () => {
    expect(
      maskEditBlockReason({
        ...OPEN,
        taskReadOnly: true,
        annotationLocked: true,
        editorPhase: "saving",
      }),
    ).toBe("task_read_only");
    expect(maskEditBlockReason({ ...OPEN, trackLocked: true, editorPhase: "ready" })).toBe(
      "track_locked",
    );
    expect(maskEditBlockReason({ ...OPEN, editorPhase: "loading" })).toBe("editor_loading");
    expect(maskEditBlockReason({ ...OPEN, editorPhase: "dirty" })).toBeNull();
  });
});

describe("canCommitMask · Enter 真实提交条件", () => {
  it("dirty + ready/dirty 相位 → 允许", () => {
    expect(canCommitMask("dirty", true)).toBe(true);
    expect(canCommitMask("ready", true)).toBe(true);
  });
  it("无变化 (dirty=false) → 拒绝 (不物化 held keyframe)", () => {
    expect(canCommitMask("ready", false)).toBe(false);
    expect(canCommitMask("dirty", false)).toBe(false);
  });
  it("loading/saving/idle/error 相位 → 拒绝", () => {
    for (const phase of ["loading", "saving", "idle", "error"] as const) {
      expect(canCommitMask(phase, true)).toBe(false);
    }
  });
});
