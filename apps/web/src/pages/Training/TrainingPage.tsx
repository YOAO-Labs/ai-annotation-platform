import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Boxes,
  Database,
  FlaskConical,
  FolderOpen,
  ListStart,
  Search,
  Server,
} from "lucide-react";
import { trainingHubApi, type QueueState } from "@/api/trainingHub";
import { PageContainer } from "@/components/layout/PageContainer";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { TrainingWarehouseCards } from "./TrainingWarehouseCards";
import { TrainingQueuePanel } from "./TrainingQueuePanel";
import { TrainingError, TrainingLoading } from "./TrainingFeedback";
import { ListPagination } from "@/components/ui/ListPagination";

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
  const [projectPageSize, setProjectPageSize] = useState(20);
  const [queuePageSize, setQueuePageSize] = useState(20);
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
  const resourceId = catalog.data?.resource_id;
  const resourceNode = resourceId?.includes("/")
    ? resourceId.slice(0, resourceId.lastIndexOf("/"))
    : null;
  const projects = useQuery({
    queryKey: ["training-hub", "projects", search, projectPage, projectPageSize],
    queryFn: () => trainingHubApi.projects(search, projectPage * projectPageSize, projectPageSize),
    enabled: tab === "start",
    refetchInterval: tab === "start" ? 10000 : false,
  });
  const queue = useQuery({
    queryKey: ["training-hub", "runs", state, queuePage, queuePageSize, framework],
    queryFn: () => trainingHubApi.runs(state, queuePage * queuePageSize, framework, queuePageSize),
    enabled: tab === "queue",
    refetchInterval: tab === "queue" ? 3000 : false,
  });
  useEffect(() => {
    setQueuePage(0);
  }, [state, framework]);
  useEffect(() => {
    if (queue.data && queuePage > 0 && queuePage * queuePageSize >= queue.data.total) {
      setQueuePage(Math.max(0, Math.ceil(queue.data.total / queuePageSize) - 1));
    }
  }, [queue.data, queuePage, queuePageSize]);
  useEffect(() => {
    if (projects.data && projectPage > 0 && projectPage * projectPageSize >= projects.data.total) {
      setProjectPage(Math.max(0, Math.ceil(projects.data.total / projectPageSize) - 1));
    }
  }, [projects.data, projectPage, projectPageSize]);
  return (
    <PageContainer>
      <header className="mb-5 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">训练队列</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            选择项目开始实验，跟进共享 GPU 上的训练。
          </p>
        </div>
        <div className="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2">
          <Server aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <div className="space-y-1">
            <p className="text-2xs text-muted-foreground">共享训练资源</p>
            <Badge
              dot
              className="bg-transparent px-0 py-0"
              variant={
                catalog.isError
                  ? "danger"
                  : catalog.data?.enabled && catalog.data.online
                    ? catalog.data.busy
                      ? "warning"
                      : "success"
                    : "default"
              }
            >
              {catalog.isLoading
                ? "正在连接执行器…"
                : catalog.isError
                  ? "执行器状态读取失败"
                  : !catalog.data?.enabled
                    ? "训练未启用"
                    : catalog.data.online
                      ? catalog.data.busy
                        ? "共享 GPU 正忙"
                        : "共享 GPU 空闲"
                      : "执行器离线"}
            </Badge>
          </div>
        </div>
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
          <TrainingError message={catalog.error.message} onRetry={() => void catalog.refetch()} />
        )}
        <TabsContent value="start">
          <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
            <section aria-labelledby="training-projects">
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 id="training-projects" className="text-md font-semibold">
                    可训练项目
                    {projects.data && (
                      <span className="ml-2 text-xs font-normal tabular-nums text-muted-foreground">
                        {projects.data.total}
                      </span>
                    )}
                  </h2>
                  <p className="mt-1 text-xs text-muted-foreground">
                    进入项目准备数据、配置训练并管理实验。
                  </p>
                </div>
                <label className="flex w-full items-center gap-2 rounded-md border border-border bg-card px-3 py-2 transition-shadow focus-within:ring-2 focus-within:ring-ring sm:w-auto">
                  <Search className="size-3.5 text-muted-foreground" />
                  <input
                    aria-label="搜索训练项目"
                    className="min-w-0 flex-1 bg-transparent text-xs outline-none sm:w-40"
                    placeholder="搜索项目名称或编号"
                    value={search}
                    onChange={(event) => {
                      setSearch(event.target.value);
                      setProjectPage(0);
                    }}
                  />
                </label>
              </div>
              <div className="overflow-hidden rounded-xl border border-border bg-card">
                {projects.isLoading && <TrainingLoading label="正在加载项目…" />}
                {projects.error && (
                  <div className="p-4">
                    <TrainingError
                      message={projects.error.message}
                      onRetry={() => void projects.refetch()}
                    />
                  </div>
                )}
                <div className="divide-y divide-border">
                  {projects.data?.items.map((project) => (
                    <article
                      key={project.id}
                      className="grid items-center gap-3 px-4 py-3 sm:grid-cols-[minmax(0,1fr)_auto]"
                    >
                      <div className="flex min-w-0 items-start gap-3">
                        <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
                          <FolderOpen className="size-4" />
                        </div>
                        <div className="min-w-0">
                          <Link
                            to={`/projects/${project.id}/learning?tab=training`}
                            className="rounded-sm break-words text-sm font-semibold transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            {project.name}
                          </Link>
                          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                            <span className="text-muted-foreground">
                              {project.display_id} ·{" "}
                              {project.data_type === "image"
                                ? "图片项目"
                                : project.data_type === "video"
                                  ? "视频项目"
                                  : "点云项目"}
                            </span>
                            {project.ready_dataset_count > 0 ? (
                              <Badge variant="success">
                                {project.ready_dataset_count} 个就绪版本
                              </Badge>
                            ) : (
                              <Badge variant="default">尚无就绪数据</Badge>
                            )}
                            {project.active_run_count > 0 && (
                              <Badge variant="warning">
                                {project.active_run_count} 个实验进行中
                              </Badge>
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
                              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              to={`/projects/${project.id}/learning?tab=data`}
                            >
                              <Database className="size-3.5" />
                              准备数据
                            </Link>
                          ) : (
                            <Link
                              className="rounded-sm text-xs text-muted-foreground underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              to={`/projects/${project.id}/settings`}
                            >
                              完善项目配置
                            </Link>
                          ))}
                        <Link
                          className="inline-flex items-center gap-1 rounded-md bg-muted px-3 py-2 text-xs font-medium transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          to={`/projects/${project.id}/learning?tab=training`}
                        >
                          进入项目
                          <ArrowRight className="size-3.5" />
                        </Link>
                      </div>
                    </article>
                  ))}
                </div>
                {projects.data?.items.length === 0 && !projects.error && (
                  <div className="px-5 py-12 text-center">
                    <FolderOpen className="mx-auto mb-3 size-7 text-muted-foreground" />
                    <h3 className="text-sm font-medium">
                      {search ? "没有匹配的项目" : "暂无可管理的训练项目"}
                    </h3>
                    <p className="mt-2 text-xs text-muted-foreground">
                      {search
                        ? "试试其他项目名称或编号。"
                        : "启用图片项目的矩形框或单多边形标注并完成审核后，即可准备训练数据。"}
                    </p>
                    {search ? (
                      <Button
                        size="sm"
                        className="mt-4"
                        onClick={() => {
                          setSearch("");
                          setProjectPage(0);
                        }}
                      >
                        清除搜索
                      </Button>
                    ) : (
                      <Link
                        to="/dashboard"
                        className="mt-4 inline-block rounded-sm text-xs underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        查看项目列表
                      </Link>
                    )}
                  </div>
                )}
              </div>
              <ListPagination
                page={projectPage}
                total={projects.data?.total ?? 0}
                onPageChange={setProjectPage}
                pageSize={projectPageSize}
                loading={projects.isFetching}
                onPageSizeChange={(size) => {
                  setProjectPageSize(size);
                  setProjectPage(0);
                }}
                label="项目"
              />
            </section>
            <aside
              aria-labelledby="available-warehouses"
              className="overflow-hidden rounded-xl border border-border bg-card"
            >
              <div className="border-b border-border px-4 py-4">
                <h2 id="available-warehouses" className="text-sm font-semibold">
                  可用训练仓库
                </h2>
                <button
                  className="mt-2 inline-flex items-center gap-1 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => patch({ tab: "warehouses" })}
                >
                  查看容器环境
                  <ArrowRight className="size-3.5" />
                </button>
              </div>
              {catalog.isLoading && <TrainingLoading label="正在加载训练仓库…" rows={2} />}
              {catalog.data && <TrainingWarehouseCards catalog={catalog.data} compact />}
              {catalog.data?.items.length === 0 && (
                <p className="p-4 text-xs leading-relaxed text-muted-foreground">
                  尚未配置训练仓库，请联系管理员配置训练镜像与基础权重。
                </p>
              )}
              <p className="flex items-start gap-2 border-t border-border bg-muted/30 p-4 text-2xs leading-relaxed text-muted-foreground">
                <Server aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                训练仓库共用同一 GPU 执行器，实验按队列依次运行。
              </p>
            </aside>
          </div>
        </TabsContent>
        <TabsContent value="queue">
          <TrainingQueuePanel
            query={queue}
            state={state}
            framework={framework}
            onFilter={(updates) => {
              setQueuePage(0);
              patch(updates);
            }}
            onStart={() => patch({ tab: "start" })}
            pagination={
              <ListPagination
                page={queuePage}
                total={queue.data?.total ?? 0}
                onPageChange={setQueuePage}
                pageSize={queuePageSize}
                loading={queue.isFetching}
                onPageSizeChange={(size) => {
                  setQueuePageSize(size);
                  setQueuePage(0);
                }}
                label="训练队列"
              />
            }
          />
        </TabsContent>
        <TabsContent value="warehouses" className="space-y-5">
          <div>
            <h2 className="text-md font-semibold">训练仓库与容器环境</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              以下是管理员配置的训练环境。多个框架可由同一容器提供，所有实验共用同一 GPU 资源。
            </p>
          </div>
          {catalog.isLoading && <TrainingLoading label="正在加载训练执行器…" />}
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
                  <div className="min-w-0">
                    <dt className="text-muted-foreground">资源</dt>
                    <dd className="mt-1 flex min-w-0 items-center gap-1 font-medium">
                      {resourceId ? (
                        <>
                          <span className="min-w-0 truncate">{resourceNode || "共享 GPU"}</span>
                          <CopyValueButton value={resourceId} kind="id" label="复制训练资源 ID" />
                        </>
                      ) : (
                        "尚未配置"
                      )}
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
              <TrainingWarehouseCards catalog={catalog.data} />
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
