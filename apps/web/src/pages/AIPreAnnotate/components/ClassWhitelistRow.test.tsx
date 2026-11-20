/**
 * ClassWhitelistRow: 静态类别表 chip 勾选 + 文本输入按类名快速勾选 (index 制)。
 */
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ClassWhitelistRow } from "./ClassWhitelistRow";

const CLASSES = [
  { index: 0, name: "person" },
  { index: 2, name: "car" },
  { index: 5, name: "bus" },
];

describe("ClassWhitelistRow 文本输入", () => {
  it("classes 就绪 → 渲染 chip + 文本输入框 (无预热 CTA)", () => {
    render(<ClassWhitelistRow classes={CLASSES} selected={new Set()} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: /person/ })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("输入类名快速勾选，如 person")).toBeInTheDocument();
  });

  it("输入类名 + 回车 → 按 index 勾选 (大小写不敏感)", () => {
    const onChange = vi.fn();
    render(<ClassWhitelistRow classes={CLASSES} selected={new Set()} onChange={onChange} />);
    const input = screen.getByPlaceholderText("输入类名快速勾选，如 person");
    fireEvent.change(input, { target: { value: "CAR" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith(new Set([2])); // car → index 2
  });

  it("输入未知类名 → 不勾选 (闭集只能选模型认识的类)", () => {
    const onChange = vi.fn();
    render(<ClassWhitelistRow classes={CLASSES} selected={new Set()} onChange={onChange} />);
    const input = screen.getByPlaceholderText("输入类名快速勾选，如 person");
    fireEvent.change(input, { target: { value: "dragon" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("输入子串 → 下拉列出匹配项, 点击勾选", () => {
    const onChange = vi.fn();
    render(<ClassWhitelistRow classes={CLASSES} selected={new Set()} onChange={onChange} />);
    const input = screen.getByPlaceholderText("输入类名快速勾选，如 person");
    fireEvent.change(input, { target: { value: "ar" } }); // 子串命中 car
    fireEvent.click(screen.getByTitle("勾选 [2] car"));
    expect(onChange).toHaveBeenCalledWith(new Set([2]));
  });

  it("classes 未就位 → 提示预热, 无文本输入", () => {
    render(
      <ClassWhitelistRow
        classes={undefined}
        selected={new Set()}
        onChange={() => {}}
        onWarm={() => {}}
      />,
    );
    expect(screen.getByText(/先预热加载类别表/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/输入类名/)).toBeNull();
  });
});
