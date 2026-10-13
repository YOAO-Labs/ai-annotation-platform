import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { StatCard } from "@/components/ui/StatCard";
import { SearchInput } from "@/components/ui/SearchInput";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { useToastStore } from "@/components/ui/Toast";
import { useProjects, useProjectStats } from "@/hooks/useProjects";
import type { ProjectResponse } from "@/api/projects";
import { buildProjectEntryUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import { projectDisplayType } from "@/utils/projectDisplay";
import { PageContainer } from "@/components/layout/PageContainer";

const FILTERS = ["全部", "进行中", "待审核", "已完成"] as const;
const FILTER_STATUS_MAP: Record<string, string | undefined> = {
  全部: undefined,
  进行中: "in_progress",
  待审核: "pending_review",
  已完成: "completed",
};
// 按媒体维度 data_type 放行工作台,图像子类型(det/ocr/seg)同走图像栈,见 DashboardPage。
const WORKBENCH_DATA_TYPES = new Set(["image", "video", "lidar"]);

const TABLE_HEAD_CELL_CLASS =
  "border-b border-border bg-muted px-3 py-2.5 text-left text-xs font-medium whitespace-nowrap text-muted-foreground [&:first-child]:pl-4";
const TABLE_CELL_CLASS =
  "border-b border-border px-3 py-3 align-middle [&:first-child]:pl-4 [&:nth-child(3)]:whitespace-nowrap [&:nth-child(4)]:whitespace-nowrap";
const EMPTY_CELL_CLASS = "p-10 text-center text-muted-foreground";

export function ViewerDashboard() {
  const [filter, setFilter] = useState<string>("全部");
  const [query, setQuery] = useState("");
  const navigate = useNavigate();
  const location = useLocation();
  const pushToast = useToastStore((s) => s.push);
  const onOpenProject = (p: ProjectResponse) => {
    if (p.data_type && WORKBENCH_DATA_TYPES.has(p.data_type)) {
      navigate(
        buildProjectEntryUrl(p.id, "viewer", { returnTo: currentWorkbenchReturnTo(location) }),
      );
    } else {
      pushToast({
        msg: `项目 "${p.name}" 已打开`,
        sub: `${projectDisplayType(p)} 的标注界面尚未实现`,
      });
    }
  };

  const { data: projects = [], isLoading } = useProjects({
    status: FILTER_STATUS_MAP[filter],
    search: query || undefined,
  });
  const { data: stats } = useProjectStats();

  return (
    <PageContainer>
      <div className="mb-5">
        <h1 className="mb-1 text-xl font-semibold">项目概览</h1>
        <p className="text-sm text-muted-foreground">查看项目进度与数据质量</p>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
        <StatCard
          icon="layers"
          label="数据总量"
          value={(stats?.total_data ?? 0).toLocaleString()}
        />
        <StatCard
          icon="check"
          label="已完成标注"
          value={(stats?.completed ?? 0).toLocaleString()}
        />
        <StatCard icon="sparkles" label="AI 接管率" value={`${stats?.ai_rate ?? 0}%`} />
        <StatCard
          icon="flag"
          label="待审核"
          value={(stats?.pending_review ?? 0).toLocaleString()}
        />
      </div>

      <Card>
        <div className="flex items-center justify-between border-b border-border px-4 py-3.5">
          <div className="flex items-center gap-3">
            <h3 className="text-sm font-semibold">项目列表</h3>
            <FilterGroup label="状态" hideLabel>
              {FILTERS.map((option) => (
                <FilterToggle
                  key={option}
                  active={filter === option}
                  onClick={() => setFilter(option)}
                >
                  {option}
                </FilterToggle>
              ))}
            </FilterGroup>
          </div>
          <SearchInput placeholder="搜索项目..." value={query} onChange={setQuery} width={220} />
        </div>
        <div className="w-full overflow-x-auto [overscroll-behavior-x:contain]">
          <table className="w-full min-w-[760px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                {["项目", "进度", "AI 模型", "状态"].map((h, i) => (
                  <th key={i} className={TABLE_HEAD_CELL_CLASS}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr>
                  <td colSpan={4} className={EMPTY_CELL_CLASS}>
                    加载中...
                  </td>
                </tr>
              )}
              {!isLoading &&
                projects.map((p) => {
                  const total = p.total_tasks || 1;
                  const pct = Math.round((p.completed_tasks / total) * 100);
                  return (
                    <tr key={p.id} onClick={() => onOpenProject(p)} className="cursor-pointer">
                      <td className={TABLE_CELL_CLASS}>
                        <div className="max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap text-sm font-medium">
                          {p.name}
                        </div>
                        <span className="mono block max-w-[260px] overflow-hidden text-ellipsis whitespace-nowrap text-xs text-muted-foreground">
                          {p.display_id}
                        </span>
                      </td>
                      <td className={`${TABLE_CELL_CLASS} min-w-[180px]`}>
                        <ProgressBar value={pct} />
                        <span className="mono text-xs text-muted-foreground">{pct}%</span>
                      </td>
                      <td className={TABLE_CELL_CLASS}>
                        {p.ai_enabled ? (
                          <Badge variant="ai">
                            <Icon name="sparkles" size={10} />
                            {p.ml_backend_id ? "已接入模型" : "未接入模型"}
                          </Badge>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className={TABLE_CELL_CLASS}>
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
                    </tr>
                  );
                })}
              {!isLoading && projects.length === 0 && (
                <tr>
                  <td colSpan={4} className={EMPTY_CELL_CLASS}>
                    没有匹配的项目
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </PageContainer>
  );
}
