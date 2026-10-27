import { afterEach, describe, expect, it, vi } from "vitest";
import type { TrainingDatasetCreate } from "@/api/trainingDatasets";
import { instanceMaskBridge } from "./trainingDatasetBridges";
import {
  readTrainingDatasetDraft,
  removeMatchingTrainingDatasetDraft,
  saveTrainingDatasetDraft,
  trainingDatasetDraftKey,
} from "./trainingDatasetDraftStorage";

const owner = {
  userId: "user-a",
  sessionId: "session-a",
  projectId: "project-a",
  task: "detect" as const,
};
const body: TrainingDatasetCreate = {
  name: "冻结首轮",
  request_id: "11111111-1111-4111-8111-111111111111",
  preflight_digest: "a".repeat(64),
  task: "detect",
  batch_id: null,
  parent_id: null,
  mode: "reviewed",
  seed: 42,
  validation_fraction: 0.2,
  task_ids: [],
  confirmed_negative_ids: [],
};

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("training dataset pending request storage", () => {
  it("retains an explicit dataset restriction in an idempotent pending request", () => {
    const scoped = { ...body, dataset_id: "11111111-2222-4333-8444-555555555555" };
    expect(saveTrainingDatasetDraft(owner, scoped)).toBeNull();
    expect(readTrainingDatasetDraft(owner)).toEqual({ body: scoped, error: null });
    expect(saveTrainingDatasetDraft(owner, { ...scoped, dataset_id: null })).toContain(
      "另一个页面",
    );
  });
  it("keeps the original body, rejects another pending request and only removes a matching request", () => {
    expect(saveTrainingDatasetDraft(owner, body)).toBeNull();
    expect(readTrainingDatasetDraft(owner)).toEqual({ body, error: null });
    const other = { ...body, request_id: "22222222-2222-4222-8222-222222222222" };
    expect(saveTrainingDatasetDraft(owner, other)).toContain("另一个页面");
    expect(saveTrainingDatasetDraft(owner, { ...body, name: "changed" })).toContain("另一个页面");
    expect(removeMatchingTrainingDatasetDraft(owner, other)).toBeNull();
    expect(readTrainingDatasetDraft(owner).body).toEqual(body);
    expect(removeMatchingTrainingDatasetDraft(owner, body)).toBeNull();
    expect(readTrainingDatasetDraft(owner)).toEqual({ body: null, error: null });
  });

  it.each([
    { userId: "user-b" },
    { sessionId: "session-b" },
    { projectId: "project-b" },
    { task: "segment" as const },
  ])("isolates a changed owner %j", (changed) => {
    saveTrainingDatasetDraft(owner, body);
    expect(readTrainingDatasetDraft({ ...owner, ...changed })).toEqual({ body: null, error: null });
  });

  it.each([
    "not-json",
    "",
    "null",
    "[]",
    JSON.stringify({ version: 1, ownerKey: "another owner", body }),
    JSON.stringify({
      version: 1,
      ownerKey: trainingDatasetDraftKey(owner),
      body: { ...body, task: "segment" },
    }),
    JSON.stringify({
      version: 1,
      ownerKey: trainingDatasetDraftKey(owner),
      body: { ...body, request_id: "invalid" },
    }),
    JSON.stringify({
      version: 1,
      ownerKey: trainingDatasetDraftKey(owner),
      body: { ...body, seed: -1 },
    }),
    JSON.stringify({
      version: 1,
      ownerKey: trainingDatasetDraftKey(owner),
      body: { ...body, task_ids: [null] },
    }),
    JSON.stringify({
      version: 1,
      ownerKey: trainingDatasetDraftKey(owner),
      body: { ...body, extra: true },
    }),
    " ".repeat(2 * 1024 * 1024 + 1),
  ])(
    "preserves malformed storage instead of silently replacing or submitting it (%#)",
    (source) => {
      const key = trainingDatasetDraftKey(owner);
      localStorage.setItem(key, source);
      expect(readTrainingDatasetDraft(owner).error).toContain("无法恢复");
      expect(saveTrainingDatasetDraft(owner, body)).toContain("无法恢复");
      expect(removeMatchingTrainingDatasetDraft(owner, body)).toContain("无法恢复");
      expect(localStorage.getItem(key)).toBe(source);
    },
  );

  it("reports unavailable storage and quota failures without claiming the request was saved", () => {
    expect(
      readTrainingDatasetDraft(owner, {
        getItem() {
          throw new Error("blocked");
        },
      }).error,
    ).toContain("无法恢复");
    expect(
      saveTrainingDatasetDraft(owner, body, {
        getItem: () => null,
        setItem() {
          throw new DOMException("Quota exceeded", "QuotaExceededError");
        },
      }),
    ).toContain("请求尚未发送");
    saveTrainingDatasetDraft(owner, body);
    expect(
      removeMatchingTrainingDatasetDraft(owner, body, {
        getItem: localStorage.getItem.bind(localStorage),
        removeItem() {
          throw new Error("blocked");
        },
      }),
    ).toContain("无法清理");
    expect(readTrainingDatasetDraft(owner).body).toEqual(body);
  });
});

it("keeps native mask and polygon requests separate without changing the legacy key", () => {
  const polygonOwner = { ...owner, task: "segment" as const };
  const maskOwner = { ...polygonOwner, bridge: instanceMaskBridge } as const;
  const polygon = { ...body, task: "segment" as const };
  const mask: TrainingDatasetCreate = {
    ...polygon,
    bridge: instanceMaskBridge,
    request_id: "22222222-2222-4222-8222-222222222222",
  };
  expect(trainingDatasetDraftKey(polygonOwner)).not.toBe(trainingDatasetDraftKey(maskOwner));
  expect(saveTrainingDatasetDraft(polygonOwner, polygon)).toBeNull();
  expect(readTrainingDatasetDraft(maskOwner).body).toBeNull();
  expect(saveTrainingDatasetDraft(maskOwner, mask)).toBeNull();
  expect(readTrainingDatasetDraft(polygonOwner).body).toEqual(polygon);
  expect(readTrainingDatasetDraft(maskOwner).body).toEqual(mask);
  expect(removeMatchingTrainingDatasetDraft(maskOwner, mask)).toBeNull();
  expect(readTrainingDatasetDraft(polygonOwner).body).toEqual(polygon);
});

it("preserves the original filtered request and rejects corrupt filter restoration", () => {
  const scoped = {
    ...body,
    dataset_id: "11111111-2222-4333-8444-555555555555",
    dataset_filter: { extension: "png", min_width: 100 },
  };
  expect(saveTrainingDatasetDraft(owner, scoped)).toBeNull();
  expect(readTrainingDatasetDraft(owner)).toEqual({ body: scoped, error: null });
  expect(
    saveTrainingDatasetDraft(owner, { ...scoped, dataset_filter: { extension: "jpg" } }),
  ).toContain("另一个页面");
  const key = trainingDatasetDraftKey(owner);
  localStorage.setItem(
    key,
    JSON.stringify({ version: 1, ownerKey: key, body: { ...scoped, dataset_filter: { page: 9 } } }),
  );
  expect(readTrainingDatasetDraft(owner).error).toBeTruthy();
});
