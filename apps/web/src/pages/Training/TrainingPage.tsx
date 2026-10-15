import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Box,
  Boxes,
  CheckCircle2,
  Clock3,
  Database,
  FlaskConical,
  FolderOpen,
  ListStart,
  Search,
  Server,
} from "lucide-react";
import { trainingHubApi, type QueueState, type TrainingHubCatalog } from "@/api/trainingHub";
import { PageContainer } from "@/components/layout/PageContainer";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import {
  trainingProgress,
  trainingStatusLabels,
  trainingStatusVariant,
} from "./trainingPresentation";

function WarehouseCards({
  catalog,
  compact = false,
}: {
  catalog: TrainingHubCatalog;
  compact?: boolean;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {catalog.items.map((item) => (
        <article
          key={item.framework}
          className="min-w-0 rounded-xl border border-border bg-card p-5"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-3">
              <div className="flex size-9 items-center justify-center rounded-lg bg-status-info-soft text-status-info">
                <Box className="size-5" />
              </div>
              <div>
                <h3 className="text-sm font-semibold">
                  {item.framework === "libreyolo" ? "LibreYOLO" : "Ultralytics"}
                </h3>
                <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                  {item.version} · 图片目标检测
                </p>
              </div>
            </div>
            <Badge
              dot
              variant={
                !catalog.enabled
                  ? "default"
                  : item.compatible === false
                    ? "danger"
                    : catalog.online
                      ? catalog.busy
                        ? "warning"
                        : "success"
                      : "default"
              }
            >
              {!catalog.enabled
                ? "未启用"
                : item.compatible === false
                  ? "环境不匹配"
                  : catalog.online
                    ? catalog.busy
                      ? "资源使用中"
                      : "在线可用"
                    : "离线 · 可排队"}
            </Badge>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Badge variant="outline">{item.base_models.map((model) => model.id).join(" / ")}</Badge>
            <Badge variant={item.capabilities.model_registration ? "ai" : "default"}>
              {item.capabilities.model_registration ? "支持模型回流" : "权重下载"}
            </Badge>
            {!compact && item.capabilities.resume && (
              <Badge variant="outline">支持检查点续训</Badge>
            )}
          </div>
          {!compact && (
            <dl className="mt-4 space-y-3 border-t border-border pt-4 text-xs">
              <div>
                <dt className="text-muted-foreground">容器镜像摘要</dt>
                <dd className="mt-1 break-all font-mono">{item.image_digest}</dd>
              </div>
              <div>
                <dt className="text-muted-foreground">基础权重</dt>
                {item.base_models.map((model) => (
                  <dd key={model.id} className="mt-1">
                    <p className="font-medium">{model.filename}</p>
                    <p className="mt-1 break-all font-mono text-2xs text-muted-foreground">
                      SHA-256 {model.sha256}
                    </p>
                  </dd>
                ))}
              </div>
            </dl>
          )}
          {item.compatible === false && (
            <p className="mt-3 text-xs text-status-danger">
              在线执行器的镜像或框架版本与仓库配置不一致，请联系管理员更新执行器。
            </p>
          )}
        </article>
      ))}
    </div>
  );
}

function Pagination({
  page,
  total,
  onChange,
  label,
}: {
  page: number;
  total: number;
  onChange: (page: number) => void;
  label: string;
}) {
  if (total <= 20 && page === 0) return null;
  return (
    <nav aria-label={`${label}分页`} className="mt-4 flex items-center justify-end gap-3">
      <span className="text-xs text-muted-foreground">
        第 {page + 1} / {Math.ceil(total / 20)} 页
      </span>
      <Button size="sm" disabled={page === 0} onClick={() => onChange(page - 1)}>
        上一页
      </Button>
      <Button size="sm" disabled={(page + 1) * 20 >= total} onClick={() => onChange(page + 1)}>
        下一页
      </Button>
    </nav>
  );
}

export function TrainingPage() {
  const [params, setParams] = useSearchParams();
  const tab = ["start", "queue", "warehouses"].includes(params.get("tab") ?? "")
    ? params.get("tab")!
    : "start";
  const state: QueueState = ["active", "finished", "failed", "all"].includes(
    params.get("state") ?? "",
  )
    ? (params.get("state") as QueueState)
    : "active";
  const framework = ["ultralytics", "libreyolo"].includes(params.get("framework") ?? "")
    ? params.get("framework")!
    : "";
  const [search, setSearch] = useState("");
  const [projectPage, setProjectPage] = useState(0);
  const [queuePage, setQueuePage] = useState(0);
  const patch = (updates: Record<string, string>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      Object.entries(updates).forEach(([key, value]) =>
        value ? next.set(key, value) : next.delete(key),
      );
      return next;
    });
  const catalog = useQuery({
    queryKey: ["training-hub", "catalog"],
    queryFn: trainingHubApi.catalog,
    refetchInterval: 5000,
  });
  const projects = useQuery({
    queryKey: ["training-hub", "projects", search, projectPage],
    queryFn: () => trainingHubApi.projects(search, projectPage * 20),
    enabled: tab === "start",
    refetchInterval: tab === "start" ? 10000 : false,
  });
  const queue = useQuery({
    queryKey: ["training-hub", "runs", state, queuePage, framework],
    queryFn: () => trainingHubApi.runs(state, queuePage * 20, framework),
    enabled: tab === "queue",
    refetchInterval: tab === "queue" ? 3000 : false,
  });
  useEffect(() => {
    setQueuePage(0);
  }, [state, framework]);
  useEffect(() => {
    if (queue.data && queuePage > 0 && queuePage * 20 >= queue.data.total) {
      setQueuePage(Math.max(0, Math.ceil(queue.data.total / 20) - 1));
    }
  }, [queue.data, queuePage]);
  useEffect(() => {
    if (projects.data && projectPage > 0 && projectPage * 20 >= projects.data.total) {
      setProjectPage(Math.max(0, Math.ceil(projects.data.total / 20) - 1));
    }
  }, [projects.data, projectPage]);
  return (
    <PageContainer>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">训练队列</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            查看训练仓库与项目，跟进训练进度，管理实验产物。
          </p>
        </div>
        <Badge
          dot
          variant={catalog.data?.online ? (catalog.data.busy ? "warning" : "success") : "default"}
        >
          {catalog.isLoading
            ? "正在连接执行器…"
            : !catalog.data?.enabled
              ? "训练未启用"
              : catalog.data.online
                ? catalog.data.busy
                  ? "共享 GPU 正忙"
                  : "共享 GPU 空闲"
                : "执行器离线"}
        </Badge>
      </header>
      <Tabs value={tab} activationMode="manual" onValueChange={(value) => patch({ tab: value })}>
        <TabsList
          variant="line"
          aria-label="训练队列视图"
          className="mb-5 w-full justify-start gap-4 overflow-x-auto border-b border-border pb-2"
        >
          <TabsTrigger value="start" className="flex-none">
            <FlaskConical className="size-4" />
            开始训练
          </TabsTrigger>
          <TabsTrigger value="queue" className="flex-none">
            <ListStart className="size-4" />
            队列与历史
          </TabsTrigger>
          <TabsTrigger value="warehouses" className="flex-none">
            <Boxes className="size-4" />
            训练仓库
          </TabsTrigger>
        </TabsList>
        {catalog.error && (
          <p
            role="alert"
            className="mb-4 rounded-lg bg-status-danger-soft p-4 text-sm text-status-danger"
          >
            {catalog.error.message}
            <button className="ml-3 underline" onClick={() => void catalog.refetch()}>
              重试
            </button>
          </p>
        )}
        <TabsContent value="start" className="space-y-6">
          <section aria-labelledby="available-warehouses">
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 id="available-warehouses" className="text-md font-semibold">
                可用训练仓库
              </h2>
              <button
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => patch({ tab: "warehouses" })}
              >
                查看容器环境
                <ArrowRight className="size-3.5" />
              </button>
            </div>
            {catalog.data && <WarehouseCards catalog={catalog.data} compact />}
            {catalog.data?.items.length === 0 && (
              <p className="rounded-lg border border-dashed border-border p-5 text-sm text-muted-foreground">
                尚未配置训练仓库，请联系管理员配置训练镜像与基础权重。
              </p>
            )}
            <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Server className="size-3.5 shrink-0" />
              训练仓库共用同一 GPU 执行器，实验按队列依次运行。
            </p>
          </section>
          <section aria-labelledby="training-projects">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 id="training-projects" className="text-md font-semibold">
                  可训练项目
                </h2>
                <p className="mt-1 text-xs text-muted-foreground">
                  当前账号可管理的项目。进入项目后准备训练数据、选择训练器并创建实验。
                </p>
              </div>
              <label className="flex items-center gap-2 rounded-md border border-border bg-background px-3 py-2">
                <Search className="size-3.5 text-muted-foreground" />
                <input
                  aria-label="搜索训练项目"
                  className="w-44 bg-transparent text-xs outline-none"
                  placeholder="搜索项目名称或编号"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                    setProjectPage(0);
                  }}
                />
              </label>
            </div>
            {projects.isLoading && (
              <p role="status" className="py-8 text-center text-sm text-muted-foreground">
                正在加载项目…
              </p>
            )}
            {projects.error && (
              <p role="alert" className="py-4 text-sm text-status-danger">
                {projects.error.message}
                <button className="ml-3 underline" onClick={() => void projects.refetch()}>
                  重试
                </button>
              </p>
            )}
            <div className="space-y-3">
              {projects.data?.items.map((project) => (
                <article
                  key={project.id}
                  className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-border bg-card p-5"
                >
                  <div className="flex min-w-0 items-start gap-3">
                    <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                      <FolderOpen className="size-4" />
                    </div>
                    <div className="min-w-0">
                      <Link
                        to={`/projects/${project.id}/learning?tab=training`}
                        className="break-words text-sm font-semibold hover:underline"
                      >
                        {project.name}
                      </Link>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {project.display_id} ·{" "}
                        {project.data_type === "image"
                          ? "图片项目"
                          : project.data_type === "video"
                            ? "视频项目"
                            : "点云项目"}
                      </p>
                      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                        {project.ready_dataset_count > 0 ? (
                          <Badge variant="success">{project.ready_dataset_count} 个就绪版本</Badge>
                        ) : (
                          <Badge variant="default">尚无就绪数据</Badge>
                        )}
                        {project.active_run_count > 0 && (
                          <Badge variant="warning">{project.active_run_count} 个实验进行中</Badge>
                        )}
                        {project.preparation_reason && (
                          <span className="text-muted-foreground">
                            {project.preparation_reason}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {project.ready_dataset_count === 0 &&
                      (project.can_prepare_dataset ? (
                        <Link
                          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs font-medium hover:bg-muted"
                          to={`/projects/${project.id}/learning?tab=data`}
                        >
                          <Database className="size-3.5" />
                          准备数据
                        </Link>
                      ) : (
                        <Link
                          className="text-xs text-muted-foreground underline"
                          to={`/projects/${project.id}/settings`}
                        >
                          完善项目配置
                        </Link>
                      ))}
                    <Link
                      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                      to={`/projects/${project.id}/learning?tab=training`}
                    >
                      进入项目
                      <ArrowRight className="size-3.5" />
                    </Link>
                  </div>
                </article>
              ))}
            </div>
            {projects.data?.items.length === 0 && (
              <div className="rounded-xl border border-dashed border-border p-10 text-center">
                <FolderOpen className="mx-auto mb-3 size-7 text-muted-foreground" />
                <h3 className="text-sm font-medium">
                  {search ? "没有匹配的项目" : "暂无可管理的训练项目"}
                </h3>
                <p className="mt-2 text-xs text-muted-foreground">
                  {search
                    ? "试试其他项目名称或编号。"
                    : "创建图片矩形框项目并完成审核后，即可准备训练数据。"}
                </p>
                <Link to="/dashboard" className="mt-4 inline-block text-xs underline">
                  查看项目列表
                </Link>
              </div>
            )}
            <Pagination
              page={projectPage}
              total={projects.data?.total ?? 0}
              onChange={setProjectPage}
              label="项目"
            />
          </section>
        </TabsContent>
        <TabsContent value="queue">
          <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {(
              [
                ["执行与恢复中", queue.data?.counts.running, Server],
                ["等待执行", queue.data?.counts.queued, Clock3],
                ["训练完成", queue.data?.counts.succeeded, CheckCircle2],
                ["训练失败", queue.data?.counts.failed, FlaskConical],
              ] as const
            ).map(([label, count, Icon]) => (
              <div key={label} className="rounded-lg border border-border bg-card p-4">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>{label}</span>
                  <Icon className="size-4" />
                </div>
                <p className="mt-2 font-mono text-stat">{count ?? "—"}</p>
              </div>
            ))}
          </div>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap gap-1" role="group" aria-label="筛选训练状态">
              {(
                [
                  ["active", "进行中"],
                  ["finished", "已结束"],
                  ["failed", "异常"],
                  ["all", "全部实验"],
                ] as const
              ).map(([key, label]) => (
                <Button
                  key={key}
                  size="sm"
                  variant={state === key ? "primary" : "ghost"}
                  aria-pressed={state === key}
                  onClick={() => {
                    setQueuePage(0);
                    patch({ state: key });
                  }}
                >
                  {label}
                </Button>
              ))}
            </div>
            <select
              aria-label="筛选训练器"
              value={framework}
              onChange={(event) => {
                setQueuePage(0);
                patch({ framework: event.target.value });
              }}
              className="rounded-md border border-border bg-background px-3 py-2 text-xs"
            >
              <option value="">全部训练器</option>
              <option value="ultralytics">Ultralytics</option>
              <option value="libreyolo">LibreYOLO</option>
            </select>
          </div>
          {queue.isLoading && (
            <p role="status" className="py-10 text-center text-sm text-muted-foreground">
              正在加载训练队列…
            </p>
          )}
          {queue.error && (
            <p role="alert" className="py-4 text-sm text-status-danger">
              {queue.error.message}
              <button className="ml-3 underline" onClick={() => void queue.refetch()}>
                重试
              </button>
            </p>
          )}
          <div className="space-y-3">
            {queue.data?.items.map(({ run, project_name }) => {
              const progress = trainingProgress(run);
              return (
                <Link
                  key={run.id}
                  to={`/projects/${run.project_id}/learning?tab=training&run=${run.id}`}
                  className="grid items-center gap-4 rounded-lg border border-border bg-card p-4 transition-colors hover:border-primary/50 hover:bg-muted sm:grid-cols-[minmax(0,1fr)_auto_minmax(120px,0.5fr)_auto]"
                >
                  <div className="min-w-0">
                    <h3 className="truncate text-sm font-semibold">{run.name}</h3>
                    <p className="mt-1 truncate text-xs text-muted-foreground">
                      {project_name} · {String(run.config.framework)} ·{" "}
                      {run.auto_trigger_id ? "自动训练" : "手动实验"}
                    </p>
                    {run.error_message && (
                      <p className="mt-2 line-clamp-2 text-xs text-status-danger">
                        {run.error_message}
                      </p>
                    )}
                  </div>
                  <Badge dot variant={trainingStatusVariant(run.status)}>
                    {trainingStatusLabels[run.status] ?? run.status}
                  </Badge>
                  <div className="min-w-0">
                    <div className="mb-2 flex justify-between text-xs text-muted-foreground">
                      <span>轮次</span>
                      <span className="font-mono">
                        {progress.epoch} / {progress.total}
                      </span>
                    </div>
                    <ProgressBar value={progress.percent} />
                  </div>
                  <ArrowRight className="hidden size-4 text-muted-foreground sm:block" />
                </Link>
              );
            })}
          </div>
          {queue.data?.items.length === 0 && (
            <div className="rounded-xl border border-dashed border-border p-12 text-center">
              <ListStart className="mx-auto mb-3 size-8 text-muted-foreground" />
              <h3 className="text-sm font-medium">
                {state === "active" ? "当前没有进行中的训练" : "此筛选下暂无实验"}
              </h3>
              <p className="mt-2 text-xs text-muted-foreground">
                {state === "active"
                  ? "可以查看已结束的实验，或选择项目开始一次训练。"
                  : "调整状态或训练器筛选后重试。"}
              </p>
              <Button
                size="sm"
                className="mt-4"
                onClick={() =>
                  state === "active" ? patch({ state: "finished" }) : patch({ tab: "start" })
                }
              >
                {state === "active" ? "查看训练历史" : "开始训练"}
              </Button>
            </div>
          )}
          <p className="mt-3 text-xs text-muted-foreground">
            统计仅包含当前管理范围内的实验，并随训练器筛选更新。失联与停止中的实验仍可能占用资源。
          </p>
          <Pagination
            page={queuePage}
            total={queue.data?.total ?? 0}
            onChange={setQueuePage}
            label="训练队列"
          />
        </TabsContent>
        <TabsContent value="warehouses" className="space-y-5">
          <div>
            <h2 className="text-md font-semibold">训练仓库与容器环境</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              以下是管理员配置的训练环境。多个框架可由同一容器提供，所有实验共用同一 GPU 资源。
            </p>
          </div>
          {catalog.data && (
            <>
              <section
                className="rounded-xl border border-border bg-muted/40 p-5"
                aria-label="训练执行器"
              >
                <div className="mb-4 flex items-center gap-2">
                  <Server className="size-4 text-status-info" />
                  <h3 className="text-sm font-semibold">共享训练执行器</h3>
                </div>
                <dl className="grid gap-4 text-xs sm:grid-cols-3">
                  <div>
                    <dt className="text-muted-foreground">资源</dt>
                    <dd className="mt-1 break-all font-mono">
                      {catalog.data.resource_id ?? "尚未配置"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">执行器</dt>
                    <dd className="mt-1 break-all font-mono">
                      {catalog.data.executor_id ?? "尚未连接"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">最近心跳</dt>
                    <dd className="mt-1">
                      {catalog.data.last_seen_at
                        ? new Date(catalog.data.last_seen_at).toLocaleString("zh-CN")
                        : "尚无心跳"}
                    </dd>
                  </div>
                </dl>
              </section>
              <WarehouseCards catalog={catalog.data} />
            </>
          )}
          {catalog.data?.items.length === 0 && (
            <p className="py-8 text-sm text-muted-foreground">
              尚无可用训练仓库，请联系管理员配置。
            </p>
          )}
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
