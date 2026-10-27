import { expect, it } from "vitest";
import { insightVersion } from "@/test/trainingDatasetInsightsFixtures";
import { datasetComparisonReason, datasetParentReason } from "./datasetCompatibility";

it("allows normalized legacy/current profiles but separates source grouping from output comparison", () => {
  const legacy = {
    ...insightVersion,
    contract_profile: {
      ...insightVersion.contract_profile!,
      schema: 1,
      source_media_type: "video",
    },
  };
  expect(datasetComparisonReason(insightVersion, legacy)).toBeNull();
  expect(datasetParentReason(legacy, "detect")).toMatch(/图片来源/);
});

it("refuses unknown media, cross-task and incompatible geometry without turning them into detection", () => {
  const unknown = {
    ...insightVersion,
    contract_profile: { ...insightVersion.contract_profile!, media_type: null },
  };
  expect(datasetComparisonReason(insightVersion, unknown)).toMatch(/图片输出/);
  expect(datasetParentReason(unknown, "detect")).toMatch(/图片输出/);
  expect(datasetComparisonReason(insightVersion, { ...insightVersion, task: "segment" })).toMatch(
    /任务不同/,
  );
  expect(
    datasetComparisonReason(insightVersion, {
      ...insightVersion,
      contract_profile: { ...insightVersion.contract_profile!, bridge: "future-bridge" },
    }),
  ).toMatch(/不兼容/);
});

it("qualifies native instance masks and rejects cross-bridge direct inheritance", () => {
  const native = {
    ...insightVersion,
    task: "segment" as const,
    contract_profile: {
      ...insightVersion.contract_profile!,
      schema: 4,
      task: "segment" as const,
      format: "aap-instance-rle",
      geometry: "instance-mask",
      bridge: "ultralytics-instance-rle-1",
    },
  };
  expect(datasetParentReason(native, "segment")).toBeNull();
  expect(datasetParentReason(native, "segment", "ultralytics-instance-rle-1")).toBeNull();
  expect(datasetParentReason(native, "segment", "ultralytics-simple-polygon-1")).toMatch(
    /桥接不同/,
  );
  expect(datasetParentReason(native, "detect")).toMatch(/任务不同/);
});
