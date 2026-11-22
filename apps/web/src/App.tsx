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
import { LoginPage } from "@/pages/Login/LoginPage";
import { ForgotPasswordPage } from "@/pages/Login/ForgotPasswordPage";
import { ResetPasswordPage } from "@/pages/Login/ResetPasswordPage";
import { VerifyEmailPage } from "@/pages/Login/VerifyEmailPage";
import { RegisterPage } from "@/pages/Register/RegisterPage";
import { UnauthorizedPage } from "@/pages/Unauthorized/UnauthorizedPage";

const DashboardPage = lazy(() =>
  import("@/pages/Dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const AdminDashboard = lazy(() =>
  import("@/pages/Dashboard/AdminDashboard").then((m) => ({ default: m.AdminDashboard })),
);
const AdminProjectsDashboard = lazy(() =>
  import("@/pages/Dashboard/AdminProjectsDashboard").then((m) => ({
    default: m.AdminProjectsDashboard,
  })),
);
const ViewerDashboard = lazy(() =>
  import("@/pages/Dashboard/ViewerDashboard").then((m) => ({ default: m.ViewerDashboard })),
);
const EmployeeDashboard = lazy(() =>
  import("@/pages/Dashboard/EmployeeDashboard").then((m) => ({ default: m.EmployeeDashboard })),
);

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
const DatasetDetailPage = lazy(() =>
  import("@/pages/Datasets/DatasetDetailPage").then((m) => ({ default: m.DatasetDetailPage })),
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
const ProjectLearningPage = lazy(() =>
  import("@/pages/Projects/ProjectLearningPage").then((m) => ({ default: m.ProjectLearningPage })),
);
const TrainingCreatePage = lazy(() =>
  import("@/pages/Training/TrainingCreatePage").then((m) => ({ default: m.TrainingCreatePage })),
);
const TrainingComparisonPage = lazy(() =>
  import("@/pages/Training/TrainingComparisonPage").then((m) => ({
    default: m.TrainingComparisonPage,
  })),
);
const TrainingExperimentPage = lazy(() =>
  import("@/pages/Training/TrainingExperimentPage").then((m) => ({
    default: m.TrainingExperimentPage,
  })),
);
const TrainingDatasetPage = lazy(() =>
  import("@/pages/Training/TrainingDatasetPage").then((m) => ({ default: m.TrainingDatasetPage })),
);
const TrainingModelPage = lazy(() =>
  import("@/pages/Training/TrainingModelPage").then((m) => ({ default: m.TrainingModelPage })),
);
const ModelLineagePage = lazy(() =>
  import("@/pages/Training/ModelLineagePage").then((m) => ({ default: m.ModelLineagePage })),
);
const SampleEvaluationPage = lazy(() =>
  import("@/pages/Training/SampleEvaluationPage").then((m) => ({
    default: m.SampleEvaluationPage,
  })),
);
const SampleEvaluationCreatePage = lazy(() =>
  import("@/pages/Training/SampleEvaluationCreate").then((m) => ({
    default: m.SampleEvaluationCreatePage,
  })),
);
const TrainingPage = lazy(() =>
  import("@/pages/Training/TrainingPage").then((m) => ({ default: m.TrainingPage })),
);
const ActiveLearningPage = lazy(() =>
  import("@/pages/Training/ActiveLearningPage").then((m) => ({ default: m.ActiveLearningPage })),
);
const ActiveLearningCreatePage = lazy(() =>
  import("@/pages/Training/ActiveLearningCreate").then((m) => ({
    default: m.ActiveLearningCreatePage,
  })),
);
const ActiveLearningRoundPage = lazy(() =>
  import("@/pages/Training/ActiveLearningRound").then((m) => ({
    default: m.ActiveLearningRoundPage,
  })),
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

  useEffect(() => {
    patchFetchForBugCapture();
    initBugReportCapture();
  }, []);

  useHeartbeat();
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
      <PerfHud />
    </div>
  );
}

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
  useNotificationSocket();
  return (
    <DiscussionDraftProvider {...discussionSession}>
      <DecisionQueueAuthBoundary />
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
          <Route
            path="/me/performance"
            element={
              <RequirePagePermission pageKey="my-performance">
                <MyPerformancePage />
              </RequirePagePermission>
            }
          />
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
            path="/datasets/:datasetId"
            element={
              <RequirePagePermission pageKey="datasets">
                <DatasetDetailPage />
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
          <Route
            path="/model-market/video-jobs"
            element={<Navigate to="/ai-pre/jobs?tab=video" replace />}
          />
          <Route
            path="/training"
            element={
              <RequirePagePermission pageKey="training">
                <TrainingPage />
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
            path="/projects/:id/learning/lineage/:kind/:nodeId"
            element={
              <RequireProjectAccess capability="project.manage">
                <ModelLineagePage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/active-learning/new"
            element={
              <RequireProjectAccess capability="project.manage">
                <ActiveLearningCreatePage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/active-learning/:roundId"
            element={
              <RequireProjectAccess capability="project.manage">
                <ActiveLearningRoundPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/active-learning"
            element={
              <RequireProjectAccess capability="project.manage">
                <ActiveLearningPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/evaluations/new"
            element={
              <RequireProjectAccess capability="project.manage">
                <SampleEvaluationCreatePage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/evaluations/:evaluationId"
            element={
              <RequireProjectAccess capability="project.manage">
                <SampleEvaluationPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/evaluations"
            element={
              <RequireProjectAccess capability="project.manage">
                <SampleEvaluationPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/compare"
            element={
              <RequireProjectAccess capability="project.manage">
                <TrainingComparisonPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/experiments/:runId"
            element={
              <RequireProjectAccess capability="project.manage">
                <TrainingExperimentPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/data/:datasetId"
            element={
              <RequireProjectAccess capability="project.manage">
                <TrainingDatasetPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning/models/:modelId"
            element={
              <RequireProjectAccess capability="project.manage">
                <TrainingModelPage />
              </RequireProjectAccess>
            }
          />
          <Route
            path="/projects/:id/learning"
            element={
              <RequireProjectAccess capability="project.manage">
                <ProjectLearningPage />
              </RequireProjectAccess>
            }
          >
            <Route path="new" element={<TrainingCreatePage />} />
          </Route>
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
