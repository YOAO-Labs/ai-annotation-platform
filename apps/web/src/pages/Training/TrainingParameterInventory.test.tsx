import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { trainingRunsApi } from "@/api/trainingRuns";
import { TrainingParameterInventory } from "./TrainingParameterInventory";

vi.mock("@/api/trainingRuns", () => ({ trainingRunsApi: { parameterSchema: vi.fn() } }));
const clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <TrainingParameterInventory
        projectId="project"
        framework="libreyolo"
        baseModel="LibreYOLO9t"
      />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.resetAllMocks();
});

it("shows all dispositions and intersects Chinese/key/alias search with groups and status", async () => {
  vi.mocked(trainingRunsApi.parameterSchema).mockResolvedValue({
    framework: "libreyolo",
    framework_version: "1.6.0",
    model_family: "yolo9",
    schema_id: "schema",
    schema_digest: "digest",
    source: "https://example.com/native",
    groups: { augmentation: "数据增强", platform: "平台与执行" },
    fields: [
      {
        key: "mixup_prob",
        label: "MixUp 概率",
        group: "augmentation",
        status: "editable",
        aliases: ["mixup"],
        default: 0,
        native_default: 0,
        reason: "检测增强",
        rules: { type: "number" },
      },
      {
        key: "device",
        label: "执行设备",
        group: "platform",
        status: "platform-managed",
        aliases: [],
        default: "cuda:0",
        native_default: "auto",
        reason: "平台分配",
        rules: {},
      },
      {
        key: "freeze",
        label: "冻结层",
        group: "platform",
        status: "unqualified",
        aliases: [],
        default: null,
        native_default: null,
        reason: "尚待验证",
        rules: {},
      },
      {
        key: "mask_ratio",
        label: "掩膜比例",
        group: "augmentation",
        status: "inapplicable",
        aliases: [],
        default: 4,
        native_default: 4,
        reason: "分割专用",
        rules: {},
      },
    ],
  } as never);
  mount();
  await screen.findByText("MixUp 概率");
  expect(trainingRunsApi.parameterSchema).toHaveBeenCalledWith(
    "project",
    "libreyolo",
    "LibreYOLO9t",
  );
  expect(screen.getByText("冻结层")).toBeVisible();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("搜索参数"), "mixup");
  expect(screen.getByText("显示 1 / 4 项")).toBeVisible();
  expect(screen.queryByText("执行设备")).not.toBeInTheDocument();
  await user.clear(screen.getByLabelText("搜索参数"));
  await user.selectOptions(screen.getByLabelText("参数支持状态"), "platform-managed");
  expect(screen.getByText("执行设备")).toBeVisible();
  await user.selectOptions(screen.getByLabelText("参数分组"), "augmentation");
  expect(screen.getByText("没有匹配的参数。")).toBeVisible();
});

it("surfaces an authorized schema read failure", async () => {
  vi.mocked(trainingRunsApi.parameterSchema).mockRejectedValue(new Error("该模型尚未通过参数验证"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("该模型尚未通过参数验证");
});
