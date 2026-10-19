import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import {
  mlBackendOperationsApi,
  type ManagedMLProvider,
  type MLBackendOperationJob,
  type MLBackendOperationProvider,
  type MLBackendRebuildTarget,
} from "@/api/mlBackendOperations";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { CopyableError } from "@/components/ui/CopyableError";
import { Icon } from "@/components/ui/Icon";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";

const PROVIDER_NAMES: Record<MLBackendOperationProvider, string> = {
  gsam2: "Grounded SAM 2",
  sam3: "SAM 3",
  yolo: "YOLO",
  onnxtools: "ONNX Tools",
  rapidocr: "RapidOCR",
};
const CREDENTIAL_LABELS = { missing: "尚未初始化", ready: "凭据已保存", invalid: "凭据需修复" };
const REGISTRY_STATE_LABELS: Record<string, string> = {
  connected: "服务已连接",
  error: "服务异常",
  disconnected: "待健康检查 / 未连接",
};
const JOB_LABELS = {
  queued: "排队中",
  running: "执行中",
  succeeded: "维护完成",
  failed: "维护失败",
  interrupted: "维护已中断",
};
const PHASE_LABELS = {
  queued: "等待执行",
  validating: "检查部署配置",
  building: "重建镜像",
  stopping: "停止原容器",
  recreating: "重建容器",
  checking: "检查容器",
  complete: "任务结束",
};
const OUTCOME_LABELS = {
  initialized: "本地凭据已初始化",
  synced: "认证已验证并同步",
  unavailable: "服务暂不可用",
  manual_preserved: "已保留手动认证",
  error: "同步失败",
};

function activeJob(job: MLBackendOperationJob | undefined) {
  return job?.status === "queued" || job?.status === "running";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请稍后重试";
}

export function MLBackendOperationsSection() {
  const userId = useAuthStore((state) => state.user?.id);
  const role = useAuthStore((state) => state.user?.role);
  const sessionId = useAuthStore((state) => state.sessionId);
  if (!userId || role !== "super_admin") return null;
  return <OperationsSection key={`${userId}:${sessionId}`} userId={userId} sessionId={sessionId} />;
}

function OperationsSection({ userId, sessionId }: { userId: string; sessionId: string | null }) {
  const client = useQueryClient();
  const mounted = useRef(true);
  const pending = useRef(false);
  const [initializing, setInitializing] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmTarget, setConfirmTarget] = useState<MLBackendRebuildTarget | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const authKey = ["managed-ml-auth", userId, sessionId];
  const statusKey = ["ml-backend-operations", userId, sessionId];
  const jobKey = (id: string | null) => ["ml-backend-operation-job", userId, sessionId, id];
  const auth = useQuery({
    queryKey: authKey,
    queryFn: ({ signal }) => mlBackendOperationsApi.managedAuth({ signal }),
    retry: false,
  });
  const operations = useQuery({
    queryKey: statusKey,
    queryFn: ({ signal }) => mlBackendOperationsApi.status({ signal }),
    retry: false,
  });
  const displayedJobId = selectedJobId ?? operations.data?.jobs[0]?.id ?? null;
  const job = useQuery({
    queryKey: jobKey(displayedJobId),
    queryFn: ({ signal }) => mlBackendOperationsApi.job(displayedJobId!, { signal }),
    enabled: !!displayedJobId,
    initialData: () => operations.data?.jobs.find((item) => item.id === displayedJobId),
    refetchInterval: (query) => (!query.state.error && activeJob(query.state.data) ? 2000 : false),
    retry: false,
  });
  const busy =
    initializing !== null ||
    submitting ||
    activeJob(job.data) ||
    !!operations.data?.jobs.some(activeJob);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (job.data && !activeJob(job.data)) {
      void client.invalidateQueries({ queryKey: ["ml-backend-operations", userId, sessionId] });
    }
  }, [client, job.data, userId, sessionId]);

  const ownsResponse = () =>
    mounted.current &&
    isCurrentAuthOwner(userId) &&
    useAuthStore.getState().sessionId === sessionId &&
    useAuthStore.getState().user?.role === "super_admin";

  const initialize = async (provider?: ManagedMLProvider) => {
    if (pending.current || busy || !ownsResponse()) return;
    pending.current = true;
    setInitializing(provider ?? "all");
    setActionError(null);
    await client.cancelQueries({ queryKey: authKey });
    try {
      if (!ownsResponse()) return;
      const result = await mlBackendOperationsApi.initialize(provider ? [provider] : undefined);
      if (ownsResponse()) client.setQueryData(authKey, result);
    } catch (error) {
      if (ownsResponse()) setActionError(`认证初始化 / 同步失败：${errorMessage(error)}`);
    } finally {
      pending.current = false;
      if (ownsResponse()) setInitializing(null);
    }
  };

  const rebuild = async () => {
    const target = confirmTarget;
    setConfirmTarget(null);
    if (!target || pending.current || busy || !ownsResponse()) return;
    const service = operations.data?.services.find((item) => item.provider === target.provider);
    if (!operations.data?.available || !service?.can_rebuild) return;
    pending.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      const result = await mlBackendOperationsApi.rebuild(target);
      if (ownsResponse()) {
        client.setQueryData(jobKey(result.id), result);
        setSelectedJobId(result.id);
        void operations.refetch();
      }
    } catch (error) {
      if (!ownsResponse()) return;
      setActionError(`维护任务未创建：${errorMessage(error)}`);
      if (error instanceof ApiError && error.status === 409) {
        const detail = error.detailRaw as { job_id?: unknown } | undefined;
        if (typeof detail?.job_id === "string") setSelectedJobId(detail.job_id);
      }
      void operations.refetch();
    } finally {
      pending.current = false;
      if (ownsResponse()) setSubmitting(false);
    }
  };

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div>
          <h3 className="m-0 text-sm font-semibold">ML 服务认证与维护</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            管理内置推理服务的独立凭据与容器维护任务。
          </p>
        </div>
        <Button
          size="sm"
          disabled={busy || auth.isFetching || operations.isFetching}
          onClick={() => {
            void auth.refetch();
            void operations.refetch();
            if (displayedJobId) void job.refetch();
          }}
        >
          <Icon name="refresh" size={13} />
          刷新状态
        </Button>
      </div>
      <div className="space-y-4 p-4">
        <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
          <p>五个内置服务各自使用系统生成的独立凭据，持久保存后可重复同步；页面不显示密钥。</p>
          <p>
            外部服务仍可手动配置认证。认证方式 None 表示不发送认证信息，不代表使用系统托管凭据。
          </p>
          <p>凭据已保存或同步成功仅表示认证配置结果；容器健康和模型业务就绪需要分别检查。</p>
          <p>初始化不会自动注册服务或启用项目；同步后请在模型市场执行实例健康检查。</p>
        </div>
        {actionError && <ErrorNotice message={actionError} />}
        {auth.isError ? (
          <ErrorNotice message={`无法读取认证状态：${errorMessage(auth.error)}`} />
        ) : auth.isPending ? (
          <p className="text-sm text-muted-foreground">正在读取认证状态…</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-xs font-semibold">内置服务</h4>
              <Button size="sm" disabled={busy} onClick={() => void initialize()}>
                {initializing === "all" ? "正在初始化 / 同步…" : "初始化 / 同步全部"}
              </Button>
            </div>
            <div className="divide-y divide-border rounded-md border border-border">
              {auth.data.items.map((item) => {
                const provider = item.provider;
                const service = operations.data?.services.find((row) => row.provider === provider);
                const reason = operations.data?.reason ?? service?.reason ?? "维护状态尚未就绪";
                const canRebuild =
                  operations.data?.available &&
                  service?.can_rebuild &&
                  !!operations.data.deployment &&
                  !!operations.data.runner_id;
                return (
                  <div key={item.provider} className="space-y-2 px-3 py-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{PROVIDER_NAMES[provider]}</span>
                        <Badge variant={item.credential_state === "ready" ? "success" : "warning"}>
                          {CREDENTIAL_LABELS[item.credential_state]}
                        </Badge>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          size="xs"
                          disabled={busy}
                          aria-label={`初始化 / 同步 ${PROVIDER_NAMES[provider]}`}
                          onClick={() => void initialize(item.provider)}
                        >
                          {initializing === item.provider ? "正在同步…" : "初始化 / 同步"}
                        </Button>
                        <Button
                          size="xs"
                          disabled={busy || !canRebuild}
                          title={!canRebuild ? reason : undefined}
                          aria-label={`重建 ${PROVIDER_NAMES[provider]} 镜像与容器`}
                          onClick={() => {
                            if (operations.data?.deployment && operations.data.runner_id) {
                              setConfirmTarget({
                                provider,
                                expected_deployment: operations.data.deployment,
                                expected_runner_id: operations.data.runner_id,
                              });
                            }
                          }}
                        >
                          重建镜像与容器
                        </Button>
                      </div>
                    </div>
                    <p className="break-all text-xs text-muted-foreground">
                      {item.service_name} · {item.url}
                    </p>
                    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      {item.registries.length ? (
                        item.registries.map((registry) => (
                          <span key={registry.id}>
                            {registry.name} ·{" "}
                            {registry.auth_method.startsWith("managed:")
                              ? "系统托管"
                              : registry.auth_method === "token"
                                ? "手动 Token"
                                : registry.auth_method === "none"
                                  ? "无认证"
                                  : registry.auth_method}{" "}
                            · {REGISTRY_STATE_LABELS[registry.state] ?? "未知状态"}
                          </span>
                        ))
                      ) : (
                        <span>尚无关联的服务注册记录</span>
                      )}
                    </div>
                    {item.outcome && (
                      <p
                        className={`text-xs ${item.outcome === "error" || item.outcome === "unavailable" ? "text-status-danger" : "text-muted-foreground"}`}
                      >
                        {OUTCOME_LABELS[item.outcome]}
                        {item.message ? `：${item.message}` : ""}
                      </p>
                    )}
                    {operations.data?.available &&
                      service &&
                      !service.can_rebuild &&
                      service.reason && (
                        <p className="text-xs text-muted-foreground">
                          维护不可用：{service.reason}
                        </p>
                      )}
                  </div>
                );
              })}
            </div>
          </>
        )}
        <div className="space-y-2 border-t border-border pt-3">
          <h4 className="text-xs font-semibold">镜像与容器维护</h4>
          {operations.isPending ? (
            <p className="text-xs text-muted-foreground">正在读取维护能力…</p>
          ) : operations.isError ? (
            <ErrorNotice message={`无法读取维护能力：${errorMessage(operations.error)}`} />
          ) : (
            <p className="text-xs text-muted-foreground">
              {operations.data.available
                ? `当前部署：${operations.data.deployment ?? "未标识"}。每次只维护一个服务。`
                : `维护不可用：${operations.data.reason ?? "维护执行器未接入"}`}
            </p>
          )}
          {job.isError && (
            <div className="space-y-2">
              <ErrorNotice message={`任务进度读取失败：${errorMessage(job.error)}`} />
              <Button size="xs" disabled={job.isFetching} onClick={() => void job.refetch()}>
                重试读取任务
              </Button>
            </div>
          )}
          {job.data ? (
            <JobDetails job={job.data} />
          ) : (
            <p className="text-xs text-muted-foreground">暂无维护任务</p>
          )}
        </div>
      </div>
      <AlertDialog
        open={confirmTarget !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              重建 {confirmTarget ? PROVIDER_NAMES[confirmTarget.provider] : ""} 镜像与容器？
            </AlertDialogTitle>
            <AlertDialogDescription>
              目标部署：{confirmTarget?.expected_deployment ?? "未标识"}
              。将重新构建所选服务的镜像并重新创建容器，期间该服务的推理会短暂停止。确认前请安排好正在使用该服务的任务。空闲跟踪会话缓存会丢失，已持久保存的模型和标注数据会保留。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy || !operations.data?.available}
              onClick={() => void rebuild()}
            >
              确认重建
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function ErrorNotice({ message }: { message: string }) {
  return (
    <div role="alert" className="text-xs text-status-danger">
      <CopyableError message={message} />
    </div>
  );
}

function JobDetails({ job }: { job: MLBackendOperationJob }) {
  return (
    <div className="space-y-2 rounded-md border border-border bg-muted/30 p-3" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium">{PROVIDER_NAMES[job.provider]}</span>
        <Badge
          variant={
            job.status === "succeeded"
              ? "success"
              : job.status === "failed" || job.status === "interrupted"
                ? "danger"
                : "default"
          }
        >
          {JOB_LABELS[job.status]}
        </Badge>
        <span className="text-muted-foreground">{PHASE_LABELS[job.phase]}</span>
      </div>
      <p className="break-all text-xs text-muted-foreground">
        部署 {job.deployment} · 更新于 {new Date(job.updated_at).toLocaleString()}
      </p>
      {job.error && <ErrorNotice message={job.error} />}
      {job.status === "interrupted" && (
        <p className="text-xs text-status-caution">
          请管理员核查实际容器状态，并在宿主机确认恢复后再创建维护任务。
        </p>
      )}
      {job.status === "succeeded" && (
        <p className="text-xs text-muted-foreground">
          镜像与容器维护已完成；模型加载和业务推理就绪请在模型市场进一步检查。
        </p>
      )}
      {job.events.length > 0 && (
        <ol aria-label="维护阶段日志" className="max-h-48 space-y-1 overflow-y-auto text-xs">
          {job.events.map((event, index) => (
            <li
              key={`${event.at}:${index}`}
              className={event.kind === "error" ? "text-status-danger" : "text-muted-foreground"}
            >
              <time className="mr-2 font-mono tabular-nums">
                {new Date(event.at).toLocaleTimeString()}
              </time>
              {event.message}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
