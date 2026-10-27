/**
 * v0.8.5 · useDashboard 系列 hooks 单测：验证 queryKey 与 queryFn 调用入参。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/dashboard", () => ({
  dashboardApi: {
    getAdminStats: vi.fn(async () => ({ total_users: 1 })),
    getReviewerStats: vi.fn(async () => ({ pending_review_count: 0 })),
    getAnnotatorStats: vi.fn(async () => ({ assigned_tasks: 0 })),
    getMyBatches: vi.fn(async () => []),
    getReviewerBatches: vi.fn(async () => []),
    getMyRecentReviews: vi.fn(async (_limit: number) => []),
    getAdminPeople: vi.fn(async (_p: any) => ({ items: [], total: 0, period: "7d" })),
    getAdminPersonDetail: vi.fn(async (_id: string, _period: string) => ({})),
  },
}));

import { dashboardApi } from "../api/dashboard";
const mockApi = dashboardApi as unknown as Record<string, ReturnType<typeof vi.fn>>;

import {
  useAdminStats,
  useReviewerStats,
  useAnnotatorStats,
  useMyBatches,
  useMyRecentReviews,
  useAdminPeople,
  useAdminPersonDetail,
} from "./useDashboard";

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

describe("useDashboard hooks", () => {
  beforeEach(() => {
    Object.values(mockApi).forEach((fn) => (fn as any).mockClear());
  });

  it("useAdminStats 调 getAdminStats", async () => {
    const { result } = renderHook(() => useAdminStats(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getAdminStats).toHaveBeenCalled();
  });

  it("useReviewerStats 调 getReviewerStats", async () => {
    const { result } = renderHook(() => useReviewerStats(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getReviewerStats).toHaveBeenCalled();
  });

  it("useAnnotatorStats 调 getAnnotatorStats", async () => {
    const { result } = renderHook(() => useAnnotatorStats(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getAnnotatorStats).toHaveBeenCalled();
  });

  it("useMyBatches 调 getMyBatches", async () => {
    const { result } = renderHook(() => useMyBatches(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getMyBatches).toHaveBeenCalled();
  });

  it("loads every annotation batch page before returning a complete array", async () => {
    const first = Array.from({ length: 200 }, (_, index) => ({ batch_id: `batch-${index}` }));
    mockApi.getMyBatches.mockResolvedValueOnce(first).mockResolvedValueOnce([{ batch_id: "last" }]);
    const { result } = renderHook(() => useMyBatches(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(201);
    expect(mockApi.getMyBatches).toHaveBeenNthCalledWith(1, 200, 0, expect.any(AbortSignal));
    expect(mockApi.getMyBatches).toHaveBeenNthCalledWith(2, 200, 200, expect.any(AbortSignal));
  });

  it("fills reviewer batches beyond the dashboard snapshot without changing the stats shape", async () => {
    const first = Array.from({ length: 200 }, (_, index) => ({ batch_id: `review-${index}` }));
    mockApi.getReviewerBatches
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce([{ batch_id: "last" }]);
    const { result } = renderHook(() => useReviewerStats(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.pending_review_count).toBe(0);
    expect(result.current.data?.reviewing_batches).toHaveLength(201);
    expect(mockApi.getReviewerBatches).toHaveBeenNthCalledWith(
      2,
      200,
      200,
      expect.any(AbortSignal),
    );
  });

  it("useMyRecentReviews 默认 limit=20", async () => {
    const { result } = renderHook(() => useMyRecentReviews(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getMyRecentReviews).toHaveBeenCalledWith(20);
  });

  it("useMyRecentReviews 自定义 limit 透传", async () => {
    const { result } = renderHook(() => useMyRecentReviews(50), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getMyRecentReviews).toHaveBeenCalledWith(50);
  });

  it("useAdminPeople 透传 params", async () => {
    const params = { role: "annotator", period: "7d" };
    const { result } = renderHook(() => useAdminPeople(params), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getAdminPeople).toHaveBeenCalledWith(params, expect.any(AbortSignal));
  });

  it("useAdminPersonDetail userId=null → enabled=false 不触发请求", async () => {
    const { result } = renderHook(() => useAdminPersonDetail(null), {
      wrapper: makeWrapper(),
    });
    // enabled=false 时不会自动 fetch，short wait 后 isFetched 仍 false
    await new Promise((r) => setTimeout(r, 30));
    expect(mockApi.getAdminPersonDetail).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("useAdminPersonDetail 提供 userId → 调 API 含 period 默认值", async () => {
    const { result } = renderHook(() => useAdminPersonDetail("u1"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // v0.12.6 (A3)：useAdminPersonDetail 签名扩为 (userId, period, project)，
    // project 默认 undefined 仍会透传到第三参。
    expect(mockApi.getAdminPersonDetail).toHaveBeenCalledWith(
      "u1",
      "4w",
      undefined,
      expect.any(AbortSignal),
    );
  });

  it("useAdminPersonDetail 自定义 period 透传", async () => {
    const { result } = renderHook(() => useAdminPersonDetail("u2", "30d"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.getAdminPersonDetail).toHaveBeenCalledWith(
      "u2",
      "30d",
      undefined,
      expect.any(AbortSignal),
    );
  });
});
