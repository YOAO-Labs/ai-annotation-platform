import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn((..._args: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    post: (...args: unknown[]) => post(...args),
  },
}));

import {
  canonicalDataManagerTaskIds,
  dataManagerTaskActionsApi,
  MAX_DATA_MANAGER_TASK_IDS,
} from "./dataManagerActions";

beforeEach(() => post.mockClear());

describe("dataManagerTaskActionsApi", () => {
  it("canonicalizes and caps explicit task selection", () => {
    expect(canonicalDataManagerTaskIds(["task-2", "task-1", "task-2"])).toEqual([
      "task-2",
      "task-1",
    ]);
    expect(() =>
      canonicalDataManagerTaskIds(
        Array.from({ length: MAX_DATA_MANAGER_TASK_IDS + 1 }, (_, i) => `task-${i}`),
      ),
    ).toThrow("Select at most 200 tasks");
  });

  it("sends scoped export with a durable retry key", () => {
    dataManagerTaskActionsApi.exportTasks(
      "project-1",
      { task_ids: ["task-1"], targets: ["coco"] },
      { idempotencyKey: "data-manager-export-1" },
    );
    expect(post).toHaveBeenCalledWith(
      "/projects/project-1/data-manager/tasks/export",
      { task_ids: ["task-1"], targets: ["coco"] },
      { headers: { "Idempotency-Key": "data-manager-export-1" } },
    );
  });

  it("keeps preannotation scope on the existing project endpoint", () => {
    dataManagerTaskActionsApi.preannotate(
      "project-1",
      { ml_backend_id: "backend-1", task_ids: ["task-1"] },
      { idempotencyKey: "data-manager-preannotate-1" },
    );
    expect(post).toHaveBeenCalledWith(
      "/projects/project-1/preannotate",
      { ml_backend_id: "backend-1", task_ids: ["task-1"] },
      { headers: { "Idempotency-Key": "data-manager-preannotate-1" } },
    );
  });
});
