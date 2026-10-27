import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(selector: (state: any) => T) => selector({ push: mockPushToast }),
  };
});

const mockRuntimeHints = vi.fn();
const mockGpuResources = vi.fn();
const mockProbe = vi.fn();
vi.mock("@/api/adminMlIntegrations", () => ({
  adminMlIntegrationsApi: {
    runtimeHints: () => mockRuntimeHints(),
    gpuResources: () => mockGpuResources(),
    probe: (payload: unknown) => mockProbe(payload),
  },
}));

const mockCreate = vi.fn();
const mockUpdate = vi.fn();
vi.mock("./useGlobalRegistry", () => ({
  useCreateRegistry: () => ({ mutateAsync: mockCreate, isPending: false }),
  useUpdateRegistry: () => ({ mutateAsync: mockUpdate, isPending: false }),
}));

import { GlobalBackendFormModal, type GlobalRegistryEditTarget } from "./GlobalBackendFormModal";

const RESOURCE = {
  gpu_resource_id: "node-a/index:0",
  node_id: "node-a",
  physical_device_token: "index:0",
  allocatable_mb: 20_000,
  configured_mode: "observe" as const,
  desired_mode: "observe" as const,
  effective_mode: "observe" as const,
  claimed_budget_mb: 19_000,
  claimed_backend_count: 2,
  status: "warning" as const,
  diagnostics: [],
};

function renderForm(backend?: GlobalRegistryEditTarget | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <GlobalBackendFormModal open backend={backend} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText("名称"), {
    target: { value: "grounded-sam2" },
  });
  fireEvent.change(screen.getByLabelText("URL"), {
    target: { value: "http://172.17.0.1:8001" },
  });
}

describe("GlobalBackendFormModal GPU claim", () => {
  beforeEach(() => {
    mockPushToast.mockReset();
    mockRuntimeHints.mockReset();
    mockGpuResources.mockReset();
    mockProbe.mockReset();
    mockCreate.mockReset();
    mockUpdate.mockReset();
    mockRuntimeHints.mockResolvedValue({ ml_backend_default_url: null });
    mockGpuResources.mockResolvedValue({
      global_desired_mode: "observe",
      runtime_ready: true,
      observe_runtime_ready: true,
      enforce_runtime_ready: false,
      resources: [RESOURCE],
      diagnostics: [],
    });
    mockCreate.mockResolvedValue({});
    mockUpdate.mockResolvedValue({});
  });

  it("创建时提交单卡 claim、预算与允许为负的驱逐优先级", async () => {
    renderForm();
    fillRequiredFields();
    await screen.findByRole("option", { name: /node-a\/index:0/ });
    fireEvent.change(screen.getByLabelText("物理 GPU 资源"), {
      target: { value: RESOURCE.gpu_resource_id },
    });
    fireEvent.change(screen.getByLabelText("显存预算（MiB）"), {
      target: { value: "2000" },
    });
    fireEvent.change(screen.getByLabelText("驱逐优先级"), {
      target: { value: "-2" },
    });

    expect(screen.getByText(/弹性超售告警/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    await waitFor(() => {
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          gpu_resource_id: "node-a/index:0",
          vram_budget_mb: 2000,
          eviction_priority: -2,
        }),
      );
    });
  });

  it("单 backend 预算超过 allocatable 时本地阻止提交", async () => {
    renderForm();
    fillRequiredFields();
    await screen.findByRole("option", { name: /node-a\/index:0/ });
    fireEvent.change(screen.getByLabelText("物理 GPU 资源"), {
      target: { value: RESOURCE.gpu_resource_id },
    });
    fireEvent.change(screen.getByLabelText("显存预算（MiB）"), {
      target: { value: "20001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    expect(await screen.findByText(/超过该卡可分配容量/)).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
  });

  it("编辑时清除资源会成对发送 null", async () => {
    renderForm({
      id: "backend-1",
      name: "grounded-sam2",
      url: "http://172.17.0.1:8001",
      auth_method: "none",
      gpu_resource_id: RESOURCE.gpu_resource_id,
      vram_budget_mb: 8000,
      eviction_priority: 3,
    });
    fireEvent.change(await screen.findByLabelText("物理 GPU 资源"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({
        id: "backend-1",
        payload: expect.objectContaining({
          gpu_resource_id: null,
          vram_budget_mb: null,
        }),
      });
    });
  });

  it("未知旧资源保持选中，未修改时不会被静默清空", async () => {
    renderForm({
      id: "backend-legacy",
      name: "legacy",
      url: "http://172.17.0.1:8010",
      auth_method: "none",
      gpu_resource_id: "node-old/index:0",
      vram_budget_mb: 4096,
      eviction_priority: 0,
    });
    expect(await screen.findByText(/当前配置中已不存在/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
    const payload = mockUpdate.mock.calls[0][0].payload;
    expect(payload).not.toHaveProperty("gpu_resource_id");
    expect(payload).not.toHaveProperty("vram_budget_mb");
  });

  it("展示结构化 gpu_config_invalid diagnostics", async () => {
    mockCreate.mockRejectedValue({
      status: 422,
      message: "Unprocessable Entity",
      detailRaw: {
        error_code: "gpu_config_invalid",
        message: "GPU 资源声明无效",
        diagnostics: [
          {
            code: "gpu_resource_unknown",
            level: "blocker",
            field: "gpu_resource_id",
            message: "资源不存在",
          },
        ],
      },
    });
    renderForm();
    fillRequiredFields();
    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    expect(await screen.findByText("gpu_resource_id：资源不存在")).toBeInTheDocument();
  });

  it.each([false, true])("首次配置 Token 认证不能留空（编辑=%s）", async (editing) => {
    renderForm(
      editing
        ? {
            id: "backend-auth",
            name: "yolo",
            url: "http://172.17.0.1:8003",
            auth_method: "none",
            gpu_resource_id: null,
            vram_budget_mb: null,
            eviction_priority: 0,
          }
        : undefined,
    );
    if (!editing) fillRequiredFields();
    fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "token" } });
    fireEvent.click(screen.getByRole("button", { name: editing ? "保存" : "注册" }));

    expect(
      await screen.findByText("请填写服务端使用的 Token；留空不会自动生成。"),
    ).toBeInTheDocument();
    expect(mockCreate).not.toHaveBeenCalled();
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("展示手动认证说明，填写后发送令牌", async () => {
    renderForm();
    fillRequiredFields();
    fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "token" } });
    expect(screen.getByText(/本机|内置服务可改选/)).toBeInTheDocument();
    expect(screen.getByText(/YOLO_MANAGED_MODELS_TOKEN/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: "a".repeat(64) } });
    fireEvent.click(screen.getByRole("button", { name: "注册" }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          auth_method: "token",
          auth_token: "a".repeat(64),
        }),
      ),
    );
  });

  it("系统托管使用部署指定地址且不要求填写或发送 Token", async () => {
    mockRuntimeHints.mockResolvedValue({
      ml_backend_default_url: null,
      managed_backend_urls: { yolo: "http://172.17.0.1:8003" },
    });
    renderForm();
    fillRequiredFields();
    fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "managed:yolo" } });
    fireEvent.click(await screen.findByRole("button", { name: "使用部署指定的服务地址" }));
    expect(screen.queryByLabelText("Token")).not.toBeInTheDocument();
    expect(screen.getByText(/普通健康检查不代表认证已就绪/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /测试连接/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "注册" }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          url: "http://172.17.0.1:8003",
          auth_method: "managed:yolo",
        }),
      ),
    );
    expect(mockCreate.mock.calls[0][0].auth_token).toBeUndefined();
  });

  it("部署提示晚到时为新内置地址补上默认认证，保留用户显式选择", async () => {
    let resolveHints!: (value: unknown) => void;
    mockRuntimeHints.mockReturnValue(
      new Promise((resolve) => {
        resolveHints = resolve;
      }),
    );
    renderForm();
    fillRequiredFields();
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "http://172.17.0.1:8003" } });
    resolveHints({
      ml_backend_default_url: null,
      managed_backend_urls: { yolo: "http://172.17.0.1:8003" },
    });
    await waitFor(() => expect(screen.getByLabelText("认证方式")).toHaveValue("managed:yolo"));
    fireEvent.change(screen.getByLabelText("认证方式"), { target: { value: "none" } });
    fireEvent.change(screen.getByLabelText("URL"), {
      target: { value: "http://172.17.0.1:8003/" },
    });
    expect(screen.getByLabelText("认证方式")).toHaveValue("none");
  });

  it("已经使用 Token 的实例留空编辑时不覆盖已保存令牌", async () => {
    renderForm({
      id: "backend-auth",
      name: "yolo",
      url: "http://172.17.0.1:8003",
      auth_method: "token",
      gpu_resource_id: null,
      vram_budget_mb: null,
      eviction_priority: 0,
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
    expect(mockUpdate.mock.calls[0][0].payload).not.toHaveProperty("auth_token");
  });
});
