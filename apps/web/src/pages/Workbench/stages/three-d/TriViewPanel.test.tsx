import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { TriViewPanel } from "./TriViewPanel";
import type { PointCloudScene } from "./PointCloudScene";

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

describe("TriViewPanel layer coverage", () => {
  it("三行连续填满面板，不给共享主画布留下透明 padding 或 gap", () => {
    render(
      <TriViewPanel
        scene={null}
        selected={null}
        editable={false}
        layoutKey="test"
        zoomByView={{ top: 1, side: 1, front: 1 }}
        onZoomChange={vi.fn()}
        onEditPsr={vi.fn()}
      />,
    );

    const panel = screen.getByTestId("tri-view-renderer-panel");
    expect(panel.className).toContain("gap-0");
    expect(panel.className).toContain("p-0");
    expect(panel.className).not.toContain("gap-1.5");
    expect(panel.className).not.toContain("p-1.5");
  });

  it("切换可见性时保留同一布局，直到面板卸载才注销", () => {
    const scene = {
      setTriViewLayout: vi.fn(),
      setTriViewActive: vi.fn(),
      setTriViewBox: vi.fn(),
      setTriViewZoomByView: vi.fn(),
      setTriViewCameraRef: vi.fn(),
    } as unknown as PointCloudScene;
    const props = {
      scene,
      selected: null,
      editable: false,
      layoutKey: "test",
      zoomByView: { top: 1, side: 1, front: 1 },
      onZoomChange: vi.fn(),
      onEditPsr: vi.fn(),
    };
    const { rerender, unmount } = render(<TriViewPanel {...props} active={false} />);

    rerender(<TriViewPanel {...props} active />);
    rerender(<TriViewPanel {...props} active={false} />);

    expect(scene.setTriViewActive).toHaveBeenCalledTimes(3);
    expect(scene.setTriViewActive).toHaveBeenNthCalledWith(1, false);
    expect(scene.setTriViewActive).toHaveBeenNthCalledWith(2, true);
    expect(scene.setTriViewActive).toHaveBeenNthCalledWith(3, false);
    expect(scene.setTriViewLayout).not.toHaveBeenCalledWith(null);

    unmount();
    expect(scene.setTriViewLayout).toHaveBeenCalledWith(null);
  });
});
