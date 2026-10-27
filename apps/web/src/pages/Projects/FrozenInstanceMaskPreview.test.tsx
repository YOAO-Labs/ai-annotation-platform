import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { trainingDatasetsApi, type TrainingDatasetInstanceMask } from "@/api/trainingDatasets";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";
import { FrozenInstanceMaskPreview } from "./FrozenInstanceMaskPreview";

const preview = vi.hoisted(() => vi.fn());
vi.mock("./InstanceMaskPreview", () => ({
  InstanceMaskPreview: (props: unknown) => {
    preview(props);
    return null;
  },
}));
vi.mock("@/api/trainingDatasets", () => ({ trainingDatasetsApi: { sampleMask: vi.fn() } }));
const label: TrainingDatasetInstanceMask = {
  type: "instance_mask",
  annotation_id: "annotation",
  annotation_version: 7,
  class_index: 0,
  class_name: "车辆",
  area: 0.5,
  area_pixels: 2,
  bbox_xyxy: [0, 0, 2, 2],
  pixel_sha256: "a".repeat(64),
  size: [2, 2],
};
const content = {
  annotation_id: label.annotation_id,
  annotation_version: label.annotation_version,
  pixel_sha256: label.pixel_sha256,
  rle: { encoding: "coco_rle" as const, size: [2, 2] as [number, number], counts: [1, 2, 1] },
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(trainingDatasetsApi.sampleMask).mockResolvedValue(content);
});
afterEach(cleanup);
function descriptor(assertOwner = vi.fn()) {
  render(
    <FrozenInstanceMaskPreview
      projectId="project"
      versionId="frozen-version"
      taskId="task"
      scopeKey="owner"
      sampleUrl="blob:frozen"
      labels={[label]}
      assertOwner={assertOwner}
    />,
  );
  return (
    preview.mock.calls[preview.mock.calls.length - 1][0] as {
      descriptors: RasterMaskRecordDescriptor[];
    }
  ).descriptors[0];
}

it("loads by immutable annotation identity and checks the frozen annotation version and pixel digest", async () => {
  const value = descriptor();
  expect(value.id).toBe("annotation");
  await expect(value.load()).resolves.toEqual(content.rle);
  expect(trainingDatasetsApi.sampleMask).toHaveBeenCalledWith(
    "project",
    "frozen-version",
    "task",
    "annotation",
  );
});
it("rejects a response from a changed annotation instead of showing current truth", async () => {
  vi.mocked(trainingDatasetsApi.sampleMask).mockResolvedValue({
    ...content,
    annotation_version: 8,
  });
  await expect(descriptor().load()).rejects.toThrow("冻结掩码响应身份与来源标注不一致");
});
it("checks ownership before and after the async mask read", async () => {
  const assertOwner = vi
    .fn()
    .mockImplementationOnce(() => undefined)
    .mockImplementationOnce(() => {
      throw new DOMException("owner changed", "AbortError");
    });
  await expect(descriptor(assertOwner).load()).rejects.toThrow("owner changed");
  expect(assertOwner).toHaveBeenCalledTimes(2);
});
