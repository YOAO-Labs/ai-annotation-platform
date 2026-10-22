import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Plus } from "lucide-react";
import { activeLearningApi, type SelectionTask } from "@/api/activeLearning";
import { PageContainer } from "@/components/layout/PageContainer";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useTrainingViewOwner } from "./trainingViewOwner";
import { PickerPage } from "./SampleEvaluationCreate";
import {
  activeLearningTask,
  roundStatus,
  runningStates,
  useActiveLearningScope,
} from "./useActiveLearning";
import { trainingInputClass } from "./TrainingParameterField";
import { trainingTaskLabel } from "./trainingPresentation";

export function ActiveLearningPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const value = params.get("task");
  const task = value === "detect" || value === "segment" ? value : undefined;
  const scope = useActiveLearningScope(id);
  return (
    <RoundList
      key={scope}
      projectId={id}
      scope={scope}
      task={task}
      onTask={(next) =>
        setParams((previous) => {
          const updated = new URLSearchParams(previous);
          if (next) updated.set("task", next);
          else updated.delete("task");
          return updated;
        })
      }
    />
  );
}
function RoundList({
  projectId,
  scope,
  task,
  onTask,
}: {
  projectId: string;
  scope: string;
  task?: SelectionTask;
  onTask: (task?: SelectionTask) => void;
}) {
  const { assert } = useTrainingViewOwner(projectId, scope);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const list = useQuery({
    queryKey: ["active-learning-rounds", projectId, scope, page, pageSize, task],
    retry: false,
    queryFn: async ({ signal }) => {
      assert(signal);
      const value = await activeLearningApi.list(
        projectId,
        page * pageSize,
        { signal },
        task,
        pageSize,
      );
      assert(signal);
      if (
        value.items.some((item) => item.project_id !== projectId || item.spec.round_id !== item.id)
      )
        throw new Error("轮次列表所属项目或身份不符");
      if (
        task &&
        value.items.some(
          (item) => activeLearningTask(item.spec.model_ref?.task ?? item.spec.task) !== task,
        )
      )
        throw new Error("轮次列表任务与当前筛选不一致");
      return value;
    },
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => runningStates.has(item.status)) ? 5000 : false,
  });
  return (
    <PageContainer>
      <Link
        className="inline-flex items-center gap-2 text-xs text-muted-foreground"
        to={`/projects/${projectId}/learning?tab=models`}
      >
        <ArrowLeft size={14} />
        返回模型版本
      </Link>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">主动学习人工选样</h1>
        <Link
          className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-xs font-medium text-primary hover:bg-muted"
          to={`/projects/${projectId}/learning/active-learning/new`}
        >
          <Plus size={14} />
          新建选样轮次
        </Link>
      </div>
      <p className="mb-5 text-sm text-muted-foreground">
        用固定模型提出下一批人工标注建议，保留等量随机对照。所有接纳任务仍走普通标注与审核流程。
      </p>
      <label className="mb-4 grid w-fit gap-2 text-xs">
        选样任务
        <select
          className={trainingInputClass}
          value={task ?? ""}
          onChange={(event) => {
            setPage(0);
            onTask((event.target.value || undefined) as SelectionTask | undefined);
          }}
        >
          <option value="">全部任务</option>
          <option value="detect">图片目标检测</option>
          <option value="segment">图片实例分割</option>
        </select>
      </label>
      {list.isPending && <p role="status">加载选样轮次…</p>}
      {list.error && (
        <div role="alert" className="text-sm text-status-danger">
          <CopyableError message={list.error.message} />
          <Button className="ml-3" size="sm" onClick={() => void list.refetch()}>
            重试
          </Button>
        </div>
      )}
      {!list.error && list.data?.items.length === 0 && (
        <p
          role="status"
          className="rounded-lg border border-border p-5 text-sm text-muted-foreground"
        >
          还没有选样轮次。准备未分派的图片和就绪模型后，可显式创建首轮。
        </p>
      )}
      <div className="divide-y divide-border">
        {!list.error &&
          list.data?.items.map((round) => (
            <Link
              key={round.id}
              className="flex flex-wrap items-center justify-between gap-3 py-4 text-sm hover:bg-muted/50"
              to={`/projects/${projectId}/learning/active-learning/${round.id}`}
            >
              <div className="min-w-0">
                <span className="break-words font-medium">{round.name}</span>
                <p className="mb-0 mt-1 text-xs text-muted-foreground">
                  {trainingTaskLabel(round.spec.model_ref?.task ?? round.spec.task)} ·{" "}
                  {round.spec.candidate_count} 个候选 · 每组 {round.spec.batch_size} 张 · 已接纳{" "}
                  {round.accepted_tasks} 张
                </p>
              </div>
              <Badge
                variant={
                  round.status === "succeeded"
                    ? "success"
                    : round.status === "failed"
                      ? "danger"
                      : "default"
                }
              >
                {roundStatus[round.status]}
              </Badge>
            </Link>
          ))}
      </div>
      <PickerPage
        page={page}
        pageSize={pageSize}
        loading={list.isFetching}
        total={list.data?.total ?? 0}
        onChange={setPage}
        label="选样轮次"
        onPageSizeChange={(size) => {
          setPageSize(size);
          setPage(0);
        }}
      />
    </PageContainer>
  );
}
