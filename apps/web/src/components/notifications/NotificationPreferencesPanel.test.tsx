import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

const api = vi.hoisted(() => ({
  getPreferences: vi.fn(),
  updatePreference: vi.fn(),
}));
vi.mock("@/api/notifications", () => ({
  notificationsApi: {
    getPreferences: api.getPreferences,
    updatePreference: api.updatePreference,
  },
}));

import {
  NotificationPreferencesPanel,
  notificationPreferencesMatchQuery,
} from "./NotificationPreferencesPanel";

const items = [
  { type: "task.rejected", in_app: true, email: false, toast: true },
  { type: "task.approved", in_app: true, email: false, toast: false },
  { type: "batch.rejected", in_app: false, email: false, toast: true },
  { type: "bug_report.commented", in_app: true, email: false, toast: false },
];

function mountPanel(filterQuery?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <NotificationPreferencesPanel filterQuery={filterQuery} />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

function deferredSave() {
  let resolve!: (value: { ok: boolean }) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<{ ok: boolean }>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.updatePreference.mockReset().mockResolvedValue({ ok: true });
  api.getPreferences.mockResolvedValue({ items });
  useAuthStore.getState().setAuth("t", { id: "u1", role: "employee" } as MeResponse);
});

describe("NotificationPreferencesPanel", () => {
  it("按组展示已知类型，每类两个开关；关闭接收时弹出只禁用不重置", async () => {
    mountPanel();
    expect(await screen.findByTestId("notification-preference-task.rejected")).toBeVisible();
    // 分组标题存在
    expect(screen.getByText("任务与审核")).toBeVisible();
    expect(screen.getByText("批次")).toBeVisible();
    expect(screen.getByText("Bug 反馈")).toBeVisible();

    const receipt = screen.getByRole("checkbox", { name: "批次被驳回 接收通知" });
    const popup = screen.getByRole("checkbox", { name: "批次被驳回 弹出提示" });
    expect(receipt).not.toBeChecked();
    expect(popup).toBeDisabled();
    // 保留的弹出选择仍显示为已选
    expect(popup).toBeChecked();

    // 接收开启的类型：两开关可用
    expect(screen.getByRole("checkbox", { name: "任务被退回 弹出提示" })).toBeEnabled();
  });

  it("切换接收只发送 in_app；切换弹出只发送 toast", async () => {
    mountPanel();
    await screen.findByTestId("notification-preference-task.approved");

    fireEvent.click(screen.getByRole("checkbox", { name: "任务审核通过 接收通知" }));
    await waitFor(() =>
      expect(api.updatePreference).toHaveBeenCalledWith("task.approved", { in_app: false }),
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "任务被退回 弹出提示" }));
    await waitFor(() =>
      expect(api.updatePreference).toHaveBeenCalledWith("task.rejected", { toast: false }),
    );
  });

  it("保存期间该类两个开关禁用并显示保存中", async () => {
    let resolveSave!: (value: { ok: boolean }) => void;
    api.updatePreference.mockReturnValue(new Promise((r) => (resolveSave = r)));
    mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");

    fireEvent.click(screen.getByRole("checkbox", { name: "任务被退回 接收通知" }));
    await waitFor(() => expect(screen.getByText("保存中…")).toBeVisible());
    expect(screen.getByRole("checkbox", { name: "任务被退回 接收通知" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "任务被退回 弹出提示" })).toBeDisabled();
    // 其它类型不受影响
    expect(screen.getByRole("checkbox", { name: "任务审核通过 接收通知" })).toBeEnabled();

    await act(async () => {
      resolveSave({ ok: true });
    });
    await waitFor(() => expect(screen.queryByText("保存中…")).toBeNull());
  });

  it("保存失败恢复上次确认值并提供本行重试", async () => {
    api.updatePreference.mockRejectedValueOnce(new Error("network"));
    mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");

    const receipt = screen.getByRole("checkbox", { name: "任务被退回 接收通知" });
    fireEvent.click(receipt);
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败");
    // 恢复为确认值
    await waitFor(() => expect(receipt).toBeChecked());

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.updatePreference).toHaveBeenCalledTimes(2));
  });

  it("连续修改不同类型时，每行等待自己的保存完成，并接受后续服务端值", async () => {
    const first = deferredSave();
    const second = deferredSave();
    api.updatePreference.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { client } = mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");
    const firstReceipt = screen.getByRole("checkbox", { name: "任务被退回 接收通知" });
    const secondReceipt = screen.getByRole("checkbox", { name: "任务审核通过 接收通知" });
    fireEvent.click(firstReceipt);
    fireEvent.click(secondReceipt);
    await waitFor(() => expect(api.updatePreference).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText("保存中…")).toHaveLength(2);

    await act(async () => second.resolve({ ok: true }));
    await waitFor(() => expect(secondReceipt).toBeEnabled());
    expect(firstReceipt).toBeDisabled();
    expect(screen.getByText("保存中…")).toBeVisible();

    api.getPreferences.mockResolvedValue({
      items: items.map((item) =>
        item.type.startsWith("task.") ? { ...item, in_app: false } : item,
      ),
    });
    await act(async () => first.resolve({ ok: true }));
    await waitFor(() => expect(firstReceipt).toBeEnabled());
    expect(firstReceipt).not.toBeChecked();

    api.getPreferences.mockResolvedValue({ items });
    await act(async () => {
      await client.refetchQueries({ queryKey: ["notification-preferences", "u1"] });
    });
    await waitFor(() => {
      expect(firstReceipt).toBeChecked();
      expect(secondReceipt).toBeChecked();
    });
  });

  it("连续保存时第一行失败仍独立回滚和重试，不影响另一行", async () => {
    const first = deferredSave();
    const second = deferredSave();
    api.updatePreference.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    mountPanel();
    const firstRow = await screen.findByTestId("notification-preference-task.rejected");
    const secondRow = screen.getByTestId("notification-preference-task.approved");
    const firstReceipt = within(firstRow).getByRole("checkbox", { name: /接收通知/ });
    const secondReceipt = within(secondRow).getByRole("checkbox", { name: /接收通知/ });
    fireEvent.click(firstReceipt);
    fireEvent.click(secondReceipt);
    await waitFor(() => expect(api.updatePreference).toHaveBeenCalledTimes(2));

    await act(async () => first.reject(new Error("first save failed")));
    expect(within(firstRow).getByRole("alert")).toHaveTextContent("保存失败");
    expect(firstReceipt).toBeChecked();
    expect(firstReceipt).toBeEnabled();
    expect(secondReceipt).toBeDisabled();
    expect(within(secondRow).getByText("保存中…")).toBeVisible();

    await act(async () => second.resolve({ ok: true }));
    await waitFor(() => expect(secondReceipt).toBeEnabled());
    expect(within(secondRow).queryByRole("alert")).toBeNull();
    fireEvent.click(within(firstRow).getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.updatePreference).toHaveBeenCalledTimes(3));
    expect(api.updatePreference).toHaveBeenLastCalledWith("task.rejected", { in_app: false });
    await waitFor(() => expect(within(firstRow).queryByRole("alert")).toBeNull());
  });

  it("初始加载失败禁用写入并显示重试", async () => {
    api.getPreferences.mockRejectedValue(new Error("down"));
    mountPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载通知偏好");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.getPreferences).toHaveBeenCalledTimes(2));
    expect(api.updatePreference).not.toHaveBeenCalled();
  });

  it("filterQuery 只显示匹配的类型", async () => {
    mountPanel("批次");
    await screen.findByTestId("notification-preferences-panel");
    expect(screen.getByTestId("notification-preference-batch.rejected")).toBeVisible();
    expect(screen.queryByTestId("notification-preference-task.rejected")).toBeNull();
    expect(screen.queryByTestId("notification-preference-bug_report.commented")).toBeNull();
  });

  it.each(["通知", "通知偏好", " 通知 "])("分类搜索「%s」保留全部通知类型", async (query) => {
    mountPanel(query);
    await screen.findByTestId("notification-preferences-panel");
    for (const item of items) {
      expect(screen.getByTestId(`notification-preference-${item.type}`)).toBeVisible();
    }
    expect(screen.queryByText("没有匹配的通知类型。")).toBeNull();
  });

  it("账号切换后丢弃旧账号的待保存/失败状态", async () => {
    api.updatePreference.mockReturnValue(new Promise(() => {}));
    mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");
    fireEvent.click(screen.getByRole("checkbox", { name: "任务被退回 接收通知" }));
    await screen.findByText("保存中…");

    act(() => {
      useAuthStore.getState().setAuth("t2", { id: "u2", role: "employee" } as MeResponse);
    });
    await waitFor(() => expect(screen.queryByText("保存中…")).toBeNull());
  });

  it("旧账号迟到的保存失败不能重新写入新账号的错误状态", async () => {
    const oldSave = deferredSave();
    api.updatePreference.mockReturnValueOnce(oldSave.promise);
    mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");
    fireEvent.click(screen.getByRole("checkbox", { name: "任务被退回 接收通知" }));
    await screen.findByText("保存中…");
    act(() => {
      useAuthStore.getState().setAuth("t2", { id: "u2", role: "employee" } as MeResponse);
    });
    await waitFor(() => expect(screen.queryByText("保存中…")).toBeNull());
    // 等新账号偏好加载完成后再放行旧账号的迟到失败:账号切换会重拉偏好,
    // 若在此前的加载态断言,会和 CI 高负载下的查询结算竞态。
    await screen.findByRole("checkbox", { name: "任务被退回 接收通知" }, { timeout: 5_000 });

    await act(async () => oldSave.reject(new Error("retired owner failed")));
    const receipt = screen.getByRole("checkbox", { name: "任务被退回 接收通知" });
    expect(receipt).toBeEnabled();
    expect(receipt).toBeChecked();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["success", "failure"])("旧账号迟到的 %s 不得结束新账号同类型的保存", async (outcome) => {
    const oldSave = deferredSave();
    const newSave = deferredSave();
    api.updatePreference.mockReturnValueOnce(oldSave.promise).mockReturnValueOnce(newSave.promise);
    mountPanel();
    await screen.findByTestId("notification-preference-task.rejected");
    fireEvent.click(screen.getByRole("checkbox", { name: "任务被退回 接收通知" }));
    await screen.findByText("保存中…");
    act(() => {
      useAuthStore.getState().setAuth("t2", { id: "u2", role: "employee" } as MeResponse);
    });
    const popup = await screen.findByRole("checkbox", { name: "任务被退回 弹出提示" });
    await waitFor(() => expect(popup).toBeEnabled());
    fireEvent.click(popup);
    await waitFor(() => expect(api.updatePreference).toHaveBeenCalledTimes(2));

    await act(async () => {
      if (outcome === "success") oldSave.resolve({ ok: true });
      else oldSave.reject(new Error("retired owner failed"));
    });
    expect(popup).toBeDisabled();
    expect(popup).not.toBeChecked();
    expect(screen.getByText("保存中…")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => newSave.reject(new Error("current owner failed")));
    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败");
    expect(popup).toBeEnabled();
    expect(popup).toBeChecked();
  });

  it("notificationPreferencesMatchQuery 命中标签与「通知」关键词", () => {
    expect(notificationPreferencesMatchQuery(items, "退回")).toBe(true);
    expect(notificationPreferencesMatchQuery(items, "task.rejected")).toBe(true);
    expect(notificationPreferencesMatchQuery(items, "通知")).toBe(true);
    expect(notificationPreferencesMatchQuery(items, "不存在的词")).toBe(false);
    expect(notificationPreferencesMatchQuery(undefined, "退回")).toBe(false);
  });
});
