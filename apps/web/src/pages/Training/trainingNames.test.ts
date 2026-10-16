import { describe, expect, it } from "vitest";
import { newTrainingVersionName } from "./trainingNames";

describe("training version name defaults", () => {
  it.each([
    [new Date(2026, 8, 9, 11, 25), "2026-0909-1125 "],
    [new Date(2028, 1, 29, 0, 5), "2028-0229-0005 "],
  ])("formats the local creation time with padded fields", (date, name) => {
    expect(newTrainingVersionName(date)).toBe(name);
  });
});
