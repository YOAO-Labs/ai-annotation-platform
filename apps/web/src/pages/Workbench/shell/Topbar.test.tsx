import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { Topbar } from "./Topbar";
import { useBugDrawerStore } from "@/stores/bugDrawerStore";

it("omits release-note entries from the header and overflow menu", () => {
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );

  expect(screen.queryByTestId("workbench-whats-new")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /更新内容/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "更多工具" }));
  expect(within(screen.getByRole("menu")).queryByText(/本次更新/)).not.toBeInTheDocument();
});

it("opens the shared bug drawer from the workbench header", () => {
  act(() => useBugDrawerStore.getState().close());
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );

  const report = screen.getByRole("button", { name: "报告 Bug / 提交反馈" });
  expect(report).toHaveAttribute("title", "报告 Bug / 提交反馈");
  fireEvent.click(report);
  expect(useBugDrawerStore.getState().open).toBe(true);
  act(() => useBugDrawerStore.getState().close());
});

it("preserves native segment selector keys without activating overflow menu actions", () => {
  const onSmartNextOpen = vi.fn();
  const onSelectVideoSegment = vi.fn();
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
      onSmartNextOpen={onSmartNextOpen}
      videoSegments={[]}
      activeVideoSegmentId="segment-1"
      onSelectVideoSegment={onSelectVideoSegment}
    />,
  );
  const trigger = screen.getByRole("button", { name: "更多工具" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("menu");
  const select = within(menu).getByRole("combobox", { name: "当前视频分段" });
  expect(within(select).getByRole("option", { name: "退出当前分段" })).toBeEnabled();
  select.focus();
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "]) {
    expect(fireEvent.keyDown(select, { key })).toBe(true);
    expect(select).toHaveFocus();
    expect(menu).toBeInTheDocument();
    expect(onSmartNextOpen).not.toHaveBeenCalled();
  }
  fireEvent.change(select, { target: { value: "" } });
  expect(onSelectVideoSegment).toHaveBeenCalledWith(null);
  // Tab 在 items 菜单内（含 footer 原生 select）统一为关闭语义：关闭并归还
  // 触发器，页面遍历从触发器继续；否则 portal 面板在 body 末尾，放行 Tab 会把
  // 焦点带出文档而菜单仍开着（PR #117 评审 P2）
  fireEvent.keyDown(select, { key: "Tab" });
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();

  fireEvent.click(trigger);
  fireEvent.keyDown(screen.getByRole("menuitem", { name: /下一未标注/ }), { key: "Enter" });
  expect(onSmartNextOpen).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

describe("Topbar · AI 工具入口", () => {
  it("updates the inference scope when switching between image and video tasks", () => {
    const props: ComponentProps<typeof Topbar> = {
      projectName: "测试",
      projectDisplayId: "P-1",
      task: undefined,
      taskIdx: 0,
      taskTotal: 1,
      aiRunning: false,
      isSubmitting: false,
      onShowHotkeys: vi.fn(),
      onPrev: vi.fn(),
      onNext: vi.fn(),
      onSubmit: vi.fn(),
      onRunAi: vi.fn(),
    };
    const { rerender } = render(<Topbar {...props} />);
    const button = screen.getByTestId("workbench-ai-single");
    expect(button).toHaveTextContent("单图");
    expect(button).toHaveAccessibleName("单图");
    expect(button).toHaveAttribute("title", "打开单图 AI 面板，对当前图片运行 AI");
    rerender(<Topbar {...props} isVideoTask />);
    expect(button).toHaveTextContent("单帧");
    expect(button).toHaveAccessibleName("单帧");
    expect(button).toHaveAttribute("title", "打开单帧 AI 面板，对当前帧运行 AI");
    rerender(<Topbar {...props} isVideoTask aiOpen />);
    expect(button).toHaveAttribute("title", "聚焦单帧 AI 面板");
  });

  it("keeps unavailable AI entries visible, explains why, and blocks clicks", () => {
    const onRunAi = vi.fn();
    const onToggleTracker = vi.fn();
    const props = {
      projectName: "测试",
      projectDisplayId: "P-1",
      task: undefined,
      taskIdx: 0,
      taskTotal: 1,
      aiRunning: false,
      isSubmitting: false,
      onShowHotkeys: vi.fn(),
      onPrev: vi.fn(),
      onNext: vi.fn(),
      onSubmit: vi.fn(),
      onRunAi,
      onToggleTracker,
    };
    const reason = "项目尚未接入 AI 后端";
    const { rerender } = render(
      <Topbar
        {...props}
        aiDisabled
        trackerDisabled
        aiDisabledReason={reason}
        trackerDisabledReason={reason}
      />,
    );
    for (const name of ["单图", "追踪"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeVisible();
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute("title", reason);
      expect(button).toHaveAttribute("aria-description", reason);
      fireEvent.click(button);
    }
    expect(onRunAi).not.toHaveBeenCalled();
    expect(onToggleTracker).not.toHaveBeenCalled();
    rerender(<Topbar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "追踪" }));
    fireEvent.click(screen.getByRole("button", { name: "单图" }));
    expect(onToggleTracker).toHaveBeenCalledOnce();
    expect(onRunAi).toHaveBeenCalledOnce();
  });

  it("将 AI 追踪放在 单图左侧，并复用同一按钮样式", () => {
    const onToggleTracker = vi.fn();
    const onRunAi = vi.fn();

    render(
      <Topbar
        projectName="测试项目"
        projectDisplayId="P-1"
        task={undefined}
        taskIdx={0}
        taskTotal={1}
        aiRunning={false}
        isSubmitting={false}
        onShowHotkeys={vi.fn()}
        onRunAi={onRunAi}
        onToggleTracker={onToggleTracker}
        trackerOpen
        aiOpen={false}
        onPrev={vi.fn()}
        onNext={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    for (const label of ["上一个任务", "下一个任务"]) {
      const navigation = screen.getByRole("button", { name: label });
      expect(navigation).toHaveAttribute("title", label);
      expect(navigation.textContent).toBe("");
      expect(navigation.querySelector("svg")).not.toBeNull();
    }
    expect(screen.getByTestId("workbench-submit")).toHaveTextContent(/^提交$/);

    const tracker = screen.getByTestId("workbench-ai-tracker");
    const single = screen.getByTestId("workbench-ai-single");

    expect(tracker.nextElementSibling).toBe(single);
    expect(tracker.className).toBe(single.className);
    expect(tracker).toHaveAttribute("aria-pressed", "true");
    expect(tracker).toHaveAccessibleName("追踪");
    expect(tracker).toHaveAttribute("title", "聚焦画布级多目标追踪");
    expect(single).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(tracker);
    fireEvent.click(single);
    expect(onToggleTracker).toHaveBeenCalledTimes(1);
    expect(onRunAi).toHaveBeenCalledTimes(1);
  });
});

it("gates layout buttons until preferences settle", () => {
  const toggle = vi.fn();
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onRunAi={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
      onToggleSide={toggle}
      sides={{ left: "open", right: "collapsed" }}
      layoutDisabled
      layoutMenuSlot={<button>布局</button>}
    />,
  );
  expect(screen.getByRole("button", { name: "左侧面板" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "右侧面板" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "布局" })).toBeVisible();
});

it("keeps the guide slot beside the desktop settings controls", () => {
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
      onOpenWorkbenchSettings={vi.fn()}
      guideSlot={<button aria-label="标注指引">指南</button>}
    />,
  );

  const guide = screen.getByRole("button", { name: "标注指引" });
  const settings = screen.getByRole("button", { name: "工作台设置" });
  expect(guide.parentElement?.parentElement).toBe(settings.parentElement?.parentElement);
});

it("labels physical sides, exposes expanded state, and disables an empty side", () => {
  const toggle = vi.fn();
  render(
    <Topbar
      projectName="测试"
      projectDisplayId="P-1"
      task={undefined}
      taskIdx={0}
      taskTotal={1}
      aiRunning={false}
      isSubmitting={false}
      onShowHotkeys={vi.fn()}
      onPrev={vi.fn()}
      onNext={vi.fn()}
      onSubmit={vi.fn()}
      onToggleSide={toggle}
      sides={{ left: "empty", right: "collapsed" }}
    />,
  );
  expect(screen.getByRole("button", { name: "左侧面板" })).toBeDisabled();
  const right = screen.getByRole("button", { name: "右侧面板" });
  expect(right).toHaveAttribute("aria-expanded", "false");
  expect(right).toHaveAttribute("title", "展开画布右侧所有面板");
  fireEvent.click(right);
  expect(toggle).toHaveBeenCalledWith("right");
});

describe("Topbar · 通知入口", () => {
  vi.mock("@/hooks/useNotifications", () => ({
    useNotifications: () => ({ data: { pages: [{ items: [] }] }, hasNextPage: false }),
    useUnreadCount: () => ({ data: { unread: 3 } }),
    useMarkRead: () => ({ mutate: vi.fn() }),
    useMarkAllRead: () => ({ mutate: vi.fn() }),
    useClearReadNotifications: () => ({ mutate: vi.fn() }),
    useDeleteNotification: () => ({ mutate: vi.fn() }),
  }));

  const baseProps = {
    projectName: "测试",
    projectDisplayId: "P-1",
    task: undefined,
    taskIdx: 0,
    taskTotal: 1,
    aiRunning: false,
    isSubmitting: false,
    onShowHotkeys: vi.fn(),
    onPrev: vi.fn(),
    onNext: vi.fn(),
    onSubmit: vi.fn(),
  };

  function renderTopbar(props: Partial<ComponentProps<typeof Topbar>> = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Topbar {...baseProps} {...props} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("缺省不渲染通知入口，传入回调后渲染带未读数的铃铛", async () => {
    const { unmount } = renderTopbar();
    expect(screen.queryByTestId("notifications-trigger")).not.toBeInTheDocument();
    unmount();

    renderTopbar({ onNotificationNavigate: vi.fn() });
    const bell = await screen.findByRole("button", { name: "通知，3 条未读" });
    expect(screen.getByTestId("notifications-unread-badge")).toHaveTextContent("3");
    // 位于 Bug/主题/设置组之外，不随窄屏隐藏组折叠
    expect(bell.parentElement).not.toHaveClass(/hidden/);
  });

  it("面板可打开关闭，Escape 后焦点回到触发器", async () => {
    const onNotificationNavigate = vi.fn();
    renderTopbar({ onNotificationNavigate });
    fireEvent.click(await screen.findByRole("button", { name: "通知，3 条未读" }));
    expect(await screen.findByRole("dialog", { name: "通知" })).toBeInTheDocument();
    // 面板打开后画布守卫可识别（data-shell-popover 存在）
    expect(document.querySelector('[data-shell-popover="notifications"]')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("dialog", { name: "通知" }), { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "通知" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "通知，3 条未读" })).toHaveFocus();
    expect(onNotificationNavigate).not.toHaveBeenCalled();
  });
});
