import { useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Database, FlaskConical, GitBranch, Layers } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { batchesApi } from "@/api/batches";
import { asyncJobsApi } from "@/api/asyncJobs";
import { tasksApi } from "@/api/tasks";
import {
  trainingDatasetsApi,
  type TrainingDatasetSummary,
  type TrainingDatasetPreflight,
  type TrainingDatasetSelection,
} from "@/api/trainingDatasets";
import { useProject } from "@/hooks/useProjects";
import { PageContainer } from "@/components/layout/PageContainer";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { ProjectTrainingPanel } from "./ProjectTrainingPanel";
import { ProjectModelsPanel } from "./ProjectModelsPanel";
import { ProjectLearningPolicyPanel } from "./ProjectLearningPolicyPanel";
import { TrainingDatasetInsights } from "./TrainingDatasetInsights";
import { useAuthStore } from "@/stores/authStore";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const statuses: Record<string, string> = {
  pending: "等待构建",
  running: "构建中",
  ready: "已就绪",
  failed: "构建失败",
  cancelled: "已取消",
};
const exclusionReasons: Record<string, string> = {
  unsupported_media: "不是图片任务",
  unusable_sample: "本轮标记为损坏、模糊或不可用",
  not_reviewed: "尚未审核通过",
  unconfirmed_negative: "空标注尚未明确确认为无目标",
};

export function ProjectLearningPage() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const userId = useAuthStore((state) => state.user?.id);
  const token = useAuthStore((state) => state.token);
  const owner = useMemo(
    () => ({ id, userId, token, key: crypto.randomUUID() }),
    [id, userId, token],
  );
  if (params.get("new") === "1") {
    const next = new URLSearchParams();
    const framework = params.get("framework");
    if (framework === "ultralytics" || framework === "libreyolo") next.set("framework", framework);
    if (params.get("run")) next.set("return_run", params.get("run")!);
    return <Navigate replace to={`/projects/${id}/learning/new${next.size ? `?${next}` : ""}`} />;
  }
  return <ProjectLearningContent key={owner.key} projectId={id} scope={owner.key} />;
}

function ProjectLearningContent({ projectId, scope }: { projectId: string; scope: string }) {
  const [params, setParams] = useSearchParams();
  const tab = ["training", "data", "models", "automation"].includes(params.get("tab") ?? "")
    ? params.get("tab")!
    : "training";
  const changeTab = (value: string) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("tab", value);
      next.delete("new");
      return next;
    });
  const client = useQueryClient();
  const project = useProject(projectId);
  const [name, setName] = useState("");
  const [batchId, setBatchId] = useState("");
  const [parentId, setParentId] = useState("");
  const [mode, setMode] = useState<"reviewed" | "manager_confirmed">("reviewed");
  const [seed, setSeed] = useState("42");
  const [fraction, setFraction] = useState("20");
  const [selected, setSelected] = useState<string[]>([]);
  const [negatives, setNegatives] = useState<string[]>([]);
  const [taskPage, setTaskPage] = useState(0);
  const [page, setPage] = useState(0);
  const detailId = params.get("dataset") ?? "";
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const setDetailId = (value: string) => {
    if (!alive.current) return;
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (value) next.set("dataset", value);
      else next.delete("dataset");
      ["data_class", "data_split", "data_kind", "data_page"].forEach((key) => next.delete(key));
      if (!value || next.get("data_baseline") === value) {
        next.delete("data_baseline");
        next.delete("data_change");
      }
      if (!value) next.delete("data_view");
      return next;
    });
  };
  const [report, setReport] = useState<TrainingDatasetPreflight | null>(null);
  const [error, setError] = useState("");
  const requestId = useRef(crypto.randomUUID());
  const checkedDigest = useRef<string | null>(null);
  const selection: TrainingDatasetSelection = useMemo(
    () => ({
      batch_id: batchId || null,
      parent_id: parentId || null,
      mode,
      seed: Number(seed),
      validation_fraction: Number(fraction) / 100,
      task_ids: mode === "manager_confirmed" ? selected : [],
      confirmed_negative_ids: mode === "manager_confirmed" ? negatives : [],
    }),
    [batchId, parentId, mode, seed, fraction, selected, negatives],
  );
  const versions = useQuery({
    queryKey: ["training-datasets", projectId, scope, page],
    queryFn: () => trainingDatasetsApi.list(projectId, page * 20),
    enabled: tab === "data",
    refetchInterval: (query) =>
      tab === "data" &&
      query.state.data?.items.some((item) => ["pending", "running"].includes(item.status))
        ? 2000
        : false,
  });
  const batches = useQuery({
    queryKey: ["batches", projectId, scope],
    queryFn: () => batchesApi.list(projectId),
    enabled: tab === "data",
  });
  const taskList = useQuery({
    queryKey: ["learning-tasks", projectId, scope, batchId, taskPage],
    queryFn: () =>
      tasksApi.listByProject(projectId, {
        batch_id: batchId || undefined,
        limit: 25,
        offset: taskPage * 25,
      }),
    enabled: tab === "data" && mode === "manager_confirmed",
  });
  const detail = useQuery({
    queryKey: ["training-dataset", projectId, scope, detailId],
    queryFn: ({ signal }) => trainingDatasetsApi.getSummary(projectId, detailId, { signal }),
    enabled: tab === "data" && !!detailId,
    refetchInterval: (query) =>
      query.state.data && ["pending", "running"].includes(query.state.data.status) ? 2000 : false,
  });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["training-datasets", projectId] });
    void client.invalidateQueries({ queryKey: ["training-dataset", projectId] });
  };
  const preflight = useMutation({
    mutationFn: () => trainingDatasetsApi.preflight(projectId, selection),
    onSuccess: (value) => {
      if (checkedDigest.current && checkedDigest.current !== value.digest)
        requestId.current = crypto.randomUUID();
      checkedDigest.current = value.digest;
      setReport(value);
      setError("");
    },
    onError: (reason: Error) => {
      setReport(null);
      setError(reason.message);
    },
  });
  const create = useMutation({
    mutationFn: () =>
      trainingDatasetsApi.create(projectId, {
        ...selection,
        name: name.trim(),
        request_id: requestId.current,
        preflight_digest: report!.digest,
      }),
    onSuccess: (value) => {
      setDetailId(value.id);
      setName("");
      setReport(null);
      requestId.current = crypto.randomUUID();
      checkedDigest.current = null;
      refresh();
    },
    onError: (reason: Error) => {
      setError(reason.message);
      setReport(null);
    },
  });
  const action = useMutation({
    mutationFn: async ({
      kind,
      version,
    }: {
      kind: "retry" | "download" | "delete" | "cancel";
      version: TrainingDatasetSummary;
    }) => {
      if (kind === "download") {
        const result = await trainingDatasetsApi.download(projectId, version.id);
        if (!alive.current) return;
        const link = document.createElement("a");
        link.href = result.url;
        link.download = `${version.name}.zip`;
        link.rel = "noopener";
        link.click();
      } else if (kind === "retry") await trainingDatasetsApi.retry(projectId, version.id);
      else if (kind === "cancel" && version.async_job_id)
        await asyncJobsApi.cancel(version.async_job_id);
      else if (
        await confirmDialog({
          title: `删除训练数据版本“${version.name}”`,
          description: "将删除冻结图片和训练包。被后续版本引用的数据不能删除。",
          tone: "danger",
          confirmLabel: "删除数据版本",
        })
      ) {
        if (!alive.current) return;
        await trainingDatasetsApi.remove(projectId, version.id);
        if (!alive.current) return;
        setParams((previous) => {
          if (previous.get("dataset") !== version.id) return previous;
          const next = new URLSearchParams(previous);
          [
            "dataset",
            "data_baseline",
            "data_view",
            "data_change",
            "data_class",
            "data_split",
            "data_kind",
            "data_page",
          ].forEach((key) => next.delete(key));
          return next;
        });
      }
    },
    onSuccess: refresh,
    onError: (reason: Error) => setError(reason.message),
  });
  const busy = preflight.isPending || create.isPending;
  const updateSelection = (update: () => void) => {
    update();
    requestId.current = crypto.randomUUID();
    checkedDigest.current = null;
    setReport(null);
    setError("");
  };
  const toggleTask = (id: string) =>
    updateSelection(() => {
      setSelected((current) =>
        current.includes(id) ? current.filter((key) => key !== id) : [...current, id],
      );
      setNegatives((current) => current.filter((key) => key !== id));
    });
  const unsupported = project.data && (project.data.data_type ?? "image") !== "image";

  return (
    <PageContainer>
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link
            to="/training"
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-3.5" />
            训练队列
          </Link>
          <h1 className="mt-2 text-xl font-semibold">{project.data?.name ?? "项目训练"}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            从标注数据到训练实验，再到模型验证与发布。
          </p>
        </div>
        <Link
          to={`/projects/${projectId}/settings`}
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          项目设置
        </Link>
      </header>
      <Tabs value={tab} activationMode="manual" onValueChange={changeTab}>
        <TabsList
          variant="line"
          aria-label="项目训练功能"
          className="mb-5 w-full justify-start gap-4 overflow-x-auto border-b border-border pb-2"
        >
          <TabsTrigger value="training" className="flex-none">
            <FlaskConical className="size-4" />
            训练实验
          </TabsTrigger>
          <TabsTrigger value="data" className="flex-none">
            <Database className="size-4" />
            训练数据
          </TabsTrigger>
          <TabsTrigger value="models" className="flex-none">
            <Layers className="size-4" />
            模型与发布
          </TabsTrigger>
          <TabsTrigger value="automation" className="flex-none">
            <GitBranch className="size-4" />
            自动训练
          </TabsTrigger>
        </TabsList>
        <TabsContent value="data" forceMount className="data-[state=inactive]:hidden">
          {(error || versions.error || project.error) && (
            <p
              role="alert"
              className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
            >
              {error || versions.error?.message || project.error?.message}
            </p>
          )}
          {unsupported ? (
            <p className="rounded-md bg-muted p-4 text-sm text-muted-foreground">
              当前支持图片矩形框检测数据。请切换到图片项目。
            </p>
          ) : (
            <section
              aria-labelledby="create-dataset-title"
              className="mb-7 border-b border-border pb-6"
            >
              <h2 id="create-dataset-title" className="mb-1 text-sm font-semibold">
                创建训练数据版本
              </h2>
              <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
                冻结本轮图片、标注与类别，生成包含原图的训练包。后续修改标注不会改变已有版本。
              </p>
              <fieldset disabled={busy} className="grid gap-4 border-0 p-0 md:grid-cols-3">
                <label className="grid gap-1.5 text-xs font-medium">
                  版本名称
                  <input
                    className={inputClass}
                    value={name}
                    maxLength={120}
                    onChange={(event) => {
                      setName(event.target.value);
                      requestId.current = crypto.randomUUID();
                    }}
                    placeholder="例如：路口车辆首轮数据"
                  />
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  数据范围
                  <select
                    className={inputClass}
                    value={batchId}
                    onChange={(event) =>
                      updateSelection(() => {
                        setBatchId(event.target.value);
                        setSelected([]);
                        setNegatives([]);
                        setTaskPage(0);
                      })
                    }
                  >
                    <option value="">整个项目</option>
                    {batches.data?.map((batch) => (
                      <option key={batch.id} value={batch.id}>
                        {batch.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  标注纳入方式
                  <select
                    className={inputClass}
                    value={mode}
                    onChange={(event) =>
                      updateSelection(() => {
                        setMode(event.target.value as typeof mode);
                        setSelected([]);
                        setNegatives([]);
                      })
                    }
                  >
                    <option value="reviewed">已审核通过</option>
                    <option value="manager_confirmed">负责人明确确认</option>
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  继承数据划分
                  <select
                    className={inputClass}
                    value={parentId}
                    onChange={(event) => updateSelection(() => setParentId(event.target.value))}
                  >
                    <option value="">首次划分</option>
                    {versions.data?.items
                      .filter((version) => version.status === "ready")
                      .map((version) => (
                        <option key={version.id} value={version.id}>
                          {version.name}
                        </option>
                      ))}
                  </select>
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  验证集比例（%）
                  <input
                    type="number"
                    min={5}
                    max={50}
                    className={inputClass}
                    value={fraction}
                    onChange={(event) => updateSelection(() => setFraction(event.target.value))}
                  />
                </label>
                <label className="grid gap-1.5 text-xs font-medium">
                  划分随机种子
                  <input
                    type="number"
                    min={0}
                    max={2147483647}
                    className={inputClass}
                    value={seed}
                    onChange={(event) => updateSelection(() => setSeed(event.target.value))}
                  />
                </label>
                {mode === "manager_confirmed" && (
                  <div className="md:col-span-3">
                    <p className="mb-2 text-xs text-muted-foreground">
                      选择本轮确认用于训练的任务。空白图片只有勾选“确认为无目标”后才会作为负样本。
                    </p>
                    {taskList.error && (
                      <p role="alert" className="text-xs text-status-danger">
                        {taskList.error.message}
                      </p>
                    )}
                    <div className="max-h-64 overflow-auto rounded-md border border-border">
                      {taskList.data?.items.map((task) => (
                        <div
                          key={task.id}
                          className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-3 py-2 text-xs last:border-b-0"
                        >
                          <label className="flex min-w-0 items-center gap-2">
                            <input
                              type="checkbox"
                              checked={selected.includes(task.id)}
                              onChange={() => toggleTask(task.id)}
                            />
                            <span className="truncate">
                              {task.display_id} · {task.file_name}
                            </span>
                          </label>
                          <label className="flex items-center gap-2 text-muted-foreground">
                            <input
                              type="checkbox"
                              disabled={!selected.includes(task.id)}
                              checked={negatives.includes(task.id)}
                              onChange={() =>
                                updateSelection(() =>
                                  setNegatives((current) =>
                                    current.includes(task.id)
                                      ? current.filter((key) => key !== task.id)
                                      : [...current, task.id],
                                  ),
                                )
                              }
                            />
                            确认为无目标
                          </label>
                        </div>
                      ))}
                    </div>
                    <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
                      <span>已选择 {selected.length} 个任务</span>
                      <Button
                        size="sm"
                        disabled={!taskPage || taskList.isFetching}
                        onClick={() => setTaskPage((value) => value - 1)}
                      >
                        上一页
                      </Button>
                      <Button
                        size="sm"
                        disabled={taskList.isFetching || (taskList.data?.items.length ?? 0) < 25}
                        onClick={() => setTaskPage((value) => value + 1)}
                      >
                        下一页
                      </Button>
                    </div>
                  </div>
                )}
              </fieldset>
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <Button
                  disabled={
                    busy || (mode === "manager_confirmed" && !selected.length) || !seed || !fraction
                  }
                  onClick={() => preflight.mutate()}
                >
                  {preflight.isPending ? "正在检查素材…" : "检查训练数据"}
                </Button>
                <Button
                  variant="primary"
                  disabled={busy || !report?.ready || !name.trim()}
                  onClick={() => create.mutate()}
                >
                  {create.isPending ? "正在创建…" : "创建数据版本"}
                </Button>
              </div>
              {report && (
                <div className="mt-4 rounded-md bg-muted p-3 text-xs" aria-live="polite">
                  <p className="font-medium">
                    {report.ready ? "数据检查通过" : "需要处理数据问题"}
                  </p>
                  <p className="mt-2 text-muted-foreground">
                    正样本 {report.summary.positives} · 负样本 {report.summary.negatives} · 排除{" "}
                    {report.summary.excluded} · 类别 {report.classes.length}
                  </p>
                  {report.issues.map((issue, index) => (
                    <p className="mt-2 text-status-danger" key={index}>
                      {String(issue.message ?? issue.code)}
                      {issue.task_id ? `（${issue.task_id}）` : ""}
                    </p>
                  ))}
                  {report.excluded.length > 0 && (
                    <details className="mt-2">
                      <summary className="cursor-pointer">查看排除原因</summary>
                      <ul className="mt-2 space-y-1">
                        {report.excluded.map((item, index) => (
                          <li key={index}>
                            任务 {String(item.task_id).slice(0, 8)}：
                            {exclusionReasons[String(item.code)] ?? "不符合当前数据条件"}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}
            </section>
          )}
          <section aria-labelledby="dataset-versions-title">
            <div className="mb-3 flex items-center justify-between">
              <h2 id="dataset-versions-title" className="text-sm font-semibold">
                数据版本
              </h2>
              <span className="text-xs tabular-nums text-muted-foreground">
                共 {versions.data?.total ?? 0} 个
              </span>
            </div>
            {versions.isLoading && (
              <p role="status" className="py-8 text-sm text-muted-foreground">
                正在加载数据版本…
              </p>
            )}
            {versions.data?.items.length === 0 && (
              <p className="py-8 text-sm text-muted-foreground">
                尚无训练数据版本。先检查上方的数据范围，再创建第一轮数据。
              </p>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-border text-muted-foreground">
                  <tr>
                    <th className="py-2 pr-4 font-medium">版本</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 font-medium">创建时间</th>
                    <th className="py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {versions.data?.items.map((version) => (
                    <tr key={version.id} className="border-b border-border">
                      <td className="min-w-40 py-3 pr-4">
                        <button
                          className="cursor-pointer text-left font-medium hover:underline"
                          onClick={() => setDetailId(version.id)}
                        >
                          {version.name}
                        </button>
                        {["pending", "running"].includes(version.status) && (
                          <div
                            className="mt-2"
                            role="progressbar"
                            aria-label={`${version.name} 构建进度`}
                            aria-valuenow={version.progress_pct}
                            aria-valuemin={0}
                            aria-valuemax={100}
                          >
                            <ProgressBar value={version.progress_pct} />
                          </div>
                        )}
                        {version.error_message && (
                          <p className="mt-1 max-w-sm text-status-danger">
                            {version.error_message}
                          </p>
                        )}
                      </td>
                      <td className="px-3 py-3">
                        <Badge
                          variant={
                            version.status === "ready"
                              ? "success"
                              : version.status === "failed"
                                ? "danger"
                                : "default"
                          }
                        >
                          {statuses[version.status] ?? version.status}
                        </Badge>
                      </td>
                      <td className="whitespace-nowrap px-3 py-3 tabular-nums text-muted-foreground">
                        {new Date(version.created_at).toLocaleString("zh-CN")}
                      </td>
                      <td className="py-3">
                        <div className="flex justify-end gap-1">
                          {["pending", "running"].includes(version.status) &&
                            version.async_job_id && (
                              <Button
                                size="sm"
                                variant="ghost"
                                disabled={action.isPending}
                                onClick={() => action.mutate({ kind: "cancel", version })}
                              >
                                取消构建
                              </Button>
                            )}
                          {version.status === "ready" && (
                            <Button
                              size="sm"
                              disabled={action.isPending}
                              onClick={() => action.mutate({ kind: "download", version })}
                            >
                              下载训练包
                            </Button>
                          )}
                          {["failed", "cancelled"].includes(version.status) && (
                            <Button
                              size="sm"
                              disabled={action.isPending}
                              onClick={() => action.mutate({ kind: "retry", version })}
                            >
                              重试
                            </Button>
                          )}
                          {!["pending", "running"].includes(version.status) && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={action.isPending}
                              onClick={() => action.mutate({ kind: "delete", version })}
                            >
                              删除
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <Button size="sm" disabled={!page} onClick={() => setPage((value) => value - 1)}>
                上一页
              </Button>
              <Button
                size="sm"
                disabled={(page + 1) * 20 >= (versions.data?.total ?? 0)}
                onClick={() => setPage((value) => value + 1)}
              >
                下一页
              </Button>
            </div>
          </section>
          {detailId && detail.isLoading && (
            <p role="status" className="mt-5 text-sm text-muted-foreground">
              正在加载所选数据版本…
            </p>
          )}
          {detail.error && (
            <p role="alert" className="mt-5 text-sm text-status-danger">
              {detail.error.message}
            </p>
          )}
          {tab === "data" && detail.data && (
            <TrainingDatasetInsights
              projectId={projectId}
              version={detail.data}
              onClose={() => setDetailId("")}
            />
          )}
        </TabsContent>
        <TabsContent value="training">
          {unsupported ? (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前支持图片矩形框检测训练，请切换到图片项目。
            </p>
          ) : (
            <ProjectTrainingPanel
              projectId={projectId}
              initialRunId={params.get("run") ?? ""}
              onRunSelect={(runId) =>
                setParams((previous) => {
                  const next = new URLSearchParams(previous);
                  next.set("tab", "training");
                  next.set("run", runId);
                  next.delete("new");
                  return next;
                })
              }
            />
          )}
        </TabsContent>
        <TabsContent value="models">
          {unsupported && (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前模型回流支持图片矩形框检测项目。
            </p>
          )}
          {!unsupported && (
            <ProjectModelsPanel
              projectId={projectId}
              classes={(project.data?.tool_bindings?.bbox?.classes ?? []).map((item) => item.name)}
            />
          )}
        </TabsContent>
        <TabsContent value="automation">
          {unsupported && (
            <p className="rounded-lg bg-muted p-5 text-sm text-muted-foreground">
              当前自动训练支持图片矩形框检测项目。
            </p>
          )}
          {!unsupported && <ProjectLearningPolicyPanel projectId={projectId} />}
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
