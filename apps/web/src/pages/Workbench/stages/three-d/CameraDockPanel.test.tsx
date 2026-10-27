vi.mock("./useCameraOriginalImage", () => ({
  useCameraOriginalImage: (source: string) => ({ url: source }),
}));
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CameraDockPanel, type CameraDockPanelProps } from "./CameraDockPanel";
import { FloatingCameraPanel } from "./FloatingCameraPanel";

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => vi.unstubAllGlobals());

function galleryProps(): CameraDockPanelProps {
  return {
    cameras: [
      { role: "CAM_BACK", name: "后置", image_url: "/rear-0.jpg" },
      { role: "CAM_FRONT", name: "前置", image_url: "/front-0.jpg" },
    ],
    boxes: [],
    highlightedIds: new Set(),
    onSelectBox: vi.fn(),
    onEnlarge: vi.fn(),
  };
}

describe("CameraDockPanel", () => {
  it("orders cameras by direction and opens the original per-role editor", () => {
    const props = galleryProps();
    const view = render(<CameraDockPanel {...props} bestRole="CAM_FRONT" />);
    expect(screen.getAllByRole("img").map((image) => image.getAttribute("alt"))).toEqual([
      "前置",
      "后置",
    ]);
    expect(screen.getByText("前方")).toBeInTheDocument();
    expect(screen.getByText("前置 · 正对 · 无标定")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "放大前置" }));
    expect(props.onEnlarge).toHaveBeenCalledWith("CAM_FRONT");

    const scroll = screen.getByRole("region", { name: "前置" }).parentElement!.parentElement!;
    scroll.scrollTop = 200;
    view.rerender(<CameraDockPanel {...props} resetKey={1} />);
    expect(scroll.scrollTop).toBe(0);
  });

  it("never presents the preceding frame during loading, errors, or missing cameras", () => {
    const props = galleryProps();
    const view = render(<CameraDockPanel {...props} />);
    expect(screen.getAllByRole("img")).toHaveLength(2);

    view.rerender(<CameraDockPanel {...props} loading />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("加载当前帧相机");
    view.rerender(<CameraDockPanel {...props} error="当前帧读取失败" />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("当前帧读取失败");
    view.rerender(<CameraDockPanel {...props} cameras={[]} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("当前帧没有相机图像");

    view.rerender(
      <CameraDockPanel
        {...props}
        cameras={[{ role: "CAM_FRONT", name: "前置", image_url: "/front-1.jpg" }]}
      />,
    );
    expect(screen.getByRole("img")).toHaveAttribute("src", "/front-1.jpg");
    expect(screen.queryByRole("img", { name: "后置" })).toBeNull();
    expect(screen.queryByRole("button", { name: "悬浮显示" })).toBeNull();
  });
});

describe("floating camera mode switch", () => {
  it.each([false, true])(
    "offers whole-group docking while collapsed=%s without changing geometry",
    (collapsed) => {
      const onDockAll = vi.fn();
      const onPositionChange = vi.fn();
      const onCollapsedChange = vi.fn();
      render(
        <FloatingCameraPanel
          role="CAM_FRONT"
          name="前置"
          imageUrl="/front.jpg"
          boxes={[]}
          highlightedIds={new Set()}
          onSelectBox={vi.fn()}
          position={{ x: 120, y: 200 }}
          collapsed={collapsed}
          onPositionChange={onPositionChange}
          onCollapsedChange={onCollapsedChange}
          onDockAll={onDockAll}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: "前置布局菜单" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "全部相机停靠" }));
      expect(onDockAll).toHaveBeenCalledTimes(1);
      expect(onPositionChange).not.toHaveBeenCalled();
      expect(onCollapsedChange).not.toHaveBeenCalled();
    },
  );
});
