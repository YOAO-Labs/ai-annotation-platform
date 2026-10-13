import { lazy, Suspense, useEffect, useState } from "react";
import { useQueryClient, useIsFetching } from "@tanstack/react-query";
import { clsx } from "clsx";
import { Icon } from "@/components/ui/Icon";
import { SearchInput } from "@/components/ui/SearchInput";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { useAuthStore } from "@/stores/authStore";
import { useLogout } from "@/hooks/useAuth";
import { useTheme } from "@/hooks/useTheme";
import type { IconName } from "@/components/ui/Icon";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import { PreannotateJobsBadge } from "./PreannotateJobsBadge";
import { JobsBell } from "./JobsBell";
import { useWhatsNewStore } from "./WhatsNewDialog";
import { CommandPalette } from "@/components/CommandPalette";
import { usePerfHudStore } from "@/components/PerfHud";
import { appVersion } from "@/utils/releaseNotes";

const NotificationsPopover = lazy(() =>
  import("./NotificationsPopover").then((module) => ({ default: module.NotificationsPopover })),
);

const ICON_BTN_CLASS =
  "inline-flex size-[30px] cursor-pointer appearance-none items-center justify-center rounded-md border border-transparent bg-transparent text-muted-foreground transition-[background-color,border-color,color,transform] duration-200 hover:-translate-y-px hover:bg-accent hover:text-foreground active:translate-y-0 active:scale-[0.96] focus-visible:ring-[3px] focus-visible:ring-ring/20";

interface TopBarProps {
  workspace: string;
  onWorkspaceChange?: () => void;
  /** 窄屏时显示 hamburger 按钮，点击打开 SidebarDrawer。 */
  showHamburger?: boolean;
  onOpenDrawer?: () => void;
}

export function TopBar({
  workspace,
  onWorkspaceChange,
  showHamburger = false,
  onOpenDrawer,
}: TopBarProps) {
  const user = useAuthStore((s) => s.user);
  const perfHudVisible = usePerfHudStore((s) => s.visible);
  const logout = useLogout();
  const qc = useQueryClient();
  const isFetching = useIsFetching();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { resolved, setTheme } = useTheme();

  // v0.7.2 · 全局 ⌘K / Ctrl+K 触发命令搜索
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
        // 在 input/textarea/contenteditable 内不拦截系统快捷键
        if (tag === "input" || tag === "textarea") return;
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleRefresh = () => {
    qc.invalidateQueries();
  };

  const nextTheme = resolved === "dark" ? "light" : "dark";
  const themeIcon: IconName = nextTheme === "dark" ? "moon" : "sun";
  const themeActionLabel = nextTheme === "dark" ? "切到夜间" : "切到日间";
  const themeTitle = `当前${resolved === "dark" ? "夜间" : "日间"}，${themeActionLabel}`;

  return (
    <>
      <header className="surface-shadow-sm z-base col-[1/-1] flex items-center justify-between border-b border-border bg-card/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-card/85">
        <div className="flex min-w-0 shrink-0 items-center gap-6">
          {showHamburger && (
            <button
              type="button"
              title="打开导航菜单"
              aria-label="打开导航菜单"
              onClick={onOpenDrawer}
              className={ICON_BTN_CLASS}
            >
              <Icon name="menu" size={16} />
            </button>
          )}
          <div className="flex shrink-0 items-center gap-2 whitespace-nowrap text-sm font-semibold tracking-[0.01em]">
            <img
              src="/ai-annotation-platform-icon.svg"
              alt=""
              aria-hidden="true"
              className="size-[22px] shrink-0 rounded-md"
            />
            <span>标注中心</span>
            {/* 版本号与登录后「本次更新」窗口联动:点击随时查看当前版本更新内容。 */}
            <button
              type="button"
              title="查看本次更新内容"
              aria-label={`查看 v${appVersion} 更新内容`}
              onClick={() => useWhatsNewStore.getState().openManually()}
              className="ml-1 cursor-pointer rounded-xs text-xs font-normal text-muted-foreground transition-colors duration-200 hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
            >
              v{appVersion}
            </button>
          </div>
          <div
            onClick={onWorkspaceChange}
            className={clsx(
              "flex shrink-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded-md border border-border bg-muted py-1 pl-2 pr-2.5 text-xs text-muted-foreground transition-[background-color,border-color,color,transform] duration-200 hover:-translate-y-px hover:bg-accent hover:text-foreground active:translate-y-0 active:scale-[0.98]",
              showHamburger && "hidden",
            )}
          >
            <span className="size-1.5 rounded-full bg-positive" />
            <span>{workspace}</span>
            <Icon name="chevDown" size={12} />
          </div>
        </div>

        <div className={clsx("flex min-w-0 flex-1 justify-center px-3", showHamburger && "hidden")}>
          <SearchInput
            placeholder="搜索项目、任务、数据集、成员..."
            width={360}
            kbd="⌘K"
            onClick={() => setPaletteOpen(true)}
            readOnly
          />
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {/* 刷新按钮 */}
          <button type="button" title="刷新" onClick={handleRefresh} className={ICON_BTN_CLASS}>
            <Icon
              name="refresh"
              size={15}
              className={isFetching > 0 ? "animate-spin" : undefined}
            />
          </button>

          {/* 主题切换 */}
          <button
            type="button"
            title={themeTitle}
            aria-label={themeTitle}
            onClick={() => setTheme(nextTheme)}
            className={clsx(ICON_BTN_CLASS, "text-foreground")}
          >
            <Icon name={themeIcon} size={15} />
          </button>

          {/* v0.9.11 PerfHud · 性能监控浮窗 toggle (admin only, 快捷键 Ctrl+Shift+P 同步) */}
          {user?.role === "super_admin" || user?.role === "project_admin" ? (
            <button
              type="button"
              title="性能监控 (Ctrl+Shift+P)"
              onClick={() => usePerfHudStore.getState().toggle()}
              aria-label="切换性能监控浮窗"
              aria-haspopup="dialog"
              aria-expanded={perfHudVisible}
              aria-controls={perfHudVisible ? "shell-popover-performance" : undefined}
              data-shell-popover-trigger="performance"
              className={clsx(ICON_BTN_CLASS, perfHudVisible && "border-border bg-muted")}
            >
              <Icon name="activity" size={15} />
            </button>
          ) : null}

          {/* v0.9.8 · 全局预标 job 徽章 (admin only, 0 个时隐身) */}
          <PreannotateJobsBadge />

          {/* v0.10.16 · 后台异步任务铃铛（all users, polling /async-jobs） */}
          <JobsBell />

          {/* 通知按钮（v0.7.6：组件自包含 trigger + popover，TopBar 不再管 open state） */}
          <Suspense
            fallback={
              <button type="button" aria-label="通知加载中" disabled className={ICON_BTN_CLASS}>
                <Icon name="bell" size={15} />
              </button>
            }
          >
            <NotificationsPopover />
          </Suspense>

          <div className="flex cursor-pointer items-center gap-2 rounded-lg py-1 pl-1 pr-2.5 transition-[background-color,transform] duration-200 hover:-translate-y-px hover:bg-accent active:translate-y-0 active:scale-[0.98]">
            <UserAvatar user={user ?? { name: "?" }} size="sm" />
            <div
              className={clsx(
                "flex flex-col items-start leading-[1.2] whitespace-nowrap",
                showHamburger && "hidden",
              )}
            >
              <span className="text-xs font-medium">{user?.name ?? "—"}</span>
              <span className="text-2xs text-muted-foreground">{user?.role ?? "—"}</span>
            </div>
          </div>
          <button
            type="button"
            title="退出登录"
            onClick={() => void logout.requestLogout()}
            disabled={logout.isLoggingOut}
            className={ICON_BTN_CLASS}
          >
            <Icon name="logout" size={15} />
          </button>
        </div>
      </header>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      <AlertDialog
        open={logout.prompt !== null}
        onOpenChange={(open) => {
          if (!open) logout.cancelLogout();
        }}
      >
        <AlertDialogContent data-testid="logout-offline-prompt">
          <AlertDialogHeader>
            <AlertDialogTitle>退出前处理离线操作</AlertDialogTitle>
            <AlertDialogDescription>
              {logout.prompt?.pendingCount == null
                ? "暂时无法确认本机待同步记录。"
                : `当前账号还有 ${logout.prompt.pendingCount} 条待同步操作。`}
              请选择如何处理；保留本机记录不会删除这些操作，重新登录当前账号后仍可继续同步。
            </AlertDialogDescription>
          </AlertDialogHeader>
          {logout.syncError && (
            <div
              role="alert"
              data-testid="logout-sync-error"
              className="rounded-md border border-status-danger/30 bg-status-danger-soft px-3 py-2 text-sm text-status-danger"
            >
              {logout.syncError}
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={logout.isLoggingOut}>继续使用</AlertDialogCancel>
            <AlertDialogAction
              variant="outline"
              disabled={logout.isLoggingOut}
              data-testid="logout-keep-queue"
              onClick={(event) => {
                event.preventDefault();
                void logout.confirmLogout("keep");
              }}
            >
              保留记录并退出
            </AlertDialogAction>
            <AlertDialogAction
              disabled={logout.isLoggingOut}
              data-testid="logout-sync-queue"
              onClick={(event) => {
                event.preventDefault();
                void logout.confirmLogout("sync");
              }}
            >
              {logout.isLoggingOut ? "处理中…" : "同步并退出"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
