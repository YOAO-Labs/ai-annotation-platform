import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Avatar } from "@/components/ui/Avatar";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { StatCard } from "@/components/ui/StatCard";
import { SearchInput } from "@/components/ui/SearchInput";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useToastStore } from "@/components/ui/Toast";
import { Can } from "@/components/guards/Can";
import { useProjectPage, useProjectStats } from "@/hooks/useProjects";
import { type ProjectResponse } from "@/api/projects";
import { CreateProjectWizard } from "@/components/projects/CreateProjectWizard";
import { ImportDatasetWizard } from "@/components/datasets/ImportDatasetWizard";
import { useAuthStore } from "@/stores/authStore";
import { useAuditLogs } from "@/hooks/useAudit";
import { auditActionLabel } from "@/utils/auditLabels";
import { ProjectFilterControl } from "./ProjectFilterControl";
import { ProjectFilterSummary } from "./ProjectFilterSummary";
import type { DashboardFilters } from "./dashboardUrlState";
import {
  DASHBOARD_FILTER_KEYS,
  dashboardUrlCodec,
  EMPTY_DASHBOARD_URL_STATE,
  type DashboardStatus,
} from "./dashboardUrlState";
import { ProjectGrid } from "./ProjectGrid";
import { ProjectPagination } from "./ProjectPagination";
import { ProjectActionsMenu } from "./ProjectActionsMenu";
import { buildProjectEntryUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import { projectDisplayType } from "@/utils/projectDisplay";
import { statSeriesHint, statSparkValues, statTrendFromSeries } from "@/utils/projectStatsSeries";
import { PageContainer } from "@/components/layout/PageContainer";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { useDashboardQuerySync } from "./useDashboardQuerySync";

// v0.10.28 · 列表图标改读媒体维度 data_type (image / video / lidar).
const DATA_TYPE_ICONS: Record<string, IconName> = {
  image: "image",
  video: "video",
  lidar: "cube",
};
// 按媒体维度 data_type 放行工作台:image / video / lidar 各有对应渲染栈
// (stageKind: lidar→3D, video→视频, 其余→图像)。图像子类型 det/ocr/seg 同走图像栈,
// 不再按 type_key 细分,避免新增图像子类型(如 OCR)漏进白名单。
const WORKBENCH_DATA_TYPES = new Set(["image", "video", "lidar"]);

// 表格单元(列表视图)共用类
const TD_CLASS = "border-b border-border p-3 align-middle";
const TH_CLASS =
  "border-b border-border bg-muted px-3 py-2.5 text-left text-xs font-medium text-muted-foreground whitespace-nowrap first:pl-4 last:pr-4";
const BATCH_LINK_CLASS =
  "mt-1 cursor-pointer appearance-none border-0 bg-transparent p-0 text-xs text-muted-foreground underline decoration-dotted underline-offset-2 [font:inherit]";

function ProjectRow({
  p,
  onOpen,
  canManage,
  onSettings,
}: {
  p: ProjectResponse;
  onOpen: (p: ProjectResponse) => void;
  canManage: boolean;
  onSettings: (p: ProjectResponse, section?: string) => void;
}) {
  const total = p.total_tasks || 1;
  const pct = Math.round((p.completed_tasks / total) * 100);
  // v0.7.0：aiPct = AI 派生标注覆盖的任务数 / 总任务数（替换 v0.6.x 的启发式 pct * 0.6）
  const aiPct = p.ai_enabled ? Math.round(((p.ai_completed_tasks ?? 0) / total) * 100) : 0;
  // v0.6.7：「已动工」副条 = (in_progress + review + completed) / total，让 0 完成但有进度的项目可见
  const startedPct = Math.round(
    (((p.in_progress_tasks ?? 0) + p.review_tasks + p.completed_tasks) / total) * 100,
  );
  const due = p.due_date ?? "—";
  const updated = p.updated_at ? new Date(p.updated_at).toLocaleDateString("zh-CN") : "—";

  return (
    <tr>
      <td className={`${TD_CLASS} pl-4`}>
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted text-muted-foreground">
            <Icon name={DATA_TYPE_ICONS[p.data_type ?? "image"] || "image"} size={14} />
          </div>
          <div className="min-w-0">
            <div className="max-w-[220px] truncate text-sm font-medium">{p.name}</div>
            <div className="mt-0.5 flex min-w-0 items-baseline gap-2">
              <span className="mono text-xs leading-4 text-muted-foreground">{p.display_id}</span>
              <span className="leading-4 text-muted-foreground">·</span>
              <span className="truncate text-xs leading-4 text-muted-foreground">
                {projectDisplayType(p)}
              </span>
            </div>
          </div>
        </div>
      </td>
      <td className={TD_CLASS}>
        <div className="flex items-center gap-2">
          <UserAvatar size="sm" user={{ name: p.owner_name, avatar_ref: p.owner_avatar_ref }} />
          <div>
            <div className="whitespace-nowrap text-sm">{p.owner_name ?? "—"}</div>
            <div className="text-xs text-muted-foreground">
              {(p.member_count ?? 0) > 0 ? `${p.member_count} 名成员` : "暂无成员"}
            </div>
          </div>
        </div>
      </td>
      <td className={`${TD_CLASS} min-w-[220px]`}>
        <ProgressBar value={pct} aiValue={aiPct} inProgressValue={startedPct} />
        <div className="mt-1 flex justify-between text-xs text-muted-foreground">
          <span className="mono">
            {p.completed_tasks.toLocaleString()} / {p.total_tasks.toLocaleString()}
            {(p.in_progress_tasks ?? 0) + p.review_tasks > 0 && (
              <span className="text-xs text-muted-foreground">
                {" · "}
                {(p.in_progress_tasks ?? 0) > 0 && <>{p.in_progress_tasks} 进行中</>}
                {(p.in_progress_tasks ?? 0) > 0 && p.review_tasks > 0 && " · "}
                {p.review_tasks > 0 && <>{p.review_tasks} 待审</>}
              </span>
            )}
          </span>
          <span className="font-medium text-foreground">{pct}%</span>
        </div>
        {canManage && (
          <>
            {(p.batch_summary?.total ?? 0) > 0 && (
              <div className="mt-[3px] text-xs text-muted-foreground">
                {p.batch_summary?.total} 个批次
                {(p.batch_summary?.assigned ?? 0) > 0 && <> · {p.batch_summary?.assigned} 已分派</>}
                {(p.batch_summary?.in_review ?? 0) > 0 && (
                  <>
                    {" "}
                    ·{" "}
                    <span className="text-status-caution">{p.batch_summary?.in_review} 审核中</span>
                  </>
                )}
              </div>
            )}
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onSettings(p, "batches");
              }}
              className={BATCH_LINK_CLASS}
              title="跳转到项目设置 → 批次管理"
            >
              <Icon name="layers" size={10} /> 查看批次分派
            </button>
          </>
        )}
      </td>
      <td className={TD_CLASS}>
        {p.ai_enabled ? (
          <Badge variant="ai">
            <Icon name="sparkles" size={10} />
            {p.ml_backend_id ? "已接入模型" : "未接入模型"}
          </Badge>
        ) : (
          <span className="whitespace-nowrap text-xs text-muted-foreground">未启用</span>
        )}
      </td>
      <td className={TD_CLASS}>
        {p.status === "in_progress" && (
          <Badge variant="accent" dot>
            进行中
          </Badge>
        )}
        {p.status === "completed" && (
          <Badge variant="success" dot>
            已完成
          </Badge>
        )}
        {p.status === "pending_review" && (
          <Badge variant="warning" dot>
            待审核
          </Badge>
        )}
      </td>
      <td className={TD_CLASS}>
        <div className="whitespace-nowrap text-xs">{due}</div>
        <div className="text-xs text-muted-foreground">更新 {updated}</div>
      </td>
      <td
        className={`${TD_CLASS} whitespace-nowrap pr-4 text-right`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-end gap-1">
          {canManage && (
            <Button size="sm" variant="ghost" onClick={() => onSettings(p)}>
              <Icon name="settings" size={12} />
              设置
            </Button>
          )}
          <Button size="sm" onClick={() => onOpen(p)}>
            打开 <Icon name="chevRight" size={11} />
          </Button>
          <ProjectActionsMenu project={p} canManage={canManage} />
        </div>
      </td>
    </tr>
  );
}

const FILTERS = ["全部", "进行中", "待审核", "已完成"] as const;
const FILTER_STATUS_MAP: Record<string, DashboardStatus | undefined> = {
  全部: undefined,
  进行中: "in_progress",
  待审核: "pending_review",
  已完成: "completed",
};

export function DashboardPage() {
  const [filterOpen, setFilterOpen] = useState(false);
  const pushToast = useToastStore((s) => s.push);
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const urlState = useUrlFilterState({
    codec: dashboardUrlCodec,
    defaults: EMPTY_DASHBOARD_URL_STATE,
    ownedKeys: DASHBOARD_FILTER_KEYS,
  });
  const {
    currentUrl,
    query,
    setQuery: updateQuery,
    patchUrl,
    writeSearchParams,
  } = useDashboardQuerySync(urlState);
  const filter =
    currentUrl.status === "in_progress"
      ? "进行中"
      : currentUrl.status === "pending_review"
        ? "待审核"
        : currentUrl.status === "completed"
          ? "已完成"
          : "全部";
  const advanced = useMemo<DashboardFilters>(
    () => ({
      status: currentUrl.status,
      data_type: currentUrl.data_type,
      member_id: currentUrl.member_id,
      created_from: currentUrl.created_from,
      created_to: currentUrl.created_to,
    }),
    [
      currentUrl.created_from,
      currentUrl.created_to,
      currentUrl.data_type,
      currentUrl.member_id,
      currentUrl.status,
    ],
  );
  const wizardOpen = searchParams.get("new") === "1";
  // v0.10.11 · 从 ProjectGrid "复制项目" 跳来时携带 ?from=<id>; Wizard 据此预填.
  const wizardSourceProjectId = searchParams.get("from") || undefined;
  // B-35 · list/grid 切换使用独立的 layout 参数，避免占用页面级 view 参数。
  const viewMode: "list" | "grid" = searchParams.get("layout") === "grid" ? "grid" : "list";
  const setViewMode = (mode: "list" | "grid") => {
    const next = dashboardUrlCodec.encode(searchParams, {
      ...currentUrl,
      query: query.trim(),
      page: query.trim() === currentUrl.query ? currentUrl.page : 1,
    });
    if (mode === "grid") next.set("layout", "grid");
    else next.delete("layout");
    writeSearchParams(next, { replace: true });
  };
  const [importOpen, setImportOpen] = useState(false);
  const currentUser = useAuthStore((s) => s.user);

  const canManageProject = (p: ProjectResponse): boolean => {
    if (!currentUser) return false;
    if (currentUser.role === "super_admin") return true;
    return currentUser.role === "project_admin" && p.owner_id === currentUser.id;
  };

  const onSettings = (p: ProjectResponse, section?: string) =>
    navigate(`/projects/${p.id}/settings${section ? `?section=${section}` : ""}`);

  const onOpenProject = (p: ProjectResponse) => {
    if (p.data_type && WORKBENCH_DATA_TYPES.has(p.data_type)) {
      navigate(
        buildProjectEntryUrl(p.id, p.my_project_role, {
          returnTo: currentWorkbenchReturnTo(location),
        }),
      );
    } else {
      pushToast({
        msg: `项目 "${p.name}" 已打开`,
        sub: `${projectDisplayType(p)} 的标注界面尚未实现`,
      });
    }
  };

  const openWizard = () => {
    const next = new URLSearchParams(searchParams);
    next.set("new", "1");
    writeSearchParams(next);
  };
  const closeWizard = () => {
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    next.delete("from");
    writeSearchParams(next, { replace: true });
  };

  const {
    data: projectPage,
    isLoading: pageLoading,
    isError,
    isFetching,
    refetch,
  } = useProjectPage({
    page: currentUrl.page,
    page_size: currentUrl.page_size,
    status: currentUrl.status,
    search: currentUrl.query || undefined,
    data_type: currentUrl.data_type.length > 0 ? currentUrl.data_type : undefined,
    member_id: currentUrl.member_id,
    created_from: currentUrl.created_from,
    created_to: currentUrl.created_to,
  });
  const projects = projectPage?.items ?? [];
  const lastPage = Math.max(1, projectPage?.pages ?? 1);
  const pageOutOfRange = !isError && projectPage !== undefined && currentUrl.page > lastPage;
  const isLoading = pageLoading || pageOutOfRange;
  useEffect(() => {
    if (pageOutOfRange && !isFetching && query.trim() === currentUrl.query) {
      patchUrl({ page: lastPage });
    }
  }, [currentUrl.query, isFetching, lastPage, pageOutOfRange, query, patchUrl]);

  const advancedActiveCount = useMemo(() => {
    let n = 0;
    if (advanced.data_type.length) n += 1;
    if (advanced.member_id) n += 1;
    if (advanced.created_from || advanced.created_to) n += 1;
    return n;
  }, [advanced]);

  const applyFilters = (next: DashboardFilters) => {
    patchUrl(
      {
        query: query.trim(),
        page: 1,
        status: next.status,
        data_type: next.data_type,
        member_id: next.member_id,
        created_from: next.created_from,
        created_to: next.created_to,
      },
      { replace: false },
    );
  };
  const updateFilterTab = (next: string) => {
    patchUrl({ status: FILTER_STATUS_MAP[next], query: query.trim(), page: 1 }, { replace: false });
  };

  const { data: stats } = useProjectStats();
  const { data: audit } = useAuditLogs({ page: 1, page_size: 8, business_only: true });
  const recentActivity = audit?.items ?? [];

  return (
    <PageContainer>
      <div className="mb-5 flex items-end justify-between gap-6 max-[900px]:flex-col max-[900px]:items-start">
        <div>
          <h1 className="mb-1 text-xl font-semibold">项目总览</h1>
          <p className="text-sm text-muted-foreground">管理你的标注项目,跟踪进度与 AI 辅助效率</p>
        </div>
        <div className="flex gap-2">
          <Can permission="dataset.create">
            <Button onClick={() => setImportOpen(true)}>
              <Icon name="upload" size={13} />
              导入数据集
            </Button>
            <ImportDatasetWizard
              open={importOpen}
              onClose={() => setImportOpen(false)}
              onUploaded={() => navigate("/datasets")}
            />
          </Can>
          <Can permission="project.create">
            <Button variant="primary" onClick={openWizard}>
              <Icon name="plus" size={13} />
              新建项目
            </Button>
            <CreateProjectWizard
              open={wizardOpen}
              onClose={closeWizard}
              sourceProjectId={wizardSourceProjectId}
            />
          </Can>
        </div>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
        <StatCard
          icon="layers"
          label="任务总量"
          value={(stats?.total_data ?? 0).toLocaleString()}
          trend={statTrendFromSeries(stats?.total_data_series)}
          sparkValues={statSparkValues(stats?.total_data_series)}
          sparkColor="var(--sc-brand)"
          hint={statSeriesHint(stats?.total_data_series)}
        />
        <StatCard
          icon="check"
          label="已完成任务"
          value={(stats?.completed ?? 0).toLocaleString()}
          trend={statTrendFromSeries(stats?.completed_series)}
          sparkValues={statSparkValues(stats?.completed_series)}
          sparkColor="var(--sc-positive)"
          hint={statSeriesHint(stats?.completed_series)}
        />
        <StatCard
          icon="sparkles"
          label="AI 派生标注率"
          value={`${stats?.ai_rate ?? 0}%`}
          trend={statTrendFromSeries(stats?.ai_rate_series)}
          sparkValues={statSparkValues(stats?.ai_rate_series)}
          sparkColor="var(--sc-chart-4)"
          hint={statSeriesHint(stats?.ai_rate_series)}
        />
        <StatCard
          icon="flag"
          label="待审核任务"
          value={(stats?.pending_review ?? 0).toLocaleString()}
          trend={statTrendFromSeries(stats?.pending_review_series)}
          sparkValues={statSparkValues(stats?.pending_review_series)}
          sparkColor="var(--sc-caution)"
          hint={statSeriesHint(stats?.pending_review_series)}
        />
      </div>

      <Card>
        <div className="flex items-center justify-between border-b border-border px-4 py-3.5 max-[900px]:flex-col max-[900px]:items-start">
          <div className="flex items-center gap-3 max-[900px]:flex-wrap">
            <h3 className="text-sm font-semibold">我的项目</h3>
            <FilterGroup label="状态" hideLabel>
              {FILTERS.map((option) => (
                <FilterToggle
                  key={option}
                  active={filter === option}
                  onClick={() => updateFilterTab(option)}
                >
                  {option}
                </FilterToggle>
              ))}
            </FilterGroup>
          </div>
          <div className="flex gap-2 max-[900px]:flex-wrap">
            <SearchInput
              placeholder="搜索项目..."
              value={query}
              onChange={updateQuery}
              width={220}
            />
            <ProjectFilterControl
              open={filterOpen}
              onOpenChange={setFilterOpen}
              initial={advanced}
              onApply={applyFilters}
              count={advancedActiveCount}
            />
            <Button
              onClick={() => setViewMode(viewMode === "grid" ? "list" : "grid")}
              title={viewMode === "grid" ? "切换到列表视图" : "切换到网格视图"}
              className={viewMode === "grid" ? "bg-muted" : undefined}
            >
              <Icon name={viewMode === "grid" ? "list" : "grid"} size={13} />
            </Button>
          </div>
        </div>
        <ProjectFilterSummary
          filters={advanced}
          onChange={applyFilters}
          onEdit={() => setFilterOpen(true)}
        />
        {isError && projectPage && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 border-b border-border bg-status-caution-soft px-4 py-2 text-xs"
          >
            刷新失败，当前显示上次成功加载的项目
            <Button size="xs" onClick={() => void refetch()}>
              重新加载
            </Button>
          </div>
        )}
        {isError && !projectPage ? (
          <div
            role="alert"
            className="flex items-center justify-center gap-3 p-10 text-sm text-muted-foreground"
          >
            项目列表暂时无法加载
            <Button size="xs" onClick={() => void refetch()}>
              重新加载
            </Button>
          </div>
        ) : viewMode === "grid" ? (
          isLoading ? (
            <div className="p-10 text-center text-muted-foreground">加载中...</div>
          ) : (
            <ProjectGrid
              projects={projects}
              onOpen={onOpenProject}
              canManage={canManageProject}
              onSettings={onSettings}
            />
          )
        ) : (
          <div className="w-full overflow-x-auto [overscroll-behavior-x:contain]">
            <table className="w-full min-w-[1040px] border-separate border-spacing-0 text-sm">
              <thead>
                <tr>
                  {["项目", "负责人", "进度", "AI 模型", "状态", "截止 / 更新", ""].map((h, i) => (
                    <th key={i} className={TH_CLASS}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {isLoading && (
                  <tr>
                    <td colSpan={7} className="p-10 text-center text-muted-foreground">
                      加载中...
                    </td>
                  </tr>
                )}
                {!isLoading &&
                  projects.map((p) => (
                    <ProjectRow
                      key={p.id}
                      p={p}
                      onOpen={onOpenProject}
                      canManage={canManageProject(p)}
                      onSettings={onSettings}
                    />
                  ))}
                {!isLoading && projects.length === 0 && (
                  <tr>
                    <td colSpan={7} className="p-10 text-center text-muted-foreground">
                      没有匹配的项目
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        {(!isError || projectPage) && (
          <ProjectPagination
            page={currentUrl.page}
            pageSize={currentUrl.page_size}
            total={projectPage?.total ?? 0}
            loading={isLoading || query.trim() !== currentUrl.query}
            onPageChange={(page) => patchUrl({ page }, { replace: false })}
            onPageSizeChange={(page_size) => patchUrl({ page_size, page: 1 }, { replace: false })}
          />
        )}
      </Card>

      <div className="mt-4 grid grid-cols-[repeat(auto-fit,minmax(320px,1fr))] gap-3 max-[900px]:grid-cols-1">
        <Card>
          <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
            <h3 className="text-sm font-semibold">AI 预标注队列</h3>
          </div>
          <div className="px-4 py-8 text-center text-sm text-muted-foreground">
            <Icon name="sparkles" size={28} className="mb-2 opacity-25" />
            <div>暂无运行中的预标注任务</div>
            <div className="mt-1 text-xs">在标注工作台中点击"AI 一键预标"启动</div>
          </div>
        </Card>

        <Card>
          <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
            <h3 className="text-sm font-semibold">近期活动</h3>
          </div>
          {recentActivity.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-muted-foreground">
              <Icon name="activity" size={26} className="mb-2 opacity-25" />
              <div>暂无业务事件</div>
            </div>
          ) : (
            <ul className="m-0 list-none p-0">
              {recentActivity.map((it) => (
                <li
                  key={it.id}
                  className="flex items-center gap-2.5 border-b border-border px-4 py-2.5 text-sm"
                >
                  <Avatar initial={(it.actor_email ?? "?").slice(0, 1).toUpperCase()} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium">{it.actor_email ?? "匿名"}</span>
                      <Badge variant="accent">{auditActionLabel(it.action)}</Badge>
                      {it.target_type && (
                        <span className="text-xs text-muted-foreground">
                          {it.target_type}
                          {it.target_id && (
                            <span className="mono ml-1">
                              {it.target_id.length > 24
                                ? it.target_id.slice(0, 8) + "…"
                                : it.target_id}
                            </span>
                          )}
                        </span>
                      )}
                    </div>
                  </div>
                  <span className="whitespace-nowrap text-xs text-muted-foreground">
                    {relativeTime(it.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </PageContainer>
  );
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.round(diff / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return `${m} 分钟前`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.round(h / 24);
  if (d < 30) return `${d} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}
