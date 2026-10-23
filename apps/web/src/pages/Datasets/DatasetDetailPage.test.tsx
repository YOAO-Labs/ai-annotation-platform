import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { DatasetDetailPage } from "./DatasetDetailPage";
import { datasetsApi } from "@/api/datasets";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    get: vi.fn(),
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
  }: {
    datasetId: string;
    initialSourceId?: string;
    initialScanId?: string;
    persistUrl?: boolean;
  }) => (
    <p data-persist-url={String(persistUrl)}>
      来源面板 {datasetId} {initialSourceId} {initialScanId}
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
});
describe("Dataset detail workbench", () => {
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
    expect(within(first).getByRole("link", { name: "创建训练版本" })).toHaveAttribute(
      "href",
      "/projects/p-one/learning?tab=data&dataset_id=dataset-one",
    );
    expect(within(second).queryByRole("link", { name: "创建训练版本" })).not.toBeInTheDocument();
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
