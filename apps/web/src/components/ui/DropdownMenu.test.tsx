import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useState } from "react";
import { DropdownMenu, type DropdownItem } from "./DropdownMenu";

function Wrapper({
  items,
  onSelectA,
  onSelectB,
}: {
  items?: DropdownItem[];
  onSelectA?: () => void;
  onSelectB?: () => void;
}) {
  const defaultItems: DropdownItem[] = [
    { id: "a", label: "Option A", onSelect: onSelectA },
    { id: "b", label: "Option B", onSelect: onSelectB },
  ];
  return (
    <DropdownMenu
      items={items ?? defaultItems}
      trigger={({ open, toggle, ref }) => (
        <button ref={ref} onClick={toggle} aria-expanded={open}>
          打开
        </button>
      )}
    />
  );
}

describe("<DropdownMenu />", () => {
  it("初始关闭，点击 trigger 后打开", () => {
    render(<Wrapper />);
    expect(screen.queryByText("Option A")).toBeNull();
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByText("Option A")).toBeInTheDocument();
    expect(screen.getByText("打开")).toHaveAttribute("aria-expanded", "true");
  });

  it("通过 portal 渲染菜单面板，避免被滚动容器裁切", () => {
    render(<Wrapper />);
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menu").parentElement).toBe(document.body);
  });

  it("点击 item 触发 onSelect 并关闭菜单", () => {
    const onSelectA = vi.fn();
    render(<Wrapper onSelectA={onSelectA} />);
    fireEvent.click(screen.getByText("打开"));
    fireEvent.click(screen.getByText("Option A"));
    expect(onSelectA).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Option A")).toBeNull();
  });

  it("打开后焦点移入首个可选中项，ArrowDown 移到下一项", () => {
    render(<Wrapper />);
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menuitem", { name: "Option A" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Option B" })).toHaveFocus();
  });

  it("Escape 关闭后焦点回到 trigger", () => {
    render(<Wrapper />);
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menuitem", { name: "Option A" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("Option A")).toBeNull();
    expect(screen.getByText("打开")).toHaveFocus();
  });

  it("Tab 关闭菜单并归还触发器焦点", () => {
    render(<Wrapper />);
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menuitem", { name: "Option A" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Tab" });
    expect(screen.queryByText("Option A")).toBeNull();
    expect(screen.getByText("打开")).toHaveFocus();
  });

  it("悬停 disabled 项不改变焦点", () => {
    const onSelectDisabled = vi.fn();
    render(
      <Wrapper
        items={[
          { id: "a", label: "Option A" },
          { id: "b", label: "Disabled", disabled: true, onSelect: onSelectDisabled },
        ]}
      />,
    );
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menuitem", { name: "Option A" })).toHaveFocus();
    fireEvent.mouseEnter(screen.getByRole("menuitem", { name: "Disabled" }));
    expect(screen.getByRole("menuitem", { name: "Option A" })).toHaveFocus();
  });

  it("items 插入新项后 Enter 仍触发原聚焦项", () => {
    const onSelectUnload = vi.fn();
    const onSelectDelete = vi.fn();
    function DynamicWrapper() {
      const [withDrift, setWithDrift] = useState(false);
      const items: DropdownItem[] = [
        { id: "unload", label: "Unload", onSelect: onSelectUnload },
        ...(withDrift ? [{ id: "drift", label: "Drift" }] : []),
        { id: "delete", label: "Delete", onSelect: onSelectDelete },
      ];
      return (
        <>
          <button onClick={() => setWithDrift(true)}>刷新拓扑</button>
          <DropdownMenu
            items={items}
            trigger={({ open, toggle, ref }) => (
              <button ref={ref} onClick={toggle} aria-expanded={open}>
                打开
              </button>
            )}
          />
        </>
      );
    }
    render(<DynamicWrapper />);
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByRole("menuitem", { name: "Unload" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    fireEvent.click(screen.getByText("刷新拓扑"));
    expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Enter" });
    expect(onSelectDelete).toHaveBeenCalledTimes(1);
    expect(onSelectUnload).not.toHaveBeenCalled();
  });

  it("disabled item 点击不触发 onSelect", () => {
    const onSelect = vi.fn();
    render(<Wrapper items={[{ id: "x", label: "Disabled", disabled: true, onSelect }]} />);
    fireEvent.click(screen.getByText("打开"));
    fireEvent.click(screen.getByText("Disabled"));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("divider 项不渲染交互", () => {
    render(
      <Wrapper
        items={[
          { id: "1", label: "One", onSelect: () => {} },
          { id: "div", divider: true, label: "" },
          { id: "2", label: "Two", onSelect: () => {} },
        ]}
      />,
    );
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByText("One")).toBeInTheDocument();
    expect(screen.getByText("Two")).toBeInTheDocument();
  });

  it("content 模式：渲染自定义内容并支持 close 主动关闭", () => {
    function ContentWrapper() {
      return (
        <DropdownMenu
          trigger={({ toggle, ref }) => (
            <button ref={ref} onClick={toggle}>
              打开
            </button>
          )}
          content={({ close }) => (
            <div>
              <span>自定义表单</span>
              <button onClick={close}>关闭</button>
            </div>
          )}
        />
      );
    }
    render(<ContentWrapper />);
    expect(screen.queryByText("自定义表单")).toBeNull();
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByText("自定义表单")).toBeInTheDocument();
    fireEvent.click(screen.getByText("关闭"));
    expect(screen.queryByText("自定义表单")).toBeNull();
  });

  it("content 模式：Escape 关闭", () => {
    render(
      <DropdownMenu
        trigger={({ toggle, ref }) => (
          <button ref={ref} onClick={toggle}>
            打开
          </button>
        )}
        content={() => <span>自定义表单</span>}
      />,
    );
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByText("自定义表单")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("自定义表单")).toBeNull();
  });

  it("content 模式不覆盖内容的 autoFocus", () => {
    render(
      <DropdownMenu
        trigger={({ toggle, ref }) => (
          <button ref={ref} onClick={toggle}>
            打开
          </button>
        )}
        content={() => <input autoFocus placeholder="预设名" />}
      />,
    );
    fireEvent.click(screen.getByText("打开"));
    expect(screen.getByPlaceholderText("预设名")).toHaveFocus();
  });
});
