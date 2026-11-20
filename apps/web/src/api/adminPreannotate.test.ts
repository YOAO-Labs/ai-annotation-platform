import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "./client";
import { adminPreannotateApi, type PreannotateQueueItem } from "./adminPreannotate";

vi.mock("./client", () => ({ apiClient: { get: vi.fn() } }));
beforeEach(() => vi.mocked(apiClient.get).mockReset());

const history: PreannotateQueueItem[] = Array.from({ length: 201 }, (_, index) => ({
  batch_id: `batch-${index}`,
  batch_name: `Batch ${index}`,
  batch_status: "pre_annotated",
  project_id: "project-one",
  project_name: "Image review",
  total_tasks: 10,
  prediction_count: 7,
  failed_count: index === 0 ? 2 : 0,
  can_retry: index === 0,
}));

describe("project preannotation history", () => {
  it("keeps every batch in order, including mixed successes and failures across pages", async () => {
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ items: structuredClone(history.slice(0, 200)) })
      .mockResolvedValueOnce({ items: structuredClone(history.slice(200)) });
    const controller = new AbortController();
    const response = await adminPreannotateApi.projectQueue("project-one", controller.signal);
    expect(response.items).toEqual(history);
    expect(vi.mocked(apiClient.get).mock.calls).toEqual([
      ["/admin/preannotate-queue?limit=200&project_id=project-one", { signal: controller.signal }],
      [
        "/admin/preannotate-queue?limit=200&project_id=project-one&offset=200",
        { signal: controller.signal },
      ],
    ]);
  });
  it("does not mistake a failed continuation for a complete collection", async () => {
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ items: history.slice(0, 200) })
      .mockRejectedValueOnce(new Error("continuation failed"));
    await expect(adminPreannotateApi.projectQueue("project-one")).rejects.toThrow(
      "continuation failed",
    );
  });
});
