import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "./client";
import { adminPreannotateApi, type PreannotateQueueItem } from "./adminPreannotate";

vi.mock("./client", () => ({ apiClient: { get: vi.fn() } }));
beforeEach(() => vi.mocked(apiClient.get).mockReset());

describe("project preannotation history", () => {
  it("reads every page for the selected project with request cancellation", async () => {
    const first = Array.from(
      { length: 200 },
      (_, index) => ({ batch_id: `batch-${index}` }) as PreannotateQueueItem,
    );
    const last = { batch_id: "last" } as PreannotateQueueItem;
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ items: first })
      .mockResolvedValueOnce({ items: [last] });
    const controller = new AbortController();
    const response = await adminPreannotateApi.projectQueue("project-one", controller.signal);
    expect(response.items).toHaveLength(201);
    expect(response.items[response.items.length - 1]).toEqual(last);
    expect(apiClient.get).toHaveBeenNthCalledWith(
      1,
      "/admin/preannotate-queue?limit=200&project_id=project-one",
      { signal: controller.signal },
    );
    expect(apiClient.get).toHaveBeenNthCalledWith(
      2,
      "/admin/preannotate-queue?limit=200&project_id=project-one&offset=200",
      { signal: controller.signal },
    );
  });
  it("does not mistake a failed continuation for a complete collection", async () => {
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ items: Array.from({ length: 200 }, () => ({})) })
      .mockRejectedValueOnce(new Error("continuation failed"));
    await expect(adminPreannotateApi.projectQueue("project-one")).rejects.toThrow(
      "continuation failed",
    );
  });
});
