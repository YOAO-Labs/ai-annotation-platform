import { useNavigate } from "react-router-dom";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { useProjectReadiness } from "@/hooks/useProjects";
import type { ProjectReadinessSummary, ProjectResponse } from "@/api/projects";

type ReadinessItem = {
  id: string;
  label: string;
  detail: string;
  ready: boolean;
  href: string;
};

function StateBadge({ ready }: { ready: boolean }) {
  return ready ? (
    <Badge variant="success" dot>
      已就绪
    </Badge>
  ) : (
    <Badge variant="warning" dot>
      待处理
    </Badge>
  );
}

export function ProjectReadinessSection({ project }: { project: ProjectResponse }) {
  const navigate = useNavigate();
  const readinessQuery = useProjectReadiness(project.id);

  if (readinessQuery.isLoading && !readinessQuery.data) {
    return (
      <Card>
        <div className="p-6 text-center text-sm text-muted-foreground">正在读取开工准备…</div>
      </Card>
    );
  }

  if (readinessQuery.isError && !readinessQuery.data) {
    return (
      <Card>
        <div role="alert" className="p-5">
          <div className="text-sm font-semibold text-status-danger">无法读取开工准备</div>
          <p className="mt-1 text-sm text-muted-foreground">
            项目数据、建任务作业或成员状态暂时不可用，未就绪项不会被误标为完成。
          </p>
          <Button size="sm" className="mt-3" onClick={() => void readinessQuery.refetch()}>
            重试
          </Button>
        </div>
      </Card>
    );
  }

  const summary = readinessQuery.data;
  if (!summary) return null;

  const readiness = buildReadinessItems(summary);
  const readyCount = readiness.filter((item) => item.ready).length;

  return (
    <Card>
      {readinessQuery.isError && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 border-b border-border bg-status-caution-soft px-4 py-2 text-xs"
        >
          <span>开工准备刷新失败，当前内容已保留。</span>
          <Button size="sm" variant="ghost" onClick={() => void readinessQuery.refetch()}>
            重试
          </Button>
        </div>
      )}
      <div className="border-b border-border px-4 py-3.5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold">开工准备</h3>
            <p className="m-0 mt-1 text-xs text-muted-foreground">
              {readyCount} / {readiness.length} 项已就绪，状态来自当前项目数据
            </p>
          </div>
          <Badge variant={readyCount === readiness.length ? "success" : "warning"}>
            {readyCount === readiness.length ? "可以开工" : "继续配置"}
          </Badge>
        </div>
      </div>
      <div className="divide-y divide-border">
        {readiness.map((item) => (
          <button
            key={item.id}
            type="button"
            className="flex w-full cursor-pointer items-center gap-3 border-0 bg-transparent px-4 py-3 text-left text-inherit hover:bg-muted/50"
            onClick={() => navigate(`/projects/${project.id}/settings?section=${item.href}`)}
            data-testid={`readiness-item-${item.id}`}
          >
            <span
              className={`flex size-7 shrink-0 items-center justify-center rounded-full ${item.ready ? "bg-status-positive-soft text-status-positive" : "bg-status-caution-soft text-status-caution"}`}
            >
              <Icon name={item.ready ? "check" : "clock"} size={14} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2 text-sm font-medium">
                {item.label}
                <StateBadge ready={item.ready} />
              </span>
              <span className="mt-0.5 block text-xs text-muted-foreground">{item.detail}</span>
            </span>
            <Icon name="chevRight" size={13} className="shrink-0 text-muted-foreground" />
          </button>
        ))}
      </div>
    </Card>
  );
}

function buildReadinessItems(summary: ProjectReadinessSummary): ReadinessItem[] {
  const hasValidData = summary.dataset_item_count > 0 || summary.task_count > 0;
  const taskTarget =
    summary.dataset_item_count > 0 ? summary.dataset_item_count : summary.task_count;
  const taskCreated =
    summary.dataset_item_count > 0 ? summary.linked_task_count : summary.task_count;
  const taskProgress = taskTarget > 0 ? Math.min(100, (taskCreated / taskTarget) * 100) : 0;
  const activeTaskCreation = summary.task_creation_active_jobs > 0;
  const latestTaskCreationFailed = summary.latest_task_creation_status === "failed";
  const taskCreationReady =
    hasValidData && !activeTaskCreation && !latestTaskCreationFailed && taskProgress >= 100;
  const classesReady = summary.classes_ready;
  const rolesReady = summary.active_annotator_count > 0 && summary.active_reviewer_count > 0;
  const batchesReady = summary.nonempty_batch_count > 0;
  const activationReady =
    summary.activated_batch_count > 0 && summary.assigned_batch_count > 0 && rolesReady;

  return [
    {
      id: "classes",
      label: "类别规范",
      detail: classesReady ? "已配置至少一个可用类别" : "还没有配置可用类别",
      ready: classesReady,
      href: "classes",
    },
    {
      id: "guide",
      label: "标注指引",
      detail: summary.guide_ready ? "项目指南已发布，成员可在工作台阅读" : "还没有发布项目标注指引",
      ready: summary.guide_ready,
      href: "annotation-guide",
    },
    {
      id: "data",
      label: "有效数据",
      detail: hasValidData
        ? `${summary.linked_dataset_count} 个数据集 · ${summary.dataset_item_count.toLocaleString()} 条数据 · ${summary.task_count.toLocaleString()} 个任务`
        : "尚未关联有效数据，也没有可用的直接上传任务",
      ready: hasValidData,
      href: "datasets",
    },
    {
      id: "tasks",
      label: "任务创建进度",
      detail: activeTaskCreation
        ? `后台正在建任务（${summary.task_creation_active_jobs} 个作业）`
        : latestTaskCreationFailed
          ? "最近一次后台建任务失败，请检查作业后重试"
          : taskTarget > 0
            ? `${taskCreated.toLocaleString()} / ${taskTarget.toLocaleString()} 条任务 · ${Math.round(taskProgress)}%`
            : "有数据后开始创建任务",
      ready: taskCreationReady,
      href: "batches",
    },
    {
      id: "batches",
      label: "批次已建立",
      detail: batchesReady
        ? `${summary.nonempty_batch_count} 个非空批次已建立`
        : summary.batch_count > 0
          ? "批次还没有任务"
          : "还没有建立批次",
      ready: batchesReady,
      href: "batches",
    },
    {
      id: "roles",
      label: "标注员与审核员",
      detail: `${summary.active_annotator_count > 0 ? `已配置 ${summary.active_annotator_count} 名有效标注员` : "缺少有效标注员"} · ${summary.active_reviewer_count > 0 ? `已配置 ${summary.active_reviewer_count} 名有效审核员` : "缺少有效审核员"}`,
      ready: rolesReady,
      href: "members",
    },
    {
      id: "activation",
      label: "批次已激活并分派",
      detail: activationReady
        ? `${summary.assigned_batch_count} 个可执行批次已同时分派标注员和审核员`
        : "至少一个有任务的批次需要激活，并分派有效标注员和审核员",
      ready: activationReady,
      href: "batches",
    },
  ];
}
