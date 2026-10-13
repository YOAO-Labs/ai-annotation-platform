import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AssigneeAvatarStack } from "./AssigneeAvatarStack";

const UPLOAD_TOKEN = "0123456789abcdef0123456789abcdef";

describe("AssigneeAvatarStack", () => {
  it("有头像引用时渲染图片,无引用时回退首字母(同一组内混排)", () => {
    const { container } = render(
      <AssigneeAvatarStack
        users={[
          { id: "u1", name: "王芳", avatar_initial: "王", avatar_ref: "preset:pixel-05" },
          { id: "u2", name: "刘洋", avatar_initial: "刘", avatar_ref: null },
        ]}
      />,
    );

    const images = container.querySelectorAll("img");
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute("src", "/avatars/pixel/pixel-05.svg");
    // 无头像者仍渲染首字母
    expect(screen.getByText("刘")).toBeInTheDocument();
  });

  it("上传头像沿用后端能力 URL", () => {
    const { container } = render(
      <AssigneeAvatarStack
        users={[{ id: "u1", name: "王芳", avatar_ref: `upload:${UPLOAD_TOKEN}` }]}
      />,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      `/api/v1/avatars/${UPLOAD_TOKEN}`,
    );
  });

  it("无头像时不产生 img(保持既有首字母行为)", () => {
    const { container } = render(
      <AssigneeAvatarStack users={[{ id: "u1", name: "王芳", avatar_initial: "王" }]} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("王")).toBeInTheDocument();
  });

  it("超过 max 时显示溢出计数", () => {
    render(
      <AssigneeAvatarStack
        max={1}
        users={[
          { id: "u1", name: "甲", avatar_initial: "甲" },
          { id: "u2", name: "乙", avatar_initial: "乙" },
        ]}
      />,
    );
    expect(screen.getByText("+1")).toBeInTheDocument();
  });
});
