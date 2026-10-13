import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, Crosshair, MessageSquareText, RefreshCw, X } from "lucide-react";

import type {
  PointCloudQualityConfig,
  PointCloudQualityIssue,
  PointCloudQualityReviewVerdict,
  PointCloudQualitySeverity,
} from "@/api/pointCloudQuality";
import { Button } from "@/components/ui/Button";
import { useCreateFeedback } from "@/hooks/useFeedbacks";
import {
  usePatchPointCloudQualityIssue,
  usePointCloudQualityIssues,
  usePointCloudQualityRun,
  useRunPointCloudQuality,
} from "@/hooks/usePointCloudQuality";
import { cn } from "@/lib/utils";

import { PointCloudQualityGovernance } from "./PointCloudQualityGovernance";

const CODE_LABEL: Record<string, string> = {
  low_point_count: "框内点数过少",
  size_outlier: "尺寸异常",
  ground_clearance: "穿地或悬浮",
  temporal_jump: "时序跳变",
  track_gap: "轨迹存在区间缺帧",
  track_identity_drift: "轨迹身份漂移",
  duplicate_track_member: "同帧重复轨迹成员",
  projection_residual: "投影残差",
};

const SEVERITY_LABEL = { blocker: "阻断", warning: "警告", info: "提示" } as const;
const VERDICT_LABEL: Record<PointCloudQualityReviewVerdict, string> = {
  confirmed: "确认问题",
  false_positive: "误报",
  accepted_exception: "接受例外",
  uncertain: "不确定",
};

function metricSummary(issue: PointCloudQualityIssue): string {
  const metric = issue.metric;
  if (typeof metric.point_count === "number") return `${metric.point_count} 点`;
  if (typeof metric.missing_frames === "number") return `缺 ${metric.missing_frames} 帧`;
  if (typeof metric.clearance_m === "number") return `离地 ${metric.clearance_m.toFixed(2)} m`;
  if (typeof metric.center_delta_m_per_frame === "number") {
    return `中心跳变 ${metric.center_delta_m_per_frame.toFixed(2)} m/帧`;
  }
  if (Array.isArray(metric.dimensions)) return `尺寸 ${metric.dimensions.join(" × ")}`;
  if (typeof metric.member_count === "number") return `${metric.member_count} 个成员`;
  return "查看证据";
}

interface PointCloudQualityPanelProps {
  projectId: string;
  sceneId: string;
  taskId: string;
  canScanScene: boolean;
  canGovern?: boolean;
  qualityConfig?: PointCloudQualityConfig | null;
  classes?: string[];
  onClose: () => void;
  onLocate: (issue: PointCloudQualityIssue) => void;
}

export function PointCloudQualityPanel({
  projectId,
  sceneId,
  taskId,
  canScanScene,
  canGovern = false,
  qualityConfig,
  classes = [],
  onClose,
  onLocate,
}: PointCloudQualityPanelProps) {
  const [tab, setTab] = useState<"issues" | "governance">("issues");
  const [filter, setFilter] = useState<"open" | "resolved" | "wont_fix" | "stale" | "all">("open");
  const [severity, setSeverity] = useState<PointCloudQualitySeverity | "all">("all");
  const [code, setCode] = useState("all");
  const [runId, setRunId] = useState<string | null>(null);
  const [dispositionId, setDispositionId] = useState<string | null>(null);
  const [verdict, setVerdict] =
    useState<Exclude<PointCloudQualityReviewVerdict, "confirmed">>("false_positive");
  const [reason, setReason] = useState("");
  const [commentIssueId, setCommentIssueId] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const ownerKey = `${projectId}\u001f${sceneId}\u001f${taskId}\u001f${canScanScene ? "scene" : "task"}`;
  const ownerKeyRef = useRef(ownerKey);
  const runIdOwnerRef = useRef(ownerKey);
  const ownerChanged = runIdOwnerRef.current !== ownerKey;
  ownerKeyRef.current = ownerKey;
  const runQueryId = ownerChanged ? null : runId;
  const issuesQuery = usePointCloudQualityIssues({
    projectId,
    ...(sceneId ? { sceneId } : { taskId }),
    ...(filter === "all" ? {} : { status: filter }),
    ...(severity === "all" ? {} : { severity }),
    ...(code === "all" ? {} : { code }),
  });
  const runMutation = useRunPointCloudQuality(
    projectId,
    canScanScene
      ? { scope: "scene_ids", scene_ids: [sceneId] }
      : { scope: "task_ids", task_ids: [taskId] },
  );
  const runQuery = usePointCloudQualityRun(projectId, runQueryId);
  const patchIssue = usePatchPointCloudQualityIssue(projectId);
  const createFeedback = useCreateFeedback({ project_id: projectId });
  const issues = useMemo(() => issuesQuery.data?.items ?? [], [issuesQuery.data?.items]);
  const counts = useMemo(
    () =>
      issues.reduce((out, issue) => ({ ...out, [issue.severity]: out[issue.severity] + 1 }), {
        blocker: 0,
        warning: 0,
        info: 0,
      }),
    [issues],
  );
  const activeRun = runQuery.data;
  const issueScopeLabel = sceneId ? "当前 Scene" : "当前任务";
  const scanning =
    runMutation.isPending || activeRun?.status === "pending" || activeRun?.status === "running";

  useEffect(() => {
    if (runIdOwnerRef.current === ownerKey) return;
    runIdOwnerRef.current = ownerKey;
    setRunId(null);
    setDispositionId(null);
    setReason("");
    setCommentIssueId(null);
    setComment("");
  }, [ownerKey]);

  const startScan = () => {
    runMutation.mutate(undefined, {
      onSuccess: (run) => {
        if (ownerKeyRef.current !== ownerKey) return;
        runIdOwnerRef.current = ownerKey;
        setRunId(run.id);
        if (run.status === "completed") void issuesQuery.refetch();
      },
    });
  };

  const submitFeedback = (issue: PointCloudQualityIssue) => {
    if (!issue.task_id || !comment.trim()) return;
    createFeedback.mutate(
      {
        kind: "comment",
        anchor_type: "point_cloud",
        project_id: projectId,
        task_id: issue.task_id,
        annotation_id: issue.annotation_id,
        anchor_position: {
          frame: issue.locator.frame_index,
          point_cloud_quality_issue_id: issue.id,
          scene_id: issue.scene_id,
          scene_track_id: issue.scene_track_id,
          auxiliary_layers: issue.locator.auxiliary_layers,
        },
        body: comment.trim(),
      },
      {
        onSuccess: () => {
          setComment("");
          setCommentIssueId(null);
        },
      },
    );
  };

  return (
    <aside
      className="absolute inset-y-0 right-0 z-local-6 flex w-[360px] max-w-[calc(100%-24px)] flex-col border-l border-border bg-card shadow-lg"
      aria-label="3D 质量问题"
      data-testid="point-cloud-quality-panel"
    >
      <header className="flex items-start gap-3 border-b border-border p-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 font-semibold text-foreground">
            <AlertTriangle className="size-4 text-status-caution" />
            3D 质量检查
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            只生成可定位的问题，不会自动修改框或轨迹。
          </p>
        </div>
        <Button size="xs" variant="ghost" aria-label="关闭 3D 质量面板" onClick={onClose}>
          <X className="size-3" />
        </Button>
      </header>

      <div className="grid grid-cols-2 border-b border-border p-2">
        <Button
          size="sm"
          variant={tab === "issues" ? "default" : "ghost"}
          onClick={() => setTab("issues")}
        >
          问题
        </Button>
        <Button
          size="sm"
          variant={tab === "governance" ? "default" : "ghost"}
          onClick={() => setTab("governance")}
        >
          规则治理
        </Button>
      </div>

      {tab === "issues" && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <Button size="sm" variant="primary" disabled={scanning} onClick={startScan}>
            <RefreshCw className={cn("size-3.5", scanning && "animate-spin")} />
            {scanning
              ? `扫描中 ${activeRun?.progress_pct ?? 0}%`
              : canScanScene
                ? "扫描当前 Scene"
                : "扫描当前任务"}
          </Button>
          <FilterGroup label="问题筛选" hideLabel compact className="ml-auto">
            <FilterSelect
              aria-label="质量问题状态"
              value={filter}
              onChange={(event) => setFilter(event.target.value as typeof filter)}
            >
              <option value="open">待处理</option>
              <option value="resolved">已解决</option>
              <option value="wont_fix">搁置</option>
              <option value="stale">已过期</option>
              <option value="all">全部</option>
            </FilterSelect>
            <FilterSelect
              aria-label="质量问题严重级别"
              value={severity}
              onChange={(event) =>
                setSeverity(event.target.value as PointCloudQualitySeverity | "all")
              }
            >
              <option value="all">全部严重级别</option>
              <option value="blocker">阻断</option>
              <option value="warning">警告</option>
              <option value="info">提示</option>
            </FilterSelect>
            <FilterSelect
              aria-label="质量问题规则"
              className="max-w-40"
              value={code}
              onChange={(event) => setCode(event.target.value)}
            >
              <option value="all">全部规则</option>
              {Object.entries(CODE_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </FilterSelect>
          </FilterGroup>
        </div>
      )}

      {tab === "issues" && (
        <div className="flex gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
          <span className="text-status-danger">已加载阻断 {counts.blocker}</span>
          <span className="text-status-caution">已加载警告 {counts.warning}</span>
          <span>已加载提示 {counts.info}</span>
          <span className="ml-auto">
            {issueScopeLabel} · 共 {issuesQuery.data?.total ?? 0}
          </span>
        </div>
      )}

      {tab === "governance" ? (
        <PointCloudQualityGovernance
          projectId={projectId}
          config={qualityConfig}
          classes={classes}
          canGovern={canGovern}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {issuesQuery.isLoading && <p className="p-3 text-sm text-muted-foreground">正在加载…</p>}
          {issuesQuery.isError && (
            <div className="p-3 text-sm text-status-danger">
              质量问题加载失败
              <Button size="xs" variant="ghost" onClick={() => void issuesQuery.refetch()}>
                重试
              </Button>
            </div>
          )}
          {!issuesQuery.isLoading && !issuesQuery.isError && issues.length === 0 && (
            <div className="p-6 text-center text-sm text-muted-foreground">
              当前筛选下没有质量问题
            </div>
          )}
          <div className="space-y-2">
            {issues.map((issue) => (
              <article
                key={issue.id}
                className="rounded-md border border-border bg-background p-2.5 text-xs"
                data-testid={`point-cloud-quality-issue-${issue.code}`}
              >
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      "mt-0.5 rounded-sm px-1.5 py-0.5 font-medium",
                      issue.severity === "blocker" && "bg-status-danger-soft text-status-danger",
                      issue.severity === "warning" && "bg-status-caution-soft text-status-caution",
                      issue.severity === "info" && "bg-muted text-muted-foreground",
                    )}
                  >
                    {SEVERITY_LABEL[issue.severity]}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-foreground">
                      {CODE_LABEL[issue.code] ?? issue.code}
                    </div>
                    <div className="mt-0.5 text-muted-foreground">
                      F{issue.frame_start ?? "—"}
                      {issue.frame_end != null && issue.frame_end !== issue.frame_start
                        ? `–F${issue.frame_end}`
                        : ""}
                      <span className="mx-1">·</span>
                      {metricSummary(issue)}
                    </div>
                    {issue.review_verdict && (
                      <div className="mt-1 text-brand">
                        人工判定：{VERDICT_LABEL[issue.review_verdict]}
                      </div>
                    )}
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Button size="xs" variant="default" onClick={() => onLocate(issue)}>
                    <Crosshair className="size-3" />
                    定位
                  </Button>
                  {issue.status === "open" && (
                    <>
                      <Button
                        size="xs"
                        variant="ghost"
                        disabled={patchIssue.isPending}
                        onClick={() =>
                          patchIssue.mutate({
                            issueId: issue.id,
                            status: "resolved",
                            reviewVerdict: "confirmed",
                          })
                        }
                      >
                        <Check className="size-3" />
                        确认问题
                      </Button>
                      <Button
                        size="xs"
                        variant="ghost"
                        onClick={() => {
                          setDispositionId(issue.id);
                          setVerdict("false_positive");
                          setReason("");
                        }}
                      >
                        其他判定
                      </Button>
                    </>
                  )}
                  {issue.status === "resolved" || issue.status === "wont_fix" ? (
                    <Button
                      size="xs"
                      variant="ghost"
                      onClick={() => patchIssue.mutate({ issueId: issue.id, status: "open" })}
                    >
                      重新打开
                    </Button>
                  ) : null}
                  {issue.task_id && (
                    <Button size="xs" variant="ghost" onClick={() => setCommentIssueId(issue.id)}>
                      <MessageSquareText className="size-3" />
                      讨论
                    </Button>
                  )}
                </div>
                {dispositionId === issue.id && (
                  <div className="mt-2 space-y-1.5">
                    <select
                      aria-label="人工判定"
                      value={verdict}
                      onChange={(event) =>
                        setVerdict(
                          event.target.value as Exclude<
                            PointCloudQualityReviewVerdict,
                            "confirmed"
                          >,
                        )
                      }
                      className="h-8 w-full rounded-sm border border-border bg-card px-2 text-foreground"
                    >
                      <option value="false_positive">误报</option>
                      <option value="accepted_exception">接受例外</option>
                      <option value="uncertain">不确定</option>
                    </select>
                    <div className="flex gap-1.5">
                      <input
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                        placeholder="填写判定依据"
                        className="min-w-0 flex-1 rounded-sm border border-border bg-card px-2 py-1 text-foreground"
                      />
                      <Button
                        size="xs"
                        variant="primary"
                        disabled={!reason.trim() || patchIssue.isPending}
                        onClick={() =>
                          patchIssue.mutate(
                            {
                              issueId: issue.id,
                              status: "wont_fix",
                              reason: reason.trim(),
                              reviewVerdict: verdict,
                              reviewNote: reason.trim(),
                            },
                            { onSuccess: () => setDispositionId(null) },
                          )
                        }
                      >
                        确认
                      </Button>
                    </div>
                  </div>
                )}
                {commentIssueId === issue.id && (
                  <div className="mt-2 flex gap-1.5">
                    <input
                      value={comment}
                      onChange={(event) => setComment(event.target.value)}
                      placeholder="记录判断或 @ 协作者"
                      className="min-w-0 flex-1 rounded-sm border border-border bg-card px-2 py-1 text-foreground"
                    />
                    <Button
                      size="xs"
                      variant="primary"
                      disabled={!comment.trim() || createFeedback.isPending}
                      onClick={() => submitFeedback(issue)}
                    >
                      发送
                    </Button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
