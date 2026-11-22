import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnnotationUpdatePayload } from "@/api/tasks";
import type { AnnotationResponse } from "@/types";

const { updateAnnotationMock, deleteAnnotationMock, enqueueDurablyMock, authState } = vi.hoisted(
  () => ({
    updateAnnotationMock: vi.fn(),
    deleteAnnotationMock: vi.fn(),
    enqueueDurablyMock: vi.fn(),
    authState: { user: { id: "user-a" } as { id: string } | null },
  }),
);

vi.mock("../api/tasks", () => ({
  tasksApi: {
    updateAnnotation: updateAnnotationMock,
    deleteAnnotation: deleteAnnotationMock,
  },
}));

vi.mock("../pages/Workbench/state/offlineQueue", () => ({
  enqueueDurably: enqueueDurablyMock,
  isOfflineCandidate: (error: unknown) =>
    error instanceof TypeError ||
    (typeof error === "object" &&
      error !== null &&
      "status" in error &&
      Number(error.status) >= 500),
}));

vi.mock("../stores/authStore", () => ({
  useAuthStore: { getState: () => authState },
  isCurrentAuthOwner: (userId: string) => authState.user?.id === userId,
}));

import { isOfflineMutationQueued, useDeleteAnnotation, useUpdateAnnotation } from "./useTasks";

type Owner = { taskId: string; videoSegmentId?: string | null };
const A: Owner = { taskId: "task-a", videoSegmentId: "segment-a" };
const B: Owner = { taskId: "task-b", videoSegmentId: "segment-b" };
const clients: QueryClient[] = [];
const pending: Promise<unknown>[] = [];

function queryKey(owner: Owner) {
  return owner.videoSegmentId
    ? ["annotations", owner.taskId, owner.videoSegmentId]
    : ["annotations", owner.taskId];
}

function annotation(id: string, owner: Owner): AnnotationResponse {
  return {
    id,
    task_id: owner.taskId,
    video_segment_id: owner.videoSegmentId ?? null,
    project_id: null,
    user_id: null,
    source: "manual",
    annotation_type: "bbox",
    class_name: "car",
    geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
    confidence: 1,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    attributes: { checked: false },
    created_at: "2026-09-09T00:00:00Z",
    updated_at: null,
  };
}

function setupUpdate(owner: Owner = A) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false, gcTime: Infinity },
    },
  });
  clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return {
    client,
    ...renderHook(
      ({ taskId, videoSegmentId }: Owner) =>
        useUpdateAnnotation(taskId, undefined, undefined, videoSegmentId),
      { initialProps: owner, wrapper },
    ),
  };
}

function setupDelete(owner: Owner = A) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity },
      mutations: { retry: false, gcTime: Infinity },
    },
  });
  clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return {
    client,
    ...renderHook(
      ({ taskId, videoSegmentId }: Owner) => useDeleteAnnotation(taskId, videoSegmentId),
      { initialProps: owner, wrapper },
    ),
  };
}

beforeEach(() => {
  authState.user = { id: "user-a" };
  updateAnnotationMock.mockReset();
  deleteAnnotationMock.mockReset();
  enqueueDurablyMock.mockReset().mockResolvedValue(undefined);
  onlineManager.setOnline(true);
});

afterEach(async () => {
  onlineManager.setOnline(true);
  cleanup();
  await Promise.allSettled(pending.splice(0));
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

function track<T>(promise: Promise<T>): Promise<T> {
  pending.push(promise);
  void promise.catch(() => undefined);
  return promise;
}

describe("annotation update mutation offline ownership", () => {
  it("runs while offline, durably restores the update, and keeps task/segment/account ownership", async () => {
    onlineManager.setOnline(false);
    const error = new TypeError("offline");
    updateAnnotationMock.mockRejectedValue(error);
    const { result, client, rerender } = setupUpdate();
    const oldA = annotation("a-1", A);
    const oldB = annotation("b-1", B);
    client.setQueryData(queryKey(A), [oldA]);
    client.setQueryData(queryKey(B), [oldB]);

    const payload: AnnotationUpdatePayload = { attributes: { checked: true } };
    let settledError: unknown;
    let request!: Promise<unknown>;
    act(() => {
      request = result.current.mutateAsync(
        { annotationId: oldA.id, payload },
        { onSettled: (_data, settled) => (settledError = settled) },
      );
    });
    const trackedRequest = track(request);
    authState.user = { id: "user-b" };
    await waitFor(() => expect(enqueueDurablyMock).toHaveBeenCalledTimes(1));
    rerender(B);
    await expect(trackedRequest).rejects.toMatchObject({
      name: "AnnotationMutationOwnerChangedError",
    });

    expect(enqueueDurablyMock).toHaveBeenCalledTimes(1);
    expect(enqueueDurablyMock).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "update",
        taskId: A.taskId,
        videoSegmentId: A.videoSegmentId,
        annotationId: oldA.id,
        payload,
      }),
      { userId: "user-a" },
    );
    expect(updateAnnotationMock).not.toHaveBeenCalled();
    expect(client.getQueryData<AnnotationResponse[]>(queryKey(A))?.[0].attributes).toEqual({
      checked: false,
    });
    expect(client.getQueryData(queryKey(B))).toEqual([oldB]);
    expect(isOfflineMutationQueued(settledError)).toBe(true);

    const fresh = setupUpdate(A);
    expect(enqueueDurablyMock).toHaveBeenCalledTimes(1);
    fresh.unmount();
  });

  it("does not queue business errors and rolls back the optimistic update", async () => {
    onlineManager.setOnline(false);
    const error = Object.assign(new Error("invalid payload"), { status: 422 });
    updateAnnotationMock.mockRejectedValue(error);
    const { result, client } = setupUpdate();
    const current = annotation("a-1", A);
    client.setQueryData(queryKey(A), [current]);
    const request = track(
      result.current.mutateAsync({
        annotationId: current.id,
        payload: { class_name: "invalid" },
      }),
    );
    await expect(request).rejects.toBe(error);
    expect(enqueueDurablyMock).not.toHaveBeenCalled();
    expect(client.getQueryData(queryKey(A))).toEqual([current]);
  });

  it("does not claim success when durable storage rejects", async () => {
    onlineManager.setOnline(false);
    const requestError = new TypeError("offline");
    const storageError = new Error("IndexedDB unavailable");
    updateAnnotationMock.mockRejectedValue(requestError);
    enqueueDurablyMock.mockRejectedValue(storageError);
    const { result, client } = setupUpdate();
    const current = annotation("a-1", A);
    client.setQueryData(queryKey(A), [current]);
    let settledError: unknown;
    const request = track(
      result.current.mutateAsync(
        { annotationId: current.id, payload: { attributes: { checked: true } } },
        { onSettled: (_data, error) => (settledError = error) },
      ),
    );
    await expect(request).rejects.toBe(requestError);
    expect(client.getQueryData(queryKey(A))).toEqual([current]);
    expect(isOfflineMutationQueued(settledError)).toBe(false);
  });

  it("leaves durable ownership to an explicit caller fallback instead of duplicating it", async () => {
    onlineManager.setOnline(false);
    const error = new TypeError("offline");
    updateAnnotationMock.mockRejectedValue(error);
    const { result } = setupUpdate();
    const fallback = vi.fn();
    const request = track(
      result.current.mutateAsync(
        { annotationId: "a-1", payload: { class_name: "truck" } },
        { onError: fallback },
      ),
    );
    await expect(request).rejects.toBe(error);
    expect(enqueueDurablyMock).not.toHaveBeenCalled();
    expect(fallback).toHaveBeenCalledWith(
      error,
      expect.anything(),
      expect.anything(),
      expect.anything(),
    );
  });

  it("leaves a versioned save transaction in control of failure and retry", async () => {
    const error = Object.assign(new Error("service unavailable"), { status: 503 });
    updateAnnotationMock.mockRejectedValueOnce(error);
    const { result, client } = setupUpdate();
    const current = annotation("a-1", A);
    client.setQueryData(queryKey(A), [current]);
    const input = {
      annotationId: current.id,
      payload: { attributes: { checked: true } },
      etag: 'W/"1"',
    };
    const request = track(result.current.mutateAsync(input, { queueOffline: false }));
    await expect(request).rejects.toBe(error);
    expect(updateAnnotationMock).toHaveBeenCalledTimes(1);
    expect(enqueueDurablyMock).not.toHaveBeenCalled();
    expect(isOfflineMutationQueued(error)).toBe(false);
    expect(client.getQueryData(queryKey(A))).toEqual([current]);

    const saved = { ...current, attributes: input.payload.attributes };
    updateAnnotationMock.mockResolvedValueOnce(saved);
    await track(result.current.mutateAsync(input, { queueOffline: false }));
    expect(updateAnnotationMock).toHaveBeenCalledTimes(2);
    expect(updateAnnotationMock).toHaveBeenLastCalledWith(
      A.taskId,
      current.id,
      input.payload,
      input.etag,
    );
    expect(client.getQueryData(queryKey(A))).toEqual([saved]);
  });

  it("preserves the version precondition when history durably queues an update", async () => {
    const error = Object.assign(new Error("service unavailable"), { status: 503 });
    updateAnnotationMock.mockRejectedValue(error);
    const { result } = setupUpdate();
    const input = {
      annotationId: "a-1",
      payload: { attributes: { checked: true } },
      etag: 'W/"7"',
    };
    await expect(track(result.current.mutateAsync(input))).rejects.toBe(error);
    expect(enqueueDurablyMock).toHaveBeenCalledOnce();
    expect(enqueueDurablyMock).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "update",
        annotationId: input.annotationId,
        payload: input.payload,
        etag: input.etag,
      }),
      { userId: "user-a" },
    );
    expect(isOfflineMutationQueued(error)).toBe(true);
  });
});

it("does not overwrite the next account's cache after query cancellation yields", async () => {
  const { result, client } = setupUpdate();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const cancel = vi.spyOn(client, "cancelQueries").mockReturnValueOnce(gate);
  const request = track(
    result.current.mutateAsync({ annotationId: "old", payload: { attributes: { checked: true } } }),
  );
  await waitFor(() => expect(cancel).toHaveBeenCalled());
  authState.user = { id: "user-b" };
  const next = annotation("old", A);
  next.attributes = { checked: "next-account" };
  client.setQueryData(queryKey(A), [next]);
  release();
  await expect(request).rejects.toMatchObject({ name: "AnnotationMutationOwnerChangedError" });
  expect(client.getQueryData(queryKey(A))).toEqual([next]);
  expect(updateAnnotationMock).not.toHaveBeenCalled();
  expect(enqueueDurablyMock).toHaveBeenCalledWith(expect.anything(), { userId: "user-a" });
});

describe("annotation delete mutation offline ownership", () => {
  it("durably accepts an offline delete and restores it for a new owner instance", async () => {
    onlineManager.setOnline(false);
    const error = new TypeError("offline");
    deleteAnnotationMock.mockRejectedValue(error);
    const { result, client } = setupDelete();
    const current = annotation("a-1", A);
    client.setQueryData(queryKey(A), [current]);
    const request = track(result.current.mutateAsync(current.id));
    await expect(request).rejects.toBe(error);
    expect(deleteAnnotationMock).toHaveBeenCalledWith(A.taskId, current.id);
    expect(enqueueDurablyMock).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "delete",
        taskId: A.taskId,
        videoSegmentId: A.videoSegmentId,
        annotationId: current.id,
      }),
      { userId: "user-a" },
    );
    expect(client.getQueryData(queryKey(A))).toEqual([]);
  });
});
