import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContextMenu } from "./ContextMenu";
import type { DropdownItem } from "./DropdownMenu";

function renderMenu(items: DropdownItem[], props?: Partial<ComponentProps<typeof ContextMenu>>) {
  const onClose = props?.onClose ?? vi.fn();
  render(
    <ContextMenu open x={props?.x ?? 20} y={props?.y ?? 30} items={items} onClose={onClose} />,
  );
  return { onClose };
}

describe("<ContextMenu />", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders at fixed coordinates and closes after selecting an enabled item", () => {
    const onSelect = vi.fn();
    const { onClose } = renderMenu([{ id: "mark", label: "Mark", onSelect }]);

    const menu = screen.getByRole("menu");
    expect(menu).toHaveStyle({ "--context-menu-x": "20px", "--context-menu-y": "30px" });

    fireEvent.click(screen.getByRole("menuitem", { name: "Mark" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not select disabled items", () => {
    const onSelect = vi.fn();
    const { onClose } = renderMenu([
      { id: "disabled", label: "Disabled", disabled: true, onSelect },
    ]);

    fireEvent.click(screen.getByRole("menuitem", { name: "Disabled" }));
    expect(onSelect).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes on Escape and outside mousedown", () => {
    const { onClose } = renderMenu([{ id: "mark", label: "Mark" }]);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("flips when the menu would overflow the viewport", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      width: 180,
      height: 120,
      right: 180,
      bottom: 120,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 200 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 140 });

    renderMenu([{ id: "mark", label: "Mark" }], { x: 190, y: 130 });

    await waitFor(() => {
      expect(screen.getByRole("menu").className).toContain("-translate-x-full");
      expect(screen.getByRole("menu").className).toContain("-translate-y-full");
    });
  });
});
