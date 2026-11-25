import { ApiError, apiClient } from "./client";
import type {
  DirectPluginProbeResponse,
  InferencePluginSummary,
  ManagedModelCompatibility,
  PluginActivateRequest,
  PluginInstallation,
  PluginInstallationCommand,
  PluginInstallationResponse,
  PluginLaunchReceipt,
  PluginManifest,
  PluginModelCapability,
  PluginPoolTopology,
  PluginQuiesceResponse,
  PluginResourceBinding,
  RegisterDirectPluginRequest,
} from "./generated/types.gen";

export type {
  DirectPluginProbeResponse,
  InferencePluginSummary,
  ManagedModelCompatibility,
  PluginActivateRequest,
  PluginInstallation,
  PluginInstallationCommand,
  PluginInstallationResponse,
  PluginLaunchReceipt,
  PluginManifest,
  PluginModelCapability,
  PluginPoolTopology,
  PluginQuiesceResponse,
  PluginResourceBinding,
  RegisterDirectPluginRequest,
};

export const INFERENCE_PLUGIN_INSTALLATIONS_QUERY_KEY = [
  "admin",
  "ml-integrations",
  "plugin-installations",
] as const;

export const adminInferencePluginsApi = {
  list: () =>
    apiClient.get<PluginInstallationResponse[]>("/admin/ml-integrations/plugins/installations"),
  get: (id: string) =>
    apiClient.get<PluginInstallationResponse>(`/admin/ml-integrations/plugins/installations/${id}`),
  registerDirect: (payload: RegisterDirectPluginRequest) =>
    apiClient.post<PluginInstallationResponse>("/admin/ml-integrations/plugins/direct", payload),
  probe: (id: string) =>
    apiClient.post<DirectPluginProbeResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/probe`,
    ),
  activateDirect: (id: string, expectedRevision: number) =>
    apiClient.post<PluginInstallationResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/activate`,
      { expected_revision: expectedRevision } satisfies PluginActivateRequest,
    ),
  disable: (id: string, expectedRevision: number) =>
    apiClient.post<PluginInstallationResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/disable`,
      { expected_revision: expectedRevision } satisfies PluginInstallationCommand,
    ),
  quiesce: (id: string, expectedRevision: number) =>
    apiClient.post<PluginQuiesceResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/quiesce`,
      { expected_revision: expectedRevision } satisfies PluginInstallationCommand,
    ),
  retire: (id: string, expectedRevision: number) =>
    apiClient.post<PluginInstallationResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/retire`,
      { expected_revision: expectedRevision } satisfies PluginInstallationCommand,
    ),
  cancelPrepared: (id: string, expectedRevision: number) =>
    apiClient.post<PluginInstallationResponse>(
      `/admin/ml-integrations/plugins/installations/${id}/cancel-prepared`,
      { expected_revision: expectedRevision } satisfies PluginInstallationCommand,
    ),
};

export function resolveInferencePluginSummary(
  item: PluginInstallationResponse,
): InferencePluginSummary {
  if (item.summary) {
    return item.summary;
  }
  const inst = item.installation;
  const executionMode = inst.execution_mode ?? inst.manifest.execution_mode ?? "container";
  const device = inst.manifest.resource.device;
  return {
    extension_point: "inference",
    plugin_id: inst.manifest.plugin_id,
    version: inst.manifest.version,
    display_name: inst.manifest.display_name ?? null,
    description: inst.manifest.description ?? null,
    homepage: inst.manifest.homepage ?? null,
    source_tier: inst.source_tier ?? "third_party",
    execution_mode: executionMode,
    status: inst.status,
    device,
    locally_arbitrated_gpu:
      inst.resource.gpu_resource_id != null || (executionMode !== "direct" && device === "cuda"),
  };
}

interface ParsedPluginErrorDetail {
  errorCode?: string;
  message?: string;
  reason?: string;
  activeReleases?: number;
  frozenExecutions?: number;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

function readStringField(record: Record<string, unknown> | null, key: string): string | undefined {
  const raw = record?.[key];
  return typeof raw === "string" && raw.trim().length > 0 ? raw.trim() : undefined;
}

function readNumberField(record: Record<string, unknown> | null, key: string): number | undefined {
  const raw = record?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : undefined;
}

export function extractInferencePluginErrorDetail(error: unknown): ParsedPluginErrorDetail {
  const rawObject =
    error instanceof ApiError
      ? asRecord(error.detailRaw)
      : asRecord(asRecord(error)?.detailRaw ?? asRecord(error)?.detail);
  const fallbackMessage =
    error instanceof Error
      ? error.message.trim() || undefined
      : readStringField(asRecord(error), "message");

  return {
    errorCode: readStringField(rawObject, "error_code") ?? readStringField(rawObject, "code"),
    message: readStringField(rawObject, "message") ?? fallbackMessage,
    reason: readStringField(rawObject, "reason"),
    activeReleases: readNumberField(rawObject, "active_releases"),
    frozenExecutions: readNumberField(rawObject, "frozen_executions"),
  };
}

function appendDetail(base: string, detail?: string): string {
  if (!detail) return base;
  return `${base}（${detail}）`;
}

export function formatInferencePluginError(error: unknown): string {
  const parsed = extractInferencePluginErrorDetail(error);
  switch (parsed.errorCode) {
    case "plugin_direct_gpu_local_collision":
      return appendDetail(
        "检测到直连插件使用的 GPU UUID 命中本机受管 GPU 仲裁资源，不能以非仲裁直连档注册或启用",
        parsed.message,
      );
    case "plugin_installation_host_denied":
      return appendDetail(
        "目标地址被推理插件出网安全白名单拒绝。私网或本机回环地址需先配置 INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST 或 INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST",
        parsed.message,
      );
    case "plugin_installation_in_use": {
      const parts: string[] = [];
      if (parsed.activeReleases !== undefined) parts.push(`活跃发布 ${parsed.activeReleases}`);
      if (parsed.frozenExecutions !== undefined) parts.push(`冻结执行 ${parsed.frozenExecutions}`);
      return appendDetail(
        "该插件安装仍被模型发布或冻结执行引用，无法退役或取消预留",
        parts.length > 0 ? parts.join("，") : parsed.message,
      );
    }
    case "plugin_endpoint_mismatch":
      return appendDetail("端点实时探活结果与插件安装清单或已批准能力不匹配", parsed.message);
    case "plugin_terminal_drain_required":
      return appendDetail(
        "该直连插件声明了终端排空能力，需在端点可达且返回终端排空证明后才能排空",
        parsed.message,
      );
    case "plugin_installation_conflict":
      return "插件安装状态或修订版本已变更，请刷新列表后重试。";
    case "plugin_installation_retired":
      return "已退役的插件安装不支持只读探活。";
    case "plugin_endpoint_unavailable":
      return appendDetail("无法连接推理插件端点，请检查服务状态与网络可达性", parsed.message);
    case "plugin_installation_invalid_endpoint":
      return "插件端点地址或访问令牌格式不符合要求。";
    case "plugin_routing_replacement_rejected":
    case "plugin_routing_control_rejected":
      return appendDetail("推理路由替换或控制请求被拒绝", parsed.reason ?? parsed.message);
    case "plugin_routing_control_unavailable":
    case "plugin_routing_control_incomplete":
    case "inference_backend_control_unavailable":
      return "推理路由或后端控制通道暂不可用，请稍后重试。";
    case "plugin_launch_unrecorded":
      return "容器插件尚未记录启动收据，请通过 yoao plugins CLI 执行。";
    case "plugin_launch_receipt_required":
      return "容器插件激活需要通过 yoao plugins install CLI 提供启动收据。";
    case "plugin_direct_receipt_unexpected":
      return "直连插件激活不接受容器启动收据或资格报告。";
    default:
      return parsed.message ?? "插件操作失败，请稍后重试。";
  }
}
