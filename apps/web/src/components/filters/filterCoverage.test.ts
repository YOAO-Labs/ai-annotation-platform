import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Presentation ownership gate: every audited surface must opt into the shared
// controls explicitly. Page interaction suites continue to own query semantics.
const surfaces = [
  ["project administrator", "pages/Dashboard/DashboardPage.tsx", "FilterGroup"],
  ["super administrator", "pages/Dashboard/AdminProjectsDashboard.tsx", "FilterGroup"],
  ["viewer projects", "pages/Dashboard/ViewerDashboard.tsx", "FilterGroup"],
  ["datasets", "pages/Datasets/DatasetsPage.tsx", "FilterGroup"],
  ["templates", "pages/ProjectTemplates/ProjectTemplatesPage.tsx", "FilterGroup"],
  ["annotation queue", "pages/Annotate/AnnotatePage.tsx", "FilterGroup"],
  ["bug reports", "pages/Bugs/BugsPage.tsx", "FilterGroup"],
  ["review assignee", "pages/Review/ReviewPage.tsx", "FilterGroup"],
  ["people analysis", "pages/Admin/AdminPeoplePage.tsx", "FilterGroup"],
  ["analytics range", "pages/Admin/AnalyticsPage.tsx", "FilterGroup"],
  ["model catalog", "pages/ModelMarket/capability/FilterToolbar.tsx", "FilterGroup"],
  ["registry status", "pages/ModelMarket/registry/ServicePoolsSection.tsx", "FilterGroup"],
  ["registry diagnostics", "pages/ModelMarket/registry/IssueCenter.tsx", "FilterPanel"],
  ["image preannotation jobs", "pages/AIPreAnnotate/AIPreAnnotateJobsPage.tsx", "FilterGroup"],
  ["video tracker jobs", "pages/ModelMarket/VideoTrackerJobsPage.tsx", "FilterGroup"],
  ["members", "pages/Users/UsersPage.tsx", "FilterPanel"],
  ["invitations", "components/users/InvitationListPanel.tsx", "FilterPanel"],
  ["audit", "pages/Audit/AuditPage.tsx", "FilterPanel"],
  ["task batch scope", "pages/Workbench/shell/TaskQueuePanel.tsx", "FilterGroup"],
  ["AI frame/source/confidence", "pages/Workbench/shell/AIInspectorPanel.tsx", "FilterGroup"],
  ["offline queue", "pages/Workbench/shell/OfflineQueueDrawer.tsx", "FilterGroup"],
  ["discussion issues", "pages/Workbench/shell/DiscussionIssuesTab.tsx", "FilterGroup"],
  ["comment read scope", "pages/Workbench/shell/CommentsPanel.tsx", "FilterGroup"],
  ["mask QC", "pages/Workbench/shell/MaskQcPanel.tsx", "FilterGroup"],
  ["point-cloud QC", "pages/Workbench/stages/three-d/PointCloudQualityPanel.tsx", "FilterGroup"],
  ["notifications", "components/shell/NotificationsPopover.tsx", "FilterGroup"],
  ["jobs center", "components/shell/JobsBell.tsx", "FilterGroup"],
  [
    "complex expressions",
    "pages/Projects/data-manager/DataManagerExpressionEditor.tsx",
    "FilterSelect",
  ],
] as const;

describe("shared filter presentation coverage", () => {
  it.each(surfaces)("%s uses its shared filter surface", (_name, path, component) => {
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../..", path),
      "utf8",
    );
    expect(source.includes("@/components/filters/")).toBe(true);
    expect(new RegExp(`<${component}\\b`).test(source)).toBe(true);
  });
});
