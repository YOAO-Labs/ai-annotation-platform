import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { PerfHud, usePerfHudStore } from "@/components/PerfHud";
import { JobsBell } from "./JobsBell";
import { NotificationsPopover } from "./NotificationsPopover";

vi.mock("@/api/asyncJobs", () => ({
  CANCELLABLE_ASYNC_JOB_KINDS: new Set(),
  asyncJobsApi: { list: async () => ({ items: [], total: 0 }) },
}));
vi.mock("@/hooks/useNotifications", () => ({
  useNotifications: () => ({ data: { pages: [{ items: [] }] }, hasNextPage: false }),
  useUnreadCount: () => ({ data: { unread: 0 } }),
  useMarkRead: () => ({ mutate: vi.fn() }),
  useMarkAllRead: () => ({ mutate: vi.fn() }),
  useClearReadNotifications: () => ({ mutate: vi.fn() }),
  useDeleteNotification: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/components/PerfHud/useMLBackendStats", () => ({
  useMLBackendStats: () => ({ snapshots: {}, history: {}, connected: false, status: "idle" }),
}));
vi.mock("@/components/PerfHud/useBrowserStats", () => ({
  useBrowserStats: () => ({ longtaskCount60s: 0, wsReconnects: 0, taskBoxCount: 0 }),
}));

function renderPanels() {
  const outsideClick = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <button onClick={outsideClick}>正文操作</button>
        <button
          data-shell-popover-trigger="performance"
          onClick={() => usePerfHudStore.getState().toggle()}
        >
          性能监控
        </button>
        <JobsBell />
        <NotificationsPopover />
        <PerfHud />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return outsideClick;
}

const panels = [
  { trigger: "性能监控", label: "GPU 性能监控" },
  { trigger: "后台任务", label: "后台任务" },
  { trigger: "通知，0 条未读", label: "通知" },
];

describe("top-bar information panels", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("fixture-token", {
      id: "fixture-admin",
      role: "super_admin",
    } as MeResponse);
    usePerfHudStore.setState({ visible: false, expanded: false });
  });

  it.each(panels)(
    "$trigger keeps inside clicks and closes on outside interaction",
    async ({ trigger, label }) => {
      const outsideClick = renderPanels();
      fireEvent.click(screen.getByRole("button", { name: trigger }));
      const panel = await screen.findByRole("dialog", { name: label });
      fireEvent.pointerDown(panel);
      fireEvent.click(panel);
      expect(panel).toBeInTheDocument();

      const outside = screen.getByRole("button", { name: "正文操作" });
      fireEvent.pointerDown(outside);
      fireEvent.click(outside);
      expect(screen.queryByRole("dialog", { name: label })).not.toBeInTheDocument();
      expect(outsideClick).toHaveBeenCalledOnce();
    },
  );

  it.each(panels)(
    "$trigger toggles off on its trigger and Escape restores focus",
    async ({ trigger, label }) => {
      renderPanels();
      const button = screen.getByRole("button", { name: trigger });
      fireEvent.click(button);
      await screen.findByRole("dialog", { name: label });
      fireEvent.pointerDown(button);
      fireEvent.click(button);
      expect(screen.queryByRole("dialog", { name: label })).not.toBeInTheDocument();

      fireEvent.click(button);
      fireEvent.keyDown(await screen.findByRole("dialog", { name: label }), { key: "Escape" });
      expect(screen.queryByRole("dialog", { name: label })).not.toBeInTheDocument();
      expect(button).toHaveFocus();
    },
  );

  it("replaces the open panel, including performance opened by a shortcut", async () => {
    renderPanels();
    fireEvent.click(screen.getByRole("button", { name: "后台任务" }));
    await screen.findByRole("dialog", { name: "后台任务" });
    fireEvent.click(screen.getByRole("button", { name: "通知，0 条未读" }));
    await screen.findByRole("dialog", { name: "通知" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);

    act(() => usePerfHudStore.getState().open());
    await screen.findByRole("dialog", { name: "GPU 性能监控" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "通知，0 条未读" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
