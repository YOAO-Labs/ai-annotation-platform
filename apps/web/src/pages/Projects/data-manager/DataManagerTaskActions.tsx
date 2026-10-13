import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { ExportOptions, ExportTarget } from "@/api/projects";
import type {
  DataManagerActionRequestOptions,
  DataManagerTaskAssignmentResponse,
  DataManagerTaskExportPayload,
} from "@/api/dataManagerActions";
import { dataManagerTaskActionsApi } from "@/api/dataManagerActions";
import { useAsyncJob } from "@/hooks/useAsyncJob";
import { useMLBackends } from "@/hooks/useMLBackends";
import type { TriggerPreannotationPayload } from "@/hooks/usePreannotation";
import { useProject, useProjectMembers } from "@/hooks/useProjects";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { PreannotateConfigForm } from "@/pages/AIPreAnnotate/components/PreannotateConfigForm";
import { usePreannotateConfig } from "@/pages/AIPreAnnotate/components/usePreannotateConfig";

const KEEP = "__keep__";
const CLEAR = "__clear__";
type AssignmentChoice = typeof KEEP | typeof CLEAR | string;

const MAX_TASK_IDS = 200;

const IMAGE_EXPORT_TARGETS: Array<{ id: ExportTarget; label: string }> = [
  { id: "coco", label: "COCO" },
  { id: "yolo-det", label: "YOLO 检测" },
  { id: "yolo-obb", label: "YOLO 旋转框" },
  { id: "yolo-seg", label: "YOLO 分割" },
  { id: "aap_json", label: "AAP JSON" },
];

const VIDEO_EXPORT_TARGETS: Array<{ id: ExportTarget; label: string }> = [
  { id: "video_json", label: "Video JSON" },
  { id: "mot", label: "MOT" },
  { id: "yolo-frames-det", label: "YOLO 逐帧检测" },
  { id: "yolo-frames-seg", label: "YOLO 逐帧分割" },
  { id: "coco-frames-seg", label: "COCO 逐帧分割" },
  { id: "davis", label: "DAVIS" },
  { id: "youtube-vos", label: "YouTube-VOS" },
  { id: "mots", label: "MOTS" },
  { id: "aap_json", label: "AAP JSON" },
];

const LIDAR_EXPORT_TARGETS: Array<{ id: ExportTarget; label: string }> = [
  { id: "aap_json", label: "AAP JSON" },
];

function exportTargetsForDataType(dataType: string | null | undefined) {
  if (dataType === "video") return VIDEO_EXPORT_TARGETS;
  if (dataType === "lidar" || dataType === "point_cloud") return LIDAR_EXPORT_TARGETS;
  return IMAGE_EXPORT_TARGETS;
}

function newActionKey(prefix: string) {
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

function actionKeyFor(
  ref: { current: { fingerprint: string; key: string } | null },
  prefix: string,
  fingerprint: string,
): string {
  if (ref.current?.fingerprint !== fingerprint) {
    ref.current = { fingerprint, key: newActionKey(prefix) };
  }
  return ref.current.key;
}

export interface DataManagerTaskExportOptions extends ExportOptions {
  targets: ExportTarget[];
}

export interface DataManagerTaskActionsProps {
  projectId: string;
  taskIds: readonly string[];
  onCompleted?: () => void;
  /** Optional prebuilt payload from an existing AI panel; omitted uses the shared config form. */
  preannotation?: Omit<TriggerPreannotationPayload, "task_ids" | "batch_id"> | null;
  /** Existing export preferences; the action opens a format selector before dispatch. */
  exportOptions?: DataManagerTaskExportOptions;
  /** Project `project.manage` capability: gates assignment and preannotation. */
  canManage?: boolean;
  /** Project `export.annotations` capability: gates selected-task export. */
  canExport?: boolean;
}

function toExportPayload(
  taskIds: string[],
  options: DataManagerTaskExportOptions,
): DataManagerTaskExportPayload {
  return {
    task_ids: taskIds,
    targets: options.targets,
    ...(options.includeAttributes === undefined
      ? {}
      : { include_attributes: options.includeAttributes }),
    ...(options.videoFrameMode ? { video_frame_mode: options.videoFrameMode } : {}),
    ...(options.indexedOverlapPolicy
      ? { indexed_overlap_policy: options.indexedOverlapPolicy }
      : {}),
    ...(options.videoOverlapPolicy ? { video_overlap_policy: options.videoOverlapPolicy } : {}),
    ...(options.motsFrameBase === undefined ? {} : { mots_frame_base: options.motsFrameBase }),
    ...(options.lidar ? { lidar: { kitti_camera_role: options.lidar.kittiCameraRole } } : {}),
  };
}

function reasonLabel(reason: string | null) {
  switch (reason) {
    case "assignment_unchanged":
      return "分派未变化";
    case "batch_admin_locked":
      return "批次已锁定";
    case "batch_archived":
      return "批次已归档";
    case "task_locked":
      return "任务正在被编辑";
    case "task_status_not_assignable":
      return "当前状态不可分派";
    case "reviewer_assignment_requires_review":
      return "仅待审核任务可指定审核员";
    case "task_not_found_or_outside_project":
      return "任务不可用";
    default:
      return reason ?? "未说明";
  }
}

const JOB_STATUS_LABEL: Record<string, string> = {
  pending: "等待中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  queued: "已入队",
};

function jobStatusLabel(status: string | undefined) {
  return status ? (JOB_STATUS_LABEL[status] ?? status) : "等待状态";
}

function memberLabel(memberById: Map<string, string>, userId: string | null): string {
  if (!userId) return "未分派";
  return memberById.get(userId) ?? userId.slice(0, 8);
}

function assignmentChanges(
  item: DataManagerTaskAssignmentResponse["items"][number],
  memberById: Map<string, string>,
): string[] {
  const changes: string[] = [];
  const beforeAnnotator = item.effective_before_annotator_id ?? item.before_annotator_id;
  const afterAnnotator = item.effective_after_annotator_id ?? item.after_annotator_id;
  const beforeReviewer = item.effective_before_reviewer_id ?? item.before_reviewer_id;
  const afterReviewer = item.effective_after_reviewer_id ?? item.after_reviewer_id;
  if (beforeAnnotator !== afterAnnotator) {
    changes.push(
      `标注员：${memberLabel(memberById, beforeAnnotator)} → ${memberLabel(
        memberById,
        afterAnnotator,
      )}`,
    );
  }
  if (beforeReviewer !== afterReviewer) {
    changes.push(
      `审核员：${memberLabel(memberById, beforeReviewer)} → ${memberLabel(
        memberById,
        afterReviewer,
      )}`,
    );
  }
  return changes;
}

function ActionJobStatus({
  label,
  jobId,
  taskIds,
  query,
  onRerun,
  busy,
}: {
  label: string;
  jobId: string;
  taskIds: readonly string[];
  query: ReturnType<typeof useAsyncJob>;
  onRerun: () => void;
  busy: boolean;
}) {
  const status = query.data?.status ?? "pending";
  const detail = query.data?.error_message;
  return (
    <span className={status === "failed" ? "text-status-danger" : undefined}>
      {label} <span className="mono">{jobId}</span> ·{" "}
      <span title={taskIds.join("\n")}>提交时选定 {taskIds.length} 个任务</span> ·{" "}
      {jobStatusLabel(status)}
      {query.data?.progress_pct !== undefined && ` · ${query.data.progress_pct}%`}
      {detail && ` · ${detail}`}
      {status === "failed" && (
        <Button size="xs" className="ml-1" disabled={busy} onClick={onRerun}>
          重新运行
        </Button>
      )}
      {query.isError && (
        <>
          {` · 查询失败`}
          <Button size="xs" className="ml-1" onClick={() => void query.refetch()}>
            重试查询
          </Button>
        </>
      )}
    </span>
  );
}

function ActionError({ message }: { message: string | null }) {
  return message ? (
    <div
      role="alert"
      className="rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
    >
      {message}
    </div>
  ) : null;
}

export function DataManagerTaskActions({
  projectId,
  taskIds,
  onCompleted,
  preannotation,
  exportOptions = { targets: ["coco"] },
  canManage = true,
  canExport = true,
}: DataManagerTaskActionsProps) {
  const ids = useMemo(() => [...new Set(taskIds)], [taskIds]);
  const projectQ = useProject(projectId);
  const queryClient = useQueryClient();
  const backendsQ = useMLBackends(projectId);
  const { data: members = [], isLoading: membersLoading } = useProjectMembers(projectId);
  const [selectedBackendId, setSelectedBackendId] = useState<string | null>(null);
  const [assignmentOpen, setAssignmentOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [preannotateOpen, setPreannotateOpen] = useState(false);
  const [selectedExportTargets, setSelectedExportTargets] = useState<ExportTarget[]>(["coco"]);
  const [annotatorChoice, setAnnotatorChoice] = useState<AssignmentChoice>(KEEP);
  const [reviewerChoice, setReviewerChoice] = useState<AssignmentChoice>(KEEP);
  const [assignmentPreview, setAssignmentPreview] =
    useState<DataManagerTaskAssignmentResponse | null>(null);
  const [assignmentResult, setAssignmentResult] =
    useState<DataManagerTaskAssignmentResponse | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "apply" | "export" | "preannotate" | null>(null);
  const [exportJobId, setExportJobId] = useState<string | null>(null);
  const [preannotateJobId, setPreannotateJobId] = useState<string | null>(null);
  const exportJobRequestRef = useRef<DataManagerTaskExportPayload | null>(null);
  const preannotateJobRequestRef = useRef<
    (Omit<TriggerPreannotationPayload, "task_ids" | "batch_id"> & { task_ids: string[] }) | null
  >(null);
  const exportKeyRef = useRef<{ fingerprint: string; key: string } | null>(null);
  const preannotateKeyRef = useRef<{ fingerprint: string; key: string } | null>(null);
  const scopeRef = useRef<string>(projectId);

  const dataType = projectQ.data?.data_type ?? "image";
  const availableExportTargets = useMemo(() => exportTargetsForDataType(dataType), [dataType]);
  const backends = useMemo(
    () => (backendsQ.data ?? []).map((backend) => ({ id: backend.id, name: backend.name })),
    [backendsQ.data],
  );
  const firstBackendId = backends[0]?.id ?? null;
  useEffect(() => {
    setSelectedBackendId(projectQ.data?.ml_backend_id ?? firstBackendId);
  }, [firstBackendId, projectQ.data?.ml_backend_id, projectId]);

  const cfg = usePreannotateConfig({
    projectId,
    backendId: selectedBackendId,
    executionUnit: dataType === "video" ? "video" : undefined,
  });
  const exportJobQ = useAsyncJob(exportJobId, true);
  const preannotateJobQ = useAsyncJob(preannotateJobId, true);
  const invalidatedJobIdsRef = useRef(new Set<string>());
  useEffect(() => {
    invalidatedJobIdsRef.current.clear();
  }, [projectId]);
  useEffect(() => {
    const completedJobs = [
      exportJobQ.data?.status === "completed" ? exportJobId : null,
      preannotateJobQ.data?.status === "completed" ? preannotateJobId : null,
    ].filter((jobId): jobId is string => !!jobId);
    const newCompletedJobs = completedJobs.filter(
      (jobId) => !invalidatedJobIdsRef.current.has(jobId),
    );
    if (!newCompletedJobs.length) return;
    newCompletedJobs.forEach((jobId) => invalidatedJobIdsRef.current.add(jobId));
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["project-task-query", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["data-manager-summary", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["data-manager-matches", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["data-manager-objects", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["data-manager-tracks", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["task-views", projectId] }),
      queryClient.invalidateQueries({
        predicate: ({ queryKey }) =>
          queryKey[0] === "project-performance" && queryKey.includes(projectId),
      }),
    ]);
  }, [
    exportJobId,
    exportJobQ.data?.status,
    preannotateJobId,
    preannotateJobQ.data?.status,
    projectId,
    queryClient,
  ]);

  const tooMany = ids.length > MAX_TASK_IDS;
  const hasSelection = ids.length > 0 && !tooMany;
  const hasAssignmentChange = annotatorChoice !== KEEP || reviewerChoice !== KEEP;
  const selectionFingerprint = ids.join(",");
  const scopeToken = `${projectId}:${selectionFingerprint}`;
  scopeRef.current = scopeToken;
  useEffect(() => {
    setAssignmentPreview(null);
    if (selectionFingerprint) setAssignmentResult(null);
    setActionError(null);
    setBusy(null);
  }, [projectId, selectionFingerprint]);
  useEffect(() => {
    setExportJobId(null);
    setPreannotateJobId(null);
    exportKeyRef.current = null;
    preannotateKeyRef.current = null;
    exportJobRequestRef.current = null;
    preannotateJobRequestRef.current = null;
  }, [projectId]);
  const memberById = useMemo(
    () =>
      new Map(
        members.map((member) => [
          member.user_id,
          member.user_name || member.user_email || member.user_id,
        ]),
      ),
    [members],
  );
  const annotators = useMemo(
    () => members.filter((member) => member.role === "annotator"),
    [members],
  );
  const reviewers = useMemo(
    () => members.filter((member) => member.role === "reviewer"),
    [members],
  );

  const assignmentPayload = useMemo(() => {
    const payload: {
      task_ids: string[];
      annotator_id?: string | null;
      reviewer_id?: string | null;
    } = { task_ids: ids };
    if (annotatorChoice !== KEEP)
      payload.annotator_id = annotatorChoice === CLEAR ? null : annotatorChoice;
    if (reviewerChoice !== KEEP)
      payload.reviewer_id = reviewerChoice === CLEAR ? null : reviewerChoice;
    return payload;
  }, [annotatorChoice, ids, reviewerChoice]);

  const closeAssignment = () => {
    if (busy) return;
    setAssignmentOpen(false);
    setAssignmentPreview(null);
    setActionError(null);
  };

  const openAssignment = () => {
    setAssignmentPreview(null);
    setAssignmentResult(null);
    setActionError(null);
    setAssignmentOpen(true);
  };

  const openExport = () => {
    const allowed = new Set(availableExportTargets.map((option) => option.id));
    const initial = exportOptions.targets.filter((target) => allowed.has(target));
    setSelectedExportTargets(initial.length ? initial : [availableExportTargets[0].id]);
    setActionError(null);
    setExportOpen(true);
  };

  const runPreannotate = async (
    configured: Omit<TriggerPreannotationPayload, "task_ids" | "batch_id">,
    capturedTaskIds?: string[],
  ) => {
    const requestScope = scopeRef.current;
    const payload = structuredClone({
      ...configured,
      task_ids: capturedTaskIds ?? ids,
      predict_mode: configured.predict_mode ?? "skip_predicted",
    });
    setBusy("preannotate");
    setActionError(null);
    const actionOptions: DataManagerActionRequestOptions = {
      idempotencyKey: actionKeyFor(
        preannotateKeyRef,
        "data-manager-preannotate",
        JSON.stringify(payload),
      ),
    };
    try {
      const result = await dataManagerTaskActionsApi.preannotate(projectId, payload, actionOptions);
      if (scopeRef.current !== requestScope) return;
      preannotateKeyRef.current = null;
      preannotateJobRequestRef.current = payload;
      setPreannotateJobId(result.job_id);
      setPreannotateOpen(false);
      if (!capturedTaskIds) onCompleted?.();
    } catch (error) {
      if (scopeRef.current !== requestScope) return;
      const message = error instanceof Error ? error.message : "";
      setActionError(
        message.includes("must belong to active or unassigned draft batches")
          ? "所选任务的批次已进入人工流程或已分派人员，不能运行预标。请改选活动批次或未分派人员的草稿批次中的待标注任务。"
          : message
            ? `无法创建预标注任务：${message}`
            : "无法创建预标注任务，请检查任务状态和模型能力",
      );
    } finally {
      if (scopeRef.current === requestScope) setBusy(null);
    }
  };

  const openPreannotate = () => {
    setActionError(null);
    if (preannotation) {
      void runPreannotate(preannotation);
    } else {
      setPreannotateOpen(true);
    }
  };

  const previewAssignment = async () => {
    const requestScope = scopeRef.current;
    setBusy("preview");
    setActionError(null);
    try {
      const result = await dataManagerTaskActionsApi.assignmentPreview(
        projectId,
        assignmentPayload,
      );
      if (scopeRef.current !== requestScope) return;
      setAssignmentPreview(result);
      setAssignmentResult(null);
    } catch {
      if (scopeRef.current !== requestScope) return;
      setActionError("无法生成分派预览，请刷新任务后重试");
    } finally {
      if (scopeRef.current === requestScope) setBusy(null);
    }
  };

  const applyAssignment = async () => {
    if (!assignmentPreview) return;
    const requestScope = scopeRef.current;
    setBusy("apply");
    setActionError(null);
    try {
      const result = await dataManagerTaskActionsApi.assignmentApply(projectId, {
        ...assignmentPayload,
        preview_version: assignmentPreview.preview_version,
      });
      if (scopeRef.current !== requestScope) return;
      setAssignmentResult(result);
      setAssignmentPreview(null);
      setAssignmentOpen(false);
      onCompleted?.();
    } catch {
      if (scopeRef.current !== requestScope) return;
      setActionError("分派预览已过期或任务状态已变化，请重新预览");
      setAssignmentPreview(null);
    } finally {
      if (scopeRef.current === requestScope) setBusy(null);
    }
  };

  const runExport = async (capturedPayload?: DataManagerTaskExportPayload) => {
    if (!capturedPayload && !selectedExportTargets.length) return;
    const requestScope = scopeRef.current;
    setBusy("export");
    setActionError(null);
    const options: DataManagerTaskExportOptions = {
      ...exportOptions,
      targets: selectedExportTargets,
    };
    const payload = structuredClone(capturedPayload ?? toExportPayload(ids, options));
    const actionOptions: DataManagerActionRequestOptions = {
      idempotencyKey: actionKeyFor(exportKeyRef, "data-manager-export", JSON.stringify(payload)),
    };
    try {
      const result = await dataManagerTaskActionsApi.exportTasks(projectId, payload, actionOptions);
      if (scopeRef.current !== requestScope) return;
      exportKeyRef.current = null;
      exportJobRequestRef.current = payload;
      setExportJobId(result.job_id);
      setExportOpen(false);
      if (!capturedPayload) onCompleted?.();
    } catch {
      if (scopeRef.current !== requestScope) return;
      setActionError("无法创建导出任务，请检查所选格式和任务范围");
    } finally {
      if (scopeRef.current === requestScope) setBusy(null);
    }
  };

  return (
    <>
      <div
        data-testid="data-manager-task-actions"
        className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-3 py-2"
      >
        <Badge variant={hasSelection ? "accent" : "outline"}>已选 {ids.length} 个任务</Badge>
        {tooMany && <span className="text-xs text-status-danger">最多支持 200 个任务</span>}
        <div className="ml-auto flex flex-wrap gap-2">
          <Button
            data-testid="data-manager-assign"
            size="sm"
            disabled={!hasSelection || busy !== null || !canManage}
            title={canManage ? undefined : "需要项目成员管理权限"}
            onClick={openAssignment}
          >
            分派
          </Button>
          <Button
            data-testid="data-manager-export"
            size="sm"
            disabled={!hasSelection || busy !== null || !canExport}
            title={canExport ? undefined : "需要项目导出权限"}
            onClick={openExport}
          >
            导出
          </Button>
          <Button
            data-testid="data-manager-preannotate"
            size="sm"
            variant="ai"
            disabled={!hasSelection || busy !== null || !canManage}
            title={canManage ? undefined : "需要项目成员管理权限"}
            onClick={openPreannotate}
          >
            {busy === "preannotate" ? "创建预标…" : "运行预标"}
          </Button>
        </div>
        {assignmentResult && (
          <div role="status" className="basis-full flex flex-wrap items-center gap-2 text-xs">
            <Badge variant="success">已更新 {assignmentResult.succeeded.length} 个任务</Badge>
            {assignmentResult.failed_count > 0 && (
              <span className="text-status-danger">失败 {assignmentResult.failed_count}</span>
            )}
            {assignmentResult.skipped_count > 0 && (
              <span className="text-muted-foreground">未变化 {assignmentResult.skipped_count}</span>
            )}
            <span className="text-muted-foreground">分派结果已保存</span>
          </div>
        )}
        {actionError && !assignmentOpen && !exportOpen && !preannotateOpen && (
          <div role="alert" className="basis-full text-xs text-status-danger">
            {actionError}
          </div>
        )}
        {(exportJobId || preannotateJobId) && (
          <div className="basis-full flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            {exportJobId && (
              <ActionJobStatus
                label="导出作业"
                jobId={exportJobId}
                taskIds={exportJobRequestRef.current?.task_ids ?? []}
                query={exportJobQ}
                busy={busy !== null}
                onRerun={() => {
                  if (exportJobRequestRef.current) void runExport(exportJobRequestRef.current);
                }}
              />
            )}
            {preannotateJobId && (
              <ActionJobStatus
                label="预标作业"
                jobId={preannotateJobId}
                taskIds={preannotateJobRequestRef.current?.task_ids ?? []}
                query={preannotateJobQ}
                busy={busy !== null}
                onRerun={() => {
                  const request = preannotateJobRequestRef.current;
                  if (request) void runPreannotate(request, request.task_ids);
                }}
              />
            )}
          </div>
        )}
      </div>

      <Modal
        open={assignmentOpen}
        onClose={closeAssignment}
        title={`分派已选任务（${ids.length}）`}
      >
        <div className="flex flex-col gap-4">
          <ActionError message={actionError} />
          {!assignmentPreview ? (
            <div className="flex flex-col gap-4">
              <p className="text-sm text-muted-foreground">
                只会更新这组任务；已完成、锁定或状态不适用的任务会在预览中标出。
              </p>
              <AssignmentSelect
                label="标注员"
                value={annotatorChoice}
                members={annotators}
                loading={membersLoading}
                onChange={setAnnotatorChoice}
              />
              <AssignmentSelect
                label="审核员"
                value={reviewerChoice}
                members={reviewers}
                loading={membersLoading}
                onChange={setReviewerChoice}
              />
              <div className="flex justify-end gap-2">
                <Button onClick={closeAssignment}>取消</Button>
                <Button
                  variant="primary"
                  disabled={!hasAssignmentChange || !hasSelection || busy !== null}
                  onClick={previewAssignment}
                >
                  {busy === "preview" ? "预览中…" : "预览分派"}
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap gap-2 text-sm">
                <Badge variant="accent">可更新 {assignmentPreview.eligible_count}</Badge>
                <Badge variant="outline">未变化 {assignmentPreview.skipped_count}</Badge>
                <Badge variant={assignmentPreview.failed_count ? "warning" : "outline"}>
                  不可用 {assignmentPreview.failed_count}
                </Badge>
              </div>
              <ul className="max-h-64 overflow-y-auto rounded-md border border-border bg-muted p-2 text-xs">
                {assignmentPreview.items.map((item) => {
                  const changes = assignmentChanges(item, memberById);
                  return (
                    <li
                      key={item.task_id}
                      className="flex items-start justify-between gap-3 border-b border-border py-2 last:border-b-0"
                    >
                      <span className="mono shrink-0">
                        {item.task_display_id ?? item.task_id.slice(0, 8)}
                      </span>
                      <span className="flex flex-col items-end gap-0.5 text-right">
                        <span
                          className={
                            item.will_change ? "text-status-positive" : "text-muted-foreground"
                          }
                        >
                          {item.will_change ? "将更新" : reasonLabel(item.reason)}
                        </span>
                        {changes.map((change) => (
                          <span key={change} className="text-muted-foreground">
                            {change}
                          </span>
                        ))}
                      </span>
                    </li>
                  );
                })}
              </ul>
              <div className="flex justify-end gap-2">
                <Button
                  onClick={() => {
                    setAssignmentPreview(null);
                    setActionError(null);
                  }}
                  disabled={busy !== null}
                >
                  返回修改
                </Button>
                <Button
                  variant="primary"
                  disabled={!assignmentPreview.eligible_count || busy !== null}
                  onClick={applyAssignment}
                >
                  {busy === "apply" ? "应用中…" : `应用 ${assignmentPreview.eligible_count} 个任务`}
                </Button>
              </div>
            </div>
          )}
        </div>
      </Modal>

      <Modal open={exportOpen} onClose={() => !busy && setExportOpen(false)} title="导出已选任务">
        <div className="flex flex-col gap-4">
          <ActionError message={actionError} />
          <p className="text-sm text-muted-foreground">
            选择导出格式。导出作业只读取这 {ids.length} 个任务，完成后可在任务铃中下载。
          </p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {availableExportTargets.map((option) => (
              <label
                key={option.id}
                className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
              >
                <input
                  type="checkbox"
                  checked={selectedExportTargets.includes(option.id)}
                  onChange={() => {
                    setSelectedExportTargets((current) =>
                      current.includes(option.id)
                        ? current.filter((target) => target !== option.id)
                        : [...current, option.id],
                    );
                    exportKeyRef.current = null;
                  }}
                />
                {option.label}
              </label>
            ))}
          </div>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setExportOpen(false)} disabled={busy !== null}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={!selectedExportTargets.length || busy !== null}
              onClick={() => void runExport()}
            >
              {busy === "export" ? "创建导出…" : "创建导出作业"}
            </Button>
          </div>
        </div>
      </Modal>

      <Modal
        open={preannotateOpen}
        onClose={() => !busy && setPreannotateOpen(false)}
        title={`预标注已选任务（${ids.length}）`}
        width={680}
      >
        <div className="flex flex-col gap-4">
          <ActionError message={actionError} />
          <p className="text-sm text-muted-foreground">
            使用项目当前启用的 ML Backend 和预标注配置；运行范围固定为当前选择。
          </p>
          <PreannotateConfigForm
            cfg={cfg}
            backends={backends}
            selectedBackendId={selectedBackendId}
            onSelectBackend={setSelectedBackendId}
            projectMlBackendId={projectQ.data?.ml_backend_id}
          />
          <div className="flex justify-end gap-2">
            <Button onClick={() => setPreannotateOpen(false)} disabled={busy !== null}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={
                !hasSelection || !cfg.configReady || !!cfg.sourceBatchableWarning || busy !== null
              }
              onClick={() => {
                const configured = cfg.buildArgs("skip_predicted");
                if (configured) void runPreannotate(configured);
              }}
            >
              {busy === "preannotate" ? "创建预标…" : "创建预标作业"}
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
}

function AssignmentSelect({
  label,
  value,
  members,
  loading,
  onChange,
}: {
  label: string;
  value: AssignmentChoice;
  members: Array<{
    user_id: string;
    user_name: string;
    user_email: string;
    /** 头像引用；结构上是 ProjectMemberResponse 的子集，缺省 = 回退首字母。 */
    avatar_ref?: string | null;
  }>;
  loading: boolean;
  onChange: (value: AssignmentChoice) => void;
}) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      {label}
      <select
        className="h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground"
        value={value}
        disabled={loading}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value={KEEP}>保留不变</option>
        <option value={CLEAR}>恢复批次默认（未分批则清空）</option>
        {members.map((member) => (
          <option key={member.user_id} value={member.user_id}>
            {member.user_name} · {member.user_email}
          </option>
        ))}
      </select>
      {value !== KEEP && value !== CLEAR && (
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <UserAvatar
            size="sm"
            user={{
              name: members.find((member) => member.user_id === value)?.user_name,
              email: members.find((member) => member.user_id === value)?.user_email,
              avatar_ref: members.find((member) => member.user_id === value)?.avatar_ref,
            }}
          />
          目标成员将在应用前再次校验
        </span>
      )}
    </label>
  );
}
