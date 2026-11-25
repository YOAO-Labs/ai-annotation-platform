import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import type {
  DirectPluginProbeResponse,
  PluginInstallationResponse,
  PluginQuiesceResponse,
} from "@/api/adminInferencePlugins";

const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

const mockList = vi.fn();
const mockRegisterDirect = vi.fn();
const mockProbe = vi.fn();
const mockActivateDirect = vi.fn();
const mockDisable = vi.fn();
const mockQuiesce = vi.fn();
const mockRetire = vi.fn();
const mockCancelPrepared = vi.fn();

vi.mock("@/api/adminInferencePlugins", async () => {
  const actual = await vi.importActual<typeof import("@/api/adminInferencePlugins")>(
    "@/api/adminInferencePlugins",
  );
  return {
    ...actual,
    adminInferencePluginsApi: {
      list: () => mockList(),
      get: vi.fn(),
      registerDirect: (...args: unknown[]) => mockRegisterDirect(...args),
      probe: (...args: unknown[]) => mockProbe(...args),
      activateDirect: (...args: unknown[]) => mockActivateDirect(...args),
      disable: (...args: unknown[]) => mockDisable(...args),
      quiesce: (...args: unknown[]) => mockQuiesce(...args),
      retire: (...args: unknown[]) => mockRetire(...args),
      cancelPrepared: (...args: unknown[]) => mockCancelPrepared(...args),
    },
  };
});

import { InferencePluginInstallationsSection } from "./InferencePluginInstallationsSection";

function makeDirectInstallation(
  overrides: {
    installation_id?: string;
    registry_id?: string;
    status?: "prepared" | "active" | "disabled" | "retired";
    revision?: number;
    plugin_id?: string;
    display_name?: string | null;
    source_tier?: "official" | "third_party";
    device?: "cpu" | "cuda";
    includeSummary?: boolean;
  } = {},
): PluginInstallationResponse {
  const installation_id = overrides.installation_id ?? "inst-direct-1";
  const registry_id = overrides.registry_id ?? "reg-direct-1";
  const status = overrides.status ?? "active";
  const revision = overrides.revision ?? 2;
  const plugin_id = overrides.plugin_id ?? "remote-ocr-direct";
  const display_name =
    overrides.display_name === undefined ? "Remote OCR 直连" : overrides.display_name;
  const source_tier = overrides.source_tier ?? "third_party";
  const device = overrides.device ?? "cuda";

  const base: PluginInstallationResponse = {
    registry_id,
    installation: {
      installation_id,
      status,
      revision,
      execution_mode: "direct",
      source_tier,
      resource: {
        gpu_resource_id: null,
        physical_gpu_uuid: null,
        logical_device: device === "cuda" ? "cuda:0" : "cpu",
      },
      manifest: {
        manifest_schema: "yoao-extension-manifest/v1",
        protocol_version: "yoao-inference/v2",
        execution_mode: "direct",
        plugin_id,
        version: "1.2.0",
        display_name,
        description: "远端 GPU 直连 OCR 推理服务",
        homepage: "https://plugins.example.com/ocr",
        publisher: "Example Lab",
        license: "Apache-2.0",
        capabilities: ["inference", "ocr", "managed_models", "managed_evaluation"],
        models: [
          {
            id: "ocr-v2",
            task: "ocr",
            is_interactive: false,
            supported_prompts: ["full_image"],
            supported_inputs: ["image"],
            supported_geometric_outputs: ["polygon"],
          },
        ],
        pools: {
          default: { cap: 2 },
        },
        resource: {
          device,
          memory_mb: null,
          gpu_memory_mb: 0,
        },
        managed_model_compatibility: [
          {
            framework: "paddleocr",
            format: "onnx",
            runtime_device: device,
          },
        ],
        allowed_asset_sources: [],
        artifact: null,
        asset_manifest_sha256: null,
      },
    },
    summary:
      overrides.includeSummary === false
        ? null
        : {
            extension_point: "inference",
            plugin_id,
            version: "1.2.0",
            display_name,
            description: "远端 GPU 直连 OCR 推理服务",
            homepage: "https://plugins.example.com/ocr",
            source_tier,
            execution_mode: "direct",
            status,
            device,
            locally_arbitrated_gpu: false,
          },
  };
  return base;
}

function makeContainerInstallation(
  overrides: {
    installation_id?: string;
    registry_id?: string;
    status?: "prepared" | "active" | "disabled" | "retired";
    revision?: number;
  } = {},
): PluginInstallationResponse {
  const installation_id = overrides.installation_id ?? "inst-container-1";
  const registry_id = overrides.registry_id ?? "reg-container-1";
  const status = overrides.status ?? "disabled";
  const revision = overrides.revision ?? 3;
  return {
    registry_id,
    installation: {
      installation_id,
      status,
      revision,
      execution_mode: "container",
      source_tier: "official",
      resource: {
        gpu_resource_id: "node-a/index:0",
        physical_gpu_uuid: "GPU-11111111-2222-3333-4444-555555555555",
        logical_device: "cuda:0",
      },
      manifest: {
        manifest_schema: "yoao-extension-manifest/v1",
        protocol_version: "yoao-inference/v2",
        execution_mode: "container",
        plugin_id: "official-detector",
        version: "2.0.0",
        display_name: "官方检测容器插件",
        description: "受管 OCI 容器推理服务",
        homepage: "https://yoao.example.com/detector",
        publisher: "YOAO",
        license: "MIT",
        artifact: `ghcr.io/yoao/detector@sha256:${"a".repeat(64)}`,
        asset_manifest_sha256: "b".repeat(64),
        entrypoint: ["yoao-detector", "serve"],
        capabilities: ["inference", "detection", "managed_models"],
        models: [
          {
            id: "det-s",
            task: "detect",
            is_interactive: false,
            supported_prompts: ["full_image"],
            supported_inputs: ["image"],
            supported_geometric_outputs: ["bbox"],
          },
          {
            id: "det-l",
            task: "detect",
            is_interactive: false,
            supported_prompts: ["full_image"],
            supported_inputs: ["image"],
            supported_geometric_outputs: ["bbox"],
          },
        ],
        pools: {
          detect_pool: { cap: 4 },
        },
        resource: {
          device: "cuda",
          memory_mb: 4096,
          gpu_memory_mb: 6144,
        },
        managed_model_compatibility: [
          {
            framework: "libreyolo",
            format: null,
            runtime_device: "cuda",
          },
        ],
        allowed_asset_sources: ["huggingface"],
      },
    },
    summary: {
      extension_point: "inference",
      plugin_id: "official-detector",
      version: "2.0.0",
      display_name: "官方检测容器插件",
      description: "受管 OCI 容器推理服务",
      homepage: "https://yoao.example.com/detector",
      source_tier: "official",
      execution_mode: "container",
      status,
      device: "cuda",
      locally_arbitrated_gpu: true,
    },
  };
}

function renderSection(props?: { onChanged?: () => void }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <InferencePluginInstallationsSection onChanged={props?.onChanged} />
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient, invalidateSpy };
}

describe("InferencePluginInstallationsSection", () => {
  beforeEach(() => {
    mockPushToast.mockReset();
    mockList.mockReset();
    mockRegisterDirect.mockReset();
    mockProbe.mockReset();
    mockActivateDirect.mockReset();
    mockDisable.mockReset();
    mockQuiesce.mockReset();
    mockRetire.mockReset();
    mockCancelPrepared.mockReset();
  });

  it("同时展示直连与容器插件摘要徽章、非本机仲裁与模型能力，并支持 summary 回退", async () => {
    mockList.mockResolvedValue([
      makeDirectInstallation({ includeSummary: false }),
      makeContainerInstallation(),
    ]);

    renderSection();

    const directRow = (await screen.findByText("Remote OCR 直连")).closest("tr")!;
    expect(within(directRow).getByText("第三方")).toBeInTheDocument();
    expect(within(directRow).getByText("直连")).toBeInTheDocument();
    expect(within(directRow).getByText("已启用")).toBeInTheDocument();
    expect(within(directRow).getByText("非本机仲裁")).toBeInTheDocument();
    expect(within(directRow).getByText("1 个模型")).toBeInTheDocument();
    expect(within(directRow).getByText("受控模型")).toBeInTheDocument();
    expect(within(directRow).getByText("受控评估")).toBeInTheDocument();

    const containerRow = screen.getByText("官方检测容器插件").closest("tr")!;
    expect(within(containerRow).getByText("官方")).toBeInTheDocument();
    expect(within(containerRow).getByText("容器")).toBeInTheDocument();
    expect(within(containerRow).getByText("已停用")).toBeInTheDocument();
    expect(within(containerRow).getByText("本机仲裁")).toBeInTheDocument();
    expect(within(containerRow).getByText("2 个模型")).toBeInTheDocument();
    expect(within(containerRow).getByText(/yoao plugins install/)).toBeInTheDocument();
    expect(within(containerRow).getByRole("button", { name: "启用" })).toBeDisabled();
    expect(within(containerRow).getByRole("button", { name: "排空" })).toBeDisabled();
  });

  it("按运行模式、状态与关键字筛选插件列表并支持清除条件", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValue([
      makeDirectInstallation({
        installation_id: "d-active",
        plugin_id: "direct-ocr",
        display_name: "Direct OCR",
        status: "active",
      }),
      makeDirectInstallation({
        installation_id: "d-retired",
        plugin_id: "direct-legacy",
        display_name: "Direct Legacy",
        status: "retired",
      }),
      makeContainerInstallation({
        installation_id: "c-disabled",
        status: "disabled",
      }),
    ]);

    renderSection();
    await screen.findByText("Direct OCR");

    await user.selectOptions(screen.getByLabelText("按运行模式筛选插件"), "direct");
    expect(screen.getByText("Direct OCR")).toBeInTheDocument();
    expect(screen.getByText("Direct Legacy")).toBeInTheDocument();
    expect(screen.queryByText("官方检测容器插件")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("按状态筛选插件"), "retired");
    expect(screen.queryByText("Direct OCR")).not.toBeInTheDocument();
    expect(screen.getByText("Direct Legacy")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "清除条件" }));
    expect(screen.getByText("Direct OCR")).toBeInTheDocument();
    expect(screen.getByText("官方检测容器插件")).toBeInTheDocument();

    await user.type(screen.getByLabelText("搜索插件"), "official-detector");
    expect(screen.getByText("官方检测容器插件")).toBeInTheDocument();
    expect(screen.queryByText("Direct OCR")).not.toBeInTheDocument();
  });

  it("通过注册直连插件弹窗校验 Token 长度、受控模型兼容性授权并在成功时提交且清空密钥", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    mockList.mockResolvedValue([]);
    const created = makeDirectInstallation({
      installation_id: "inst-new",
      plugin_id: "custom-seg",
      display_name: "自定义分割直连",
    });
    mockRegisterDirect.mockResolvedValue(created);

    const { invalidateSpy } = renderSection({ onChanged });
    await screen.findByText("尚无已安装的推理插件");

    await user.click(screen.getByRole("button", { name: /注册直连插件/ }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText("名称"), "seg-direct-1");
    await user.type(within(dialog).getByLabelText("服务地址"), "http://127.0.0.1:8090");
    await user.type(within(dialog).getByLabelText("访问令牌"), "too-short-token");
    await user.click(within(dialog).getByRole("button", { name: "注册直连插件" }));

    expect(await within(dialog).findByText(/访问令牌至少需要 32 个字符/)).toBeInTheDocument();
    expect(mockRegisterDirect).not.toHaveBeenCalled();

    const secretToken = "s".repeat(36);
    await user.clear(within(dialog).getByLabelText("访问令牌"));
    await user.type(within(dialog).getByLabelText("访问令牌"), secretToken);
    await user.selectOptions(within(dialog).getByLabelText("来源层级"), "official");

    await user.click(within(dialog).getByRole("button", { name: /更多元数据与高级授权/ }));
    const evalCheckbox = within(dialog).getByRole("checkbox", {
      name: /批准受控评估/,
    });
    expect(evalCheckbox).toBeDisabled();

    await user.type(within(dialog).getByLabelText("显示名称（可选）"), "自定义分割直连");
    await user.type(within(dialog).getByLabelText("主页地址（可选）"), "https://example.com/seg");
    await user.click(within(dialog).getByRole("button", { name: /添加受控模型兼容项/ }));
    await user.type(within(dialog).getByLabelText("框架 #1"), "customseg");
    await user.type(within(dialog).getByLabelText("格式（可空）"), "onnx");
    await user.selectOptions(within(dialog).getByLabelText("运行设备"), "cuda");

    expect(evalCheckbox).not.toBeDisabled();
    await user.click(evalCheckbox);

    await user.click(within(dialog).getByRole("button", { name: "注册直连插件" }));

    await waitFor(() => {
      expect(mockRegisterDirect).toHaveBeenCalledWith({
        name: "seg-direct-1",
        url: "http://127.0.0.1:8090",
        auth_token: secretToken,
        source_tier: "official",
        display_name: "自定义分割直连",
        homepage: "https://example.com/seg",
        approved_managed_model_compatibility: [
          { framework: "customseg", format: "onnx", runtime_device: "cuda" },
        ],
        approved_managed_evaluation: true,
      });
    });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "plugin-installations"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "topology"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "all"],
    });

    await user.click(screen.getByRole("button", { name: /注册直连插件/ }));
    const reopened = await screen.findByRole("dialog");
    expect(within(reopened).getByLabelText("访问令牌")).toHaveValue("");
  });

  it("注册失败时清晰展示白名单与本机 GPU 冲突错误且绝不回显令牌", async () => {
    const user = userEvent.setup();
    mockList.mockResolvedValue([]);
    const secretToken = "super-secret-direct-plugin-token-9876543210";

    mockRegisterDirect
      .mockRejectedValueOnce(
        new ApiError(422, "host denied", {
          error_code: "plugin_installation_host_denied",
          message: "loopback host not allowlisted",
        }),
      )
      .mockRejectedValueOnce(
        new ApiError(409, "gpu collision", {
          error_code: "plugin_direct_gpu_local_collision",
          message: "GPU-abc collides with local arbiter",
        }),
      );

    renderSection();
    await screen.findByText("尚无已安装的推理插件");
    await user.click(screen.getByRole("button", { name: /注册直连插件/ }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText("名称"), "local-direct");
    await user.type(within(dialog).getByLabelText("服务地址"), "http://127.0.0.1:9001");
    await user.type(within(dialog).getByLabelText("访问令牌"), secretToken);

    await user.click(within(dialog).getByRole("button", { name: "注册直连插件" }));
    const alertFirst = await within(dialog).findByRole("alert");
    expect(alertFirst).toHaveTextContent(/出网安全白名单拒绝/);
    expect(alertFirst).toHaveTextContent(/loopback host not allowlisted/);
    expect(alertFirst.textContent).not.toContain(secretToken);

    await user.click(within(dialog).getByRole("button", { name: "注册直连插件" }));
    await waitFor(() => {
      const alertSecond = within(dialog).getByRole("alert");
      expect(alertSecond).toHaveTextContent(/命中本机受管 GPU 仲裁资源/);
      expect(alertSecond).toHaveTextContent(/GPU-abc collides with local arbiter/);
      expect(alertSecond.textContent).not.toContain(secretToken);
    });
  });

  it("详情弹窗支持非退役插件只读探活并展示偏差快照，已退役插件禁用探活", async () => {
    const user = userEvent.setup();
    const activeDirect = makeDirectInstallation({
      installation_id: "inst-active",
      display_name: "活跃直连插件",
      status: "active",
    });
    const retiredDirect = makeDirectInstallation({
      installation_id: "inst-retired",
      display_name: "已退役直连插件",
      status: "retired",
    });
    mockList.mockResolvedValue([activeDirect, retiredDirect]);

    const probeSnapshot: DirectPluginProbeResponse = {
      registry_id: activeDirect.registry_id,
      installation_id: activeDirect.installation.installation_id,
      execution_mode: "direct",
      status: "active",
      healthy: true,
      protocol_version: "2.0",
      boot_id: "boot-xyz-999",
      device: "cuda",
      terminal_drain_supported: true,
      observed_models: [
        {
          id: "ocr-v2",
          task: "ocr",
          is_interactive: false,
          supported_prompts: ["full_image"],
          supported_inputs: ["image"],
          supported_geometric_outputs: ["polygon"],
        },
      ],
      observed_pools: { default: { cap: 2 } },
      observed_managed_model_compatibility: [
        { framework: "paddleocr", format: "onnx", runtime_device: "cuda" },
      ],
      observed_managed_evaluation: true,
      matches_manifest: false,
      mismatch_reason: "observed pool cap changed from 2 to 4",
    };
    mockProbe.mockResolvedValue(probeSnapshot);

    renderSection();
    const activeRow = (await screen.findByText("活跃直连插件")).closest("tr")!;
    await user.click(within(activeRow).getByRole("button", { name: /详情 \/ 探活/ }));

    const activeDialog = await screen.findByRole("dialog");
    expect(activeDialog).toHaveTextContent("活跃直连插件");
    const probeButton = within(activeDialog).getByRole("button", { name: "只读探活" });
    expect(probeButton).not.toBeDisabled();
    await user.click(probeButton);

    await waitFor(() => expect(mockProbe).toHaveBeenCalledWith("inst-active"));
    expect(await within(activeDialog).findByText("端点健康")).toBeInTheDocument();
    expect(within(activeDialog).getByText("与清单不一致")).toBeInTheDocument();
    expect(
      within(activeDialog).getByText(/observed pool cap changed from 2 to 4/),
    ).toBeInTheDocument();
    expect(within(activeDialog).getByText("boot-xyz-999")).toBeInTheDocument();
    expect(within(activeDialog).getByText("支持终端排空")).toBeInTheDocument();
    expect(within(activeDialog).getByText("端点声明受控评估")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const retiredRow = screen.getByText("已退役直连插件").closest("tr")!;
    await user.click(within(retiredRow).getByRole("button", { name: "详情" }));
    const retiredDialog = await screen.findByRole("dialog");
    expect(within(retiredDialog).getByRole("button", { name: "只读探活" })).toBeDisabled();
    expect(within(retiredDialog).getByText("已退役的插件安装不支持只读探活。")).toBeInTheDocument();
  });

  it("执行停用、启用、排空、退役与取消预留操作并刷新相关缓存与报错提示", async () => {
    const user = userEvent.setup();
    const activeDirect = makeDirectInstallation({
      installation_id: "inst-active",
      display_name: "活跃直连",
      status: "active",
      revision: 2,
    });
    const disabledDirect = makeDirectInstallation({
      installation_id: "inst-disabled",
      display_name: "停用直连",
      status: "disabled",
      revision: 5,
    });
    const preparedDirect = makeDirectInstallation({
      installation_id: "inst-prepared",
      display_name: "预留直连",
      status: "prepared",
      revision: 1,
    });
    mockList.mockResolvedValue([activeDirect, disabledDirect, preparedDirect]);

    mockDisable.mockResolvedValue(
      makeDirectInstallation({
        installation_id: "inst-active",
        display_name: "活跃直连",
        status: "disabled",
        revision: 3,
      }),
    );
    mockActivateDirect.mockResolvedValue(
      makeDirectInstallation({
        installation_id: "inst-disabled",
        display_name: "停用直连",
        status: "active",
        revision: 6,
      }),
    );
    const quiesceReply: PluginQuiesceResponse = {
      ...disabledDirect,
      safe_to_stop: true,
      launch: null,
    };
    mockQuiesce.mockResolvedValue(quiesceReply);
    mockRetire
      .mockRejectedValueOnce(
        new ApiError(409, "Conflict", {
          error_code: "plugin_installation_in_use",
          active_releases: 2,
          frozen_executions: 1,
        }),
      )
      .mockResolvedValueOnce(
        makeDirectInstallation({
          installation_id: "inst-disabled",
          display_name: "停用直连",
          status: "retired",
          revision: 6,
        }),
      );
    mockCancelPrepared.mockResolvedValue(
      makeDirectInstallation({
        installation_id: "inst-prepared",
        display_name: "预留直连",
        status: "retired",
        revision: 2,
      }),
    );

    const { invalidateSpy } = renderSection();
    const activeRow = (await screen.findByText("活跃直连")).closest("tr")!;
    await user.click(within(activeRow).getByRole("button", { name: "停用" }));
    await waitFor(() => expect(mockDisable).toHaveBeenCalledWith("inst-active", 2));
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "plugin-installations"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "topology"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["admin", "ml-integrations", "all"],
    });

    const disabledRow = screen.getByText("停用直连").closest("tr")!;
    await user.click(within(disabledRow).getByRole("button", { name: "启用" }));
    await waitFor(() => expect(mockActivateDirect).toHaveBeenCalledWith("inst-disabled", 5));

    await user.click(within(disabledRow).getByRole("button", { name: "排空" }));
    await waitFor(() => expect(mockQuiesce).toHaveBeenCalledWith("inst-disabled", 5));
    expect(await screen.findByRole("status")).toHaveTextContent(/safe_to_stop: 是/);

    await user.click(within(disabledRow).getByRole("button", { name: "退役" }));
    const firstRetireDialog = await screen.findByRole("alertdialog");
    await user.click(within(firstRetireDialog).getByRole("button", { name: "确认退役" }));
    await waitFor(() => expect(mockRetire).toHaveBeenCalledWith("inst-disabled", 5));
    expect(await screen.findByRole("alert")).toHaveTextContent(/活跃发布 2，冻结执行 1/);

    await user.click(within(disabledRow).getByRole("button", { name: "退役" }));
    const secondRetireDialog = await screen.findByRole("alertdialog");
    await user.click(within(secondRetireDialog).getByRole("button", { name: "确认退役" }));
    await waitFor(() => expect(mockRetire).toHaveBeenCalledTimes(2));

    const preparedRow = screen.getByText("预留直连").closest("tr")!;
    await user.click(within(preparedRow).getByRole("button", { name: "取消预留" }));
    const cancelDialog = await screen.findByRole("alertdialog");
    await user.click(within(cancelDialog).getByRole("button", { name: "确认取消预留" }));
    await waitFor(() => expect(mockCancelPrepared).toHaveBeenCalledWith("inst-prepared", 1));
  });

  it("排空或启停遇到终端排空必选与修订冲突时展示明确中文提示", async () => {
    const user = userEvent.setup();
    const disabledDirect = makeDirectInstallation({
      installation_id: "inst-disabled",
      display_name: "冲突直连插件",
      status: "disabled",
      revision: 4,
    });
    mockList.mockResolvedValue([disabledDirect]);
    mockQuiesce.mockRejectedValueOnce(
      new ApiError(409, "Conflict", {
        error_code: "plugin_terminal_drain_required",
        message: "Direct plugin requires live terminal drain proof before quiescence.",
      }),
    );
    mockActivateDirect.mockRejectedValueOnce(
      new ApiError(409, "Conflict", {
        error_code: "plugin_installation_conflict",
        message: "Installation state or revision changed; read the installation and retry.",
      }),
    );

    renderSection();
    const row = (await screen.findByText("冲突直连插件")).closest("tr")!;

    await user.click(within(row).getByRole("button", { name: "排空" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/终端排空证明/);

    await user.click(within(row).getByRole("button", { name: "启用" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/状态或修订版本已变更/);
    });
  });
});
