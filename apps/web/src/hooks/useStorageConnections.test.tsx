import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { createTestQueryClient } from "@/test/queryClient";
import { useAuthStore } from "@/stores/authStore";
import { useCreateStorageConnection, useStorageConnections } from "./useStorageConnections";

const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn() }));
vi.mock("@/api/storageConnections", () => ({ storageConnectionsApi: api }));
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    sessionId: "connection-session",
    token: "token",
    user: { id: "manager", role: "project_admin" } as any,
  });
});
function setup() {
  const client = createTestQueryClient();
  clients.push(client);
  return {
    client,
    wrapper: ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}
it.each([null, "employee", "annotator", "reviewer", "viewer"])(
  "does not request configuration for the %s role",
  async (role) => {
    useAuthStore.setState({ user: role ? ({ id: "staff", role } as any) : null });
    const { wrapper } = setup();
    const hook = renderHook(() => useStorageConnections(), { wrapper });
    await act(async () => {});
    expect(api.list).not.toHaveBeenCalled();
    expect(hook.result.current.data).toBeUndefined();
    expect(hook.result.current.fetchStatus).toBe("idle");
  },
);
it.each(["super_admin", "project_admin"])("loads configuration for %s", async (role) => {
  useAuthStore.setState({ user: { id: "manager", role } as any });
  api.list.mockResolvedValueOnce([{ id: "visible" }]);
  const { wrapper } = setup();
  const hook = renderHook(() => useStorageConnections(), { wrapper });
  await waitFor(() => expect(hook.result.current.data).toEqual([{ id: "visible" }]));
});
it("retires a manager query on downgrade without exposing a late configuration response", async () => {
  let resolve!: (value: unknown) => void;
  api.list.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { wrapper } = setup();
  const hook = renderHook(() => useStorageConnections(), { wrapper });
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
  const signal = api.list.mock.calls[0][0] as AbortSignal;
  act(() => useAuthStore.setState({ user: { id: "manager", role: "employee" } as any }));
  await waitFor(() => expect(signal.aborted).toBe(true));
  await act(async () => resolve([{ id: "retired-private-config" }]));
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(hook.result.current.data).toBeUndefined();
  expect(hook.result.current.fetchStatus).toBe("idle");
});
it("aborts a retired connection query and never exposes its late list to a new login", async () => {
  let resolveOld!: (value: unknown) => void;
  api.list
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    )
    .mockResolvedValueOnce([{ id: "new" }]);
  const { wrapper } = setup();
  const hook = renderHook(() => useStorageConnections(), { wrapper });
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
  const oldSignal = api.list.mock.calls[0][0] as AbortSignal;
  act(() => useAuthStore.setState({ sessionId: "new-session" }));
  await waitFor(() => expect(hook.result.current.data).toEqual([{ id: "new" }]));
  expect(oldSignal.aborted).toBe(true);
  await act(async () => {
    resolveOld([{ id: "old-private" }]);
  });
  expect(hook.result.current.data).toEqual([{ id: "new" }]);
});
it("does not invalidate a new session's location/configuration cache after a retired create completes", async () => {
  let resolve!: (value: unknown) => void;
  api.create.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const { client, wrapper } = setup();
  const invalidation = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(() => useCreateStorageConnection(), { wrapper });
  act(() => hook.result.current.mutate({ name: "Archive", kind: "s3", config: {}, secret: {} }));
  await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
  act(() => useAuthStore.setState({ sessionId: "new-session" }));
  await act(async () => {
    resolve({ id: "retired-created" });
  });
  expect(invalidation).not.toHaveBeenCalled();
});
