import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import type { NotificationsResponse, UnreadCountResponse } from "@/api/notifications";
import { useAuthStore } from "@/stores/authStore";
import { createTestQueryClient } from "@/test/queryClient";
import { useNotifications, useUnreadCount } from "../useNotifications";

const api = vi.hoisted(() => ({ list: vi.fn(), unreadCount: vi.fn() }));
vi.mock("@/api/notifications", () => ({ notificationsApi: api }));
const privatePage: NotificationsResponse = {
  items: [
    {
      id: "private-inventory",
      type: "job.failed",
      target_type: "async_job",
      target_id: "job",
      payload: { kind: "storage_migration_inventory", error_message: "private result" },
      read_at: null,
      created_at: "2026-10-25T00:00:00Z",
    },
  ],
  total: 1,
  unread: 1,
};
const publicPage: NotificationsResponse = {
  items: [{ ...privatePage.items[0], id: "ordinary-job", payload: { kind: "dataset_import" } }],
  total: 1,
  unread: 1,
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
const unmounts: (() => void)[] = [];
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const view = renderHook(() => ({ list: useNotifications(), unread: useUnreadCount() }), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  unmounts.push(view.unmount);
  return view;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function login() {
  useAuthStore
    .getState()
    .setAuth("notification-token", { id: "owner", role: "super_admin" } as MeResponse);
}
beforeEach(() => {
  vi.clearAllMocks();
  login();
  api.list.mockResolvedValue(privatePage);
  api.unreadCount.mockResolvedValue({ unread: 9 });
});
afterEach(() => {
  unmounts.splice(0).forEach((unmount) => unmount());
  clients.splice(0).forEach((client) => client.clear());
  act(() => useAuthStore.getState().logout());
});

it("same-account loss of superadmin qualification immediately retires cached private notifications and count", async () => {
  const { result } = mount();
  await waitFor(() => expect(result.current.list.data?.pages[0]).toEqual(privatePage));
  await waitFor(() => expect(result.current.unread.data?.unread).toBe(9));
  const nextList = deferred<NotificationsResponse>();
  const nextUnread = deferred<UnreadCountResponse>();
  api.list.mockReturnValueOnce(nextList.promise);
  api.unreadCount.mockReturnValueOnce(nextUnread.promise);
  act(() => useAuthStore.getState().setUser({ id: "owner", role: "project_admin" } as MeResponse));
  expect(result.current.list.data).toBeUndefined();
  expect(result.current.unread.data).toBeUndefined();
  await act(async () => {
    nextList.resolve(publicPage);
    nextUnread.resolve({ unread: 1 });
  });
  await waitFor(() => expect(result.current.list.data?.pages[0]).toEqual(publicPage));
  expect(result.current.unread.data?.unread).toBe(1);
});

it.each(["qualification", "session"])(
  "late private list and count cannot enter the replacement %s owner",
  async (change) => {
    const oldList = deferred<NotificationsResponse>();
    const oldUnread = deferred<UnreadCountResponse>();
    api.list.mockReturnValueOnce(oldList.promise).mockResolvedValue(publicPage);
    api.unreadCount.mockReturnValueOnce(oldUnread.promise).mockResolvedValue({ unread: 1 });
    const { result } = mount();
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
    act(() => {
      if (change === "qualification")
        useAuthStore.getState().setUser({ id: "owner", role: "project_admin" } as MeResponse);
      else {
        useAuthStore.getState().logout();
        login();
      }
    });
    await waitFor(() => expect(result.current.list.data?.pages[0]).toEqual(publicPage));
    await waitFor(() => expect(result.current.unread.data?.unread).toBe(1));
    await act(async () => {
      oldList.resolve(privatePage);
      oldUnread.resolve({ unread: 999 });
    });
    expect(result.current.list.data?.pages[0]).toEqual(publicPage);
    expect(result.current.unread.data?.unread).toBe(1);
  },
);

it("ordinary role updates retain existing notification semantics without a new privilege partition", async () => {
  useAuthStore.getState().setUser({ id: "owner", role: "employee" } as MeResponse);
  api.list.mockResolvedValue(publicPage);
  const { result } = mount();
  await waitFor(() => expect(result.current.list.data?.pages[0]).toEqual(publicPage));
  act(() => useAuthStore.getState().setUser({ id: "owner", role: "project_admin" } as MeResponse));
  expect(result.current.list.data?.pages[0]).toEqual(publicPage);
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(api.unreadCount).toHaveBeenCalledTimes(1);
});
