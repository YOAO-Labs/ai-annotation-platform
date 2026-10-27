import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "./client";
import { tasksApi } from "./tasks";
import { videoApi } from "./videos";

vi.mock("./client", () => ({ apiClient: { get: vi.fn(), silentGet: vi.fn() } }));
const output = "20000000-0000-4000-8000-000000000001";
const generation = "10000000-0000-4000-8000-000000000001";

describe("video immutable output request guards", () => {
  beforeEach(() => vi.clearAllMocks());

  it("binds packet offsets to the expected server output", () => {
    const init = { signal: new AbortController().signal };
    videoApi.getChunkSamples("item-1", 3, init, output);
    expect(apiClient.silentGet).toHaveBeenCalledWith(
      `/videos/item-1/chunks/3/samples?expected_output_id=${output}`,
      init,
    );
  });

  it("includes timetable generation while preserving requested frame range", () => {
    const init = { signal: new AbortController().signal };
    tasksApi.getVideoFrameTimetable(
      "task-1",
      { from: 3, to: 9, expected_metadata_generation_id: generation },
      init,
    );
    expect(apiClient.get).toHaveBeenCalledWith(
      `/tasks/task-1/video/frame-timetable?from=3&to=9&expected_metadata_generation_id=${generation}`,
      init,
    );
  });

  it("keeps the optional adapter parameters backward compatible", () => {
    videoApi.getChunkSamples("item-1", 3);
    tasksApi.getVideoFrameTimetable("task-1");
    expect(apiClient.silentGet).toHaveBeenCalledWith("/videos/item-1/chunks/3/samples", undefined);
    expect(apiClient.get).toHaveBeenCalledWith("/tasks/task-1/video/frame-timetable", undefined);
  });

  it("keeps expected metadata preparation polling out of global error toasts", () => {
    tasksApi.getVideoManifest("task-1");
    expect(apiClient.silentGet).toHaveBeenCalledWith("/tasks/task-1/video/manifest", undefined);
    expect(apiClient.get).not.toHaveBeenCalled();
  });
});
