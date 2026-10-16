import { afterEach, describe, expect, it, vi } from "vitest";
import type { TrainingDatasetCreate } from "@/api/generated/types.gen";
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
