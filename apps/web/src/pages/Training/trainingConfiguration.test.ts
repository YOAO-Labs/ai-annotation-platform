import { describe, expect, it } from "vitest";
import type { TrainingParameterField, TrainingParameterSchema } from "@/api/generated/types.gen";
import {
  collectOverrides,
  compatibleDrafts,
  importConfiguration,
  parseParameterDraft,
  portableConfiguration,
  valueToDraft,
  type ParameterRule,
} from "./trainingConfiguration";

const field = (
  key: string,
  rules: ParameterRule,
  extra: Partial<TrainingParameterField> = {},
): TrainingParameterField => ({
  key,
  label: key,
  rules: rules as TrainingParameterField["rules"],
  group: "basic",
  status: "editable",
  aliases: [],
  default: 0,
  native_default: 0,
  native_type: "number",
  defined_in: "native",
  reason: "",
  ...extra,
});
const schema: TrainingParameterSchema = {
  adapter_revision: 2,
  framework: "ultralytics",
  framework_version: "8.4.177",
  model_family: "yolo11",
  task: "detect",
  schema_id: "training-schema",
  schema_digest: "a".repeat(64),
  source: "native",
  source_sha256: "b".repeat(64),
  groups: { basic: "基本参数" },
  fields: [
    field("lr0", { type: "number", exclusiveMinimum: 0, maximum: 0.1 }),
    field("epochs", { type: "integer", minimum: 1, maximum: 1000 }),
    field("imgsz", { type: "integer", minimum: 128, maximum: 1280, multipleOf: 32 }),
    field("rect", { type: "boolean" }),
    field("optimizer", { enum: ["SGD", "AdamW"] }, { default: "SGD" }),
    field(
      "nbs",
      { anyOf: [{ type: "null" }, { type: "integer", minimum: 1, maximum: 1024 }] },
      { default: null },
    ),
    field("scale", {
      anyOf: [
        { type: "number", minimum: 0, maximum: 1 },
        {
          type: "array",
          minItems: 2,
          maxItems: 2,
          ordered: true,
          items: { type: "number", exclusiveMinimum: 0, maximum: 2 },
        },
      ],
    }),
    field("mixup_prob", { type: "number", minimum: 0, maximum: 1 }, { aliases: ["mixup"] }),
    field("device", {}, { status: "platform-managed" }),
  ],
};
const image = `sha256:${"c".repeat(64)}`;
const parse = (key: string, value: unknown) =>
  parseParameterDraft(schema.fields.find((item) => item.key === key)!, valueToDraft(value));
const read = (source: string) => importConfiguration(source, schema, "yolo11n", image);

describe("strict training values", () => {
  it("preserves scientific notation while rejecting empty, hexadecimal and nonfinite input", () => {
    expect(parse("lr0", "1e-6")).toEqual({ ok: true, value: 0.000001 });
    for (const text of ["", " ", "0x10", "1e999", "NaN", "Infinity", "1x", "0"])
      expect(parse("lr0", text).ok).toBe(false);
    expect(parse("epochs", "1.5").ok).toBe(false);
    expect(parse("epochs", "9007199254740993").ok).toBe(false);
    expect(parse("imgsz", "129").ok).toBe(false);
  });
  it("retains boolean/null/enum types and validates scalar/range unions", () => {
    expect(parse("rect", true)).toEqual({ ok: true, value: true });
    expect(parse("rect", "1").ok).toBe(false);
    expect(parse("nbs", null)).toEqual({ ok: true, value: null });
    expect(parse("optimizer", "auto").ok).toBe(false);
    expect(parse("scale", [0.8, 1.2])).toEqual({ ok: true, value: [0.8, 1.2] });
    for (const pair of [[1.2, 0.8], [0, 1], [1], [0.8, ""]])
      expect(parse("scale", pair).ok).toBe(false);
  });
  it("submits only overrides and explicitly reports unsupported old drafts", () => {
    expect(
      collectOverrides(schema, {
        lr0: valueToDraft("1e-6"),
        device: valueToDraft("cuda:1"),
        epochs: valueToDraft(0),
      }),
    ).toEqual({
      values: { lr0: 0.000001 },
      errors: { device: "该参数当前不可编辑", epochs: "不能小于 1" },
    });
    const drafts = { lr0: valueToDraft("1e-6"), removed: valueToDraft(2), epochs: valueToDraft(0) };
    expect(compatibleDrafts(schema, drafts)).toEqual({
      retained: { lr0: drafts.lr0 },
      discarded: ["removed", "epochs"],
    });
  });
});

describe("bounded YAML import and export", () => {
  it("round-trips canonical overrides and identities without runtime paths", () => {
    const values = { lr0: 0.000001, scale: [0.8, 1.2], nbs: null, rect: true };
    const source = portableConfiguration(schema, "yolo11n", image, values);
    expect(read(source)).toEqual({ values, errors: [], warnings: [] });
    expect(source).not.toContain("dataset");
    expect(source).not.toContain("cuda");
    const changed = source.replace(image, `sha256:${"d".repeat(64)}`);
    expect(read(changed).warnings).toHaveLength(1);
    for (const [before, after] of [
      ["yolo11n", "other-model"],
      [schema.schema_digest, "e".repeat(64)],
      ["8.4.177", "other-version"],
    ])
      expect(read(source.replace(before, after)).errors).not.toHaveLength(0);
  });
  it("normalizes aliases, rejects collisions, and does not coerce imported types", () => {
    expect(read("mixup: 0.2\nlr0: 1e-6").values).toEqual({ mixup_prob: 0.2, lr0: 0.000001 });
    for (const source of [
      "mixup: 0.2\nmixup_prob: 0.3",
      "lr0: '0.01'",
      "rect: 1",
      "device: cuda:0",
      "unknown: 1",
      "optimizer: auto",
    ])
      expect(read(source).errors).not.toHaveLength(0);
    const partial = read("lr0: 0.01\nepochs: 0\ndevice: cuda:0");
    expect(partial.values).toEqual({ lr0: 0.01 });
    expect(partial.errors).toHaveLength(2);
  });
  it.each([
    "",
    "# only a comment",
    "[]",
    "epochs: 1\nepochs: 2",
    "---\nepochs: 1\n---\nepochs: 2",
    "epochs: &e 2",
    "epochs: *e",
    "epochs: !!int 2",
    "epochs: !custom 2",
    "<<: {epochs: 1}",
    "__proto__: {polluted: true}",
    "constructor: 1",
    "prototype: 1",
    "1: 2",
    "lr0: .inf",
    "lr0: .nan",
    `x: ${"[".repeat(20)}0${"]".repeat(20)}`,
    `epochs: 1\n#${"x".repeat(65536)}`,
  ])("rejects unsafe, ambiguous or unsupported document %#", (source) => {
    expect(read(source).errors.length).toBeGreaterThan(0);
  });
});
