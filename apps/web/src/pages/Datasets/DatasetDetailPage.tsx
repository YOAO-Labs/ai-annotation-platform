import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Grid2X2, List, Upload } from "lucide-react";
import {
  datasetsApi,
  type DatasetItemResponse,
  type DatasetResponse,
  type DatasetWorkbenchProject,
} from "@/api/datasets";
import {
  useDataset,
  useDatasetItems,
  useDatasetItem,
  useDatasetWorkbench,
  useDatasetTrainingVersions,
  useLinkProject,
  useUnlinkProject,
  useScanDatasetItems,
  useBackfillDimensions,
  useBackfillMedia,
  useUpdateDataset,
} from "@/hooks/useDatasets";
import { DatasetItemFilters } from "./DatasetItemFilters";
import { DatasetQualityPanel } from "./DatasetQualityPanel";
import { DatasetProjectStatistics } from "./DatasetProjectStatistics";
import { datasetDataManagerPath } from "../Projects/data-manager/datasetScope";
import {
  datasetTrainingFilterJSON,
  datasetTrainingFilterSummary,
  datasetTrainingPath,
} from "./datasetTrainingSelection";
import { DatasetLifecycleAction } from "@/components/datasets/DatasetLifecycleAction";
import {
  AgentSourceStatus,
  agentAvailabilityReason,
} from "@/components/datasets/AgentSourceStatus";
import { DatasetSavedViews } from "./DatasetSavedViews";
import { projectsApi } from "@/api/projects";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { useVerifiedImage, verifiedContentError } from "@/hooks/useVerifiedImage";
import {
  captureOriginalOwner,
  isOriginalOwnerCurrent,
  readOriginal as downloadOriginalBytes,
} from "@/api/originals";
import { parseReference } from "@/utils/referenceContent";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import { useToastStore } from "@/components/ui/Toast";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { DirectoryReferencePanel } from "@/components/datasets/DirectoryReferencePanel";
import { ImportDatasetWizard } from "@/components/datasets/ImportDatasetWizard";
import { LinkJobProgress } from "@/components/datasets/LinkJobProgress";
import { AxisConventionPicker } from "@/components/datasets/AxisConventionPicker";
import type { LidarAxisConvention } from "@/types";
import {
  availabilityLabels,
  datasetFilterPatch,
  clampDatasetPage,
  parseDatasetDetailUrl,
  safeDatasetsReturn,
  type DatasetTab,
} from "./datasetsUrlState";

const inputClass =
  "rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const tdClass = "border-b border-border px-3 py-2.5 text-left text-sm";
const tabs: { value: DatasetTab; label: string }[] = [
  { value: "data", label: "数据" },
  { value: "sources", label: "来源与同步" },
  { value: "quality", label: "质量" },
  { value: "projects", label: "项目关联" },
  { value: "versions", label: "关联版本" },
];
const statusLabels: Record<string, string> = {
  pending: "待构建",
  running: "构建中",
  ready: "已就绪",
  failed: "失败",
  cancelled: "已取消",
};
function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "操作失败，请重试";
}
function ErrorState({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-2 rounded-md bg-status-danger-soft p-3 text-xs text-status-danger"
    >
      <CopyableError message={errorMessage(error)} />
      <Button size="sm" onClick={retry}>
        重试
      </Button>
    </div>
  );
}
function mediaInfo(item: DatasetItemResponse) {
  if (item.file_type === "image")
    return item.width && item.height ? `${item.width}×${item.height}` : "尺寸待回填";
  const video = item.metadata.video as Record<string, unknown> | undefined;
  if (item.file_type !== "video") return "—";
  if (video?.probe_error) return `解析失败：${video.probe_error}`;
  return video
    ? [
        video.width && video.height ? `${video.width}×${video.height}` : "",
        video.fps ? `${video.fps}fps` : "",
        video.frame_count ? `${video.frame_count}帧` : "",
        video.codec,
      ]
        .filter(Boolean)
        .join(" · ")
    : "元数据生成中";
}
function Availability({ item }: { item: DatasetItemResponse }) {
  return (
    <span
      className={
        item.availability && ["missing", "changed", "unavailable"].includes(item.availability.state)
          ? "text-status-caution"
          : "text-muted-foreground"
      }
    >
      {availabilityLabels[item.availability?.state ?? "unverified"]}
      {item.availability?.agent_status && item.availability.agent_status.state !== "online" && (
        <span className="ml-2">
          <AgentSourceStatus status={item.availability.agent_status} compact />
        </span>
      )}
      {item.availability?.original_cache_recorded && (
        <span className="ml-2 text-xs text-muted-foreground">有完整缓存记录</span>
      )}
    </span>
  );
}
function previewPath(datasetId: string, item: DatasetItemResponse) {
  return item.availability?.preview_available &&
    item.availability.allowed_actions.includes("read_preview")
    ? `/datasets/${datasetId}/items/${item.id}/preview`
    : null;
}
function ItemThumbnail({ datasetId, item }: { datasetId: string; item: DatasetItemResponse }) {
  const image = useVerifiedImage(previewPath(datasetId, item));
  const url = image.url ?? (!parseReference(item.file_path) ? item.thumbnail_url : null);
  return url ? (
    <img
      loading="lazy"
      src={url}
      alt={`${item.file_name} 缩略图`}
      className="h-24 w-full rounded-md bg-muted object-contain"
    />
  ) : (
    <div className="flex h-24 items-center justify-center rounded-md bg-muted text-xs text-muted-foreground">
      {image.error ? "预览暂不可用" : "无可用预览"}
    </div>
  );
}
export function DatasetDetailPage() {
  const { datasetId = "" } = useParams();
  const revision = useSessionRevision();
  return <DatasetDetailOwner key={`${revision}/${datasetId}`} datasetId={datasetId} />;
}
function DatasetDetailOwner({ datasetId }: { datasetId: string }) {
  const result = useDataset(datasetId);
  const [params] = useSearchParams();
  const back = safeDatasetsReturn(params.get("return"));
  return (
    <PageContainer>
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft className="size-3.5" />
        {back.startsWith("/storage") ? "返回存储位置" : "返回数据集列表"}
      </Link>
      {result.isLoading && (
        <p role="status" className="p-6 text-sm text-muted-foreground">
          正在加载数据集详情…
        </p>
      )}
      {result.error && <ErrorState error={result.error} retry={() => void result.refetch()} />}
      {result.data && !result.error && <DatasetDetailContent ds={result.data} />}
    </PageContainer>
  );
}
function DatasetDetailContent({ ds }: { ds: DatasetResponse }) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const state = parseDatasetDetailUrl(params);
  const [freezeFilter, setFreezeFilter] = useState<{ query: string; signature: string } | null>(
    null,
  );
  useEffect(() => {
    if (freezeFilter && freezeFilter.signature !== location.key) setFreezeFilter(null);
  }, [freezeFilter, location.key]);
  const filterJson = datasetTrainingFilterJSON(state.filters);
  const freezeButtonRef = useRef<HTMLButtonElement>(null);
  const capture = useSessionGuard();
  const currentOwner = capture();
  const { role } = usePermissions();
  const userId = useAuthStore((s) => s.user?.id);
  const revision = useSessionRevision();
  const canManageLifecycle =
    role === "super_admin" || (role === "project_admin" && ds.created_by === userId);
  const archived = !!ds.archived_at;
  const canManage = canManageLifecycle && !archived;
  const client = useQueryClient();
  const toast = useToastStore((s) => s.push);
  const workbench = useDatasetWorkbench(ds.id);
  const files = useDatasetItems(
    ds.id,
    {
      limit: state.pageSize,
      offset: state.page * state.pageSize,
      ...state.filters,
    },
    !state.restoreSavedView,
  );
  const versions = useDatasetTrainingVersions(
    ds.id,
    { limit: state.versionPageSize, offset: state.versionPage * state.versionPageSize },
    state.tab === "versions",
  );
  const itemPage =
    files.data && !files.isFetching
      ? clampDatasetPage(state.page, state.pageSize, files.data.total)
      : state.page;
  const projectPage =
    workbench.data && !workbench.isFetching
      ? clampDatasetPage(state.projectPage, state.projectPageSize, workbench.data.projects.length)
      : state.projectPage;
  const versionPage =
    versions.data && !versions.isFetching
      ? clampDatasetPage(state.versionPage, state.versionPageSize, versions.data.total)
      : state.versionPage;
  useEffect(() => {
    const changes = [
      ["page", itemPage, state.page],
      ["project_page", projectPage, state.projectPage],
      ["version_page", versionPage, state.versionPage],
    ] as const;
    if (!currentOwner() || !changes.some(([, value, current]) => value !== current)) return;
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        changes.forEach(([key, value, current]) => {
          if (value === current) return;
          if (value) next.set(key, String(value + 1));
          else next.delete(key);
        });
        return next;
      },
      { replace: true },
    );
  }, [
    itemPage,
    projectPage,
    versionPage,
    state.page,
    state.projectPage,
    state.versionPage,
    currentOwner,
    setParams,
  ]);
  const allProjects = useQuery({
    queryKey: ["dataset-link-project-options", ds.id, revision],
    queryFn: async ({ signal }) => {
      if (!currentOwner() || signal.aborted) throw new DOMException("请求已失效", "AbortError");
      const value = await projectsApi.list(undefined, { signal });
      if (!currentOwner() || signal.aborted) throw new DOMException("请求已失效", "AbortError");
      return value;
    },
    enabled: canManage && state.tab === "projects",
  });
  const link = useLinkProject(ds.id);
  const unlink = useUnlinkProject(ds.id);
  const scan = useScanDatasetItems(ds.id);
  const dimensions = useBackfillDimensions(ds.id);
  const media = useBackfillMedia(ds.id);
  const update = useUpdateDataset();
  const [unlinkProject, setUnlinkProject] = useState<DatasetWorkbenchProject | null>(null);
  const [linkJob, setLinkJob] = useState<{ jobId: string; projectId: string } | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [name, setName] = useState(ds.name);
  const [description, setDescription] = useState(ds.description);
  const [actionError, setActionError] = useState("");
  const selectedTrigger = useRef<HTMLElement | null>(null);
  const patch = (changes: Record<string, string | undefined>, replace = false) => {
    if (!currentOwner()) return;
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        Object.entries(changes).forEach(([key, value]) =>
          value ? next.set(key, value) : next.delete(key),
        );
        return next;
      },
      { replace },
    );
  };
  const refresh = () => {
    if (!currentOwner()) return;
    [
      "dataset",
      "datasets",
      "dataset-items",
      "dataset-item",
      "dataset-workbench",
      "dataset-project-statistics",
      "dataset-training-versions",
      "tasks",
      "projects",
      "project-stats",
    ].forEach((key) => void client.invalidateQueries({ queryKey: [key] }));
  };
  const success = (msg: string) => {
    if (currentOwner()) {
      setActionError("");
      refresh();
      toast({ msg });
    }
  };
  const failure = (error: unknown) => {
    if (currentOwner()) setActionError(errorMessage(error));
  };
  const projects = workbench.error ? [] : (workbench.data?.projects ?? []);
  const sources = workbench.data?.sources ?? [];
  const availableProjects = (allProjects.data ?? []).filter(
    (project) => !projects.some((linked) => linked.id === project.id),
  );
  const importOpen = ["1", ds.id].includes(params.get("import") ?? "");
  const changeAxis = async (next: LidarAxisConvention) => {
    if (!canManage || next === (ds.axis_convention ?? "iso_8855") || update.isPending) return;
    if (
      (ds.project_count ?? 0) > 0 &&
      !(await confirmDialog({
        title: "切换坐标系约定",
        description: "该数据集已关联项目。若已有 3D 标注，切换坐标系后历史标注可能与点云不一致。",
        confirmLabel: "继续切换",
      }))
    )
      return;
    if (!currentOwner()) return;
    update.mutate(
      { id: ds.id, payload: { axis_convention: next } },
      { onSuccess: () => success("坐标系约定已更新"), onError: failure },
    );
  };
  return (
    <>
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{ds.name}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {ds.description || "管理样本、来源与项目使用"}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            {ds.file_count.toLocaleString()} 个已登记样本 ·{" "}
            {ds.has_scenes ? "含 Scene" : "无 Scene"} ·{" "}
            {ds.is_temporal ? "时序数据集" : "普通数据集"} · 最近变更{" "}
            {new Date(ds.updated_at).toLocaleString("zh-CN")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canManageLifecycle && <DatasetLifecycleAction dataset={ds} onApplied={refresh} />}
          {canManage && (
            <div className="flex gap-2">
              <Button
                onClick={() => {
                  setName(ds.name);
                  setDescription(ds.description);
                  setEditOpen(true);
                }}
              >
                编辑信息
              </Button>
              <Button onClick={() => patch({ import: "1" })}>
                <Upload className="mr-1.5 size-3.5" />
                上传文件
              </Button>
            </div>
          )}
        </div>
      </header>
      {archived && (
        <section
          aria-label="数据集归档状态"
          className="mb-4 rounded-lg border border-border bg-muted p-4 text-sm"
        >
          <h2 className="text-md font-semibold">数据集已归档</h2>
          <p className="mt-2 leading-relaxed">
            样本、来源历史、已有项目任务、标注和冻结版本均保留。数据集的新接入、修改、关联和新训练冻结已停止；已有任务继续按当前项目权限使用。
          </p>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            恢复只解除归档，不恢复已撤销的人员权限、来源或同步委托，也不自动重启旧后台任务。
          </p>
        </section>
      )}
      {actionError && (
        <div
          role="alert"
          className="mb-3 rounded-md bg-status-danger-soft p-3 text-xs text-status-danger"
        >
          <CopyableError message={actionError} />
        </div>
      )}
      <nav
        aria-label="数据集详情页签"
        className="mb-4 flex flex-wrap gap-1 border-b border-border pb-2"
      >
        {tabs.map((tab) => (
          <Button
            key={tab.value}
            variant={state.tab === tab.value ? "primary" : "ghost"}
            size="sm"
            aria-current={state.tab === tab.value ? "page" : undefined}
            onClick={() => patch({ tab: tab.value })}
          >
            {tab.label}
          </Button>
        ))}
      </nav>
      {workbench.error && (
        <div className="mb-3">
          <ErrorState error={workbench.error} retry={() => void workbench.refetch()} />
        </div>
      )}
      {state.tab === "data" && (
        <section aria-label="数据浏览" className="rounded-lg border border-border bg-card">
          {!archived && freezeFilter && freezeFilter.signature === location.key && (
            <Modal
              open
              returnFocusRef={freezeButtonRef}
              title="冻结当前筛选"
              onClose={() => setFreezeFilter(null)}
            >
              <div className="space-y-4 text-sm">
                <p>选择管理的关联项目，使用该项目的标注准备训练版本。</p>
                <p className="break-all text-xs leading-relaxed text-muted-foreground">
                  {datasetTrainingFilterSummary(JSON.parse(freezeFilter.query))}
                </p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  当前条件仍为动态查询。进入项目后检查最终任务、标注资格与复制体量，再创建固定版本；页面分页不限制范围。
                </p>
                {!workbench.error &&
                  projects
                    .filter((project) => project.can_manage)
                    .map((project) => (
                      <Link
                        key={project.id}
                        to={datasetTrainingPath(project.id, ds.id, freezeFilter.query)}
                        className="block rounded-md border border-border px-3 py-2 text-primary hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        在“{project.name}”中准备版本
                      </Link>
                    ))}
              </div>
            </Modal>
          )}
          <DatasetSavedViews
            datasetId={ds.id}
            filters={state.filters}
            selectedId={state.savedViewId}
            restore={state.restoreSavedView}
            onApply={(filters, id) =>
              patch({
                ...datasetFilterPatch(filters),
                saved_view: id,
                view_query: id ? "1" : undefined,
                page: undefined,
              })
            }
            onSelect={(id) => patch({ saved_view: id, view_query: id ? "1" : undefined })}
          />
          <div className="border-b border-border p-3">
            <DatasetItemFilters
              key={JSON.stringify([
                state.filters.media_type,
                state.filters.extension,
                state.filters.dimension_state,
                state.filters.min_width,
                state.filters.max_width,
                state.filters.min_height,
                state.filters.max_height,
              ])}
              filters={state.filters}
              datasetId={ds.id}
              hasScenes={!!ds.has_scenes}
              sources={sources}
              onChange={(filters) => patch({ ...datasetFilterPatch(filters), page: undefined })}
            />
            <div className="mt-3 flex flex-wrap justify-end gap-2">
              {projects.some((project) => project.can_manage) && (
                <Button
                  size="sm"
                  ref={freezeButtonRef}
                  disabled={
                    archived ||
                    state.restoreSavedView ||
                    !files.data ||
                    files.isFetching ||
                    !!files.error ||
                    filterJson === null ||
                    !!workbench.error
                  }
                  onClick={() => {
                    if (!archived && currentOwner() && filterJson !== null)
                      setFreezeFilter({ query: filterJson, signature: location.key });
                  }}
                >
                  冻结当前筛选
                </Button>
              )}
              <Button
                size="sm"
                aria-label="表格视图"
                aria-pressed={state.view === "table"}
                onClick={() => patch({ view: undefined })}
              >
                <List className="size-3.5" />
              </Button>
              <Button
                size="sm"
                aria-label="网格视图"
                aria-pressed={state.view === "grid"}
                onClick={() => patch({ view: "grid" })}
              >
                <Grid2X2 className="size-3.5" />
              </Button>
            </div>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border px-3 py-2 text-xs text-muted-foreground">
            {Object.entries(workbench.data?.availability_counts ?? {}).map(([key, count]) => (
              <span key={key}>
                {availabilityLabels[key as keyof typeof availabilityLabels] ?? key}：{count}
              </span>
            ))}
            <span>状态基于最近观测，实际读取时重新核验。</span>
          </div>
          {canManage && (
            <div className="flex flex-wrap gap-2 border-b border-border p-3">
              <Button
                size="sm"
                disabled={scan.isPending}
                onClick={() =>
                  scan.mutate(undefined, {
                    onSuccess: (value) =>
                      success(
                        value.new_items
                          ? `扫描完成，新增 ${value.new_items} 个文件`
                          : "扫描完成，无新文件",
                      ),
                    onError: failure,
                  })
                }
              >
                {scan.isPending ? "扫描中…" : "扫描导入"}
              </Button>
              {ds.data_type === "image" && (
                <Button
                  size="sm"
                  disabled={dimensions.isPending}
                  onClick={() =>
                    dimensions.mutate(undefined, {
                      onSuccess: (value) =>
                        success(
                          `回填完成 · 处理 ${value.processed} / 失败 ${value.failed}${value.remaining_hint ? "，仍有未处理项" : ""}`,
                        ),
                      onError: failure,
                    })
                  }
                >
                  {dimensions.isPending ? "回填中…" : "回填维度"}
                </Button>
              )}
              {ds.data_type === "video" && (
                <Button
                  size="sm"
                  disabled={media.isPending}
                  onClick={() =>
                    media.mutate(undefined, {
                      onSuccess: () => success("已提交视频元数据补生成任务"),
                      onError: failure,
                    })
                  }
                >
                  补生成元数据
                </Button>
              )}
            </div>
          )}
          {ds.data_type === "point_cloud" && (
            <div className="border-b border-border p-3">
              <h2 className="mb-2 text-xs font-medium">点云坐标系</h2>
              <AxisConventionPicker
                value={ds.axis_convention}
                datasetId={ds.id}
                disabled={!canManage || update.isPending}
                onChange={changeAxis}
              />
            </div>
          )}
          {(files.isLoading || state.restoreSavedView) && (
            <p role="status" className="p-6 text-sm text-muted-foreground">
              {state.restoreSavedView
                ? "等待恢复个人视图；不可用时请重新选择视图。"
                : "正在加载样本…"}
            </p>
          )}
          {files.error && <ErrorState error={files.error} retry={() => void files.refetch()} />}
          {state.view === "table" ? (
            <div className="overflow-x-auto">
              <table className={`${listStyles.table} min-w-full`}>
                <thead>
                  <tr>
                    {["文件名", "类型", "大小", "媒体信息", "来源", "原图状态", "登记时间"].map(
                      (label) => (
                        <th
                          key={label}
                          className={`${tdClass} bg-muted font-medium text-muted-foreground`}
                        >
                          {label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {!state.restoreSavedView &&
                    files.data?.items.map((item) => (
                      <tr key={item.id}>
                        <td className={tdClass}>
                          <button
                            className="max-w-72 truncate text-left font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            onClick={(e) => {
                              selectedTrigger.current = e.currentTarget;
                              patch({ item: item.id });
                            }}
                          >
                            {item.file_name}
                          </button>
                          {typeof item.metadata.source_revision_number === "number" && (
                            <Badge variant="outline">
                              修订 {item.metadata.source_revision_number}
                            </Badge>
                          )}
                        </td>
                        <td className={tdClass}>{item.file_type}</td>
                        <td className={`${tdClass} tabular-nums`}>
                          {item.file_size == null
                            ? "—"
                            : `${(item.file_size / 1024).toFixed(1)} KB`}
                        </td>
                        <td className={tdClass}>{mediaInfo(item)}</td>
                        <td className={tdClass}>
                          {item.availability?.source_name ?? "托管上传或未提供"}
                        </td>
                        <td className={tdClass}>
                          <Availability item={item} />
                        </td>
                        <td className={tdClass}>
                          {new Date(item.created_at).toLocaleDateString("zh-CN")}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3 p-3 sm:grid-cols-3 xl:grid-cols-5">
              {!state.restoreSavedView &&
                files.data?.items.map((item) => (
                  <button
                    key={item.id}
                    className="min-w-0 rounded-md border border-border p-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={(e) => {
                      selectedTrigger.current = e.currentTarget;
                      patch({ item: item.id });
                    }}
                  >
                    <ItemThumbnail datasetId={ds.id} item={item} />
                    <span className="mt-2 block truncate font-medium">{item.file_name}</span>
                    <span className="mt-1 block">
                      <Availability item={item} />
                    </span>
                  </button>
                ))}
            </div>
          )}
          {!state.restoreSavedView &&
            !files.isLoading &&
            !files.error &&
            !files.data?.items.length && (
              <p className="p-6 text-center text-sm text-muted-foreground">
                {Object.keys(state.filters).length
                  ? "没有匹配的样本，调整筛选后重试"
                  : "暂无已登记样本；扫描中时请等待来源任务完成"}
              </p>
            )}
          <ListPagination
            label="数据集文件"
            page={state.page}
            pageSize={state.pageSize}
            total={state.restoreSavedView ? 0 : (files.data?.total ?? 0)}
            loading={state.restoreSavedView || files.isFetching}
            onPageChange={(page) => patch({ page: page ? String(page + 1) : undefined })}
            onPageSizeChange={(size) =>
              patch({ page_size: size === 20 ? undefined : String(size), page: undefined })
            }
          />
        </section>
      )}
      {state.tab === "quality" && <DatasetQualityPanel datasetId={ds.id} canManage={canManage} />}
      {state.tab === "sources" && (
        <section aria-label="来源与同步">
          {canManageLifecycle && ds.data_type === "image" && !ds.is_temporal ? (
            <DirectoryReferencePanel
              key={`${ds.id}/${revision}`}
              {...{ persistUrl: true }}
              datasetId={ds.id}
              archived={archived}
              initialSourceId={params.get("source") ?? undefined}
              initialScanId={params.get("scan") ?? undefined}
            />
          ) : (
            <p className="rounded-md bg-muted p-4 text-sm text-muted-foreground">
              当前账号没有此数据集的来源管理权限，或该数据类型暂未开放目录接入。样本读取状态请在“数据”中查看；来源问题请联系管理员。
            </p>
          )}
        </section>
      )}
      {state.tab === "projects" && (
        <section aria-label="项目关联" className="space-y-3">
          {workbench.isLoading && <p role="status">正在加载关联项目…</p>}
          {workbench.data && !workbench.error && (
            <DatasetProjectStatistics
              datasetId={ds.id}
              projects={projects}
              selectedProjectId={state.statsProjectId}
              onProjectChange={(id) => patch({ stats_project: id })}
            />
          )}
          {!workbench.isLoading && !workbench.error && !projects.length && (
            <p className="rounded-md bg-muted p-4 text-sm text-muted-foreground">
              未关联可见项目；关联后可在项目中标注，并创建冻结训练版本。
            </p>
          )}
          {projects
            .slice(
              state.projectPage * state.projectPageSize,
              (state.projectPage + 1) * state.projectPageSize,
            )
            .map((project) => (
              <article key={project.id} className="rounded-lg border border-border bg-card p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <Link
                      to={datasetDataManagerPath(project.id, ds.id)}
                      className="text-sm font-medium text-primary hover:underline"
                    >
                      {project.name}
                    </Link>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {project.display_id} · {project.type_label}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    {!archived && project.can_manage && (
                      <Link
                        className="text-xs text-primary hover:underline"
                        to={datasetTrainingPath(project.id, ds.id)}
                      >
                        为整份数据集创建版本
                      </Link>
                    )}
                    {canManage && project.can_manage && (
                      <Button size="sm" variant="ghost" onClick={() => setUnlinkProject(project)}>
                        取消关联
                      </Button>
                    )}
                  </div>
                </div>
                <dl className="mt-3 flex flex-wrap gap-5 text-xs">
                  <div>
                    <dt className="text-muted-foreground">本数据集任务</dt>
                    <dd className="mt-1 tabular-nums">{project.total_tasks}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">有标注任务</dt>
                    <dd className="mt-1 tabular-nums">{project.labeled_tasks}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">已完成任务</dt>
                    <dd className="mt-1 tabular-nums">{project.completed_tasks}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">待审核任务</dt>
                    <dd className="mt-1 tabular-nums">{project.review_tasks}</dd>
                  </div>
                </dl>
              </article>
            ))}
          {!workbench.error && projects.length > 0 && (
            <ListPagination
              label="关联项目"
              page={state.projectPage}
              pageSize={state.projectPageSize}
              total={projects.length}
              loading={workbench.isFetching}
              onPageChange={(page) => patch({ project_page: page ? String(page + 1) : undefined })}
              onPageSizeChange={(size) =>
                patch({
                  project_page_size: size === 20 ? undefined : String(size),
                  project_page: undefined,
                })
              }
            />
          )}
          {canManage && !workbench.error && availableProjects.length > 0 && (
            <label className="grid max-w-md gap-2 text-xs">
              关联到项目
              <select
                className={inputClass}
                aria-label="关联到项目"
                value=""
                disabled={link.isPending}
                onChange={(e) => {
                  const id = e.target.value;
                  if (id)
                    link.mutate(id, {
                      onSuccess: (value) => {
                        if (!currentOwner()) return;
                        if (value.async_job_id)
                          setLinkJob({ jobId: value.async_job_id, projectId: id });
                        success("项目已关联");
                      },
                      onError: failure,
                    });
                }}
              >
                <option value="">选择项目…</option>
                {availableProjects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name} ({project.display_id})
                  </option>
                ))}
              </select>
            </label>
          )}
          {allProjects.error && (
            <ErrorState error={allProjects.error} retry={() => void allProjects.refetch()} />
          )}
          {linkJob && (
            <LinkJobProgress
              {...linkJob}
              onDone={() => {
                if (currentOwner()) {
                  setLinkJob(null);
                  refresh();
                }
              }}
            />
          )}
        </section>
      )}
      {state.tab === "versions" && (
        <section aria-label="关联训练版本" className="rounded-lg border border-border bg-card">
          <div className="border-b border-border p-3">
            <h2 className="text-sm font-semibold">项目中的冻结训练版本</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              版本属于各自项目，图片和标注以冻结快照为准；活跃数据集继续变化不会改写版本。
            </p>
            <div className="mt-3 flex flex-wrap gap-3">
              {projects
                .filter((p) => !archived && p.can_manage)
                .map((project) => (
                  <Link
                    key={project.id}
                    className="text-xs text-primary hover:underline"
                    to={datasetTrainingPath(project.id, ds.id)}
                  >
                    为“{project.name}”冻结整份数据集
                  </Link>
                ))}
            </div>
            {!projects.some((p) => p.can_manage) && (
              <p className="mt-2 text-xs text-muted-foreground">请先关联有管理权限的项目。</p>
            )}
          </div>
          {versions.isLoading && (
            <p role="status" className="p-4 text-xs">
              正在加载关联版本…
            </p>
          )}
          {versions.error && (
            <ErrorState error={versions.error} retry={() => void versions.refetch()} />
          )}
          {versions.data?.items.map((version) => (
            <div
              key={version.id}
              className="flex flex-wrap items-center justify-between gap-2 border-b border-border p-3 text-sm"
            >
              <div>
                <Link
                  className="font-medium text-primary hover:underline"
                  to={`/projects/${version.project_id}/learning/data/${version.id}`}
                >
                  {version.name}
                </Link>
                <p className="mt-1 text-muted-foreground">
                  {version.project_name} · {new Date(version.created_at).toLocaleString("zh-CN")}
                </p>
              </div>
              <Badge variant="outline">{statusLabels[version.status] ?? version.status}</Badge>
            </div>
          ))}
          {!versions.isLoading && !versions.error && !versions.data?.items.length && (
            <p className="p-6 text-xs text-muted-foreground">暂无可见的关联训练版本</p>
          )}
          <ListPagination
            label="关联版本"
            page={state.versionPage}
            pageSize={state.versionPageSize}
            total={versions.data?.total ?? 0}
            loading={versions.isFetching}
            onPageChange={(page) => patch({ version_page: page ? String(page + 1) : undefined })}
            onPageSizeChange={(size) =>
              patch({
                version_page_size: size === 20 ? undefined : String(size),
                version_page: undefined,
              })
            }
          />
        </section>
      )}
      {state.itemId && (
        <ItemInspector
          key={`${ds.id}/${state.itemId}`}
          datasetId={ds.id}
          itemId={state.itemId}
          onClose={() => patch({ item: undefined }, true)}
          returnFocusRef={selectedTrigger}
          detailReturn={`${location.pathname}${location.search}`}
        />
      )}
      {!archived && unlinkProject && (
        <UnlinkDatasetProject
          key={unlinkProject.id}
          dataset={ds}
          project={unlinkProject}
          pending={unlink.isPending}
          onClose={() => setUnlinkProject(null)}
          onConfirm={() =>
            unlink.mutate(unlinkProject.id, {
              onSuccess: (value) => {
                if (!currentOwner()) return;
                setUnlinkProject(null);
                success(
                  `已取消关联 · 清理 ${value.deleted_tasks} 个任务、${value.deleted_annotations} 个标注`,
                );
              },
              onError: failure,
            })
          }
        />
      )}
      <ImportDatasetWizard
        open={importOpen && canManage}
        onClose={() => patch({ import: undefined }, true)}
        datasetId={ds.id}
        datasetName={ds.name}
        onUploaded={refresh}
      />
      <Modal open={editOpen && canManage} onClose={() => setEditOpen(false)} title="编辑数据集信息">
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!currentOwner()) return;
            update.mutate(
              { id: ds.id, payload: { name: name.trim(), description } },
              {
                onSuccess: () => {
                  if (!currentOwner()) return;
                  setEditOpen(false);
                  success("数据集信息已更新");
                },
                onError: failure,
              },
            );
          }}
        >
          <label className="grid gap-1 text-xs">
            数据集名称
            <input
              className={inputClass}
              minLength={2}
              maxLength={60}
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="grid gap-1 text-xs">
            说明
            <textarea
              className={inputClass}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <p className="text-xs text-muted-foreground">修改名称或说明不会移动原图或修改源目录。</p>
          <Button type="submit" disabled={update.isPending || name.trim().length < 2}>
            保存
          </Button>
        </form>
      </Modal>
    </>
  );
}
function ItemInspector({
  datasetId,
  itemId,
  onClose,
  returnFocusRef,
  detailReturn,
}: {
  datasetId: string;
  itemId: string;
  onClose: () => void;
  returnFocusRef: React.RefObject<HTMLElement | null>;
  detailReturn: string;
}) {
  const result = useDatasetItem(datasetId, itemId);
  const [readOriginal, setReadOriginal] = useState(false);
  const [readPreview, setReadPreview] = useState(false);
  const item = result.data;
  const identity = item ? parseReference(item.file_path) : null;
  const actions = item?.availability?.allowed_actions ?? [];
  const imagePath =
    readOriginal && item && identity && actions.includes("read_original")
      ? `/dataset-items/${item.id}/content?sha256=${identity.sha256}`
      : readPreview && item
        ? previewPath(datasetId, item)
        : null;
  const image = useVerifiedImage(imagePath);
  const userId = useAuthStore((state) => state.user?.id);
  const sessionId = useAuthStore((state) => state.sessionId);
  const downloadScope = JSON.stringify([userId, sessionId, datasetId, itemId]);
  const downloadOwner = useRef({ scope: downloadScope, active: true, epoch: 0 });
  if (downloadOwner.current.scope !== downloadScope) {
    downloadOwner.current.active = false;
    downloadOwner.current = { scope: downloadScope, active: true, epoch: 0 };
  }
  const scope = downloadOwner.current;
  const pendingDownload = useRef<AbortController | null>(null);
  const [downloadState, setDownloadState] = useState<{
    scope: string;
    pending: boolean;
    error?: string;
  }>({ scope: downloadScope, pending: false });
  useEffect(() => {
    scope.active = true;
    return () => {
      scope.active = false;
      scope.epoch += 1;
      pendingDownload.current?.abort();
      pendingDownload.current = null;
    };
  }, [scope]);
  const downloadOriginal = async () => {
    const owner = captureOriginalOwner();
    if (!item || !owner || pendingDownload.current) return;
    const epoch = scope.epoch;
    const controller = new AbortController();
    pendingDownload.current = controller;
    const current = () =>
      downloadOwner.current === scope &&
      scope.active &&
      epoch === scope.epoch &&
      !controller.signal.aborted &&
      isOriginalOwnerCurrent(owner);
    setDownloadState({ scope: downloadScope, pending: true });
    let url: string | undefined;
    let anchor: HTMLAnchorElement | undefined;
    try {
      const { blob } = await downloadOriginalBytes(
        { kind: "item", datasetId, itemId },
        { owner, signal: controller.signal },
      );
      if (!current()) return;
      url = URL.createObjectURL(blob);
      anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.file_name;
      anchor.hidden = true;
      document.body.appendChild(anchor);
      anchor.click();
    } catch (error) {
      if (current())
        setDownloadState({
          scope: downloadScope,
          pending: false,
          error:
            (error as { status?: number })?.status === 413
              ? "原件超过完整下载大小限制，请联系管理员处理"
              : verifiedContentError(error),
        });
    } finally {
      anchor?.remove();
      if (url) URL.revokeObjectURL(url);
      if (pendingDownload.current === controller) pendingDownload.current = null;
      if (current()) setDownloadState((previous) => ({ ...previous, pending: false }));
    }
  };
  const closeInspector = () => {
    scope.active = false;
    scope.epoch += 1;
    pendingDownload.current?.abort();
    pendingDownload.current = null;
    onClose();
  };
  return (
    <Modal
      open
      onClose={closeInspector}
      title="样本检查器"
      width={720}
      returnFocusRef={returnFocusRef}
    >
      {result.isLoading && <p role="status">正在读取样本…</p>}
      {result.error && <ErrorState error={result.error} retry={() => void result.refetch()} />}
      {item && (
        <>
          <h2 className="break-words text-sm font-semibold">{item.file_name}</h2>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-xs">
            <div>
              <dt className="text-muted-foreground">媒体信息</dt>
              <dd className="mt-1">
                {mediaInfo(item)} · {item.file_type}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">大小</dt>
              <dd className="mt-1">
                {item.file_size == null ? "未提供" : `${item.file_size.toLocaleString()} 字节`}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">原图状态</dt>
              <dd className="mt-1">
                <Availability item={item} />
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">完整原图缓存</dt>
              <dd className="mt-1">
                {item.availability?.original_cache_recorded
                  ? "已有登记，读取时复核"
                  : "无当前有效记录"}
              </dd>
              {item.availability?.original_cache_expires_at && (
                <dd className="mt-1 text-muted-foreground">
                  到期{" "}
                  {new Date(item.availability.original_cache_expires_at).toLocaleString("zh-CN")}
                </dd>
              )}
            </div>
            <div>
              <dt className="text-muted-foreground">预览状态</dt>
              <dd className="mt-1">
                {item.availability?.preview_available ? "已有预览，读取时复核" : "无可用预览"}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">来源</dt>
              <dd className="mt-1">{item.availability?.source_name ?? "托管上传或未提供"}</dd>
              {item.availability?.agent_status && (
                <dd className="mt-1">
                  <AgentSourceStatus status={item.availability.agent_status} />
                </dd>
              )}
            </div>
            <div>
              <dt className="text-muted-foreground">状态观测时间</dt>
              <dd className="mt-1">
                {item.availability?.observed_at
                  ? new Date(item.availability.observed_at).toLocaleString("zh-CN")
                  : "未提供"}
              </dd>
            </div>
          </dl>
          {item.availability?.reason_code && (
            <p className="mt-3 break-all text-xs text-muted-foreground">
              状态原因：
              {agentAvailabilityReason(item.availability.reason_code) ??
                item.availability.reason_code}
            </p>
          )}
          <p className="mt-3 text-xs text-muted-foreground">
            来源状态是历史观测；完整缓存记录不表示来源已恢复。预览仅供只读浏览，不能代替原图。核验读取后仍须进入有权限的项目进行标注。
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {identity && actions.includes("read_original") && (
              <Button
                size="sm"
                onClick={() => {
                  setReadOriginal(true);
                  setReadPreview(false);
                }}
              >
                核验读取原图
              </Button>
            )}
            {actions.includes("read_preview") && item.availability?.preview_available && (
              <Button
                size="sm"
                onClick={() => {
                  setReadPreview(true);
                  setReadOriginal(false);
                }}
              >
                读取只读预览
              </Button>
            )}
            {!identity && (
              <Button
                size="sm"
                disabled={downloadState.scope === downloadScope && downloadState.pending}
                onClick={() => void downloadOriginal()}
              >
                {downloadState.scope === downloadScope && downloadState.pending
                  ? "正在核验并下载…"
                  : "核验下载原件"}
              </Button>
            )}
            {actions.includes("view_location") && item.availability?.location_id && (
              <Link
                className="text-xs text-primary hover:underline"
                to={`/storage?${new URLSearchParams({ location: item.availability.location_id, returnTo: detailReturn })}`}
              >
                查看存储位置
              </Link>
            )}
          </div>
          {downloadState.scope === downloadScope && downloadState.error && (
            <p role="alert" className="mt-3 text-xs text-status-danger">
              {downloadState.error}
            </p>
          )}
          {image.url && (
            <img
              src={image.url}
              alt={readOriginal ? "已核验原图" : "只读缓存预览"}
              className="mt-3 max-h-96 w-full object-contain"
            />
          )}
          {imagePath && !image.url && (
            <p role="status" className="mt-3 text-xs text-muted-foreground">
              {image.error ?? "正在读取并复核内容…"}
            </p>
          )}
          {image.error && (
            <Button size="sm" onClick={image.retry}>
              重试图片读取
            </Button>
          )}
        </>
      )}
    </Modal>
  );
}
function UnlinkDatasetProject({
  dataset,
  project,
  pending,
  onClose,
  onConfirm,
}: {
  dataset: DatasetResponse;
  project: DatasetWorkbenchProject;
  pending: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const current = capture();
  const [name, setName] = useState("");
  const preview = useQuery({
    queryKey: ["dataset-unlink-preview", dataset.id, project.id, revision],
    queryFn: async ({ signal }) => {
      if (!current() || signal.aborted) throw new DOMException("请求已失效", "AbortError");
      const value = await datasetsApi.previewUnlink(dataset.id, project.id, { signal });
      if (!current() || signal.aborted) throw new DOMException("请求已失效", "AbortError");
      return value;
    },
    retry: false,
  });
  const dangerous = (preview.data?.will_delete_tasks ?? 0) > 0;
  return (
    <Modal open onClose={onClose} title="确认取消关联">
      <p className="text-sm">
        取消“{dataset.name}”与“{project.name}”的关联
      </p>
      {preview.isLoading && (
        <p role="status" className="mt-3 text-xs">
          正在统计影响范围…
        </p>
      )}
      {preview.error && (
        <div className="mt-3">
          <ErrorState error={preview.error} retry={() => void preview.refetch()} />
          <p className="mt-2 text-xs text-muted-foreground">影响范围未确认，暂不能取消关联。</p>
        </div>
      )}
      {preview.data && (
        <p className="mt-3 text-xs text-muted-foreground">
          将删除 {preview.data.will_delete_tasks} 个任务、{preview.data.will_delete_annotations}{" "}
          个标注，并清理 {preview.data.will_delete_batches} 个空批次。此操作不可恢复。
        </p>
      )}
      {dangerous && (
        <label className="mt-3 grid gap-1 text-xs">
          请输入数据集名称“{dataset.name}”确认
          <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>取消</Button>
        <Button
          variant="danger"
          disabled={
            !preview.isSuccess ||
            preview.isFetching ||
            pending ||
            (dangerous && name.trim() !== dataset.name)
          }
          onClick={() => {
            if (current()) onConfirm();
          }}
        >
          确认删除并取消关联
        </Button>
      </div>
    </Modal>
  );
}
