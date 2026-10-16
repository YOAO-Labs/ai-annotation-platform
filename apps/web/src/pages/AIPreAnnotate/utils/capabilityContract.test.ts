/**
 * v0.19.3 WS3 · 能力判据跨端契约 (前端侧)。
 *
 * 与后端 pytest (tests/test_pipeline_validation.py) 共读同一份 fixture
 * (src/__fixtures__/capability-validation-cases.json)。本测把 fixture 的 model_caps /
 * writes_attributes 映射成 stageWarning 的 (payload, caps) 入参 —— 复刻 StageCard 真实派生 ——
 * 断言首条警示与 expect_codes[0] 一致。前端判据漂移 (改 stageWarning batchable/class 分支或
 * StageCard 派生) → 本测红。
 */
import { describe, it, expect } from "vitest";

import { stageWarning, type StageCaps } from "./pipelineGraph";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";
import fixture from "@/__fixtures__/capability-validation-cases.json";

interface Case {
  name: string;
  model_caps: {
    resource_profile?: Record<string, unknown>;
    output_attribute_types?: string[];
    task?: string;
  };
  writes_attributes: boolean;
  expect_codes: string[];
}

// 写属性的下游 → 属性 payload (producesGeometry=false, class 分支可达);
// 否则 → 几何 payload (源/几何下游, class 分支不可达, 与后端 writes_attributes 门控对称)。
const attrPayload = {
  stage: 0,
  ml_backend_id: "bk",
  write: { target: "attributes" },
} as PipelineStagePayload;
const geomPayload = {
  stage: 0,
  ml_backend_id: "bk",
  model_id: "det",
  input: { mode: "crop" },
  write: { target: "geometry" },
} as PipelineStagePayload;

// 复刻 StageCard 的能力派生 (pipelineGraph.StageCaps 真值来源)。
function toCaps(mc: Case["model_caps"]): StageCaps {
  const types = mc.output_attribute_types ?? [];
  const producesClass = types.length > 0 ? types.includes("class") : undefined;
  const b = mc.resource_profile?.batchable;
  const batchable = typeof b === "boolean" ? b : undefined;
  return {
    hasCapabilities: true,
    knownInputs: true,
    // crop/bbox 均接受 → 隔离几何不可达分支, 只考能力判据 (batchable/class)。
    acceptsCrop: true,
    acceptsBboxPrompt: true,
    // 恒产属性 → 隔离 no-attr 分支。
    producesAttributes: true,
    producesClass,
    batchable,
  };
}

const CODE_MATCH: Record<string, RegExp> = {
  not_batchable: /batchable/i,
  no_class_attribute: /class|类别/i,
};

describe("能力判据跨端契约 (前端 stageWarning ↔ 后端 pipeline_validation)", () => {
  const cases = (fixture as { cases: Case[] }).cases;

  it("fixture 非空 (双端共读同一份)", () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  for (const c of cases) {
    it(c.name, () => {
      // task 经 payload.task_type 传入 (复刻 StageCard: 分类/识别下游 payload 带 task_type);
      // stageWarning 据此对 task=ocr 豁免 class 判据。
      const payload = c.writes_attributes
        ? ({ ...attrPayload, task: c.model_caps.task } as PipelineStagePayload)
        : geomPayload;
      const warning = stageWarning(payload, toCaps(c.model_caps));
      // 前端单条警示: 取 expect_codes[0] (后端顺序 batchable 先于 class); 空 → 无警示。
      const first = c.expect_codes[0];
      if (!first) {
        expect(warning).toBeNull();
      } else {
        expect(warning).not.toBeNull();
        expect(warning).toMatch(CODE_MATCH[first]);
      }
    });
  }
});
