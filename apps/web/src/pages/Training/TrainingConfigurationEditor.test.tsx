import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { TrainingConfigurationEditor } from "./TrainingConfigurationEditor";
import type { ParameterDrafts } from "./trainingConfiguration";

const schema: TrainingParameterSchema = {
  adapter_revision: 1,
  framework: "ultralytics",
  framework_version: "8.4.183",
  model_family: "yolo11",
  task: "detect",
  schema_id: "schema",
  schema_digest: "digest",
  source: "native",
  source_sha256: "source-digest",
  groups: { basic: "基础训练", optimizer: "优化器与学习率", platform: "平台管理" },
  fields: [
    { key: "epochs", label: "训练轮数", group: "basic", default: 50 },
    { key: "batch", label: "批大小", group: "basic", default: 8 },
    { key: "lr0", label: "初始学习率", group: "optimizer", default: 0.01 },
    { key: "device", label: "设备", group: "platform", default: 0 },
  ].map((field) => ({
    ...field,
    native_default: field.default,
    rules: { type: "number", minimum: 0, maximum: 1000 },
    status: field.key === "device" ? "platform-managed" : "editable",
    aliases: [],
    native_type: "number",
    defined_in: "native",
    reason: "",
  })),
};

function Editor({ initial = {} }: { initial?: ParameterDrafts }) {
  const [fields, setFields] = useState(initial);
  return (
    <TrainingConfigurationEditor
      schema={schema}
      fields={fields}
      disabled={false}
      onChange={(key, value) => setFields((old) => ({ ...old, [key]: value }))}
      onReset={(keys) =>
        setFields((old) =>
          Object.fromEntries(Object.entries(old).filter(([key]) => !keys.includes(key))),
        )
      }
    />
  );
}

afterEach(cleanup);

describe("modified training parameter groups", () => {
  it("keeps every editable section and navigation entry with zero counts when nothing changed", async () => {
    const user = userEvent.setup();
    render(<Editor />);
    await user.selectOptions(screen.getByRole("combobox", { name: "参数范围" }), "all");
    await user.click(screen.getByRole("checkbox", { name: "仅看已修改" }));
    expect(screen.getByRole("button", { name: "基础训练 0" })).toBeVisible();
    expect(screen.getByRole("button", { name: "优化器与学习率 0" })).toBeVisible();
    expect(screen.getAllByText("0 项")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("显示 0 / 3 个可编辑参数 · 0 项已修改");
    expect(screen.queryByText("平台管理")).not.toBeInTheDocument();
    expect(screen.queryByText(/没有匹配的参数/)).not.toBeInTheDocument();
    for (const reset of screen.getAllByRole("button", { name: "重置本组" })) {
      expect(reset).toBeDisabled();
    }
    const summary = screen.getByText(/^优化器与学习率/, { selector: "summary" });
    await user.click(summary);
    expect(within(summary.closest("details")!).getByText("本组没有已修改的参数。")).toBeVisible();
  });

  it("retains empty sections alongside a changed group, through search and resetting the last change", async () => {
    const user = userEvent.setup();
    render(<Editor initial={{ epochs: { kind: "scalar", text: "51" } }} />);
    await user.click(screen.getByRole("checkbox", { name: "仅看已修改" }));
    expect(screen.getByRole("button", { name: "基础训练 1" })).toBeVisible();
    expect(screen.getByRole("button", { name: "优化器与学习率 0" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "训练轮数" })).toHaveValue("51");
    await user.type(screen.getByRole("textbox", { name: "搜索训练参数" }), "学习率");
    expect(screen.getAllByText("0 项")).toHaveLength(2);
    await user.clear(screen.getByRole("textbox", { name: "搜索训练参数" }));
    await user.click(screen.getByRole("button", { name: "重置训练轮数" }));
    expect(screen.getByRole("button", { name: "基础训练 0" })).toBeVisible();
    expect(screen.getByRole("button", { name: "优化器与学习率 0" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("显示 0 / 3 个可编辑参数 · 0 项已修改");
  });

  it("keeps the ordinary search empty state when modified-only filtering is off", async () => {
    const user = userEvent.setup();
    render(<Editor />);
    await user.type(screen.getByRole("textbox", { name: "搜索训练参数" }), "不存在的参数");
    expect(screen.getByText(/没有匹配的参数/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /基础训练/ })).not.toBeInTheDocument();
  });
});
