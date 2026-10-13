import { useEffect, useState } from "react";
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { useProject } from "@/hooks/useProjects";
import { useIsProjectOwner } from "@/hooks/useIsProjectOwner";
import { usePermissions } from "@/hooks/usePermissions";
import { GeneralSection } from "./sections/GeneralSection";
import { MembersSection } from "./sections/MembersSection";
import { OwnerSection } from "./sections/OwnerSection";
import { DangerSection } from "./sections/DangerSection";
import { BatchesSection } from "./sections/BatchesSection";
import { ClassesSection } from "./sections/ClassesSection";
import { DatasetsSection } from "./sections/DatasetsSection";
import { MlBackendsSection } from "./sections/MlBackendsSection";
import { RenderingConfigSection } from "./sections/RenderingConfigSection";
import { VideoSamplingSection } from "./sections/VideoSamplingSection";
import { AnnotationGuideSection } from "./sections/AnnotationGuideSection";
import { ProjectReadinessSection } from "./sections/ProjectReadinessSection";
import { buildWorkbenchUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import { ANNOTATION_GUIDE_UI_ENABLED } from "@/config/featureFlags";
import { PageContainer } from "@/components/layout/PageContainer";

const NAV_BUTTON_BASE =
  "flex cursor-pointer appearance-none items-center gap-2 self-stretch rounded-sm border-0 bg-transparent px-2.5 py-2 text-left text-sm font-medium whitespace-nowrap max-md:flex-[0_0_auto]";

type SectionKey =
  | "general"
  | "readiness"
  | "classes"
  | "attributes"
  | "members"
  | "datasets"
  | "batches"
  | "ml-backends"
  | "rendering"
  | "video-sampling"
  | "annotation-guide"
  | "owner"
  | "danger";

const SECTIONS: {
  key: SectionKey;
  label: string;
  icon:
    | "settings"
    | "users"
    | "user"
    | "trash"
    | "tag"
    | "rect"
    | "layers"
    | "db"
    | "bot"
    | "eye"
    | "book"
    | "target";
}[] = [
  { key: "general", label: "基本信息", icon: "settings" },
  { key: "readiness", label: "开工准备", icon: "target" },
  { key: "classes", label: "类别与属性", icon: "rect" },
  { key: "members", label: "成员管理", icon: "users" },
  { key: "datasets", label: "关联数据集", icon: "db" },
  { key: "batches", label: "批次管理", icon: "layers" },
  { key: "ml-backends", label: "ML 模型", icon: "bot" },
  // v0.10.10 · I17.3
  { key: "rendering", label: "工作台规范", icon: "eye" },
  // v0.10.29 · 视频帧采样（仅 video 项目可见）
  { key: "video-sampling", label: "视频采样", icon: "target" },
  // v0.10.13 · E1
  { key: "annotation-guide", label: "标注指引", icon: "book" },
  { key: "owner", label: "负责人", icon: "user" },
  { key: "danger", label: "危险操作", icon: "trash" },
];

const VALID_SECTIONS: SectionKey[] = [
  "general",
  "readiness",
  "classes",
  "attributes",
  "members",
  "datasets",
  "batches",
  "ml-backends",
  "rendering",
  "video-sampling",
  "annotation-guide",
  "owner",
  "danger",
];

export function ProjectSettingsPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { role } = usePermissions();
  const { data: project, isLoading, error } = useProject(id);
  const isOwner = useIsProjectOwner(project ?? null);
  // v0.6.7 B-12-④：解析 ?section=batches 等深链参数。
  const initialSection = (() => {
    const q = searchParams.get("section");
    if (!q || !(VALID_SECTIONS as string[]).includes(q)) return "general";
    if (q === "attributes") return "classes";
    if (q === "annotation-guide" && !ANNOTATION_GUIDE_UI_ENABLED) return "general";
    return q as SectionKey;
  })();
  const [section, setSection] = useState<SectionKey>(initialSection);

  // Keep deep links reactive when a readiness item, browser history entry, or
  // another tab changes ?section after the page has mounted.
  useEffect(() => {
    const requested = searchParams.get("section");
    if (!requested || !(VALID_SECTIONS as string[]).includes(requested)) {
      setSection("general");
      return;
    }
    if (requested === "attributes") {
      setSection("classes");
      return;
    }
    if (requested === "annotation-guide" && !ANNOTATION_GUIDE_UI_ENABLED) {
      setSection("general");
      return;
    }
    setSection(requested as SectionKey);
  }, [searchParams]);

  const selectSection = (next: SectionKey) => {
    setSection(next);
    const params = new URLSearchParams(searchParams);
    params.set("section", next);
    setSearchParams(params, { replace: true });
  };

  if (isLoading) {
    return <div className="p-15 text-center text-muted-foreground">加载中...</div>;
  }
  if (error || !project) {
    return <Navigate to="/unauthorized" replace />;
  }
  if (!isOwner) {
    return <Navigate to="/unauthorized" replace />;
  }

  const isVideoProject = project.data_type === "video";
  const isPointCloudProject =
    project.data_type === "lidar" ||
    project.data_type === "point_cloud" ||
    project.type_key === "lidar";
  const canOpenWorkbench =
    project.type_key === "image-det" || isVideoProject || isPointCloudProject;
  const visibleSections = SECTIONS.filter((s) => {
    if (s.key === "owner") return role === "super_admin";
    if (s.key === "danger") return isOwner;
    // v0.10.29 · 视频帧采样仅对 video 项目展示。
    if (s.key === "video-sampling") return isVideoProject;
    // Annotation Guide 前端暂时下线（见 featureFlags）。
    if (s.key === "annotation-guide") return ANNOTATION_GUIDE_UI_ENABLED;
    return true;
  });

  return (
    <PageContainer>
      <div className="mb-4">
        <button
          type="button"
          onClick={() => navigate("/dashboard")}
          className="mb-2 inline-flex cursor-pointer appearance-none items-center gap-1 border-0 bg-transparent p-0 text-xs text-muted-foreground"
        >
          <Icon name="chevLeft" size={12} />
          返回 Dashboard
        </button>
        <div className="flex items-center justify-between gap-4 max-md:flex-col max-md:items-start">
          <div>
            <h1 className="mb-1 text-xl font-semibold">{project.name}</h1>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="mono">{project.display_id}</span>
              <span>·</span>
              <span>{project.type_label}</span>
              <span>·</span>
              <Badge
                variant={
                  project.status === "completed"
                    ? "success"
                    : project.status === "pending_review"
                      ? "warning"
                      : "accent"
                }
                dot
              >
                {project.status === "in_progress" && "进行中"}
                {project.status === "completed" && "已完成"}
                {project.status === "pending_review" && "待审核"}
                {project.status === "archived" && "已归档"}
              </Badge>
            </div>
          </div>
          <div className="flex gap-2">
            {role === "super_admin" && (
              <Button
                onClick={() => navigate(`/audit?target_type=project&target_id=${project.id}`)}
                title="查看该项目的完整审计时间线"
              >
                <Icon name="activity" size={12} />
                审计追溯
              </Button>
            )}
            <Button onClick={() => navigate(`/projects/${project.id}/data-manager`)}>
              <Icon name="dataManager" size={12} />
              Data Manager
            </Button>
            {canOpenWorkbench && (
              <Button
                onClick={() =>
                  navigate(
                    buildWorkbenchUrl(project.id, {
                      returnTo: currentWorkbenchReturnTo(location),
                    }),
                  )
                }
              >
                <Icon name="target" size={12} />
                打开工作台
              </Button>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-[200px_1fr] gap-5 max-md:grid-cols-1">
        <nav className="flex flex-col gap-0.5 self-start rounded-lg border border-border bg-card p-1.5 max-md:flex-row max-md:overflow-x-auto">
          {visibleSections.map((s) => {
            const active = section === s.key;
            return (
              <button
                key={s.key}
                type="button"
                data-testid={`settings-tab-${s.key}`}
                onClick={() => selectSection(s.key)}
                className={`${NAV_BUTTON_BASE} ${
                  active ? "bg-muted font-semibold text-foreground" : "text-muted-foreground"
                }`}
              >
                <Icon name={s.icon} size={13} />
                {s.label}
              </button>
            );
          })}
        </nav>

        <div className="min-w-0">
          {section === "general" && <GeneralSection project={project} />}
          {section === "readiness" && <ProjectReadinessSection project={project} />}
          {section === "classes" && <ClassesSection project={project} />}
          {section === "members" && <MembersSection project={project} />}
          {section === "datasets" && <DatasetsSection project={project} />}
          {section === "batches" && <BatchesSection project={project} />}
          {section === "ml-backends" && <MlBackendsSection project={project} />}
          {section === "rendering" && <RenderingConfigSection project={project} />}
          {section === "video-sampling" && isVideoProject && (
            <VideoSamplingSection project={project} />
          )}
          {section === "annotation-guide" && <AnnotationGuideSection project={project} />}
          {section === "owner" && role === "super_admin" && <OwnerSection project={project} />}
          {section === "danger" && <DangerSection project={project} />}
        </div>
      </div>
    </PageContainer>
  );
}
