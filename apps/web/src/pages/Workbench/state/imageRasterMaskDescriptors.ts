import type { AnnotationResponse } from "@/types";
import { rasterMasksApi } from "@/api/rasterMasks";
import { classColorForCanvas } from "../stage/colors";
import type { RasterMaskRecordDescriptor } from "../stage/shared/useRasterMaskRecords";
import type { AiBox } from "./transforms";

export function buildImageRasterMaskDescriptors(
  annotations: readonly AnnotationResponse[],
  selectedIds: ReadonlySet<string>,
): RasterMaskRecordDescriptor<"annotation">[] {
  return annotations.flatMap((annotation) => {
    // Optimistic creates have uploaded content, but no readable annotation endpoint yet.
    if (annotation.geometry.type !== "raster_mask" || annotation.id.startsWith("tmp_")) return [];
    const color = classColorForCanvas(annotation.class_name);
    return [
      {
        id: annotation.id,
        source: "annotation" as const,
        ref: annotation.geometry.mask,
        revision: annotation.version ?? annotation.geometry.mask.sha256,
        color,
        colorRevision: color,
        zOrder: annotation.z_order ?? 0,
        selected: selectedIds.has(annotation.id),
        load: () => rasterMasksApi.annotationRasterMaskContent(annotation.id),
      },
    ];
  });
}

export function buildImagePredictionMaskDescriptors(
  taskId: string,
  candidates: readonly AiBox[],
  selectedIds: ReadonlySet<string>,
): RasterMaskRecordDescriptor<"prediction">[] {
  return candidates.flatMap((candidate, index) => {
    if (candidate.geometry?.type !== "raster_mask") return [];
    const color = classColorForCanvas(candidate.cls);
    return [
      {
        id: candidate.id,
        source: "prediction" as const,
        ref: candidate.geometry.mask,
        revision: candidate.geometry.mask.sha256,
        color,
        colorRevision: color,
        zOrder: index,
        selected: selectedIds.has(candidate.id),
        // Each candidate checks its own Task/Prediction/shape authorization.
        deduplicateLoad: false,
        load: () =>
          rasterMasksApi.predictionRasterMaskContent(
            taskId,
            candidate.predictionId,
            candidate.shapeIndex,
          ),
      },
    ];
  });
}
