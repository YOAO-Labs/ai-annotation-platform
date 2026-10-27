import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  videoTrackerApi,
  type VideoTrackQualityIssue,
  type VideoTrackQualityRun,
} from "@/api/videoTracker";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

function metric(value: unknown): string {
  return typeof value === "number" ? value.toFixed(3) : "暂无";
}

type QualityAggregate = {
  annotation_id?: string;
  segment_id?: string;
  side?: string;
  chapter_id?: string;
  title?: string;
  class_name?: string;
  track_id?: string | null;
  matched_frames?: number;
  track_count?: number;
  issue_frames: number;
};

export function VideoTrackQualitySidebar({
  taskId,
  onSeekFrame,
  onPreviewIssue,
}: {
  taskId: string;
  onSeekFrame: (frame: number) => void;
  onPreviewIssue?: (run: VideoTrackQualityRun, issue: VideoTrackQualityIssue) => void;
}) {
  const queryClient = useQueryClient();
  const runsQuery = useQuery({
    queryKey: ["video-track-quality", taskId],
    queryFn: ({ signal }) => videoTrackerApi.trackQuality(taskId, { signal }),
    refetchInterval: (query) =>
      (query.state.data ?? []).some((run) => ["pending", "running"].includes(run.status))
        ? 3000
        : false,
  });
  const [runId, setRunId] = useState<string | null>(null);
  const taskRunIds = useMemo(
    () => new Set((runsQuery.data ?? []).map((candidate) => candidate.id)),
    [runsQuery.data],
  );
  const selectedRunId = runId && taskRunIds.has(runId) ? runId : (runsQuery.data?.[0]?.id ?? null);
  const detailQuery = useQuery({
    queryKey: ["video-track-quality", taskId, selectedRunId],
    queryFn: ({ signal }) => videoTrackerApi.trackQualityDetail(taskId, selectedRunId!, { signal }),
    enabled: !!selectedRunId,
  });
  const run = detailQuery.data;
  const [decisions, setDecisions] = useState<Record<string, "same_track" | "different_track">>({});
  const [manualPairs, setManualPairs] = useState<VideoTrackQualityRun["pairs"]>([]);
  const [manualLeftId, setManualLeftId] = useState("");
  const [manualRightId, setManualRightId] = useState("");
  useEffect(() => {
    setRunId(null);
    setDecisions({});
    setManualPairs([]);
    setManualLeftId("");
    setManualRightId("");
  }, [taskId]);
  useEffect(() => {
    if (!run) return;
    setDecisions(
      Object.fromEntries(
        run.pairs.map((pair) => [
          `${pair.left_annotation_id}:${pair.right_annotation_id}`,
          pair.decision,
        ]),
      ),
    );
    setManualPairs([]);
    setManualLeftId("");
    setManualRightId("");
  }, [run]);
  const reviewedPairs = [...(run?.pairs ?? []), ...manualPairs];
  const accept = useMutation({
    mutationFn: (target: VideoTrackQualityRun) =>
      videoTrackerApi.acceptTrackQuality(taskId, target.id, {
        input_digest: target.input_digest,
        pairs: reviewedPairs.map((pair) => ({
          left_annotation_id: pair.left_annotation_id,
          right_annotation_id: pair.right_annotation_id,
          decision:
            decisions[`${pair.left_annotation_id}:${pair.right_annotation_id}`] ?? pair.decision,
        })),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["video-track-quality", taskId] });
    },
  });
  const reopen = useMutation({
    mutationFn: (segmentId: string) => videoTrackerApi.reopenSegment(taskId, segmentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["video-track-quality", taskId] });
      queryClient.invalidateQueries({ queryKey: ["video-segments", taskId] });
    },
  });
  const rerun = useMutation({
    mutationFn: (target: VideoTrackQualityRun) =>
      videoTrackerApi.runTrackQuality(taskId, target.left_segment_id, target.right_segment_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["video-track-quality", taskId] });
    },
  });
  const summary = useMemo<Array<[string, unknown]>>(
    () =>
      run
        ? [
            ["HOTA", run.metrics.HOTA],
            ["IDF1", run.metrics.IDF1],
            ["MOTA L", run.metrics.MOTA_left],
            ["MOTA R", run.metrics.MOTA_right],
          ]
        : [],
    [run],
  );
  const pairedLeftIds = new Set(reviewedPairs.map((pair) => pair.left_annotation_id));
  const pairedRightIds = new Set(reviewedPairs.map((pair) => pair.right_annotation_id));
  const leftFragments =
    run?.fragments.filter(
      (item) => item.segment_id === run.left_segment_id && !pairedLeftIds.has(item.annotation_id),
    ) ?? [];
  const selectedLeft = leftFragments.find((item) => item.annotation_id === manualLeftId);
  const rightFragments =
    run?.fragments.filter(
      (item) =>
        item.segment_id === run.right_segment_id &&
        !pairedRightIds.has(item.annotation_id) &&
        (!selectedLeft ||
          (item.class_name === selectedLeft.class_name &&
            item.tool_unit_id === selectedLeft.tool_unit_id &&
            item.geometry_family === selectedLeft.geometry_family)),
    ) ?? [];
  const aggregates = (run?.metrics.aggregates ?? {}) as Partial<
    Record<"tracks" | "segments" | "chapters", QualityAggregate[]>
  >;

  return (
    <section className="grid gap-2 border-t border-border pt-3" lang="zh">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-foreground">Track 边界质量</h3>
        <span className="mono text-2xs text-muted-foreground">
          {runsQuery.data?.length ?? 0} boundaries
        </span>
      </div>
      <div className="flex gap-1 overflow-x-auto pb-1">
        {(runsQuery.data ?? []).map((item, index) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setRunId(item.id)}
            className={cn(
              "min-h-10 shrink-0 rounded border px-2 text-xs active:scale-95 focus-visible:ring-2 focus-visible:ring-ring",
              item.id === selectedRunId
                ? "border-brand bg-brand/10 text-brand"
                : "border-border bg-card text-muted-foreground",
            )}
          >
            B{index + 1} · {item.status}
          </button>
        ))}
      </div>
      {run && (
        <>
          <div className="grid grid-cols-4 gap-px overflow-hidden rounded bg-border">
            {summary.map(([label, value]) => (
              <div key={label as string} className="bg-card px-2 py-2 text-center">
                <div className="mono text-xs font-semibold text-foreground">{metric(value)}</div>
                <div className="text-2xs text-muted-foreground">{label}</div>
              </div>
            ))}
          </div>
          {reviewedPairs.length > 0 && (
            <div className="grid gap-1">
              {reviewedPairs.map((pair) => {
                const key = `${pair.left_annotation_id}:${pair.right_annotation_id}`;
                return (
                  <div
                    key={key}
                    className="flex min-h-10 items-center gap-2 border-b border-border py-1"
                  >
                    <span className="min-w-0 flex-1 truncate text-xs text-foreground">
                      {pair.class_name} · {pair.matched_frames} 帧
                    </span>
                    <select
                      aria-label={`${pair.class_name} 轨迹关系`}
                      value={decisions[key] ?? pair.decision}
                      onChange={(event) =>
                        setDecisions((current) => ({
                          ...current,
                          [key]: event.target.value as "same_track" | "different_track",
                        }))
                      }
                      className="h-8 rounded border border-border bg-card px-1 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <option value="same_track">同一轨迹</option>
                      <option value="different_track">不同轨迹</option>
                    </select>
                  </div>
                );
              })}
            </div>
          )}
          {leftFragments.length > 0 && rightFragments.length > 0 && (
            <div className="grid grid-cols-[1fr_1fr_auto] gap-1">
              <select
                aria-label="左侧未匹配 fragment"
                value={manualLeftId}
                onChange={(event) => {
                  setManualLeftId(event.target.value);
                  setManualRightId("");
                }}
                className="h-8 min-w-0 rounded border border-border bg-card px-1 text-xs text-foreground"
              >
                <option value="" disabled hidden>
                  左 fragment
                </option>
                {leftFragments.map((item) => (
                  <option key={item.annotation_id} value={item.annotation_id}>
                    {item.class_name} · {item.track_id ?? item.annotation_id.slice(0, 8)}
                  </option>
                ))}
              </select>
              <select
                aria-label="右侧未匹配 fragment"
                value={manualRightId}
                onChange={(event) => setManualRightId(event.target.value)}
                className="h-8 min-w-0 rounded border border-border bg-card px-1 text-xs text-foreground"
              >
                <option value="" disabled hidden>
                  右 fragment
                </option>
                {rightFragments.map((item) => (
                  <option key={item.annotation_id} value={item.annotation_id}>
                    {item.class_name} · {item.track_id ?? item.annotation_id.slice(0, 8)}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                disabled={!manualLeftId || !manualRightId}
                onClick={() => {
                  const left = leftFragments.find((item) => item.annotation_id === manualLeftId);
                  if (
                    !left ||
                    reviewedPairs.some(
                      (pair) =>
                        pair.left_annotation_id === manualLeftId &&
                        pair.right_annotation_id === manualRightId,
                    )
                  )
                    return;
                  const key = `${manualLeftId}:${manualRightId}`;
                  setManualPairs((current) => [
                    ...current,
                    {
                      left_annotation_id: manualLeftId,
                      right_annotation_id: manualRightId,
                      class_name: left.class_name,
                      geometry_family: left.geometry_family ?? "unknown",
                      matched_frames: 0,
                      suggestion: "different_track",
                      decision: "same_track",
                    },
                  ]);
                  setDecisions((current) => ({ ...current, [key]: "same_track" }));
                  setManualLeftId("");
                  setManualRightId("");
                }}
              >
                添加
              </Button>
            </div>
          )}
          <div className="max-h-40 overflow-auto">
            {run.issues.map((issue) => (
              <button
                key={issue.id}
                type="button"
                onClick={() => {
                  onSeekFrame(issue.frame_start);
                  onPreviewIssue?.(run, issue);
                }}
                className="flex min-h-10 w-full items-center gap-2 border-b border-border px-1 text-left text-xs text-muted-foreground active:scale-[0.99] focus-visible:ring-2 focus-visible:ring-ring"
              >
                <Icon name="crosshair" size={11} />
                <span className="min-w-0 flex-1 truncate">{issue.code}</span>
                <span className="mono text-foreground">
                  {issue.frame_start}-{issue.frame_end}
                </span>
              </button>
            ))}
          </div>
          {(["tracks", "segments", "chapters"] as const).map((scope) => {
            const rows = aggregates[scope] ?? [];
            if (rows.length === 0) return null;
            return (
              <div key={scope} className="grid gap-1">
                <h4 className="text-2xs font-semibold text-muted-foreground">
                  {scope === "tracks" ? "按 Track" : scope === "segments" ? "按分段" : "按章节"}
                </h4>
                {rows.map((row) => (
                  <div
                    key={row.annotation_id ?? row.segment_id ?? row.chapter_id}
                    className="flex min-h-8 items-center justify-between gap-2 rounded bg-muted px-2 text-xs"
                  >
                    <span className="min-w-0 truncate text-foreground">
                      {row.title ?? row.class_name ?? row.side ?? row.track_id ?? "未命名"}
                    </span>
                    <span className="mono shrink-0 text-muted-foreground">
                      {row.issue_frames} 异常帧
                      {row.matched_frames !== undefined ? ` · ${row.matched_frames} 匹配` : ""}
                      {row.track_count !== undefined ? ` · ${row.track_count} tracks` : ""}
                    </span>
                  </div>
                ))}
              </div>
            );
          })}
          <Button
            size="sm"
            variant="primary"
            disabled={run.status !== "completed" || accept.isPending}
            onClick={() => accept.mutate(run)}
          >
            <Icon name="check" size={12} />
            接受边界对账
          </Button>
          {(run.status === "failed" || run.status === "stale") && (
            <Button size="sm" disabled={rerun.isPending} onClick={() => rerun.mutate(run)}>
              重新运行质量检查
            </Button>
          )}
          <div className="grid grid-cols-2 gap-1">
            <Button
              size="sm"
              disabled={reopen.isPending}
              onClick={() => reopen.mutate(run.left_segment_id)}
            >
              返工左分段
            </Button>
            <Button
              size="sm"
              disabled={reopen.isPending}
              onClick={() => reopen.mutate(run.right_segment_id)}
            >
              返工右分段
            </Button>
          </div>
        </>
      )}
      {!runsQuery.isLoading && !run && (
        <p className="text-xs text-muted-foreground">相邻分段提交后会自动生成质量报告。</p>
      )}
    </section>
  );
}
