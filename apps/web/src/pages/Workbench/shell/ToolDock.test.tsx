import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolDock } from "./ToolDock";
import { dispatchKey, type DispatchCtx } from "../state/hotkeys";

const resizeCallbacks = new Set<() => void>();

it.each([false, true])(
  "does not advertise disabled tool bindings (video=%s)",
  async (videoMode) => {
    const user = userEvent.setup();
    render(
      <ToolDock
        tool="box"
        onSetTool={vi.fn()}
        videoMode={videoMode}
        toolHotkeys={{ [`${videoMode ? "video" : "image"}.tool.box`]: null }}
      />,
    );
    const box = screen.getByTestId(videoMode ? "video-tool-btn-box" : "tool-btn-box");
    expect(box).toBeVisible();
    expect(box).not.toHaveTextContent("B");
    await user.hover(box);
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("矩形");
    expect(tooltip).not.toHaveTextContent("Alt+1");
  },
);
beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(private callback: () => void) {}
      observe(target: HTMLElement) {
        if (target.dataset.testid === "tool-dock") resizeCallbacks.add(this.callback);
      }
      unobserve() {}
      disconnect() {
        resizeCallbacks.delete(this.callback);
      }
    },
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resizeCallbacks.clear();
});

function measureDock(initialHeight: number) {
  let height = initialHeight;
  const original = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.dataset.testid === "tool-dock") return { ...original.call(this), width: 47, height };
    const measured = this.dataset.dockMeasure;
    if (measured)
      return {
        ...original.call(this),
        width: 38,
        height: measured === "button" ? 38 : measured === "divider" ? 13 : 12,
      };
    return original.call(this);
  });
  const computedStyle = window.getComputedStyle;
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
    const style = computedStyle(element);
    return (element as HTMLElement).dataset.testid === "tool-dock"
      ? new Proxy(style, {
          get(target, key) {
            if (key === "rowGap") return "6px";
            if (key === "paddingTop" || key === "paddingBottom") return "10px";
            return Reflect.get(target, key, target);
          },
        })
      : style;
  });
  return (next: number) =>
    act(() => {
      height = next;
      [...resizeCallbacks].forEach((callback) => callback());
    });
}

describe("ToolDock · 高度溢出", () => {
  it("当前工具留在主栏，菜单项仍按能力禁用；选择只调用一次原动作", async () => {
    measureDock(220);
    const onSetVideoTool = vi.fn();
    const user = userEvent.setup();
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="mask"
        onSetVideoTool={onSetVideoTool}
        isPromptSupported={() => false}
      />,
    );
    expect(screen.getByTestId("video-tool-btn-select")).toBeVisible();
    expect(screen.getByTestId("video-tool-btn-mask")).toBeVisible();
    expect(screen.queryByTestId("video-tool-btn-polygon")).toBeNull();
    await user.click(screen.getByRole("button", { name: "更多工具" }));
    expect(screen.getByTestId("tool-overflow-item-smart-point")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByTestId("tool-overflow-item-smart-point")).toHaveAccessibleDescription(
      "当前后端不支持此交互模式",
    );
    await user.click(screen.getByTestId("tool-overflow-item-polygon"));
    expect(onSetVideoTool).toHaveBeenCalledTimes(1);
    expect(onSetVideoTool).toHaveBeenCalledWith("polygon");
    expect(screen.getByRole("button", { name: "更多工具" })).toHaveFocus();
  });

  it("高度变化收回菜单并恢复焦点，空间足够后恢复全量工具", async () => {
    const resize = measureDock(220);
    const user = userEvent.setup();
    render(<ToolDock tool="box" onSetTool={vi.fn()} />);
    const more = screen.getByRole("button", { name: "更多工具" });
    await user.click(more);
    expect(screen.getByTestId("tool-dock-menu")).toContainElement(
      document.activeElement as HTMLElement,
    );
    resize(300);
    expect(screen.queryByTestId("tool-dock-menu")).toBeNull();
    await waitFor(() => expect(more).toHaveFocus());
    resize(1200);
    expect(screen.queryByRole("button", { name: "更多工具" })).toBeNull();
    expect(screen.getByTestId("tool-btn-box")).toHaveFocus();
    expect(screen.getByTestId("tool-btn-magic-box")).toBeVisible();
  });

  it("用户已点击画布空白后，容量变化不抢回旧工具焦点", async () => {
    const resize = measureDock(220);
    const user = userEvent.setup();
    render(<ToolDock tool="box" onSetTool={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "更多工具" }));
    await user.keyboard("{Escape}");
    await user.click(document.body);
    expect(document.body).toHaveFocus();
    resize(300);
    expect(document.body).toHaveFocus();
  });
});

describe("ToolDock · video tools", () => {
  it("projects the requested scope and keeps select without the retired pan tool", () => {
    const { rerender } = render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );

    expect(screen.getByTestId("video-tool-btn-select")).toBeInTheDocument();
    expect(screen.getByTestId("video-tool-btn-box")).toBeInTheDocument();
    expect(screen.queryByTestId("video-tool-btn-track")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-hand")).toBeNull();
    expect(screen.queryByRole("button", { name: "平移" })).toBeNull();
    rerender(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope="track"
        onSetVideoTool={vi.fn()}
      />,
    );
    expect(screen.getByTestId("video-tool-btn-select")).toBeInTheDocument();
    expect(screen.getByTestId("video-tool-btn-track")).toBeInTheDocument();
    expect(screen.queryByTestId("video-tool-btn-box")).toBeNull();
  });

  it("keeps video select when creation modes are disabled without falling back to hand", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        isVideoToolEnabled={() => false}
      />,
    );

    expect(screen.getByTestId("video-tool-btn-select")).toBeInTheDocument();
    expect(screen.queryByTestId("video-tool-btn-box")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-track")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-polygon")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-polyline")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-hand")).toBeNull();
  });

  it("在各自范围内保持单帧、SAM 与轨迹分组，AI 追踪不占用工具栏", () => {
    const { rerender } = render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );

    const frameGroup = screen.getByRole("group", { name: "单帧工具" });
    const samGroup = within(frameGroup).getByRole("group", { name: "SAM 工具" });
    const toolIds = (root: HTMLElement) =>
      [...root.querySelectorAll<HTMLElement>("[data-testid^='video-tool-btn-']")].map((button) =>
        button.dataset.testid?.replace("video-tool-btn-", ""),
      );

    expect(toolIds(frameGroup)).toEqual([
      "box",
      "rotated-box",
      "keypoint",
      "polygon",
      "polyline",
      "mask",
      "smart-point",
      "smart-box",
      "exemplar",
      "magic-box",
    ]);
    expect(toolIds(samGroup)).toEqual(["smart-point", "smart-box", "exemplar", "magic-box"]);
    expect(screen.queryByRole("group", { name: "轨迹工具" })).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-ai-track")).toBeNull();
    expect(frameGroup).not.toContainElement(screen.getByTestId("video-tool-btn-select"));
    expect(screen.getByTestId("video-tool-btn-keypoint")).toBeDisabled();
    rerender(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope="track"
        onSetVideoTool={vi.fn()}
      />,
    );
    expect(toolIds(screen.getByRole("group", { name: "轨迹工具" }))).toEqual([
      "track",
      "polygon-track",
      "polyline-track",
      "mask-track",
    ]);
    expect(screen.queryByRole("group", { name: "单帧工具" })).toBeNull();
    expect(screen.queryByRole("group", { name: "SAM 工具" })).toBeNull();
  });

  it("配置骨骼节点后启用视频关键点工具", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoKeypointNodeCount={3}
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );
    expect(screen.getByTestId("video-tool-btn-keypoint")).toBeEnabled();
  });

  it("矩形框轨迹使用独立图标，视频 AI 与 Mask 图标和图片工作台一致", () => {
    const { rerender } = render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );

    const smartPoint = screen.getByTestId("video-tool-btn-smart-point");
    const smartBox = screen.getByTestId("video-tool-btn-smart-box");
    const exemplar = screen.getByTestId("video-tool-btn-exemplar");
    const mask = screen.getByTestId("video-tool-btn-mask");
    expect(smartPoint.querySelector(".lucide-target")).toBeInTheDocument();
    expect(smartBox.querySelector(".lucide-scan")).toBeInTheDocument();
    expect(exemplar.querySelector(".lucide-copy")).toBeInTheDocument();
    expect(mask.querySelector(".lucide-brush")).toBeInTheDocument();
    rerender(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope="track"
        onSetVideoTool={vi.fn()}
      />,
    );
    const track = screen.getByTestId("video-tool-btn-track");
    const maskTrack = screen.getByTestId("video-tool-btn-mask-track");
    expect(track).toHaveAccessibleName("矩形框轨迹");
    expect(track.querySelector(".lucide-gallery-horizontal-end")).toBeInTheDocument();
    expect(maskTrack).toHaveAccessibleName("Mask 轨迹");
    expect(maskTrack.querySelector("svg")?.innerHTML).not.toEqual(
      mask.querySelector("svg")?.innerHTML,
    );
  });

  it("shares image/video geometry while distinguishing each trajectory silhouette", () => {
    const { unmount } = render(<ToolDock tool="select" onSetTool={vi.fn()} />);
    const shared = [
      "box",
      "rotated-box",
      "keypoint",
      "polygon",
      "polyline",
      "mask",
      "smart-point",
      "smart-box",
      "exemplar",
      "magic-box",
    ];
    const imageGeometry = new Map(
      shared.map((id) => [
        id,
        screen.getByTestId(`tool-btn-${id}`).querySelector("svg")!.innerHTML,
      ]),
    );
    unmount();
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );
    for (const id of shared) {
      expect(screen.getByTestId(`video-tool-btn-${id}`).querySelector("svg")!.innerHTML).toEqual(
        imageGeometry.get(id),
      );
    }
    const buttons = document.querySelectorAll("[data-testid^='video-tool-btn-']");
    const silhouettes = [...buttons].map((button) => button.querySelector("svg")!.innerHTML);
    expect(new Set(silhouettes).size).toBe(buttons.length);
  });

  it("项目开关隐藏全部创建工具时不渲染空分组", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        aiInteractiveEnabled={false}
        isVideoToolEnabled={() => false}
      />,
    );

    expect(screen.queryByRole("group", { name: "单帧工具" })).toBeNull();
    expect(screen.queryByRole("group", { name: "SAM 工具" })).toBeNull();
    expect(screen.queryByRole("group", { name: "轨迹工具" })).toBeNull();
  });
});

describe("ToolDock · 视频范围命令", () => {
  it("先发受控范围请求，准入完成前保留原范围和工具", async () => {
    const onSetVideoToolScope = vi.fn();
    const onSetVideoTool = vi.fn();
    const user = userEvent.setup();
    const props = {
      tool: "select" as const,
      onSetTool: vi.fn(),
      videoMode: true,
      onSetVideoTool,
      onSetVideoToolScope,
    };
    const { rerender } = render(<ToolDock {...props} videoTool="polygon" videoToolScope="frame" />);
    const captured: EventTarget[][] = [];
    const onPointerDown = (event: Event) => captured.push(event.composedPath());
    document.addEventListener("pointerdown", onPointerDown, true);
    try {
      const scopeButton = screen.getByRole("button", { name: "轨迹范围" });
      await user.click(scopeButton);
      expect(captured[captured.length - 1]).toContain(scopeButton);
      expect(scopeButton).toHaveAttribute("data-workbench-video-tool-command");
      expect(onSetVideoToolScope).toHaveBeenCalledTimes(1);
      expect(onSetVideoToolScope).toHaveBeenCalledWith("track");
      expect(onSetVideoTool).not.toHaveBeenCalled();
      expect(screen.getByTestId("video-tool-scope")).toHaveAttribute("data-scope", "frame");
      expect(screen.getByTestId("video-tool-btn-polygon")).toHaveAttribute("aria-pressed", "true");
      rerender(<ToolDock {...props} videoTool="polygon-track" videoToolScope="track" />);
      expect(screen.getByRole("button", { name: "轨迹范围" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.getByTestId("video-tool-scope")).toHaveAttribute("data-scope", "track");
      expect(screen.getByTestId("video-tool-btn-polygon-track")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      expect(screen.queryByTestId("video-tool-btn-polygon")).toBeNull();
      expect(onSetVideoTool).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("pointerdown", onPointerDown, true);
    }
  });

  it("范围按钮保留原生 Enter/Space 激活，字母键不变成范围或工具命令", async () => {
    const onSetVideoToolScope = vi.fn();
    const onSetVideoTool = vi.fn();
    const user = userEvent.setup();
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope="frame"
        onSetVideoTool={onSetVideoTool}
        onSetVideoToolScope={onSetVideoToolScope}
      />,
    );
    const button = screen.getByRole("button", { name: "轨迹范围" });
    button.focus();
    await user.keyboard("{Enter}");
    expect(onSetVideoToolScope).toHaveBeenCalledTimes(1);
    expect(onSetVideoToolScope).toHaveBeenLastCalledWith("track");
    await user.keyboard(" ");
    expect(onSetVideoToolScope).toHaveBeenCalledTimes(2);
    await user.keyboard("bpmtv");
    expect(onSetVideoToolScope).toHaveBeenCalledTimes(2);
    expect(onSetVideoTool).not.toHaveBeenCalled();
    expect(screen.getByTestId("tool-dock")).not.toHaveAttribute(
      "data-workbench-video-tool-command",
    );
  });

  it("可用工具投影改变时关闭更多菜单，即使主栏始终只有选择工具", async () => {
    measureDock(90);
    const user = userEvent.setup();
    const view = (scope: "frame" | "track") => (
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope={scope}
        onSetVideoTool={vi.fn()}
        onSetVideoToolScope={vi.fn()}
      />
    );
    const { rerender } = render(view("frame"));
    for (const target of ["track", "frame", "track"] as const) {
      await user.click(screen.getByRole("button", { name: "更多工具" }));
      expect(screen.getByTestId("tool-dock-menu")).toBeVisible();
      rerender(view(target));
      await waitFor(() => expect(screen.queryByTestId("tool-dock-menu")).toBeNull());
      await waitFor(() => expect(screen.getByRole("button", { name: "更多工具" })).toHaveFocus());
      expect(screen.getByTestId("video-tool-btn-select")).toBeVisible();
      expect(screen.getByTestId("video-tool-scope")).toHaveAttribute("data-scope", target);
    }
    await user.click(screen.getByRole("button", { name: "更多工具" }));
    expect(screen.getByTestId("tool-overflow-item-polygon-track")).toHaveAttribute(
      "data-workbench-video-tool-command",
    );
    expect(screen.queryByTestId("tool-overflow-item-polygon")).toBeNull();
  });

  it("短屏范围切换保留实际激活工具，并且重新渲染不派发工具切换", () => {
    measureDock(140);
    const onSetVideoTool = vi.fn();
    const props = {
      tool: "select" as const,
      onSetTool: vi.fn(),
      videoMode: true,
      onSetVideoTool,
      onSetVideoToolScope: vi.fn(),
    };
    const { rerender } = render(<ToolDock {...props} videoTool="mask" videoToolScope="frame" />);
    expect(screen.getByTestId("video-tool-btn-mask")).toHaveAttribute("aria-pressed", "true");
    rerender(<ToolDock {...props} videoTool="mask-track" videoToolScope="track" />);
    expect(screen.getByTestId("video-tool-btn-mask-track")).toBeVisible();
    expect(screen.getByTestId("video-tool-btn-mask-track")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("video-tool-btn-mask-track")).toHaveAttribute(
      "data-workbench-video-tool-command",
    );
    expect(screen.getByTestId("video-tool-btn-select")).toBeVisible();
    expect(screen.getByRole("button", { name: "更多工具" })).toBeVisible();
    rerender(<ToolDock {...props} videoTool="mask-track" videoToolScope="track" />);
    expect(onSetVideoTool).not.toHaveBeenCalled();
  });
});

// AI 工具三层门控 (ai_interactive 伪单位退役后):
//   1. project.ai_interactive_enabled 关 → 整组隐藏
//   2. 后端不支持该 prompt → 置灰
//   3. 产出几何所属单位未启用 → 隐藏 (smart-* → region, magic-box → bbox)
describe("ToolDock · AI 工具三层门控", () => {
  const AI_TOOL_IDS = ["smart-point", "smart-box", "smart-scribble", "exemplar", "magic-box"];

  it("默认 (总开关未加载 + 无 tool_bindings) → AI 工具全部显示", () => {
    render(<ToolDock tool="select" onSetTool={vi.fn()} />);
    for (const id of AI_TOOL_IDS) {
      expect(screen.getByTestId(`tool-btn-${id}`)).toBeInTheDocument();
    }
  });

  it("层 1 · 项目总开关关闭 → AI 工具整组隐藏, 绘制工具不受影响", () => {
    render(<ToolDock tool="select" onSetTool={vi.fn()} aiInteractiveEnabled={false} />);
    for (const id of AI_TOOL_IDS) {
      expect(screen.queryByTestId(`tool-btn-${id}`)).toBeNull();
    }
    expect(screen.getByTestId("tool-btn-box")).toBeInTheDocument();
    expect(screen.getByTestId("tool-btn-select")).toBeInTheDocument();
  });

  it("层 1 · 总开关开启 → AI 工具恢复显示", () => {
    render(<ToolDock tool="select" onSetTool={vi.fn()} aiInteractiveEnabled={true} />);
    for (const id of AI_TOOL_IDS) {
      expect(screen.getByTestId(`tool-btn-${id}`)).toBeInTheDocument();
    }
  });

  it("层 2 · 后端不支持该 prompt → 置灰而非隐藏", () => {
    render(<ToolDock tool="select" onSetTool={vi.fn()} isPromptSupported={(p) => p !== "point"} />);
    // smart-point 要求 point prompt → 置灰但仍在栏内
    const smartPoint = screen.getByTestId("tool-btn-smart-point");
    expect(smartPoint).toBeInTheDocument();
    expect(smartPoint).toBeDisabled();
    // smart-box 要求 interactive_box → 正常可用
    expect(screen.getByTestId("tool-btn-smart-box")).not.toBeDisabled();
  });

  it("层 3 · 只启用 bbox 单位 → smart-*(产 polygon) 隐藏, magic-box(产 bbox) 仍在", () => {
    render(<ToolDock tool="select" onSetTool={vi.fn()} enabledToolUnits={new Set(["bbox"])} />);
    expect(screen.queryByTestId("tool-btn-smart-point")).toBeNull();
    expect(screen.queryByTestId("tool-btn-smart-box")).toBeNull();
    expect(screen.queryByTestId("tool-btn-smart-scribble")).toBeNull();
    expect(screen.queryByTestId("tool-btn-exemplar")).toBeNull();
    // magic-box 把 SAM 多边形收紧成外接矩形 → 归 bbox 单位, 故仍显示
    expect(screen.getByTestId("tool-btn-magic-box")).toBeInTheDocument();
    expect(screen.getByTestId("tool-btn-box")).toBeInTheDocument();
    // region 未启用 → 手画 polygon / mask 一并隐藏 (与 smart-* 同待遇)
    expect(screen.queryByTestId("tool-btn-polygon")).toBeNull();
  });

  it("层 3 · 启用 region 单位 → smart-* 恢复显示", () => {
    render(
      <ToolDock tool="select" onSetTool={vi.fn()} enabledToolUnits={new Set(["bbox", "region"])} />,
    );
    expect(screen.getByTestId("tool-btn-smart-point")).toBeInTheDocument();
    expect(screen.getByTestId("tool-btn-smart-scribble")).toBeInTheDocument();
    expect(screen.getByTestId("tool-btn-exemplar")).toBeInTheDocument();
    expect(screen.getByTestId("tool-btn-magic-box")).toBeInTheDocument();
  });

  it("笔迹后端能力已就绪但未选 Mask 时按上下文置灰", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        isPromptSupported={() => true}
        toolDisabledReasons={{ "smart-scribble": "请先选中原生 Mask" }}
      />,
    );
    expect(screen.getByTestId("tool-btn-smart-scribble")).toBeDisabled();
  });

  it("超限大图保留 Mask 入口并附带明确尺寸原因", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        enabledToolUnits={new Set(["bbox", "region"])}
        toolDisabledReasons={{
          mask: "当前图片 14575×8441 超过 Mask 上限（单边 8192、总像素 67,108,864）",
        }}
      />,
    );

    expect(screen.getByTestId("tool-btn-mask")).toBeDisabled();
  });

  it("层 1 优先于层 3 · 总开关关闭时, 即使单位已启用 AI 工具仍隐藏", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        aiInteractiveEnabled={false}
        enabledToolUnits={new Set(["bbox", "region"])}
      />,
    );
    for (const id of AI_TOOL_IDS) {
      expect(screen.queryByTestId(`tool-btn-${id}`)).toBeNull();
    }
    expect(screen.getByTestId("tool-btn-polygon")).toBeInTheDocument();
  });
});

// v0.21.23 · 视频侧交互式 SAM 工具（此前视频分支完全没有 ML 能力门控）
describe("ToolDock · 视频 AI 工具三层门控", () => {
  const VIDEO_AI = ["smart-point", "smart-box", "exemplar", "magic-box"];

  it("默认全开 → 视频 AI 工具显示", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
      />,
    );
    for (const id of VIDEO_AI) {
      expect(screen.getByTestId(`video-tool-btn-${id}`)).toBeInTheDocument();
    }
  });

  it("层 1 · 项目总开关关闭 → 视频 AI 工具隐藏, 几何工具不受影响", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        aiInteractiveEnabled={false}
      />,
    );
    for (const id of VIDEO_AI) {
      expect(screen.queryByTestId(`video-tool-btn-${id}`)).toBeNull();
    }
    expect(screen.getByTestId("video-tool-btn-box")).toBeInTheDocument();
    expect(screen.getByTestId("video-tool-btn-polygon")).toBeInTheDocument();
  });

  it("层 2 · 后端不支持 point → smart-point 置灰, smart-box 仍可用", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        isPromptSupported={(p) => p !== "point"}
      />,
    );
    expect(screen.getByTestId("video-tool-btn-smart-point")).toBeDisabled();
    expect(screen.getByTestId("video-tool-btn-smart-box")).not.toBeDisabled();
    // exemplar 各按自己的 requiredPrompt 判定, 不受 point 不支持牵连。
    expect(screen.getByTestId("video-tool-btn-exemplar")).not.toBeDisabled();
  });

  it("层 2 · 后端只支持 point → 仅 smart-point 可用, smart-box / exemplar 置灰", () => {
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        isPromptSupported={(p) => p === "point"}
      />,
    );
    expect(screen.getByTestId("video-tool-btn-smart-point")).not.toBeDisabled();
    expect(screen.getByTestId("video-tool-btn-smart-box")).toBeDisabled();
    expect(screen.getByTestId("video-tool-btn-exemplar")).toBeDisabled();
  });

  it("层 2 · 置灰的工具点击不切换工具", () => {
    const onSetVideoTool = vi.fn();
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={onSetVideoTool}
        isPromptSupported={() => false}
      />,
    );
    screen.getByTestId("video-tool-btn-smart-point").click();
    expect(onSetVideoTool).not.toHaveBeenCalled();
  });

  it("层 3 · region 单位未启用 → smart-* 随多边形一起隐藏（产出几何归属）", () => {
    // 模拟只启用 bbox 单位: box / track / magic-box 归 bbox, 其余归 region / polyline。
    render(
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        onSetVideoTool={vi.fn()}
        isVideoToolEnabled={(t) => t === "box" || t === "track" || t === "magic-box"}
      />,
    );
    expect(screen.queryByTestId("video-tool-btn-smart-point")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-smart-box")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-exemplar")).toBeNull();
    expect(screen.queryByTestId("video-tool-btn-polygon")).toBeNull();
    expect(screen.getByTestId("video-tool-btn-box")).toBeInTheDocument();
    // magic-box 产矩形框 → 归 bbox 单位, 只启用 bbox 时它**仍在**（与 smart-* 分家）。
    expect(screen.getByTestId("video-tool-btn-magic-box")).toBeInTheDocument();
  });
});

describe("ToolDock · 视频工具角标不撒谎", () => {
  // 角标是给用户看的承诺: 按这个键就切到这个工具。历史上 polygon 标 G、polyline 标 L 都没绑定,
  // 而视频 L 是播放 jog —— 按下去会快进。这条测试把角标与 hotkeys.ts 的真实绑定钉在一起。
  it("每个渲染出的角标都真能 dispatch 到它标注的工具", () => {
    const view = (scope: "frame" | "track") => (
      <ToolDock
        tool="select"
        onSetTool={vi.fn()}
        videoMode
        videoTool="select"
        videoToolScope={scope}
        onSetVideoTool={vi.fn()}
        isPromptSupported={() => true}
      />
    );
    const { rerender } = render(view("frame"));
    const base: DispatchCtx = {
      isInputFocused: false,
      hasSelection: false,
      pendingActive: false,
      videoMode: true,
    };
    const buttons = [...document.querySelectorAll<HTMLElement>("[data-testid^='video-tool-btn-']")];
    rerender(view("track"));
    buttons.push(...document.querySelectorAll<HTMLElement>("[data-testid^='video-tool-btn-']"));
    expect(new Set(buttons.map((button) => button.dataset.testid)).size).toBe(15);

    let checked = 0;
    for (const btn of buttons) {
      const id = btn.dataset.testid!.replace("video-tool-btn-", "");
      const badge = btn.querySelector("span[aria-hidden]")?.textContent?.trim();
      if (!badge) continue; // 无角标 = 未承诺快捷键 (polyline / *-track), 合法
      const action = dispatchKey(
        {
          key: badge.toLowerCase(),
          ctrlKey: false,
          metaKey: false,
          shiftKey: false,
          altKey: false,
        } as KeyboardEvent,
        base,
      );
      expect(action, `角标 ${badge} 标在 ${id} 上，但按下去不是切到它`).toEqual({
        type: "setVideoTool",
        tool: id,
      });
      checked += 1;
    }
    expect(checked).toBeGreaterThanOrEqual(8); // V B T P S D E G
  });
});
