import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { PageContainer } from "@/components/layout/PageContainer";
import { useProjects } from "@/hooks/useProjects";
import { buildProjectEntryUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import type { ProjectResponse } from "@/api/projects";
import { AnnotatorDashboard } from "./AnnotatorDashboard";
import { ReviewerDashboard } from "./ReviewerDashboard";

type WorkTab = "annotate" | "review" | "browse";

/**
 * Employee home.  An employee may annotate in one project and review in another,
 * so this composes the existing annotation and review dashboards (each already
 * server-filtered by project membership) behind an explicit work-mode switch.
 * A viewer membership is real, authorized access too: viewer-only employees get
 * a read-only project list instead of two empty work tabs.
 * No global "current role" is fabricated; the tab only selects presentation.
 */
export function EmployeeDashboard() {
  // null = the initial tab has not been chosen yet; viewer-only employees then
  // land on the browse tab instead of an empty annotate view.
  const [chosenTab, setTab] = useState<WorkTab | null>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const projectsQuery = useProjects();

  const projects = projectsQuery.data ?? [];
  const viewerProjects = projects.filter((project) => project.my_project_role === "viewer");
  const hasStaffWork = projects.some(
    (project) => project.my_project_role === "annotator" || project.my_project_role === "reviewer",
  );
  const tab: WorkTab =
    chosenTab ?? (hasStaffWork || viewerProjects.length === 0 ? "annotate" : "browse");

  if (projectsQuery.isSuccess && projects.length === 0) {
    return (
      <PageContainer>
        <div className="rounded-lg border border-border bg-card px-6 py-15 text-center">
          <Icon name="folder" size={36} className="mx-auto mb-3 opacity-25" />
          <h1 className="text-lg font-semibold">等待分配项目</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            你还没有加入任何项目，请联系项目负责人将你添加为标注员或质检员。
          </p>
        </div>
      </PageContainer>
    );
  }

  return (
    <div>
      <div className="sticky top-0 z-local-1 flex flex-wrap items-center justify-between gap-3 border-b border-border bg-background px-7 py-3">
        <div className="flex items-center gap-1.5" role="tablist" aria-label="工作模式">
          <Button
            role="tab"
            aria-selected={tab === "annotate"}
            variant={tab === "annotate" ? "primary" : "default"}
            size="sm"
            onClick={() => setTab("annotate")}
            data-testid="employee-tab-annotate"
          >
            <Icon name="target" size={12} />
            标注工作
          </Button>
          <Button
            role="tab"
            aria-selected={tab === "review"}
            variant={tab === "review" ? "primary" : "default"}
            size="sm"
            onClick={() => setTab("review")}
            data-testid="employee-tab-review"
          >
            <Icon name="check" size={12} />
            质检工作
          </Button>
          {viewerProjects.length > 0 && (
            <Button
              role="tab"
              aria-selected={tab === "browse"}
              variant={tab === "browse" ? "primary" : "default"}
              size="sm"
              onClick={() => setTab("browse")}
              data-testid="employee-tab-browse"
            >
              <Icon name="folder" size={12} />
              浏览项目
            </Button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => navigate("/annotate", { state: location.state })}>
            标注列表
          </Button>
          <Button size="sm" onClick={() => navigate("/review")}>
            审核列表
          </Button>
        </div>
      </div>
      {tab === "annotate" ? (
        <AnnotatorDashboard />
      ) : tab === "review" ? (
        <ReviewerDashboard />
      ) : (
        <ViewerProjectsTab projects={viewerProjects} />
      )}
    </div>
  );
}

function ViewerProjectsTab({ projects }: { projects: ProjectResponse[] }) {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <PageContainer>
      <div className="flex flex-col gap-3">
        <p className="m-0 text-sm text-muted-foreground">
          你在这些项目中拥有只读访问权限，可浏览项目数据。
        </p>
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {projects.map((project) => (
            <li
              key={project.id}
              className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3"
              data-testid="viewer-project-card"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{project.name}</div>
                <div className="truncate text-xs text-muted-foreground">观察者 · 只读访问</div>
              </div>
              <Button
                size="sm"
                onClick={() =>
                  navigate(
                    buildProjectEntryUrl(project.id, "viewer", {
                      returnTo: currentWorkbenchReturnTo(location),
                    }),
                  )
                }
              >
                打开
              </Button>
            </li>
          ))}
        </ul>
      </div>
    </PageContainer>
  );
}
