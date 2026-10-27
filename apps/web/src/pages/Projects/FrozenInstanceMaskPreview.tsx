import { useMemo } from "react";
import { trainingDatasetsApi, type TrainingDatasetInstanceMask } from "@/api/trainingDatasets";
import { validateCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";
import { InstanceMaskPreview } from "./InstanceMaskPreview";

const colors = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2"];
export function FrozenInstanceMaskPreview({
  projectId,
  versionId,
  taskId,
  scopeKey,
  sampleUrl,
  labels,
  assertOwner,
}: {
  projectId: string;
  versionId: string;
  taskId: string;
  scopeKey: string;
  sampleUrl: string;
  labels: TrainingDatasetInstanceMask[];
  assertOwner: () => void;
}) {
  const descriptors = useMemo(
    () =>
      labels.map(
        (label): RasterMaskRecordDescriptor<"frozen-instance"> => ({
          id: label.annotation_id,
          source: "frozen-instance",
          ref: { size: [label.size[0], label.size[1]], sha256: label.pixel_sha256 },
          revision: label.annotation_version,
          color: colors[label.class_index % colors.length],
          colorRevision: "frozen-1",
          zOrder: label.class_index,
          selected: false,
          deduplicateLoad: false,
          load: async () => {
            assertOwner();
            const value = await trainingDatasetsApi.sampleMask(
              projectId,
              versionId,
              taskId,
              label.annotation_id,
            );
            assertOwner();
            if (
              value.annotation_id !== label.annotation_id ||
              value.annotation_version !== label.annotation_version ||
              value.pixel_sha256 !== label.pixel_sha256
            )
              throw new Error("冻结掩码响应身份与来源标注不一致");
            const rle = validateCocoRle(value.rle);
            if (rle.size[0] !== label.size[0] || rle.size[1] !== label.size[1])
              throw new Error("冻结实例掩码尺寸与训练包不一致");
            return rle;
          },
        }),
      ),
    [projectId, versionId, taskId, labels, assertOwner],
  );
  return (
    <InstanceMaskPreview scopeKey={scopeKey} sampleUrl={sampleUrl} descriptors={descriptors} />
  );
}
