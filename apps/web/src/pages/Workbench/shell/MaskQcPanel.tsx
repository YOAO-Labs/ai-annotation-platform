import { FilterGroup, FilterSelect, FilterToggle } from "@/components/filters/FilterControls";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { Icon } from "@/components/ui/Icon";
import type {
  MaskCompareBaseline,
  MaskCompareResult,
  MaskQcIssue,
  MaskQcIssueStatus,
  MaskQcSeverity,
  MaskRepairAction,
  MaskRepairKind,
} from "@/api/maskQc";
import { Checkbox } from "@/components/shadcn/ui/checkbox";
import { Field, FieldLegend, FieldSet } from "@/components/shadcn/ui/field";
import { useCreateFeedback, useInfiniteFeedbacks } from "@/hooks/useFeedbacks";
import type { MaskFeedbackCompareLocator } from "@/api/feedbacks";
import {
  useMaskQcIssues,
  usePatchMaskQcIssue,
  useRunTaskMaskQc,
  useTaskMaskQcSummary,
} from "@/hooks/useMaskQc";
import type { RasterMaskCompareMode } from "../stage/shared/rasterMaskWorkerProtocol";
import type { MaskQcNavigationPhase, MaskQcTrackerCandidate } from "../state/useMaskQcReview";
import { MaskRepairSheet } from "./MaskRepairSheet";

const MODE_LABELS: Array<{ mode: RasterMaskCompareMode; label: string }> = [
  { mode: "overlay", label: "叠加" },
  { mode: "boundary", label: "边界" },
  { mode: "xor", label: "差异" },
  { mode: "added", label: "新增" },
  { mode: "removed", label: "移除" },
];

const PHASE_LABELS: Record<MaskQcNavigationPhase, string> = {
  idle: "",
  switching_task: "正在切换任务…",
  waiting_task: "正在等待任务数据…",
  waiting_annotations: "正在等待标注…",
  waiting_manifest: "正在等待视频清单…",
  seeking_frame: "正在定位视频帧…",
  selecting_annotation: "正在选择标注…",
  focusing_region: "正在聚焦问题区域…",
  loading_compare: "正在载入对比内容…",
  ready: "对比已就绪",
  error: "定位失败",
};

const RULE_LABELS: Record<string, string> = {
  empty_mask: "空 Mask",
  near_empty_mask: "近空 Mask",
  touches_border: "接触边界",
  small_island: "小孤岛",
  small_hole: "小孔洞",
  narrow_bridge: "狭窄连接",
  boundary_noise: "边界噪声",
  derived_geometry_mismatch: "派生几何不一致",
  same_class_overlap: "同类重叠",
  cross_class_overlap: "跨类重叠",
  flicker: "时序闪烁",
  drift: "时序漂移",
};

const DETERMINISTIC_REPAIR_BY_RULE: Partial<Record<string, MaskRepairKind>> = {
  small_island: "delete_small_islands",
  small_hole: "fill_small_holes",
  same_class_overlap: "resolve_same_class_overlap",
};

function cx(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

async function sha256Text(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export interface MaskQcPanelProps {
  projectId: string;
  taskId: string;
  activeIssue: MaskQcIssue | null;
  phase: MaskQcNavigationPhase;
  error: string | null;
  compare: MaskCompareResult | null;
  baseline: MaskCompareBaseline;
  aiCandidateAvailable: boolean;
  trackerCandidates: MaskQcTrackerCandidate[];
  trackerCandidateKey: string | null;
  mode: RasterMaskCompareMode;
  onNavigateIssue: (issue: MaskQcIssue) => void;
  onReplayFeedback: (issue: MaskQcIssue, locator: MaskFeedbackCompareLocator) => void;
  onRetryNavigation: () => void;
  onClearIssue: () => void;
  onSetMode: (mode: RasterMaskCompareMode) => void;
  onSetBaseline: (baseline: MaskCompareBaseline) => void;
  onSetTrackerCandidate: (candidate: MaskQcTrackerCandidate) => void;
  onDecideTrackerRegion: (
    issue: MaskQcIssue,
    candidate: MaskQcTrackerCandidate,
    decision: "accept" | "reject",
  ) => Promise<{ ok: boolean; reason?: string }>;
  onUpdateIssue: (issue: MaskQcIssue) => void;
}

export function MaskQcPanel({
  projectId,
  taskId,
  activeIssue,
  phase,
  error,
  compare,
  baseline,
  aiCandidateAvailable,
  trackerCandidates,
  trackerCandidateKey,
  mode,
  onNavigateIssue,
  onReplayFeedback,
  onRetryNavigation,
  onClearIssue,
  onSetMode,
  onSetBaseline,
  onSetTrackerCandidate,
  onDecideTrackerRegion,
  onUpdateIssue,
}: MaskQcPanelProps) {
  const [status, setStatus] = useState<MaskQcIssueStatus | "all">("all");
  const [severity, setSeverity] = useState<MaskQcSeverity | "all">("all");
  const [code, setCode] = useState("all");
  const [scope, setScope] = useState<"task" | "project">("task");
  const [comment, setComment] = useState("");
  const [regionDecision, setRegionDecision] = useState<"accept" | "reject" | null>(null);
  const [regionDecisionError, setRegionDecisionError] = useState<string | null>(null);
  const [selectedRepairIssueIds, setSelectedRepairIssueIds] = useState<Set<string>>(new Set());
  const [repairOpen, setRepairOpen] = useState(false);
  const query = useMaskQcIssues({
    projectId,
    taskId: scope === "task" ? taskId : undefined,
    status: status === "all" ? undefined : status,
    severity: severity === "all" ? undefined : severity,
    code: code === "all" ? undefined : code,
  });
  const summary = useTaskMaskQcSummary(taskId);
  const run = useRunTaskMaskQc(projectId, taskId);
  const patchIssue = usePatchMaskQcIssue(projectId, taskId);
  const feedbackParams = useMemo(
    () => ({
      project_id: projectId,
      task_id: activeIssue?.task_id ?? taskId,
      annotation_id: activeIssue?.annotation_id,
      kind: "comment" as const,
      anchor_type: "pixel" as const,
      limit: 100,
    }),
    [activeIssue?.annotation_id, activeIssue?.task_id, projectId, taskId],
  );
  const feedbackQuery = useInfiniteFeedbacks(feedbackParams, !!activeIssue);
  const createFeedback = useCreateFeedback(feedbackParams);
  const lastCompletedRunRef = useRef<string | null>(null);
  const refetchIssues = query.refetch;
  const refetchSummary = summary.refetch;
  const issues = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data?.pages],
  );
  const repairActions = useMemo<MaskRepairAction[]>(
    () =>
      issues
        .filter((issue) => selectedRepairIssueIds.has(issue.id))
        .map((issue) => ({
          issue_id: issue.id,
          kind: DETERMINISTIC_REPAIR_BY_RULE[issue.code]!,
        })),
    [issues, selectedRepairIssueIds],
  );
  const issueComments =
    feedbackQuery.data?.pages
      .flatMap((page) => page.items)
      .filter((feedback) => {
        const anchor = feedback.anchor_position;
        if (!activeIssue || !anchor) return false;
        if (anchor.mask_qc_issue_id && anchor.mask_qc_issue_id !== activeIssue.id) return false;
        if ((anchor.frame ?? null) !== (activeIssue.frame_start ?? null)) return false;
        if (activeIssue.region_digest) return anchor.region_digest === activeIssue.region_digest;
        const bbox = activeIssue.region_bbox;
        const anchorBbox = anchor.region_bbox;
        return bbox && anchorBbox
          ? anchorBbox[0] === bbox.x0 &&
              anchorBbox[1] === bbox.y0 &&
              anchorBbox[2] === bbox.x1 &&
              anchorBbox[3] === bbox.y1
          : bbox == null && anchorBbox == null;
      }) ?? [];
  const issueScopeLabel = scope === "task" ? "当前任务" : "整个项目";
  const busy = phase !== "idle" && phase !== "ready" && phase !== "error";
  const stale = activeIssue?.effective_status === "stale";
  const iou =
    compare && compare.metrics.iou_denominator > 0
      ? compare.metrics.iou_numerator / compare.metrics.iou_denominator
      : compare
        ? 1
        : null;
  const dice =
    compare && compare.metrics.dice_denominator > 0
      ? compare.metrics.dice_numerator / compare.metrics.dice_denominator
      : compare
        ? 1
        : null;
  const selectedTrackerCandidate =
    trackerCandidates.find((candidate) => candidate.key === trackerCandidateKey) ??
    trackerCandidates[0] ??
    null;
  const canDecideTrackerRegion = Boolean(
    !stale &&
    activeIssue?.effective_status === "open" &&
    activeIssue.region_digest &&
    activeIssue.frame_start != null &&
    activeIssue.frame_start === activeIssue.frame_end &&
    baseline === "tracker_candidate" &&
    compare?.baseline.candidate_job_id &&
    compare.baseline.candidate_digest &&
    selectedTrackerCandidate &&
    compare.baseline.candidate_job_id === selectedTrackerCandidate.jobId &&
    compare.baseline.candidate_digest === selectedTrackerCandidate.digest,
  );

  useEffect(() => {
    const completedRunId = summary.data?.status === "completed" ? summary.data.run_id : null;
    if (!completedRunId || lastCompletedRunRef.current === completedRunId) return;
    lastCompletedRunRef.current = completedRunId;
    void refetchIssues();
  }, [refetchIssues, summary.data?.run_id, summary.data?.status]);

  useEffect(() => {
    setComment("");
    setRegionDecision(null);
    setRegionDecisionError(null);
  }, [activeIssue?.id]);

  useEffect(() => {
    if (!activeIssue) return;
    const latest = issues.find((issue) => issue.id === activeIssue.id);
    if (!latest || latest === activeIssue) return;
    if (
      latest.updated_at !== activeIssue.updated_at ||
      latest.effective_status !== activeIssue.effective_status ||
      latest.annotation_version !== activeIssue.annotation_version
    )
      onUpdateIssue(latest);
  }, [activeIssue, issues, onUpdateIssue]);

  useEffect(() => {
    const eligibleIds = new Set(
      issues
        .filter(
          (issue) => issue.effective_status === "open" && DETERMINISTIC_REPAIR_BY_RULE[issue.code],
        )
        .map((issue) => issue.id),
    );
    setSelectedRepairIssueIds((current) => {
      const next = new Set([...current].filter((issueId) => eligibleIds.has(issueId)));
      return next.size === current.size && [...next].every((issueId) => current.has(issueId))
        ? current
        : next;
    });
  }, [issues]);

  useEffect(() => {
    if (selectedRepairIssueIds.size === 0) setRepairOpen(false);
  }, [selectedRepairIssueIds.size]);

  useEffect(() => {
    setSelectedRepairIssueIds(new Set());
    setRepairOpen(false);
  }, [projectId, taskId, scope]);

  const finishRepair = useCallback(() => {
    setSelectedRepairIssueIds(new Set());
    void refetchIssues();
    void refetchSummary();
  }, [refetchIssues, refetchSummary]);

  const submitComment = async () => {
    if (!activeIssue || stale || !comment.trim()) return;
    const bbox = activeIssue.region_bbox;
    const boundaryDigest =
      mode === "boundary" && compare && activeIssue.region_digest
        ? await sha256Text(
            JSON.stringify({
              current: compare.current.digest,
              baseline: compare.baseline.digest,
              bbox,
              frame: activeIssue.frame_start,
            }),
          )
        : null;
    createFeedback.mutate(
      {
        kind: "comment",
        anchor_type: "pixel",
        project_id: projectId,
        task_id: activeIssue.task_id,
        annotation_id: activeIssue.annotation_id,
        anchor_position: {
          x: bbox ? (bbox.x0 + bbox.x1) / 2 : 0.5,
          y: bbox ? (bbox.y0 + bbox.y1) / 2 : 0.5,
          frame: activeIssue.frame_start,
          region_bbox: bbox ? [bbox.x0, bbox.y0, bbox.x1, bbox.y1] : null,
          region_digest: activeIssue.region_digest,
          boundary_digest: boundaryDigest,
          mask_qc_issue_id: activeIssue.id,
          compare_locator: compare
            ? {
                baseline_kind: compare.baseline_kind,
                mode,
                current_digest: compare.current.digest,
                baseline_digest: compare.baseline.digest,
                candidate_job_id: compare.baseline.candidate_job_id,
                candidate_job_revision:
                  baseline === "tracker_candidate"
                    ? (trackerCandidates.find((candidate) => candidate.key === trackerCandidateKey)
                        ?.jobRevision ?? null)
                    : null,
                candidate_digest: compare.baseline.candidate_digest,
                candidate_instance_id: compare.baseline.candidate_instance_id,
              }
            : null,
        },
        body: comment.trim(),
      },
      { onSuccess: () => setComment("") },
    );
  };

  const decideTrackerRegion = async (decision: "accept" | "reject") => {
    if (!activeIssue || !selectedTrackerCandidate || !canDecideTrackerRegion) return;
    const verb = decision === "accept" ? "接受" : "拒绝";
    const confirmed = await confirmDialog({
      tone: decision === "accept" ? "default" : "danger",
      title: `确认${verb}该区域候选？`,
      description: `${verb}只会作用于当前质检问题区域，区域外候选保持待审。`,
      confirmLabel: verb,
    });
    if (!confirmed) return;
    // 等待决定期间问题/候选可能已被切换或决定;重验后才提交。
    if (!activeIssue || !selectedTrackerCandidate || !canDecideTrackerRegion) return;
    setRegionDecision(decision);
    setRegionDecisionError(null);
    try {
      const outcome = await onDecideTrackerRegion(activeIssue, selectedTrackerCandidate, decision);
      if (!outcome.ok) {
        setRegionDecisionError(outcome.reason ?? "区域决定失败，请刷新候选后重试");
        return;
      }
      await Promise.all([query.refetch(), summary.refetch()]);
    } finally {
      setRegionDecision(null);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 text-xs text-muted-foreground">
          {summary.isLoading ? (
            "正在读取质检状态…"
          ) : (
            <>
              <span className="font-semibold text-foreground">{issueScopeLabel}</span> · 已加载{" "}
              <span className="font-semibold text-foreground">{issues.length}</span> 个问题 ·{" "}
              {summary.data?.status ?? "未知"}
            </>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          title={
            summary.data?.status === "not_applicable" ? "当前任务没有可质检的 Mask" : undefined
          }
          disabled={
            run.isPending ||
            summary.data?.status === "running" ||
            summary.data?.status === "pending" ||
            summary.data?.status === "not_applicable"
          }
          onClick={() => run.mutate()}
        >
          <Icon name="refresh" size={12} /> {run.isPending ? "提交中" : "重新扫描"}
        </Button>
      </div>

      {(summary.data?.status === "running" || summary.data?.status === "pending") && (
        <div className="rounded-md border border-border bg-muted px-2 py-1.5 text-xs text-muted-foreground">
          质检任务运行中：{summary.data.progress_pct}%，结果会自动刷新。
        </div>
      )}

      <FilterGroup label="范围" compact className="gap-1" aria-label="问题范围">
        {(["task", "project"] as const).map((value) => (
          <FilterToggle
            compact
            active={scope === value}
            key={value}
            type="button"
            onClick={() => setScope(value)}
          >
            {value === "task" ? "当前任务" : "整个项目"}
          </FilterToggle>
        ))}
        {scope === "project" && (
          <span className="ml-auto self-center text-2xs text-muted-foreground">
            已加载 {issues.length} 条
          </span>
        )}
      </FilterGroup>

      <FilterGroup label="状态" compact className="gap-1">
        {(["all", "open", "resolved", "wont_fix", "stale"] as const).map((value) => (
          <FilterToggle
            compact
            active={status === value}
            key={value}
            type="button"
            onClick={() => setStatus(value)}
          >
            {value === "all"
              ? "全部"
              : value === "open"
                ? "未解决"
                : value === "resolved"
                  ? "已解决"
                  : value === "wont_fix"
                    ? "搁置"
                    : "已过期"}
          </FilterToggle>
        ))}
      </FilterGroup>

      <div className="grid grid-cols-2 gap-1">
        <FilterSelect
          compact
          aria-label="严重级别"
          value={severity}
          onChange={(event) => setSeverity(event.target.value as MaskQcSeverity | "all")}
        >
          <option value="all">全部严重级别</option>
          <option value="blocker">阻断</option>
          <option value="warning">警告</option>
          <option value="info">提示</option>
        </FilterSelect>
        <FilterSelect
          compact
          aria-label="问题规则"
          value={code}
          onChange={(event) => setCode(event.target.value)}
        >
          <option value="all">全部规则</option>
          {Object.entries(RULE_LABELS).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </FilterSelect>
      </div>

      {query.isLoading && <div className="py-2 text-xs text-muted-foreground">加载问题列表…</div>}
      {query.isError && <div className="py-2 text-xs text-status-danger">问题列表加载失败</div>}
      {!query.isLoading && !query.isError && issues.length === 0 && (
        <div className="rounded-md border border-dashed border-border p-3 text-xs text-muted-foreground">
          当前筛选下没有 Mask 质检问题。
        </div>
      )}

      {issues.some(
        (issue) => issue.effective_status === "open" && DETERMINISTIC_REPAIR_BY_RULE[issue.code],
      ) && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-card px-2 py-1.5">
          <span className="text-xs text-muted-foreground">
            已选 {selectedRepairIssueIds.size} 个可确定修复的问题
          </span>
          <Button
            variant="primary"
            size="sm"
            disabled={selectedRepairIssueIds.size === 0}
            onClick={() => setRepairOpen(true)}
          >
            预览批量修复
          </Button>
        </div>
      )}

      <FieldSet className="gap-2">
        <FieldLegend className="sr-only">Mask 质检问题批量选择</FieldLegend>
        {issues.map((issue) => {
          const repairKind = DETERMINISTIC_REPAIR_BY_RULE[issue.code];
          const repairEligible = issue.effective_status === "open" && repairKind !== undefined;
          return (
            <Field
              key={issue.id}
              orientation="horizontal"
              data-disabled={!repairEligible}
              className={cx(
                "items-start gap-2 rounded-md border border-border bg-muted p-2",
                activeIssue?.id === issue.id && "border-brand",
              )}
            >
              <Checkbox
                aria-label={`选择修复 ${RULE_LABELS[issue.code] ?? issue.code}`}
                checked={selectedRepairIssueIds.has(issue.id)}
                disabled={!repairEligible}
                onCheckedChange={(checked) => {
                  setSelectedRepairIssueIds((current) => {
                    const next = new Set(current);
                    if (checked === true) next.add(issue.id);
                    else next.delete(issue.id);
                    return next;
                  });
                }}
              />
              <button
                type="button"
                onClick={() => onNavigateIssue(issue)}
                className="flex min-w-0 flex-1 cursor-pointer flex-col gap-1 border-0 bg-transparent p-0 text-left"
              >
                <span className="flex items-center gap-1.5 text-xs">
                  <span
                    className={cx(
                      "rounded px-1.5 py-px text-2xs",
                      issue.severity === "blocker"
                        ? "bg-status-danger-soft text-status-danger"
                        : issue.severity === "warning"
                          ? "bg-status-caution-soft text-status-caution"
                          : "bg-status-info-soft text-status-info-alt",
                    )}
                  >
                    {issue.severity === "blocker"
                      ? "阻断"
                      : issue.severity === "warning"
                        ? "警告"
                        : "提示"}
                  </span>
                  <span className="font-semibold text-foreground">
                    {RULE_LABELS[issue.code] ?? issue.code}
                  </span>
                  {issue.frame_start != null && (
                    <span className="ml-auto text-2xs text-muted-foreground">
                      F{issue.frame_start}
                    </span>
                  )}
                </span>
                {issue.suggestion && (
                  <span className="text-2xs text-muted-foreground">{issue.suggestion}</span>
                )}
                <span className="text-2xs text-muted-foreground">
                  状态：{issue.effective_status}
                </span>
              </button>
            </Field>
          );
        })}
      </FieldSet>
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          disabled={query.isFetchingNextPage}
          onClick={() => {
            void query.fetchNextPage();
          }}
        >
          {query.isFetchingNextPage ? "加载中…" : "加载更多问题"}
        </Button>
      )}

      {activeIssue && (
        <div className="mt-1 flex flex-col gap-2 rounded-md border border-border bg-card p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-semibold text-foreground">问题对比</span>
            <button
              type="button"
              onClick={onClearIssue}
              className="cursor-pointer border-0 bg-transparent text-muted-foreground"
              aria-label="关闭对比"
            >
              <Icon name="x" size={13} />
            </button>
          </div>
          {stale && (
            <div className="rounded border border-border bg-muted px-2 py-1 text-2xs text-muted-foreground">
              此问题引用旧版本，仅可查看，不能修改状态。
            </div>
          )}
          {busy && <div className="text-xs text-muted-foreground">{PHASE_LABELS[phase]}</div>}
          {error && (
            <div className="flex items-center justify-between gap-2 text-xs text-status-danger">
              <span>{error}</span>
              <Button variant="ghost" size="sm" onClick={onRetryNavigation}>
                重试
              </Button>
            </div>
          )}
          <select
            aria-label="对比基线"
            value={baseline}
            disabled={busy}
            onChange={(event) => {
              const next = event.target.value as MaskCompareBaseline;
              if (next === "tracker_candidate" && trackerCandidates[0]) {
                onSetTrackerCandidate(trackerCandidates[0]);
              } else {
                onSetBaseline(next);
              }
            }}
            className="rounded border border-border bg-background px-2 py-1 text-xs text-foreground"
          >
            <option value="previous_version">上一版本</option>
            <option value="tracker_candidate" disabled={trackerCandidates.length === 0}>
              Tracker 候选{trackerCandidates.length === 0 ? "（无匹配项）" : ""}
            </option>
            <option value="ai_candidate" disabled={!aiCandidateAvailable}>
              当前 AI 候选
            </option>
            <option value="neighbor_keyframe">邻近关键帧</option>
          </select>
          {baseline === "tracker_candidate" && trackerCandidates.length > 0 && (
            <select
              aria-label="Tracker 候选"
              value={trackerCandidateKey ?? trackerCandidates[0].key}
              disabled={busy}
              onChange={(event) => {
                const candidate = trackerCandidates.find((item) => item.key === event.target.value);
                if (candidate) onSetTrackerCandidate(candidate);
              }}
              className="rounded border border-border bg-background px-2 py-1 text-xs text-foreground"
            >
              {trackerCandidates.map((candidate) => (
                <option key={candidate.key} value={candidate.key}>
                  {candidate.label} · F{candidate.frameIndex} · {candidate.digest.slice(0, 8)}
                </option>
              ))}
            </select>
          )}
          {compare && (
            <>
              <div className="grid grid-cols-2 gap-1 text-2xs">
                <div className="rounded bg-muted px-2 py-1">
                  当前 v{compare.current.annotation_version} · {compare.current.source}
                </div>
                <div className="rounded bg-muted px-2 py-1">
                  基线 v{compare.baseline.annotation_version} · {compare.baseline.source}
                </div>
              </div>
              <div className="grid grid-cols-3 gap-1 text-center text-2xs">
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">IoU</div>
                  <div className="font-semibold text-foreground">{(iou! * 100).toFixed(1)}%</div>
                </div>
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">Dice</div>
                  <div className="font-semibold text-foreground">{(dice! * 100).toFixed(1)}%</div>
                </div>
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">变化</div>
                  <div className="font-semibold text-foreground">
                    {compare.metrics.changed_pixels}
                  </div>
                </div>
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">当前面积</div>
                  <div className="font-semibold text-foreground">
                    {compare.metrics.current_area_pixels}
                  </div>
                </div>
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">基线面积</div>
                  <div className="font-semibold text-foreground">
                    {compare.metrics.baseline_area_pixels}
                  </div>
                </div>
                <div className="rounded bg-muted px-1 py-1">
                  <div className="text-muted-foreground">帧</div>
                  <div className="font-semibold text-foreground">
                    {compare.current.frame_index ?? "图片"}
                  </div>
                </div>
              </div>
              {compare.loss.length > 0 && (
                <div className="text-2xs text-status-caution">损失：{compare.loss.join("、")}</div>
              )}
              <div className="flex flex-wrap gap-1">
                {MODE_LABELS.map((item) => (
                  <button
                    key={item.mode}
                    type="button"
                    onClick={() => onSetMode(item.mode)}
                    className={cx(
                      "cursor-pointer rounded border border-border bg-transparent px-2 py-0.5 text-2xs text-muted-foreground",
                      mode === item.mode && "border-brand text-foreground",
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </>
          )}
          {canDecideTrackerRegion && (
            <div className="flex flex-col gap-1 rounded border border-border bg-muted p-2">
              <span className="text-2xs text-muted-foreground">
                只处理质检区域内的 Tracker 差异；区域外与其它帧候选保持待审。
              </span>
              <div className="flex gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={regionDecision !== null}
                  onClick={() => {
                    void decideTrackerRegion("accept");
                  }}
                >
                  {regionDecision === "accept" ? "提交中…" : "区域接受"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={regionDecision !== null}
                  onClick={() => {
                    void decideTrackerRegion("reject");
                  }}
                >
                  {regionDecision === "reject" ? "提交中…" : "区域拒绝"}
                </Button>
              </div>
              {regionDecisionError && (
                <span className="text-2xs text-status-danger">{regionDecisionError}</span>
              )}
            </div>
          )}
          {!stale && (
            <div className="flex gap-1">
              {activeIssue.status !== "resolved" && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    patchIssue.mutate(
                      { issueId: activeIssue.id, status: "resolved" },
                      { onSuccess: onUpdateIssue },
                    )
                  }
                >
                  解决
                </Button>
              )}
              {activeIssue.status !== "wont_fix" && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    patchIssue.mutate(
                      { issueId: activeIssue.id, status: "wont_fix" },
                      { onSuccess: onUpdateIssue },
                    )
                  }
                >
                  搁置
                </Button>
              )}
              {activeIssue.status !== "open" && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    patchIssue.mutate(
                      { issueId: activeIssue.id, status: "open" },
                      { onSuccess: onUpdateIssue },
                    )
                  }
                >
                  重开
                </Button>
              )}
            </div>
          )}
          <div className="flex flex-col gap-1">
            <span className="text-2xs font-medium text-foreground">区域评论</span>
            {feedbackQuery.isLoading && (
              <span className="text-2xs text-muted-foreground">正在加载评论…</span>
            )}
            {!feedbackQuery.isLoading && issueComments.length === 0 && (
              <span className="text-2xs text-muted-foreground">当前区域暂无评论。</span>
            )}
            {issueComments.map((feedback) => (
              <div
                key={feedback.id}
                className="rounded border border-border bg-muted px-2 py-1.5 text-2xs"
              >
                <div className="flex items-center gap-1 text-muted-foreground">
                  <span>{feedback.author_name ?? "未知用户"}</span>
                  {feedback.anchor_position?.boundary_digest && <span>· 边界</span>}
                  <button
                    type="button"
                    className="ml-auto cursor-pointer border-0 bg-transparent text-brand"
                    onClick={() => {
                      const locator = feedback.anchor_position?.compare_locator;
                      if (locator) onReplayFeedback(activeIssue, locator);
                      else onNavigateIssue(activeIssue);
                    }}
                  >
                    定位
                  </button>
                </div>
                <div className="mt-0.5 whitespace-pre-wrap text-foreground">{feedback.body}</div>
                <div className="mt-0.5 font-mono text-muted-foreground">
                  {(
                    feedback.anchor_position?.boundary_digest ??
                    feedback.anchor_position?.region_digest ??
                    "无摘要"
                  ).slice(0, 12)}
                </div>
              </div>
            ))}
            {feedbackQuery.hasNextPage && (
              <Button
                variant="ghost"
                size="sm"
                disabled={feedbackQuery.isFetchingNextPage}
                onClick={() => {
                  void feedbackQuery.fetchNextPage();
                }}
              >
                {feedbackQuery.isFetchingNextPage ? "加载中…" : "加载更多评论"}
              </Button>
            )}
          </div>
          <div className="flex gap-1">
            <input
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              placeholder={stale ? "旧版本问题仅可查看" : "记录区域评论"}
              disabled={stale}
              className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-brand"
            />
            <Button
              variant="ghost"
              size="sm"
              disabled={stale || !comment.trim() || createFeedback.isPending}
              onClick={() => {
                void submitComment();
              }}
            >
              发送
            </Button>
          </div>
        </div>
      )}
      <MaskRepairSheet
        open={repairOpen}
        projectId={projectId}
        actions={repairActions}
        ownerKey={`${projectId}\u001f${taskId}\u001f${scope}`}
        onOpenChange={setRepairOpen}
        onFinished={finishRepair}
      />
    </div>
  );
}
