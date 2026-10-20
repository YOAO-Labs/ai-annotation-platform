import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MaskPrediction } from "@/api/activeLearning";
import { decodeCocoRle, encodeCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";
import { ActiveLearningMaskPreview } from "./ActiveLearningMaskPreview";
const preview = vi.hoisted(() => vi.fn());
vi.mock("../Projects/InstanceMaskPreview", () => ({
  InstanceMaskPreview: (props: unknown) => {
    preview(props);
    return null;
  },
}));
const pixels = Uint8Array.from([255, 255, 255, 255, 0, 255, 255, 255, 255]);
const prediction: MaskPrediction = {
  index: 0,
  class_index: 1,
  native_class_index: 1,
  score: 0.7,
  rle: encodeCocoRle(pixels, 3, 3),
  mask_sha256: "a".repeat(64),
  area_pixels: 8,
};
beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);
function mount(predictions = [prediction], width = 3, height = 3, assertOwner = vi.fn()) {
  render(
    <ActiveLearningMaskPreview
      scopeKey="owner:round:attempt:sample"
      sampleUrl="blob:frozen"
      width={width}
      height={height}
      predictions={predictions}
      assertOwner={assertOwner}
    />,
  );
  return preview.mock.calls[preview.mock.calls.length - 1][0] as {
    descriptors: RasterMaskRecordDescriptor[];
    error: string;
    sampleUrl: string;
  };
}
it("keeps original per-instance mask pixels including holes and checks the current owner before decoding", async () => {
  const assertOwner = vi.fn();
  const value = mount([prediction], 3, 3, assertOwner);
  expect(value.error).toBe("");
  expect(value.sampleUrl).toBe("blob:frozen");
  expect(value.descriptors[0].ref.sha256).toBe(prediction.mask_sha256);
  const rle = await value.descriptors[0].load();
  expect(decodeCocoRle(rle)).toEqual(pixels);
  assertOwner.mockImplementation(() => {
    throw new Error("查看会话已变化");
  });
  await expect(value.descriptors[0].load()).rejects.toThrow("查看会话已变化");
});
it.each([
  [
    { ...prediction, rle: { ...prediction.rle, size: [1, 9] } },
    "候选掩码尺寸或像素面积与冻结证据不一致",
  ],
  [{ ...prediction, area_pixels: 9 }, "候选掩码尺寸或像素面积与冻结证据不一致"],
  [{ class_index: 1, score: 0.7, xyxy: [0, 0, 1, 1] }, "候选实例掩码身份或置信度无效"],
])("withholds inconsistent mask evidence instead of substituting geometry", (invalid, message) => {
  const value = mount([invalid as MaskPrediction]);
  expect(value.descriptors).toEqual([]);
  expect(value.error).toBe(message);
});
it("rejects candidate images beyond the segmentation pixel budget before loading any masks", () => {
  const value = mount([], 1281, 1000);
  expect(value.error).toBe("候选掩码超过本轮原图或实例预算");
  expect(value.descriptors).toEqual([]);
});
it("bounds aggregate per-sample RLE before rendering", () => {
  const large = Array.from({ length: 8 }, (_, index) => ({
    ...prediction,
    index,
    area_pixels: 500000,
    rle: {
      encoding: "coco_rle" as const,
      size: [1000, 1000] as [number, number],
      counts: Array(100000).fill(10),
    },
  }));
  const value = mount(large, 1000, 1000);
  expect(value.error).toBe("候选 RLE 超过单图 2 MiB 预算");
  expect(value.descriptors).toEqual([]);
});
