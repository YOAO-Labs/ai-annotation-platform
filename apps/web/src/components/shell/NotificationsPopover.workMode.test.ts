import { describe, it, expect } from "vitest";
import { chooseWorkMode } from "./NotificationsPopover";

describe("chooseWorkMode", () => {
  it("审核任务优先打开审核模式", () => {
    expect(chooseWorkMode(new Set(["annotation.write", "review.write"]), "review")).toBe("review");
  });

  it("只能审核的账号即使任务不在 review 也进审核模式", () => {
    expect(chooseWorkMode(new Set(["review.write"]), "in_progress")).toBe("review");
  });

  it("只能标注的账号审核通知回落标注模式，由调用方给出权限错误", () => {
    expect(chooseWorkMode(new Set(["annotation.write"]), "review")).toBe("annotate");
  });

  it("两种能力都有时默认标注模式", () => {
    expect(chooseWorkMode(new Set(["annotation.write", "review.write"]), "in_progress")).toBe(
      "annotate",
    );
  });

  it("无写能力时仍返回标注模式，调用方据此拒绝", () => {
    expect(chooseWorkMode(new Set(["task.read"]))).toBe("annotate");
  });
});
