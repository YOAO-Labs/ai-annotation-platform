import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UserAvatar } from "./UserAvatar";

const UPLOAD_TOKEN = "0123456789abcdef0123456789abcdef";

describe("UserAvatar", () => {
  it("无头像引用时只显示首字母,不渲染 img", () => {
    const { container } = render(
      <UserAvatar user={{ id: "u1", name: "张三", avatar_ref: null }} />,
    );
    expect(screen.getByText("张")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("引用为空字符串时不渲染 img", () => {
    const { container } = render(<UserAvatar user={{ name: "Ann", avatar_ref: "" }} />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("内置像素头像渲染静态 SVG 路径", () => {
    const { container } = render(
      <UserAvatar user={{ name: "Ann", avatar_ref: "preset:pixel-07" }} />,
    );
    const img = container.querySelector("img");
    expect(img).toHaveAttribute("src", "/avatars/pixel/pixel-07.svg");
    // 姓名已在旁侧展示,头像不重复播报。
    expect(img).toHaveAttribute("aria-hidden", "true");
  });

  it("上传头像渲染后端能力 URL", () => {
    const { container } = render(
      <UserAvatar user={{ name: "Ann", avatar_ref: `upload:${UPLOAD_TOKEN}` }} />,
    );
    expect(container.querySelector("img")).toHaveAttribute(
      "src",
      `/api/v1/avatars/${UPLOAD_TOKEN}`,
    );
  });

  it("图片加载失败时移除 img 并保留首字母", () => {
    const { container } = render(
      <UserAvatar user={{ name: "Ann", avatar_ref: "preset:pixel-03" }} />,
    );
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img!);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("优先使用后端下发的 avatar_initial", () => {
    render(<UserAvatar user={{ name: "ann", avatar_initial: "甲", avatar_ref: null }} />);
    expect(screen.getByText("甲")).toBeInTheDocument();
  });

  it("avatar_ref 变化后重新尝试加载(失败状态只针对旧 URL)", () => {
    const { container, rerender } = render(
      <UserAvatar user={{ name: "Ann", avatar_ref: "preset:pixel-03" }} />,
    );
    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector("img")).toBeNull();

    // 同一挂载实例内保存了新头像:必须重新渲染 img,而不是沿用旧的失败状态。
    rerender(<UserAvatar user={{ name: "Ann", avatar_ref: "preset:pixel-04" }} />);
    expect(container.querySelector("img")).toHaveAttribute("src", "/avatars/pixel/pixel-04.svg");
  });

  it("畸形引用不产生 img(不把任意字符串拼进 src)", () => {
    const { container } = render(
      <UserAvatar user={{ name: "Ann", avatar_ref: "preset:../../evil" }} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("A")).toBeInTheDocument();
  });
});
