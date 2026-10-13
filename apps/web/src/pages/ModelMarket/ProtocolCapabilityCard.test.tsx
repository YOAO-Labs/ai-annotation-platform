// v0.14.11 · ProtocolCapabilityCard 单测.
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import type { ProtocolTask } from "@/api/mlCapabilities";
import { ProtocolCapabilityCard } from "./ProtocolCapabilityCard";
import type { FlatModel } from "./capability/types";

function makeTask(overrides: Partial<ProtocolTask> = {}): ProtocolTask {
  return {
    id: "ocr",
    label: "OCR",
    summary: "从图像中提取文本框与文本内容。",
    default_geometry: ["bbox"],
    default_modalities: ["image"],
    typical_models: ["PaddleOCR", "RapidOCR"],
    protocol_notes: "...",
    suggested_backends: [
      {
        name: "PaddleOCR",
        repo_url: "https://github.com/PaddlePaddle/PaddleOCR",
        summary: "Paddle 系 OCR。",
        research_link: null,
        infra: "paddle",
        builtin: false,
      },
    ],
    ...overrides,
  };
}

// 协议卡内已接入模型现复用 ModelCard, 入参为 FlatModel。
function makeFlat(
  custom: {
    id?: string;
    display_name?: string;
    backendName?: string;
    projectName?: string;
    source?: FlatModel["source"];
  } = {},
): FlatModel {
  const source = custom.source ?? "registered";
  return {
    model: {
      id: custom.id ?? "yolov8",
      display_name: custom.display_name ?? "YOLOv8 检测",
      task: "detection",
      infra: "pytorch",
      is_interactive: false,
      supported_geometric_outputs: ["bbox"],
    },
    backendId: `instance:${custom.backendName ?? "prod-yolo"}`,
    backendName: custom.backendName ?? "prod-yolo",
    projectId: "",
    projectName: custom.projectName ?? (source === "env_only" ? "平台内置" : "项目 A"),
    source,
    registeredProjects: [],
    stale: false,
  };
}

const noopLabel = (v: string) => v;

function renderCard(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ProtocolCapabilityCard", () => {
  it("0 model 时显示「暂无接入」徽标 + 一行典型模型说明; 推荐后端收进展开区", async () => {
    renderCard(
      <ProtocolCapabilityCard
        task={makeTask()}
        mounted={[]}
        infraLabel={noopLabel}
        modalityLabel={noopLabel}
      />,
    );
    expect(screen.getByText("暂无接入")).toBeInTheDocument();
    // 首层一行说明：典型模型。
    expect(screen.getByText(/PaddleOCR \/ RapidOCR/)).toBeInTheDocument();
    // 推荐后端在展开区域列出（plan §4.1）。
    expect(screen.queryByText("Paddle 系 OCR。")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /推荐后端/ }));
    expect(await screen.findByText("PaddleOCR")).toBeInTheDocument();
    expect(screen.getByText("Paddle 系 OCR。")).toBeInTheDocument();
  });

  it("N model 时显示「N 个模型已接入」并渲染 model 卡", () => {
    renderCard(
      <ProtocolCapabilityCard
        task={makeTask({ id: "detection", label: "目标检测" })}
        mounted={[
          makeFlat(),
          makeFlat({ id: "yolov8n", display_name: "YOLOv8n", backendName: "prod-yolo-2" }),
        ]}
        infraLabel={noopLabel}
        modalityLabel={noopLabel}
      />,
    );
    expect(screen.getByText("2 个模型已接入")).toBeInTheDocument();
    expect(screen.getByText("YOLOv8 检测")).toBeInTheDocument();
    expect(screen.getByText("YOLOv8n")).toBeInTheDocument();
    expect(screen.queryByText("暂无接入")).not.toBeInTheDocument();
  });

  it("点击「去注册」CTA 触发 onGoToRegistry 回调", () => {
    const onGoToRegistry = vi.fn();
    renderCard(
      <ProtocolCapabilityCard
        task={makeTask()}
        mounted={[]}
        infraLabel={noopLabel}
        modalityLabel={noopLabel}
        onGoToRegistry={onGoToRegistry}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /去注册/ }));
    expect(onGoToRegistry).toHaveBeenCalledTimes(1);
  });

  it("挂载模型卡展示来源 backend 名", () => {
    renderCard(
      <ProtocolCapabilityCard
        task={makeTask({ id: "detection" })}
        mounted={[
          makeFlat({ id: "m-env", source: "env_only", backendName: "gsam2" }),
          makeFlat({ id: "m-reg", source: "registered", backendName: "sam3.1" }),
        ]}
        infraLabel={noopLabel}
        modalityLabel={noopLabel}
      />,
    );
    expect(screen.getByText(/gsam2/)).toBeInTheDocument();
    expect(screen.getByText(/sam3\.1/)).toBeInTheDocument();
  });
});
