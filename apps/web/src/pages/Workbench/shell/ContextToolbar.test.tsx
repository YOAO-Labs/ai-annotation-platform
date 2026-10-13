import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ContextToolbar } from "./ContextToolbar";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";

// A second, non-Mask consumer verifies the shell has no editing or persistence assumptions.
describe("ContextToolbar", () => {
  it("reveals required input on hover and guards its keys without opening full settings", async () => {
    const user = userEvent.setup();
    const command = vi.fn();
    function Owner() {
      const [text, setText] = useState("");
      const input = (
        <input aria-label="目标" value={text} onChange={(event) => setText(event.target.value)} />
      );
      return (
        <ContextToolbar
          id="primary"
          label="示例"
          summary="工具"
          summaryLabel="示例常用工具"
          quickActions={[]}
          primaryContent={
            <>
              {input}
              <button onClick={command}>运行</button>
            </>
          }
        >
          {(close) => (
            <>
              {input}
              <button onClick={close}>收起</button>
            </>
          )}
        </ContextToolbar>
      );
    }
    render(<Owner />);
    const blocked = vi.fn();
    const keyListener = (event: KeyboardEvent) => blocked(isWorkbenchInteractionBlocked(event));
    window.addEventListener("keydown", keyListener);
    try {
      expect(screen.queryByRole("textbox", { name: "目标" })).toBeNull();
      await user.hover(screen.getByTestId("primary-settings-trigger"));
      await user.type(screen.getByRole("textbox", { name: "目标" }), "car");
      expect(blocked.mock.calls.every(([value]) => value)).toBe(true);
      expect(screen.queryByTestId("primary-toolbar")).toBeNull();
      await user.click(screen.getByRole("button", { name: "更多 示例 工具" }));
      expect(screen.getByRole("textbox", { name: "目标" })).toHaveValue("car");
      await user.type(screen.getByRole("textbox", { name: "目标" }), " . person");
      fireEvent.keyDown(screen.getByRole("textbox", { name: "目标" }), {
        key: "Escape",
        isComposing: true,
      });
      expect(screen.getByTestId("primary-toolbar")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "收起" }));
      await user.hover(screen.getByTestId("primary-settings-trigger"));
      expect(screen.getByRole("textbox", { name: "目标" })).toHaveValue("car . person");
      expect(command).not.toHaveBeenCalled();
      await user.click(screen.getByRole("button", { name: "运行" }));
      expect(command).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("keydown", keyListener);
    }
  });

  it("uses the fixed capsule dimensions when closing with primary controls", async () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.dataset.testid === "closing-tool-capsule") return new DOMRect(10, 20, 100, 40);
        if (this.hasAttribute("data-radix-popper-content-wrapper"))
          return new DOMRect(300, 20, 600, 120);
        return new DOMRect();
      });
    try {
      const user = userEvent.setup();
      render(
        <ContextToolbar
          id="closing"
          label="示例"
          summary="工具"
          summaryLabel="示例常用工具"
          quickActions={[]}
          primaryContent={<input aria-label="目标" />}
        >
          {(close) => <button onClick={close}>收起</button>}
        </ContextToolbar>,
      );
      await user.click(screen.getByRole("button", { name: "示例常用工具" }));
      await user.click(screen.getByRole("button", { name: "更多 示例 工具" }));
      const panel = screen.getByTestId("closing-toolbar");
      await waitFor(() => expect(panel).toHaveAttribute("data-motion-ready", "true"));
      // Preserve Radix's exit presence in jsdom, whose CSS animations are disabled.
      panel.style.animationName = "toolbarCollapse";
      await user.click(screen.getByRole("button", { name: "收起" }));
      expect(panel.style.getPropertyValue("--toolbar-target")).toBe(
        "translate(-290px, 0px) scale(0.16666666666666666, 0.3333333333333333)",
      );
    } finally {
      rect.mockRestore();
    }
  });
  it("keeps focused quick actions available when the pointer leaves and ignores drawing drags", async () => {
    const user = userEvent.setup();
    render(
      <ContextToolbar
        id="pointer"
        label="示例"
        summary="工具"
        summaryLabel="示例工具"
        quickActions={[]}
      >
        {() => <p>设置</p>}
      </ContextToolbar>,
    );
    const capsule = screen.getByTestId("pointer-tool-capsule");
    fireEvent.pointerEnter(capsule, { buttons: 1 });
    expect(capsule).toHaveAttribute("data-expanded", "false");
    await user.tab();
    await user.tab();
    const more = screen.getByRole("button", { name: "更多 示例 工具" });
    expect(more).toHaveFocus();
    fireEvent.pointerLeave(capsule);
    expect(more).toBeVisible();
    expect(more).toHaveFocus();
  });

  it("focuses the full panel and lets Escape reach the workbench after collapsing", async () => {
    const user = userEvent.setup();
    const escape = vi.fn();
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.key === "Escape" &&
        !event.defaultPrevented &&
        !isWorkbenchInteractionBlocked(event)
      ) {
        escape();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    try {
      render(
        <ContextToolbar
          id="keyboard"
          label="示例"
          summary="◇"
          summaryLabel="示例常用工具"
          quickActions={[]}
        >
          {() => <button>面板操作</button>}
        </ContextToolbar>,
      );
      const summary = screen.getByRole("button", { name: "示例常用工具" });
      await user.tab();
      expect(summary).toHaveFocus();
      await user.keyboard("{Escape}");
      expect(summary).toHaveAttribute("aria-expanded", "false");
      expect(escape).not.toHaveBeenCalled();
      await user.keyboard("{Escape}");
      expect(escape).toHaveBeenCalledTimes(1);

      await user.keyboard("{Enter}{Tab}{Enter}");
      await waitFor(() => expect(screen.getByTestId("keyboard-toolbar")).toHaveFocus());
      await user.tab();
      expect(screen.getByRole("button", { name: "面板操作" })).toHaveFocus();
      await user.keyboard("{Escape}");
      expect(summary).toHaveFocus();
      expect(summary).toHaveAttribute("aria-expanded", "false");
      expect(escape).toHaveBeenCalledTimes(1);
      await user.keyboard("{Escape}");
      expect(escape).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener("keydown", onKeyDown);
    }
  });

  it("derives the opening transform from the actual capsule and panel rectangles", async () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.dataset.testid === "geometry-tool-capsule") return new DOMRect(10, 20, 100, 40);
        if (this.hasAttribute("data-radix-popper-content-wrapper"))
          return new DOMRect(300, 20, 600, 120);
        return new DOMRect();
      });
    try {
      const user = userEvent.setup();
      render(
        <ContextToolbar
          id="geometry"
          label="几何"
          summary="◇"
          summaryLabel="几何工具"
          quickActions={[]}
        >
          {() => <p>完整工具栏</p>}
        </ContextToolbar>,
      );
      await user.click(screen.getByRole("button", { name: "几何工具" }));
      await user.click(screen.getByRole("button", { name: "更多 几何 工具" }));
      const panel = screen.getByTestId("geometry-toolbar");
      await waitFor(() => expect(panel).toHaveAttribute("data-motion-ready", "true"));
      expect(panel.style.getPropertyValue("--toolbar-from")).toBe(
        "translate(-290px, 0px) scale(0.16666666666666666, 0.3333333333333333)",
      );
    } finally {
      rect.mockRestore();
    }
  });

  it("dispatches supplied actions and preserves owner content across close/reopen", async () => {
    const user = userEvent.setup();
    const choose = vi.fn();
    render(
      <ContextToolbar
        id="sample"
        label="示例"
        summary="◇"
        summaryLabel="示例常用工具"
        quickActions={[{ id: "pick", label: "点选", icon: "·", onSelect: choose }]}
      >
        {(close) => (
          <>
            <p>工具提供的内容</p>
            <button onClick={close}>收起</button>
          </>
        )}
      </ContextToolbar>,
    );
    const summary = screen.getByRole("button", { name: "示例常用工具" });
    await user.click(summary);
    await user.click(screen.getByRole("button", { name: "点选" }));
    expect(choose).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "更多 示例 工具" }));
    expect(screen.getByText("工具提供的内容")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "收起" }));
    expect(summary).toHaveFocus();
    expect(summary).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(summary);
    await user.click(screen.getByRole("button", { name: "更多 示例 工具" }));
    expect(screen.getByText("工具提供的内容")).toBeVisible();
  });

  it("keeps the expanded panel when a pointerdown lands inside a stacked modal dialog", async () => {
    const user = userEvent.setup();
    function Owner() {
      return (
        <>
          <ContextToolbar
            id="guarded"
            label="示例"
            summary="◇"
            summaryLabel="示例常用工具"
            quickActions={[]}
          >
            {() => <p>工具提供的内容</p>}
          </ContextToolbar>
          {/* Stacked decision dialogs mark themselves with data-modal; Radix can
              reach the outside check after such a dialog already closed, so the
              toolbar must ignore interactions owned by it. */}
          <div role="alertdialog" data-modal>
            <button type="button">覆盖</button>
          </div>
        </>
      );
    }
    render(<Owner />);
    await user.click(screen.getByRole("button", { name: "示例常用工具" }));
    await user.click(screen.getByRole("button", { name: "更多 示例 工具" }));
    expect(screen.getByText("工具提供的内容")).toBeVisible();
    fireEvent.pointerDown(screen.getByRole("button", { name: "覆盖" }));
    await waitFor(() => expect(screen.queryByTestId("guarded-toolbar")).not.toBeNull());
    // Genuine outside interactions still collapse the panel.
    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(screen.queryByTestId("guarded-toolbar")).toBeNull());
  });
});
