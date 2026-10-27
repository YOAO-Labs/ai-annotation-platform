import { useMemo } from "react";
import type { MaskPrediction } from "@/api/activeLearning";
import { InstanceMaskPreview } from "../Projects/InstanceMaskPreview";
import { cocoRleArea, validateCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";

const colors = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2"];
export function ActiveLearningMaskPreview({
  scopeKey,
  sampleUrl,
  width,
  height,
  predictions,
  assertOwner,
}: {
  scopeKey: string;
  sampleUrl: string;
  width: number;
  height: number;
  predictions: MaskPrediction[];
  assertOwner: () => void;
}) {
  const { descriptors, error } = useMemo(() => {
    try {
      assertOwner();
      if (
        !Number.isSafeInteger(width) ||
        !Number.isSafeInteger(height) ||
        width <= 0 ||
        height <= 0 ||
        Math.max(width, height) > 4096 ||
        width * height > 1_280_000 ||
        predictions.length > 100
      )
        throw new Error("候选掩码超过本轮原图或实例预算");
      let rleBytes = 0;
      const descriptors = predictions.map(
        (prediction, index): RasterMaskRecordDescriptor<"frozen-instance"> => {
          if (
            !("rle" in prediction) ||
            prediction.index !== index ||
            !Number.isSafeInteger(prediction.class_index) ||
            prediction.class_index < 0 ||
            !Number.isSafeInteger(prediction.native_class_index) ||
            prediction.native_class_index < 0 ||
            !Number.isFinite(prediction.score) ||
            prediction.score < 0 ||
            prediction.score > 1 ||
            !/^[0-9a-f]{64}$/.test(prediction.mask_sha256)
          )
            throw new Error("候选实例掩码身份或置信度无效");
          const rle = validateCocoRle(prediction.rle);
          if (
            rle.size[0] !== height ||
            rle.size[1] !== width ||
            prediction.area_pixels <= 0 ||
            cocoRleArea(rle) !== prediction.area_pixels
          )
            throw new Error("候选掩码尺寸或像素面积与冻结证据不一致");
          rleBytes += JSON.stringify(rle).length;
          if (rleBytes > 2 * 1024 * 1024) throw new Error("候选 RLE 超过单图 2 MiB 预算");
          return {
            id: String(prediction.index),
            source: "frozen-instance",
            ref: { size: rle.size, sha256: prediction.mask_sha256 },
            revision: scopeKey,
            color: colors[prediction.class_index % colors.length],
            colorRevision: "active-learning-1",
            zOrder: prediction.index,
            selected: false,
            deduplicateLoad: false,
            load: async () => {
              assertOwner();
              return rle;
            },
          };
        },
      );
      return { descriptors, error: "" };
    } catch (reason) {
      return {
        descriptors: [],
        error: reason instanceof Error ? reason.message : "候选掩码无法读取",
      };
    }
  }, [scopeKey, width, height, predictions, assertOwner]);
  return (
    <InstanceMaskPreview
      scopeKey={scopeKey}
      sampleUrl={sampleUrl}
      descriptors={descriptors}
      error={error}
    />
  );
}
