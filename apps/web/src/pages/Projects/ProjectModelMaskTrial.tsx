import { useMemo } from "react";
import { record, trainedModelsApi } from "@/api/trainedModels";
import type { CocoRleMaskRef } from "@/types";
import { validateCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";
import { InstanceMaskPreview } from "./InstanceMaskPreview";

const instanceColors = ["#2563eb", "#d97706", "#059669", "#db2777", "#7c3aed", "#0891b2"];

export function ProjectModelMaskTrial({
  projectId,
  modelId,
  deploymentId,
  trial,
  onReady,
  assertOwner,
}: {
  projectId: string;
  modelId: string;
  deploymentId: string;
  trial: Record<string, unknown>;
  onReady: (ready: boolean) => void;
  assertOwner?: () => void;
}) {
  const { descriptors, error, labels } = useMemo(() => {
    const descriptors: RasterMaskRecordDescriptor<"frozen-instance">[] = [];
    const labels: string[] = [];
    try {
      if (!Array.isArray(trial.result)) throw new Error("试用结果缺少逐实例掩码");
      trial.result.forEach((raw, index) => {
        const shape = record(raw);
        const geometry = record(shape.geometry);
        const mask = record(geometry.mask);
        if (
          shape.tool_unit_id !== "region" ||
          geometry.type !== "raster_mask" ||
          !Array.isArray(mask.size) ||
          mask.size.length !== 2 ||
          !mask.size.every((value) => Number.isSafeInteger(value) && Number(value) > 0) ||
          typeof mask.sha256 !== "string" ||
          !/^[0-9a-f]{64}$/.test(mask.sha256)
        )
          throw new Error("试用结果包含无效的实例掩码引用，请重新加载模型");
        const ref = mask as unknown as CocoRleMaskRef;
        labels.push(String(shape.class_name ?? "未命名类别"));
        descriptors.push({
          id: String(index),
          source: "frozen-instance",
          ref,
          revision: deploymentId,
          color: instanceColors[index % instanceColors.length],
          colorRevision: "trial-1",
          zOrder: index,
          selected: false,
          deduplicateLoad: false,
          load: async () => {
            assertOwner?.();
            const rle = validateCocoRle(
              await trainedModelsApi.trialMaskContent(projectId, modelId, deploymentId, index),
            );
            assertOwner?.();
            if (rle.size[0] !== ref.size[0] || rle.size[1] !== ref.size[1])
              throw new Error("冻结实例掩码尺寸与引用不一致");
            return rle;
          },
        });
      });
      return { descriptors, labels, error: "" };
    } catch (reason) {
      return {
        descriptors: [],
        labels: [],
        error: reason instanceof Error ? reason.message : "试用掩码读取失败",
      };
    }
  }, [projectId, modelId, deploymentId, trial, assertOwner]);
  return (
    <>
      <InstanceMaskPreview
        scopeKey={`${projectId}:${modelId}:${deploymentId}`}
        descriptors={descriptors}
        sampleUrl={typeof trial.sample_url === "string" ? trial.sample_url : undefined}
        error={error}
        onReady={onReady}
      />
      <p className="mt-2 text-xs text-muted-foreground">
        固定验证样本检出 {descriptors.length} 个实例掩码 · 推理{" "}
        {String(trial.inference_time_ms ?? "—")} ms。逐实例保留孔洞、多部件与重叠。
      </p>
      {labels.length > 0 && <p className="mt-1 text-xs">{labels.join("、")}</p>}
    </>
  );
}
