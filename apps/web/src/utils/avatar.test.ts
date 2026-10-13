import { describe, expect, it } from "vitest";

import { avatarInitial, presetAvatarUrl, resolveAvatarUrl } from "./avatar";

describe("resolveAvatarUrl", () => {
  it("内置头像解析为随前端发布的静态 SVG", () => {
    expect(resolveAvatarUrl("preset:pixel-01")).toBe("/avatars/pixel/pixel-01.svg");
    expect(presetAvatarUrl("pixel-32")).toBe("/avatars/pixel/pixel-32.svg");
  });

  it("上传头像解析为后端能力 URL", () => {
    const token = "0123456789abcdef0123456789abcdef";
    expect(resolveAvatarUrl(`upload:${token}`)).toBe(`/api/v1/avatars/${token}`);
  });

  it("空值返回 null（调用方回退首字母）", () => {
    expect(resolveAvatarUrl(null)).toBeNull();
    expect(resolveAvatarUrl(undefined)).toBeNull();
    expect(resolveAvatarUrl("")).toBeNull();
  });

  it("拒绝畸形/越界引用，绝不拼进 img src", () => {
    expect(resolveAvatarUrl("preset:../../etc/passwd")).toBeNull();
    expect(resolveAvatarUrl("preset:PIXEL-01")).toBeNull();
    expect(resolveAvatarUrl(`preset:${"a".repeat(41)}`)).toBeNull();
    expect(resolveAvatarUrl(`upload:${"A".repeat(32)}`)).toBeNull();
    expect(resolveAvatarUrl("upload:0123")).toBeNull();
    expect(resolveAvatarUrl("https://evil.test/a.svg")).toBeNull();
    expect(resolveAvatarUrl("data:image/svg+xml,<svg/>")).toBeNull();
  });
});

describe("avatarInitial", () => {
  it("优先后端下发的 avatar_initial", () => {
    expect(avatarInitial("ann", "ann@x.io", "甲")).toBe("甲");
  });

  it("回退到姓名首字母并大写", () => {
    expect(avatarInitial("ann", "ann@x.io")).toBe("A");
    expect(avatarInitial(null, "ann@x.io")).toBe("A");
  });

  it("完全无信息时给占位符", () => {
    expect(avatarInitial(null, null)).toBe("?");
    expect(avatarInitial("   ")).toBe("?");
  });
});
