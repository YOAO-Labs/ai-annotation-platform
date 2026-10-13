/**
 * issue #124 · useBatchEventsSocket 事件处理单测.
 *
 * 守护: batch.status_changed 与 batch.assignment_changed 都 invalidate
 * ["batches", projectId] / ["projects"]; 其它帧 (ping / 未知类型 / 非法 JSON) 不触发。
 */
import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

let capturedOnMessage: ((e: MessageEvent) => void) | null = null;
let capturedUrl: string | null = null;

vi.mock("@/hooks/useReconnectingWebSocket", () => ({
  useReconnectingWebSocket: (
    url: string | null,
    opts: { onMessage?: (e: MessageEvent) => void },
  ) => {
    capturedUrl = url;
    capturedOnMessage = opts.onMessage ?? null;
    return { state: "open", retries: 0 };
  },
}));

import { useBatchEventsSocket } from "./useBatchEventsSocket";
import { useAuthStore } from "@/stores/authStore";

function makeWrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

function message(data: unknown): MessageEvent {
  return { data: JSON.stringify(data) } as MessageEvent;
}

afterEach(() => {
  capturedOnMessage = null;
  capturedUrl = null;
  cleanup();
});

describe("useBatchEventsSocket", () => {
  it("assignment_changed 与 status_changed 都 invalidate 批次缓存", () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useBatchEventsSocket("p1"), { wrapper: makeWrapper(client) });

    capturedOnMessage?.(message({ type: "batch.assignment_changed", batch_ids: ["b1"] }));
    capturedOnMessage?.(message({ type: "batch.status_changed", batch_id: "b1" }));

    expect(spy).toHaveBeenCalledWith({ queryKey: ["batches", "p1"] });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["projects"] });
    // 每个事件各触发一次 batches + projects
    expect(spy).toHaveBeenCalledTimes(4);
  });

  it("ping / 未知类型 / 非法 JSON 不触发 invalidate", () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useBatchEventsSocket("p1"), { wrapper: makeWrapper(client) });

    capturedOnMessage?.(message({ type: "ping" }));
    capturedOnMessage?.(message({ type: "batch.created" }));
    capturedOnMessage?.({ data: "not-json" } as MessageEvent);

    expect(spy).not.toHaveBeenCalled();
  });

  it("waits for auth hydration before connecting (no unauth fallback)", () => {
    act(() => useAuthStore.setState({ token: null, user: null }));
    renderHook(() => useBatchEventsSocket("p1"), { wrapper: makeWrapper(new QueryClient()) });
    expect(capturedUrl).toBeNull();
  });

  it("carries the account token in the root WS URL", () => {
    act(() => useAuthStore.setState({ token: "tok-1", user: { id: "u1" } as never }));
    renderHook(() => useBatchEventsSocket("p1"), { wrapper: makeWrapper(new QueryClient()) });
    expect(capturedUrl).toContain("/ws/batches/project/p1");
    expect(capturedUrl).toContain("token=tok-1");
  });
});
