import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { DatasetDetailPage } from "./DatasetDetailPage";
import { datasetsApi } from "@/api/datasets";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { readOriginal } from "@/api/originals";
import { taskViewsApi } from "@/api/taskViews";
import { SUMMARY } from "@/test/dataManagerApi";

vi.mock("@/api/originals", async (load) => {
  const actual = await load<typeof import("@/api/originals")>();
  return { ...actual, readOriginal: vi.fn() };
});

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@/api/taskViews", () => ({ taskViewsApi: { summary: vi.fn() } }));
vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    get: vi.fn(),
    sceneOptions: vi.fn().mockResolvedValue({ items: [], total: 0, selected: null }),
    savedViews: vi.fn().mockResolvedValue({ items: [] }),
    saveView: vi.fn(),
    deleteView: vi.fn(),
    workbench: vi.fn(),
    listItems: vi.fn(),
    item: vi.fn(),
    trainingVersions: vi.fn(),
    previewUnlink: vi.fn(),
    unlinkProject: vi.fn(),
    scanItems: vi.fn(),
    backfillDimensions: vi.fn(),
    backfillMedia: vi.fn(),
    update: vi.fn(),
    linkProject: vi.fn(),
  },
}));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: "super_admin" }) }));
vi.mock("@/api/projects", () => ({ projectsApi: { list: vi.fn().mockResolvedValue([]) } }));
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: unknown) => unknown) => selector({ push: toast }),
}));
vi.mock("@/hooks/useVerifiedImage", () => ({
  useVerifiedImage: () => ({ url: null, error: null, retry: vi.fn() }),
}));
vi.mock("@/components/datasets/DirectoryReferencePanel", () => ({
  DirectoryReferencePanel: ({
    datasetId,
    initialSourceId,
    initialScanId,
    persistUrl,
    archived,
  }: {
    datasetId: string;
    initialSourceId?: string;
    initialScanId?: string;
    persistUrl?: boolean;
    archived?: boolean;
  }) => (
    <p data-persist-url={String(persistUrl)} data-archived={String(archived)}>
      来源面板 {datasetId} {initialSourceId} {initialScanId}
    </p>
  ),
}));
vi.mock("@/components/datasets/ReferenceReplicas", () => ({
  ReferenceReplicas: ({ itemId, archived }: { itemId: string; archived: boolean }) => (
    <p data-testid="reference-replicas" data-item={itemId} data-archived={String(archived)}>
      备用来源管理
    </p>
  ),
}));
vi.mock("@/components/datasets/ImportDatasetWizard", () => ({
  ImportDatasetWizard: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div>
        上传向导<button onClick={onClose}>关闭导入</button>
      </div>
    ) : null,
}));
vi.mock("@/components/datasets/AxisConventionPicker", () => ({
  AxisConventionPicker: () => <p>坐标系选择</p>,
}));
vi.mock("@/components/datasets/LinkJobProgress", () => ({ LinkJobProgress: () => null }));
const clients: QueryClient[] = [];
const dataset = {
  id: "dataset-one",
  name: "车辆目录",
  data_type: "image",
  created_by: "owner-a",
  file_count: 60,
  project_count: 2,
  description: "目录说明",
  display_id: "DS-1",
  has_scenes: true,
  is_temporal: false,
  created_at: "2026-10-23T00:00:00Z",
  updated_at: "2026-10-23T00:00:00Z",
};
const item = {
  id: "item-one",
  dataset_id: dataset.id,
  file_name: "car.png",
  file_path: "__aap_reference__/11111111-1111-4111-8111-111111111111/" + "a".repeat(64),
  file_type: "image",
  file_size: 1200,
  width: 48,
  height: 32,
  metadata: {},
  created_at: dataset.created_at,
  availability: {
    state: "preview_only",
    reason_code: "source_read_failed",
    observed_at: dataset.created_at,
    preview_available: true,
    source_id: "src",
    source_name: "公司目录",
    location_id: "loc",
    allowed_actions: ["read_preview"],
  },
};
const projects = [
  {
    id: "p-one",
    name: "车辆检测",
    display_id: "P1",
    type_label: "检测",
    total_tasks: 60,
    labeled_tasks: 40,
    completed_tasks: 35,
    review_tasks: 5,
    can_manage: true,
  },
  {
    id: "p-two",
    name: "路口复查",
    display_id: "P2",
    type_label: "检测",
    total_tasks: 30,
    labeled_tasks: 10,
    completed_tasks: 3,
    review_tasks: 7,
    can_manage: false,
  },
];
function Location() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <p data-testid="location">
        {location.pathname}
        {location.search}
      </p>
      <button onClick={() => navigate("/datasets/dataset-two")}>切换数据集</button>
      <button onClick={() => navigate("?q=changed")}>切换筛选条件</button>
      <button onClick={() => navigate(-1)}>返回先前筛选</button>
    </>
  );
}
function mount(path = "/datasets/dataset-one") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/datasets/:datasetId"
            element={
              <>
                <DatasetDetailPage />
                <Location />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(taskViewsApi.summary).mockResolvedValue(SUMMARY);
  vi.mocked(datasetsApi.savedViews).mockResolvedValue({ items: [] });
  useAuthStore.getState().setAuth("a", { id: "owner-a" } as MeResponse);
  vi.mocked(datasetsApi.get).mockImplementation(
    async (id) =>
      ({ ...dataset, id, name: id === "dataset-two" ? "第二目录" : dataset.name }) as never,
  );
  vi.mocked(datasetsApi.workbench).mockImplementation(async (id) => ({
    dataset_id: id,
    sources: [
      { id: "src", name: "公司目录", location_id: "loc", enabled: true, unregistered_at: null },
    ],
    projects,
    availability_counts: { preview_only: 1, verified: 59 },
  }));
  vi.mocked(datasetsApi.listItems).mockImplementation(
    async (id) =>
      ({ items: [{ ...item, dataset_id: id }], total: 60, limit: 20, offset: 0 }) as never,
  );
  vi.mocked(datasetsApi.item).mockImplementation(
    async (id, itemId) => ({ ...item, id: itemId, dataset_id: id }) as never,
  );
  vi.mocked(datasetsApi.trainingVersions).mockResolvedValue({
    items: [
      {
        id: "version-one",
        project_id: "p-one",
        project_name: "车辆检测",
        name: "冻结首轮",
        status: "ready",
        created_at: dataset.created_at,
      },
    ],
    total: 25,
    limit: 20,
    offset: 0,
  });
  vi.mocked(datasetsApi.previewUnlink).mockResolvedValue({
    will_delete_tasks: 3,
    will_delete_annotations: 2,
    will_delete_batches: 1,
  });
  vi.mocked(datasetsApi.unlinkProject).mockResolvedValue({
    deleted_tasks: 3,
    deleted_annotations: 2,
  } as never);
  vi.mocked(datasetsApi.scanItems).mockResolvedValue({ new_items: 2 } as never);
  vi.mocked(datasetsApi.backfillDimensions).mockResolvedValue({
    processed: 2,
    failed: 0,
    remaining_hint: false,
  });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
  vi.unstubAllGlobals();
  if (vi.isMockFunction(HTMLAnchorElement.prototype.click))
    vi.mocked(HTMLAnchorElement.prototype.click).mockRestore();
});
describe("Dataset detail workbench", () => {
  it.each([
    { reference: true, manage: true, archived: false, visible: true },
    { reference: true, manage: true, archived: true, visible: true },
    { reference: true, manage: false, archived: false, visible: false },
    { reference: false, manage: true, archived: false, visible: false },
  ])(
    "limits replica management to manageable reference items: %j",
    async ({ reference, manage, archived, visible }) => {
      vi.mocked(datasetsApi.get).mockResolvedValue({
        ...dataset,
        archived_at: archived ? dataset.created_at : null,
      } as never);
      vi.mocked(datasetsApi.item).mockResolvedValue({
        ...item,
        id: "outside",
        file_path: reference ? item.file_path : "managed/image.png",
        availability: { ...item.availability, allowed_actions: manage ? ["manage_source"] : [] },
      } as never);
      mount("/datasets/dataset-one?item=outside");
      const dialog = await screen.findByRole("dialog", { name: "样本检查器" });
      await within(dialog).findByText(item.file_name);
      if (visible) {
        const section = within(dialog).getByTestId("reference-replicas");
        expect(section).toHaveAttribute("data-item", "outside");
        expect(section).toHaveAttribute("data-archived", String(archived));
      } else expect(within(dialog).queryByTestId("reference-replicas")).not.toBeInTheDocument();
    },
  );
  it("keeps cached-original and preview actions visible while the source Agent is offline", async () => {
    vi.mocked(datasetsApi.item).mockResolvedValue({
      ...item,
      id: "outside",
      availability: {
        ...item.availability,
        reason_code: "agent_offline",
        original_cache_recorded: true,
        allowed_actions: ["read_preview", "read_original"],
        agent_status: {
          state: "offline",
          reason_code: "agent_offline",
          site_name: "上海站点",
          agent_name: "图片 Agent",
          last_seen: null,
          measured_at: dataset.created_at,
        },
      },
    } as never);
    mount("/datasets/dataset-one?item=outside");
    const dialog = await screen.findByRole("dialog", { name: "样本检查器" });
    expect((await within(dialog).findAllByText(/上海站点.*图片 Agent.*离线/))[0]).toBeVisible();
    expect(within(dialog).getByText("已有登记，读取时复核")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "核验读取原图" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "读取只读预览" })).toBeEnabled();
    expect(within(dialog).getByText(/状态原因：Agent 已离线/)).toBeVisible();
  });
  it("waits for a saved-view-only link and restores its source before querying samples", async () => {
    let resolve!: (value: never) => void;
    vi.mocked(datasetsApi.savedViews).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount("/datasets/dataset-one?saved_view=source-view");
    await screen.findByRole("combobox", { name: "个人视图" });
    expect(datasetsApi.listItems).not.toHaveBeenCalled();
    await act(async () =>
      resolve({
        items: [
          {
            id: "source-view",
            dataset_id: "dataset-one",
            name: "源图",
            query: { source_id: "src", extension: "png" },
          },
        ],
      } as never),
    );
    await waitFor(() =>
      expect(datasetsApi.listItems).toHaveBeenCalledWith(
        "dataset-one",
        expect.objectContaining({ source_id: "src", extension: "png" }),
        expect.anything(),
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("view_query=1");
  });
  it("keeps an unavailable saved-view link blocked instead of showing all samples", async () => {
    mount("/datasets/dataset-one?saved_view=missing-view");
    await screen.findByText(/此个人视图已删除/);
    expect(datasetsApi.listItems).not.toHaveBeenCalled();
  });
  it("applies combined server filters, preserves typing focus and rejects reversed ranges", async () => {
    const user = userEvent.setup();
    mount();
    const search = await screen.findByRole("textbox", { name: "搜索样本" });
    await user.type(search, "car");
    expect(search).toHaveFocus();
    expect(search).toHaveValue("car");
    await user.click(screen.getByText("更多筛选"));
    await user.selectOptions(screen.getByRole("combobox", { name: "媒体类型" }), "image");
    await user.type(screen.getByRole("textbox", { name: "文件扩展名" }), "JPEG");
    await user.type(screen.getByRole("spinbutton", { name: "最小宽度" }), "640");
    await user.type(screen.getByRole("spinbutton", { name: "最大宽度" }), "320");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByRole("alert")).toHaveTextContent("最小尺寸不能大于最大尺寸");
    await user.clear(screen.getByRole("spinbutton", { name: "最大宽度" }));
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    await waitFor(() =>
      expect(datasetsApi.listItems).toHaveBeenLastCalledWith(
        "dataset-one",
        expect.objectContaining({
          search: "car",
          media_type: "image",
          extension: "jpeg",
          min_width: 640,
          offset: 0,
        }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("min_width=640");
    expect(screen.getByRole("combobox", { name: "个人视图" })).toHaveValue("__custom__");
  });

  it("clears unapplied advanced drafts as well as active conditions", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByRole("textbox", { name: "搜索样本" });
    await user.click(screen.getByText("更多筛选"));
    const extension = screen.getByRole("textbox", { name: "文件扩展名" });
    await user.type(extension, "png");
    await user.click(screen.getByRole("button", { name: "清除更多条件" }));
    expect(extension).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("extension=png");
  });
  it("retires a view dialog and its late callback when the same dataset URL conditions change", async () => {
    const user = userEvent.setup();
    let resolve!: (value: never) => void;
    vi.mocked(datasetsApi.saveView).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount();
    await screen.findByRole("textbox", { name: "搜索样本" });
    await user.click(screen.getByRole("button", { name: "保存为新视图" }));
    await user.type(screen.getByRole("textbox", { name: "视图名称" }), "old scope");
    await user.click(screen.getByRole("button", { name: "保存视图" }));
    // Browser history can change while a modal owns focus.
    fireEvent.click(screen.getByRole("button", { name: "切换筛选条件", hidden: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () =>
      resolve({ id: "late", dataset_id: "dataset-one", name: "old scope", query: {} } as never),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("q=changed");
    expect(screen.getByTestId("location")).not.toHaveTextContent("saved_view=late");
  });
  it("restores personal query conditions, flags edits and saves a new view in a modal", async () => {
    const user = userEvent.setup();
    const view = {
      id: "saved-one",
      dataset_id: "dataset-one",
      name: "宽幅图片",
      query: { extension: "png", min_width: 640 },
      query_schema_version: 1 as const,
      created_at: dataset.created_at,
      updated_at: dataset.created_at,
    };
    let saved = [view];
    vi.mocked(datasetsApi.savedViews).mockImplementation(async () => ({ items: saved }));
    vi.mocked(datasetsApi.saveView).mockImplementation(async (_id, payload) => {
      const result = { ...view, ...payload, id: "saved-two", query_schema_version: 1 as const };
      saved = [...saved, result as typeof view];
      return result;
    });
    mount("/datasets/dataset-one?page=2&view=grid&availability=changed");
    const picker = await screen.findByRole("combobox", { name: "个人视图" });
    await screen.findByRole("option", { name: "宽幅图片" });
    await user.selectOptions(picker, "saved-one");
    await waitFor(() =>
      expect(datasetsApi.listItems).toHaveBeenLastCalledWith(
        "dataset-one",
        expect.objectContaining({
          extension: "png",
          min_width: 640,
          offset: 0,
        }),
        expect.anything(),
      ),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("saved_view=saved-one");
    expect(screen.getByTestId("location")).toHaveTextContent("view=grid");
    const search = screen.getByRole("textbox", { name: "搜索样本" });
    await user.type(search, "new");
    expect(screen.getByText(/当前条件有未保存修改/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存为新视图" }));
    const dialog = await screen.findByRole("dialog", { name: "保存个人视图" });
    await user.type(within(dialog).getByRole("textbox", { name: "视图名称" }), "新图片");
    await user.click(within(dialog).getByRole("button", { name: "保存视图" }));
    await waitFor(() =>
      expect(datasetsApi.saveView).toHaveBeenCalledWith(
        "dataset-one",
        expect.objectContaining({
          name: "新图片",
          query: expect.objectContaining({ search: "new", extension: "png", min_width: 640 }),
        }),
        undefined,
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("location")).toHaveTextContent("saved_view=saved-two");
  });

  it("ignores a late personal view save after switching datasets", async () => {
    const user = userEvent.setup();
    let resolve!: (value: never) => void;
    vi.mocked(datasetsApi.saveView).mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    mount();
    await screen.findByRole("textbox", { name: "搜索样本" });
    await user.click(screen.getByRole("button", { name: "保存为新视图" }));
    await user.type(screen.getByRole("textbox", { name: "视图名称" }), "late");
    await user.click(screen.getByRole("button", { name: "保存视图" }));
    // Route navigation happens outside the modal to exercise owner retirement.
    cleanup();
    mount("/datasets/dataset-two");
    await screen.findByRole("heading", { name: /第二目录/ });
    await act(async () =>
      resolve({ id: "late", dataset_id: "dataset-one", name: "late", query: {} } as never),
    );
    expect(screen.getByTestId("location")).not.toHaveTextContent("saved_view=late");
  });

  it("bounds sample, project and version pages after their totals shrink", async () => {
    mount(
      "/datasets/dataset-one?tab=versions&page=99&project_page=99&version_page=99&source=src&scan=run",
    );
    await waitFor(() => {
      const params = new URLSearchParams(screen.getByTestId("location").textContent!.split("?")[1]);
      expect(params.get("page")).toBe("3");
      expect(params.has("project_page")).toBe(false);
      expect(params.get("version_page")).toBe("2");
      expect(params.get("source")).toBe("src");
      expect(params.get("scan")).toBe("run");
    });
    await waitFor(() =>
      expect(datasetsApi.trainingVersions).toHaveBeenLastCalledWith(
        "dataset-one",
        { limit: 20, offset: 20 },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });
  it("restores server filters, grid and paging and preserves them when switching views", async () => {
    const user = userEvent.setup();
    mount(
      "/datasets/dataset-one?view=grid&page=3&page_size=10&source_id=src&availability=preview_only&q=car&return=%2Fdatasets%3Fpage%3D4",
    );
    expect(await screen.findByRole("heading", { name: "车辆目录" })).toBeVisible();
    await waitFor(() =>
      expect(datasetsApi.listItems).toHaveBeenCalledWith(
        "dataset-one",
        { limit: 10, offset: 20, source_id: "src", availability: "preview_only", search: "car" },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(screen.getByRole("link", { name: "返回数据集列表" })).toHaveAttribute(
      "href",
      "/datasets?page=4",
    );
    expect(screen.getByRole("button", { name: "网格视图" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(screen.getByRole("button", { name: "表格视图" }));
    expect(screen.getByTestId("location")).toHaveTextContent("page=3");
    expect(screen.getByTestId("location")).not.toHaveTextContent("view=grid");
    expect(screen.getByText("含 Scene", { exact: false })).toBeVisible();
  });
  it("loads a deep-linked item directly instead of searching all sample pages", async () => {
    mount("/datasets/dataset-one?item=outside");
    expect(await screen.findByRole("dialog", { name: "样本检查器" })).toBeVisible();
    await waitFor(() =>
      expect(datasetsApi.item).toHaveBeenCalledWith(
        "dataset-one",
        "outside",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByText("仅预览可用")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "读取只读预览" })).toBeEnabled();
    expect(within(dialog).queryByRole("button", { name: "核验读取原图" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("link", { name: "查看存储位置" })).not.toBeInTheDocument();
    expect(datasetsApi.listItems).toHaveBeenCalledTimes(1);
  });
  it("shows separate dataset-scoped project counts and only authorized training entry points", async () => {
    mount("/datasets/dataset-one?tab=projects");
    const first = (await screen.findByRole("link", { name: "车辆检测" })).closest("article")!;
    const second = screen.getByRole("link", { name: "路口复查" }).closest("article")!;
    expect(within(first).getByText("35")).toBeVisible();
    expect(within(second).getByText("3")).toBeVisible();
    for (const [card, projectId] of [
      [first, "p-one"],
      [second, "p-two"],
    ] as const) {
      const url = new URL(
        within(card)
          .getByRole("link", { name: projectId === "p-one" ? "车辆检测" : "路口复查" })
          .getAttribute("href")!,
        "http://localhost",
      );
      expect(url.pathname).toBe(`/projects/${projectId}/data-manager`);
      expect(JSON.parse(url.searchParams.get("filter")!).value).toEqual({
        field: "dataset.dataset_id",
        op: "eq",
        value: "dataset-one",
      });
    }
    expect(within(first).getByRole("link", { name: "为整份数据集创建版本" })).toHaveAttribute(
      "href",
      "/projects/p-one/learning?tab=data&dataset_id=dataset-one",
    );
    expect(
      within(second).queryByRole("link", { name: "为整份数据集创建版本" }),
    ).not.toBeInTheDocument();
  });
  it("restores and persists the selected statistics project through URL history", async () => {
    const user = userEvent.setup();
    mount("/datasets/dataset-one?tab=projects&stats_project=p-two&project_page=1");
    const select = await screen.findByRole("combobox", { name: "统计项目" });
    expect(select).toHaveValue("p-two");
    await waitFor(() =>
      expect(taskViewsApi.summary).toHaveBeenCalledWith(
        "p-two",
        {
          field: "dataset.dataset_id",
          op: "eq",
          value: "dataset-one",
        },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    await user.selectOptions(select, "p-one");
    expect(screen.getByTestId("location")).toHaveTextContent("stats_project=p-one");
    await user.click(screen.getByRole("button", { name: "返回先前筛选" }));
    expect(screen.getByRole("combobox", { name: "统计项目" })).toHaveValue("p-two");
    expect(screen.getByTestId("location")).toHaveTextContent("stats_project=p-two");
  });
  it.each(["", "unlinked-project"])(
    "does not query or replace an invalid explicit statistics selection %j",
    async (id) => {
      mount(`/datasets/dataset-one?tab=projects&stats_project=${id}`);
      expect(await screen.findByText(/所选项目不可用/)).toBeVisible();
      expect(taskViewsApi.summary).not.toHaveBeenCalled();
      expect(screen.getByTestId("location")).toHaveTextContent(`stats_project=${id}`);
    },
  );
  it("hides cached project statistics when the workbench access check fails", async () => {
    vi.mocked(taskViewsApi.summary).mockResolvedValue({
      ...SUMMARY,
      annotations: { ...SUMMARY.annotations, by_class: { 私有标签: 2 } },
    });
    mount("/datasets/dataset-one?tab=projects&stats_project=p-one");
    await screen.findByRole("cell", { name: "私有标签" });
    vi.mocked(datasetsApi.workbench).mockRejectedValueOnce(new Error("没有数据集访问权限"));
    await act(async () => clients[0].invalidateQueries({ queryKey: ["dataset-workbench"] }));
    expect(await screen.findByRole("alert")).toHaveTextContent("没有数据集访问权限");
    expect(screen.queryByRole("table", { name: "项目标签数量" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "车辆检测" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "路口复查" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "关联项目分页" })).not.toBeInTheDocument();
    expect(taskViewsApi.summary).toHaveBeenCalledTimes(1);
  });
  it("retires project statistics and cards when dataset access is revoked", async () => {
    vi.mocked(taskViewsApi.summary).mockResolvedValue({
      ...SUMMARY,
      annotations: { ...SUMMARY.annotations, by_class: { 私有标签: 2 } },
    });
    mount("/datasets/dataset-one?tab=projects&stats_project=p-one");
    await screen.findByRole("table", { name: "项目标签数量" });
    vi.mocked(datasetsApi.get).mockRejectedValueOnce(new Error("数据集访问已撤销"));
    await act(async () => clients[0].invalidateQueries({ queryKey: ["dataset"] }));
    expect(await screen.findByRole("alert")).toHaveTextContent("数据集访问已撤销");
    expect(screen.queryByRole("region", { name: "项目标签统计" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "车辆检测" })).not.toBeInTheDocument();
  });
  it("paginates associated versions and links to the existing training detail", async () => {
    mount("/datasets/dataset-one?tab=versions&version_page=2");
    expect(await screen.findByRole("link", { name: "冻结首轮" })).toHaveAttribute(
      "href",
      "/projects/p-one/learning/data/version-one",
    );
    expect(datasetsApi.trainingVersions).toHaveBeenCalledWith(
      "dataset-one",
      { limit: 20, offset: 20 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
  it("fails closed on unlink preview errors and requires the dataset name after retry", async () => {
    vi.mocked(datasetsApi.previewUnlink).mockRejectedValueOnce(new Error("无法核对影响"));
    const user = userEvent.setup();
    mount("/datasets/dataset-one?tab=projects");
    await user.click(await screen.findByRole("button", { name: "取消关联" }));
    expect(await screen.findByText("影响范围未确认，暂不能取消关联。")).toBeVisible();
    expect(screen.getByRole("button", { name: "确认删除并取消关联" })).toBeDisabled();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: /请输入数据集名称/ })).toBeVisible(),
    );
    expect(screen.getByRole("button", { name: "确认删除并取消关联" })).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: /请输入数据集名称/ }), "车辆目录");
    await user.click(screen.getByRole("button", { name: "确认删除并取消关联" }));
    await waitFor(() =>
      expect(datasetsApi.unlinkProject).toHaveBeenCalledWith("dataset-one", "p-one"),
    );
  });
  it("ignores late mutation callbacks after switching resources", async () => {
    let resolve!: (value: unknown) => void;
    vi.mocked(datasetsApi.scanItems).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }) as never,
    );
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "扫描导入" }));
    await waitFor(() => expect(datasetsApi.scanItems).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "切换数据集" }));
    expect(await screen.findByRole("heading", { name: "第二目录" })).toBeVisible();
    await act(async () => resolve({ new_items: 999 }));
    expect(toast).not.toHaveBeenCalled();
  });
  it("aborts a retired inspector read across authentication transitions", async () => {
    let resolve!: (value: unknown) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(datasetsApi.item).mockImplementationOnce((_id, _itemId, init) => {
      signal = init?.signal as AbortSignal;
      return new Promise((done) => {
        resolve = done;
      }) as never;
    });
    mount("/datasets/dataset-one?item=outside");
    await waitFor(() => expect(signal).toBeDefined());
    await act(async () => useAuthStore.getState().setAuth("b", { id: "owner-b" } as MeResponse));
    await waitFor(() => expect(datasetsApi.item).toHaveBeenCalledTimes(2));
    expect(signal!.aborted).toBe(true);
    await act(async () => resolve({ ...item, id: "outside", file_name: "retired-secret.png" }));
    expect(screen.queryByText("retired-secret.png")).not.toBeInTheDocument();
  });
  it("restores exact source/scan focus and a safe return to storage", async () => {
    mount(
      "/datasets/dataset-one?tab=sources&source=src&scan=run&return=%2Fstorage%3Flocation%3Dloc",
    );
    expect(await screen.findByText("来源面板 dataset-one src run")).toHaveAttribute(
      "data-persist-url",
      "true",
    );
    expect(screen.getByRole("link", { name: "返回存储位置" })).toHaveAttribute(
      "href",
      "/storage?location=loc",
    );
  });
  it("preserves old media maintenance and coordinate-system controls", async () => {
    vi.mocked(datasetsApi.get).mockResolvedValueOnce({ ...dataset, data_type: "video" } as never);
    const rendered = mount();
    expect(await screen.findByRole("button", { name: "补生成元数据" })).toBeVisible();
    rendered.unmount();
    vi.mocked(datasetsApi.get).mockResolvedValueOnce({
      ...dataset,
      data_type: "point_cloud",
    } as never);
    mount();
    expect(await screen.findByText("坐标系选择")).toBeVisible();
  });
});

it("keeps Scene identity through advanced-filter clearing", async () => {
  vi.mocked(datasetsApi.sceneOptions).mockResolvedValue({
    items: [{ id: "scene-a", display_id: "S1", name: "街道 A" }],
    total: 1,
    selected: { id: "scene-a", display_id: "S1", name: "街道 A" },
  });
  mount("/datasets/dataset-one?scene_id=scene-a&extension=png");
  await screen.findByRole("option", { name: "街道 A" });
  expect(datasetsApi.listItems).toHaveBeenCalledWith(
    "dataset-one",
    expect.objectContaining({ scene_id: "scene-a", extension: "png" }),
    expect.anything(),
  );
  fireEvent.click(screen.getByRole("button", { name: "清除更多条件" }));
  await waitFor(() => expect(screen.getByTestId("location")).not.toHaveTextContent("extension="));
  expect(screen.getByTestId("location")).toHaveTextContent("scene_id=scene-a");
  expect(screen.getByRole("combobox", { name: "Scene 筛选" })).toHaveValue("scene-a");
});

it("offers the complete current filter in a centered project choice", async () => {
  const user = userEvent.setup();
  mount("/datasets/dataset-one?extension=png&min_width=640&page=2");
  await user.click(await screen.findByRole("button", { name: "冻结当前筛选" }));
  const dialog = screen.getByRole("dialog", { name: "冻结当前筛选" });
  const link = within(dialog).getByRole("link", { name: "在“车辆检测”中准备版本" });
  const url = new URL(link.getAttribute("href")!, "http://localhost");
  expect(url.searchParams.get("dataset_id")).toBe("dataset-one");
  expect(JSON.parse(url.searchParams.get("dataset_filter")!)).toEqual({
    extension: "png",
    min_width: 640,
  });
  expect(url.searchParams.has("page")).toBe(false);
  expect(within(dialog).queryByRole("link", { name: /路口复查/ })).not.toBeInTheDocument();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "冻结当前筛选" })).toHaveFocus();
});

it("retires the filter project dialog across navigation and browser return", async () => {
  const user = userEvent.setup();
  mount("/datasets/dataset-one?q=car");
  await user.click(await screen.findByRole("button", { name: "冻结当前筛选" }));
  fireEvent.click(screen.getByRole("button", { name: "切换筛选条件", hidden: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "返回先前筛选" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("q=car"));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("keeps archived samples readable while hiding management and new freeze actions", async () => {
  vi.mocked(datasetsApi.get).mockResolvedValue({
    ...dataset,
    archived_at: dataset.created_at,
    lifecycle_revision: 2,
  } as never);
  mount();
  await screen.findByRole("region", { name: "数据集归档状态" });
  expect(await screen.findByText("car.png")).toBeVisible();
  expect(screen.queryByRole("button", { name: "编辑信息" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "冻结当前筛选" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "恢复数据集" })).toBeEnabled();
});
it("shows archived source history in readonly mode and retains linked projects and versions", async () => {
  vi.mocked(datasetsApi.get).mockResolvedValue({
    ...dataset,
    archived_at: dataset.created_at,
    lifecycle_revision: 2,
  } as never);
  mount("/datasets/dataset-one?tab=sources&source=src&scan=run");
  expect(await screen.findByText(/来源面板 dataset-one/)).toHaveAttribute("data-archived", "true");
  fireEvent.click(screen.getByRole("button", { name: "项目关联" }));
  await screen.findByRole("link", { name: "车辆检测" });
  expect(screen.queryByRole("button", { name: "取消关联" })).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "为整份数据集创建版本" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关联版本" }));
  expect(await screen.findByText("冻结首轮")).toBeVisible();
});

describe("authenticated item downloads", () => {
  function managedItem() {
    vi.mocked(datasetsApi.item).mockResolvedValue({
      ...item,
      file_path: "managed/car.png",
      file_url: "https://old-signed.invalid/trap",
    } as never);
    vi.stubGlobal(
      "URL",
      Object.assign(class extends URL {}, {
        createObjectURL: vi.fn(() => "blob:download"),
        revokeObjectURL: vi.fn(),
      }),
    );
    return vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  }
  it("downloads verified bytes and revokes the temporary address instead of opening a signed link", async () => {
    const click = managedItem();
    vi.mocked(readOriginal).mockResolvedValue({
      identity: {} as never,
      blob: new Blob(["original"]),
    });
    mount("/datasets/dataset-one?item=item-one");
    await userEvent.click(await screen.findByRole("button", { name: "核验下载原件" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(readOriginal).toHaveBeenCalledWith(
      { kind: "item", datasetId: "dataset-one", itemId: "item-one" },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:download");
    expect(
      screen.queryByRole("link", { name: "查看托管原图（未提供实时核验）" }),
    ).not.toBeInTheDocument();
  });
  it("aborts on close and never starts a late download", async () => {
    const click = managedItem();
    let complete!: (value: Awaited<ReturnType<typeof readOriginal>>) => void;
    vi.mocked(readOriginal).mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    mount("/datasets/dataset-one?item=item-one");
    await userEvent.click(await screen.findByRole("button", { name: "核验下载原件" }));
    const calls = vi.mocked(readOriginal).mock.calls;
    const signal = calls[calls.length - 1][1]?.signal;
    await act(async () => {
      fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭" }));
      complete({ identity: {} as never, blob: new Blob(["late"]) });
      await Promise.resolve();
    });
    await waitFor(() => expect(signal?.aborted).toBe(true));
    expect(click).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});
