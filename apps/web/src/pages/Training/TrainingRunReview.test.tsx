import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { TrainingRunReview } from "./TrainingRunReview";

afterEach(cleanup);
const schema: TrainingParameterSchema = {
  adapter_revision: 2,
  framework: "ultralytics",
  framework_version: "8.4.177",
  model_family: "yolo11",
  task: "detect",
  schema_id: "review-schema",
  schema_digest: "a".repeat(64),
  source: "native",
  source_sha256: "b".repeat(64),
  groups: { basic: "基础训练", evaluation: "验证与产物" },
  fields: [
    { key: "epochs", label: "训练轮次", group: "basic", default: 50 },
    { key: "patience", label: "早停耐心轮次", group: "basic", default: 50 },
    { key: "evaluate_train", label: "逐轮评估训练集", group: "evaluation", default: false },
  ].map((field) => ({
    ...field,
    status: "editable",
    native_type: "number",
    defined_in: "native",
    native_default: field.default,
    aliases: [],
    rules: {},
    reason: "",
  })),
};
const group = (label: string) =>
  screen.getByText(label, { selector: "summary", exact: false }).closest("details")!;

it("collapses unchanged parameters, including explicit default overrides, and allows expansion", async () => {
  render(<TrainingRunReview schema={schema} values={{ epochs: 50, evaluate_train: false }} />);
  expect(group("基础训练")).not.toHaveAttribute("open");
  expect(group("验证与产物")).not.toHaveAttribute("open");
  await userEvent.click(screen.getByText("验证与产物", { selector: "summary", exact: false }));
  expect(group("验证与产物")).toHaveAttribute("open");
});

it("opens changed groups and keeps their unchanged parameters collapsed", async () => {
  render(<TrainingRunReview schema={schema} values={{ epochs: 25 }} />);
  expect(group("基础训练")).toHaveAttribute("open");
  expect(group("验证与产物")).not.toHaveAttribute("open");
  expect(screen.getByText("训练轮次")).toHaveClass("text-status-info");
  expect(screen.getByText("默认：50")).toBeVisible();
  expect(screen.getByText("25")).toHaveClass("text-status-info");
  expect(screen.getByText("早停耐心轮次")).not.toBeVisible();
  await userEvent.click(screen.getByText("1 项默认参数", { selector: "summary" }));
  expect(screen.getByText("早停耐心轮次")).toBeVisible();
});

it("compares server effective values instead of treating every user override as a change", () => {
  render(
    <TrainingRunReview
      schema={schema}
      values={{ epochs: 50 }}
      config={{
        configuration: {
          effective_values: { epochs: 50, patience: 50, evaluate_train: true },
          origins: { epochs: "user", evaluate_train: "dependency" },
        },
      }}
      digest="verified"
    />,
  );
  expect(group("基础训练")).not.toHaveAttribute("open");
  expect(group("验证与产物")).toHaveAttribute("open");
  expect(screen.getByText("逐轮评估训练集")).toHaveClass("text-status-info");
  expect(screen.getByText("默认：关闭")).toBeVisible();
});
