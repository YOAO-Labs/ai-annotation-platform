import type { Ref, ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import type { DataManagerSection } from "./dataManagerUrlState";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

const SECTIONS: Array<{ key: DataManagerSection; label: string; hint: string }> = [
  { key: "overview", label: "项目概览", hint: "交付与质量" },
  { key: "data", label: "数据浏览", hint: "任务、对象、轨迹" },
  { key: "members", label: "成员绩效", hint: "产出与瓶颈" },
];

export function DataManagerFrame({
  projectId,
  projectName,
  projectDisplayId,
  section,
  onSectionChange,
  canViewMembers = false,
  dataScrollRef,
  children,
}: {
  projectId: string;
  projectName: string;
  projectDisplayId: string;
  section: DataManagerSection;
  onSectionChange: (section: DataManagerSection) => void;
  canViewMembers?: boolean;
  dataScrollRef?: Ref<HTMLDivElement>;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  // The Data section uses the frame root as the page-level scroll owner so the
  // natural-height task/entity results can flow past the viewport. Overview and
  // Members keep their own constrained scroll containers.
  const pageScrolling = section === "data";

  return (
    <div
      ref={pageScrolling ? dataScrollRef : undefined}
      data-dm-scroll-owner={pageScrolling ? "data" : undefined}
      className={cn(
        "mx-auto flex h-full min-h-0 max-w-[1800px] flex-col px-4 pt-2 pb-3 text-foreground md:px-6",
        pageScrolling ? "overflow-y-auto" : "overflow-hidden",
      )}
    >
      <header className="shrink-0 border-b border-border pb-2">
        <div className="flex items-start justify-between gap-4 max-md:flex-col">
          <div className="min-w-0">
            <button
              type="button"
              className="mb-1 inline-flex min-h-7 items-center gap-1 border-0 bg-transparent p-0 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() =>
                navigate(canViewMembers ? `/projects/${projectId}/settings` : "/dashboard")
              }
            >
              <Icon name="chevLeft" size={12} />
              {canViewMembers ? "返回项目设置" : "返回项目总览"}
            </button>
            <div className="flex min-w-0 items-baseline gap-2">
              <h1 className="truncate text-lg font-semibold tracking-tight">{projectName}</h1>
              <span className="shrink-0 font-mono text-xs text-muted-foreground">
                {projectDisplayId}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">Data Manager · 项目工作面</p>
          </div>
          <nav aria-label="Data Manager 项目区域" className="flex shrink-0 gap-1 max-md:w-full">
            {SECTIONS.filter((item) => item.key !== "members" || canViewMembers).map((item) => {
              const active = item.key === section;
              return (
                <button
                  key={item.key}
                  type="button"
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "min-h-10 rounded-md border border-transparent px-3 py-1.5 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring max-md:flex-1",
                    active ? "border-border bg-muted text-foreground" : "text-muted-foreground",
                  )}
                  onClick={() => onSectionChange(item.key)}
                >
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span
                    className={cn(
                      "mt-0.5 block text-2xs",
                      active ? "text-foreground" : "text-muted-foreground",
                    )}
                  >
                    {item.hint}
                  </span>
                </button>
              );
            })}
          </nav>
        </div>
      </header>
      <div className={cn("flex flex-col pt-3", !pageScrolling && "min-h-0 flex-1")}>{children}</div>
    </div>
  );
}
