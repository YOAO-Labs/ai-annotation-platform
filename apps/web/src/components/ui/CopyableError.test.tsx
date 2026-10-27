import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { copyToClipboard } from "@/lib/clipboard";
import { CopyableError } from "./CopyableError";
import { useToastStore } from "./Toast";

vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn() }));
const originalPush = useToastStore.getState().push;

afterEach(() => {
  act(() => useToastStore.setState({ push: originalPush }));
  vi.resetAllMocks();
});

describe("CopyableError", () => {
  it("copies the original multiline log even when only a short preview is shown", async () => {
    const push = vi.fn();
    useToastStore.setState({ push });
    let complete!: (copied: boolean) => void;
    vi.mocked(copyToClipboard).mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const message = `  RuntimeError\n${"diagnostic ".repeat(100)}\n最后一行  `;
    render(<CopyableError message={message} preview="任务失败…" truncate />);
    const button = screen.getByRole("button", { name: "复制完整错误信息" });
    expect(button).toHaveTextContent("任务失败…");
    expect(button).toHaveAttribute("title", `${message}\n\n点击复制完整错误信息`);
    fireEvent.click(button);
    expect(copyToClipboard).toHaveBeenCalledWith(message);
    expect(push).not.toHaveBeenCalled();
    complete(true);
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith({ msg: "已复制完整错误信息", kind: "success" }),
    );
  });

  it("reports copy failure without a success notification", async () => {
    const push = vi.fn();
    useToastStore.setState({ push });
    vi.mocked(copyToClipboard).mockResolvedValue(false);
    render(<CopyableError message="failed" />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith({ msg: "复制失败，请检查浏览器剪贴板权限", kind: "error" }),
    );
    expect(push).toHaveBeenCalledTimes(1);
  });

  it("supports keyboard and pointer copying without triggering the enclosing row or form", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const onClick = vi.fn();
    const onPointerDown = vi.fn();
    const onKeyDown = vi.fn();
    useToastStore.setState({ push: vi.fn() });
    vi.mocked(copyToClipboard).mockResolvedValue(true);
    render(
      <form onSubmit={onSubmit}>
        <div onClick={onClick} onPointerDown={onPointerDown} onKeyDown={onKeyDown}>
          <CopyableError message="full diagnostic" />
        </div>
      </form>,
    );
    await user.tab();
    await user.keyboard("{Enter} ");
    await user.click(screen.getByRole("button"));
    expect(copyToClipboard).toHaveBeenCalledTimes(3);
    expect(copyToClipboard).toHaveBeenLastCalledWith("full diagnostic");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onClick).not.toHaveBeenCalled();
    expect(onPointerDown).not.toHaveBeenCalled();
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it.each(["", "  \n "])("does not offer an empty copy action for %j", (message) => {
    render(<CopyableError message={message} />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
