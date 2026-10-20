import { Link, Outlet, useLocation } from "react-router-dom";
import { PageContainer } from "@/components/layout/PageContainer";
import { Icon, type IconName } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

const VIEWS: Array<{
  path: string;
  label: string;
  title: string;
  description: string;
  icon: IconName;
}> = [
  {
    path: "/ai-pre",
    label: "执行预标",
    title: "AI 预标",
    icon: "wandSparkles",
    description: "选择项目与批次，配置模型或编排，批量生成待审候选。",
  },
  {
    path: "/ai-pre/pipelines",
    label: "编排库",
    title: "编排库",
    icon: "layers",
    description: "创建可复用的预标流程，供项目套用并运行。",
  },
  {
    path: "/ai-pre/jobs",
    label: "完整历史",
    title: "AI 任务历史",
    icon: "history",
    description: "查看图片预标和视频追踪的运行进度、结果与失败记录。",
  },
];

export default function AIPreAnnotateLayout() {
  const { pathname } = useLocation();
  const active = VIEWS.find((view) => view.path === pathname.replace(/\/+$/, "")) ?? VIEWS[0];
  return (
    <PageContainer>
      <header className="mb-4">
        <h1 className="mb-1 text-xl font-semibold">{active.title}</h1>
        <p className="text-sm text-muted-foreground">{active.description}</p>
      </header>
      <nav
        aria-label="AI 预标视图"
        className="mb-5 flex gap-5 overflow-x-auto border-b border-border"
      >
        {VIEWS.map((view) => (
          <Link
            key={view.path}
            to={view.path}
            aria-current={active.path === view.path ? "page" : undefined}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-1 pb-2.5 pt-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
              active.path === view.path
                ? "border-foreground text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon name={view.icon} size={14} />
            {view.label}
          </Link>
        ))}
      </nav>
      <div className="min-w-0">
        <Outlet />
      </div>
    </PageContainer>
  );
}
