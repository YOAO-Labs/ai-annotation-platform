import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";
import {
  tasksApi,
  type SceneTrackCommandPreview,
  type SceneTrackCommandRequest,
} from "@/api/tasks";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useToastStore } from "@/components/ui/Toast";
import { cn } from "@/lib/utils";

interface TrackOperationsPanelProps {
  taskId: string;
  currentFrame: number;
  sceneStartFrame: number;
  sceneEndFrame: number;
  selectedTrackId: string | null;
  selectedAnnotationId: string | null;
  readOnly: boolean;
  onCompleted?: () => void;
}

type Operation = "split" | "merge" | "mark_absent" | "resume" | "terminate";

const OPERATION_LABEL: Record<Operation, string> = {
  split: "拆分",
  merge: "合并",
  mark_absent: "标记缺席",
  resume: "恢复出现",
  terminate: "终止轨迹",
};

function shortTrackId(trackId: string): string {
  return trackId.length > 18 ? `${trackId.slice(0, 10)}…${trackId.slice(-5)}` : trackId;
}

function errorReason(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.detailRaw || typeof error.detailRaw !== "object") {
    return null;
  }
  const reason = (error.detailRaw as { reason?: unknown }).reason;
  return typeof reason === "string" ? reason : null;
}

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

function intervalLabel(start: number, end: number | null): string {
  return `F${start}–${end == null ? "∞" : `F${end}`}`;
}

export function TrackOperationsPanel({
  taskId,
  currentFrame,
  sceneStartFrame,
  sceneEndFrame,
  selectedTrackId,
  selectedAnnotationId,
  readOnly,
  onCompleted,
}: TrackOperationsPanelProps) {
  const queryClient = useQueryClient();
  const pushToast = useToastStore((state) => state.push);
  const [operation, setOperation] = useState<Operation>("split");
  const [secondaryTrackId, setSecondaryTrackId] = useState("");
  const [resumeFrame, setResumeFrame] = useState(() => Math.min(sceneEndFrame, currentFrame + 1));
  const [resumeAfterAbsence, setResumeAfterAbsence] = useState(() => currentFrame < sceneEndFrame);
  const [confirmDeactivation, setConfirmDeactivation] = useState(false);
  const [preview, setPreview] = useState<SceneTrackCommandPreview | null>(null);

  useEffect(() => {
    setSecondaryTrackId("");
    setPreview(null);
    setConfirmDeactivation(false);
    setResumeFrame(Math.min(sceneEndFrame, currentFrame + 1));
    setResumeAfterAbsence(currentFrame < sceneEndFrame);
  }, [currentFrame, sceneEndFrame, selectedTrackId, taskId]);

  const detailQuery = useQuery({
    queryKey: ["scene-track-detail", taskId, selectedTrackId],
    queryFn: ({ signal }) => tasksApi.getSceneTrack(taskId, selectedTrackId!, { signal }),
    enabled: !!selectedTrackId,
  });
  const historyQuery = useQuery({
    queryKey: ["scene-track-operations", taskId, selectedTrackId],
    queryFn: ({ signal }) =>
      tasksApi.listSceneTrackOperations(taskId, selectedTrackId!, { signal }),
    enabled: !!selectedTrackId,
  });
  const candidatesQuery = useQuery({
    queryKey: ["point-cloud-track-operation-candidates", taskId, selectedTrackId],
    queryFn: ({ signal }) =>
      tasksApi.listPointCloudTrackOperationCandidates(taskId, selectedTrackId!, { signal }),
    enabled: operation === "merge" && !!selectedTrackId && !readOnly,
  });
  const candidates = useMemo(
    () => candidatesQuery.data?.candidates ?? [],
    [candidatesQuery.data?.candidates],
  );

  const commandRequest = useMemo<SceneTrackCommandRequest | null>(() => {
    if (!selectedTrackId) return null;
    if (operation === "merge") {
      return secondaryTrackId &&
        candidates.some((candidate) => candidate.track_id === secondaryTrackId)
        ? {
            kind: "merge",
            track_id: selectedTrackId,
            secondary_track_id: secondaryTrackId,
          }
        : null;
    }
    if (operation === "resume") {
      return selectedAnnotationId
        ? {
            kind: "resume",
            track_id: selectedTrackId,
            resume_frame: resumeFrame,
            source_annotation_id: selectedAnnotationId,
          }
        : null;
    }
    if (operation === "mark_absent") {
      return {
        kind: "mark_absent",
        track_id: selectedTrackId,
        frame_index: currentFrame,
        ...(resumeAfterAbsence ? { resume_frame: resumeFrame } : {}),
      };
    }
    return {
      kind: operation,
      track_id: selectedTrackId,
      frame_index: currentFrame,
    };
  }, [
    candidates,
    currentFrame,
    operation,
    resumeAfterAbsence,
    resumeFrame,
    secondaryTrackId,
    selectedAnnotationId,
    selectedTrackId,
  ]);

  const invalidateTrackViews = () => {
    queryClient.invalidateQueries({ queryKey: ["annotations"] });
    queryClient.invalidateQueries({ queryKey: ["scene-timeline"] });
    queryClient.invalidateQueries({ queryKey: ["scene-track-detail"] });
    queryClient.invalidateQueries({ queryKey: ["scene-track-operations"] });
    queryClient.invalidateQueries({ queryKey: ["point-cloud-track-operation-candidates"] });
  };

  const previewMutation = useMutation({
    mutationFn: (payload: SceneTrackCommandRequest) =>
      tasksApi.previewSceneTrackCommand(taskId, payload),
    onSuccess: (value) => {
      setPreview(value);
      setConfirmDeactivation(false);
    },
    onError: () => {
      setPreview(null);
      pushToast({ msg: "轨迹生命周期预览失败", kind: "error" });
    },
  });
  const executeMutation = useMutation({
    mutationFn: ({
      payload,
      snapshotToken,
    }: {
      payload: SceneTrackCommandRequest;
      snapshotToken: string;
    }) =>
      tasksApi.executeSceneTrackCommand(taskId, {
        ...payload,
        confirm_member_deactivation: confirmDeactivation,
        snapshot_token: snapshotToken,
        idempotency_key: idempotencyKey(`scene-track-${payload.kind}`),
      }),
    onSuccess: (result) => {
      setPreview(null);
      setSecondaryTrackId("");
      setConfirmDeactivation(false);
      invalidateTrackViews();
      pushToast({ msg: `${OPERATION_LABEL[result.kind as Operation]}已提交`, kind: "success" });
      onCompleted?.();
    },
    onError: (error) => {
      const reason = errorReason(error);
      if (reason === "track_snapshot_stale") {
        setPreview(null);
        pushToast({ msg: "轨迹已变化，请重新预览", kind: "warning" });
        return;
      }
      pushToast({ msg: "轨迹生命周期操作失败", kind: "error" });
    },
  });
  const revertMutation = useMutation({
    mutationFn: (operationId: string) =>
      tasksApi.revertSceneTrackOperation(taskId, operationId, idempotencyKey("scene-track-revert")),
    onSuccess: () => {
      invalidateTrackViews();
      pushToast({ msg: "最近一次轨迹操作已撤销", kind: "success" });
      onCompleted?.();
    },
    onError: (error) => {
      pushToast({
        msg:
          errorReason(error) === "operation_revert_stale"
            ? "轨迹后来已被修改，不能安全撤销"
            : "撤销轨迹操作失败",
        kind: "error",
      });
    },
  });

  const selectOperation = (next: Operation) => {
    setOperation(next);
    setPreview(null);
    setConfirmDeactivation(false);
    if (next !== "merge") setSecondaryTrackId("");
  };
  const latestUndoable = historyQuery.data?.operations.find(
    (item) => item.status === "committed" && item.kind !== "revert",
  );
  const needsConfirmation = preview?.affected_members.requires_confirmation ?? false;
  const availableCommands = detailQuery.data?.available_commands ?? [];
  const operationAvailable = detailQuery.isLoading || availableCommands.includes(operation);
  const canPreview = !!commandRequest && !readOnly && !previewMutation.isPending;
  const canConfirm =
    !!preview &&
    !readOnly &&
    !executeMutation.isPending &&
    (!needsConfirmation || confirmDeactivation);

  return (
    <section className="space-y-4" aria-label="3D 轨迹生命周期">
      <div>
        <h3 className="text-sm font-semibold text-foreground">轨迹生命周期</h3>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">
          统一管理身份、存在区间和帧成员。所有修改先预览、写入操作账本，并可在无后续修改时撤销。
        </p>
      </div>

      {!selectedTrackId ? (
        <p className="rounded-md border border-dashed border-border px-3 py-5 text-center text-xs text-muted-foreground">
          请先在当前帧选择一个已有跨帧轨迹的 3D 框。
        </p>
      ) : (
        <>
          <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
            <div className="flex items-center gap-2">
              <span className="font-mono text-foreground" title={selectedTrackId}>
                {shortTrackId(selectedTrackId)}
              </span>
              <Badge variant="outline">revision {detailQuery.data?.revision ?? "…"}</Badge>
              {detailQuery.data && (
                <Badge variant="outline">
                  {detailQuery.data.presence_mode === "explicit" ? "显式存在" : "成员包络"}
                </Badge>
              )}
              <span className="ml-auto text-muted-foreground">当前 F{currentFrame}</span>
            </div>
            {detailQuery.data && (
              <div className="mt-2 flex flex-wrap gap-1.5" aria-label="轨迹存在区间">
                {detailQuery.data.intervals.map((interval) => (
                  <span
                    key={interval.id}
                    className="rounded-sm bg-brand/10 px-1.5 py-0.5 text-brand"
                  >
                    {intervalLabel(interval.start_frame, interval.end_frame)}
                  </span>
                ))}
                <span className="text-muted-foreground">
                  {detailQuery.data.members.by_temporal_role.keyframe ?? 0} 关键帧 ·{" "}
                  {detailQuery.data.members.by_temporal_role.derived ?? 0} 派生 ·{" "}
                  {detailQuery.data.members.by_temporal_role.sample ?? 0} 样本
                </span>
              </div>
            )}
          </div>

          <div className="grid grid-cols-3 gap-2" role="group" aria-label="轨迹生命周期操作">
            {(Object.keys(OPERATION_LABEL) as Operation[]).map((item) => (
              <Button
                key={item}
                size="sm"
                variant={operation === item ? "primary" : "default"}
                aria-pressed={operation === item}
                disabled={readOnly || (!detailQuery.isLoading && !availableCommands.includes(item))}
                onClick={() => selectOperation(item)}
              >
                {OPERATION_LABEL[item]}
              </Button>
            ))}
          </div>

          <div className="space-y-3">
            {operation === "split" && (
              <p className="text-xs leading-5 text-muted-foreground">
                F{currentFrame} 及之前保留原身份；之后的区间与成员迁移到新轨迹。
              </p>
            )}
            {operation === "merge" && (
              <label className="block space-y-1 text-xs text-muted-foreground">
                合并候选
                <select
                  aria-label="合并候选轨迹"
                  value={secondaryTrackId}
                  disabled={candidatesQuery.isLoading || candidates.length === 0}
                  onChange={(event) => {
                    setSecondaryTrackId(event.target.value);
                    setPreview(null);
                  }}
                  className="h-9 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground"
                >
                  <option value="" disabled hidden>
                    {candidatesQuery.isLoading
                      ? "正在查找候选…"
                      : candidates.length === 0
                        ? "没有可安全合并的同类轨迹"
                        : "选择同类、无重叠轨迹"}
                  </option>
                  {secondaryTrackId &&
                    !candidates.some((candidate) => candidate.track_id === secondaryTrackId) && (
                      <option value={secondaryTrackId} disabled hidden>
                        已选轨迹（已不在合并候选中）
                      </option>
                    )}
                  {candidates.map((candidate) => (
                    <option key={candidate.track_id} value={candidate.track_id}>
                      {candidate.class_name} · F{candidate.first_frame}–F{candidate.last_frame} ·{" "}
                      {candidate.member_count} 框 · {shortTrackId(candidate.track_id)}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {operation === "mark_absent" && (
              <div className="space-y-2 text-xs text-muted-foreground">
                <p className="leading-5">
                  从 F{currentFrame} 起移出存在区间；对应活跃成员将作为隐藏历史停用。
                </p>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={resumeAfterAbsence}
                    disabled={currentFrame >= sceneEndFrame}
                    onChange={(event) => {
                      setResumeAfterAbsence(event.target.checked);
                      setPreview(null);
                    }}
                  />
                  在后续帧恢复同一轨迹
                </label>
                {resumeAfterAbsence && (
                  <label className="block space-y-1">
                    恢复帧
                    <input
                      aria-label="缺席后的恢复帧"
                      type="number"
                      min={currentFrame + 1}
                      max={sceneEndFrame}
                      value={resumeFrame}
                      onChange={(event) => {
                        setResumeFrame(Number(event.target.value));
                        setPreview(null);
                      }}
                      className="h-9 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground"
                    />
                  </label>
                )}
              </div>
            )}
            {operation === "terminate" && (
              <p className="text-xs leading-5 text-muted-foreground">
                保留 F{currentFrame}，关闭全部后续存在区间并停用未来成员。
              </p>
            )}
            {operation === "resume" && (
              <label className="block space-y-1 text-xs text-muted-foreground">
                恢复帧
                <input
                  aria-label="轨迹恢复帧"
                  type="number"
                  min={sceneStartFrame}
                  max={sceneEndFrame}
                  value={resumeFrame}
                  onChange={(event) => {
                    setResumeFrame(Number(event.target.value));
                    setPreview(null);
                  }}
                  className="h-9 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground"
                />
                <span className="block leading-5">
                  从当前所选框复制几何，在缺席帧创建新的人工关键帧；不会猜测成新对象。
                </span>
              </label>
            )}
            {operation === "resume" && !selectedAnnotationId && (
              <p className="text-xs text-status-caution">
                恢复需要选中一个可作为几何来源的 3D 框。
              </p>
            )}
            {readOnly && (
              <p className="text-xs text-status-caution">当前任务只读，不能修改轨迹。</p>
            )}
            {!readOnly && !operationAvailable && (
              <p className="text-xs text-muted-foreground">
                该操作不适用于轨迹当前的存在区间和成员状态。
              </p>
            )}
            <Button
              size="sm"
              variant="default"
              disabled={!canPreview || !operationAvailable}
              onClick={() => commandRequest && previewMutation.mutate(commandRequest)}
            >
              {previewMutation.isPending ? "计算中…" : "预览影响"}
            </Button>
          </div>
        </>
      )}

      {preview && commandRequest && (
        <div className="space-y-3 rounded-md border border-brand/30 bg-brand/5 p-3" role="status">
          <div className="flex items-center gap-2">
            <Badge variant="accent">待确认</Badge>
            <span className="text-xs font-medium text-foreground">
              {OPERATION_LABEL[preview.kind as Operation]}
            </span>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            影响 {preview.affected_members.total} 个成员
            {preview.affected_members.frames.length > 0 &&
              ` · F${preview.affected_members.frames[0]}–F${
                preview.affected_members.frames[preview.affected_members.frames.length - 1]
              }`}
          </p>
          <div className="flex flex-wrap gap-1.5 text-2xs">
            {Object.entries(preview.affected_members.by_temporal_role).map(([role, count]) => (
              <span key={role} className="rounded-sm bg-muted px-1.5 py-0.5 text-muted-foreground">
                {role} {count}
              </span>
            ))}
          </div>
          {needsConfirmation && (
            <label className="flex items-start gap-2 rounded-md border border-status-caution/40 bg-status-caution-soft p-2 text-xs text-status-caution">
              <input
                type="checkbox"
                checked={confirmDeactivation}
                onChange={(event) => setConfirmDeactivation(event.target.checked)}
              />
              我已确认：关键帧或 sample 成员会停用并保留为隐藏历史，可通过本次操作撤销。
            </label>
          )}
          <div className="flex justify-end gap-2">
            <Button size="xs" variant="ghost" onClick={() => setPreview(null)}>
              取消
            </Button>
            <Button
              size="xs"
              variant={preview.kind === "terminate" ? "danger" : "primary"}
              disabled={!canConfirm}
              onClick={() =>
                executeMutation.mutate({
                  payload: commandRequest,
                  snapshotToken: preview.snapshot_token,
                })
              }
            >
              {executeMutation.isPending
                ? "提交中…"
                : `确认${OPERATION_LABEL[preview.kind as Operation]}`}
            </Button>
          </div>
        </div>
      )}

      {selectedTrackId && (
        <div className="border-t border-border pt-3">
          <div className="flex items-center justify-between gap-2">
            <div>
              <p className="text-xs font-medium text-foreground">最近操作</p>
              <p className="mt-0.5 text-2xs text-muted-foreground">
                {latestUndoable
                  ? `${OPERATION_LABEL[latestUndoable.kind as Operation]} · ${new Date(
                      latestUndoable.created_at,
                    ).toLocaleString()}`
                  : "暂无可撤销操作"}
              </p>
            </div>
            <Button
              size="xs"
              variant="default"
              disabled={!latestUndoable || readOnly || revertMutation.isPending}
              onClick={() => latestUndoable && revertMutation.mutate(latestUndoable.id)}
            >
              {revertMutation.isPending ? "撤销中…" : "撤销最近操作"}
            </Button>
          </div>
          {historyQuery.isError && (
            <p className={cn("mt-2 text-xs", "text-status-danger")}>操作历史加载失败。</p>
          )}
        </div>
      )}
    </section>
  );
}
