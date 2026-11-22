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

function toCaps(mc: Case["model_caps"]): StageCaps {
  const types = mc.output_attribute_types ?? [];
  const producesClass = types.length > 0 ? types.includes("class") : undefined;
  const b = mc.resource_profile?.batchable;
  const batchable = typeof b === "boolean" ? b : undefined;
  return {
    hasCapabilities: true,
    knownInputs: true,
    acceptsCrop: true,
    acceptsBboxPrompt: true,
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
      const payload = c.writes_attributes
        ? ({ ...attrPayload, task: c.model_caps.task } as PipelineStagePayload)
        : geomPayload;
      const warning = stageWarning(payload, toCaps(c.model_caps));
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
