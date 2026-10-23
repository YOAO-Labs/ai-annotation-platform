/**
 * ImportDatasetWizard 单测
 * 覆盖: 初始渲染(step1=选择来源) / 选源后推进 step2=基本信息 / 名称校验 /
 *       skipCreate 直接在来源步提交 / 文件模式 / ZIP 模式 / 上传触发 mutation /
 *       连接器模式按 source_path 末段自动命名
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";

const mockCreateDatasetMutateAsync = vi.fn();
const mockCreateDatasetReset = vi.fn();
const mockImportFromConnectionMutateAsync = vi.fn();
const mockPushToast = vi.fn();
const mockStorage = vi.hoisted(() => ({
  connections: [{ id: "c1", name: "Conn", kind: "s3", scope: "global" }],
}));

vi.mock("@/hooks/useDatasets", () => ({
  useCreateDataset: () => ({
    mutateAsync: mockCreateDatasetMutateAsync,
    reset: mockCreateDatasetReset,
    isPending: false,
  }),
  useImportFromConnection: () => ({
    mutateAsync: mockImportFromConnectionMutateAsync,
    isPending: false,
  }),
}));

vi.mock("@/hooks/useStorageConnections", () => ({
  useStorageConnections: () => ({
    data: mockStorage.connections,
    isLoading: false,
  }),
  useCreateStorageConnection: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: "super_admin" }),
}));

vi.mock("@/api/datasets", () => ({
  datasetsApi: {
    uploadInit: vi.fn().mockResolvedValue({ upload_url: "http://fake/upload", item_id: "item1" }),
    uploadComplete: vi.fn().mockResolvedValue(undefined),
    uploadZip: vi.fn().mockResolvedValue({ added: 5, skipped: 0, errors: [], total_in_zip: 5 }),
  },
}));

vi.mock("@/utils/uploadQueue", () => ({
  putWithProgress: vi.fn().mockResolvedValue(undefined),
  runUploadQueue: vi
    .fn()
    .mockImplementation(
      async (
        _tasks: unknown[],
        map: Map<string, { status: string; progress: number }>,
        opts: { onUpdate?: () => void },
      ) => {
        for (const [k, v] of map.entries()) {
          v.status = "done";
          v.progress = 100;
          map.set(k, v);
        }
        opts?.onUpdate?.();
      },
    ),
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { ImportDatasetWizard } from "./ImportDatasetWizard";

function RouterState() {
  const location = useLocation();
  return (
    <output data-testid="wizard-url">
      {location.pathname}
      {location.search}
    </output>
  );
}
function renderUI(
  props: Partial<React.ComponentProps<typeof ImportDatasetWizard>> = {},
  initialEntry = "/datasets",
) {
  const defaults = {
    open: true,
    onClose: vi.fn(),
  };
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ImportDatasetWizard {...defaults} {...props} />
      <RouterState />
    </MemoryRouter>,
  );
}

/** 选中一个文件，让「来源」步的文件模式可推进。 */
function pickFile() {
  const fileInput = document.querySelector('input[type="file"][multiple]') as HTMLInputElement;
  const fakeFile = new File(["content"], "img.jpg", { type: "image/jpeg" });
  Object.defineProperty(fileInput, "files", { value: [fakeFile], configurable: true });
  fireEvent.change(fileInput);
}

describe("ImportDatasetWizard", () => {
  beforeEach(() => {
    sessionStorage.clear();
    useAuthStore.setState({
      sessionId: "wizard-session",
      user: { id: "wizard-user", role: "super_admin" } as any,
    });
    mockStorage.connections = [{ id: "c1", name: "Conn", kind: "s3", scope: "global" }];
    mockCreateDatasetMutateAsync.mockReset().mockResolvedValue({ id: "ds-new", name: "Test DS" });
    mockCreateDatasetReset.mockReset();
    mockImportFromConnectionMutateAsync.mockReset();
    mockPushToast.mockReset();
  });

  it("初始渲染: step1 显示「选择来源」, 文件模式未选文件时「下一步」禁用", () => {
    renderUI();
    expect(screen.getByText("选择来源")).toBeInTheDocument();
    expect(screen.getByText(/拖拽文件到此处/)).toBeInTheDocument();
    // step1 不显示名称输入框（名称在 step2）
    expect(screen.queryByPlaceholderText(/商品检测训练集/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /下一步/ })).toBeDisabled();
  });

  it("选好文件后「下一步」启用 + 点击进入 step2「基本信息」", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    const nextBtn = screen.getByRole("button", { name: /下一步/ });
    expect(nextBtn).not.toBeDisabled();
    fireEvent.click(nextBtn);
    expect(screen.getByText("基本信息")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/商品检测训练集/)).toBeInTheDocument();
  });

  it("step2 名称不足 2 字符时「开始上传」禁用, 填合法名称后启用", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    const nameInput = screen.getByPlaceholderText(/商品检测训练集/);
    fireEvent.change(nameInput, { target: { value: "A" } });
    expect(screen.getByRole("button", { name: /开始上传/ })).toBeDisabled();
    fireEvent.change(nameInput, { target: { value: "合法数据集名称" } });
    expect(screen.getByRole("button", { name: /开始上传/ })).not.toBeDisabled();
  });

  it("skipCreate (datasetId 给定) 直接在「来源」步提交, 无名称字段、无「下一步」", () => {
    renderUI({ datasetId: "existing-ds", datasetName: "已有数据集" });
    expect(screen.getByText(/拖拽文件到此处/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/商品检测训练集/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下一步/ })).not.toBeInTheDocument();
    // 未选文件时提交按钮禁用
    expect(screen.getByRole("button", { name: /开始上传/ })).toBeDisabled();
  });

  it("切换到 ZIP 模式显示 ZIP 相关文案", () => {
    renderUI({ datasetId: "ds1" });
    fireEvent.click(screen.getByRole("button", { name: /ZIP 包/ }));
    expect(screen.getByText(/拖入或/)).toBeInTheDocument();
    expect(screen.getByText(/整包 ≤ 200MB/)).toBeInTheDocument();
  });

  it("连接器模式: 选定 source_path 后按末段自动填充数据集名", async () => {
    renderUI();
    fireEvent.click(screen.getByRole("button", { name: /连接器导入/ }));
    // 选连接器
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "c1" } });
    // 输入 source path
    fireEvent.change(screen.getByPlaceholderText("batch-a/"), {
      target: { value: "root/dataset-A" },
    });
    // 进入基本信息步，名称应自动为 dataset-A
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    const nameInput = screen.getByPlaceholderText(/商品检测训练集/) as HTMLInputElement;
    expect(nameInput.value).toBe("dataset-A");
  });
  it("keeps the selected connection ID while the available list is refreshed", () => {
    const view = renderUI();
    fireEvent.click(screen.getByRole("button", { name: /连接器导入/ }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "c1" } });
    mockStorage.connections = [
      { id: "c2", name: "Another connection", kind: "s3", scope: "global" },
    ];
    view.rerender(
      <MemoryRouter>
        <ImportDatasetWizard open onClose={vi.fn()} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("combobox")).toHaveValue("c1");
    expect(screen.getByRole("combobox")).toHaveDisplayValue("已选连接器（正在核对可用状态）");
  });

  it("moves configuration to storage and restores text choices to the same dataset/session", async () => {
    const datasetId = "11111111-1111-1111-1111-111111111111";
    const path = `/datasets/${datasetId}?tab=items&page=3&item=selected`;
    const view = renderUI({ datasetId }, path);
    fireEvent.click(screen.getByRole("button", { name: /连接器导入/ }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "c1" } });
    fireEvent.change(screen.getByPlaceholderText("batch-a/"), { target: { value: "fleet/2026" } });
    fireEvent.change(screen.getByPlaceholderText("*.jpg, *.png"), { target: { value: "*.png" } });
    fireEvent.click(screen.getByLabelText("递归扫描"));
    expect(screen.queryByRole("button", { name: "新建连接器" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "管理存储位置" }));
    const url = new URL(screen.getByTestId("wizard-url").textContent!, "https://local.test");
    expect(url.pathname).toBe("/storage");
    expect(url.searchParams.get("returnTo")).toBe(`${path}&import=1`);
    view.unmount();
    renderUI({ datasetId }, `${path}&import=1`);
    await waitFor(() => expect(screen.getByPlaceholderText("batch-a/")).toHaveValue("fleet/2026"));
    expect(screen.getByRole("combobox")).toHaveValue("c1");
    expect(screen.getByPlaceholderText("*.jpg, *.png")).toHaveValue("*.png");
    expect(screen.getByLabelText("递归扫描")).not.toBeChecked();
  });

  it("完整新建流程: 选文件 → 下一步 → 填名 → 开始上传 → 触发 createDataset", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.change(screen.getByPlaceholderText(/商品检测训练集/), {
      target: { value: "新建集合" },
    });
    const uploadBtn = screen.getByRole("button", { name: /开始上传/ });
    expect(uploadBtn).not.toBeDisabled();
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(mockCreateDatasetMutateAsync).toHaveBeenCalledTimes(1);
    });
  });

  it("3D 点云 + 勾选时序: createDataset 带 data_type=point_cloud 与 is_temporal=true", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.change(screen.getByPlaceholderText(/商品检测训练集/), {
      target: { value: "点云时序集" },
    });
    // 切到 3D 点云 → 时序开关出现 → 勾选
    fireEvent.click(screen.getByRole("button", { name: /3D 点云/ }));
    fireEvent.click(screen.getByLabelText(/声明为时序数据集/));
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));
    await waitFor(() => {
      expect(mockCreateDatasetMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ data_type: "point_cloud", is_temporal: true }),
      );
    });
  });

  it("图片类型也显示时序开关, 勾选后发送 is_temporal", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.change(screen.getByPlaceholderText(/商品检测训练集/), {
      target: { value: "图片时序集" },
    });
    // 图片(默认 data_type)同样有时序开关——图片 ZIP 也会产生 scene
    fireEvent.click(screen.getByLabelText(/声明为时序数据集/));
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));
    await waitFor(() => {
      expect(mockCreateDatasetMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ data_type: "image", is_temporal: true }),
      );
    });
  });

  it("未勾选时序时 is_temporal 不发送 (undefined)", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.change(screen.getByPlaceholderText(/商品检测训练集/), {
      target: { value: "普通图片集" },
    });
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));
    await waitFor(() => {
      expect(mockCreateDatasetMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ is_temporal: undefined }),
      );
    });
  });

  it("其他/多模态类型不显示时序开关", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    // 默认 image 时开关存在
    expect(screen.getByLabelText(/声明为时序数据集/)).toBeInTheDocument();
    // 切到「多模态」后开关消失
    fireEvent.click(screen.getByRole("button", { name: /多模态/ }));
    expect(screen.queryByLabelText(/声明为时序数据集/)).not.toBeInTheDocument();
    // 切到「其他」同样无开关
    fireEvent.click(screen.getByRole("button", { name: /其他/ }));
    expect(screen.queryByLabelText(/声明为时序数据集/)).not.toBeInTheDocument();
  });

  it("勾选时序后切换数据类型会重置 isTemporal (再切回不发送 is_temporal)", async () => {
    renderUI();
    pickFile();
    await waitFor(() => expect(screen.getByText(/已选 1 个文件/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.change(screen.getByPlaceholderText(/商品检测训练集/), {
      target: { value: "重置测试集" },
    });
    // image 下勾选时序
    fireEvent.click(screen.getByLabelText(/声明为时序数据集/));
    // 切到视频(同样显示开关)——切换应重置为未勾选
    fireEvent.click(screen.getByRole("button", { name: /视频/ }));
    const checkbox = screen.getByLabelText(/声明为时序数据集/) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));
    await waitFor(() => {
      expect(mockCreateDatasetMutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({ data_type: "video", is_temporal: undefined }),
      );
    });
  });

  it("open=false 时 wizard 不渲染内容", () => {
    renderUI({ open: false });
    expect(screen.queryByText("选择来源")).not.toBeInTheDocument();
  });
});
