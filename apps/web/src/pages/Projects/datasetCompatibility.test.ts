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
