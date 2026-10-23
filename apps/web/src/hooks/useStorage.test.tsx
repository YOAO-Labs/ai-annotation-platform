import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { createTestQueryClient } from "@/test/queryClient";
import { useAuthStore } from "@/stores/authStore";
import { useStorageBuckets, useStartStorageInventory } from "./useStorage";
const api = vi.hoisted(() => ({ buckets: vi.fn(), startInventory: vi.fn() }));
vi.mock("@/api/storage", () => ({ storageApi: api }));
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    sessionId: "usage-session",
    token: "token",
    user: { id: "admin", role: "super_admin" } as any,
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
it("aborts an old owner's snapshot read and discards its late result", async () => {
  let old!: (value: unknown) => void;
  api.buckets
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          old = resolve;
        }),
    )
    .mockResolvedValueOnce({ items: ["new"] });
  const { wrapper } = setup();
  const hook = renderHook(() => useStorageBuckets(), { wrapper });
  await waitFor(() => expect(api.buckets).toHaveBeenCalledTimes(1));
  const signal = api.buckets.mock.calls[0][0] as AbortSignal;
  act(() => useAuthStore.setState({ sessionId: "next" }));
  await waitFor(() => expect(hook.result.current.data).toEqual({ items: ["new"] }));
  expect(signal.aborted).toBe(true);
  await act(async () => {
    old({ items: ["private"] });
  });
  expect(hook.result.current.data).toEqual({ items: ["new"] });
});
it("ignores invalidation from a start belonging to a retired session", async () => {
  let done!: (value: unknown) => void;
  api.startInventory.mockReturnValue(
    new Promise((resolve) => {
      done = resolve;
    }),
  );
  const { client, wrapper } = setup();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const hook = renderHook(() => useStartStorageInventory(), { wrapper });
  act(() => hook.result.current.mutate());
  await waitFor(() => expect(api.startInventory).toHaveBeenCalledTimes(1));
  act(() => useAuthStore.setState({ sessionId: "next" }));
  await act(async () => {
    done({ job_id: "old" });
  });
  expect(invalidate).not.toHaveBeenCalled();
});
