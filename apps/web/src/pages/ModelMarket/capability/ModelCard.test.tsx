// v0.19.4 · ModelCard 能力徽标: batchable / device 语义徽标 + supported_inputs 可接受输入行。
// 模型市场多 TAB UI 优化 · 阶段二（plan §4.1）：卡片首层摘要化——徽标与
// 「输入 → 输出」摘要在首层；框架、完整输入、输出几何/属性、资源、变体收进
// 「详情」展开区。涉及这些行的断言先展开详情。
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ModelCard } from "./ModelCard";
import type { FlatModel } from "./types";
import type { MLModelCapability } from "@/api/ml-backends";

function makeItem(model: Partial<MLModelCapability>): FlatModel {
  return {
    model: { id: "m1", display_name: "Model 1", task: "detection", ...model },
    backendId: "instance:bk",
    backendName: "bk",
    projectId: "",
    projectName: "平台内置",
    source: "env_only",
    registeredProjects: [],
    stale: false,
  };
}

async function renderCard(model: Partial<MLModelCapability>) {
  const qc = new QueryClient();
  const view = render(
    <QueryClientProvider client={qc}>
      <ModelCard item={makeItem(model)} />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  return { user, ...view };
}

describe("ModelCard 能力徽标 (v0.19.4)", () => {
  it("batchable=true + supported_inputs → 可批量 + 输入→输出摘要; 详情区含完整输入行", async () => {
    const { user } = await renderCard({
      resource_profile: { batchable: true, device: "gpu" },
      supported_inputs: ["full_image", "crop"],
      supported_geometric_outputs: ["bbox"],
    });
    expect(screen.getByText("可批量")).toBeInTheDocument();
    expect(screen.getByText("GPU")).toBeInTheDocument();
    // 首层「输入 → 输出」摘要。
    expect(screen.getByText(/整图 \/ 裁剪 →/)).toBeInTheDocument();
    // 已升级为徽标的 batchable/device 不再以裸 k:v 重复出现在资源行。
    expect(screen.queryByText(/batchable:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/device:/)).not.toBeInTheDocument();
    // 完整输入标签收进详情展开区。
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("整图")).toBeInTheDocument();
    expect(screen.getByText("裁剪")).toBeInTheDocument();
  });

  it("batchable=false → 交互/有状态徽标", async () => {
    await renderCard({ resource_profile: { batchable: false } });
    expect(screen.getByText("交互/有状态")).toBeInTheDocument();
    expect(screen.queryByText("可批量")).not.toBeInTheDocument();
  });

  it("老 backend (空 resource_profile / 无 supported_inputs) → 不显示批量徽标, 不报错", async () => {
    const { user } = await renderCard({ resource_profile: {}, supported_inputs: [] });
    expect(screen.queryByText("可批量")).not.toBeInTheDocument();
    expect(screen.queryByText("交互/有状态")).not.toBeInTheDocument();
    // 首层摘要回落「整图」占位。
    expect(screen.getByText(/整图 →/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("整图")).toBeInTheDocument();
  });

  it("supported_inputs=video → 首层摘要使用词表标签", async () => {
    await renderCard({ supported_inputs: ["video"], supported_geometric_outputs: ["bbox"] });
    expect(screen.getByText(/视频 →/)).toBeInTheDocument();
  });

  it("resource_profile 仅含 device/batchable (均为徽标) → 详情区隐藏「资源」行, 不显示空「—」", async () => {
    // gsam2 / sam3 / yolo 的 resource_profile 就是 {device, batchable}; 二者已是顶部徽标,
    // 资源行无余项时应整行隐藏, 而非恒显示「—」。
    const { user } = await renderCard({ resource_profile: { device: "gpu", batchable: true } });
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.queryByText("资源")).not.toBeInTheDocument();
  });

  it("resource_profile 含余项 (vram 等) → 详情区「资源」行渲染余项", async () => {
    const { user } = await renderCard({
      resource_profile: { device: "gpu", batchable: true, vram_gb: 8 },
    });
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("资源")).toBeInTheDocument();
    expect(screen.getByText("vram_gb: 8")).toBeInTheDocument();
  });

  it("只报扁平 output_attribute_types (无 schema) → 详情区输出属性显示扁平值", async () => {
    // gsam2 / sam3 / yolo 只报 output_attribute_types, schema 为空。
    const { user } = await renderCard({
      output_attribute_types: ["class"],
      output_attribute_schema: [],
    });
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("class")).toBeInTheDocument();
  });

  it("output_attribute_schema 非空 → 详情区输出属性优先显示 schema 的 label", async () => {
    // rapidocr 等同时报扁平 keys 与结构化 schema; 展示优先取更友好的 label。
    const { user } = await renderCard({
      output_attribute_types: ["text", "language"],
      output_attribute_schema: [
        { key: "text", label: "识别文本", type: "text" },
        { key: "language", label: "语言", type: "select", options: [] },
      ],
    });
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("识别文本")).toBeInTheDocument();
    expect(screen.getByText("语言")).toBeInTheDocument();
    // 不显示扁平 key
    expect(screen.queryByText("text")).not.toBeInTheDocument();
  });

  it("驻留未知 (无 healthMeta) → 首层显示「运行状态未知」, 不冒充未加载", async () => {
    await renderCard({});
    expect(screen.getByText("运行状态未知")).toBeInTheDocument();
    expect(screen.queryByText("未加载")).not.toBeInTheDocument();
  });

  it("showTaskBadge=false (协议分组) → 卡内不重复任务徽标", async () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <ModelCard item={makeItem({})} showTaskBadge={false} />
      </QueryClientProvider>,
    );
    // detection 的词表标签是「目标检测」。
    expect(screen.queryByText("目标检测")).not.toBeInTheDocument();
  });
});
