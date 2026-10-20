import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { modelCatalogApi, type CatalogEntry, type ModelCatalogResponse } from "@/api/modelCatalog";
import { ModelDirectoryPanel } from "./ModelDirectoryPanel";
import { groupModelDirectory } from "./modelDirectoryGroups";

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (state: unknown) => unknown) => select({ user: { id: "owner" } }),
}));
vi.mock("@/api/mlCapabilities", () => ({
  useProtocolCapabilities: () => ({ data: { tasks: [{ id: "detect", label: "目标检测" }] } }),
}));

function entry(id: string, size = "n", overrides: Partial<CatalogEntry> = {}): CatalogEntry {
  return {
    id,
    name: "YOLO 目标检测",
    source: "builtin",
    model_id: "detect",
    task: "detect",
    media_type: "image",
    framework: "pytorch",
    model_variants: { series: "yolo11", size },
    publication: "builtin",
    capabilities: {
      id: "detect",
      supported_geometric_outputs: ["bbox"],
      classes: [{ index: 0, name: `${size}-class` }],
    },
    class_metadata: { class_source: "builtin_manifest", verification_status: "uninspected" },
    placements: [{ pool_id: "pool", registry_id: "registry", name: "YOLO 服务", ready: true }],
    operations: { preannotate: { allowed: true } },
    ...overrides,
  };
}
function response(items: CatalogEntry[], total = items.length): ModelCatalogResponse {
  return { items, total, page: 1, page_size: 200, projects: [{ id: "project", name: "测试项目" }] };
}
const clients: QueryClient[] = [];
function renderPanel(url = "/model-market?model_project=project") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <ModelDirectoryPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(modelCatalogApi, "list").mockResolvedValue(
    response([entry("nano"), entry("small", "s")]),
  );
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

describe("模型目录分组与详情", () => {
  it("同系列同任务合并，服务池、系列、任务与训练版本仍独立", () => {
    const entries = [
      entry("n"),
      entry("s", "s"),
      entry("v8", "n", { model_variants: { series: "yolov8", size: "n" } }),
      entry("other-pool", "n", {
        placements: [{ pool_id: "other", registry_id: "r2", name: "第二服务池", ready: true }],
      }),
      entry("segment", "n", { task: "segment" }),
      entry("trained-a", "n", { source: "project_trained" }),
      entry("trained-b", "n", { source: "project_trained" }),
    ];
    const groups = groupModelDirectory(entries);
    expect(groups).toHaveLength(6);
    expect(
      groups.find((group) => group.entries.length === 2)?.entries.map((item) => item.id),
    ).toEqual(["n", "s"]);
    expect(
      groupModelDirectory([
        entry("a", "n", { placements: undefined }),
        entry("b", "s", { placements: undefined }),
      ]),
    ).toHaveLength(2);
  });

  it("切换型号同时更新类别与精确模型链接，详情在 Sheet 中打开并支持 Escape 返回焦点", async () => {
    const user = userEvent.setup();
    renderPanel();
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText("1 组模型")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "s" }));
    expect(within(table).getByRole("button", { name: "s" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(table).getByRole("link", { name: "用于预标注 / 管线" })).toHaveAttribute(
      "href",
      "/ai-pre?project=project&catalog_model=small",
    );
    const trigger = within(table).getByRole("button", { name: "查看详情" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "YOLO 目标检测" });
    expect(dialog).toHaveAttribute("data-slot", "sheet-content");
    expect(within(dialog).getByText("0 · s-class")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "n" }));
    expect(within(dialog).getByText("0 · n-class")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "用于预标注 / 管线" })).toHaveAttribute(
      "href",
      "/ai-pre?project=project&catalog_model=nano",
    );
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("先合并跨 API 分页的完整型号，再按组分页", async () => {
    const first = Array.from({ length: 200 }, (_, i) => entry(`variant-${i}`, String(i)));
    vi.mocked(modelCatalogApi.list).mockImplementation(async (filters = {}) => {
      if (filters.page_size === 1) return response(first.slice(0, 1), 201);
      return response(filters.page === 2 ? [entry("last-variant", "last")] : first, 201);
    });
    renderPanel();
    await screen.findByText("1 组模型");
    expect(screen.getByRole("button", { name: "last" })).toBeInTheDocument();
    expect(screen.getByText(/201 个规格/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(modelCatalogApi.list).toHaveBeenCalledWith(
      expect.objectContaining({ page: 2, page_size: 200 }),
      undefined,
      expect.any(AbortSignal),
    );
  });

  it("切换到不可用型号时移除使用入口并显示该型号的原因", async () => {
    vi.mocked(modelCatalogApi.list).mockResolvedValue(
      response([
        entry("nano"),
        entry("small", "s", {
          operations: { preannotate: { allowed: false, reason: "实例离线" } },
        }),
      ]),
    );
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "s" }));
    expect(screen.getByText("实例离线")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "用于预标注 / 管线" })).not.toBeInTheDocument();
  });

  it("空结果与失败状态保持可见", async () => {
    vi.mocked(modelCatalogApi.list).mockResolvedValue(response([]));
    renderPanel();
    expect(await screen.findByText(/没有匹配的模型/)).toBeInTheDocument();
    cleanup();
    vi.mocked(modelCatalogApi.list).mockRejectedValue(new Error("unavailable"));
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("模型目录读取失败");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
});
