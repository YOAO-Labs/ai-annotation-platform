import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { GuideMarkdownView } from "@/components/markdown/GuideMarkdownView";
import type { ProjectResponse } from "@/api/projects";
import type { MyBatchItem } from "@/api/dashboard";
import { useOnboardingProjectSummary } from "@/hooks/useDashboard";
import { useGuideAssets } from "@/hooks/useGuideAssets";
import { useOnboardingProjectState } from "@/hooks/useOnboardingProjectState";
import { annotationGuideVersion, isGuideSeen } from "@/utils/annotationGuide";
import {
  buildWorkbenchUrl,
  currentWorkbenchReturnTo,
  getRememberedWorkbenchTask,
} from "@/utils/workbenchNavigation";
import { useAuthStore } from "@/stores/authStore";

type Step = {
  id: "guide" | "task" | "annotation" | "result";
  label: string;
  detail: string;
  done: boolean;
  actionLabel: string;
  onAction: () => void;
};

function projectBatches(projectId: string, batches: MyBatchItem[]) {
  return batches
    .filter((batch) => batch.project_id === projectId)
    .sort((a, b) => {
      const priority = (status: string) =>
        status === "rejected" ? 0 : status === "annotating" ? 1 : status === "active" ? 2 : 3;
      return priority(a.status) - priority(b.status);
    });
}

export function StartChecklistCard({
  project,
  batches,
}: {
  project: ProjectResponse;
  batches: MyBatchItem[];
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const userId = useAuthStore((state) => state.user?.id);
  const [guideOpen, setGuideOpen] = useState(false);
  const [resultOpen, setResultOpen] = useState(false);
  const guideVersion = annotationGuideVersion(project.annotation_guide);
  const { dismissed, guideRead, isSaving, saveError, dismiss, reopen, retry, markGuideRead } =
    useOnboardingProjectState(project.id, guideVersion);
  const summaryQuery = useOnboardingProjectSummary(project.id);
  const summary = summaryQuery.isError ? undefined : summaryQuery.data;
  const { resolveImage, imageScope } = useGuideAssets(project.id);
  const batchesForProject = useMemo(
    () => projectBatches(project.id, batches),
    [batches, project.id],
  );
  const activeBatch = batchesForProject[0] ?? null;
  const memoryScope = userId ? `${userId}:annotate` : "annotate";
  const rememberedBatch = batchesForProject.find((batch) =>
    getRememberedWorkbenchTask(batch.batch_id, undefined, memoryScope),
  );
  const rememberedTaskId = rememberedBatch
    ? getRememberedWorkbenchTask(rememberedBatch.batch_id, undefined, memoryScope)
    : null;
  const guideExists = Boolean(project.annotation_guide?.trim());
  const hasAssignedTask =
    (summary?.assigned_task_count ?? 0) > 0 ||
    batchesForProject.some((batch) => batch.total_tasks > 0);
  const hasOpenedTask = (summary?.opened_task_count ?? 0) > 0 || Boolean(rememberedTaskId);
  const hasAnnotation = (summary?.saved_annotation_count ?? 0) > 0;
  const hasSubmissionResult = (summary?.reviewed_task_count ?? 0) > 0;

  const openProjectWork = (taskId?: string | null) => {
    navigate(
      buildWorkbenchUrl(project.id, {
        batchId: taskId
          ? taskId === rememberedTaskId
            ? rememberedBatch?.batch_id
            : undefined
          : activeBatch?.batch_id,
        taskId: taskId ?? undefined,
        returnTo: currentWorkbenchReturnTo(location),
      }),
    );
  };

  const steps: Step[] = [
    {
      id: "guide",
      label: "阅读项目指引",
      detail: guideExists
        ? "先了解类别定义、边界和常见反例"
        : `项目负责人尚未发布标注指引${project.owner_name ? `，请联系 ${project.owner_name}` : "，请联系项目负责人"}`,
      done: guideExists && guideRead,
      actionLabel: guideExists ? "打开指引" : "等待负责人发布",
      onAction: () => {
        if (guideExists) setGuideOpen(true);
      },
    },
    {
      id: "task",
      label: "打开分派任务",
      detail: hasAssignedTask
        ? hasOpenedTask
          ? "已记录打开任务，可从上次位置继续"
          : "已有分派任务，打开后才会完成此步"
        : "等待项目负责人创建并分派任务",
      done: hasOpenedTask,
      actionLabel: hasOpenedTask ? "再次打开任务" : "打开任务",
      onAction: () => openProjectWork(rememberedTaskId),
    },
    {
      id: "annotation",
      label: "完成首条标注",
      detail: hasAnnotation ? "已检测到你保存的有效标注" : "在工作台保存一条有效标注",
      done: hasAnnotation,
      actionLabel: hasAnnotation ? "继续标注" : "开始标注",
      onAction: () => openProjectWork(rememberedTaskId),
    },
    {
      id: "result",
      label: "查看送审结果",
      detail: hasSubmissionResult
        ? "已检测到审核产生的通过或退回结果"
        : "送审后可在这里看到审核结果",
      done: hasSubmissionResult,
      actionLabel: hasSubmissionResult ? "再次查看结果" : "查看结果",
      onAction: () => setResultOpen(true),
    },
  ];

  const readFromStorage = isGuideSeen(userId, project.id, guideVersion);
  const visibleGuideRead = guideRead || readFromStorage;
  if (dismissed) {
    return (
      <Card data-testid="start-checklist-collapsed">
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <div className="text-sm font-semibold">开工清单已跳过</div>
            <div className="mt-1 text-xs text-muted-foreground">项目状态仍会继续更新。</div>
          </div>
          <Button size="sm" onClick={() => void reopen()} disabled={isSaving}>
            重新打开清单
          </Button>
        </div>
        {saveError && (
          <div
            role="alert"
            className="flex items-center gap-2 border-t border-border px-4 py-2 text-xs text-status-danger"
          >
            <span>
              <CopyableError message={saveError} />
            </span>
            <button type="button" className="underline" onClick={() => void retry()}>
              重试
            </button>
          </div>
        )}
      </Card>
    );
  }

  const doneCount = steps.filter((step) =>
    step.id === "guide" ? visibleGuideRead : step.done,
  ).length;

  return (
    <Card data-testid="start-checklist-card">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3.5">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="m-0 text-sm font-semibold">开工清单</h2>
            <Badge variant={doneCount === steps.length ? "success" : "accent"}>
              {doneCount} / {steps.length}
            </Badge>
          </div>
          <p className="m-0 mt-1 text-xs text-muted-foreground">
            {project.name} · 按真实任务状态更新
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={() => void dismiss()} disabled={isSaving}>
          跳过
        </Button>
      </div>
      <div className="grid gap-2 p-3">
        {steps.map((step) => {
          const done = step.id === "guide" ? visibleGuideRead : step.done;
          return (
            <div
              key={step.id}
              className={`flex items-center gap-3 rounded-md border px-3 py-2.5 ${done ? "border-status-positive/30 bg-status-positive-soft/40" : "border-border bg-card"}`}
              data-testid={`start-checklist-step-${step.id}`}
            >
              <span
                className={`flex size-7 shrink-0 items-center justify-center rounded-full ${done ? "bg-status-positive-soft text-status-positive" : "bg-muted text-muted-foreground"}`}
              >
                <Icon name={done ? "check" : "circleDot"} size={14} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">{step.label}</div>
                <div className="mt-0.5 text-xs text-muted-foreground">{step.detail}</div>
              </div>
              {(step.id !== "guide" || guideExists) && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={step.onAction}
                  disabled={
                    step.id === "result"
                      ? !hasSubmissionResult
                      : step.id !== "guide" && !hasAssignedTask
                  }
                >
                  {done && step.id === "guide" ? "再次阅读" : step.actionLabel}
                </Button>
              )}
            </div>
          );
        })}
      </div>
      {summaryQuery.isError && (
        <div
          role="alert"
          className="flex items-center gap-2 border-t border-border px-4 py-2 text-xs text-status-danger"
        >
          <span>无法读取项目真实进度，当前清单不会据此标记完成。</span>
          <button type="button" className="underline" onClick={() => void summaryQuery.refetch()}>
            重试
          </button>
        </div>
      )}
      {saveError && (
        <div
          role="alert"
          className="flex items-center gap-2 border-t border-border px-4 py-2 text-xs text-status-danger"
        >
          <span>
            <CopyableError message={saveError} />
          </span>
          <button type="button" className="underline" onClick={() => void retry()}>
            重试
          </button>
        </div>
      )}
      {resultOpen && hasSubmissionResult && summary && (
        <div className="border-t border-border px-4 py-3" data-testid="start-checklist-result">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">
              最近审核结果 · {summary.reviewed_task_display_id ?? "任务"}
            </div>
            <Button size="sm" variant="ghost" onClick={() => setResultOpen(false)}>
              收起结果
            </Button>
          </div>
          <p className="mb-0 mt-2 text-sm">
            {summary.reviewed_task_status === "rejected"
              ? "任务已退回，请按审核意见修改。"
              : "任务已通过审核。"}
          </p>
          {summary.reviewed_task_status === "rejected" && (
            <p className="mb-0 mt-2 whitespace-pre-wrap text-xs text-muted-foreground">
              退回理由：{summary.reviewed_task_reason || "审核员未填写退回理由"}
            </p>
          )}
        </div>
      )}
      {guideOpen && guideExists && (
        <div
          className="border-t border-border bg-muted/30 px-4 py-3"
          data-testid="start-checklist-guide"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">项目标注指引</div>
            <Button size="sm" variant="ghost" onClick={() => setGuideOpen(false)}>
              收起
            </Button>
          </div>
          <div className="max-h-[32rem] overflow-auto rounded-md border border-border bg-card p-3">
            <GuideMarkdownView
              content={project.annotation_guide!.trim()}
              resolveImage={resolveImage}
              imageScope={imageScope}
            />
          </div>
          <Button
            size="sm"
            className="mt-3"
            disabled={visibleGuideRead || isSaving}
            onClick={() => void markGuideRead()}
          >
            {isSaving ? "保存中…" : visibleGuideRead ? "已确认阅读" : "确认已阅读"}
          </Button>
        </div>
      )}
    </Card>
  );
}
