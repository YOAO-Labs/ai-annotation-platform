import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useTask, useAnnotations, useReviewClaim } from "@/hooks/useTasks";
import { usePredictions } from "@/hooks/usePredictions";
import { useProject } from "@/hooks/useProjects";
import { ImageStage } from "@/pages/Workbench/stage/ImageStage";
import {
  annotationToBox,
  collectOccludedKeys,
  predictionsToBoxes,
} from "@/pages/Workbench/state/transforms";
import { useViewportTransform } from "@/pages/Workbench/state/useViewportTransform";
import { CommentsPanel } from "@/pages/Workbench/shell/CommentsPanel";
import { ReviewerMiniPanel } from "./ReviewerMiniPanel";
import { useAuthStore } from "@/stores/authStore";
import type { ReviewClaimResponse } from "@/types";
import {
  useWorkbenchImageSource,
  workbenchImagePreviewUrl,
} from "@/pages/Workbench/stage/useWorkbenchImageSource";
import { useSessionStats } from "@/pages/Workbench/state/useSessionStats";

type DiffMode = "final" | "raw" | "diff";

// v0.8.8 · skip_reason 枚举到中文标签
function skipReasonLabel(reason: string): string {
  switch (reason) {
    case "image_corrupt":
      return "图片损坏";
    case "no_target":
      return "无标注目标";
    case "unclear":
      return "标注规则不清";
    case "other":
      return "其他";
    default:
      return reason;
  }
}

interface ReviewWorkbenchProps {
  taskId: string;
  onApprove: () => void;
  onReject: () => void;
  onPrev?: () => void;
  onNext?: () => void;
}

export function ReviewWorkbench({
  taskId,
  onApprove,
  onReject,
  onPrev,
  onNext,
}: ReviewWorkbenchProps) {
  const { data: task } = useTask(taskId);
  const { source: imageSource, retry: retryImagePyramid } = useWorkbenchImageSource(
    task,
    task?.dataset_item_id ?? task?.id ?? null,
  );
  const imagePreviewUrl = workbenchImagePreviewUrl(imageSource);
  const { data: project } = useProject(task?.project_id ?? "");
  const { data: annotationsData } = useAnnotations(taskId);
  const predictionsInfinite = usePredictions(taskId);
  const predictionsData = useMemo(
    () => (predictionsInfinite.data?.pages ?? []).flatMap((p) => p),
    [predictionsInfinite.data],
  );

  const [mode, setMode] = useState<DiffMode>("diff");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [commentsOpen, setCommentsOpen] = useState(false);
  const { vp, setVp } = useViewportTransform();
  const [fitTick, setFitTick] = useState(0);
  const meUserId = useAuthStore((s) => s.user?.id);
  useSessionStats(taskId, project?.id ?? task?.project_id ?? null, "review", meUserId ?? null);

  // v0.6.5: 进入审核页时调 claim（幂等），冻结标注员的 withdraw 入口；
  // 仅在 status=review 时调用，避免对 completed/rejected 任务多余请求。
  const claimMut = useReviewClaim();
  const [claimInfo, setClaimInfo] = useState<ReviewClaimResponse | null>(null);
  useEffect(() => {
    if (!taskId || task?.status !== "review") return;
    claimMut.mutate(taskId, {
      onSuccess: (data) => setClaimInfo(data),
      onError: () => {},
    });
    // claimMut intentionally omitted to keep effect single-fire per task
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, task?.status]);

  const selectedAnnotation = useMemo(
    () => (annotationsData ?? []).find((a) => a.id === selectedId && a.task_id === taskId) ?? null,
    [annotationsData, selectedId, taskId],
  );

  // v0.11.27 · 遮挡样式 key 并集（跨工具单位），复核界面与工作台保持一致的遮挡视觉。
  const occludedKeys = useMemo(() => {
    const keys = new Set<string>();
    const tb = project?.tool_bindings ?? {};
    for (const binding of Object.values(tb)) {
      for (const k of collectOccludedKeys(binding?.attribute_schema?.fields ?? [])) {
        keys.add(k);
      }
    }
    return keys;
  }, [project]);

  const userBoxes = useMemo(
    () => (annotationsData ?? []).map((a) => annotationToBox(a, occludedKeys)),
    [annotationsData, occludedKeys],
  );
  // 评论卡片绑定 chip 用：annotation_id → class_name。
  const annotationClassById = useMemo(
    () => Object.fromEntries((annotationsData ?? []).map((a) => [a.id, a.class_name])),
    [annotationsData],
  );
  const allAi = useMemo(() => predictionsToBoxes(predictionsData), [predictionsData]);

  // 已被采纳的 prediction id 集合
  const acceptedPredIds = useMemo(() => {
    const s = new Set<string>();
    for (const a of annotationsData ?? []) {
      if (a.parent_prediction_id) s.add(a.parent_prediction_id);
    }
    return s;
  }, [annotationsData]);

  const fadedAiIds = useMemo(() => {
    const s = new Set<string>();
    for (const b of allAi) if (acceptedPredIds.has(b.predictionId)) s.add(b.id);
    return s;
  }, [allAi, acceptedPredIds]);

  const renderUser = mode !== "raw";
  const renderAi = mode !== "final";

  return (
    <div className="flex h-full flex-row overflow-hidden">
      <div className="flex flex-1 flex-col overflow-hidden">
        <ReviewerMiniPanel />
        {claimInfo && !claimInfo.is_self && (
          <div className="flex items-center gap-1.5 border-b border-amber-500/30 bg-status-caution-soft px-3.5 py-1.5 text-xs text-status-caution">
            <Icon name="warning" size={13} />
            已被其他审核员认领（{new Date(claimInfo.reviewer_claimed_at).toLocaleString("zh-CN")}
            ），仍可接力处理
          </div>
        )}
        {task?.skip_reason && (
          <div
            className="flex items-center gap-1.5 border-b border-violet-500/30 bg-status-info-soft px-3.5 py-1.5 text-xs text-status-info"
            data-testid="reviewer-skip-banner"
          >
            <Icon name="warning" size={13} />
            标注员跳过此题：<strong>{skipReasonLabel(task.skip_reason)}</strong>
            <span className="ml-2 text-status-info/80">可通过（无目标即视为完成）或退回重派</span>
          </div>
        )}
        <div className="flex items-center justify-between border-b border-border bg-card px-3.5 py-2">
          <div className="flex items-center gap-1.5">
            <span className="mono text-xs font-semibold">{task?.display_id ?? "—"}</span>
            <span className="text-xs text-muted-foreground">{task?.file_name}</span>
            {task?.skip_reason && (
              <span
                className="ml-1 rounded-sm border border-violet-500/30 bg-status-info-soft px-1.5 py-px text-2xs font-semibold uppercase tracking-[0.4px] text-status-info"
                data-testid="reviewer-skip-badge"
              >
                SKIP
              </span>
            )}
          </div>
          <div className="flex gap-1">
            {(["final", "raw", "diff"] as const).map((m) => (
              <Button
                key={m}
                variant={mode === m ? "primary" : "ghost"}
                size="sm"
                onClick={() => setMode(m)}
              >
                {m === "final" ? "仅最终" : m === "raw" ? "仅 AI 原始" : "叠加 diff"}
              </Button>
            ))}
          </div>
          <div className="flex gap-1.5">
            <Button size="sm" onClick={() => setFitTick((n) => n + 1)} className="text-xs">
              适应
            </Button>
            <Button
              size="sm"
              variant={commentsOpen ? "primary" : "ghost"}
              onClick={() => setCommentsOpen((v) => !v)}
              disabled={!selectedAnnotation}
              title={selectedAnnotation ? "查看 / 留下批注（含画布批注）" : "先选中一个标注"}
            >
              <Icon name="bell" size={12} />
              评论
            </Button>
            {onPrev && (
              <Button size="sm" onClick={onPrev}>
                <Icon name="chevLeft" size={12} />
                上一
              </Button>
            )}
            {onNext && (
              <Button size="sm" onClick={onNext}>
                下一
                <Icon name="chevRight" size={12} />
              </Button>
            )}
            <Button variant="primary" size="sm" onClick={onApprove} data-testid="review-approve">
              <Icon name="check" size={12} />
              通过
            </Button>
            <Button variant="danger" size="sm" onClick={onReject} data-testid="review-reject">
              <Icon name="x" size={12} />
              退回
            </Button>
          </div>
        </div>

        <ImageStage
          fileUrl={task?.file_url ?? null}
          imageSource={imageSource}
          onRetryImagePyramid={retryImagePyramid}
          mediaKey={task?.dataset_item_id ?? task?.id ?? null}
          blurhash={task?.blurhash}
          imageWidth={task?.image_width}
          imageHeight={task?.image_height}
          tool="hand"
          activeClass=""
          selectedId={selectedId}
          userBoxes={renderUser ? userBoxes : []}
          aiBoxes={renderAi ? allAi : []}
          spacePan={false}
          vp={vp}
          setVp={setVp}
          fitTick={fitTick}
          readOnly
          fadedAiIds={mode === "diff" ? fadedAiIds : undefined}
          onSelectBox={setSelectedId}
          onCursorMove={() => {}}
        />

        <div className="flex justify-between border-t border-border bg-card px-3.5 py-1.5 text-xs text-muted-foreground">
          <div className="flex gap-4">
            <span>
              <span className="mono">{userBoxes.length}</span> 标注
            </span>
            <span>
              <Icon name="sparkles" size={11} className="align-[-2px] text-status-info" />{" "}
              <span className="mono">{allAi.length}</span> AI 预测（{acceptedPredIds.size} 已采纳）
            </span>
          </div>
          <div className="mono">
            {task?.image_width && task?.image_height
              ? `${task.image_width}×${task.image_height}`
              : "—"}
          </div>
        </div>
      </div>
      {commentsOpen && selectedAnnotation && (
        <aside className="w-80 overflow-y-auto border-l border-border bg-card">
          <CommentsPanel
            annotationId={selectedAnnotation.id}
            annotationTaskId={selectedAnnotation.task_id}
            projectId={selectedAnnotation.project_id}
            currentUserId={meUserId}
            backgroundUrl={imagePreviewUrl}
            imageWidth={task?.image_width}
            imageHeight={task?.image_height}
            enableCanvasDrawing
            annotationClassById={annotationClassById}
            onSelectAnnotation={setSelectedId}
          />
        </aside>
      )}
    </div>
  );
}
