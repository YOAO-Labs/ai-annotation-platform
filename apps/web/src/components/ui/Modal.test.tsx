import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Modal } from "./Modal";
import { useRef, useState } from "react";
import userEvent from "@testing-library/user-event";

describe("<Modal />", () => {
  it("focuses a requested field and restores the controlled trigger after Escape", async () => {
    function Form() {
      const [open, setOpen] = useState(false);
      const field = useRef<HTMLInputElement>(null);
      const trigger = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button ref={trigger} onClick={() => setOpen(true)}>
            新版本
          </button>
          <Modal
            open={open}
            onClose={() => setOpen(false)}
            title="创建版本"
            initialFocusRef={field}
            returnFocusRef={trigger}
          >
            <input aria-label="版本名称" ref={field} />
          </Modal>
        </>
      );
    }
    const user = userEvent.setup();
    render(<Form />);
    await user.click(screen.getByRole("button", { name: "新版本" }));
    expect(screen.getByLabelText("版本名称")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新版本" })).toHaveFocus();
  });
  it("open=false 不渲染", () => {
    render(
      <Modal open={false} onClose={() => {}}>
        <p>hidden</p>
      </Modal>,
    );
    expect(screen.queryByText("hidden")).toBeNull();
  });

  it("open=true 渲染 children + title", () => {
    render(
      <Modal open onClose={() => {}} title="测试 modal">
        <p>visible</p>
      </Modal>,
    );
    expect(screen.getByText("visible")).toBeInTheDocument();
    expect(screen.getByText("测试 modal")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("data-state", "open");
    expect(dialog).toHaveAttribute("aria-labelledby");
  });

  it.each([true, false])("backdropBlur=%s 时 Escape 触发 onClose", (backdropBlur) => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} backdropBlur={backdropBlur}>
        <p>x</p>
      </Modal>,
    );
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("默认保留背景模糊，关闭模糊后仍保留模态遮罩", () => {
    const { rerender } = render(
      <Modal open onClose={() => {}}>
        <p>canvas dialog</p>
      </Modal>,
    );
    expect(screen.getByTestId("modal-overlay")).toHaveClass("backdrop-blur-overlay");
    rerender(
      <Modal open onClose={() => {}} backdropBlur={false}>
        <p>canvas dialog</p>
      </Modal>,
    );
    expect(screen.getByTestId("modal-overlay")).not.toHaveClass("backdrop-blur-overlay");
    expect(screen.getByTestId("modal-overlay")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("modal-overlay")).toHaveClass("bg-black/40");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("点击内容不触发 onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <button>inside</button>
      </Modal>,
    );
    fireEvent.pointerDown(screen.getByText("inside"));
    fireEvent.click(screen.getByText("inside"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it.each([true, false])("backdropBlur=%s 时点击 overlay 触发 onClose", async (backdropBlur) => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} backdropBlur={backdropBlur}>
        <p>x</p>
      </Modal>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.pointerDown(screen.getByTestId("modal-overlay"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("点击右上角关闭按钮触发 onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="t">
        <p>x</p>
      </Modal>,
    );
    fireEvent.click(screen.getByLabelText("关闭"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not treat another dialog's action as a backdrop click", async () => {
    const onClose = vi.fn();
    render(
      <>
        <Modal open onClose={onClose} title="任务详情">
          <p>Keep the detail available after cancellation.</p>
        </Modal>
        <div role="alertdialog" aria-label="离开确认">
          <button>继续绘制</button>
        </div>
      </>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    const cancel = screen.getByRole("button", { name: "继续绘制", hidden: true });
    fireEvent.pointerDown(cancel);
    fireEvent.click(cancel);
    expect(onClose).not.toHaveBeenCalled();
  });
});
