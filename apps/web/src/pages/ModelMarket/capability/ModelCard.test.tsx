import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ModelCard } from "./ModelCard";
import { ModelListTable } from "./ModelListTable";
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

describe("ModelCard 能力徽标", () => {
  it("batchable=true + supported_inputs → 可批量 + 输入→输出摘要; 详情区含完整输入行", async () => {
    const { user } = await renderCard({
      resource_profile: { batchable: true, device: "gpu" },
      supported_inputs: ["full_image", "crop"],
      supported_geometric_outputs: ["bbox"],
    });
    expect(screen.getByText("可批量")).toBeInTheDocument();
    expect(screen.getByText("GPU")).toBeInTheDocument();
    expect(screen.getByText(/整图 \/ 裁剪 →/)).toBeInTheDocument();
    expect(screen.queryByText(/batchable:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/device:/)).not.toBeInTheDocument();
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
    expect(screen.getByText(/整图 →/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("整图")).toBeInTheDocument();
  });

  it("supported_inputs=video → 首层摘要使用词表标签", async () => {
    await renderCard({ supported_inputs: ["video"], supported_geometric_outputs: ["bbox"] });
    expect(screen.getByText(/视频 →/)).toBeInTheDocument();
  });

  it("resource_profile 仅含 device/batchable (均为徽标) → 详情区隐藏「资源」行, 不显示空「—」", async () => {
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
    const { user } = await renderCard({
      output_attribute_types: ["class"],
      output_attribute_schema: [],
    });
    await user.click(screen.getByRole("button", { name: /详情/ }));
    expect(screen.getByText("class")).toBeInTheDocument();
  });

  it("output_attribute_schema 非空 → 详情区输出属性优先显示 schema 的 label", async () => {
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
    expect(screen.queryByText("text")).not.toBeInTheDocument();
  });

  it("驻留未知 (无 healthMeta) → 首层显示「运行状态未知」, 不冒充未加载", async () => {
    await renderCard({});
    expect(screen.getByText("运行状态未知")).toBeInTheDocument();
    expect(screen.queryByText("未加载")).not.toBeInTheDocument();
  });

  it("keeps an unobserved model's load state unknown in the list", () => {
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <ModelListTable items={[makeItem({})]} />
      </QueryClientProvider>,
    );
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
    expect(screen.queryByText("目标检测")).not.toBeInTheDocument();
  });
});
