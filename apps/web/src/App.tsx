import { useState, useEffect, lazy, useRef, Suspense } from "react";
import { Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { TopBar } from "@/components/shell/TopBar";
import { WhatsNewDialog } from "@/components/shell/WhatsNewDialog";
import { PerfHud, usePerfHudStore } from "@/components/PerfHud";
import { useNotificationSocket } from "@/hooks/useNotificationSocket";
import { useHeartbeat } from "@/hooks/useHeartbeat";
import { Sidebar } from "@/components/shell/Sidebar";
import { SidebarDrawer } from "@/components/shell/SidebarDrawer";
import { ToastRack, useToastStore } from "@/components/ui/Toast";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
// 仪表盘 / 登录 类首屏关键路径：保持同步加载（避免 Suspense 闪烁）
import { DashboardPage } from "@/pages/Dashboard/DashboardPage";
import { AdminDashboard } from "@/pages/Dashboard/AdminDashboard";
import { AdminProjectsDashboard } from "@/pages/Dashboard/AdminProjectsDashboard";
import { ViewerDashboard } from "@/pages/Dashboard/ViewerDashboard";
import { EmployeeDashboard } from "@/pages/Dashboard/EmployeeDashboard";
import { LoginPage } from "@/pages/Login/LoginPage";
import { ForgotPasswordPage } from "@/pages/Login/ForgotPasswordPage";
import { ResetPasswordPage } from "@/pages/Login/ResetPasswordPage";
import { VerifyEmailPage } from "@/pages/Login/VerifyEmailPage";
import { RegisterPage } from "@/pages/Register/RegisterPage";
import { UnauthorizedPage } from "@/pages/Unauthorized/UnauthorizedPage";

// v0.6.6 · 重型 / 非首屏页面 lazy-load，让登录 / 仪表盘不下载 konva 等
const WorkbenchPage = lazy(() =>
  import("@/pages/Workbench/WorkbenchPage").then((m) => ({ default: m.WorkbenchPage })),
);
const UsersPage = lazy(() =>
  import("@/pages/Users/UsersPage").then((m) => ({ default: m.UsersPage })),
);
const ReviewPage = lazy(() =>
  import("@/pages/Review/ReviewPage").then((m) => ({ default: m.ReviewPage })),
);
const AnnotatePage = lazy(() =>
  import("@/pages/Annotate/AnnotatePage").then((m) => ({ default: m.AnnotatePage })),
);
const DatasetsPage = lazy(() =>
  import("@/pages/Datasets/DatasetsPage").then((m) => ({ default: m.DatasetsPage })),
);
const StoragePage = lazy(() =>
  import("@/pages/Storage/StoragePage").then((m) => ({ default: m.StoragePage })),
);
const ProjectSettingsPage = lazy(() =>
  import("@/pages/Projects/ProjectSettingsPage").then((m) => ({ default: m.ProjectSettingsPage })),
);
const ProjectDataManagerPage = lazy(() =>
  import("@/pages/Projects/ProjectDataManagerPage").then((m) => ({
    default: m.ProjectDataManagerPage,
  })),
);
const ProjectTemplatesPage = lazy(() =>
  import("@/pages/ProjectTemplates/ProjectTemplatesPage").then((m) => ({
    default: m.ProjectTemplatesPage,
  })),
);
const AuditPage = lazy(() =>
  import("@/pages/Audit/AuditPage").then((m) => ({ default: m.AuditPage })),
);
const BugsPage = lazy(() => import("@/pages/Bugs/BugsPage").then((m) => ({ default: m.BugsPage })));
const BugReportDrawer = lazy(() =>
  import("@/components/bugreport/BugReportDrawer").then((m) => ({ default: m.BugReportDrawer })),
);
const SettingsPage = lazy(() =>
  import("@/pages/Settings/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);
const ModelMarketPage = lazy(() =>
  import("@/pages/ModelMarket/ModelMarketPage").then((m) => ({ default: m.ModelMarketPage })),
);
const AIPreAnnotatePage = lazy(() =>
  import("@/pages/AIPreAnnotate/AIPreAnnotatePage").then((m) => ({ default: m.default })),
);
const AIPreAnnotateLayout = lazy(() =>
  import("@/pages/AIPreAnnotate/AIPreAnnotateLayout").then((m) => ({ default: m.default })),
);
const AIPreAnnotateJobsPage = lazy(() =>
  import("@/pages/AIPreAnnotate/AIPreAnnotateJobsPage").then((m) => ({ default: m.default })),
);
const GlobalPipelineLibraryPage = lazy(() =>
  import("@/pages/AIPreAnnotate/GlobalPipelineLibraryPage").then((m) => ({ default: m.default })),
);
const AdminPeoplePage = lazy(() =>
  import("@/pages/Admin/AdminPeoplePage").then((m) => ({ default: m.AdminPeoplePage })),
);
const AnalyticsPage = lazy(() =>
  import("@/pages/Admin/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })),
);
const SystemHealthPage = lazy(() =>
  import("@/pages/Admin/SystemHealthPage").then((m) => ({ default: m.SystemHealthPage })),
);
const MyPerformancePage = lazy(() =>
  import("@/pages/Me/MyPerformancePage").then((m) => ({ default: m.MyPerformancePage })),
);
import { RequireAuth } from "@/components/routing/RequireAuth";
import { RequirePagePermission } from "@/components/routing/RequirePagePermission";
import { RequireProjectAccess } from "@/components/routing/RequireProjectAccess";
import { useAuthStore } from "@/stores/authStore";
import { usePermissions } from "@/hooks/usePermissions";
import { useAppStore } from "@/stores/appStore";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { BugReportFAB } from "@/components/bugreport/BugReportFAB";
import { useFabAutoHideDriver } from "@/stores/fabRevealStore";
import { initBugReportCapture, patchFetchForBugCapture } from "@/utils/bugReportCapture";
import { PageLoader } from "@/components/PageLoader";
import { useBugDrawerStore } from "@/stores/bugDrawerStore";
import { defaultHomePath } from "@/utils/authRedirect";
import { DiscussionDraftProvider } from "@/pages/Workbench/state/DiscussionDraftProvider";
import { useAuthenticatedDiscussionSession } from "@/pages/Workbench/state/useAuthenticatedDiscussionSession";
import styles from "./App.module.css";

function DashboardRouter() {
  const { role } = usePermissions();
  switch (role) {
    case "super_admin":
      return <AdminProjectsDashboard />;
    case "project_admin":
      return <DashboardPage />;
    case "employee":
      return <EmployeeDashboard />;
    case "viewer":
      return <ViewerDashboard />;
    default:
      return <DashboardPage />;
  }
}

function AdminOverviewRoute() {
  const { role } = usePermissions();
  if (role !== "super_admin") {
    return <Navigate to="/unauthorized" replace />;
  }
  return <AdminDashboard />;
}

function RedirectWithSearch({ to }: { to: string }) {
  const { search } = useLocation();
  return <Navigate to={`${to}${search}`} replace />;
}

// super_admin 默认主页是「平台概览」(/overview);其余角色进 /dashboard。
function DefaultLandingRedirect() {
  const { role } = usePermissions();
  return <Navigate to={defaultHomePath(role)} replace />;
}

function AppShell() {
  const workspace = useAppStore((s) => s.workspace);
  const pushToast = useToastStore((s) => s.push);
  const compact = useMediaQuery("(max-width: 1023px)");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const bugDrawerOpen = useBugDrawerStore((s) => s.open);
  const bugDrawerFocusId = useBugDrawerStore((s) => s.focusBugId);
  const openBugDrawer = useBugDrawerStore((s) => s.openDrawer);
  const closeBugDrawer = useBugDrawerStore((s) => s.close);

  // 初始化 bug 反馈自动捕获
  useEffect(() => {
    patchFetchForBugCapture();
    initBugReportCapture();
  }, []);

  useHeartbeat();
  // v0.9.11 PerfHud · 全局快捷键 Ctrl+Shift+P 切换性能监控浮窗 (super_admin/project_admin only).
  // 权限 gating 在 PerfHud 组件内做; 此 listener 只负责 toggle store.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === "P" || e.key === "p")) {
        const target = e.target as HTMLElement | null;
        const tag = target?.tagName?.toLowerCase();
        if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;
        e.preventDefault();
        usePerfHudStore.getState().toggle();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className={compact ? `${styles.appShell} ${styles.appShellCompact}` : styles.appShell}>
      <TopBar
        workspace={workspace}
        onWorkspaceChange={() => pushToast({ msg: "切换工作区面板已展开" })}
        showHamburger={compact}
        onOpenDrawer={() => setDrawerOpen(true)}
      />
      {compact ? (
        <>
          <aside className={styles.hiddenSidebarSlot} aria-hidden="true" />
          <SidebarDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)}>
            <Sidebar reviewCount={0} drawer />
          </SidebarDrawer>
        </>
      ) : (
        <Sidebar reviewCount={0} />
      )}
      <main className={styles.main}>
        <Suspense fallback={<PageLoader />}>
          <Outlet />
        </Suspense>
      </main>
      <ToastRack />
      {/* 决策对话框宿主:与 ToastRack 同位双挂载,全屏工作台路由也覆盖 */}
      <DecisionDialogHost />
      <BugReportFAB onClick={() => openBugDrawer()} />
      {bugDrawerOpen && (
        <Suspense fallback={null}>
          <BugReportDrawer
            open={bugDrawerOpen}
            focusBugId={bugDrawerFocusId}
            onClose={closeBugDrawer}
          />
        </Suspense>
      )}
      {/* v0.9.11 PerfHud · 全局浮窗 (内部按角色 gating + visibility store 控制渲染) */}
      <PerfHud />
    </div>
  );
}

function PlaceholderPage({ title }: { title: string }) {
  return (
    <div className={styles.placeholder}>
      <div className={styles.placeholderIcon}>🚧</div>
      <h2 className={styles.placeholderTitle}>{title}</h2>
      <p className={styles.placeholderText}>此功能模块正在开发中</p>
    </div>
  );
}

/**
 * decisionDialog 队列的认证归属边界:队列是全局 Zustand,而挂载 Host 的壳在登出时
 * 会被 RequireAuth 拆掉,排队中的请求却残留下来;换账号再登录后旧对话框会重新出现,
 * 确认它可能用新凭据执行旧调用方捕获的变更。这里在认证归属变化(登出/换人)时
 * 以取消值结算并清空整条队列(plan 1789527942 评审意见,Codex P1)。
 */
function DecisionQueueAuthBoundary() {
  const ownerId = useAuthStore((s) => s.user?.id ?? null);
  const prevOwnerRef = useRef(ownerId);
  useEffect(() => {
    if (prevOwnerRef.current === ownerId) return;
    prevOwnerRef.current = ownerId;
    useDecisionDialogStore.getState().cancelAll();
  }, [ownerId]);
  return null;
}

function FullScreenWorkbench({ mode }: { mode?: "annotate" | "review" }) {
  const tooNarrow = useMediaQuery("(max-width: 767px)");
  const bugDrawerOpen = useBugDrawerStore((s) => s.open);
  const bugDrawerFocusId = useBugDrawerStore((s) => s.focusBugId);
  const closeBugDrawer = useBugDrawerStore((s) => s.close);

  useEffect(() => {
    patchFetchForBugCapture();
    initBugReportCapture();
  }, []);
  useHeartbeat();
  // 右下角按钮列日常隐藏:光标进右下角指定区域才露出。
  useFabAutoHideDriver();

  return (
    <div className={styles.fullScreenWorkbench}>
      <Suspense fallback={<PageLoader />}>
        <WorkbenchPage mode={mode} />
      </Suspense>
      <ToastRack />
      <DecisionDialogHost />
      {bugDrawerOpen && (
        <Suspense fallback={null}>
          <BugReportDrawer
            open={bugDrawerOpen}
            focusBugId={bugDrawerFocusId}
            onClose={closeBugDrawer}
          />
        </Suspense>
      )}
      {tooNarrow && <MobileWorkbenchBlock />}
    </div>
  );
}

function MobileWorkbenchBlock() {
  return (
    <div role="dialog" aria-modal="true" className={styles.mobileWorkbenchBlock}>
      <div className={styles.mobileWorkbenchIcon}>🖥️</div>
      <h2 className={styles.mobileWorkbenchTitle}>请切换到桌面端</h2>
      <p className={styles.mobileWorkbenchText}>
        标注工作台依赖快捷键、画布鼠标交互和大屏侧栏。当前屏幕小于
        768px，仅以只读方式展示，避免误操作。
      </p>
      <div className={styles.mobileWorkbenchHint}>建议宽度 ≥ 1024px</div>
    </div>
  );
}

export function App() {
  const discussionSession = useAuthenticatedDiscussionSession();
  // v0.9 · 通知 WS 挂在 App 生命周期：主界面与全屏标注/审核工作台共用一条
  // 个人通知连接，路由切换不拆链；登出/换账号由 hook 清理（token/userId 依赖）。
  useNotificationSocket();
  return (
    <DiscussionDraftProvider {...discussionSession}>
      <DecisionQueueAuthBoundary />
      {/* 版本更新提醒:登录/会话恢复后每版本弹一次,内部按账号已读版本(服务端)gate。 */}
      <WhatsNewDialog />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />
        <Route path="/verify-email" element={<VerifyEmailPage />} />

        <Route
          path="/projects/:id/annotate"
          element={
            <RequireAuth>
              <RequireProjectAccess capability="annotation.write">
                <FullScreenWorkbench />
              </RequireProjectAccess>
            </RequireAuth>
          }
        />
        <Route
          path="/projects/:id/review"
          element={
            <RequireAuth>
              <RequireProjectAccess capability="review.write">
                <FullScreenWorkbench mode="review" />
              </RequireProjectAccess>
            </RequireAuth>
          }
        />
        <Route
          path="/projects/:id/data-manager"
          element={
            <RequireAuth>
              <RequireProjectAccess capability="project.read">
                <AppShell />
              </RequireProjectAccess>
            </RequireAuth>
          }
        >
          <Route index element={<ProjectDataManagerPage />} />
        </Route>

        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<DefaultLandingRedirect />} />
          <Route path="/dashboard" element={<DashboardRouter />} />
          <Route path="/overview" element={<AdminOverviewRoute />} />
          <Route path="/projects" element={<RedirectWithSearch to="/dashboard" />} />
          <Route
            path="/admin/people"
            element={
              <RequirePagePermission pageKey="admin-people">
                <AdminPeoplePage />
              </RequirePagePermission>
            }
          />
          {/* v0.10.16 · DuckDB 离线分析面板（super_admin only） */}
          <Route
            path="/admin/analytics"
            element={
              <RequirePagePermission pageKey="admin-analytics">
                <AnalyticsPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/admin/health"
            element={
              <RequirePagePermission pageKey="admin-health">
                <SystemHealthPage />
              </RequirePagePermission>
            }
          />
          {/* v0.12.3 · 我的绩效（所有角色，自助自视） */}
          <Route
            path="/me/performance"
            element={
              <RequirePagePermission pageKey="my-performance">
                <MyPerformancePage />
              </RequirePagePermission>
            }
          />
          {/* v0.9.3 phase 2 · /admin/failed-predictions 与 /admin/ml-integrations 已合并到 /model-market */}
          <Route
            path="/review"
            element={
              <RequirePagePermission pageKey="review">
                <ReviewPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/annotate"
            element={
              <RequirePagePermission pageKey="annotate">
                <AnnotatePage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/users"
            element={
              <RequirePagePermission pageKey="users">
                <UsersPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/datasets"
            element={
              <RequirePagePermission pageKey="datasets">
                <DatasetsPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/storage"
            element={
              <RequirePagePermission pageKey="storage">
                <StoragePage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/ai-pre"
            element={
              <RequirePagePermission pageKey="ai-pre">
                <AIPreAnnotateLayout />
              </RequirePagePermission>
            }
          >
            <Route index element={<AIPreAnnotatePage />} />
            <Route path="pipelines" element={<GlobalPipelineLibraryPage />} />
            <Route path="jobs" element={<AIPreAnnotateJobsPage />} />
          </Route>
          <Route
            path="/model-market"
            element={
              <RequirePagePermission pageKey="model-market">
                <ModelMarketPage />
              </RequirePagePermission>
            }
          />
          {/* v0.10.38 · 视频追踪监控并入 /ai-pre/jobs 视频 tab (epic 阶段 3); 旧链接 301 到新址 */}
          <Route
            path="/model-market/video-jobs"
            element={<Navigate to="/ai-pre/jobs?tab=video" replace />}
          />
          <Route
            path="/training"
            element={
              <RequirePagePermission pageKey="training">
                <PlaceholderPage title="训练队列" />
              </RequirePagePermission>
            }
          />
          <Route
            path="/audit"
            element={
              <RequirePagePermission pageKey="audit">
                <AuditPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/bugs"
            element={
              <RequirePagePermission pageKey="bugs">
                <BugsPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/settings"
            element={
              <RequirePagePermission pageKey="settings">
                <SettingsPage />
              </RequirePagePermission>
            }
          />
          <Route
            path="/projects/:id/settings"
            element={
              <RequireProjectAccess capability="project.manage">
                <ProjectSettingsPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/project-templates"
            element={
              <RequirePagePermission pageKey="project-templates">
                <ProjectTemplatesPage />
              </RequirePagePermission>
            }
          />
          <Route path="/unauthorized" element={<UnauthorizedPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </DiscussionDraftProvider>
  );
}
