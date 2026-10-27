import { beforeEach, expect, it, vi } from "vitest";
const client = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => client.get(...args),
    post: (...args: unknown[]) => client.post(...args),
    put: (...args: unknown[]) => client.put(...args),
    delete: (...args: unknown[]) => client.delete(...args),
  },
}));
import { learningPoliciesApi } from "./learningPolicies";
beforeEach(() => vi.clearAllMocks());

it("keeps default detection policy requests unchanged", () => {
  learningPoliciesApi.get("p");
  learningPoliciesApi.disable("p", 3);
  learningPoliciesApi.check("p");
  learningPoliciesApi.triggers("p", 20);
  learningPoliciesApi.remove("p");
  expect(client.get.mock.calls).toEqual([
    ["/projects/p/learning-policy"],
    ["/projects/p/learning-policy/triggers?limit=20&offset=20"],
  ]);
  expect(client.post.mock.calls).toEqual([
    ["/projects/p/learning-policy/disable?expected_revision=3"],
    ["/projects/p/learning-policy/check"],
  ]);
  expect(client.delete).toHaveBeenCalledWith("/projects/p/learning-policy");
});

it("targets segmentation policy reads, mutations and history explicitly", () => {
  learningPoliciesApi.get("p", "segment");
  learningPoliciesApi.disable("p", 4, "segment");
  learningPoliciesApi.check("p", "segment");
  learningPoliciesApi.triggers("p", 40, "segment");
  learningPoliciesApi.remove("p", "segment");
  expect(client.get.mock.calls).toEqual([
    ["/projects/p/learning-policy?task=segment"],
    ["/projects/p/learning-policy/triggers?limit=20&offset=40&task=segment"],
  ]);
  expect(client.post.mock.calls).toEqual([
    ["/projects/p/learning-policy/disable?expected_revision=4&task=segment"],
    ["/projects/p/learning-policy/check?task=segment"],
  ]);
  expect(client.delete).toHaveBeenCalledWith("/projects/p/learning-policy?task=segment");
  const body = {
    task: "segment" as const,
    expected_revision: 0,
    parent_id: "data",
    base_model: "yolo11n-seg",
    configuration: { schema_id: "schema", schema_digest: "digest", values: {} },
    min_changed_samples: 10,
    check_interval_minutes: 5,
    cooldown_minutes: 60,
  };
  learningPoliciesApi.save("p", body);
  expect(client.put).toHaveBeenCalledWith("/projects/p/learning-policy", body);
});
