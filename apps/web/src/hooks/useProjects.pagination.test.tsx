import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const page = vi.fn();
const list = vi.fn();
const remove = vi.fn();
vi.mock("@/api/projects", () => ({
  projectsApi: {
    page: (...args: unknown[]) => page(...args),
    list: (...args: unknown[]) => list(...args),
    remove: (...args: unknown[]) => remove(...args),
  },
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ user: { id: "u1" }, token: "test-token" }),
}));

import { useDeleteProject, useProjectPage, useProjects } from "./useProjects";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const response = { items: [], total: 21, page: 2, page_size: 20, pages: 2 };

beforeEach(() => {
  page.mockReset();
  list.mockReset();
  remove.mockReset().mockResolvedValue(undefined);
});

describe("project page query ownership", () => {
  it("cancels the old page and ignores its delayed response", async () => {
    let resolveFirst!: (value: unknown) => void;
    page.mockImplementation((params) =>
      params.page === 1
        ? new Promise((resolve) => {
            resolveFirst = resolve;
          })
        : Promise.resolve(response),
    );
    const hook = renderHook(({ number }) => useProjectPage({ page: number, page_size: 20 }), {
      initialProps: { number: 1 },
      wrapper,
    });
    await waitFor(() => expect(page).toHaveBeenCalledOnce());
    const signal = page.mock.calls[0][1].signal as AbortSignal;
    hook.rerender({ number: 2 });
    await waitFor(() => expect(hook.result.current.data).toEqual(response));
    expect(signal.aborted).toBe(true);
    await act(async () => {
      resolveFirst({ ...response, page: 1 });
    });
    expect(hook.result.current.data?.page).toBe(2);
  });

  it("keeps legacy arrays separate and invalidates both shapes after a project mutation", async () => {
    page.mockResolvedValue(response);
    list.mockResolvedValue([{ id: "p1" }]);
    const hook = renderHook(
      () => ({
        legacy: useProjects(),
        paged: useProjectPage({ page: 2, page_size: 20 }),
        deletion: useDeleteProject(),
      }),
      { wrapper },
    );
    await waitFor(() => expect(hook.result.current.paged.isSuccess).toBe(true));
    expect(hook.result.current.legacy.data).toEqual([{ id: "p1" }]);
    expect(hook.result.current.paged.data).toEqual(response);
    await act(async () => {
      await hook.result.current.deletion.mutateAsync("p1");
    });
    await waitFor(() => expect(page).toHaveBeenCalledTimes(2));
    expect(list).toHaveBeenCalledTimes(2);
  });
});
