import { beforeEach, expect, it, vi } from "vitest";
const get = vi.fn();
const post = vi.fn();
vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));
import { activeLearningApi } from "./activeLearning";
beforeEach(() => vi.clearAllMocks());
it("preserves the unfiltered list and explicitly filters detection and segmentation rounds", () => {
  activeLearningApi.list("p", 20);
  activeLearningApi.list("p", 0, undefined, "detect");
  activeLearningApi.list("p", 40, undefined, "segment");
  expect(get.mock.calls).toEqual([
    ["/projects/p/active-learning-rounds?limit=20&offset=20", undefined],
    ["/projects/p/active-learning-rounds?limit=20&offset=0&task=detect", undefined],
    ["/projects/p/active-learning-rounds?limit=20&offset=40&task=segment", undefined],
  ]);
});
it("lets the fixed model define selection task without adding task to the strict input", () => {
  const input = {
    model_version_id: "segment-model",
    deployment_id: "ready",
    sample_task_ids: ["a", "b"],
    seed: 42,
    batch_size: 1,
    max_inference_dispatches: 2,
    max_review_tasks: 2,
    confidence: 0.1,
    nms_iou: 0.7,
  };
  activeLearningApi.preview("p", input);
  expect(post).toHaveBeenCalledWith("/projects/p/active-learning-rounds:preview", input);
  expect(post.mock.calls[0][1]).not.toHaveProperty("task");
});
