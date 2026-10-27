import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ProtocolCapabilities } from "@/api/mlCapabilities";
import { ProtocolCatalogPanel } from "./ProtocolCatalogPanel";

const data: ProtocolCapabilities = {
  version: "2",
  infras: [],
  prompts: [],
  inputs: [],
  modalities: [{ id: "image", label: "图片", summary: "" }],
  geometries: [{ id: "bbox", label: "矩形框", summary: "" }],
  tasks: [
    {
      id: "detect",
      label: "目标检测",
      summary: "查找对象的位置与类别。",
      default_modalities: ["image"],
      default_geometry: ["bbox"],
      typical_models: ["YOLO"],
      protocol_notes: "result.type=rectanglelabels",
      suggested_backends: [
        {
          name: "参考后端",
          repo_url: "https://example.org/backend",
          summary: "后端实现说明",
          research_link: null,
          infra: null,
          builtin: false,
        },
      ],
    },
    {
      id: "pose",
      label: "关键点",
      summary: "查找人体关节。",
      default_modalities: ["image"],
      default_geometry: [],
      typical_models: ["HRNet"],
      protocol_notes: "result.type=keypointlabels",
      suggested_backends: [],
    },
  ],
};
let protocolError = false;
let modelsError = false;
const refetch = vi.fn();
vi.mock("@/api/mlCapabilities", () => ({
  useProtocolCapabilities: () => ({
    data: protocolError ? undefined : data,
    isPending: false,
    isSuccess: !protocolError,
    isError: protocolError,
    refetch,
  }),
}));
vi.mock("@/api/modelCatalog", () => ({
  useModelCatalog: () => ({
    data: modelsError ? undefined : { task_counts: { detect: 5 } },
    isError: modelsError,
  }),
}));
function renderPanel(url = "/model-market?tab=protocol") {
  render(
    <MemoryRouter initialEntries={[url]}>
      <ProtocolCatalogPanel />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  protocolError = false;
  modelsError = false;
  refetch.mockClear();
});
afterEach(cleanup);

describe("协议能力信息层级", () => {
  it("列表按任务分开输入输出，技术字段在可见的侧栏中阅读", async () => {
    const user = userEvent.setup();
    renderPanel();
    const article = screen.getByRole("heading", { name: "目标检测 detect" }).closest("article")!;
    expect(within(article).getByText("图片")).toBeInTheDocument();
    expect(within(article).getByText("矩形框")).toBeInTheDocument();
    expect(screen.queryByText("result.type=rectanglelabels")).not.toBeInTheDocument();
    expect(within(article).getByRole("link", { name: "查看模型（5）" })).toHaveAttribute(
      "href",
      "/model-market?model_task=detect",
    );
    const trigger = within(article).getByRole("button", { name: "查看协议" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "目标检测 · 协议详情" });
    expect(within(dialog).getByText("result.type=rectanglelabels")).toBeInTheDocument();
    expect(within(dialog).getByText("YOLO")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: /参考后端/ })).toHaveAttribute(
      "href",
      "https://example.org/backend",
    );
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("支持按参考模型搜索，任务筛选可见且可清除", async () => {
    const user = userEvent.setup();
    renderPanel("/model-market?tab=protocol&protocol_task=pose");
    expect(screen.getByRole("combobox", { name: "任务" })).toHaveValue("pose");
    expect(screen.queryByRole("heading", { name: /目标检测/ })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "任务" }), "");
    await user.type(screen.getByRole("textbox", { name: "搜索协议能力" }), "yolo");
    expect(screen.getByRole("heading", { name: /目标检测/ })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /关键点/ })).not.toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "搜索协议能力" }));
    await user.type(screen.getByRole("textbox", { name: "搜索协议能力" }), "unknown");
    expect(screen.getByText("没有匹配的协议能力。")).toBeInTheDocument();
  });

  it("目录读取失败不冒充零个模型，协议仍可独立浏览", () => {
    modelsError = true;
    renderPanel();
    expect(screen.getAllByText("目录数量暂不可用")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "查看协议" })).toHaveLength(2);
    expect(screen.queryByRole("link", { name: "查看模型（0）" })).not.toBeInTheDocument();
  });

  it("协议失败时显示重试", async () => {
    protocolError = true;
    renderPanel();
    expect(screen.getByRole("alert")).toHaveTextContent("协议能力读取失败");
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});
