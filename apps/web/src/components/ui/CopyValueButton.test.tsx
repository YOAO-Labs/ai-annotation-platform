import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { copyToClipboard } from "@/lib/clipboard";
import { CopyValueButton } from "./CopyValueButton";
import { useToastStore } from "./Toast";

vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn() }));
const originalPush = useToastStore.getState().push;

afterEach(() => {
  act(() => useToastStore.setState({ push: originalPush }));
  vi.resetAllMocks();
});

describe("CopyValueButton", () => {
  it("copies the full hash without displaying it and waits for success", async () => {
    const push = vi.fn();
    useToastStore.setState({ push });
    let complete!: (copied: boolean) => void;
    vi.mocked(copyToClipboard).mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const value = `sha256:${"a".repeat(64)}`;
    render(<CopyValueButton value={value} label="复制权重哈希" />);
    expect(screen.queryByText(value)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制权重哈希" }));
    expect(copyToClipboard).toHaveBeenCalledWith(value);
    expect(push).not.toHaveBeenCalled();
    complete(true);
    await waitFor(() => expect(push).toHaveBeenCalledWith({ msg: "已复制哈希", kind: "success" }));
  });

  it("shows failure when copying is rejected", async () => {
    const push = vi.fn();
    useToastStore.setState({ push });
    vi.mocked(copyToClipboard).mockResolvedValue(false);
    render(<CopyValueButton value="hash" />);
    fireEvent.click(screen.getByRole("button", { name: "复制哈希" }));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith({
        msg: "复制失败，请检查浏览器剪贴板权限",
        kind: "error",
      }),
    );
  });

  it("supports focus tooltip and keyboard copying without submitting or activating the parent", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const onClick = vi.fn();
    const onPointerDown = vi.fn();
    const onKeyDown = vi.fn();
    const push = vi.fn();
    useToastStore.setState({ push });
    vi.mocked(copyToClipboard).mockResolvedValue(true);
    const value = "ea4b2a65-a050-42f3-a652-68d1c67368df";
    render(
      <form
        onSubmit={onSubmit}
        onClick={onClick}
        onPointerDown={onPointerDown}
        onKeyDown={onKeyDown}
      >
        <CopyValueButton value={value} kind="id" label="复制实例 ID" />
      </form>,
    );
    await user.tab();
    expect(screen.getByRole("button", { name: "复制实例 ID" })).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("复制 ID");
    onKeyDown.mockClear();
    await user.keyboard("{Enter} ");
    await user.click(screen.getByRole("button", { name: "复制实例 ID" }));
    expect(copyToClipboard).toHaveBeenCalledWith(value);
    expect(copyToClipboard).toHaveBeenCalledTimes(3);
    expect(push).toHaveBeenCalledWith({ msg: "已复制 ID", kind: "success" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onClick).not.toHaveBeenCalled();
    expect(onPointerDown).not.toHaveBeenCalled();
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it("shows the generic hash tooltip on hover", async () => {
    const user = userEvent.setup();
    render(<CopyValueButton value="hash" label="复制配置哈希" />);
    await user.hover(screen.getByRole("button", { name: "复制配置哈希" }));
    expect(await screen.findByRole("tooltip")).toHaveTextContent("复制哈希");
  });

  it.each([undefined, null, ""])("renders an empty sentinel without an action for %s", (value) => {
    render(<CopyValueButton value={value} />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
