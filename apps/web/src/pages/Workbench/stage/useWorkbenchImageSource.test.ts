import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { tasksApi } from "@/api/tasks";
import { ApiError } from "@/api/client";
import type { TaskResponse } from "@/types";
import type { ImagePyramidManifestV1, ImagePyramidResponse } from "./imagePyramid";
import { resolveWorkbenchImageSource, useWorkbenchImageSource } from "./useWorkbenchImageSource";

const manifest: ImagePyramidManifestV1 = {
  schema: "aap-image-pyramid/v1",
  generation: 2,
  sourceFingerprint: "sha256:source",
  normalizationVersion: "exif-autorotate-srgb-v1",
  width: 8192,
  height: 8192,
  tileSize: 512,
  overlap: 1,
  format: "webp",
  levels: Array.from({ length: 14 }, (_, level) => {
    const scaleFactor = 2 ** level;
    const width = Math.ceil(8192 / scaleFactor);
    const height = Math.ceil(8192 / scaleFactor);
    return {
      level,
      scaleFactor,
      width,
      height,
      columns: Math.ceil(width / 512),
      rows: Math.ceil(height / 512),
    };
  }),
  overview: { width: 512, height: 512, contentDigest: "sha256:overview" },
};

function task(
  required: boolean,
  status: "pending" | "building" | "ready" | "failed",
): TaskResponse {
  return {
    id: "task-1",
    dataset_item_id: "item-1",
    file_type: "image",
    file_url: "/original.png",
    thumbnail_url: "/thumb.webp",
    blurhash: "hash",
    image_width: 8192,
    image_height: 8192,
    image_pyramid: {
      status,
      generation: 2,
      width: 8192,
      height: 8192,
      tile_size: 512,
      format: "webp",
      required,
    },
  } as TaskResponse;
}

const ready: ImagePyramidResponse = {
  task_id: "task-1",
  status: "ready",
  required: true,
  retryable: false,
  retry_after_ms: null,
  generation: 2,
  building_generation: null,
  building_status: null,
  error_code: null,
  manifest,
  overview: { url: "/overview.webp", expires_at: "2026-07-31T00:00:00Z" },
};

describe("resolveWorkbenchImageSource", () => {
  afterEach(() => vi.restoreAllMocks());

  it("selects an immutable pyramid without exposing the original to the stage", () => {
    expect(
      resolveWorkbenchImageSource(task(true, "ready"), ready, { enabled: true }),
    ).toMatchObject({
      kind: "pyramid",
      taskId: "task-1",
      generation: 2,
      overviewUrl: "/overview.webp",
    });
  });

  it("never auto-selects the original for a required pending or gate-disabled image", () => {
    expect(
      resolveWorkbenchImageSource(task(true, "building"), undefined, { enabled: true }),
    ).toMatchObject({ kind: "pyramid-pending", taskId: "task-1" });
    expect(
      resolveWorkbenchImageSource(task(true, "ready"), undefined, { enabled: false }),
    ).toMatchObject({ kind: "pyramid-failed", errorCode: "client_gate_disabled" });
  });

  it("keeps the existing single-image path for an optional pending pyramid", () => {
    const optional = task(false, "pending");
    optional.image_width = 4096;
    optional.image_height = 4096;
    optional.image_pyramid = {
      ...optional.image_pyramid!,
      width: 4096,
      height: 4096,
    };
    expect(resolveWorkbenchImageSource(optional, undefined, { enabled: true })).toMatchObject({
      kind: "single",
      url: "/original.png",
    });
  });

  it("blocks the original while a required pyramid has no generation yet", () => {
    const missing = task(true, "failed");
    missing.image_pyramid = {
      ...missing.image_pyramid!,
      status: "missing",
      generation: null,
      retryable: true,
    };
    expect(resolveWorkbenchImageSource(missing, undefined, { enabled: true })).toMatchObject({
      kind: "pyramid-failed",
      identity: "item-1/pyramid/missing",
      retryable: true,
    });
    expect(resolveWorkbenchImageSource(missing, undefined, { enabled: true })).not.toHaveProperty(
      "url",
    );
  });

  it("does not fall back to an optional original above the decoded-byte budget", () => {
    expect(
      resolveWorkbenchImageSource(task(false, "pending"), undefined, { enabled: true }),
    ).toMatchObject({ kind: "pyramid-pending" });
    expect(
      resolveWorkbenchImageSource(task(false, "failed"), undefined, { enabled: false }),
    ).toMatchObject({
      kind: "pyramid-failed",
      errorCode: "single_decode_budget_exceeded",
    });
  });

  it("treats a fresh pending response as authoritative after retry", () => {
    expect(
      resolveWorkbenchImageSource(
        task(true, "failed"),
        { ...ready, status: "pending", manifest: null, overview: null },
        { enabled: true },
      ),
    ).toMatchObject({ kind: "pyramid-pending", taskId: "task-1" });
  });

  it.each(["source", "profile", "dimensions"] as const)(
    "rejects a cached response after the %s changes",
    (change) => {
      const changed = task(true, "failed");
      changed.file_url = "/new-large.png";
      changed.image_pyramid = {
        ...changed.image_pyramid!,
        source_snapshot: "b".repeat(64),
        profile_version: "profile-b",
      };
      const stale: ImagePyramidResponse = {
        ...ready,
        status: change === "dimensions" ? "not_available" : "ready",
        manifest: change === "dimensions" ? null : ready.manifest,
        required: change !== "dimensions",
        source_snapshot: change === "source" ? "a".repeat(64) : "b".repeat(64),
        profile_version: change === "profile" ? "profile-a" : "profile-b",
        width: change === "dimensions" ? 32 : 8192,
        height: change === "dimensions" ? 24 : 8192,
      };
      const source = resolveWorkbenchImageSource(changed, stale, { enabled: true });
      expect(source).toMatchObject({
        kind: "pyramid-failed",
        retryable: false,
        errorCode: "source_changed",
      });
      expect(source).not.toHaveProperty("url");
    },
  );

  it.each(["source", "profile"] as const)(
    "discards cached pixels when the same Task changes %s",
    async (change) => {
      const initial = task(true, "ready");
      initial.image_pyramid = {
        ...initial.image_pyramid!,
        source_snapshot: "a".repeat(64),
        profile_version: "profile-a",
      };
      const first = { ...ready, source_snapshot: "a".repeat(64), profile_version: "profile-a" };
      let finish!: (value: ImagePyramidResponse) => void;
      const get = vi
        .spyOn(tasksApi, "getImagePyramid")
        .mockResolvedValueOnce(first)
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              finish = resolve;
            }),
        );
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const wrapper = ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children);
      const { result, rerender, unmount } = renderHook(
        ({ current }) => useWorkbenchImageSource(current),
        {
          initialProps: { current: initial },
          wrapper,
        },
      );
      await waitFor(() => expect(result.current.source?.kind).toBe("pyramid"));
      const changed = {
        ...initial,
        image_pyramid: {
          ...initial.image_pyramid!,
          source_snapshot: change === "source" ? "b".repeat(64) : "a".repeat(64),
          profile_version: change === "profile" ? "profile-b" : "profile-a",
        },
      };
      rerender({ current: changed });
      await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
      expect(result.current.source?.kind).toBe("pyramid-pending");
      await act(async () => {
        finish({
          ...ready,
          source_snapshot: changed.image_pyramid.source_snapshot,
          profile_version: changed.image_pyramid.profile_version,
        });
      });
      await waitFor(() => expect(result.current.source?.kind).toBe("pyramid"));
      expect(result.current.source?.identity).toContain(changed.image_pyramid.source_snapshot);
      expect(result.current.source?.identity).toContain(changed.image_pyramid.profile_version);
      unmount();
      client.clear();
    },
  );

  it.each(["small", "failed"] as const)(
    "polls metadata until %s without needing a Task refetch, then stops",
    async (outcome) => {
      const unknown = task(false, "pending");
      unknown.image_width = null;
      unknown.image_height = null;
      unknown.image_pyramid = {
        ...unknown.image_pyramid!,
        status: "not_available",
        generation: null,
        width: null,
        height: null,
        retryable: false,
      };
      const pending: ImagePyramidResponse = {
        ...ready,
        status: "pending",
        required: false,
        generation: null,
        width: null,
        height: null,
        manifest: null,
        overview: null,
      };
      const get = vi
        .spyOn(tasksApi, "getImagePyramid")
        .mockResolvedValueOnce(pending)
        .mockResolvedValue({
          ...pending,
          status: outcome === "small" ? "not_available" : "failed",
          width: outcome === "small" ? 24 : null,
          height: outcome === "small" ? 16 : null,
          error_code: outcome === "small" ? null : "decode_failed",
        });
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const wrapper = ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children);
      const { result, unmount } = renderHook(() => useWorkbenchImageSource(unknown), { wrapper });
      await waitFor(() => expect(result.current.source?.kind).toBe("pyramid-pending"));
      await waitFor(
        () =>
          expect(result.current.source).toMatchObject(
            outcome === "small"
              ? { kind: "single", width: 24, height: 16, url: "/original.png" }
              : { kind: "pyramid-failed", retryable: false, errorCode: "decode_failed" },
          ),
        { timeout: 3_000 },
      );
      expect(result.current.retry).toBeUndefined();
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 1_100));
      });
      expect(get).toHaveBeenCalledTimes(2);
      unmount();
      client.clear();
    },
  );

  it("refreshes a Task whose original URL was unavailable before metadata completed", async () => {
    const unknown = task(false, "pending");
    unknown.file_url = null;
    unknown.image_width = null;
    unknown.image_height = null;
    unknown.image_pyramid = {
      ...unknown.image_pyramid!,
      status: "not_available",
      generation: null,
      width: null,
      height: null,
    };
    const resolved = {
      ...unknown,
      file_url: "/resolved.png",
      image_width: 32,
      image_height: 24,
      image_pyramid: { ...unknown.image_pyramid, width: 32, height: 24 },
    };
    vi.spyOn(tasksApi, "getImagePyramid").mockResolvedValue({
      ...ready,
      status: "not_available",
      required: false,
      generation: null,
      width: 32,
      height: 24,
      manifest: null,
      overview: null,
    });
    const getTask = vi.fn().mockResolvedValue(resolved);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(
      () => {
        const current = useQuery({
          queryKey: ["task", unknown.id],
          queryFn: getTask,
          initialData: unknown,
          staleTime: Infinity,
        });
        return useWorkbenchImageSource(current.data);
      },
      { wrapper },
    );
    await waitFor(() =>
      expect(result.current.source).toMatchObject({
        kind: "single",
        url: "/resolved.png",
        width: 32,
        height: 24,
      }),
    );
    expect(getTask).toHaveBeenCalledTimes(1);
    unmount();
    client.clear();
  });

  it("refreshes signed overview URLs through a read without rebuilding the pyramid", async () => {
    const get = vi
      .spyOn(tasksApi, "getImagePyramid")
      .mockResolvedValueOnce(ready)
      .mockResolvedValue({
        ...ready,
        overview: { url: "/fresh-overview.webp", expires_at: "2026-09-15T00:00:00Z" },
      });
    const rebuild = vi.spyOn(tasksApi, "retryImagePyramid");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(() => useWorkbenchImageSource(task(true, "ready")), {
      wrapper,
    });
    await waitFor(() =>
      expect(result.current.source).toMatchObject({ overviewUrl: "/overview.webp" }),
    );
    await act(async () => {
      await result.current.refresh();
    });
    await waitFor(() =>
      expect(result.current.source).toMatchObject({ overviewUrl: "/fresh-overview.webp" }),
    );
    expect(get).toHaveBeenCalledTimes(2);
    expect(rebuild).not.toHaveBeenCalled();
    unmount();
    client.clear();
  });

  it("recovers when another worker finishes before a retry click", async () => {
    const get = vi
      .spyOn(tasksApi, "getImagePyramid")
      .mockResolvedValueOnce({
        ...ready,
        status: "failed",
        retryable: true,
        manifest: null,
        overview: null,
      })
      .mockResolvedValue(ready);
    vi.spyOn(tasksApi, "retryImagePyramid").mockRejectedValue(
      new ApiError(409, "already ready", { reason: "already_ready" }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(() => useWorkbenchImageSource(task(true, "failed")), {
      wrapper,
    });
    await waitFor(() => expect(result.current.retry).toBeDefined());
    await act(async () => {
      await result.current.retry?.();
    });
    await waitFor(() => expect(result.current.source?.kind).toBe("pyramid"));
    expect(result.current.retry).toBeUndefined();
    expect(get).toHaveBeenCalledTimes(2);
    unmount();
    client.clear();
  });

  it("optimistically enters pending and polls after an explicit retry", async () => {
    const failedResponse: ImagePyramidResponse = {
      ...ready,
      status: "failed",
      retryable: true,
      error_code: "generation_failed",
      manifest: null,
      overview: null,
    };
    vi.spyOn(tasksApi, "getImagePyramid").mockResolvedValue(failedResponse);
    const retry = vi
      .spyOn(tasksApi, "retryImagePyramid")
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValue({ task_id: "task-1", status: "queued", celery_task_id: "job-1" });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    const { result } = renderHook(() => useWorkbenchImageSource(task(true, "failed"), "item-1"), {
      wrapper,
    });
    await waitFor(() => expect(result.current.source?.kind).toBe("pyramid-failed"));

    await act(async () => {
      await expect(result.current.retry?.()).rejects.toThrow("response lost");
    });
    await act(async () => {
      await result.current.retry?.();
    });

    const key = retry.mock.calls[0][1]?.idempotencyKey;
    expect(key).toMatch(/^[0-9a-f-]{36}$/);
    expect(retry).toHaveBeenNthCalledWith(2, "task-1", { idempotencyKey: key });
    expect(result.current.source).toMatchObject({ kind: "pyramid-pending" });
    queryClient.clear();
  });
});

describe("authenticated single selection", () => {
  afterEach(() => vi.restoreAllMocks());
  it("keeps required and ready pyramids off the complete original loader", async () => {
    const original = await import("@/hooks/useOriginalImage");
    const image = vi
      .spyOn(original, "useOriginalImage")
      .mockReturnValue({ url: null, version: 0, error: null, ready: false, retry: vi.fn() });
    vi.spyOn(tasksApi, "getImagePyramid").mockResolvedValue(ready);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, unmount } = renderHook(() => useWorkbenchImageSource(task(true, "ready")), {
      wrapper,
    });
    await waitFor(() => expect(result.current.source?.kind).toBe("pyramid"));
    expect(image.mock.calls.every(([resource]) => resource === null)).toBe(true);
    unmount();
    client.clear();
  });
  it("uses owned addresses and their actual versions without signed original fallback", async () => {
    const original = await import("@/hooks/useOriginalImage");
    const image = vi.spyOn(original, "useOriginalImage").mockReturnValue({
      url: "blob:owned-one",
      version: 1,
      error: null,
      ready: true,
      retry: vi.fn(),
    });
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const single = {
      ...task(false, "pending"),
      image_pyramid: null,
      file_url: "https://old-signed-original.invalid/trap",
    };
    const { result, rerender, unmount } = renderHook(() => useWorkbenchImageSource(single), {
      wrapper,
    });
    expect(result.current.source).toMatchObject({ kind: "single", url: "blob:owned-one" });
    const firstIdentity = result.current.source?.identity;
    image.mockReturnValue({
      url: "blob:owned-two",
      version: 2,
      error: null,
      ready: true,
      retry: vi.fn(),
    });
    rerender();
    expect(result.current.source?.identity).not.toBe(firstIdentity);
    image.mockReturnValue({
      url: null,
      version: 0,
      error: "来源已变化",
      ready: false,
      retry: vi.fn(),
    });
    rerender();
    expect(result.current.source).toBeNull();
    expect(result.current.contentBlockedReason).toBe("来源已变化");
    unmount();
    client.clear();
  });
});

describe("session-scoped task retention", () => {
  afterEach(async () => {
    vi.restoreAllMocks();
    const { useAuthStore } = await import("@/stores/authStore");
    useAuthStore.getState().logout();
  });
  it("retains only the same requested Task across a refresh gap and rejects its old object in a new login", async () => {
    const { useAuthStore } = await import("@/stores/authStore");
    const user = { id: "reader" } as Parameters<
      ReturnType<typeof useAuthStore.getState>["setAuth"]
    >[1];
    useAuthStore.getState().setAuth("first", user);
    const original = await import("@/hooks/useOriginalImage");
    const image = vi
      .spyOn(original, "useOriginalImage")
      .mockReturnValue({ url: "blob:owned", version: 1, error: null, ready: true, retry: vi.fn() });
    const single = { ...task(false, "pending"), image_pyramid: null };
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const { result, rerender, unmount } = renderHook(
      ({ incoming, requested }: { incoming: TaskResponse | undefined; requested: string }) =>
        useWorkbenchImageSource(incoming, null, requested),
      {
        initialProps: { incoming: single as TaskResponse | undefined, requested: single.id },
        wrapper,
      },
    );
    expect(result.current.source?.kind).toBe("single");
    act(() => useAuthStore.getState().setToken("refresh"));
    rerender({ incoming: undefined, requested: single.id });
    expect(result.current.source?.kind).toBe("single");
    expect(image.mock.lastCall?.[0]).toEqual({ kind: "task", taskId: single.id });
    rerender({ incoming: undefined, requested: "different-task" });
    expect(result.current.source).toBeNull();
    rerender({ incoming: single, requested: single.id });
    act(() => useAuthStore.getState().setAuth("new-login", user));
    expect(result.current.source).toBeNull();
    expect(image.mock.lastCall?.[0]).toBeNull();
    unmount();
    client.clear();
  });
});
