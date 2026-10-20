import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.19.0 · ADR-0044 · superadmin 全局 ML backend 注册/编辑弹窗。
 *
 * 字段：name / url / 认证 / GPU 静态 claim / max_concurrency / extra_params + 测试连接，
 * 调用全局注册表端点：新建 = createRegistry，编辑 = updateRegistry。
 */
import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  adminMlIntegrationsApi,
  type GPUArbiterResourceItem,
  type MLBackendAuthMethod,
  type GPUConfigDiagnostic,
  type ProbeResponse,
  type MLBackendRegistryCreatePayload,
  type MLBackendRegistryUpdatePayload,
} from "@/api/adminMlIntegrations";
import { useCreateRegistry, useUpdateRegistry } from "./useGlobalRegistry";

/** 编辑模式入参：来自 GlobalBackendItem 的最小子集（不含 extra_params）。 */
export interface GlobalRegistryEditTarget {
  id: string;
  name: string;
  url: string;
  auth_method: string;
  gpu_resource_id: string | null;
  vram_budget_mb: number | null;
  eviction_priority: number;
}

interface Props {
  open: boolean;
  /** 提供则进入编辑模式 */
  backend?: GlobalRegistryEditTarget | null;
  onClose: () => void;
}

const LABEL_CLASS = "mb-1.5 block text-xs font-medium text-muted-foreground";
const INPUT_CLASS =
  "box-border w-full appearance-none rounded-md border border-border bg-muted px-2.5 py-2 text-sm text-foreground outline-none [font-family:inherit]";
const MONO_INPUT_CLASS = "font-mono text-xs";
const HELP_CLASS = "mt-1 text-2xs text-muted-foreground";
const MANAGED_LABELS: Record<string, string> = {
  gsam2: "Grounded SAM 2",
  sam3: "SAM 3",
  yolo: "YOLO",
  onnxtools: "ONNXTools",
  rapidocr: "RapidOCR",
};

function resourceOptionLabel(resource: GPUArbiterResourceItem) {
  return `${resource.gpu_resource_id} · ${resource.claimed_budget_mb}/${resource.allocatable_mb} MiB · ${resource.desired_mode}→${resource.effective_mode}`;
}

export function GlobalBackendFormModal({ open, backend, onClose }: Props) {
  const isEdit = !!backend;
  const pushToast = useToastStore((s) => s.push);
  const create = useCreateRegistry();
  const update = useUpdateRegistry();

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [authMethod, setAuthMethod] = useState<MLBackendAuthMethod>("none");
  const [authToken, setAuthToken] = useState("");
  const [authTouched, setAuthTouched] = useState(false);
  const [maxConcurrency, setMaxConcurrency] = useState("");
  const [extraText, setExtraText] = useState("");
  const [extraOpen, setExtraOpen] = useState(false);
  const [gpuResourceId, setGpuResourceId] = useState("");
  const [vramBudgetMb, setVramBudgetMb] = useState("");
  const [evictionPriority, setEvictionPriority] = useState("0");
  const [gpuDiagnostics, setGpuDiagnostics] = useState<GPUConfigDiagnostic[]>([]);
  const [error, setError] = useState<string | null>(null);

  const hintsQ = useQuery({
    queryKey: ["admin", "ml-integrations", "runtime-hints"],
    queryFn: () => adminMlIntegrationsApi.runtimeHints(),
    staleTime: Infinity,
    enabled: open,
  });
  const managedUrls = hintsQ.data?.managed_backend_urls ?? {};
  const managedProvider = authMethod.startsWith("managed:") ? authMethod.slice(8) : null;
  const managedUrl = managedProvider ? managedUrls[managedProvider] : undefined;
  const urlPlaceholder = hintsQ.data?.ml_backend_default_url ?? "http://172.17.0.1:8001";
  const resourcesQ = useQuery({
    queryKey: ["admin", "ml-integrations", "gpu-resources"],
    queryFn: () => adminMlIntegrationsApi.gpuResources(),
    staleTime: 30_000,
    enabled: open,
  });
  const resources = resourcesQ.data?.resources ?? [];
  const selectedResource = resources.find((resource) => resource.gpu_resource_id === gpuResourceId);
  const hasUnknownCurrentResource =
    !!gpuResourceId &&
    !resourcesQ.isLoading &&
    !selectedResource &&
    backend?.gpu_resource_id === gpuResourceId;

  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<ProbeResponse | null>(null);

  useEffect(() => {
    if (!open || backend || authTouched || !hintsQ.data?.managed_backend_urls) return;
    const provider = Object.entries(hintsQ.data.managed_backend_urls).find(
      ([, target]) => target.replace(/\/$/, "") === url.replace(/\/$/, ""),
    )?.[0];
    setAuthMethod(provider ? (`managed:${provider}` as MLBackendAuthMethod) : "none");
  }, [open, backend, authTouched, hintsQ.data, url]);

  const onProbe = async () => {
    if (managedProvider) return;
    const trimmedUrl = url.trim();
    if (!trimmedUrl) {
      setProbeResult({ ok: false, latency_ms: 0, error: "请先填 URL" });
      return;
    }
    if (!/^https?:\/\//i.test(trimmedUrl)) {
      setProbeResult({ ok: false, latency_ms: 0, error: "URL 需 http(s):// 开头" });
      return;
    }
    setProbing(true);
    setProbeResult(null);
    try {
      const res = await adminMlIntegrationsApi.probe({
        url: trimmedUrl,
        auth_method: authMethod as "none" | "token",
        auth_token: authMethod === "token" && authToken.trim() ? authToken.trim() : null,
      });
      setProbeResult(res);
    } catch (e) {
      setProbeResult({ ok: false, latency_ms: 0, error: (e as Error).message ?? "请求失败" });
    } finally {
      setProbing(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    setError(null);
    setGpuDiagnostics([]);
    setProbeResult(null);
    setAuthTouched(false);
    if (backend) {
      setName(backend.name);
      setUrl(backend.url);
      setAuthMethod((backend.auth_method as MLBackendAuthMethod) ?? "none");
      setAuthToken("");
      // GlobalBackendItem 不含 extra_params，编辑时留空，提交时不下发以保留后端原值。
      setMaxConcurrency("");
      setExtraText("");
      setExtraOpen(false);
      setGpuResourceId(backend.gpu_resource_id ?? "");
      setVramBudgetMb(backend.vram_budget_mb == null ? "" : String(backend.vram_budget_mb));
      setEvictionPriority(String(backend.eviction_priority));
    } else {
      setName("");
      setUrl("");
      setAuthMethod("none");
      setAuthToken("");
      setMaxConcurrency("");
      setExtraText("");
      setExtraOpen(false);
      setGpuResourceId("");
      setVramBudgetMb("");
      setEvictionPriority("0");
    }
  }, [open, backend]);

  const submitting = create.isPending || update.isPending;
  const parsedBudget = Number(vramBudgetMb);
  const previousBudgetOnResource =
    backend?.gpu_resource_id === gpuResourceId ? (backend.vram_budget_mb ?? 0) : 0;
  const projectedClaimedBudget = selectedResource
    ? selectedResource.claimed_budget_mb -
      previousBudgetOnResource +
      (Number.isInteger(parsedBudget) && parsedBudget > 0 ? parsedBudget : 0)
    : null;
  const projectedOversubscribed =
    selectedResource != null &&
    projectedClaimedBudget != null &&
    projectedClaimedBudget > selectedResource.allocatable_mb;

  const onSubmit = async () => {
    setError(null);
    setGpuDiagnostics([]);
    const trimmedName = name.trim();
    const trimmedUrl = url.trim();
    if (!trimmedName) {
      setError("名称不能为空");
      return;
    }
    if (!trimmedUrl) {
      setError("URL 不能为空");
      return;
    }
    if (!/^https?:\/\//i.test(trimmedUrl)) {
      setError("URL 需以 http:// 或 https:// 开头");
      return;
    }

    const priority = Number(evictionPriority);
    if (!Number.isInteger(priority) || priority < -2_147_483_648 || priority > 2_147_483_647) {
      setError("驱逐优先级需为 32 位整数");
      return;
    }

    let budget: number | null = null;
    if (gpuResourceId) {
      budget = Number(vramBudgetMb);
      if (!Number.isInteger(budget) || budget < 1 || budget > 2_147_483_647) {
        setError("选择 GPU 资源后，显存预算需为正整数 MiB");
        return;
      }
      if (selectedResource && budget > selectedResource.allocatable_mb) {
        setError(
          `显存预算 ${budget} MiB 超过该卡可分配容量 ${selectedResource.allocatable_mb} MiB`,
        );
        return;
      }
    }

    // extra_params：仅当用户填了 textarea 或 max_concurrency 时才构造并下发。
    let extraParams: Record<string, unknown> | undefined;
    if (extraText.trim()) {
      try {
        const parsed = JSON.parse(extraText);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("extra_params 必须是 JSON 对象");
        }
        extraParams = parsed as Record<string, unknown>;
      } catch (e) {
        setError(`extra_params JSON 解析失败：${(e as Error).message}`);
        return;
      }
    }
    if (maxConcurrency.trim()) {
      const mc = Number(maxConcurrency);
      if (!Number.isInteger(mc) || mc < 1 || mc > 32) {
        setError("max_concurrency 需为 1-32 整数");
        return;
      }
      extraParams = { ...(extraParams ?? {}), max_concurrency: mc };
    }

    const token = authMethod === "token" && authToken.trim() ? authToken.trim() : undefined;
    if (authMethod === "token" && !token && backend?.auth_method !== "token") {
      setError("请填写服务端使用的 Token；留空不会自动生成。");
      return;
    }

    try {
      if (isEdit && backend) {
        const payload: MLBackendRegistryUpdatePayload = {
          name: trimmedName,
          url: trimmedUrl,
          auth_method: authMethod,
        };
        if (extraParams !== undefined) payload.extra_params = extraParams;
        if (token) payload.auth_token = token;
        if (
          gpuResourceId !== (backend.gpu_resource_id ?? "") ||
          budget !== backend.vram_budget_mb
        ) {
          payload.gpu_resource_id = gpuResourceId || null;
          payload.vram_budget_mb = budget;
        }
        if (priority !== backend.eviction_priority) payload.eviction_priority = priority;
        await update.mutateAsync({ id: backend.id, payload });
        pushToast({ msg: "已更新全局 backend", kind: "success" });
      } else {
        const payload: MLBackendRegistryCreatePayload = {
          name: trimmedName,
          url: trimmedUrl,
          auth_method: authMethod,
          gpu_resource_id: gpuResourceId || null,
          vram_budget_mb: budget,
          eviction_priority: priority,
        };
        if (extraParams !== undefined) payload.extra_params = extraParams;
        if (token) payload.auth_token = token;
        await create.mutateAsync(payload);
        pushToast({ msg: "已注册全局 backend", kind: "success" });
      }
      onClose();
    } catch (e) {
      const apiErr = e as {
        status?: number;
        message?: string;
        detailRaw?: {
          error_code?: string;
          message?: string;
          diagnostics?: GPUConfigDiagnostic[];
        };
      };
      const detail = apiErr.detailRaw;
      if (apiErr.status === 409) {
        setError(
          detail?.error_code === "gpu_backend_active"
            ? detail.message || "backend 仍有活动负载，请先 drain 并卸载后再修改 GPU claim"
            : detail?.message || apiErr.message || "该 URL 已被注册（不可重复）",
        );
        return;
      }
      if (apiErr.status === 422 && detail?.error_code === "gpu_config_invalid") {
        setGpuDiagnostics(detail.diagnostics ?? []);
        setError(detail.message || "GPU 资源声明无效");
        return;
      }
      setError(apiErr.message ?? "请求失败");
    }
  };

  return (
    <Modal
      open={open}
      onClose={submitting ? () => {} : onClose}
      title={isEdit ? "编辑全局 ML Backend" : "注册全局 ML Backend"}
      width={560}
    >
      <div className="flex flex-col gap-3.5">
        <div>
          <label htmlFor="global-backend-name" className={LABEL_CLASS}>
            名称
          </label>
          <input
            id="global-backend-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如 grounded-sam2-prod"
            maxLength={120}
            className={INPUT_CLASS}
          />
        </div>
        <div>
          <label htmlFor="global-backend-url" className={LABEL_CLASS}>
            URL
          </label>
          <input
            id="global-backend-url"
            value={url}
            onChange={(e) => {
              const nextUrl = e.target.value;
              setUrl(nextUrl);
              if (!backend && !authTouched) {
                const provider = Object.entries(managedUrls).find(
                  ([, target]) => target.replace(/\/$/, "") === nextUrl.replace(/\/$/, ""),
                )?.[0];
                setAuthMethod(provider ? (`managed:${provider}` as MLBackendAuthMethod) : "none");
              }
              setProbeResult(null);
            }}
            placeholder={urlPlaceholder}
            className={clsx(INPUT_CLASS, MONO_INPUT_CLASS)}
          />
          <div className={HELP_CLASS}>
            后端容器内可达地址。Docker 同主机宿主网常用{" "}
            <span className="font-mono">172.17.0.1</span>。
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              onClick={onProbe}
              disabled={probing || !!managedProvider}
            >
              {probing ? (
                <Icon name="loader2" size={11} className="spin" />
              ) : (
                <Icon name="activity" size={11} />
              )}
              {probing ? "探测中..." : "测试连接"}
            </Button>
            {probeResult && (
              <span
                className={clsx(
                  "inline-flex items-center gap-1 text-xs",
                  probeResult.ok ? "text-status-positive" : "text-status-danger",
                )}
              >
                <Icon name={probeResult.ok ? "check" : "warning"} size={11} />
                {probeResult.ok ? (
                  <>
                    已连接 ({probeResult.latency_ms}ms
                    {probeResult.model_version ? `, ${probeResult.model_version}` : ""})
                  </>
                ) : (
                  <>
                    无法连接：
                    <CopyableError message={probeResult.error ?? "未知错误"} />
                  </>
                )}
              </span>
            )}
          </div>
        </div>
        <div className={HELP_CLASS}>
          <Icon name="sparkles" size={12} className="mr-1 inline text-chart-4" />
          交互能力 / 支持模态（图像 prompt、视频 tracker）将在健康检查时从 backend 自报的{" "}
          <span className="font-mono">/setup</span> 自动探测，无需手填。
        </div>
        <fieldset className="flex flex-col gap-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-semibold text-foreground">GPU 资源声明</legend>
          <div>
            <label htmlFor="global-backend-gpu-resource" className={LABEL_CLASS}>
              物理 GPU 资源
            </label>
            <select
              id="global-backend-gpu-resource"
              value={gpuResourceId}
              onChange={(event) => {
                const next = event.target.value;
                setGpuResourceId(next);
                setGpuDiagnostics([]);
                if (!next) setVramBudgetMb("");
              }}
              className={clsx(INPUT_CLASS, MONO_INPUT_CLASS, "cursor-pointer")}
            >
              <option value="">无 GPU 声明</option>
              {hasUnknownCurrentResource && (
                <option value={gpuResourceId}>{gpuResourceId} · 当前配置中已不存在</option>
              )}
              {resources.map((resource) => (
                <option key={resource.gpu_resource_id} value={resource.gpu_resource_id}>
                  {resourceOptionLabel(resource)}
                </option>
              ))}
            </select>
            <div className={HELP_CLASS}>
              无 GPU 声明不等于已确认使用 CPU。 单个 backend 只能绑定一张稳定物理资源；不会从容器内
              cuda:0 或 URL 自动推断。
            </div>
            {resourcesQ.isLoading && <div className={HELP_CLASS}>正在读取 GPU 资源配置…</div>}
            {resourcesQ.isError && (
              <div className="mt-1 text-2xs text-status-danger">
                GPU 资源配置加载失败：
                <CopyableError message={(resourcesQ.error as Error).message} />
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
            <div>
              <label htmlFor="global-backend-vram-budget" className={LABEL_CLASS}>
                显存预算（MiB）
              </label>
              <input
                id="global-backend-vram-budget"
                type="number"
                min={1}
                max={2_147_483_647}
                value={vramBudgetMb}
                onChange={(event) => {
                  setVramBudgetMb(event.target.value);
                  setGpuDiagnostics([]);
                }}
                placeholder={gpuResourceId ? "例如 8192" : "先选择物理资源"}
                disabled={!gpuResourceId}
                className={INPUT_CLASS}
              />
              <div className={HELP_CLASS}>覆盖允许并存的全部 pool、临时 buffer 与安全余量。</div>
            </div>
            <div>
              <label htmlFor="global-backend-eviction-priority" className={LABEL_CLASS}>
                驱逐优先级
              </label>
              <input
                id="global-backend-eviction-priority"
                type="number"
                min={-2_147_483_648}
                max={2_147_483_647}
                value={evictionPriority}
                onChange={(event) => setEvictionPriority(event.target.value)}
                className={INPUT_CLASS}
              />
              <div className={HELP_CLASS}>越大越难被驱逐；它不是请求排队优先级。</div>
            </div>
          </div>
          {selectedResource && (
            <div className={HELP_CLASS}>
              可分配 {selectedResource.allocatable_mb} MiB · 当前静态声明合计{" "}
              {selectedResource.claimed_budget_mb} MiB · 模式 {selectedResource.desired_mode}→
              {selectedResource.effective_mode}
            </div>
          )}
          {projectedOversubscribed && (
            <div className="text-2xs text-status-caution">
              保存后该卡静态预算合计预计为 {projectedClaimedBudget}{" "}
              MiB，超过可分配容量；这是允许驱逐的弹性超售告警，不会阻止保存。
            </div>
          )}
          {gpuDiagnostics.length > 0 && (
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-2xs text-status-danger">
              {gpuDiagnostics.map((diagnostic, index) => (
                <li key={`${diagnostic.code}-${diagnostic.field ?? ""}-${index}`}>
                  <CopyableError
                    message={`${diagnostic.field ? `${diagnostic.field}：` : ""}${diagnostic.message}`}
                  />
                </li>
              ))}
            </ul>
          )}
        </fieldset>
        <div>
          <label htmlFor="global-backend-auth-method" className={LABEL_CLASS}>
            认证方式
          </label>
          <select
            id="global-backend-auth-method"
            value={authMethod}
            onChange={(e) => {
              setAuthMethod(e.target.value as MLBackendAuthMethod);
              setAuthTouched(true);
              setProbeResult(null);
            }}
            className={clsx(INPUT_CLASS, "cursor-pointer")}
          >
            <option value="none">none（无认证）</option>
            {Object.entries(MANAGED_LABELS).map(([provider, label]) => (
              <option key={provider} value={`managed:${provider}`}>
                系统托管（{label}）
              </option>
            ))}
            <option value="token">token（手动 Bearer Token）</option>
          </select>
          {managedProvider && (
            <div className="mt-2 rounded-md bg-muted p-2.5 text-xs text-muted-foreground">
              <p>
                {backend?.auth_method?.startsWith("managed:")
                  ? "已保存系统托管认证。"
                  : "保存时自动初始化认证。"}
                平台会生成或复用私有凭据，与指定内置服务验证后同步保存，无需复制 Token。
              </p>
              <p className="mt-1">
                部署指定的服务地址：
                <span className="font-mono">{managedUrl || "未配置"}</span>。
                认证在保存时验证；普通健康检查不代表认证已就绪。
              </p>
              <p className="mt-1">
                旧服务需先升级共享目录挂载；已手动配置服务端 Token 的实例请继续使用手动认证。
              </p>
              {managedUrl && url !== managedUrl && (
                <Button
                  size="sm"
                  variant="ghost"
                  className="mt-1"
                  onClick={() => {
                    setUrl(managedUrl!);
                    setProbeResult(null);
                  }}
                >
                  使用部署指定的服务地址
                </Button>
              )}
            </div>
          )}
          {authMethod === "token" && (
            <div className="mt-2">
              <label htmlFor="global-backend-auth-token" className={LABEL_CLASS}>
                Token
              </label>
              <input
                id="global-backend-auth-token"
                type="password"
                value={authToken}
                onChange={(e) => setAuthToken(e.target.value)}
                placeholder={
                  backend?.auth_method === "token"
                    ? "留空仅保留已保存的 Token"
                    : "填写与服务端相同的 Token"
                }
                aria-describedby="global-backend-auth-help"
                className={INPUT_CLASS}
              />
              <div id="global-backend-auth-help" className={HELP_CLASS}>
                用于远程或自行配置的服务，填写与服务端相同的 Token。 YOLO 模型回流要求至少 32
                个字符，并与 YOLO_MANAGED_MODELS_TOKEN 一致。 内置服务可改选「系统托管」自动初始化。
                {backend?.auth_method === "token" &&
                  "留空仅保留已保存的令牌；若原本未配置，仍需填写。"}
              </div>
            </div>
          )}
        </div>
        <div>
          <label htmlFor="global-backend-max-concurrency" className={LABEL_CLASS}>
            最大并发（max_concurrency）
          </label>
          <input
            id="global-backend-max-concurrency"
            type="number"
            min={1}
            max={32}
            value={maxConcurrency}
            onChange={(e) => setMaxConcurrency(e.target.value)}
            placeholder={isEdit ? "留空保持原值" : "默认 4"}
            className={clsx(INPUT_CLASS, "w-[120px]")}
          />
          <div className={HELP_CLASS}>
            当前仅约束单进程 / 事件循环的同时请求；跨进程全局上限尚未生效。
            {isEdit ? "留空保持原值。" : "留空走默认（4）。"}
          </div>
        </div>
        <div>
          <button
            type="button"
            onClick={() => setExtraOpen((s) => !s)}
            className="inline-flex cursor-pointer appearance-none items-center gap-1 border-0 bg-transparent p-0 text-xs text-muted-foreground [font-family:inherit]"
          >
            <Icon name={extraOpen ? "chevDown" : "chevRight"} size={12} />
            高级 · extra_params (JSON)
          </button>
          {extraOpen && (
            <textarea
              value={extraText}
              onChange={(e) => setExtraText(e.target.value)}
              placeholder='{ "model_size": "large" }'
              rows={4}
              className={clsx(INPUT_CLASS, MONO_INPUT_CLASS, "mt-1.5 resize-y")}
            />
          )}
          {isEdit && <div className={HELP_CLASS}>编辑时留空不会清除后端已有的 extra_params。</div>}
        </div>
        {error && (
          <div className="flex items-start gap-1.5 rounded-md border border-status-danger bg-status-danger-soft px-2.5 py-2 text-xs text-status-danger">
            <Icon name="warning" size={12} className="mt-0.5 flex-shrink-0" />
            <span>
              <CopyableError message={error} />
            </span>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button variant="primary" onClick={onSubmit} disabled={submitting}>
            {submitting ? "提交中..." : isEdit ? "保存" : "注册"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
