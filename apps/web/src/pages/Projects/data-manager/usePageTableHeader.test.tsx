import { act, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { pageTableHeaderOffset, usePageTableHeader } from "./usePageTableHeader";

describe("pageTableHeaderOffset", () => {
  it.each([
    [48, 400, 4000, 40, 0],
    [48, -200, 4000, 40, 248],
    [48, -5000, 4000, 40, 3960],
    [48, -200, 20, 40, 0],
  ])(
    "bounds the header within its table (%s, %s, %s, %s)",
    (page, table, height, header, expected) => {
      expect(pageTableHeaderOffset(page, table, height, header)).toBe(expected);
    },
  );
});

describe("usePageTableHeader", () => {
  afterEach(() => vi.restoreAllMocks());
  it("measures a late-mounted header, follows page scrolling and cancels queued work", () => {
    let top = 100;
    let scheduled: FrameRequestCallback | null = null;
    const cancel = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      scheduled = callback;
      return 7;
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      return { top: this.dataset.testid === "page" ? 48 : top } as DOMRect;
    });
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(500);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(40);
    function View({ ready }: { ready: boolean }) {
      const page = useRef<HTMLDivElement>(null);
      const table = useRef<HTMLDivElement>(null);
      const header = usePageTableHeader(page, table);
      return (
        <div ref={page} data-testid="page">
          {ready && (
            <div ref={table}>
              <div ref={header} data-testid="header" />
            </div>
          )}
        </div>
      );
    }
    const view = render(<View ready={false} />);
    view.rerender(<View ready />);
    const header = screen.getByTestId("header");
    expect(header.style.transform).toBe("translateY(0px)");
    top = -200;
    fireEvent.scroll(screen.getByTestId("page"));
    act(() => scheduled?.(0));
    expect(header.style.transform).toBe("translateY(248px)");
    fireEvent.scroll(screen.getByTestId("page"));
    view.unmount();
    expect(cancel).toHaveBeenCalledWith(7);
    expect(header.style.transform).toBe("");
  });
});
