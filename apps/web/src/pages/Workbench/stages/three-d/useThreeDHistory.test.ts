import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import type { AnnotationPayload } from "@/api/tasks";
import { useAuthStore } from "@/stores/authStore";

import { useThreeDHistory } from "./useThreeDHistory";

beforeEach(() =>
  useAuthStore.getState().setAuth("history-token", { id: "history-test" } as MeResponse),
);
afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
  sessionStorage.removeItem("wb:hist:history-test:t1");
});

function makeMutations() {
  return {
    createAnnotation: {
      mutateAsync: vi.fn(async (payload: AnnotationPayload) => ({
        id: "created-id",
        task_id: "t1",
        project_id: null,
        user_id: null,
        source: "manual",
        annotation_type: payload.annotation_type ?? "box_3d",
        class_name: payload.class_name,
        geometry: payload.geometry,
        confidence: 1,
        parent_prediction_id: null,
        parent_annotation_id: null,
        lead_time: null,
        is_active: true,
        ground_truth: false,
        attributes: payload.attributes ?? {},
        created_at: "2026-06-07T00:00:00Z",
        updated_at: null,
      })),
    },
    deleteAnnotation: {
      mutateAsync: vi.fn(async (_id: string) => undefined),
    },
    updateAnnotation: {
      mutateAsync: vi.fn(async (_input: { annotationId: string }) => undefined),
    },
  };
}

describe("useThreeDHistory", () => {
  it("delegates update undo / redo to the 3D update mutation", async () => {
    const mutations = makeMutations();
    const { result } = renderHook(() => useThreeDHistory("t1", mutations));
    act(() => {
      result.current.push({
        kind: "update",
        annotationId: "a1",
        before: { class_name: "car" },
        after: { class_name: "truck" },
      });
    });

    await act(async () => {
      await result.current.undo();
    });
    await waitFor(() => expect(result.current.canRedo).toBe(true));
    expect(mutations.updateAnnotation.mutateAsync).toHaveBeenCalledWith({
      annotationId: "a1",
      payload: { class_name: "car" },
    });

    await act(async () => {
      await result.current.redo();
    });
    await waitFor(() => expect(result.current.canUndo).toBe(true));
    expect(mutations.updateAnnotation.mutateAsync).toHaveBeenCalledWith({
      annotationId: "a1",
      payload: { class_name: "truck" },
    });
  });

  it("delegates create undo and redo to delete/create mutations", async () => {
    const mutations = makeMutations();
    const payload = {
      annotation_type: "box_3d",
      tool_unit_id: "lidar_box_3d",
      class_name: "car",
      geometry: {
        type: "box_3d" as const,
        center: [0, 0, 0] as [number, number, number],
        size: [1, 1, 1] as [number, number, number],
        rotation: [0, 0, 0] as [number, number, number],
      },
    };
    const { result } = renderHook(() => useThreeDHistory("t1", mutations));
    act(() => {
      result.current.push({ kind: "create", annotationId: "a1", payload });
    });

    await act(async () => {
      await result.current.undo();
    });
    await waitFor(() => expect(result.current.canRedo).toBe(true));
    expect(mutations.deleteAnnotation.mutateAsync).toHaveBeenCalledWith("a1");

    await act(async () => {
      await result.current.redo();
    });
    await waitFor(() => expect(result.current.canUndo).toBe(true));
    expect(mutations.createAnnotation.mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({ ...payload, client_request_id: expect.any(String) }),
    );
  });

  it("records only acknowledged 3D changes and retries a failed batch undo", async () => {
    const mutations = makeMutations();
    mutations.updateAnnotation.mutateAsync
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("locked"))
      .mockResolvedValueOnce(undefined);
    const { result } = renderHook(() => useThreeDHistory("t1", mutations));
    await act(async () =>
      result.current.commit(
        ["a", "b", "c"].map((annotationId) => ({
          kind: "update",
          annotationId,
          before: { class_name: "car" },
          after: { class_name: "truck" },
        })),
      ),
    );
    mutations.updateAnnotation.mutateAsync.mockClear();
    mutations.updateAnnotation.mutateAsync
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("locked"));
    await act(async () => result.current.undo());
    expect(result.current.canUndo).toBe(true);
    expect(result.current.canRedo).toBe(true);
    await act(async () => result.current.undo());
    expect(
      mutations.updateAnnotation.mutateAsync.mock.calls.map(([arg]) => arg.annotationId),
    ).toEqual(["c", "a", "a"]);
  });
});
