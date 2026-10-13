// v0.18.28 · popover「运行当前题（按项目编排）」载荷构造的纯函数守护。
import { describe, it, expect } from "vitest";
import {
  buildPipelineRunPayload,
  missingBackendIdsForStages,
  selectProjectPipelineStages,
} from "../useWorkbenchShellModel.helpers";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";

const STAGES: PipelineStagePayload[] = [
  { stage: 0, ml_backend_id: "be-detect", model_id: "detect" },
  {
    stage: 1,
    ml_backend_id: "be-classify",
    model_id: "va",
    parent_stage: 0,
    roi: { mode: "crop", pad: 0.05 },
    write: { target: "attributes", keys: ["color"] },
  },
];

describe("buildPipelineRunPayload", () => {
  it("有编排 + taskId → 顶层 backend=源阶段, task_ids=[当前], overwrite + last_wins", () => {
    const out = buildPipelineRunPayload(STAGES, "T-1");
    expect(out).toEqual({
      ml_backend_id: "be-detect",
      task_ids: ["T-1"],
      execution_scope: "workbench",
      pipeline_stages: STAGES,
      predict_mode: "overwrite",
      on_key_conflict: "last_wins",
    });
  });

  it("源阶段非首位时仍取 parent_stage=null 的 backend 作顶层", () => {
    const reordered: PipelineStagePayload[] = [STAGES[1]!, STAGES[0]!];
    const out = buildPipelineRunPayload(reordered, "T-2");
    expect(out?.ml_backend_id).toBe("be-detect");
  });

  it("空编排 → null (不发请求)", () => {
    expect(buildPipelineRunPayload([], "T-1")).toBeNull();
    expect(buildPipelineRunPayload(null, "T-1")).toBeNull();
    expect(buildPipelineRunPayload(undefined, "T-1")).toBeNull();
  });

  it("无 taskId → null (不发请求)", () => {
    expect(buildPipelineRunPayload(STAGES, null)).toBeNull();
    expect(buildPipelineRunPayload(STAGES, undefined)).toBeNull();
    expect(buildPipelineRunPayload(STAGES, "")).toBeNull();
  });

  // claude[bot] P1 #5 · 编排引用的 backend 必须都在 availableBackendIds 集合里, 否则 null
  // (避免默默发请求换通用 422)。
  it("availableBackendIds 未传 → 跳过校验, 向后兼容", () => {
    expect(buildPipelineRunPayload(STAGES, "T-1")).not.toBeNull();
  });

  it("availableBackendIds 含全部引用 backend → 正常返回 payload", () => {
    const known = new Set(["be-detect", "be-classify", "be-other"]);
    expect(buildPipelineRunPayload(STAGES, "T-1", known)).not.toBeNull();
  });

  it("availableBackendIds 缺源阶段 backend → null", () => {
    const known = new Set(["be-classify"]);
    expect(buildPipelineRunPayload(STAGES, "T-1", known)).toBeNull();
  });

  it("availableBackendIds 缺下游阶段 backend → null", () => {
    const known = new Set(["be-detect"]);
    expect(buildPipelineRunPayload(STAGES, "T-1", known)).toBeNull();
  });

  it("missingBackendIdsForStages: 缺则报缺项, 重复引用去重", () => {
    const dup: PipelineStagePayload[] = [...STAGES, { ...STAGES[1]!, stage: 2 }];
    expect(missingBackendIdsForStages(dup, new Set(["be-detect"]))).toEqual(["be-classify"]);
    expect(missingBackendIdsForStages(STAGES, new Set(["be-detect", "be-classify"]))).toEqual([]);
    // 空集合 / 空 stages 守卫。
    expect(missingBackendIdsForStages([], new Set())).toEqual([]);
    expect(missingBackendIdsForStages(STAGES, null)).toEqual([]);
  });

  it("selectProjectPipelineStages: 默认命名编排优先于旧 project.preannotate_pipeline", () => {
    const legacy: PipelineStagePayload[] = [
      { stage: 0, ml_backend_id: "legacy", model_id: "legacy-detect" },
    ];
    const named: PipelineStagePayload[] = [
      { stage: 0, ml_backend_id: "named", model_id: "named-detect" },
    ];

    expect(
      selectProjectPipelineStages(
        [
          { is_default: false, stages: legacy },
          { is_default: true, stages: named },
        ],
        legacy,
      ),
    ).toBe(named);
  });

  it("selectProjectPipelineStages: 无默认命名编排时回落旧字段", () => {
    const legacy: PipelineStagePayload[] = [
      { stage: 0, ml_backend_id: "legacy", model_id: "legacy-detect" },
    ];

    expect(selectProjectPipelineStages([{ is_default: false, stages: STAGES }], legacy)).toBe(
      legacy,
    );
    expect(selectProjectPipelineStages([], legacy)).toBe(legacy);
    expect(selectProjectPipelineStages(undefined, null)).toBeNull();
  });
});
