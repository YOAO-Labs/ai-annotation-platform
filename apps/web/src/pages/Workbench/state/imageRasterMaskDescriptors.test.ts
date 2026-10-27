import { describe, expect, it, vi } from "vitest";
import { rasterMasksApi } from "@/api/rasterMasks";
import { buildImagePredictionMaskDescriptors } from "./imageRasterMaskDescriptors";
import { predictionsToBoxes } from "./transforms";
import type { PredictionResponse } from "@/types";

describe("image prediction mask identity", () => {
  it("uses original indexes after filtering and rechecks each content owner", async () => {
    const mask = {
      encoding: "coco_rle_ref",
      size: [3, 3],
      object_key: "mask.json",
      sha256: "a".repeat(64),
      runs: 4,
      bytes: 24,
    };
    const boxes = predictionsToBoxes([
      {
        id: "prediction-a",
        tool_unit_id: "region",
        result: [
          {
            shape_index: 8,
            tool_unit_id: "region",
            class_name: "car",
            confidence: 0.9,
            geometry: { type: "raster_mask", mask },
          },
          {
            shape_index: 10,
            tool_unit_id: "region",
            class_name: "car",
            confidence: 0.8,
            geometry: { type: "raster_mask", mask },
          },
          {
            shape_index: 11,
            tool_unit_id: "bbox",
            class_name: "car",
            confidence: 0.7,
            geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
          },
        ],
      },
    ] as unknown as PredictionResponse[]);
    const fetch = vi
      .spyOn(rasterMasksApi, "predictionRasterMaskContent")
      .mockResolvedValue({ encoding: "coco_rle", size: [3, 3], counts: [0, 9] });
    try {
      const descriptors = buildImagePredictionMaskDescriptors(
        "task-a",
        boxes,
        new Set([boxes[0].id]),
      );
      expect(descriptors).toHaveLength(2);
      expect(descriptors.map((d) => [d.id, d.source, d.selected, d.deduplicateLoad])).toEqual([
        ["pred-prediction-a-8", "prediction", true, false],
        ["pred-prediction-a-10", "prediction", false, false],
      ]);
      await Promise.all(descriptors.map((d) => d.load()));
      expect(fetch.mock.calls).toEqual([
        ["task-a", "prediction-a", 8],
        ["task-a", "prediction-a", 10],
      ]);
      const filtered = buildImagePredictionMaskDescriptors("task-b", boxes.slice(1), new Set());
      await filtered[0].load();
      expect(fetch).toHaveBeenLastCalledWith("task-b", "prediction-a", 10);
    } finally {
      fetch.mockRestore();
    }
  });
});
