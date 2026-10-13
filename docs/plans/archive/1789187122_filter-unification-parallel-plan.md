# Unified filtering: full scope and parallel delivery plan

> Status: complete. Implementation, behavior acceptance, selected media review, documentation and cleanup are finished. The user authorized this rollout on 2026-09-12.
> Prepared: 2026-09-12.
> Baseline: feat/opt_260912 at 8ef762fb293cbc3b004c03bd615b9afb8e70b1a3.
> Owning checkout: /Users/yangyiqing/orca/workspaces/ai-annotation-platform/opt_260912.
> Release assignment: none. Maintainers decide release milestones and versions.

## 1. Outcome and scope

Make filtering consistent across the existing application while preserving each domain's meaning, authorization, pagination, and mutation rules. Cover Data Manager, operational lists, Workbench, model administration, shell panels, selectors, and SDK/CLI/TUI consumers. Every inventoried surface has an explicit migration or retention decision below.

The recommended design has three shared contracts: field editing and presentation; applied state and URL handling; structural expression validation. SQL execution, full-text search, geometry visibility, model eligibility, and write eligibility remain owned by their existing domains.

This is a rollout touching substantially more than eight files and spanning frontend, API, SDK, tests, and documentation. It is divided into twelve bounded work packages with an explicit dependency graph. Packages must remain usable if later packages never ship.

The smallest useful alternative is to fix Data Manager expression round trips and project-list status ownership only. That leaves the documented inconsistencies in other lists and panels. The full plan is recommended because the requested scope is project-wide, and actual duplicated implementations and behavioral defects have already been identified.

### Included

- Preserve existing root rules and nested AND/OR expressions through editing, URL restoration, saved views, and execution.
- Consolidate reusable field controls, value validation, applied-condition chips, clear/reset behavior, and request debouncing.
- Make URL-backed page state and local panel state explicit; avoid duplicate live owners.
- Keep filtering, pagination, statistics, export, and selected-item invalidation coherent within a domain.
- Align Workbench frame/source/review/hide rules and distinguish visible rows from actionable items.
- Align Data Manager schema, validation, query, counts, and match explanations.
- Preserve existing SDK and command contracts and test them against the final API behavior.
- Deliver user/developer documentation and real-browser evidence for affected workflows.

### Boundaries

- No universal SQL compiler across tasks, objects, tracks, users, audit records, and jobs.
- No new filtering service, database table, global Zustand store, dependency, environment variable, external account, or API key.
- No cross-resource saved-view service. Existing project task views remain scoped to tasks, objects, or tracks.
- No new Workbench expression language; its visibility and action predicates stay local to the Workbench domain.
- No changes to preannotation class_filter/parent_class_filter, backend capability eligibility, authorization policy, or task assignment policy merely because they use filtering internally.
- No version bump, release, push, PR merge, or public artifact publication.
- No data migration or index addition in this rollout. Query performance changes must first be supported by the measurements specified below.

## 2. Evidence and baseline findings

| Finding                                                                    | Evidence                                                                                                                                                                                                                                                      | Consequence                                                                             |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Task-lens restoration drops nested groups and rebuilds OR as AND           | [ProjectDataManagerPage.tsx](../../../apps/web/src/pages/Projects/ProjectDataManagerPage.tsx), editableRulesFromFilter and buildFilterJson; [views.py](../../../apps/api/app/services/data_management/views.py), AI-review and missing-required-attributes builtins | Existing builtins can execute different conditions from their saved-view counts         |
| Entity input normalizes during every keystroke                             | [EntityDataManagerLens.tsx](../../../apps/web/src/pages/Projects/data-manager/EntityDataManagerLens.tsx), normalizeValue and its controlled input                                                                                                                | Input 1, loses its comma; 1. loses its decimal point; clearing a number produces 0      |
| Two owners select project status                                           | [DashboardPage.tsx](../../../apps/web/src/pages/Dashboard/DashboardPage.tsx) and [AdminProjectsDashboard.tsx](../../../apps/web/src/pages/Dashboard/AdminProjectsDashboard.tsx), effectiveStatus                                                                    | Advanced status can override a later tab selection                                      |
| URL handling differs between pages                                         | [AuditPage.tsx](../../../apps/web/src/pages/Audit/AuditPage.tsx), [AdminPeoplePage.tsx](../../../apps/web/src/pages/Admin/AdminPeoplePage.tsx), [UsersPage.tsx](../../../apps/web/src/pages/Users/UsersPage.tsx)                                                       | Refresh, browser navigation, and shared links behave differently                        |
| Many text filters directly trigger queries                                 | Project, dataset, member, invitation, and job lists                                                                                                                                                                                                           | Extra requests; member/invitation searches can trigger both list and statistics queries |
| Bug list uses manual request completion without list-request ownership     | [BugsPage.tsx](../../../apps/web/src/pages/Bugs/BugsPage.tsx), loadList                                                                                                                                                                                          | A slower old list request can overwrite the latest filter result                        |
| Structural validation is duplicated and uneven                             | [task_filters.py](../../../apps/api/app/services/data_management/task_filters.py), [entity_filters.py](../../../apps/api/app/services/data_management/entity_filters.py), [views.py](../../../apps/api/app/services/data_management/views.py)                          | Child shape, field, operator, and IN-size checks can differ by route                    |
| Match explanation uses referenced-field presence                           | [service.py](../../../apps/api/app/services/data_management/service.py), matches and field probes                                                                                                                                                                | A field in a false OR branch can determine which entity families the drawer shows       |
| Frame/review filtering is repeated in canvas and rosters                   | [videoFrameViews.ts](../../../apps/web/src/pages/Workbench/stage/videoFrameViews.ts), [VideoTrackPanel.tsx](../../../apps/web/src/pages/Workbench/stage/VideoTrackPanel.tsx)                                                                                        | Rules can diverge between canvas, rows, and counts                                      |
| Workbench candidate display and batch mutation have different frame scopes | [useImageAnnotationActions.ts](../../../apps/web/src/pages/Workbench/stages/image/useImageAnnotationActions.ts), handleAcceptAll; [AIInspectorPanel.tsx](../../../apps/web/src/pages/Workbench/shell/AIInspectorPanel.tsx), frameFilter                             | A current-frame display must not implicitly redefine current-task batch acceptance      |

The planning session executed the original frontend conversion functions without modifying source. It confirmed OR-to-AND conversion, nested-group loss, and numeric/list intermediate-input loss. Other behavioral findings are static code evidence and require the regression/browser checks below; they are not presented as completed live acceptance.

Current implementation references take precedence over historical plans. The prior [Data Manager plan](2026-06-07-v0.14.8-data-manager-saved-views-filter-dsl.md) established the controlled DSL. The current [query reference](../../../docs-site/dev/reference/data-manager-query.md) establishes entity grains, visible-task authorization, and saved-view compatibility.

## 3. Complete surface disposition

Package identifiers are defined in section 6. A retention decision is part of full coverage, not an unassigned implementation task.

| Surface and entry points                                                                                    | Execution and state decision                                                                                                                   | Package      |
| ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| DashboardPage, AdminProjectsDashboard, FilterDrawer                                                         | One applied project-filter state; URL-backed; tabs and drawer edit the same status                                                             | F1           |
| DatasetsPage                                                                                                | Server search/type filters; URL-backed; preserve dataset deep-link expansion separately                                                        | F1           |
| ProjectTemplatesPage                                                                                        | Server scope/search; URL-backed; preserve existing tab-to-scope meanings                                                                       | F1           |
| UsersPage                                                                                                   | Server filters, count, statistics, and export; URL-backed member filters; changing filter clears cross-page selection                          | F2           |
| InvitationListPanel                                                                                         | Server filters/statistics/export; URL namespace separate from member filters                                                                   | F2           |
| AdminPeoplePage                                                                                             | Retain URL ownership and existing role/project/period/sort/q keys; normalize and debounce text                                                 | F2           |
| AuditPage                                                                                                   | Retain existing deep-link keys; all editable filters write back consistently; list/export share one applied parameter object                   | F2           |
| BugsPage                                                                                                    | Server status/severity filters; query-key isolation; retain existing list-size contract                                                        | F2           |
| Image preannotation jobs                                                                                    | Server status/search/project; preserve pagination and job deep links; use one applied scope per panel                                          | F3           |
| Preannotation HistoryTable                                                                                  | Local search/sort/page over the bounded queue snapshot; preserve explicitly accumulated selection                                              | F3           |
| VideoTrackerJobsPage                                                                                        | Server project/status/model filters; reset cursor history on query changes                                                                     | F3           |
| CapabilityCatalogPanel and capability/FilterToolbar                                                         | Local multi-select, OR within an axis and AND across axes; retain capability grouping                                                          | F3           |
| RegisteredBackendsTab and IssueCenter                                                                       | Local derived filters over loaded snapshots; preserve diagnostic identity and global-vs-filtered counts                                        | F3           |
| RuntimeObservePanel, registry GPU/project tabs                                                              | Retain disclosure/grouping controls; these are not query filters                                                                               | F3           |
| Admin Analytics (AnalyticsPage.tsx)                                                                         | Retain local days=30 default and 7/30/90 choices; existing four server aggregates share that range; no URL change                              | F3 retention |
| NotificationsPopover                                                                                        | Keep category filtering over loaded notifications; explicitly label this scope; retain loaded-page progress                                    | F3           |
| JobsBell                                                                                                    | Keep per-account display preference and dismissed IDs; retain session/auth ownership                                                           | F3           |
| UserPicker, ClassPalette, class-picker wrappers, settings/hotkey search                                     | Keep immediate local option filtering and keyboard behavior; reuse visual primitives only where needed                                         | F3 / W2      |
| Project batch lists, member assignment/distribution pickers, project-wizard selectors, connector allowlists | Keep local selection/eligibility and current API contracts; preserve empty-selection meaning per caller                                        | F1 / F3      |
| Data Manager tasks/objects/tracks, charts, matches, saved views                                             | Shared field controls plus intact existing expression tree; schema-driven; preserve version-1 URL envelope                                     | F0 / B0 / B1 |
| Annotate/Review task queues and batch selector                                                              | Server queue scope and cursor pagination; preserve rejected/redo and unbatched semantics                                                       | W2 / A0      |
| AIInspectorPanel, image candidate actions, current-task inference                                           | Distinct eligibility, source visibility, current-frame display, and current-task batch-action scopes                                           | W1           |
| Image/video/3D canvas and video rosters                                                                     | Shared domain predicates; keep hidden rows restorable and canvas geometry filtered                                                             | W0           |
| OfflineQueueDrawer                                                                                          | Local current-task/all and retry filters; absent task cannot widen a current-task action to all                                                | W2           |
| DiscussionIssuesTab and issue pins                                                                          | Server status filtering before pagination; badge scope explicit                                                                                | W2           |
| MaskQcPanel and PointCloudQualityPanel                                                                      | Existing server scope/status/severity/rule filters; keep repair eligibility separate                                                           | W2           |
| VideoTrackQualitySidebar                                                                                    | Retain geometry/segment/class/tool matching as action eligibility                                                                              | W2           |
| Python SDK, CLI, TUI                                                                                        | Preserve supported ordinary endpoint parameters and native filtering; compatibility verification only; no Data Manager methods currently exist | C0           |
| Data Manager API                                                                                            | Shared structural validation and schema capabilities, domain-specific SQL and evidence semantics                                               | B0 / B1      |
| Ordinary list/count/statistics/export APIs                                                                  | Reuse each domain's existing base query; preserve legacy endpoint visibility semantics                                                         | A0           |

Global search retains its existing search API and 200ms delay. TUI retains its documented 300ms delay. Web server-backed list text uses the current Data Manager convention of 250ms. Local small-list filtering remains immediate.

## 4. Shared contracts to freeze before dispatch

### 4.1 Field controls and presentation

New shared modules live under apps/web/src/components/filters/ and apps/web/src/lib/filters/. They are local application code, not a new package.

- FilterFieldDefinition uses the existing key, label, group, value_type, operators, and options vocabulary. Data Manager adapts its existing response without renaming wire fields. Domain metadata such as expensive, tool_unit_id, and attribute_key stays with the Data Manager adapter.
- FilterBar retains the current DataManagerFilterBar inputs: fields, chips, quickFilters, onAdd, and onClear. It owns presentation and field search, not queries or saved views.
- FilterValueEditor takes a field definition, operator, applied value, logical editor identity, commit callback, and draft-validity callback. The latter lets a parent disable saving while a draft is invalid.
- Draft values remain strings while edited. Finite-number conversion, complete range validation, array conversion, and boolean parsing occur only on a valid commit. Whitespace-only input is empty; zero and false are real values.
- Enum IN uses multiple selected values. Numeric/date between uses two labelled endpoints. Existing valid arbitrary text and values containing commas must survive saved-view restoration without a split/join round trip.
- Suggestions are not automatically exhaustive allowlists. Orphan class names and saved values absent from current options remain visible and removable; only the domain's actual contract can reject a value.
- During mixed frontend/backend rollout, a restored operator that exists in TaskFilterOp but is missing from an older schema's advertised operators is retained as a legacy condition and validated by the server. New operator choices use schema metadata. Do not reject an unchanged, valid saved operator merely because the old schema under-advertises support; B0 aligns that metadata.
- Invalid drafts show a field-local error and retain the last applied query. An invalid restored expression blocks that query rather than silently widening it. Missing/corrupt URL state follows the existing safe view fallback and shows a recovery notice.
- Changing an operator must revalidate its value; it cannot submit a scalar under IN or an incomplete pair under between.
- Basic text filters may apply after debounce; completed selects/checkboxes commit immediately. The project advanced-filter dialog keeps Apply/Cancel as a draft transaction.
- Reset labels reflect scope. Ordinary-page reset restores that page's documented defaults, including active users rather than all accounts. Data Manager's filter-bar clear removes structured/quick conditions while retaining the separate keyword, sort, and columns; label it "清除条件". It leaves the selected saved view dirty rather than overwriting it. The shared component invokes its owner callback and does not invent a global definition of "all".
- Use local Input/Popover/Select/Button adapters and semantic tokens. Preserve labels, keyboard access, focus return, aria-pressed for toggle chips, and supported responsive behavior.

Extract existing code and delete replaced duplication. Do not build plugin registration, arbitrary widget factories, or a shared form runtime.

### 4.2 Expression integrity

Data Manager continues to accept its current filter_json object shape, including an empty object, a root rule, and nested AND/OR groups.

- Reuse TaskFilterRule, TaskFilterGroup, and TaskFilterOp. Introduce an explicit expression union at frontend domain boundaries instead of treating internal state as an unstructured record.
- Preserve parent operators, child order, nesting, value types, and same-object grouping. Do not flatten AND groups: grouping also controls whether annotation predicates share one correlated EXISTS.
- Expose path-based rule update/remove operations. Editor identity is transient and is never persisted into filter_json.
- Keyword q is an additional top-level AND condition. Extract an existing keyword only when it is a standalone rule or an unambiguous direct conjunct; a keyword inside OR remains inside that tree.
- Quick filters and chart clicks add a conjunct around the current expression. Removing a quick condition removes only that condition, leaving saved OR/group structure intact.
- Simple flat expressions retain condition chips. Existing complex groups get a readable grouped summary and an explicit edit surface that preserves AND/OR boundaries. Editing existing groups, adding/removing rules and groups, and switching AND/OR are included; no raw JSON editor is introduced.
- Empty expressions keep the existing backend truth behavior. Removing the last rule results in the existing unfiltered representation. A temporary incomplete editor group is a draft, not a request.
- Saving and comparing dirty state use validated semantic values. Input-only IDs, open popovers, and pagination are excluded.

### 4.3 URL, state, and request ownership

Create a narrow useUrlFilterState helper under apps/web/src/hooks/ and a pure URL codec under apps/web/src/lib/filters/. Reuse React Router rather than introducing another URL library.

The helper returns decoded applied state, decode issues, and atomic patch/reset operations. The page supplies its owned keys, defaults, parsing/serialization, and dependent pagination reset. It does not own API hooks or make domain-specific default choices.

- The URL is the live owner on page-sized query surfaces. Local state is reserved for uncommitted input/dialog drafts, selections, and presentation.
- Preserve unrelated parameters, including layout, new, from, dataset, selected, tab, return locations, and existing deep-link keys. Clear only the calling surface's filter keys.
- Distinguish absent values from explicit empty values. A short saved-view link restores its saved conditions; a URL containing a full filter envelope with omitted q means an explicitly empty external keyword.
- Preserve current Data Manager v1 envelopes and tasks/objects/tracks lens values. Browser back/forward and same-component route changes must rehydrate; initialization cannot be a one-time useState snapshot.
- Text edits use history replacement. An explicit saved-view/lens navigation keeps its existing navigation semantics. Do not create one history entry per keystroke.
- Normalize text whitespace, known enum values, finite numbers, and dates. Sort unordered multi-select URL values only where the domain declares set semantics; never reorder expression-tree children or arbitrary arrays.
- When applied filters or sort change, reset offset/page/cursor and obsolete selections in the same logical transition. Query construction must never emit new filters with a retired cursor/page before an effect resets it.
- useInfiniteQuery already scopes pages by its query key; use that behavior rather than inventing another cache. Cursor history is transient and never decoded from application-owned URL internals.
- Query keys include normalized filters and domain/owner identity. UI-only chips and raw input drafts do not create cache entries.
- Consume AbortSignal through existing apiClient RequestInit support. Cancellation is neutral; it must not display a failed-load toast. Query-key isolation and request cancellation are distinct guarantees.
- Preserve existing offline/paused, error, retry, and placeholder states. A retained prior page must be visibly loading and cannot be counted as the current scope or used for a new bulk action.

### 4.4 Query, count, export, and evidence scope

A domain builds one authorized, filtered base relation, then derives pagination, total, and applicable statistics/export. User-supplied conditions never replace permission predicates.

Keep three quantities distinct:

- authorized total: all data the actor may access;
- matched total: the complete active query scope;
- loaded/visible total: materialized pages or current-frame/panel rows.

Statistics must state which quantity they describe. Preserve intentional global overview cards and self-excluding facet counts; do not mechanically force every card to use the filtered list.

Data Manager keeps task offset pagination and object/track keyset pagination. Task summary covers all objects in matched tasks; entity facets cover the complete matched entity set. Existing visible_tasks_stmt and annotation same-object constraints remain authoritative.

Ordinary APIs retain their existing parameter names, defaults, response envelopes, sort order, and permission behavior. Client adapters provide shared UX without translating these APIs into Data Manager filter_json.

### 4.5 Workbench scope and ownership

Keep existing owners and define their lifecycle explicitly:

| State                             | Owner and lifecycle                                                                                      |
| --------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Confidence threshold              | Existing useWorkbenchState session preference; retain its persistence behavior                           |
| Review display mode               | Existing review-session owner; retain raw/diff/final semantics                                           |
| Current/all-frame list filter     | AIInspectorPanel transient state, reset to current on task change without remounting transaction owners  |
| Prediction source visibility      | Existing useImageAnnotationActions owner; reset to default-visible on task/project/auth identity changes |
| Dismissed candidate IDs           | Existing task-local candidate owner                                                                      |
| Hidden-track IDs and track colors | Existing task-reset Workbench UI state                                                                   |
| Persisted annotation is_hidden    | Existing annotation mutation and server data; list rows remain restorable                                |
| QC/feedback/queue filters         | Their panel's local query state; owner changes reset pagination and invalid selections                   |
| Batch accept eligibility          | Existing prediction-decision owner and task identity guards                                              |

The candidate pipeline remains: loaded candidates, confidence/accepted/dismissed eligibility, source visibility, then optional current-frame display. Batch accept uses current-task loaded candidates with the existing source/confidence/deduplication rules across loaded frames. Displaying only the current frame does not change that action scope. The action label and count must explicitly reflect its actual target set.

## 5. Dependency graph and parallel schedule

```text
Start from the same recorded local baseline
   |
   +-- F0 Shared frontend + Data Manager --+-- F1 Project/data/template lists --+
   |                                     +-- F2 People/audit/BUG lists -------+
   |                                     +-- F3 Jobs/models/shell ------------+
   |                                     +-- W2 Queue/feedback/QC ------------+
   |
   +-- B0 Data Manager validation ----------- B1 Query/evidence consistency ---+
   |
   +-- A0 Ordinary API base queries --------- C0 SDK/CLI/TUI ------------------+
   |
   +-- W0 Workbench visibility --------------- W1 AI candidate/action scopes --+
                                                                              |
                    V0 integrates and verifies each slice as it lands <--------+
                    Final acceptance joins all packages
```

W2 consumes F0 controls but does not edit W0/W1-owned files. C0 depends only on A0 because the current SDK has no Data Manager methods. All other arrows shown are mandatory. Frontend packages continue using the compatible current API while backend packages land.

- Initial parallel wave: F0, B0, A0, W0.
- As prerequisites integrate: F1, F2, F3, W2, B1, and W1 become runnable.
- C0 runs once the ordinary API package settles; it does not wait for unrelated Data Manager SQL.
- V0 is coordinator-owned rolling integration/documentation/browser acceptance, not a final branch that postpones correctness until the end.
- At most five modifying workers plus one coordinator. Fewer workers may run when runtime resources are constrained.
- Only one E2E/seed-reset job may use a given database at a time. Multiple checkouts alone do not isolate a shared database.
- A package that depends on another starts from the integration HEAD containing that dependency, never from a speculative interface stub.

The shared frontend package and Workbench-shell mutations are intentionally not split across simultaneous writers. This is a real dependency, not parallel work hidden behind repeated merge conflict resolution.

## 6. Work packages and exclusive ownership

Paths are repository-relative. Listed new modules are proposed implementation targets, not files created by this planning session. Existing adjacent test files may be extended within the same owner scope.

### F0 — Shared frontend primitives and all Data Manager lenses

Complexity/risk: large; high semantic sensitivity. Prerequisites: none.

Exclusive source ownership:

- New apps/web/src/components/filters/FilterBar.tsx and FilterValueEditor.tsx.
- New apps/web/src/lib/filters/types.ts, filterValues.ts, filterUrlState.ts.
- New apps/web/src/hooks/useUrlFilterState.ts and useDebouncedValue.ts.
- apps/web/src/pages/Projects/ProjectDataManagerPage.tsx.
- apps/web/src/pages/Projects/data-manager/, including EntityDataManagerLens, DataManagerFilterBar, dataManagerUrlState, charts, and TaskMatchesSheet.
- New domain helpers dataManagerFilterExpression.ts and DataManagerExpressionEditor.tsx in that directory.
- apps/web/src/api/taskViews.ts and apps/web/src/hooks/useTaskViews.ts.

Deliverables: implement sections 4.1–4.3 with Data Manager as the first real consumer; delete duplicate task/entity value conversions and operator labels; preserve complex saved views, URL navigation, schema loading/error behavior, and dirty-state confirmation; pass cancellation through all affected Data Manager reads.

Contract: keep filter_json, task-view payloads, existing v1 URL keys, and pagination envelopes compatible. Invalid schema fields remain visible as invalid, never silently removed.

Tests: extend ProjectDataManagerPage.test.tsx, dataManagerUrlState.test.ts, DataManagerFilterBar.test.tsx, and chart tests; add focused tests beside the new pure helpers/editor and an EntityDataManagerLens.test.tsx flow test.

Acceptance: AI-review OR and missing-required nested groups retain results; round trips preserve root rules, nested keywords, comma-containing values, zero/false, and all grains; typing incomplete numbers/ranges does not change the last applied query; clear/edit/save/cancel/back/forward behave consistently; a changed query cannot reuse a retired cursor.

Independent delivery: all three Data Manager lenses use the shared modules in this patch; other pages may remain unchanged. Rollback: revert F0 and any later consumers first; saved data and URL envelopes need no migration.

### B0 — Data Manager structural validation and schema alignment

Complexity/risk: large; medium compatibility risk. Prerequisites: none.

Exclusive ownership:

- New apps/api/app/services/data_management/filter_tree.py.
- apps/api/app/services/data_management/task_filters.py, entity_filters.py, schema.py, and views.py.
- Existing filter validation paths in apps/api/app/api/v1/task_views.py and data_manager.py.
- apps/api/tests/test_task_views_filter.py and test_task_views.py; new test_data_manager_filter_contract.py.
- B1 owns service.py, entities.py, tracks.py, and their existing integration tests; it reads B0 helpers after integration.

Deliverables: share structural traversal/field collection; reject non-object child nodes consistently; apply the existing IN value cap of 200 across annotation and entity branches; align declared field/operator capabilities with actual accepted behavior. Keep domain SQL callbacks separate and preserve same-object EXISTS grouping.

Compatibility: options are not universal allowlists. Preserve valid legacy operators by accurately advertising support where semantics exist. Project/grain-incompatible saved fields are reported through invalid_fields and never rewritten. Existing valid API inputs must retain their result meaning.

Add explicit structural budgets as named internal constants: maximum nesting depth 32 and maximum 4096 nodes, with root counted as depth 1 and one node. These are new planned ceilings, not claims about current limits. Return a normal 422 before compilation when exceeded; add boundary tests and a generated maximum builtin-view test. They are not environment settings. Preserve builtin view readability through invalid_fields if an existing record exceeds the new ceiling.

Acceptance: root/empty/nested filters, malformed children, wrong scalar/array types, unknown fields/operators, dynamic attributes, dots in attribute keys, and all budget boundaries behave consistently. No arbitrary SQL/JSONB field access is introduced.

Independent delivery: existing clients continue to query valid trees; malformed input gets deterministic errors. Rollback: revert B0; no rows are deleted or migrated.

### B1 — Data Manager query, aggregate, and match-evidence consistency

Complexity/risk: large; high semantic sensitivity. Prerequisites: B0.

Exclusive ownership:

- apps/api/app/services/data_management/service.py, entities.py, tracks.py, task_metrics.py, and cursor.py.
- New apps/api/app/services/data_management/match_evidence.py.
- apps/api/tests/test_data_manager.py and test_data_manager_entities.py.
- Ownership of views.py's obsolete match-projection helper transfers from B0 to B1 only after B0 lands; remove it when match_evidence replaces it. Other B0 modules remain predecessor-owned.

Deliverables: retain shared authorized scopes for list/count/summary/facets; remove repeated source-detection traversals using B0 field/tree helpers; build branch-aware match evidence instead of deciding sources from field presence alone.

Evidence semantics are fixed for this rollout:

- First enforce the full task filter and visibility.
- For OR, include evidence only from true branches and union that evidence.
- For AND, preserve the same-annotation witness requirement within an annotation group; evidence from distinct source families may both explain the matched task.
- A true task-only branch retains active-annotation context as the current unstructured task drawer does.
- Positive or upper-bound candidate counts show the remaining candidates used for that count; a true zero-count condition can legitimately have no entity witness.
- Low-confidence evidence uses the same remaining-shape set and score threshold as the existing low-confidence metric.
- Historical prediction metadata remains task-level context; it must not be relabelled as a current pending candidate.
- Keep source ordering, total, and one global pagination window across annotations, prediction shapes, and tracker jobs. Do not materialize every geometry merely to paginate.

Acceptance: list/count/summary/saved counts agree on task membership; entity totals/facets are independent of pagination; mixed OR/AND evidence, zero counts, low-confidence candidates, accepted/rejected exclusions, tracker creator restrictions, hidden batches, and cross-source page boundaries are covered.

Independent delivery: uses existing response envelopes. Rollback: revert B1 without changing saved views or annotations.

### A0 — Ordinary API query/count/statistics/export reuse

Complexity/risk: medium; authorization-sensitive. Prerequisites: none.

Exclusive source ownership: apps/api/app/api/v1/tasks/list.py and apps/api/app/api/v1/async_jobs.py. Tests: test_tasks_drill_filters.py, test_task_batch_visibility.py, test_tasks_list_sequence_order.py, test_async_jobs.py, and new test_management_filter_contract.py under apps/api/tests/.

Deliverables: derive task page/count from one local authorized filtered relation, retaining the current task_visibility_clause and privilege policy. In async_jobs.py, apply one local filter helper to page/count. Use read-only contract tests to retain the already shared management and audit base queries. Keep project-admin, assignee, reviewer, unbatched, rejected/redo, and legacy-list boundaries explicit.

No public endpoint or response redesign. A legacy endpoint that intentionally has different visibility or output remains a separate adapter, with tests recording the difference. Do not replace all legacy task access with Data Manager visibility just because both expose tasks.

Acceptance: identical applied filters produce identical matched scope across each supported list/count/statistics/export pair, including no-results and cross-project permission cases. Preserve default sort/tie-breakers, supported aliases, and page-size limits. Regression tests cover API rather than browser-only filtering.

Rollback: revert the package; no data migration or bulk write is part of this work.

### F1 — Project, dataset, template, and project-selector filters

Complexity/risk: medium. Prerequisites: F0.

Exclusive ownership:

- apps/web/src/pages/Dashboard/DashboardPage.tsx, AdminProjectsDashboard.tsx, FilterDrawer.tsx, and corresponding tests.
- apps/web/src/pages/Datasets/DatasetsPage.tsx and ProjectTemplates/ProjectTemplatesPage.tsx, plus their tests.
- apps/web/src/hooks/useProjects.ts, useDatasets.ts, useProjectTemplates.ts and corresponding api/projects.ts, datasets.ts, projectTemplates.ts.
- Project list-specific codec modules placed beside their pages.
- Project batch/wizard selector files are retention targets only. F1 does not change their source.

Deliverables: one status owner for tabs and Apply/Cancel drawer; URL restoration; debounced server text; existing date/type/member semantics; signal propagation. Preserve create/duplicate/layout/dataset deep-link parameters. Clear only filters and reset affected pagination/selection.

Acceptance: advanced status followed by a tab updates both display and query; cancelling a drawer does not apply a draft; refresh/share/back/forward work; dataset target expansion and project wizard links remain functional. Retained local selectors keep their existing empty-selection and permission behavior.

Rollback: frontend/API-client revert; server contracts unchanged.

### F2 — People, invitations, audit, and BUG filters

Complexity/risk: large; multiple operational surfaces. Prerequisites: F0.

Exclusive ownership:

- pages/Users/UsersPage.tsx, components/users/InvitationListPanel.tsx, pages/Admin/AdminPeoplePage.tsx, pages/Audit/AuditPage.tsx, pages/Bugs/BugsPage.tsx.
- Their existing/new colocated tests and page-specific URL codecs.
- hooks/useUsers.ts, useInvitations.ts, useAudit.ts, useDashboard.ts; matching API client modules users.ts, invitations.ts, audit.ts, dashboard.ts, bug-reports.ts.
- New hooks/useBugReports.ts for list reads only; preserve existing detail/comment transaction ownership.

Deliverables: normalized applied parameters reused by list/statistics/export; namespaced invitation URL keys; full audit deep-link read/write symmetry; debounce text; cancellation; query-keyed Bug lists. Preserve paused/offline handling and auth-owner guards.

Acceptance: switching member filters clears cross-page selected IDs; invitation and member filters cannot overwrite one another; export uses the latest applied conditions; audit row drill-down and editable controls produce the same URL state; delayed old Bug results cannot replace the current list. Preserve the existing Bug limit contract rather than silently adding export-wide pagination behavior.

Rollback: revert F2; authentication, invitations, and stored Bug data are untouched.

### F3 — Jobs, model catalog/registry, shell panels, and local selectors

Complexity/risk: large but domain-local. Prerequisites: F0.

Exclusive ownership:

- pages/AIPreAnnotate/AIPreAnnotateJobsPage.tsx and components/HistoryTable.tsx.
- pages/ModelMarket/VideoTrackerJobsPage.tsx, CapabilityCatalogPanel.tsx, capability/FilterToolbar.tsx, RegisteredBackendsTab.tsx, RuntimeObservePanel.tsx, registry/, and runtimeTopology filter helpers.
- components/shell/NotificationsPopover.tsx and helpers, JobsBell.tsx; components/UserPicker.tsx and CommandPalette.tsx only for matching filter UX.
- hooks/useGlobalSearch.ts, api/search.ts, asyncJobs.ts, videoTrackerJobs.ts; list reads stay in their existing page hooks. The tracker-session hook useVideoTrackerJobs.ts is explicitly outside F3.
- Local project/assignment/wizard selectors and connector allowlists listed in the appendix are retention targets, with no source migration in this rollout.

Deliverables: shared applied-filter behavior for server job lists, cursor resets, current parameter compatibility, local catalog multi-select semantics, explicit loaded-only notification labels, and account-scoped job preferences. Apply model-family filtering consistently to flat/grouped/protocol catalog views. HistoryTable remains local over its bounded snapshot; reset its page on search changes, clamp after data shrink, and retain accumulated selected IDs with an explicit selected count even when some are hidden. Global search keeps 200ms. Local choices remain immediate and do not gain Data Manager DSL.

Acceptance: image/video tabs and status deep links restore correctly; model filter changes reset cursor history; model axes compose consistently; diagnostics are not duplicated and retain severity scope labels; notifications show loaded-only results; account changes cannot retain another account's dismissed jobs or pending result. Keyboard selection behavior remains intact.

Rollback: revert F3; no model capability or task execution payload changes.

### W0 — Workbench frame/review/hide visibility

Complexity/risk: large; renderer-sensitive. Prerequisites: none.

Exclusive ownership:

- Workbench/shell/annotationFrameScope.ts and stage/aiBoxFrames.ts with their tests.
- Workbench/stage/videoFrameViews.ts, VideoKonvaStage.tsx, VideoTrackPanel.tsx, VideoTrackSidebar.tsx, ImageStage.tsx.
- Workbench/stages/three-d/ThreeDWorkbench.tsx.
- Their existing geometry/visibility/renderer tests and a new VideoTrackPanel.test.tsx when required.

Deliverables: reuse existing videoStageGeometry and frame helpers; consolidate review-mode predicates between rosters and canvas; align persisted is_hidden behavior across image/video/3D; retain restorable hidden list rows and existing hiddenTrackIds behavior. Preserve supported geometry and outside-range semantics.

Acceptance: image/video/3D hidden annotations disappear from the corresponding canvas and can be restored; bbox/polygon/polyline/mask tracks agree on frame membership; raw/diff/final and ghosts/reference layers retain their documented meanings. Changing visibility must not create, accept, delete, or modify geometry.

Independent delivery: renderer behavior improves without F0 or W1. Rollback: revert W0; existing persisted hide values remain.

### W1 — AI candidate display and batch-action scope

Complexity/risk: large; write-scope-sensitive. Prerequisites: W0.

Exclusive ownership:

- Workbench/stages/image/useImageAnnotationActions.ts and usePredictionDecisions.ts.
- Workbench/shell/AIInspectorPanel.tsx.
- Workbench/state/useWorkbenchShellModel.tsx.
- Existing related tests; new usePredictionDecisions.test.ts.

Deliverables: explicitly derive visible candidates, batch-eligible candidates, and their counts from the existing source pipeline. Apply section 4.5 lifecycle resets without moving transactions or persistence into popovers. Preserve current-task, all-loaded-frame batch acceptance; labels/counts must say what will be accepted.

Acceptance: source/confidence changes affect the intended list/canvas/action sets; frame-only display never widens a mutation; task/project/auth switches reset transient filters; late results and pending confirmations cannot affect another task; IoU-dimmed/accepted/dismissed candidates remain excluded.

Do not remount WorkbenchShell, transaction hooks, or tracker session owners merely to reset a filter. Reset the small local filter state explicitly or isolate only the presentation component.

Rollback: revert W1; annotations and accepted predictions are not rolled back as data.

### W2 — Queues, discussion/feedback, quality, and Workbench local selectors

Complexity/risk: medium; selection-sensitive. Prerequisites: F0.

Exclusive ownership:

- Workbench/shell/OfflineQueueDrawer.tsx, DiscussionIssuesTab.tsx, MaskQcPanel.tsx.
- Workbench/state/useIssuePins.ts.
- Workbench/stages/three-d/PointCloudQualityPanel.tsx and sidebar/VideoTrackQualitySidebar.tsx.
- pages/Annotate/AnnotatePage.tsx, pages/Review/ReviewPage.tsx and ReviewSidebar.tsx for queue filter adapters.
- hooks/useTasks.ts, useFeedbacks.ts, useMaskQc.ts, usePointCloudQuality.ts and matching task/feedback/QC API clients only where filter/cancellation changes are needed.
- Workbench ClassPalette/settings/hotkey filtering is retained local; no global state change.
- Colocated tests; new OfflineQueueDrawer.test.tsx and DiscussionIssuesTab.test.tsx.

Deliverables: apply feedback status before server pagination using the existing status parameter; retain task/project panel scope; prevent an absent current task from broadening offline queue scope; preserve queue selection and rejected/redo/unbatched navigation; prune selected QC repair IDs when hidden or ineligible. A first-page badge must not imply a project total.

Acceptance: status results remain correct across pages; new filters start at the first page/cursor; queue errors remain distinguishable from empty results; hidden QC issues cannot be submitted; video quality fragment matching remains eligibility rather than visual filtering.

Rollback: revert W2; retain queued operations and stored quality/feedback records.

### C0 — SDK, CLI, TUI compatibility and filtering regression

Complexity/risk: small. Prerequisites: A0.

Exclusive ownership: packages/python-sdk/tests/test_projects.py, test_tasks.py, test_datasets.py, test_jobs.py, test_cli_commands.py, test_tui_app.py, test_tui_states.py, and test_openapi_contract.py. Existing SDK source is read-only unless a failing ordinary-filter regression requires a minimal compatibility fix in its owner.

Deliverables: verify existing project/task/dataset/job flags, paging, TUI 300ms search, stale-result protection, and per-view reset behavior against A0. The current SDK client and coverage manifest contain no Data Manager/task-view methods, so no SDK filter_json or nested-expression support is claimed or added. Data Manager JSON contracts are exercised through API/frontend tests in F0/B0/B1.

Keep api-coverage.toml and independent SDK release metadata unchanged. If a public OpenAPI snapshot changes, the coordinator refreshes that artifact and C0 runs existing contract verification. Adding Data Manager SDK coverage is explicitly outside this existing-capability unification plan.

Acceptance: existing CLI flags and SDK methods retain behavior and legacy response handling; TUI filter changes reset offset and discard stale responses; existing contract/coverage tests pass.

Rollback: revert C0; no SDK release/version assignment.

### V0 — Rolling integration, documentation, browser acceptance

Complexity/risk: coordinator-owned. Starts immediately and runs after each integrated package.

Exclusive shared ownership:

- This plan, CHANGELOG.md, README.md, docs-site/, and any necessary ADR.
- apps/api/openapi.snapshot.json and derived generated API artifacts if actual schema changes require regeneration.
- apps/web/src/api/generated/ regeneration in the integration checkout.
- New apps/web/e2e/tests/filter-data-manager.spec.ts, filter-operational-lists.spec.ts, filter-workbench.spec.ts.
- Existing apps/web/e2e/fixtures/seed.ts and apps/api/app/api/v1/\_test_seed.py; new apps/web/e2e/fixtures/filtering.ts and apps/api/app/api/v1/\_test_seed_filters.py; new apps/api/tests/test_filter_seed.py plus existing test_seed_router.py guard coverage.
- Fixture construction and cleanup follow section 7.1. These test-only files are coordinator-owned and never edited concurrently by business-package workers.
- package manifests/lockfiles, App.tsx, shared design primitives/styles, and CI remain coordinator-only and unchanged unless a concrete task need is reviewed.

Each worker supplies exact documentation deltas and verification evidence with its commit. The coordinator updates relevant documentation before that slice is committed/integrated as a completed delivery; knowledge cannot wait only in this plan.

Acceptance and rollback procedures are in sections 7–9. V0 never marks browser/API acceptance from unit results or fixture transport alone.

## 7. Verification contracts

### 7.1 Dedicated disposable filtering fixtures

V0 owns this prerequisite alongside the initial implementation wave. Existing seed.reset() provides a basic image project with five tasks and no attribute schema; existing video/LiDAR helpers supply media and selected workflows but do not establish the full filtering matrix. The new browser tests must not pretend those fixtures already prove nested expressions, permission boundaries, or pagination.

Implementation targets are the V0 files listed in section 6:

- Add a small \_test_seed_filters.py fixture builder, invoked by POST /api/v1/\_\_test/seed/filtering on the existing \_test_seed router after seed.reset(). It takes no arbitrary database/table/SQL input and uses only the existing E2E identities.
- Preserve the router's non-production switch, actual database-name guard, and include_in_schema=False. The fixture endpoint is not a public product API and does not change OpenAPI compatibility.
- Add filtering.ts to construct/reset this fixture, obtain the existing seed-role tokens, and expose named scenario IDs to the three specs. Reuse seed.ts, videoTask/trackerReview, LiDAR/media helpers, and current API models rather than adding another runtime/bootstrap.
- Return a manifest of role IDs, project/batch/task/entity/view IDs and expected result IDs. Expected sets come from the explicitly constructed fixture records, never from calling the query/compiler being tested.

Construct these independent scenarios:

| Fixture project/scope | Required data                                                                                                                                                                                         | Expected invariant                                                                                                                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Image semantics       | A task with separate car/red and person/blue annotations; a second task with one car/blue annotation; inactive/cancelled matching annotations; visible and actor-inaccessible batches                 | car AND blue in one annotation group matches only the same-object case; inactive/cancelled/hidden-batch rows do not leak                 |
| Required attributes   | A bbox schema with required color, applies_to=car, and a visible_if dependency on an enumerated weather field; eligible missing, eligible present, and ineligible missing examples                    | The generated missing-required nested builtin selects the explicitly eligible missing task only                                          |
| Video AI review       | Four named tasks: detection-only pending, tracker-only pending, both pending, and neither; remaining shapes below/at/above 0.5, plus already accepted/rejected shapes and creator-scoped tracker jobs | AI-review OR matches three tasks while AND matches one; low-confidence evidence and actor visibility match the manifest                  |
| Paging image project  | Exactly 101 active visible object annotations with stable sort/tie-break values; reuse a small fixture image                                                                                          | Page 1 has 100, page 2 has one; total/facets remain 101; changing filters from page 2 resets correctly                                   |
| Scene LiDAR project   | Two media-backed scene frames and 101 logical track IDs represented in both frames, plus one hidden-batch member to test visibility                                                                   | Two pages of logical tracks, no duplicate track per member/frame; hidden members do not change authorized facet/location results         |
| Operational lists     | Two projects/datasets/templates with distinguishing names/types, active/inactive users, invitation statuses, audit rows, BUG states, and image/video job statuses under E2E identities                | Server text/enum filtering, latest applied export scope, URL restoration, and empty/error/loading cases are observable through real APIs |

Use ORM construction only inside the guarded disposable seed helper for states that would otherwise require a running ML worker. Browser reads and saved-view/annotation decisions then use the real product APIs and database. Stored ML candidates are deterministic fixtures; inference quality and real backend throughput are not being qualified.

Create private/shared saved views for root OR, nested required attributes, object filters, and track filters. Include a structurally valid saved view whose attribute is subsequently removed, so invalid_fields recovery is exercised without corrupting unrelated data.

Extend fixture-scoped cleanup so reset/global teardown removes every new fixture-owned record and its task-owned objects. Verify cleanup by manifest IDs after a run; do not add broad TRUNCATE, erase shared media, or relax the database guard. Add test_filter_seed.py to check the scenario manifest, cleanup, and literal expected memberships; extend test_seed_router.py to confirm the new endpoint remains guarded and absent from public OpenAPI.

V0 can prepare this test-only patch from the initial baseline because it uses existing models. It lands with its own guard/cleanup tests before any new browser spec relies on it. If media/runtime infrastructure is unavailable, API integration evidence may still complete, but the affected browser acceptance remains explicitly pending; it is not silently downgraded to mocked query responses.

### Expression and value matrix

| Case                                                      | Required invariant                                                                    |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Empty/root rule/flat AND/root OR/nested groups            | Query meaning survives load/edit/save/URL round trip                                  |
| Same-object annotation conditions                         | Different objects cannot jointly satisfy one same-object group                        |
| Mixed task/annotation/AI OR                               | Evidence follows true branches; list membership is unchanged                          |
| Numeric zero, false, negative/decimal drafts, empty draft | Valid values preserved; unfinished input never silently becomes another applied value |
| IN/between/contains_any/contains_all                      | Arrays and ranges validated without losing commas inside string values                |
| Unknown field/operator/value shape                        | Actionable error; no widened fallback query or raw server error                       |
| Missing/corrupt/newer URL envelope                        | Existing fallback with clear recovery notice; no overwrite of a saved view            |
| Schema or owner change while editing                      | Old draft/request cannot overwrite the new scope                                      |
| Filter/sort change on later page                          | No request combines new filters and old pagination                                    |
| Cancelled/slow request                                    | Neutral cancellation; old response cannot replace the active query                    |
| Saved view with removed attribute                         | Record remains; invalid field is visible and removable                                |

### Browser acceptance

Use the existing Playwright stack with one verified disposable database per concurrent run. The current basic seed is insufficient for this matrix; construct the dedicated fixtures in section 7.1 before claiming browser acceptance. Add the three named regression specs and reuse existing media/seed helpers. Cover:

1. Data Manager task/object/track lenses; AI-review OR and missing-required groups; grouped editing; keyword/number/multi-select/range; chart toggles; save/copy/update/cancel; scope changes; refresh and back/forward; invalid URL; hidden-batch permissions; list/facet/summary/evidence consistency.
2. Project status tabs versus drawer; members/invitations/audit/BUG representative flows; datasets/templates/job/model routes; retained Analytics 7/30/90-day choices and initialization-error state; filters retained on navigation; selection resets; export scope; no-result/error/loading distinctions.
3. Image/video/3D Workbench: frame/source/confidence/review/hide; restored hidden rows; current-task batch scope across frames; late task results; feedback/QC pagination; offline queue current-task isolation.
4. Keyboard and focus behavior, app light/dark themes, supported desktop widths 1440 and 1024. Operational lists also receive a 390px check; Workbench phone interaction remains subject to its existing mobile blocking behavior.
5. Inspect affected API calls and browser console errors. Report fixtures separately from real API persistence.

Reuse existing video-tracker-review-scope.spec.ts, video-tracker-local-review.spec.ts, review-feedback-loop.spec.ts, and relevant image/video/pointcloud smoke paths where changes intersect those workflows. Do not rerun unrelated rendering benchmarks merely because filtering changed.

### Commands

Run commands in the stated working directory. New tests named in section 6 must exist by their package's verification.

From the repository root, frontend targeted checks use:

```sh
rtk proxy pnpm --dir apps/web exec vitest run src/pages/Projects/ProjectDataManagerPage.test.tsx src/pages/Projects/data-manager
rtk proxy pnpm --dir apps/web exec vitest run src/pages/Dashboard/DashboardPage.test.tsx src/pages/Dashboard/AdminProjectsDashboard.test.tsx src/pages/Users/UsersPage.test.tsx src/pages/Audit/AuditPage.test.tsx
rtk proxy pnpm --dir apps/web exec vitest run src/pages/Workbench/shell/annotationFrameScope.test.ts src/pages/Workbench/stage/aiBoxFrames.test.ts src/pages/Workbench/stage/videoFrameViews.test.ts
rtk proxy pnpm --dir apps/web run typecheck
rtk proxy pnpm --dir apps/web run lint
rtk git diff --check
```

Web lint includes CSS-token validation; run lint:css-tokens separately only when doing an earlier styling-only check. Workers add their own changed-file suites; the commands above are shared anchors, not a claim that all package behavior is covered by these few tests.

From apps/api, after confirming the test target is disposable:

```sh
rtk proxy uv run pytest -q tests/test_task_views_filter.py tests/test_task_views.py tests/test_data_manager.py tests/test_data_manager_entities.py tests/test_data_manager_filter_contract.py
rtk proxy uv run pytest -q tests/test_management_api.py tests/test_management_consistency.py tests/test_audit_logs.py tests/test_async_jobs.py tests/test_tasks_drill_filters.py
```

From the root, use the repository-pinned Ruff commands for changed Python files and the targeted SDK commands in the ownership appendix. Browser checks from apps/web:

```sh
rtk proxy pnpm exec playwright test e2e/tests/filter-data-manager.spec.ts e2e/tests/filter-operational-lists.spec.ts e2e/tests/filter-workbench.spec.ts --project=chromium
```

The pointcloud-specific acceptance runs with the existing pointcloud Playwright project and fixtures rather than claiming that the Chromium project qualified 3D.

### Runtime and performance

- apps/api/tests/conftest.py derives annotation_test by default but honors TEST_DATABASE_URL and applies migrations. Verify the actual database host/name/role before running; its name alone does not establish disposability.
- apps/web/playwright.config.ts uses isolated local API/Web servers and annotation_e2e by default, with environment overrides and one worker. Verify the resolved target and that it is disposable before prepare_e2e_db or any seed reset.
- API tests/E2E are coordinator-serialized when sharing database/services. Workers without safe runtime access deliver static/unit results and explicitly mark integration checks pending.
- Before dependency or configuration operations, inspect .env and node_modules symlink targets. Keep Python environments and generated frontend API clients checkout-local.
- No Celery task/signature change is planned. If an actual dependency reaches a worker, follow the runtime skill and verify only affected running workers; API reload alone is insufficient.
- Record request counts for a fixed typing sequence before/after debounce, stale-result behavior, and first/next-page queries. Do not claim an arbitrary speedup.
- For changed Data Manager SQL, reuse apps/api/scripts/benchmark_data_manager_entities.sql in a disposable target and compare query plans, matched totals, and fixed-query-count behavior against the same dataset. Preserve existing columns-driven projection. Add no index without an observed plan bottleneck.
- Stop only task-owned servers and clean only task-owned test data/artifacts. Do not reset the everyday development database.

## 8. Parallel execution and integration runbook

Follow [parallel-work.md](../../../.agents/references/parallel-work.md), the [runtime skill](../../../.agents/skills/aap-runtime/SKILL.md), and [Workbench lifecycle guidance](../../../.agents/skills/aap-workbench-state/SKILL.md).

The installed Orca CLI guide and worktree-create help were checked during planning; the app/runtime was reachable. No implementation worktree was created. Re-resolve the installed executable and reload its guide at execution time.

1. Record the originating checkout's actual local HEAD and clean/dirty state. Preserve unrelated changes. An implementation worker must include unpushed parent work; do not use origin/main as an assumed baseline.
2. For each runnable package, create an Orca child with explicit parent-worktree and base-branch pointing to the recorded local integration commit. Parent lineage alone does not choose the Git base.
3. Assign the returned absolute path, package ID, predecessor commit hashes, exact write scope, frozen contracts, tests, and known runtime limitations. Use the configured agent/model unless the user specifies otherwise.
4. Modifying workers work only in isolated checkouts and provide real commits as integration artifacts under repository delegation rules. Read-only review agents may share the integration checkout.
5. Workers do not edit coordinator-owned files. A needed shared change goes to its owner as a concrete request; the owner commits it first, then dependent workers update their base.
6. Require a handoff containing commit hash, changed paths, behavior deltas, commands and results, tests not run and why, compatibility notes, and documentation deltas. No claims based solely on an agent saying it is done.
7. Integrate complete package commits in dependency order. Resolve conflicts in the coordinator checkout after reading both behaviors; never blanket-choose one side.
8. Run the smallest affected integration suite and update documentation for that slice. A failed check reopens that package; unrelated independent packages may continue.
9. After integration, remove only this task's clean integrated child worktrees and merged/equivalent worker branches through Orca. Preserve unmerged or dirty work.
10. Final acceptance joins all package evidence. Local completion does not imply remote CI, a push, a release, or physical-device performance qualification.

Reservation rules: F0 owns shared filters and Data Manager frontend; W1 alone owns useWorkbenchShellModel; W0 alone owns renderers; W2 alone owns queue/feedback/QC adapters; B0 and B1 are sequential owners of Data Manager backend concerns; A0 stays outside that directory. F1/F2/F3 must not edit each other's shared API/hook modules.

## 9. Documentation, completion, and rollback

V0 updates these current-system documents with each affected delivery:

- Data Manager: docs-site/user-guide/projects/data-manager.md and docs-site/dev/reference/data-manager-query.md.
- Operational lists: docs-site/user-guide/projects/index.md, datasets/index.md, projects/project-templates.md, superadmin/user-management.md, superadmin/audit-logs.md, superadmin/bug-management.md, and superadmin/model-market.md.
- Jobs/shell: projects/ai-preannotate.md, superadmin/failed-predictions.md, reference/notifications.md.
- Workbench: ai/candidate-review.md, ai/current-task-inference.md, workbench/index.md, workbench/video-track.md, workbench/mask-brush.md, and docs-site/dev/concepts/workbench-shell.md.
- SDK: docs-site/dev/sdk/tui.md and affected SDK/API reference pages.
- New developer reference: docs-site/dev/reference/filtering.md, documenting shared field/state contracts, result scopes, URL ownership, and domain boundaries. Link it from the existing design-system/query references rather than adding a navigation framework.
- CHANGELOG.md Unreleased entries describe user-visible fixes and capabilities. Pure internal deduplication does not need a separate entry.
- Update affected API docs/README and regenerate API artifacts only when the public contract changes. No version provenance is added to ordinary rendered guides.
- Use the documentation-media workflow for screenshots whose visible filter controls change; retain or update evidence explicitly, without claiming recordings were regenerated when only code selectors changed.

Completion means every inventory row has its migration or tested retention recorded; each package has actual commits/checks and resolved integration status; relevant docs are current; focused browser/API tests pass; and any untested environment is named precisely. Append an Outcome section only after implementation, with actual evidence and remaining limitations.

Rollback is code reversion in reverse dependency order. Revert consumers before shared frontend primitives, B1 before B0, and W1 before W0. Keep saved views, annotations, predictions, notifications, pending queue operations, and user preferences as data. Do not delete or rewrite them during rollback.

## 10. Design risks and official references

- The most fragile assumption is that existing ordinary lists continue to use fixed resource-specific fields. If cross-resource arbitrary expressions become a real requirement, a separate capability/API design is needed; this rollout remains useful because its controls and state contracts already compose.
- Strict validation can reveal formerly accepted malformed views. Preserve the record and actionable invalid_fields; never repair a saved filter silently.
- A universal group simplifier would break same-object semantics. Expression helpers preserve grouping even where plain Boolean algebra would allow flattening.
- A URL feedback loop can overwrite browser navigation or an uncommitted draft. Test external URL navigation, explicit empty values, loading schemas, and owner changes.
- Workbench filter remounts can retire transaction owners. Reset only the filter state and keep session/write guards mounted.
- Shared databases, global docs/changelog files, and large shell-owner files are concurrency bottlenecks. The ownership table and serialized validation lane address them directly.
- No new third-party service is needed. API tokens for the application remain existing test-session credentials, not new integration setup.

Existing official platform features cover URL state, query identity, and cancellation:

- [React Router useSearchParams](https://reactrouter.com/6.30.1/hooks/use-search-params).
- [TanStack Query keys](https://tanstack.com/query/latest/docs/framework/react/guides/query-keys).
- [TanStack Query cancellation](https://tanstack.com/query/latest/docs/framework/react/guides/query-cancellation).

These references establish available mechanisms, not proof that the repository's changed workflows pass acceptance.

## Appendix A. Page URL and reset contract

These are the planned page codecs. Existing keys retain their meaning; newly introduced keys are additive. Each codec deletes only the keys it owns. Defaults below come from current page/API code unless explicitly described as a new URL representation.

| Page                                  | Owned filter keys and defaults                                                                                           | Pagination/selection rule                                                                                                                         |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Project lists                         | New URL representation: q empty; status omitted/all; repeated data_type; member_id, created_from, created_to omitted     | Existing non-paginated API unchanged; one status value feeds tabs and drawer; layout/new/from are not filters                                     |
| Datasets                              | New q empty and data_type omitted/all; dataset deep link remains independent                                             | Preserve existing API pagination/default size; filter changes clear obsolete expanded list identity without deleting selected wizard IDs          |
| Project templates                     | New q empty; scope defaults private; organization/public retained; explicit scope=all means unfiltered                   | Preserve existing non-paginated list and create/apply modal state                                                                                 |
| Users members                         | New tab defaults members; q empty; status defaults active; role/project_id/group_id omitted; page defaults 1             | Preserve current page size; filter changes reset page and clear selected users; page changes preserve cross-page selection                        |
| Invitations in Users                  | New invite_q empty, invite_status=all, invite_scope=me, invite_role/invite_project_id omitted, invite_page=1             | Page size remains 25; namespace cannot overwrite member state; invitation token/copy state never enters URL                                       |
| People performance                    | Existing role/project/q empty, period=7d, sort=throughput                                                                | Preserve required project selection for project admins; retain existing non-paginated results                                                     |
| Audit                                 | Existing action/target_type/target_id/actor_id/detail_key/detail_value; new scope=business by default and page=1         | Retain page size and exports; preserve empty detail_value when detail_key is set; export strips pagination only as its existing API does          |
| BUG list                              | New status/severity omitted by default                                                                                   | Preserve current limit=50; no pagination UI added; existing detail/comment owner is separate                                                      |
| Image jobs                            | Existing tab/project_id/status keys; new q empty; status omitted/all; new page=1 in URL maps to zero-based internal page | Keep limit=20; keyword/status reset page; legacy status=failed remains valid                                                                      |
| Video tracker jobs                    | Existing tab=video and project_id; new video_status and video_model_key, both empty by default                           | Keep limit=20 and transient opaque cursor/history; query changes reset cursor; image status key does not become a video status                    |
| Data Manager                          | Existing lens/view/q/filter/sort/columns/selected and v1 envelopes                                                       | Task page defaults 0 locally; entity pages belong to normalized filter/sort query key; scope/changed filters invalidate obsolete selected details |
| Catalog/registry/IssueCenter          | Keep existing top-level tab navigation; filters remain local canonical state                                             | No cursor or full-result claims for a partially loaded/failed capability snapshot                                                                 |
| Admin Analytics                       | Retain local days=30, choices 7/30/90; no URL keys                                                                       | Range affects all four aggregate queries; no row selection or pagination                                                                          |
| Workbench queues                      | Preserve existing annotate batch/status=rejected and review project/batch/assignee/taskId keys                           | Follow queue owner rules; cursor reset cannot silently pick a different task or widen an unbatched/redo scope                                     |
| Notifications/JobsBell/option pickers | No URL filter state                                                                                                      | Keep the explicit local/auth/panel lifecycle described in section 4.5 and the inventory                                                           |

All page text values use their existing backend parameter names through adapters: q may map to search, while People already uses q. Local display-name labels are never API enum values. Preserve project date boundary semantics; send date strings through the existing adapter and reject reversed ranges in the draft editor.

Users and HistoryTable intentionally have different selection policies. Users clears selection when filters change, while HistoryTable preserves an explicitly accumulated batch selection across search/page and shows its selected count. Option-picker search preserves selected IDs even if matching options become invisible; changing role/model/project resets only the dependencies already defined by that business owner.

## Appendix B. Retained API and selector owners

### Ordinary API boundaries

| Resource                            | Current implementation to reuse or retain                                                                                      | Production write owner                             |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------- |
| Tasks                               | api/v1/tasks/list.py; scheduler task_visibility_clause; sequence_order/created_at/id order; total only on first page           | A0 changes list.py only; scheduler policy retained |
| Async jobs                          | api/v1/async_jobs.py; status/kind/project/search and list/count scope                                                          | A0                                                 |
| Users                               | services/management.py build_user_query; api/v1/users.py; separate legacy list behavior                                        | Retained; A0 adds contract tests                   |
| Invitations                         | services/management.py build_invitation_query and api/v1/invitations_admin.py query/stats/export; legacy list adapter retained | Retained; A0 adds contract tests                   |
| Audit                               | api/v1/audit_logs.py shared base query and export                                                                              | Retained; A0 adds contract tests                   |
| Projects/datasets/templates/batches | api/v1/projects.py, datasets.py, project_templates.py, batches.py, and their existing services                                 | Retained; frontend adapters keep current contracts |
| Video tracker jobs/feedback/QC      | Existing source-specific query parameters and permission checks                                                                | Retained; F3/W2 use current API capabilities       |

Project, dataset, template, batch, audit, user, and invitation SQL is not rewritten to satisfy a uniform class interface. A0's management contract test asserts each supported list/stats/export relationship and records intentional global-summary differences.

### Retained selector entry points

These paths are read-only retention targets for this rollout. They are not unassigned optional coding work.

- components/projects/AssignMemberModal.tsx: retain name/email/group search, role reset, and existing-member exclusion.
- components/projects/BatchAssignmentModal.tsx and ProjectDistributeBatchesModal.tsx: retain role-separated project member eligibility.
- components/projects/steps/Step4Ai.tsx, Step5Datasets.tsx, Step6Members.tsx: retain wizard-owned selected IDs and data/model/member compatibility; no new search feature.
- pages/Projects/sections/PrefillFromBackendDialog.tsx: retain backend/model capability eligibility and defaults.
- pages/AIPreAnnotate/components/PreannotateConfigForm.tsx, StageCard.tsx, GlobalStageInspector.tsx, ChipMultiSelect.tsx, ClassWhitelistRow.tsx: retain execution/configuration values and empty-set meaning.
- pages/AIPreAnnotate/GlobalPipelineLibraryPage.tsx and components/ProjectDetailPanel.tsx: retain pipeline and active-batch selection contracts.
- components/ml/VariantSelector.tsx: retain valid-combination dependent axes.
- components/connections/ConnectorAllowlistSettings.tsx: retain allowlist editing semantics.
- pages/Admin/AnalyticsPage.tsx and api/adminAnalytics.ts: retain days=30 and 7/30/90-day ranges, four aggregate query keys, and existing DuckDB initialization/error behavior.
- pages/Projects/sections/BatchesSection.tsx and BatchesKanbanView.tsx: retain view/selection controls; they do not gain an arbitrary query builder.
- Workbench/shell/ClassPalette.tsx, ClassPickerPopover.tsx, ContinuousCreationControls.tsx, WorkbenchSettingsDialog.tsx, HotkeyCheatSheet.tsx: retain local search and availability predicates.

Paths in this selector list are relative to apps/web/src/. V0 records retention checks through existing relevant tests and one representative keyboard/selection browser flow, without forcing cosmetic rewrites.

## Appendix C. Package verification inventory

All commands are run after their package exists; newly proposed files are identified below. Tests run only against an appropriate environment, as required in section 7.

| Package | Additional focused test targets beyond section 7                                                                                                                                                                                                                   |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| F0      | New FilterValueEditor.test.tsx, filterValues.test.ts, filterUrlState.test.ts, useUrlFilterState.test.tsx, dataManagerFilterExpression.test.ts, DataManagerExpressionEditor.test.tsx, EntityDataManagerLens.test.tsx; existing DataManagerCharts and overview tests |
| B0      | Existing test_task_views_filter.py and test_task_views.py; new test_data_manager_filter_contract.py                                                                                                                                                                |
| B1      | Existing test_data_manager.py and test_data_manager_entities.py                                                                                                                                                                                                    |
| A0      | Existing test_tasks_drill_filters.py, test_task_batch_visibility.py, test_tasks_list_sequence_order.py, test_async_jobs.py; new test_management_filter_contract.py                                                                                                 |
| F1      | Existing DashboardPage, AdminProjectsDashboard, DatasetsPage, ProjectTemplatesPage tests                                                                                                                                                                           |
| F2      | Existing UsersPage, InvitationListPanel, AdminPeoplePage, AuditPage, BugsPage tests; new useBugReports.test.tsx when list race isolation is best tested at the hook                                                                                                |
| F3      | Existing AIPreAnnotateJobsPage, VideoTrackerJobsPage, CapabilityCatalogPanel, RegisteredBackendsTab, runtimeTopology, HistoryTable, JobsBell, NotificationsPopover and Admin/AnalyticsPage tests                                                                   |
| W0      | Existing annotationFrameScope, aiBoxFrames, videoFrameViews, VideoKonvaStage.konva, VideoTrackSidebar tests; new VideoTrackPanel.test.tsx                                                                                                                          |
| W1      | Existing AIInspectorPanel.test.tsx, AIInspectorPanel.phases.test.tsx, AIInspectorPanel.videoSelection.test.tsx, useImageAnnotationActions.test.ts; new usePredictionDecisions.test.ts                                                                              |
| W2      | Existing AnnotatePage, ReviewPage, MaskQcPanel, PointCloudQualityPanel, useIssuePins tests; new OfflineQueueDrawer.test.tsx and DiscussionIssuesTab.test.tsx                                                                                                       |
| C0      | Existing Python SDK project/task/dataset/job/CLI/TUI/OpenAPI contract tests                                                                                                                                                                                        |
| V0      | Three new filter E2E specs, intersecting existing Workbench/feedback specs, documentation checks, and final static checks                                                                                                                                          |

Frontend targets are colocated with the owning source from section 6; API targets are under apps/api/tests/. Extend a relevant existing suite rather than creating a second suite for the same behavior. New helper tests cover user-visible semantics, not individual implementation details.

From packages/python-sdk, use its existing test environment:

```sh
rtk proxy uv run --extra test pytest -q tests/test_projects.py tests/test_tasks.py tests/test_datasets.py tests/test_jobs.py tests/test_cli_commands.py tests/test_tui_app.py tests/test_tui_states.py tests/test_openapi_contract.py
```

From the repository root, for the final documentation/API integration:

```sh
rtk proxy pnpm docs:impact
rtk proxy node scripts/check-dead-links.mjs docs-site
rtk proxy pnpm docs:build
rtk git diff --check
```

Run pnpm openapi:export, pnpm openapi:check, and pnpm codegen only if the public schema/artifacts changed. The coordinator owns those outputs. No application or SDK version bump accompanies code generation.

Use the repository-pinned Python lint commands (pnpm lint:python and format:python:check) at integration. A worker may run the same pinned Ruff version against its changed files earlier; an unpinned globally installed Ruff is not the acceptance result.

For each package, V0 records: baseline and integrated commit; unit/static result; API/database result; browser result; fixture-vs-live boundary; documentation paths; retained surfaces checked; and remaining limitations. This is a finite acceptance ledger, not a new CI service.

## Execution ledger

- The user authorized committing the plan and parallel implementation on 2026-09-12.
- Plan commit: 32e30fc602a4c187d82bb8a4b45cf04f0029420e.
- Initial workers use separate Orca child worktrees and worktree-agent-filter-\* branches from that exact local commit.
- F0 integrated as c22881a1f435bafba71c235ff05f938c699881c9, from fad55260 and 82c20c76 plus coordinator corrections for typed quick-filter values and direct-AND-only toggle detection. Root verification: 39 focused tests, typecheck, ESLint, CSS token lint and diff checks passed. Draft-validity follow-ups 9989412a, b8976998 and 35e3abef integrated as 3def12e5; root passed 29 focused tests and numeric/deep-filter browser cases. Boolean null validation was tightened in f93295e after final review, while nullable non-boolean values remain valid. V0 delegated its isolated seed-builder subset; the coordinator retains documentation, browser specs, runtime, and integration ownership. An independent seed review identified overly broad cleanup, an identity-map false-positive preservation assertion, and missing camera calibration. The fixes were integrated with the seed as 52aaed9c; root reran all 40 fixture/router tests successfully. The 101-row boundary adjustment and four Data Manager browser cases landed as c744b2c5. Their four cases passed across focused runs.
- A0 worker commit f6584d58e62f63cd6f37dcd721e221da6e122dae was reviewed and integrated as 74c8741fb8a003bb42ae3088a23717551d1ca6be. The coordinator reran all 35 selected task/job/management API tests successfully on the A0 lane. The change is a pure refactor with no documentation behavior delta.
- C0 completed from 74c8741f: 134 SDK/CLI/TUI/OpenAPI tests passed, with no skips or changes. Its ignored SDK virtualenv is checkout-local; no empty commit was created. These are isolated compatibility tests, not a separate live SDK deployment.
- W0 worker 26747bed was reviewed and integrated with user/developer guides and changelog as af9ad8a9f737fee1f87d3eed27da63805a2609a3. The coordinator reran 162 selected frame/render/sidebar tests successfully; typecheck, changed-source ESLint, CSS-token lint, formatting, and diff checks passed. Mocked canvas warnings do not constitute live renderer validation.
- B0 workers 901265d1 and 000e49bd were reviewed and integrated with API/developer docs, README, and changelog as 9c377d0865cf1036f9c8df4984cf48040be1256f. Actual PostgreSQL probes exposed date/UUID bind errors before the follow-up fix; the coordinator then reran all 90 selected Data Manager tests successfully.
- B1 workers 775403fd and f32c9c76 integrated as 8942def after review replaced per-node SQL columns and materialized UUID sets with a single OR-truth array and direct SQL witness predicates. Root reran 142 tests successfully; four facade cases skip because their parameter sets are empty. Full pinned Python lint and format check passed (1179 files).
- W1 workers f15cc798 and ec53c4d7 integrated with guides/changelog as 0d144df6. Root reran 99 focused tests and typecheck successfully; pre-invocation and late-completion auth guards are covered.
- F1 workers 12392b4e and 516b011a integrated as 65f3aa81 with a StrictMode-safe immediate debounce update. Root passed 66 focused tests and project/dataset/template browser cases. The dataset browser locator was narrowed to its row after it initially also matched the app sidebar collapse button.
- F2 workers ba88de80 (copied as 472bc102) and 9d288745 integrated as 15f6d44 with user/audit/BUG/people guides and changelog. Root passed 65 focused tests and member/invitation/export plus audit-empty-value browser cases. The export assertion reads the actual downloaded CSV; Playwright response.text() returned an empty transport body while the download contained the expected filtered row.
- W2 workers 887c25e2 (copied as 1008151a), 2d4e19da and 7738745f integrated as 0de29733 with review/Mask/point-cloud/offline guides and changelog. Root passed 88 focused tests, the real Scene quality-filter query case (no scan or annotation mutation), and existing review-feedback plus cross-task Issue navigation browser flows.
- Auth-query follow-up 5657de37 integrated as f93295e, including root corrections for a jsdom scroll stub, test cleanup, and non-nullable boolean values. Root passed 50 intersecting tests, 13 focused schema/owner tests, and three intersecting project/DM browser cases. Private query keys include account/token identity; task and summary placeholders remain within the same project/owner.
- F3 original 25eb175d (copied as 4de00493) plus a656b79b, e9722a6c and f9617d03 integrated as 51797b6. Root verified 125 tests across focused runs and both image/video job browser cases; all seven operational-list browser cases passed across focused runs. Root added the project-picker AbortSignal and made the HistoryTable test timestamp deterministic after a wall-clock tick changed the intended first page. The Data Manager saved-view navigation fix is delivered by child commit 063b519. It distinguishes explicit view navigation from condition URL writes, routes entity navigation through the same transition, and retires stale/unmounted save completions. Root passed 11 lifecycle unit tests and all seven Data Manager browser cases in one run (43.2 seconds), including creation, grouped update, reload, cancel and independent copy. Root retains selected media/documentation ownership. No remote action or release has been performed.

### Retention audit

A read-only source audit confirmed the separate Users/HistoryTable selection policies; wizard, assignment, whitelist, class-picker and settings searches retain their existing business-owned selections. Admin Analytics uses one local 30-day default with 7/30/90 choices across all four aggregate query keys. GlobalStageInspector and StageCard do not clear every class/write selection on model change; this rollout retains that existing behavior rather than claiming a broader reset contract. Existing tests for several retained selectors are rendering/interaction smoke tests, so they are not evidence for every possible dependency transition. Root ran 97 existing tests across 17 retained suites successfully. V0 also ran both existing workbench-video-candidate-decisions browser cases, covering native-select keyboard ownership, class-picker retry, duplicate in-flight decisions and task-switch late responses; both passed.

### Temporary validation resources

These databases were created fresh for this execution and verified to have zero public tables before migrations: annotation_filter_b0_1789189209_test, annotation_filter_a0_1789189209_test, annotation_filter_main_1789189209_test, and annotation_filter_browser_1789189209_e2e on the existing local PostgreSQL service.

Each lane also owns seven newly created, initially empty MinIO buckets with prefix filter-1789189209-LANE- and suffixes annotations, datasets, bug-reports, media-cache, audit-archive, import, and export. LANE is one of b0, a0, main, browser. Runtime overrides are process-local; shared .env is unchanged. Root dependency symlinks were detached after the primary install lacked two declared editor packages; a frozen-lockfile local install reused cached packages without changing manifests, lockfiles, or the primary checkout. Selected child dependency links point to this complete root install. Database isolation alone does not isolate fixed seed-media keys, so seed/reset/cleanup must use these buckets.

The main and B0 database/storage lanes are now released to the coordinator after worker checks completed. Browser validation uses its separate browser lane and reserved API/Web ports 18110/18111, which were free when checked. The task-only launcher /tmp/aap-filter-runtime-1789189209.py constructs connection settings in memory without printing credentials. Remove only these recorded task-owned resources after validation and retain any resources needed to diagnose a reported remaining failure.

The browser database was migrated successfully to Alembic revision 0165. A subsequent database-name/revision readback confirmed the intended target; current-month audit partitions already existed.

The browser acceptance fixture was increased from 51 to 101 objects/logical tracks after verifying EntityDataManagerLens retains its existing 100-row page size. This changes only fixture volume; production pagination is unchanged. API fixture assertions use the same 100-row boundary.

Workbench browser coverage: current-frame display versus all-loaded-frame batch acceptance, source/frame reset on task switch, and image hide/reload/show passed. Image visibility is checked against painted canvas pixels and API geometry readback; archived before/hidden/restored images were visually inspected. Byte-identical PNG comparison was replaced because it was stricter than the behavior contract. Commit 6e4a236 contains these cases. Video persisted-hide and software-WebGL point-mask coverage also landed as 9c7fdf23. No production Workbench patch was made for the screenshot-only false negative.

The persisted-video-hide case passed with reload, painted-canvas checks and unchanged geometry. The point-mask case passed on Chromium 147.0.7727.15, Legacy WebGL2 via ANGLE/Vulkan SwiftShader, DPR 1, 1440x900, with 34752 points (runtime source HEAD 6e4a236). It selects the mask and compares only scene pixels, excluding sidebars/counts; archived visible/hidden/restored crops were visually reviewed. This is software-renderer behavior evidence, not Apple WebGPU performance qualification.

Media audit before selected refresh: broken0, stale182, review-due1, current28 across211 referenced files;163 generation-provenance warnings. Many watched paths already span earlier UI/media work and fixture helpers. This rollout will refresh only visibly affected filtering screenshots and preserve unrelated review/provenance records.

The main lane additionally hosts the complete offline screenshot profile, using task-only Redis DB 14 (verified empty with no clients before use), API 18112 and web 18113. Its dedicated filter-screenshots-1789189209 worker consumes only media; database/migration target and broker readbacks were verified. Existing cached screenshot assets and nuScenes data were reused without downloads. Omit --cache-dir for the combined seed command because source-media and nuScenes defaults are separate caches.

Final static checks on the integrated packages passed: web ESLint (two existing warnings in EditUserModal and useOnboardingProjectState), CSS token lint, TypeScript, pinned Python lint, and pinned Python format check (1179 files). Documentation build passed; generated public OpenAPI/ADR mirrors must exist before the dead-link checker, which then passed. The docs-impact check over the original baseline through HEAD matched seven advisory rules and 25 recommendations. The rollout changes filter/read ownership and display predicates, not scheduler policy, task locking, inference execution, or public schema shapes; their unrelated runbooks/ADRs were not rewritten simply to satisfy broad path suggestions. Current behavior is documented in the affected guides and filtering/query references.

Eighteen completed child worktrees were removed through Orca after checking clean Git state and source equivalence against their integration commits. Where a worker was superseded or the coordinator added reviewed corrections, those differences were checked explicitly. Seventeen preserved cherry-picked branches were then deleted only after verifying their exact recorded heads; C0 was deleted by Orca as an ordinary merged branch. The private audit at /tmp/aap-filter-worktree-audit-1789189209.json retains all heads and integration mappings. The saved-view child integrated as 59216f75 and was removed after exact source-equivalence verification. All 19 task worktrees and their task branches are now removed.

Redis DB 13 was verified empty and unused before reserving it for the remaining browser checks. Earlier browser runs used the harness default broker with isolated database/storage and did not start inference workers; they are browser/API behavior evidence, not Celery/inference runtime qualification. The static screenshot lane remains on its independent Redis DB 14.

## Outcome

- All twelve packages are implemented or explicitly retained and verified. Shared field controls, typed values, URL state and validation are reused; domain APIs, SQL relations and Workbench write owners retain their responsibilities.
- Plan commit: 32e30fc6. Implementation commits and focused evidence are listed in the execution ledger; no release milestone was assigned.
- User documentation: projects/data-manager.md, projects/index.md, datasets/index.md, projects/project-templates.md, superadmin/user-management.md, audit-logs.md, bug-management.md, index.md and model-market.md, workflows/failed-prediction-recovery.md, reference/notifications.md, review/index.md, and affected Workbench/AI guides under docs-site/user-guide/.
- Developer documentation: docs-site/dev/reference/filtering.md (new), docs-site/dev/reference/data-manager-query.md, docs-site/dev/reference/design-system.md, docs-site/dev/concepts/workbench-shell.md and docs-site/dev/testing.md. API behavior is documented in docs-site/api/guides/projects.md and README.md; the route index reflects the guarded test fixture module.
- Browser behavior acceptance: 24 unique cases passed across focused runs: seven Data Manager, seven operational lists, four Workbench filters, one point-mask visibility, one real Scene quality query, two existing candidate-decision and two review/Issue flows. Software graphics evidence does not claim Apple GPU performance or real-model inference.
- Full web lint/CSS tokens/typecheck, pinned Python lint/format, relevant API/SDK/unit suites, documentation build and dead-link checks passed. The SDK retained all 134 compatibility tests without changes. Remote CI was not run.
- CHANGELOG: Unreleased entries describe the user-facing behavior. No package version change, push, deployment, or production-runtime refresh occurred.
- Selected media: both Data Manager screenshots were captured from clean 59216f75, then visually inspected along with the regenerated homepage WebP. Only those three assets and their provenance/review entries are refreshed. Visual-review records identify codex as the reviewer and bind to fd1ae10d; all three selected assets are current with no provenance issues. Unrelated stale media records remain outside this rollout.
- Cleanup: all 19 task child worktrees/branches, four disposable databases, 28 task buckets (381 objects), task Redis DBs 13/14, and task API/Web/media-worker processes were removed or stopped and verified. The primary checkout/runtime and shared asset caches remain outside this cleanup.

Selected media generation passed two desktop-light capture cases in 9.1 seconds on Chromium 147.0.7727.15, 1440x900, DPR 1. Capture source was clean 59216f75. Per-entry provenance was merged from actual screenshot-manifest attachments, preserving unrelated entries and the historic full-matrix metadata; the provenance reader now honors per-entry dirty-state evidence. The existing homepage generator used installed cwebp because this host's ffmpeg lacks libwebp; its narrow fallback was exercised successfully, with no dependency installation. Derivation records its actual dirty input state rather than inventing a clean derivation. PNGs and the 52,872-byte WebP were visually inspected. Strict image-manifest and media-integrity checks passed; two unrelated uncaptured scenes and the historic matrix seed revision remain advisory warnings. Documentation build and final dead-link/format checks passed.

Final media readback: integrity passed, broken 0, stale 191, review_due 0, current 20 across 211 referenced assets. All three selected assets are current without provenance issues. The remaining stale markers include earlier media and broad watched-source changes; they were not bulk-approved or regenerated. Full release-level media qualification is outside this non-release rollout. Final source/media commits include 59216f75 and fd1ae10d. All task listener ports 18110–18113 are free, and only the original non-task worktrees remain. No required implementation or acceptance work remains.
