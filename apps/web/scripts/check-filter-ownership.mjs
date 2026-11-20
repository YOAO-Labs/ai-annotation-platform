import { dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

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
];

const src = fileURLToPath(new URL("../src/", import.meta.url));
const filters = resolve(src, "components/filters");
const program = ts.createProgram(
  surfaces.map(([, path]) => resolve(src, path)),
  { jsx: ts.JsxEmit.Preserve, noResolve: true, noLib: true, types: [] },
);
const checker = program.getTypeChecker();

function importsControl(tag, control) {
  const qualified = ts.isPropertyAccessExpression(tag);
  const local = qualified ? tag.expression : tag;
  if (!ts.isIdentifier(local) || (!qualified && /^[a-z]/.test(local.text))) return false;
  const binding = checker.getSymbolAtLocation(local)?.declarations?.[0];
  if (!binding) return false;
  const exported =
    qualified && ts.isNamespaceImport(binding)
      ? tag.name.text
      : !qualified && ts.isImportSpecifier(binding) && !binding.isTypeOnly
        ? (binding.propertyName ?? binding.name).text
        : undefined;
  if (exported !== control) return false;
  const declaration = ts.findAncestor(binding, ts.isImportDeclaration);
  if (!declaration || declaration.importClause.isTypeOnly) return false;
  const source = declaration.moduleSpecifier.text;
  const target = source.startsWith("@/")
    ? resolve(src, source.slice(2))
    : source.startsWith(".")
      ? resolve(dirname(declaration.getSourceFile().fileName), source)
      : undefined;
  return target === filters || target?.startsWith(filters + sep);
}

function usesControl(node, control) {
  if (
    (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
    importsControl(node.tagName, control)
  )
    return true;
  return ts.forEachChild(node, (child) => usesControl(child, control));
}

for (const [surface, path, control] of surfaces) {
  const source = program.getSourceFile(resolve(src, path));
  if (!source || !usesControl(source, control)) {
    console.error(`${path}: ${surface} must render the shared ${control}`);
    process.exitCode = 1;
  }
}
if (!process.exitCode)
  console.log(`Verified shared filter ownership on ${surfaces.length} surfaces.`);
