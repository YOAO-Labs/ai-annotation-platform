import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn((..._args: unknown[]) => Promise.resolve([]));

vi.mock("./client", () => ({
  apiClient: { post: (...args: unknown[]) => post(...args) },
}));

import { predictionsApi } from "./predictions";

beforeEach(() => post.mockClear());

describe("predictionsApi.accept", () => {
  it("includes the claimed video segment", () => {
    predictionsApi.accept("task-1", "prediction-1", 2, undefined, undefined, "segment-1");

    expect(post).toHaveBeenCalledWith(
      "/tasks/task-1/predictions/prediction-1/accept?shape_index=2&video_segment_id=segment-1",
      undefined,
      undefined,
    );
  });
});
