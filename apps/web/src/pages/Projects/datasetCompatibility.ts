import type { TrainingDatasetSummary } from "@/api/trainingDatasets";
import { instanceMaskBridge } from "../Training/trainingDatasetBridges";

export function datasetComparisonReason(
  target: TrainingDatasetSummary,
  baseline: TrainingDatasetSummary,
): string | null {
  if (target.project_id !== baseline.project_id) return "不属于同一项目";
  if (target.task !== baseline.task) return "训练任务不同";
  if (baseline.status !== "ready") return "版本尚未就绪";
  const a = target.contract_profile;
  const b = baseline.contract_profile;
  if (!a || !b) return "冻结契约资料不完整";
  if (a.media_type !== "image" || b.media_type !== "image") return "当前仅资格验证图片输出";
  if (
    ["task", "media_type", "format", "geometry", "bridge"].some(
      (key) => a[key as keyof typeof a] !== b[key as keyof typeof b],
    )
  )
    return "媒体、格式、几何或桥接契约不兼容";
  return null;
}

export function datasetParentReason(
  version: TrainingDatasetSummary,
  task: "detect" | "segment",
  bridge?: string,
): string | null {
  if (version.status !== "ready") return "版本尚未就绪";
  if (version.task !== task) return "训练任务不同";
  const profile = version.contract_profile;
  if (!profile) return "冻结契约资料不完整";
  if (profile.media_type !== "image" || profile.source_media_type !== "image")
    return "当前继承仅资格验证图片来源与图片输出";
  const nativeMask =
    task === "segment" &&
    profile.format === "aap-instance-rle" &&
    profile.geometry === "instance-mask" &&
    profile.bridge === instanceMaskBridge;
  const polygonOrBox =
    profile.format === "ultralytics-yolo" &&
    profile.geometry === (task === "detect" ? "normalized-bbox" : "normalized-simple-polygon") &&
    profile.bridge === (task === "detect" ? "ultralytics-bbox-1" : "ultralytics-simple-polygon-1");
  if (!nativeMask && !polygonOrBox) return "该几何或桥接契约尚未资格验证";
  if (bridge && profile.bridge !== bridge) return "训练桥接不同，不能直接继承划分";
  return null;
}
