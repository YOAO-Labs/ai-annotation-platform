import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { asyncJobsApi } from "@/api/asyncJobs";
import type { PointCloudEnhancementConfig } from "@/api/projects";
import { pointCloudDensificationApi, type PreparationRequest } from "@/api/pointCloudDensification";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useAuthStore } from "@/stores/authStore";

interface Props {
  projectId: string;
  config: PointCloudEnhancementConfig;
  canManage: boolean;
  onConfigChange: (next: PointCloudEnhancementConfig) => void;
  saving?: boolean;
}

const FULL_LIDAR_PROFILE = "omega-current-mono-512-full-lidar";
const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const STATUS: Record<string, string> = {
  pending: "等待生成",
  running: "正在生成",
  completed: "生成结束",
  failed: "生成失败",
  cancelled: "已取消",
};

function errorMessage(error: unknown): string {
  const detail = (error as { detailRaw?: { reason?: string } })?.detailRaw;
  const reasons: Record<string, string> = {
    deployment_disabled: "部署尚未开启 AI 补强实验。",
    coordinate_calibration_mismatch: "相机标定与逐帧位姿不一致，请先完成标定检查。",
    source_hash_missing: "源文件缺少可信摘要，请先完成数据检查。",
    producer_pool_unavailable: "所选模型服务暂不可用。",
    producer_capability_missing: "所选服务不支持当前帧点云补强。",
    producer_authentication_failed: "模型服务鉴权失败，请检查服务连接配置。",
    producer_request_failed: "模型服务请求失败，请检查服务状态后重试。",
    producer_or_geometry_failed: "模型推理或点云校准失败，请检查源数据和服务后重试。",
    preparation_authority_revoked: "生成权限已变化，请重新检查项目访问权限。",
    source_changed_since_request: "源数据已变化，请重新创建生成任务。",
    six_current_cameras_required: "生成需要该帧完整的六路相机。",
  };
  return (
    (detail?.reason && reasons[detail.reason]) ||
    (error instanceof Error ? error.message : "操作失败，请重试。")
  );
}

export function PointCloudPreparation(props: Props) {
  const accountId = useAuthStore((s) => s.user?.id ?? "anonymous");
  // A new owner starts without the previous account's job or late responses.
  return (
    <PreparationOwner key={`${accountId}:${props.projectId}`} {...props} accountId={accountId} />
  );
}

function PreparationOwner({
  projectId,
  config,
  canManage,
  onConfigChange,
  saving,
  accountId,
}: Props & { accountId: string }) {
  const client = useQueryClient();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [sceneId, setSceneId] = useState("");
  const [poolId, setPoolId] = useState(config.ml_backend_pool_id ?? "");
  useEffect(() => {
    setPoolId(config.ml_backend_pool_id ?? "");
  }, [config.ml_backend_pool_id]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const options = useQuery({
    queryKey: ["point-cloud-preparation-options", accountId, projectId],
    queryFn: ({ signal }) => pointCloudDensificationApi.options(projectId, signal),
    enabled: canManage && config.enabled,
  });
  const job = useQuery({
    queryKey: ["point-cloud-preparation-job", accountId, projectId, jobId],
    queryFn: ({ signal }) => asyncJobsApi.get(jobId!, { signal }),
    enabled: canManage && !!jobId,
    refetchInterval: (query) =>
      query.state.data && TERMINAL.has(query.state.data.status) ? false : 1500,
  });
  const jobUnavailable = [403, 404].includes(
    (job.error as { status?: number } | null)?.status ?? 0,
  );
  const active = !!jobId && !jobUnavailable && (!job.data || !TERMINAL.has(job.data.status));
  const producers = options.data?.producers ?? [];
  const ready =
    canManage &&
    options.isSuccess &&
    config.enabled &&
    options.data?.deployment_enabled &&
    config.profile === FULL_LIDAR_PROFILE &&
    producers.some((p) => p.id === config.ml_backend_pool_id);
  const sceneAvailable = options.isSuccess && options.data.scenes.some((s) => s.id === sceneId);
  const failedIds = Array.isArray(job.data?.result.failed_task_ids)
    ? job.data.result.failed_task_ids.filter((id): id is string => typeof id === "string")
    : [];
  const failureMessages = Array.isArray(job.data?.result.failures)
    ? [
        ...new Set(
          job.data.result.failures.map((failure: unknown) => {
            const reason = (failure as { reason?: unknown } | null)?.reason;
            return errorMessage({
              detailRaw: { reason: typeof reason === "string" ? reason : "" },
            });
          }),
        ),
      ]
    : [];

  const create = async (request: PreparationRequest) => {
    if (busy || active || !ready || (request.scope === "scene_id" && !sceneAvailable)) return;
    setBusy(true);
    setError(null);
    try {
      const created = await pointCloudDensificationApi.create(projectId, request);
      if (alive.current) setJobId(created.job_id);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const cancel = async () => {
    if (!jobId || busy) return;
    setBusy(true);
    setError(null);
    try {
      await asyncJobsApi.cancel(jobId);
      if (alive.current)
        await client.invalidateQueries({
          queryKey: ["point-cloud-preparation-job", accountId, projectId, jobId],
        });
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  if (!canManage || !config.enabled) return null;
  const selectClass =
    "h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground disabled:opacity-50";
  return (
    <div
      className="mt-3 space-y-3 border-t border-border pt-3"
      data-testid="point-cloud-preparation"
    >
      <p className="m-0 text-xs text-muted-foreground">
        为所选场景逐帧生成，使用每帧真实 LiDAR
        校准。已有有效结果会复用，标注员打开工作台后直接加载。
      </p>
      {config.profile !== FULL_LIDAR_PROFILE && (
        <p className="m-0 text-xs text-muted-foreground">
          当前使用已导入的试验结果。应用完整 LiDAR
          生成配置后，工作台将使用新生成的结果；已导入资产仍会保留。
        </p>
      )}
      {options.data && !options.data.deployment_enabled && (
        <p className="text-xs text-muted-foreground">部署尚未开启 AI 补强实验。</p>
      )}
      {options.isLoading && <p className="text-xs text-muted-foreground">读取生成配置…</p>}
      {options.error && <p className="text-xs text-status-danger">{errorMessage(options.error)}</p>}
      {options.data?.deployment_enabled && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-48 flex-col gap-1 text-xs text-muted-foreground">
            生成服务
            <select
              aria-label="点云补强生成服务"
              className={selectClass}
              value={poolId}
              disabled={saving || active || busy || !options.isSuccess}
              onChange={(event) => setPoolId(event.target.value)}
            >
              <option value="">请选择已启用的服务</option>
              {producers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          {poolId &&
            (poolId !== config.ml_backend_pool_id || config.profile !== FULL_LIDAR_PROFILE) && (
              <Button
                size="sm"
                disabled={
                  saving || active || !options.isSuccess || !producers.some((p) => p.id === poolId)
                }
                onClick={() =>
                  onConfigChange({
                    ...config,
                    profile: FULL_LIDAR_PROFILE,
                    ml_backend_pool_id: poolId,
                  })
                }
              >
                应用完整 LiDAR 生成配置
              </Button>
            )}
          <label className="flex min-w-48 flex-col gap-1 text-xs text-muted-foreground">
            场景
            <select
              aria-label="点云补强生成场景"
              className={selectClass}
              value={sceneId}
              disabled={active || busy || !options.isSuccess}
              onChange={(e) => setSceneId(e.target.value)}
            >
              <option value="">请选择场景</option>
              {options.data.scenes.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <Button
            size="sm"
            variant="primary"
            disabled={!ready || !sceneAvailable || busy || active || saving}
            onClick={() => void create({ scope: "scene_id", scene_id: sceneId })}
          >
            生成所选场景
          </Button>
        </div>
      )}
      {options.data?.deployment_enabled && producers.length === 0 && (
        <p className="text-xs text-muted-foreground">
          请先在模型服务中为本项目启用支持当前帧补强的服务池。
        </p>
      )}
      {jobId && (
        <div className="space-y-2 text-xs" aria-live="polite">
          <div className="flex items-center gap-3">
            <span>
              {jobUnavailable
                ? "无法访问此生成任务"
                : job.data
                  ? STATUS[job.data.status]
                  : "读取生成进度…"}{" "}
              {job.data?.progress_pct ?? 0}%
            </span>
            {active && (
              <Button
                size="xs"
                disabled={busy || !!job.data?.payload.cancel_requested}
                onClick={() => void cancel()}
              >
                {job.data?.payload.cancel_requested ? "正在取消…" : "取消生成"}
              </Button>
            )}
            {!active && failedIds.length > 0 && (
              <Button
                size="xs"
                disabled={busy || !ready}
                onClick={() =>
                  void create({ scope: "task_ids", task_ids: failedIds, retry_job_id: jobId })
                }
              >
                重试失败帧（{failedIds.length}）
              </Button>
            )}
          </div>
          {job.error && (
            <Button
              size="xs"
              onClick={() => (jobUnavailable ? setJobId(null) : void job.refetch())}
            >
              {jobUnavailable ? "关闭进度" : "重读进度"}
            </Button>
          )}
          <ProgressBar value={job.data?.progress_pct ?? 0} />
          {job.data && (
            <p className="m-0 text-muted-foreground">
              新生成 {Number(job.data.result.success_count ?? 0)} 帧 · 复用{" "}
              {Number(job.data.result.skipped_count ?? 0)} 帧 · 失败{" "}
              {Number(job.data.result.failed_count ?? 0)} 帧
            </p>
          )}
          {job.error && <p className="text-status-danger">{errorMessage(job.error)}</p>}
          {failureMessages.length > 0 && (
            <p className="m-0 text-status-danger">{failureMessages.join(" ")}</p>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs text-status-danger">
          {error}
        </p>
      )}
    </div>
  );
}
