import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn().mockResolvedValue({});
const post = vi.fn().mockResolvedValue({});
vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));
import { comparisonIdentity, trainedModelsApi, type TrainedModelOut } from "./trainedModels";

beforeEach(() => vi.clearAllMocks());

describe("trained model task contract", () => {
  it("filters model lists by task and keeps legacy release requests on detection", () => {
    trainedModelsApi.list("project", 20, undefined, "exclude", { task: "segment" });
    expect(get).toHaveBeenLastCalledWith(
      "/projects/project/trained-models?limit=20&offset=20&archive=exclude&task=segment",
      undefined,
    );
    trainedModelsApi.releases("project");
    expect(get).toHaveBeenLastCalledWith(
      "/projects/project/model-releases?limit=20&offset=0&task=detect",
    );
    trainedModelsApi.releases("project", 40, "segment");
    expect(get).toHaveBeenLastCalledWith(
      "/projects/project/model-releases?limit=20&offset=40&task=segment",
    );
  });

  it("reads trial masks through the owning project, model and deployment", () => {
    trainedModelsApi.trialMaskContent("project", "model", "deployment", 2);
    expect(get).toHaveBeenCalledWith(
      "/projects/project/trained-models/model/deployments/deployment/mask-content/2",
      undefined,
    );
  });

  it("publishes to the explicitly selected task", () => {
    const body = { request_id: "request", task: "segment" as const, expected_generation: 3 };
    trainedModelsApi.publish("project", body);
    expect(post).toHaveBeenCalledWith("/projects/project/model-releases", body);
  });

  it("does not compare different tasks with matching native evaluation fields", () => {
    const model = {
      framework: "ultralytics",
      framework_version: "8",
      binding_digest: "same",
      classes: ["car"],
      provenance: { validation_digest: "same" },
      evaluation: { validation_digest: "same", evaluation: { split: "val" }, per_class: [] },
    } as unknown as TrainedModelOut;
    expect(comparisonIdentity(model)).not.toBe(comparisonIdentity({ ...model, task: "segment" }));
  });
});
