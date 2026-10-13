import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UserPicker } from "./UserPicker";

const options = [
  { id: "u1", name: "Alice" },
  { id: "u2", name: "Bob" },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UserPicker", () => {
  it("Enter picks the active option and never bubbles into the editor submit handler", () => {
    const onPick = vi.fn();
    const onClose = vi.fn();
    render(
      <UserPicker
        anchor={{ left: 10, top: 200, bottom: 220 }}
        options={options}
        query=""
        onPick={onPick}
        onClose={onClose}
      />,
    );
    // 代表 React 根节点上编辑器的 onKeyDown（Enter 提交）。
    const editorKeydown = vi.fn();
    document.addEventListener("keydown", editorKeydown);

    fireEvent.keyDown(document.body, { key: "Enter" });

    expect(onPick).toHaveBeenCalledWith(expect.objectContaining({ id: "u1" }));
    expect(editorKeydown).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    document.removeEventListener("keydown", editorKeydown);
  });

  it("shows the role hint and the email as separate lines", () => {
    render(
      <UserPicker
        anchor={{ left: 10, top: 200, bottom: 220 }}
        options={[{ id: "u1", name: "Alice", email: "alice@example.com", hint: "超级管理员" }]}
        query=""
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("超级管理员")).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
  });

  it("ignores keys during IME composition and lets them reach the input method", () => {
    const onPick = vi.fn();
    render(
      <UserPicker
        anchor={{ left: 10, top: 200, bottom: 220 }}
        options={options}
        query=""
        onPick={onPick}
        onClose={vi.fn()}
      />,
    );
    // 代表编辑器 / 输入法：组合中的按键必须继续传播，不能被浮层吞掉。
    const downstream = vi.fn();
    document.addEventListener("keydown", downstream);

    const composingEnter = fireEvent.keyDown(document.body, {
      key: "Enter",
      isComposing: true,
    });
    const legacyImeKey = fireEvent.keyDown(document.body, {
      key: "Process",
      keyCode: 229,
    });

    expect(onPick).not.toHaveBeenCalled();
    expect(composingEnter).toBe(true); // 未 preventDefault
    expect(legacyImeKey).toBe(true);
    expect(downstream).toHaveBeenCalledTimes(2);

    document.removeEventListener("keydown", downstream);
  });

  it("opens downward when there is enough room below the caret", () => {
    Object.defineProperty(window, "innerHeight", { value: 1000, configurable: true });
    render(
      <UserPicker
        anchor={{ left: 10, top: 200, bottom: 220 }}
        options={options}
        query=""
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("listbox").style.getPropertyValue("--user-picker-top")).toBe("224px");
  });

  it("flips above the caret when the viewport bottom would clip the list", () => {
    Object.defineProperty(window, "innerHeight", { value: 300, configurable: true });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(220);
    render(
      <UserPicker
        anchor={{ left: 10, top: 250, bottom: 270 }}
        options={options}
        query=""
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    // spaceBelow=30 不足以容纳 220px 列表；向上展开：250 - 220 - 4 = 26。
    expect(screen.getByRole("listbox").style.getPropertyValue("--user-picker-top")).toBe("26px");
  });
});
