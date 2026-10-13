import { Card } from "@/components/ui/Card";
import { Icon, type IconName } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { type ProjectResponse } from "@/api/projects";
import { ProjectActionsMenu } from "./ProjectActionsMenu";
import { projectDisplayType } from "@/utils/projectDisplay";

// v0.10.28 · 卡片图标改读媒体维度 data_type (image / video / lidar).
const DATA_TYPE_ICONS: Record<string, IconName> = {
  image: "image",
  video: "video",
  lidar: "cube",
};

interface Props {
  projects: ProjectResponse[];
  onOpen: (p: ProjectResponse) => void;
  canManage: (p: ProjectResponse) => boolean;
  onSettings: (p: ProjectResponse, section?: string) => void;
}

/** v0.7.2 · 项目网格视图 — DashboardPage 用作 list 视图的可切换姿态。
 *  B-47 · 卡片操作为 [设置] [⋮(导出 / 复制 / 导入 …)] [打开]（列表视图把 ⋮ 收到末位）。
 */
export function ProjectGrid({ projects, onOpen, canManage, onSettings }: Props) {
  if (projects.length === 0) {
    return <div className="px-4 py-10 text-center text-muted-foreground">没有匹配的项目</div>;
  }

  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3 p-4">
      {projects.map((p) => {
        const total = p.total_tasks || 1;
        const pct = Math.round((p.completed_tasks / total) * 100);
        const aiPct = p.ai_enabled ? Math.round(((p.ai_completed_tasks ?? 0) / total) * 100) : 0;
        const startedPct = Math.round(
          (((p.in_progress_tasks ?? 0) + p.review_tasks + p.completed_tasks) / total) * 100,
        );
        const due = p.due_date ?? "—";

        return (
          <Card key={p.id} onClick={() => onOpen(p)}>
            <div className="flex min-h-full cursor-pointer flex-col gap-2.5 p-3.5">
              <div className="flex items-start gap-2.5">
                <div className="flex h-8 w-8 flex-[0_0_32px] items-center justify-center rounded-md border border-border bg-muted text-muted-foreground">
                  <Icon name={DATA_TYPE_ICONS[p.data_type ?? "image"] || "image"} size={15} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="overflow-hidden text-ellipsis whitespace-nowrap text-sm font-semibold">
                    {p.name}
                  </div>
                  <div className="mt-0.5 flex items-baseline gap-1.5">
                    <span className="mono text-xs leading-[15px] text-muted-foreground">
                      {p.display_id}
                    </span>
                    <span className="text-xs leading-[15px] text-muted-foreground">
                      {projectDisplayType(p)}
                    </span>
                  </div>
                </div>
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
              </div>

              <div>
                <ProgressBar value={pct} aiValue={aiPct} inProgressValue={startedPct} />
                <div className="mt-1 flex justify-between text-xs text-muted-foreground">
                  <span className="mono">
                    {p.completed_tasks.toLocaleString()} / {p.total_tasks.toLocaleString()}
                  </span>
                  <span className="font-medium text-foreground">{pct}%</span>
                </div>
              </div>

              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-1.5">
                  <UserAvatar
                    size="sm"
                    user={{ name: p.owner_name, avatar_ref: p.owner_avatar_ref }}
                  />
                  <span className="overflow-hidden text-ellipsis whitespace-nowrap text-xs text-muted-foreground">
                    {p.owner_name ?? "—"}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    · {p.member_count ?? 0} 成员
                  </span>
                </div>
                <span className="flex-[0_0_auto] text-xs text-muted-foreground">截止 {due}</span>
              </div>

              <div
                className="mt-auto flex justify-end gap-1.5"
                onClick={(e) => e.stopPropagation()}
              >
                {canManage(p) && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSettings(p);
                    }}
                  >
                    <Icon name="settings" size={12} />
                    设置
                  </Button>
                )}
                <ProjectActionsMenu project={p} canManage={canManage(p)} />
                <Button
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpen(p);
                  }}
                >
                  打开 <Icon name="chevRight" size={11} />
                </Button>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
