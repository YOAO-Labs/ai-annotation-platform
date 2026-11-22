import { useRef } from "react";
import { useElementStyle } from "@/components/ui/useElementStyle";
import {
  useSidebarLayout,
  clampSidebarWidth,
  SIDEBAR_MIN,
  SIDEBAR_MAX,
  SIDEBAR_COLLAPSED,
} from "./useSidebarLayout";
import { NavLink } from "react-router-dom";
import { clsx } from "clsx";
import { cn } from "@/lib/utils";
import { Icon } from "@/components/ui/Icon";
import { usePermissions } from "@/hooks/usePermissions";
import { useFailedPredictions } from "@/hooks/useFailedPredictions";
import { useAdminStats } from "@/hooks/useDashboard";
import type { PageKey } from "@/types";
import type { IconName } from "@/components/ui/Icon";

const NAV_ITEM_CLASS =
  "flex items-center gap-2.5 rounded-md border border-transparent bg-transparent px-2.5 py-1.5 text-sm font-normal text-muted-foreground no-underline cursor-pointer select-none transition-[background-color,border-color,color,box-shadow,transform] duration-200 hover:-translate-y-px hover:bg-accent hover:text-foreground active:translate-y-0 active:scale-[0.99] focus-visible:ring-[3px] focus-visible:ring-ring/20";
const NAV_ITEM_ACTIVE_CLASS = "border-border bg-card font-medium text-foreground surface-shadow-sm";
const BADGE_BASE =
  "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-1.5 text-2xs font-medium";
const BADGE_AI = "bg-status-info-soft text-status-info";
const BADGE_DANGER = "bg-status-danger-soft text-status-danger";

interface SidebarProps {
  reviewCount: number;
  drawer?: boolean;
}

interface NavItem {
  key: PageKey;
  path: string;
  icon: IconName;
  label: string;
  badge?: string;
}

const sectionsForRole = (isSuperAdmin: boolean): { label: string; items: NavItem[] }[] => [
  {
    label: "工作区",
    items: [
      ...(isSuperAdmin
        ? [
            {
              key: "dashboard" as PageKey,
              path: "/overview",
              icon: "dashboard" as IconName,
              label: "平台概览",
            },
            {
              key: "dashboard" as PageKey,
              path: "/dashboard",
              icon: "folderKanban" as IconName,
              label: "项目管理",
            },
          ]
        : [
            {
              key: "dashboard" as PageKey,
              path: "/dashboard",
              icon: "dashboard" as IconName,
              label: "项目总览",
            },
          ]),
      { key: "annotate", path: "/annotate", icon: "target", label: "标注工作" },
      { key: "review", path: "/review", icon: "clipboardCheck", label: "质检审核" },
    ],
  },
  {
    label: "智能",
    items: [
      { key: "ai-pre", path: "/ai-pre", icon: "sparkles", label: "AI 预标注" },
      { key: "model-market", path: "/model-market", icon: "bot", label: "模型市场" },
      { key: "training", path: "/training", icon: "listStart", label: "训练队列" },
    ],
  },
  {
    label: "管理",
    items: [
      { key: "datasets", path: "/datasets", icon: "layers", label: "数据集" },
      { key: "storage", path: "/storage", icon: "db", label: "存储管理" },
      { key: "project-templates", path: "/project-templates", icon: "book", label: "项目模板" },
      { key: "users", path: "/users", icon: "userRoundCog", label: "用户与权限" },
      { key: "audit", path: "/audit", icon: "shield", label: "审计日志" },
      {
        key: "admin-people" as PageKey,
        path: "/admin/people",
        icon: "chartPerformance" as IconName,
        label: "标注员绩效",
      },
      ...(isSuperAdmin
        ? [
            {
              key: "admin-analytics" as PageKey,
              path: "/admin/analytics",
              icon: "chartAnalytics" as IconName,
              label: "离线分析",
            },
          ]
        : []),
      ...(isSuperAdmin
        ? [
            {
              key: "admin-health" as PageKey,
              path: "/admin/health",
              icon: "activity" as IconName,
              label: "系统健康",
            },
          ]
        : []),
      ...(isSuperAdmin
        ? [{ key: "bugs" as PageKey, path: "/bugs", icon: "bug" as IconName, label: "BUG反馈" }]
        : []),
      { key: "my-performance", path: "/me/performance", icon: "gauge", label: "我的绩效" },
      { key: "settings", path: "/settings", icon: "settings", label: "设置" },
    ],
  },
];

export function Sidebar({ reviewCount, drawer = false }: SidebarProps) {
  const { width, collapsed: savedCollapsed, setLayout } = useSidebarLayout();
  const collapsed = !drawer && savedCollapsed;
  const drag = useRef<{ x: number; width: number } | null>(null);
  const sidebarRef = useElementStyle<HTMLDivElement>({
    width: drawer ? "100%" : collapsed ? SIDEBAR_COLLAPSED : width,
  });
  const { canAccessPage, hasAnyPermission, role } = usePermissions();
  const canSeeFailed = hasAnyPermission("ml-backend.manage");
  const failedQuery = useFailedPredictions(1, 1, false, canSeeFailed);
  const failedTotal = failedQuery.data?.total ?? 0;
  const adminStatsQ = useAdminStats(role === "super_admin");
  const preAnnotatedTotal = adminStatsQ.data?.pre_annotated_batches ?? 0;
  const navigationLabel = (item: NavItem) => {
    if (item.key === "review" && reviewCount > 0) {
      return `${item.label} · ${reviewCount} 待审核`;
    }
    if (item.key === "ai-pre") {
      return `${item.label} · ${preAnnotatedTotal} 待接管 · ${failedTotal} 失败`;
    }
    return item.label;
  };

  const sections = sectionsForRole(role === "super_admin");
  const visibleSections = sections
    .map((sec) => ({
      ...sec,
      items: sec.items.filter((item) => canAccessPage(item.key)),
    }))
    .filter((sec) => sec.items.length > 0);

  return (
    <aside
      ref={sidebarRef}
      aria-label="主导航"
      className="relative flex min-h-0 flex-col border-r border-border bg-card"
    >
      {!drawer && (
        <div
          className={clsx(
            "flex h-11 shrink-0 items-center px-2",
            collapsed ? "justify-center" : "justify-between",
          )}
        >
          {!collapsed && (
            <span className="px-2.5 text-xs font-medium text-muted-foreground">导航</span>
          )}
          <button
            type="button"
            aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
            aria-expanded={!collapsed}
            aria-controls="sidebar-navigation"
            title={collapsed ? "展开侧边栏" : "收起侧边栏"}
            className="flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground active:scale-95 focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() =>
              setLayout((previous) => ({ ...previous, collapsed: !previous.collapsed }))
            }
          >
            <Icon name="panelLeft" size={16} />
          </button>
        </div>
      )}
      <div
        id={drawer ? undefined : "sidebar-navigation"}
        className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-2 pb-3"
      >
        {visibleSections.map((sec) => (
          <div
            key={sec.label}
            className={clsx(collapsed && "border-b border-border py-2 last:border-0")}
          >
            <div
              className={clsx(
                "px-2.5 pb-1.5 pt-3.5 text-xs font-medium text-muted-foreground",
                collapsed && "sr-only",
              )}
            >
              {sec.label}
            </div>
            {sec.items.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                title={collapsed ? navigationLabel(item) : undefined}
                aria-label={navigationLabel(item)}
                className={({ isActive }) =>
                  cn(
                    NAV_ITEM_CLASS,
                    "relative flex-wrap",
                    collapsed && "justify-center px-0 py-2",
                    isActive && NAV_ITEM_ACTIVE_CLASS,
                  )
                }
              >
                <Icon name={item.icon} size={16} className="shrink-0 opacity-[0.85]" />
                <span className={clsx("whitespace-nowrap", collapsed && "sr-only")}>
                  {item.label}
                </span>
                {collapsed &&
                  ((item.key === "review" && reviewCount > 0) ||
                    (item.key === "ai-pre" && (preAnnotatedTotal > 0 || failedTotal > 0))) && (
                    <span className="absolute right-1 top-1 size-1.5 rounded-full bg-status-caution" />
                  )}
                {!collapsed && item.key === "review" && reviewCount > 0 && (
                  <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                    {reviewCount}
                  </span>
                )}
                {!collapsed && item.badge && (
                  <span className={clsx(BADGE_BASE, BADGE_AI, "ml-auto")}>{item.badge}</span>
                )}
                {!collapsed &&
                  item.key === "ai-pre" &&
                  (preAnnotatedTotal > 0 || failedTotal > 0) && (
                    <span className="ml-auto inline-flex gap-1">
                      {preAnnotatedTotal > 0 && (
                        <span
                          title={`${preAnnotatedTotal} 批 AI 预标完成、待人工接管`}
                          className={clsx(BADGE_BASE, BADGE_AI)}
                        >
                          {preAnnotatedTotal > 99 ? "99+" : preAnnotatedTotal} 待接管
                        </span>
                      )}
                      {failedTotal > 0 && (
                        <span
                          title={`${failedTotal} 条失败预测待处理`}
                          className={clsx(BADGE_BASE, BADGE_DANGER)}
                        >
                          {failedTotal > 99 ? "99+" : failedTotal} 失败
                        </span>
                      )}
                    </span>
                  )}
              </NavLink>
            ))}
          </div>
        ))}
      </div>
      {!drawer && !collapsed && (
        <div
          role="separator"
          tabIndex={0}
          aria-label="调整侧边栏宽度"
          aria-orientation="vertical"
          aria-valuemin={SIDEBAR_MIN}
          aria-valuemax={SIDEBAR_MAX}
          aria-valuenow={width}
          title="拖动调整宽度，双击恢复默认；方向键微调"
          className="absolute inset-y-0 -right-1 z-base w-2 touch-none cursor-col-resize select-none hover:bg-brand/20 focus-visible:bg-brand/20 focus-visible:outline-none"
          onDoubleClick={() => setLayout((previous) => ({ ...previous, width: 220 }))}
          onPointerDown={(event) => {
            if (event.button !== 0) return;
            event.preventDefault();
            drag.current = { x: event.clientX, width };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (!drag.current) return;
            const next = clampSidebarWidth(drag.current.width + event.clientX - drag.current.x);
            setLayout((previous) => ({ ...previous, width: next }));
          }}
          onPointerUp={(event) => {
            drag.current = null;
            event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={() => {
            drag.current = null;
          }}
          onLostPointerCapture={() => {
            drag.current = null;
          }}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next =
              event.key === "Home"
                ? SIDEBAR_MIN
                : event.key === "End"
                  ? SIDEBAR_MAX
                  : width + (event.key === "ArrowLeft" ? -10 : 10);
            setLayout((previous) => ({ ...previous, width: clampSidebarWidth(next) }));
          }}
        />
      )}
    </aside>
  );
}
