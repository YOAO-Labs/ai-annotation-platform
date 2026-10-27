import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.23.4 · unregistered env container card (plan §6.2 / §A.4).
 *
 * Env containers discovered via `/observe` with `registered === false` have no
 * routing state — no routable flag, weight or traffic distribution. This card
 * only shows direct-probe health / latency / compute / GPU / model residency,
 * plus the two allowed actions: "显式注册" (open the registry form) and
 * "试启动" (smoke test). It NEVER shows routable/weight/traffic fields.
 */
import { useState, type ReactNode } from "react";

import { adminMlIntegrationsApi, type ObserveTarget } from "@/api/adminMlIntegrations";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/shadcn/ui/tooltip";
import { isCpuFallback } from "@/utils/mlBackendCompute";
import { NO_METRICS_LABEL } from "../runtimeTopology";

export interface EnvOnlyContainerCardProps {
  target: ObserveTarget;
  /** "显式注册" callback — opens GlobalBackendFormModal / switches to registry tab. */
  onRegister?: (url: string) => void;
}

const CARD_CLASS = "flex flex-col gap-2.5 rounded-md border border-border bg-card p-3";
const URL_CLASS = "mono max-w-[420px] truncate text-xs text-muted-foreground";

export function EnvOnlyContainerCard({ target, onRegister }: EnvOnlyContainerCardProps): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const [busy, setBusy] = useState(false);

  const onSmokeTest = async () => {
    setBusy(true);
    try {
      const res = await adminMlIntegrationsApi.observeSmokeTest({
        url: target.url,
      });
      pushToast({
        msg: res.message,
        kind: res.ok ? "success" : "warning",
        sub: res.error ?? undefined,
      });
    } catch (e) {
      pushToast({ msg: "试启动请求失败", sub: (e as Error).message, kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const compute = target.compute ?? null;
  const gpuInfo = target.gpu_info;
  const pool = target.pool;
  const videoPool = target.video_pool;
  const modelVersion = target.model_version;
  const cacheHitRate = target.cache?.hit_rate;
  const loadedCount =
    pool?.current_size ?? pool?.loaded_keys?.length ?? pool?.loaded_variants?.length ?? 0;
  const videoLoadedCount =
    videoPool?.current_size ??
    videoPool?.loaded_keys?.length ??
    videoPool?.loaded_variants?.length ??
    0;

  return (
    <div className={CARD_CLASS}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={target.ok ? "success" : "danger"} dot>
          {target.ok ? "在线" : "离线"}
        </Badge>
        <span title="来自 ML_BACKEND_OBSERVE_URLS，未注册到全局表">
          <Badge variant="outline">未纳管容器</Badge>
        </span>
        {isCpuFallback(compute) && (
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="warning">⚠ CPU 回退</Badge>
              </TooltipTrigger>
              <TooltipContent side="top">配置了 GPU 但已静默退回 CPU 推理</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        <span className={URL_CLASS}>{target.url}</span>
        {target.ok && <span className="text-xs text-muted-foreground">{target.latency_ms}ms</span>}
      </div>

      {!target.ok ? (
        <div className="text-xs text-status-danger">
          <CopyableError message={target.error ?? "不可达"} />
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          {modelVersion && <span className="mono">{modelVersion}</span>}
          {gpuInfo?.memory_used_mb != null && gpuInfo?.memory_total_mb != null && (
            <span title="整卡已用/总显存">
              GPU{gpuInfo.device_index ?? 0} {gpuInfo.memory_used_mb}/{gpuInfo.memory_total_mb} MB
            </span>
          )}
          {(gpuInfo?.physical_device_token || gpuInfo?.mig_uuid || gpuInfo?.device_uuid) && (
            <span className="inline-flex items-center gap-1">
              设备
              <CopyValueButton
                value={gpuInfo.physical_device_token || gpuInfo.mig_uuid || gpuInfo.device_uuid}
                kind="id"
                label="复制物理设备 ID"
              />
            </span>
          )}
          {cacheHitRate != null && <span>cache {(cacheHitRate * 100).toFixed(1)}%</span>}
          <span>
            图像池 {loadedCount}
            {pool?.cap != null && `/${pool.cap}`}
          </span>
          {videoPool && (
            <span>
              视频池 {videoLoadedCount}
              {videoPool.cap != null && `/${videoPool.cap}`}
              {videoPool.active_sessions != null && ` · ${videoPool.active_sessions} 会话`}
            </span>
          )}
        </div>
      )}

      {/* NO routable / weight / traffic fields — env containers have none (§6.2). */}
      <div className="text-2xs text-muted-foreground">
        未纳管容器不携带服务池接流、权重或流量信息{NO_METRICS_LABEL.replace("路由", "")}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {onRegister && (
          <Button
            size="xs"
            onClick={() => onRegister(target.url)}
            title="打开注册表单显式纳入全局表"
          >
            <Icon name="plus" size={10} />
            显式注册
          </Button>
        )}
        <Button
          size="xs"
          onClick={onSmokeTest}
          disabled={busy || !target.ok}
          title={target.ok ? "空池时 warm→自动卸载验证可加载性" : "容器不可达"}
        >
          <Icon name="play" size={10} />
          试启动
        </Button>
      </div>
    </div>
  );
}
