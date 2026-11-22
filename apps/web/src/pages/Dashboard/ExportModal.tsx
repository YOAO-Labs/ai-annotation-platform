import { useEffect, useState } from "react";
import {
  projectsApi,
  type ExportOptions,
  type ExportTarget,
  type LidarExportPreflight,
  type VideoExportScope,
  type VideoFrameMode,
} from "@/api/projects";
import { maskFormatsApi, type MaskFormatExportPreflight } from "@/api/maskFormats";
import { tasksApi } from "@/api/tasks";
import { videoTrackerApi, type VideoSegment } from "@/api/videoTracker";
import type { TaskResponse } from "@/types";
import { Modal } from "@/components/ui/Modal";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";

const OPTION_BUTTON_CLASS =
  "flex min-w-0 cursor-pointer appearance-none flex-row items-start gap-2.5 rounded-md border border-border bg-muted px-3 py-2.5 text-left text-xs font-normal leading-snug text-foreground hover:border-border hover:bg-muted";
const OPTION_BUTTON_ACTIVE_CLASS = "border-brand bg-brand/10 font-semibold text-brand";
const OPTION_LABEL_CLASS = "font-semibold text-inherit";

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

interface TargetOption {
  value: ExportTarget;
  label: string;
  description: string;
  disabled?: boolean;
}

type ImageOption =
  | ({ kind: "single" } & TargetOption)
  | { kind: "group"; label: string; description: string; members: TargetOption[] };

const IMAGE_OPTIONS: ImageOption[] = [
  {
    kind: "single",
    value: "coco",
    label: "COCO",
    description: "通用检测 / 分割 / 关键点，单 annotations.json。",
  },
  {
    kind: "group",
    label: "YOLO",
    description: "按标注几何选变体，可多选。",
    members: [
      { value: "yolo-det", label: "检测", description: "导出矩形框，用于 YOLOv8 检测。" },
      { value: "yolo-obb", label: "旋转框", description: "导出旋转框的四个角点，用于 OBB。" },
      { value: "yolo-seg", label: "分割", description: "将多边形和 Mask 导出为归一化多边形。" },
    ],
  },
  {
    kind: "single",
    value: "native_json",
    label: "原生 JSON",
    description: "分别保存预测候选和正式标注，并保留项目配置。",
  },
  {
    kind: "single",
    value: "label-studio-brush",
    label: "Label Studio Brush",
    description: "BrushLabels 官方 RLE，可被 Label Studio 直接消费。",
  },
  {
    kind: "single",
    value: "binary-png",
    label: "逐实例 Binary PNG",
    description: "每个实例独立 0/255 PNG，无损保留重叠。",
  },
  {
    kind: "single",
    value: "indexed-png",
    label: "Indexed PNG",
    description: "每张图用一张索引 PNG 区分实例；重叠时需选择处理方式。",
  },
];

const VIDEO_OPTIONS: TargetOption[] = [
  {
    value: "video_json",
    label: "Video JSON",
    description: "平台视频轨迹 JSON，可选关键帧或展开所有帧。",
  },
  {
    value: "yolo-frames-det",
    label: "YOLO 逐帧",
    description: "按采样间隔抽帧，导出检测训练标注。",
  },
  {
    value: "yolo-frames-seg",
    label: "YOLO 逐帧分割",
    description: "按采样间隔抽帧，仅支持多边形；其他形状需调整导出格式。",
  },
  {
    value: "coco-frames-seg",
    label: "COCO 逐帧分割",
    description: "按采样间隔抽帧，支持多边形和 Mask；其他形状需调整导出格式。",
  },
  {
    value: "davis",
    label: "DAVIS Mask",
    description: "按采样间隔导出原尺寸索引 PNG；同一视频内对象编号稳定。",
  },
  {
    value: "youtube-vos",
    label: "YouTube-VOS",
    description: "只导出 Mask 关键帧；导入时需选择未标注帧的处理方式。",
  },
  {
    value: "mots",
    label: "MOTS",
    description: "逐帧导出分割数据，并记录类别、轨迹与帧号的对应关系。",
  },
  {
    value: "native_json",
    label: "原生 JSON",
    description: "保留视频轨迹的受支持标注形状和项目配置。",
  },
  { value: "mot", label: "MOT", description: "MOT 16/17/20 跟踪评测格式，按采样网格重排帧号。" },
  { value: "kitti", label: "KITTI", description: "KITTI Tracking 2D labels，适配 KITTI 工具链。" },
];

const LIDAR_OPTIONS: TargetOption[] = [
  {
    value: "native_json",
    label: "原生 JSON",
    description: "YOAO 原生交换格式，保留 3D 几何与项目配置。",
  },
  {
    value: "kitti",
    label: "KITTI 3D",
    description: "逐帧 label_2 + calib，输出 KITTI camera 坐标。",
  },
  {
    value: "coco-multicamera",
    label: "Multi-camera COCO",
    description: "合并全部相机的持久化人工 2D 框，不使用 3D 投影补齐。",
  },
  {
    value: "nuscenes",
    label: "nuScenes",
    description: "官方 13 表 + 原始媒体清单，仅允许完整可信的 nuScenes Scene。",
  },
  {
    value: "pointmask",
    label: "Point Mask",
    description: "逐点 uint32 label + 类别映射，适配 3D 分割训练前处理。",
  },
];

const FRAME_MODES: { value: VideoFrameMode; label: string; description: string }[] = [
  { value: "keyframes", label: "关键帧", description: "只导出人工 / 预测关键帧。" },
  { value: "all_frames", label: "所有帧", description: "按相邻有效关键帧线性插值展开。" },
];

export function ExportModal({
  open,
  onClose,
  projectId,
  projectDataType,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  projectDataType?: string;
}) {
  return (
    <Modal open={open} title="导出标注数据" width={520} onClose={onClose}>
      <ExportForm
        projectId={projectId}
        projectDataType={projectDataType}
        onDone={onClose}
        onCancel={onClose}
      />
    </Modal>
  );
}

function CheckCard({
  option,
  active,
  onToggle,
}: {
  option: TargetOption;
  active: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={option.disabled}
      aria-pressed={active}
      className={cn(
        OPTION_BUTTON_CLASS,
        active && OPTION_BUTTON_ACTIVE_CLASS,
        option.disabled && "cursor-not-allowed opacity-55",
      )}
    >
      <span
        className={cn(
          "mt-px flex size-4 flex-shrink-0 items-center justify-center rounded-sm border-[1.5px] border-border bg-card",
          active && "border-brand bg-brand text-card",
        )}
        aria-hidden
      >
        {active && <Icon name="check" size={12} />}
      </span>
      <span className="flex min-w-0 flex-col gap-1">
        <span className={OPTION_LABEL_CLASS}>{option.label}</span>
        <span className="text-xs font-normal text-muted-foreground">{option.description}</span>
      </span>
    </button>
  );
}

function ExportForm({
  projectId,
  projectDataType,
  onDone,
  onCancel,
}: {
  projectId: string;
  projectDataType?: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const isVideoProject = projectDataType === "video";
  const isLidarProject = projectDataType === "lidar";
  const [targets, setTargets] = useState<ExportTarget[]>(
    isVideoProject ? ["video_json"] : isLidarProject ? ["native_json"] : ["coco"],
  );
  const [yoloExpanded, setYoloExpanded] = useState(false);
  const [includeAttributes, setIncludeAttributes] = useState(true);
  const [videoFrameMode, setVideoFrameMode] = useState<VideoFrameMode>("keyframes");
  const [indexedOverlapPolicy, setIndexedOverlapPolicy] = useState<
    "error" | "z_order" | "larger_area" | "smaller_area"
  >("error");
  const [videoOverlapPolicy, setVideoOverlapPolicy] = useState<
    "error" | "z_order" | "larger_area" | "smaller_area"
  >("error");
  const [motsFrameBase, setMotsFrameBase] = useState<0 | 1>(0);
  const [scopeMode, setScopeMode] = useState<"project" | "task">("project");
  const [rangeKind, setRangeKind] = useState<"segments" | "frames">("segments");
  const [videoTasks, setVideoTasks] = useState<TaskResponse[]>([]);
  const [taskCursor, setTaskCursor] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [segments, setSegments] = useState<VideoSegment[]>([]);
  const [startSegmentId, setStartSegmentId] = useState("");
  const [endSegmentId, setEndSegmentId] = useState("");
  const [frameFrom, setFrameFrom] = useState("0");
  const [frameTo, setFrameTo] = useState("");
  const [scopeLoading, setScopeLoading] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preflight, setPreflight] = useState<MaskFormatExportPreflight | null>(null);
  const [lidarPreflight, setLidarPreflight] = useState<LidarExportPreflight | null>(null);
  const [lidarPreflightLoading, setLidarPreflightLoading] = useState(false);
  const [kittiCameraRole, setKittiCameraRole] = useState("");
  const [lossyConfirmed, setLossyConfirmed] = useState(false);
  const pushToast = useToastStore((s) => s.push);

  const showFrameMode = isVideoProject && targets.includes("video_json");

  useEffect(() => {
    if (!isVideoProject || scopeMode !== "task") return;
    let cancelled = false;
    setScopeLoading(true);
    setScopeError(null);
    tasksApi
      .listByProject(projectId, { limit: 100 })
      .then((page) => {
        if (cancelled) return;
        setVideoTasks(page.items);
        setTaskCursor(page.next_cursor ?? null);
        setSelectedTaskId((current) => current || page.items[0]?.id || "");
      })
      .catch((error) => {
        if (!cancelled) setScopeError(error instanceof Error ? error.message : "视频任务加载失败");
      })
      .finally(() => {
        if (!cancelled) setScopeLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isVideoProject, projectId, scopeMode]);

  useEffect(() => {
    if (!selectedTaskId || scopeMode !== "task") {
      setSegments([]);
      return;
    }
    let cancelled = false;
    setScopeLoading(true);
    setScopeError(null);
    videoTrackerApi
      .segments(selectedTaskId)
      .then((response) => {
        if (cancelled) return;
        setSegments(response.segments);
        const first = response.segments[0];
        const last = response.segments[response.segments.length - 1];
        setStartSegmentId(first?.id ?? "");
        setEndSegmentId(first?.id ?? "");
        setFrameFrom("0");
        setFrameTo(last ? String(last.end_frame) : "");
      })
      .catch((error) => {
        if (!cancelled) setScopeError(error instanceof Error ? error.message : "视频分段加载失败");
      })
      .finally(() => {
        if (!cancelled) setScopeLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scopeMode, selectedTaskId]);

  useEffect(() => {
    if (
      !isLidarProject ||
      (!targets.includes("coco-multicamera") &&
        !targets.includes("kitti") &&
        !targets.includes("nuscenes"))
    ) {
      setLidarPreflight(null);
      setLidarPreflightLoading(false);
      return;
    }
    let cancelled = false;
    setLidarPreflightLoading(true);
    projectsApi
      .preflightLidarExport(projectId, targets, kittiCameraRole ? { kittiCameraRole } : undefined)
      .then((report) => {
        if (!cancelled) setLidarPreflight(report);
      })
      .catch((error) => {
        if (cancelled) return;
        setLidarPreflight({
          ready: false,
          camera_roles: [],
          selected_camera_role: kittiCameraRole || null,
          checked_tasks: 0,
          issue_count: 1,
          issues_truncated: false,
          issues: [
            {
              code: "preflight_request_failed",
              message: error instanceof Error ? error.message : "LiDAR 导出预检失败",
              task_id: null,
              task_display_id: null,
              frame_key: null,
              camera_role: null,
            },
          ],
        });
      })
      .finally(() => {
        if (!cancelled) setLidarPreflightLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isLidarProject, kittiCameraRole, projectId, targets]);

  const startSegment = segments.find((segment) => segment.id === startSegmentId);
  const endSegment = segments.find((segment) => segment.id === endSegmentId);
  const maxFrame = segments.length > 0 ? segments[segments.length - 1].end_frame : -1;
  const parsedFrom = Number(frameFrom);
  const parsedTo = Number(frameTo);
  const frameRangeValid =
    frameFrom !== "" &&
    frameTo !== "" &&
    Number.isInteger(parsedFrom) &&
    Number.isInteger(parsedTo) &&
    parsedFrom >= 0 &&
    parsedFrom <= parsedTo &&
    parsedTo <= maxFrame;
  const segmentRangeValid =
    !!startSegment && !!endSegment && startSegment.segment_index <= endSegment.segment_index;
  const scopeValid =
    scopeMode === "project" ||
    (!!selectedTaskId &&
      (rangeKind === "segments" ? segmentRangeValid : frameRangeValid) &&
      !scopeLoading);
  const videoExportScope: VideoExportScope | undefined =
    scopeMode !== "task" || !scopeValid
      ? undefined
      : {
          task_id: selectedTaskId,
          selection:
            rangeKind === "segments"
              ? {
                  kind: "segments",
                  start_segment_id: startSegmentId,
                  end_segment_id: endSegmentId,
                }
              : {
                  kind: "frames",
                  from_frame: parsedFrom,
                  to_frame: parsedTo,
                },
        };

  useEffect(() => {
    setPreflight(null);
    setLossyConfirmed(false);
  }, [
    includeAttributes,
    indexedOverlapPolicy,
    motsFrameBase,
    targets,
    videoFrameMode,
    videoOverlapPolicy,
    scopeMode,
    rangeKind,
    selectedTaskId,
    startSegmentId,
    endSegmentId,
    frameFrom,
    frameTo,
  ]);

  const loadMoreTasks = async () => {
    if (!taskCursor || scopeLoading) return;
    setScopeLoading(true);
    try {
      const page = await tasksApi.listByProject(projectId, { limit: 100, cursor: taskCursor });
      setVideoTasks((current) => [...current, ...page.items]);
      setTaskCursor(page.next_cursor ?? null);
    } catch (error) {
      setScopeError(error instanceof Error ? error.message : "视频任务加载失败");
    } finally {
      setScopeLoading(false);
    }
  };

  const toggleTarget = (value: ExportTarget) => {
    setTargets((prev) =>
      prev.includes(value) ? prev.filter((t) => t !== value) : [...prev, value],
    );
  };

  const handleExport = async () => {
    if (targets.length === 0 || !scopeValid) return;
    setBusy(true);
    try {
      const options: ExportOptions = {
        includeAttributes,
        ...(showFrameMode ? { videoFrameMode } : {}),
        ...(targets.includes("indexed-png") ? { indexedOverlapPolicy } : {}),
        ...(targets.some((target) => target === "davis" || target === "youtube-vos")
          ? { videoOverlapPolicy }
          : {}),
        ...(targets.includes("mots") ? { motsFrameBase } : {}),
        ...(videoExportScope ? { scope: videoExportScope } : {}),
        ...(isLidarProject && targets.includes("kitti") ? { lidar: { kittiCameraRole } } : {}),
      };
      if (isLidarProject) {
        const checked = await projectsApi.preflightLidarExport(projectId, targets, options.lidar);
        setLidarPreflight(checked);
        if (!checked.ready) {
          pushToast({
            msg: "LiDAR 导出预检未通过",
            sub: "请按问题清单补齐来源、Scene、位姿、媒体或标定合同",
            kind: "warning",
          });
          return;
        }
      } else {
        const checked =
          preflight ?? (await maskFormatsApi.preflightExport(projectId, targets, options));
        if (!preflight) setPreflight(checked);
        if (checked.loss_class === "unsupported") {
          pushToast({
            msg: "当前导出计划包含不支持的标注",
            sub: "请查看预检报告并调整格式或项目内容",
            kind: "warning",
          });
          return;
        }
        if (checked.loss_class === "lossy" && !lossyConfirmed) return;
      }
      await projectsApi.exportProject(projectId, targets, {
        ...options,
      });
      pushToast({
        msg: "导出已入队",
        sub: "可在右上角任务铃查看进度并下载",
        kind: "success",
      });
      onDone();
    } catch (err) {
      pushToast({
        msg: "导出发起失败",
        sub: err instanceof Error ? err.message : undefined,
        kind: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <div className="text-xs font-semibold text-foreground">导出目标（可多选）</div>
        <div className="flex flex-col gap-2">
          {isVideoProject || isLidarProject
            ? (isVideoProject ? VIDEO_OPTIONS : LIDAR_OPTIONS).map((o) => (
                <CheckCard
                  key={o.value}
                  option={o}
                  active={targets.includes(o.value)}
                  onToggle={() => toggleTarget(o.value)}
                />
              ))
            : IMAGE_OPTIONS.map((o) =>
                o.kind === "single" ? (
                  <CheckCard
                    key={o.value}
                    option={o}
                    active={targets.includes(o.value)}
                    onToggle={() => toggleTarget(o.value)}
                  />
                ) : (
                  <div
                    key={o.label}
                    className="overflow-hidden rounded-md border border-border bg-muted"
                  >
                    <button
                      type="button"
                      onClick={() => setYoloExpanded((v) => !v)}
                      aria-expanded={yoloExpanded}
                      className="flex w-full cursor-pointer appearance-none items-center gap-2 border-0 bg-transparent px-3 py-2.5 text-left text-xs text-foreground hover:bg-muted"
                    >
                      <Icon name={yoloExpanded ? "chevDown" : "chevRight"} size={14} />
                      <span className={OPTION_LABEL_CLASS}>{o.label}</span>
                      {(() => {
                        const n = o.members.filter((m) => targets.includes(m.value)).length;
                        return n > 0 ? (
                          <span className="rounded-full bg-brand/10 px-1.5 py-px text-2xs font-semibold text-brand">
                            {n}/{o.members.length}
                          </span>
                        ) : null;
                      })()}
                      <span className="text-xs font-normal text-muted-foreground">
                        {o.description}
                      </span>
                    </button>
                    {yoloExpanded && (
                      <div className="flex flex-col gap-1.5 px-2.5 pb-2.5">
                        {o.members.map((m) => (
                          <CheckCard
                            key={m.value}
                            option={m}
                            active={targets.includes(m.value)}
                            onToggle={() => toggleTarget(m.value)}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                ),
              )}
        </div>
        <div className="text-xs leading-snug text-muted-foreground">
          {targets.length === 0 ? (
            <span className="text-status-caution">请至少选择一个导出目标</span>
          ) : targets.length === 1 ? (
            "将导出 1 个目标，产出单个压缩包。"
          ) : (
            `已选 ${targets.length} 个目标 → 打包为 1 个 zip（各目标分子目录）。`
          )}
        </div>
      </div>
      {isLidarProject &&
        (targets.includes("coco-multicamera") ||
          targets.includes("kitti") ||
          targets.includes("nuscenes")) && (
          <div className="flex flex-col gap-2 rounded-md border border-border bg-muted px-3 py-2.5">
            {targets.includes("kitti") && (
              <>
                <label
                  htmlFor="kitti-camera-role"
                  className="text-xs font-semibold text-foreground"
                >
                  KITTI 投影相机
                </label>
                <select
                  id="kitti-camera-role"
                  value={kittiCameraRole}
                  onChange={(event) => setKittiCameraRole(event.target.value)}
                  className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
                >
                  <option value="" disabled hidden>
                    请选择相机通道
                  </option>
                  {kittiCameraRole && !lidarPreflight?.camera_roles.includes(kittiCameraRole) && (
                    <option value={kittiCameraRole} disabled hidden>
                      已选相机通道（正在核对可用状态）
                    </option>
                  )}
                  {(lidarPreflight?.camera_roles ?? []).map((role) => (
                    <option key={role} value={role}>
                      {role.replace(/^camera_/, "")}
                    </option>
                  ))}
                </select>
              </>
            )}
            {lidarPreflightLoading && (
              <div className="text-xs text-muted-foreground">正在核对全部点云帧…</div>
            )}
            {lidarPreflight && !lidarPreflightLoading && (
              <div
                className={cn(
                  "flex flex-col gap-1.5 rounded-sm border px-2.5 py-2 text-xs",
                  lidarPreflight.ready
                    ? "border-status-success/40 bg-status-success/10"
                    : "border-status-danger/40 bg-status-danger/10",
                )}
                data-testid="lidar-export-preflight"
              >
                <div className="font-semibold text-foreground">
                  {lidarPreflight.ready
                    ? `预检通过 · ${lidarPreflight.checked_tasks} 帧`
                    : `预检阻止 · ${lidarPreflight.issue_count} 个问题`}
                </div>
                {lidarPreflight.issues.slice(0, 8).map((issue, index) => (
                  <div
                    key={`${issue.code}-${issue.task_id ?? "global"}-${index}`}
                    className="text-muted-foreground"
                  >
                    <code className="text-foreground">{issue.code}</code>
                    {issue.task_display_id || issue.frame_key
                      ? ` · ${issue.task_display_id ?? issue.frame_key}`
                      : ""}
                    {` · ${issue.message}`}
                  </div>
                ))}
                {(lidarPreflight.issue_count > 8 || lidarPreflight.issues_truncated) && (
                  <div className="text-muted-foreground">
                    仅显示前 8 项；预检共发现 {lidarPreflight.issue_count} 项。
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      {isVideoProject && (
        <div className="flex flex-col gap-2 rounded-md border border-border bg-muted px-3 py-2.5">
          <label htmlFor="video-export-scope" className="text-xs font-semibold text-foreground">
            导出范围
          </label>
          <select
            id="video-export-scope"
            value={scopeMode}
            onChange={(event) => setScopeMode(event.target.value as typeof scopeMode)}
            className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
          >
            <option value="project">整个项目</option>
            <option value="task">单个视频范围</option>
          </select>
          {scopeMode === "task" && (
            <div className="flex flex-col gap-2" data-testid="video-export-range-fields">
              <label htmlFor="video-export-task" className="text-xs text-muted-foreground">
                视频任务
              </label>
              <select
                id="video-export-task"
                value={selectedTaskId}
                onChange={(event) => {
                  setSelectedTaskId(event.target.value);
                  setSegments([]);
                  setStartSegmentId("");
                  setEndSegmentId("");
                  setFrameTo("");
                }}
                className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
                disabled={videoTasks.length === 0}
              >
                {videoTasks.length === 0 && (
                  <option value="" disabled hidden>
                    暂无视频任务
                  </option>
                )}
                {videoTasks.map((task) => (
                  <option key={task.id} value={task.id}>
                    {task.display_id} · {task.file_name}
                  </option>
                ))}
              </select>
              {taskCursor && (
                <button
                  type="button"
                  onClick={loadMoreTasks}
                  disabled={scopeLoading}
                  className="self-start cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs font-semibold text-brand disabled:opacity-60"
                >
                  加载更多视频
                </button>
              )}
              <label htmlFor="video-export-range-kind" className="text-xs text-muted-foreground">
                范围方式
              </label>
              <select
                id="video-export-range-kind"
                value={rangeKind}
                onChange={(event) => setRangeKind(event.target.value as typeof rangeKind)}
                className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
              >
                <option value="segments">按 Segment</option>
                <option value="frames">按帧区间</option>
              </select>
              {rangeKind === "segments" ? (
                <div className="grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    起始 Segment
                    <select
                      aria-label="起始 Segment"
                      value={startSegmentId}
                      onChange={(event) => setStartSegmentId(event.target.value)}
                      className="rounded-sm border border-border bg-card px-2 py-2 text-foreground"
                    >
                      {segments.map((segment) => (
                        <option key={segment.id} value={segment.id}>
                          #{segment.segment_index + 1} · {segment.start_frame}–{segment.end_frame}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    结束 Segment
                    <select
                      aria-label="结束 Segment"
                      value={endSegmentId}
                      onChange={(event) => setEndSegmentId(event.target.value)}
                      className="rounded-sm border border-border bg-card px-2 py-2 text-foreground"
                    >
                      {segments.map((segment) => (
                        <option key={segment.id} value={segment.id}>
                          #{segment.segment_index + 1} · {segment.start_frame}–{segment.end_frame}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-2">
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    起始帧
                    <input
                      aria-label="起始帧"
                      type="number"
                      min={0}
                      max={maxFrame >= 0 ? maxFrame : undefined}
                      value={frameFrom}
                      onChange={(event) => setFrameFrom(event.target.value)}
                      className="rounded-sm border border-border bg-card px-2 py-2 text-foreground"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    结束帧
                    <input
                      aria-label="结束帧"
                      type="number"
                      min={0}
                      max={maxFrame >= 0 ? maxFrame : undefined}
                      value={frameTo}
                      onChange={(event) => setFrameTo(event.target.value)}
                      className="rounded-sm border border-border bg-card px-2 py-2 text-foreground"
                    />
                  </label>
                </div>
              )}
              {scopeError && <div className="text-xs text-status-danger">{scopeError}</div>}
              {!scopeLoading && !scopeError && !scopeValid && (
                <div className="text-xs text-status-caution">请选择有效的连续范围</div>
              )}
            </div>
          )}
        </div>
      )}
      {showFrameMode && (
        <div className="flex flex-col gap-2">
          <div className="text-xs font-semibold text-foreground">帧模式（单选）</div>
          <div className="grid grid-cols-2 gap-2" role="radiogroup">
            {FRAME_MODES.map((item) => {
              const on = videoFrameMode === item.value;
              return (
                <button
                  key={item.value}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  onClick={() => setVideoFrameMode(item.value)}
                  className={cn(OPTION_BUTTON_CLASS, on && OPTION_BUTTON_ACTIVE_CLASS)}
                >
                  <span
                    className={cn(
                      "mt-px size-4 flex-shrink-0 rounded-full border-[1.5px] border-border bg-card",
                      on && "border-[4px] border-brand",
                    )}
                    aria-hidden
                  />
                  <span className="flex min-w-0 flex-col gap-1">
                    <span className={OPTION_LABEL_CLASS}>{item.label}</span>
                    <span className="text-xs font-normal text-muted-foreground">
                      {item.description}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
      {targets.includes("indexed-png") && (
        <div className="flex flex-col gap-2">
          <label htmlFor="indexed-overlap-policy" className="text-xs font-semibold text-foreground">
            Indexed PNG 重叠策略
          </label>
          <select
            id="indexed-overlap-policy"
            value={indexedOverlapPolicy}
            onChange={(event) =>
              setIndexedOverlapPolicy(event.target.value as typeof indexedOverlapPolicy)
            }
            className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
          >
            <option value="error">检测到重叠时阻止（默认）</option>
            <option value="z_order">z-order 较高者覆盖</option>
            <option value="larger_area">较大实例覆盖</option>
            <option value="smaller_area">较小实例覆盖</option>
          </select>
        </div>
      )}
      {targets.some((target) => target === "davis" || target === "youtube-vos") && (
        <div className="flex flex-col gap-2">
          <label htmlFor="video-overlap-policy" className="text-xs font-semibold text-foreground">
            视频 Mask 重叠策略
          </label>
          <select
            id="video-overlap-policy"
            value={videoOverlapPolicy}
            onChange={(event) =>
              setVideoOverlapPolicy(event.target.value as typeof videoOverlapPolicy)
            }
            className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
          >
            <option value="error">检测到重叠时阻止（默认）</option>
            <option value="z_order">z-order 较高者覆盖</option>
            <option value="larger_area">较大实例覆盖</option>
            <option value="smaller_area">较小实例覆盖</option>
          </select>
        </div>
      )}
      {targets.includes("mots") && (
        <div className="flex flex-col gap-2">
          <label htmlFor="mots-frame-base" className="text-xs font-semibold text-foreground">
            MOTS 帧号基准
          </label>
          <select
            id="mots-frame-base"
            value={motsFrameBase}
            onChange={(event) => setMotsFrameBase(Number(event.target.value) as 0 | 1)}
            className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
          >
            <option value={0}>0-based</option>
            <option value={1}>1-based</option>
          </select>
        </div>
      )}
      <div className="flex flex-col gap-1.5 rounded-md border border-border bg-muted px-3 py-2.5">
        <label className="flex cursor-pointer items-center gap-1.5 text-xs">
          <input
            type="checkbox"
            checked={includeAttributes}
            onChange={(e) => setIncludeAttributes(e.target.checked)}
            className="cursor-pointer"
          />
          <span className="text-foreground">包含属性数据</span>
        </label>
        <div className="text-xs leading-snug text-muted-foreground">
          {includeAttributes
            ? "导出包将包含每个标注的 attributes 字段。"
            : "不包含属性，兼容旧格式。"}
        </div>
        <div className="text-xs leading-snug text-muted-foreground">
          仅对 COCO / YOLO / Video JSON / LiDAR 标准格式生效；原生 JSON 始终包含，MOT 无此字段。
        </div>
      </div>
      {preflight && (
        <div
          className={cn(
            "flex flex-col gap-2 rounded-md border px-3 py-2.5 text-xs",
            preflight.loss_class === "unsupported"
              ? "border-status-danger/40 bg-status-danger/10"
              : preflight.loss_class === "lossy"
                ? "border-status-caution/40 bg-status-caution/10"
                : "border-status-success/40 bg-status-success/10",
          )}
          data-testid="mask-format-preflight"
        >
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-foreground">
              {preflight.loss_class === "lossless"
                ? "预检通过 · 无损"
                : preflight.loss_class === "lossy"
                  ? "预检通过 · 有损"
                  : "预检阻止 · 不支持"}
            </span>
            <span className="text-muted-foreground">
              {preflight.estimated_objects} 对象 · {preflight.estimated_files} 文件
            </span>
          </div>
          {(preflight.losses.length > 0 ||
            preflight.plans.some((plan) =>
              plan.items.some((item) => item.warnings.length > 0),
            )) && (
            <div className="flex flex-col gap-1 text-muted-foreground">
              {preflight.losses.map((loss, index) => (
                <div key={`${loss.code}-${index}`}>
                  <code className="text-foreground">{loss.code}</code> · {loss.message}
                </div>
              ))}
              {preflight.plans.flatMap((plan) =>
                plan.items.flatMap((item) =>
                  item.warnings.map((warning, index) => (
                    <div key={`${item.item_id}-${warning.code}-${index}`}>
                      <code className="text-foreground">{warning.code}</code> · {warning.message}
                    </div>
                  )),
                ),
              )}
            </div>
          )}
          {preflight.loss_class === "lossy" && (
            <label className="flex cursor-pointer items-center gap-2 text-foreground">
              <input
                type="checkbox"
                checked={lossyConfirmed}
                onChange={(event) => setLossyConfirmed(event.target.checked)}
              />
              我已了解以上格式损失，继续导出
            </label>
          )}
        </div>
      )}
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          className="cursor-pointer appearance-none rounded-sm border border-border bg-card px-3 py-2 text-xs font-semibold text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          取消
        </button>
        <button
          type="button"
          disabled={
            busy ||
            targets.length === 0 ||
            !scopeValid ||
            preflight?.loss_class === "unsupported" ||
            (isLidarProject &&
              (targets.includes("coco-multicamera") ||
                targets.includes("kitti") ||
                targets.includes("nuscenes")) &&
              ((targets.includes("kitti") && !kittiCameraRole) ||
                lidarPreflightLoading ||
                !lidarPreflight?.ready))
          }
          onClick={handleExport}
          className="cursor-pointer appearance-none rounded-sm border border-brand bg-brand px-3 py-2 text-xs font-semibold text-brand-foreground hover:bg-brand/90 disabled:cursor-wait disabled:opacity-60"
        >
          {busy
            ? "检查中…"
            : preflight?.loss_class === "lossy" && !lossyConfirmed
              ? "确认格式损失"
              : "开始导出"}
        </button>
      </div>
    </div>
  );
}
