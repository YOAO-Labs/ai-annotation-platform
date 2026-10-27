import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { DatasetsPage } from "./DatasetsPage";
import { datasetsApi } from "@/api/datasets";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";

vi.mock("@/api/datasets", () => ({ datasetsApi: { list: vi.fn() } }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: "super_admin" }) }));
vi.mock("@/components/datasets/ImportDatasetWizard", () => ({
  ImportDatasetWizard: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div>
        上传向导<button onClick={onClose}>关闭导入</button>
      </div>
    ) : null,
}));
vi.mock("@/components/datasets/CreateReferenceDatasetModal", () => ({
  CreateReferenceDatasetModal: () => <p>目录接入向导</p>,
}));
const clients: QueryClient[] = [];
const dataset = {
  id: "dataset-one",
  name: "车辆目录",
  data_type: "image",
  file_count: 125,
  project_count: 2,
  description: "",
  display_id: "DS-1",
  updated_at: "2026-10-23T00:00:00Z",
};
function Location() {
  const location = useLocation();
  return (
    <p data-testid="location">
      {location.pathname}
      {location.search}
    </p>
  );
}
function mount(path = "/datasets") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/datasets"
            element={
              <>
                <DatasetsPage />
                <Location />
              </>
            }
          />
          <Route path="/datasets/:datasetId" element={<Location />} />
          <Route path="/storage" element={<Location />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().setAuth("a", { id: "owner-a" } as MeResponse);
  vi.mocked(datasetsApi.list).mockResolvedValue({
    items: [dataset],
    total: 80,
    limit: 20,
    offset: 0,
  } as never);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
});
describe("Datasets list routes", () => {
  it("converts a legacy source and scan link without traversing the dataset list", async () => {
    mount("/datasets?dataset=outside&source=src&scan=run&q=vehicle&page=3");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/datasets/outside?"),
    );
    const url = new URL(screen.getByTestId("location").textContent!, "http://localhost");
    expect(url.searchParams.get("source")).toBe("src");
    expect(url.searchParams.get("scan")).toBe("run");
    expect(url.searchParams.get("return")).toBe("/datasets?q=vehicle&page=3");
    expect(datasetsApi.list).not.toHaveBeenCalled();
  });
  it("sends restored list pagination and location to the server, and preserves return context", async () => {
    mount("/datasets?q=vehicle&data_type=image&location_id=loc&page=3&page_size=10");
    expect(await screen.findByRole("link", { name: "车辆目录" })).toHaveAttribute(
      "href",
      expect.stringContaining("return=%2Fdatasets%3Fq%3Dvehicle"),
    );
    expect(datasetsApi.list).toHaveBeenCalledWith(
      expect.objectContaining({
        search: "vehicle",
        data_type: "image",
        location_id: "loc",
        limit: 10,
        offset: 20,
      }),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("page=4"));
    expect(screen.queryByText("MinIO")).not.toBeInTheDocument();
    expect(screen.queryByText("数据连接器")).not.toBeInTheDocument();
  });
  it("keeps list failures distinct from an empty catalog", async () => {
    vi.mocked(datasetsApi.list).mockRejectedValue(new Error("列表读取失败"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("列表读取失败");
    expect(screen.queryByText(/暂无数据集/)).not.toBeInTheDocument();
  });
  it("returns an out-of-range list page to the last server page without dropping filters", async () => {
    mount("/datasets?q=vehicle&page=99&import=1");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("page=4"));
    expect(screen.getByTestId("location")).toHaveTextContent("q=vehicle");
    expect(screen.getByText("上传向导")).toBeVisible();
    await waitFor(() =>
      expect(datasetsApi.list).toHaveBeenLastCalledWith(
        expect.objectContaining({ search: "vehicle", offset: 60 }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });
  it("aborts retired authentication reads and never shows their late result", async () => {
    let resolve!: (value: unknown) => void;
    let signal: AbortSignal | undefined;
    vi.mocked(datasetsApi.list).mockImplementationOnce((_params, init) => {
      signal = init?.signal as AbortSignal;
      return new Promise((done) => {
        resolve = done;
      }) as never;
    });
    mount();
    await waitFor(() => expect(signal).toBeDefined());
    await act(async () => useAuthStore.getState().setAuth("b", { id: "owner-b" } as MeResponse));
    expect(await screen.findByRole("link", { name: "车辆目录" })).toBeVisible();
    expect(signal!.aborted).toBe(true);
    await act(async () => resolve({ items: [{ ...dataset, name: "旧账号目录" }], total: 1 }));
    expect(screen.queryByText("旧账号目录")).not.toBeInTheDocument();
  });
  it("returns from storage into the import dialog and closes it without dropping pagination", async () => {
    mount("/datasets?page=3&import=1");
    expect(screen.getByText("上传向导")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "关闭导入" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/datasets?page=3");
  });
  it("redirects the old connector tab to the storage configuration owner", async () => {
    mount("/datasets?tab=connectors");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/storage?tab=locations"),
    );
  });
});

it("restores archived filters and resets pagination when the status changes", async () => {
  mount("/datasets?state=archived&page=3&q=vehicle");
  await screen.findByRole("link", { name: "车辆目录" });
  expect(datasetsApi.list).toHaveBeenCalledWith(
    expect.objectContaining({ state: "archived", offset: 40 }),
    expect.anything(),
  );
  expect(screen.getByRole("combobox", { name: "数据集状态" })).toHaveValue("archived");
  fireEvent.change(screen.getByRole("combobox", { name: "数据集状态" }), {
    target: { value: "all" },
  });
  await waitFor(() =>
    expect(datasetsApi.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ state: "all", offset: 0 }),
      expect.anything(),
    ),
  );
  expect(screen.getByTestId("location")).toHaveTextContent("state=all");
  expect(screen.getByTestId("location")).not.toHaveTextContent("page=3");
});
