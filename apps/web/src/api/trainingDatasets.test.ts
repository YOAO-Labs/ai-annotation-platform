import { beforeEach, expect, it, vi } from "vitest";
const get = vi.fn();
const post = vi.fn();
vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));
import { trainingDatasetsApi } from "./trainingDatasets";
beforeEach(() => vi.clearAllMocks());
it("reads a frozen instance mask by stable source annotation identity", () => {
  trainingDatasetsApi.sampleMask("project", "version", "task", "annotation");
  expect(get).toHaveBeenCalledWith(
    "/projects/project/training-datasets/version/insights/samples/task/masks/annotation",
    undefined,
  );
});
it("carries an explicit native mask bridge without changing old default requests", () => {
  const native = { task: "segment" as const, bridge: "ultralytics-instance-rle-1" as const };
  trainingDatasetsApi.preflight("project", native);
  expect(post).toHaveBeenLastCalledWith("/projects/project/training-datasets:preflight", native);
  const legacy = { task: "segment" as const };
  trainingDatasetsApi.preflight("project", legacy);
  expect(post).toHaveBeenLastCalledWith("/projects/project/training-datasets:preflight", legacy);
});
