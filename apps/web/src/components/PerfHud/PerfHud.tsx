import { useMemo, useState, type CSSProperties } from "react";
import { clsx } from "clsx";
import { Icon } from "@/components/ui/Icon";
import { Sparkline } from "@/components/ui/Sparkline";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useAuthStore } from "@/stores/authStore";
import { ShellPopover, SHELL_POPOVER_HEADER_CLASS } from "@/components/shell/ShellPopover";
import { isCpuFallback } from "@/utils/mlBackendCompute";
import { useMLBackendStats, type BackendSnapshot, type BackendHistory } from "./useMLBackendStats";
import { useBrowserStats } from "./useBrowserStats";
import { usePerfHudStore } from "./usePerfHudStore";
import styles from "./PerfHud.module.css";

function colorFor(pct: number | null | undefined): string {
  if (pct == null) return "var(--sc-muted-foreground)";
  if (pct >= 90) return "var(--sc-destructive)";
  if (pct >= 70) return "var(--sc-caution)";
  return "var(--sc-positive)";
}

function MetricBar({
  label,
  value,
  unit,
  pct,
}: {
  label: string;
  value: string;
  unit?: string;
  pct: number | null | undefined;
}) {
  const color = colorFor(pct);
  const valueRef = useElementStyle<HTMLSpanElement>({
    "--perf-hud-metric-color": color,
  } as CSSProperties);
  const fillRef = useElementStyle<HTMLDivElement>({
    "--perf-hud-metric-width": `${Math.max(0, Math.min(100, pct ?? 0))}%`,
    "--perf-hud-metric-color": color,
  } as CSSProperties);
  return (
    <div className={styles.metric}>
      <div className={styles.metricHeader}>
        <span>{label}</span>
        <span ref={valueRef} className={styles.metricValue}>
          {value}
          {unit ? <span className={styles.metricUnit}>{unit}</span> : null}
        </span>
      </div>
      <div className={styles.metricTrack}>
        <div ref={fillRef} className={styles.metricFill} />
      </div>
    </div>
  );
}

function formatIdleAge(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 60) return `idle ${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `idle ${Math.round(seconds / 60)}m`;
  return `idle ${Math.round(seconds / 3600)}h`;
}

function getLoadLabel(snap: BackendSnapshot): string | null {
  const imageLoaded =
    snap.pool?.current_size ??
    snap.pool?.loaded_keys?.length ??
    snap.pool?.loaded_variants?.length ??
    snap.gpu_info?.image_pool_loaded_variants?.length ??
    null;
  const videoLoaded =
    snap.video_pool?.current_size ??
    snap.video_pool?.loaded_keys?.length ??
    snap.video_pool?.loaded_variants?.length ??
    snap.gpu_info?.video_pool_loaded_variants?.length ??
    null;
  if ((imageLoaded ?? 0) > 0 || (videoLoaded ?? 0) > 0) return "loaded";
  if (snap.loaded === true) return "loaded";
  if (snap.loaded === false || imageLoaded === 0 || videoLoaded === 0) return "idle unloaded";
  return null;
}

function getBackendLabel(snap: BackendSnapshot): string {
  const bindings = snap.bindings ?? [];
  const physicalLabel = snap.url_host ?? snap.physical_key ?? snap.backend_id.slice(0, 6);
  if (bindings.length > 1) {
    return `${physicalLabel} · ${bindings.length} projects`;
  }
  const alias = bindings[0]?.backend_name ?? snap.backend_name;
  return alias ? `${physicalLabel} · ${alias}` : physicalLabel;
}

function formatBinding(binding: NonNullable<BackendSnapshot["bindings"]>[number]): string {
  const project = binding.project_display_id ?? binding.project_name;
  return project ? `${project}: ${binding.backend_name}` : binding.backend_name;
}

function BackendPanel({
  snap,
  hist,
  expanded,
}: {
  snap: BackendSnapshot;
  hist: BackendHistory | undefined;
  expanded: boolean;
}) {
  const gpuUtil = snap.gpu_info?.gpu_utilization_percent ?? null;
  const vramUsed = snap.gpu_info?.memory_used_mb ?? null;
  const vramTotal = snap.gpu_info?.memory_total_mb ?? null;
  const vramPct = vramUsed != null && vramTotal ? (vramUsed / vramTotal) * 100 : null;
  const cpu = snap.host?.container_cpu_percent ?? null;
  const mem = snap.host?.container_memory_percent ?? null;
  const temp = snap.gpu_info?.gpu_temperature_celsius;
  const power = snap.gpu_info?.gpu_power_watts;
  const hitRate = snap.cache?.hit_rate;
  const loadLabel = getLoadLabel(snap);
  const idleAge = formatIdleAge(snap.last_request_age_seconds);
  const bindings = snap.bindings ?? [];

  return (
    <div className={styles.backendPanel}>
      <MetricBar label="GPU util" value={gpuUtil != null ? `${gpuUtil}%` : "—"} pct={gpuUtil} />
      <MetricBar
        label="VRAM"
        value={vramUsed != null && vramTotal ? `${vramUsed} / ${vramTotal}` : "—"}
        unit="MB"
        pct={vramPct}
      />
      <MetricBar label="CPU" value={cpu != null ? `${cpu.toFixed(1)}%` : "—"} pct={cpu} />
      <MetricBar label="RAM" value={mem != null ? `${mem.toFixed(1)}%` : "—"} pct={mem} />
      {expanded && hist ? (
        <div className={styles.sparkGrid}>
          <SparkRow label="GPU" values={hist.gpuUtil} color="var(--sc-positive)" />
          <SparkRow label="VRAM" values={hist.vramPercent} color="var(--sc-brand)" />
          <SparkRow label="CPU" values={hist.cpu} color="var(--sc-caution)" />
          <SparkRow label="RAM" values={hist.mem} color="var(--sc-destructive)" />
        </div>
      ) : null}
      <div className={styles.backendMeta}>
        {bindings.length > 0 ? <span>{bindings.map(formatBinding).join(" · ")}</span> : null}
        {snap.gpu_info?.device_name ? <span>{snap.gpu_info.device_name}</span> : null}
        {isCpuFallback(snap.compute) && (
          <span className="text-status-caution" title="GPU 配置但已退回 CPU">
            · ⚠ CPU
          </span>
        )}
        {snap.url_host ? <span>· {snap.url_host}</span> : null}
        {loadLabel ? <span>· {loadLabel}</span> : null}
        {idleAge ? <span>· {idleAge}</span> : null}
        {temp != null ? <span>· {temp}°C</span> : null}
        {power != null ? <span>· {power}W</span> : null}
        {hitRate != null ? <span>· cache {(hitRate * 100).toFixed(0)}%</span> : null}
        {snap.model_version ? <span>· {snap.model_version}</span> : null}
      </div>
    </div>
  );
}

function BrowserPanel({ enabled }: { enabled: boolean }) {
  const stats = useBrowserStats(enabled);
  return (
    <div className={styles.browserPanel}>
      <h4 className={styles.browserSectionTitle}>Browser</h4>
      <Kv label="FPS" value={stats.fps != null ? `${stats.fps}` : "—"} />
      <Kv label="JS heap" value={stats.jsHeapMB != null ? `${stats.jsHeapMB} MB` : "N/A"} />
      <Kv label="API p95" value={stats.apiP95Ms != null ? `${stats.apiP95Ms} ms` : "—"} />
      <Kv
        label="Longtask 60s"
        value={
          stats.longtaskLastMs != null
            ? `${stats.longtaskCount60s} · last ${stats.longtaskLastMs}ms`
            : `${stats.longtaskCount60s}`
        }
      />
      <Kv label="WS reconnects" value={`${stats.wsReconnects}`} />
      <Kv label="Task boxes" value={`${stats.taskBoxCount}`} />
    </div>
  );
}

function Kv({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.browserKv}>
      <span className={styles.browserKvLabel}>{label}</span>
      <span className={styles.browserKvValue}>{value}</span>
    </div>
  );
}

function SparkRow({ label, values, color }: { label: string; values: number[]; color: string }) {
  if (values.length < 2) return null;
  return (
    <div className={styles.sparkRow}>
      <span className={styles.sparkLabel}>{label}</span>
      <Sparkline values={values} color={color} width={220} height={20} />
    </div>
  );
}

export function PerfHud() {
  const visible = usePerfHudStore((s) => s.visible);
  const expanded = usePerfHudStore((s) => s.expanded);
  const close = usePerfHudStore((s) => s.close);
  const setExpanded = usePerfHudStore((s) => s.setExpanded);
  const role = useAuthStore((s) => s.user?.role);
  const isAdmin = role === "super_admin" || role === "project_admin";

  const { snapshots, history, connected, status } = useMLBackendStats();
  const backendIds = useMemo(() => Object.keys(snapshots), [snapshots]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const activeId = selectedId && snapshots[selectedId] ? selectedId : (backendIds[0] ?? null);
  const activeSnap = activeId ? snapshots[activeId] : null;
  const activeHist = activeId ? history[activeId] : undefined;
  const activeLabel = activeSnap ? getBackendLabel(activeSnap) : undefined;

  if (!visible || !isAdmin) return null;

  return (
    <ShellPopover id="performance" label="GPU 性能监控" onClose={close}>
      <div className={SHELL_POPOVER_HEADER_CLASS}>
        <span
          aria-hidden
          title={connected ? "实时连接中" : "未连接"}
          className={clsx(styles.statusDot, connected && styles.statusDotConnected)}
        />
        <span className={styles.headerTitle}>性能监控</span>
        {backendIds.length > 1 ? (
          <select
            value={activeId ?? ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className={styles.backendSelect}
            title={activeLabel}
          >
            {backendIds.map((id) => (
              <option key={id} value={id}>
                {getBackendLabel(snapshots[id])}
              </option>
            ))}
          </select>
        ) : null}
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-label={expanded ? "收起趋势图" : "展开趋势图"}
          title={expanded ? "收起" : "展开 60s 趋势"}
          className={styles.iconButton}
        >
          <Icon name={expanded ? "chevUp" : "chevDown"} size={14} />
        </button>
        <button type="button" onClick={close} aria-label="关闭" className={styles.closeButton}>
          <Icon name="x" size={14} />
        </button>
      </div>
      <div className={styles.body}>
        {activeSnap ? (
          <BackendPanel snap={activeSnap} hist={activeHist} expanded={expanded} />
        ) : (
          <div className={styles.empty}>
            {status === "connecting" ? "正在连接 /ws/ml-backend-stats…" : null}
            {status === "auth_failed" ? (
              <>
                鉴权失败 (1008)
                <div className={styles.emptyHint}>仅 super_admin / project_admin 可见此面板</div>
              </>
            ) : null}
            {status === "closed" ? (
              <>
                连接关闭
                <div className={styles.emptyHint}>确认 API + Celery beat 已重启</div>
              </>
            ) : null}
            {status === "connected" ? (
              <>
                等待 backend 上报…
                <div className={styles.emptyHint}>
                  Celery beat 1s task 是否在跑？(check <code>publish-ml-backend-stats</code>)
                </div>
              </>
            ) : null}
            {status === "idle" ? "未连接" : null}
          </div>
        )}
        <BrowserPanel enabled={visible} />
      </div>
    </ShellPopover>
  );
}
