# Data Manager and project member performance

> Status: complete. Parallel implementation, parent integration, adversarial review and local acceptance are finished. The delivered scope is whole-surface optimization and a project management dashboard for member contributions and bottlenecks. No release or remote publishing was requested.
> Reviewed against the current worktree on 2026-09-14. No release milestone is assigned.

## Product outcome

The project owner can answer three questions without leaving project context: what data needs attention, where delivery is blocked, and how each member contributes. Data Manager becomes a project data and operations surface with three sections: **Overview / Data / Members**. Tasks, objects and tracks remain grains inside Data, rather than treating members as another annotation grain.

The minimal option is a project-pinned link to the existing member dashboard plus a task table thumbnail. It is useful but does not meet the confirmed scope: the current metrics need correction, and existing batch commands cannot safely operate on an arbitrary task selection. The recommendation is four independently mergeable slices below, retaining existing queries, components and command services.

### Success criteria

- A project owner can reach a member's underlying contribution records from the project in two interactions.
- Every displayed total names its unit, project, time basis and applicable filters; table, detail and CSV use the same scope.
- Current members with zero activity remain visible. Missing history and uncollected time display an unavailable state rather than a fabricated zero or grade.
- A member switch, account switch or project switch cannot show a previous scope's late results.
- A task bulk action affects exactly the reviewed task IDs. The UI never silently expands a partial batch to the full batch.
- Existing saved views, nested expressions, old URLs, entity locations and batch visibility continue working.

## Verified baseline and gaps

| Area               | Current evidence                                                                                                                                                                   | Decision                                                                                                                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Data browsing      | `ProjectDataManagerPage.tsx` uses a 50-row task table; `EntityDataManagerLens.tsx` uses cursor loading and virtualization. Both duplicate their header and view rail.              | Share the page frame and toolbar arrangement. Keep the different pagination contracts; a 50-row table does not require a virtualization rewrite.  |
| Visual exploration | The checked-in Data Manager screenshot and current components show text rows with no media preview. `task_views.py` already calls `_attach_dimensions_batch` and `_task_with_url`. | Reuse signed thumbnail URLs and `Thumbnail`; add task list previews and a gallery.                                                                |
| Filters and charts | Nested filter expressions, saved views, URL restoration, server aggregates and some chart drill-downs already exist.                                                               | Preserve them. Improve arrangement, labels and field discovery; do not rebuild the DSL.                                                           |
| Project entry      | `ProjectSettingsPage.tsx` exposes Data Manager through settings.                                                                                                                   | Add a direct project-level entry and retain the existing settings link.                                                                           |
| People UI          | `AdminPeoplePage.tsx` uses a card grid, percentile bars and a composite ring; its detail request hardcodes `4w` separately from the list period.                                   | Use a comparison table for project members, inherit the actual applied period in details, and omit composite grades.                              |
| Quality            | `_first_pass_yield` in `services/dashboard_stats.py` counts submitted tasks with `reopened_count == 0`, including tasks without a completed review.                                | This is not a first-review pass rate. Do not carry the existing value into a new first-pass column.                                               |
| Time               | `useSessionStats` records task-switch intervals, discards intervals outside its window, and the shell supplies `annotate`.                                                         | Label existing data as recorded session time. Do not call it active work time or use it for ranked productivity.                                  |
| Time trust         | `api/v1/me.py` accepts client task/project IDs without verifying their relationship; current worker insertion is not retry-safe for duplicate IDs.                                 | Validate ownership, visibility, interval bounds and event identity before any new time-based metric becomes available. Legacy time is unverified. |
| Activity evidence  | Project person detail currently reads a global last-50 audit timeline.                                                                                                             | Project evidence must filter project, actor, time and visible task before ordering/pagination.                                                    |
| Bulk operations    | Data Manager has only single-row detail selection. Existing assignment commands operate on whole batches; export loading accepts project/batch scope.                              | Add genuine explicit task scopes; do not map task IDs to batch IDs.                                                                               |

Source anchors: `apps/web/src/pages/Projects/ProjectDataManagerPage.tsx`, `apps/web/src/pages/Projects/data-manager/`, `apps/web/src/pages/Admin/AdminPeoplePage.tsx`, `apps/api/app/api/v1/task_views.py`, `apps/api/app/api/v1/dashboard/admin.py`, `apps/api/app/services/dashboard_stats.py`, `apps/web/src/pages/Workbench/state/useSessionStats.ts`.

## Page and interaction design

### Shared project frame

- Keep the app's existing navigation, compact typography, semantic colors, local shadcn/Radix controls and Lucide icons.
- Put project breadcrumb/title first, section navigation second, and only relevant controls in the section toolbar.
- The existing URL without a new section key still opens Data. Add `section=overview|data|members`, leaving `lens=tasks|objects|tracks` exclusively for Data. Explicit new Overview links include their section key.
- Data owns the existing view/search/filter/sort/column keys. Members owns namespaced date, work-type, member-search and selected-person keys. Switching sections does not silently reuse annotation filters as performance filters.
- Data keeps its fixed viewport and internally scrolling result table. Overview and member details use normal content scrolling; do not force a tall management dashboard into the Data table's viewport.

### Overview

Show four compact current-state metrics: visible tasks, completed tasks with denominator, awaiting review, and unresolved feedback. Below, show delivery status and quality issues using existing aggregates. Put period-based team output in a clearly separate panel with explicit dates. Clicking a supported quality/status metric opens Data with a valid stored-value filter.

Do not repeat every chart from Data. Show at most two analysis panels at once: delivery status and the most useful configured quality distribution. Attribute/track-specific issues appear only when the project supports them. Trend charts require historical events; do not invent a trend from a current snapshot.

### Data

- Keep Tasks / Objects / Tracks as the inner grain switch. Hide unsupported grains.
- Make the view rail collapsible; group built-in, private and project-shared views. On narrow screens use the existing select pattern.
- Place search, filter, sort, columns and list/gallery in one toolbar. Applied chips and advanced AND/OR groups occupy the next row only when needed.
- Give the task identifier/filename a stable readable column. Move long source breakdowns and expensive secondary fields to optional columns or detail.
- Add small task thumbnails and an image/video gallery using existing thumbnail URLs, blurhash, lazy loading and error fallback. Keep pure point-cloud entries as explicitly labeled modality placeholders; no new BEV rendering pipeline.
- Details show preview, matching evidence, attributes, feedback and a precise Workbench location. Object crops are excluded from the first slice: detail responses deliberately omit raw geometry.
- Retain chart click-to-filter semantics, raw attribute values, independent chart theme refresh, saved-view dirty guards, invalid draft handling and selected-entity deep links.
- Loading, API failure, no visible tasks, zero matches, invalid saved views and expired thumbnails are separate states with a relevant recovery action.

### Members

Use a sortable table as the primary comparison surface. Filters are current project, work type (annotation/review), explicit time interval, member search, and an optional batch scope once it is supported consistently by every metric. Do not ship a batch filter that only filters the roster.

Default to the current project and the last seven local calendar dates through the current instant. Show the actual date range and timezone. Add today, last 30 days and a custom range of at most 90 days. Use `from <= timestamp < to`; the response echoes resolved UTC bounds, IANA timezone and `as_of`. Previous-period comparison uses an equal duration and the same scope; no-data comparison is unavailable rather than an infinite increase.

The annotation table contains member/project role, distinct submitted tasks, approved task outcomes, completed first-review outcomes when available, recorded time, and current backlog. A second work-type view shows review decisions, approvals, rejections, recorded review time and current review backlog. One person may have rows in both work types.

Clicking a member opens a detail sheet with output trend, review reasons, class/source distribution and a paginated evidence list. Trend dates, task lists and exported CSV inherit the table scope. A snapshot-owned task and its historical submitter are distinct identities in evidence.

Show every current project member and the owner, including no-activity members, disabled accounts and read-only roles. Default to current membership; a separate 'include historical contributors' option can include users with recorded project activity but no current membership, labeled 'not a current member'. Activity does not establish their former role or membership dates. Do not rank people by platform role or omit them solely because their account is inactive. No membership-history table is required for this view.

## Metric contract

| Dimension           | Metric and attribution                                                                                                          | Time / denominator                                                                                               | Availability rule                                                                                                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Output              | Submitted tasks: distinct task IDs per actual submitting actor; resubmission count shown separately.                            | Successful submit events inside the selected interval.                                                           | Expose the recorded event count; label incomplete history. Do not use current assignee for old submissions.                                                                     |
| Delivery            | Approved task outcomes: attribute to the assignee/submitter snapshot recorded for the approved round; show reviewer separately. | Approval event time; per-member distinct task count, with repeat approvals separately available.                 | Missing historical attribution is an unattributed bucket. Project distinct total is computed independently, not the sum of member totals.                                       |
| Contribution detail | Currently retained active, non-cancelled annotations created in the interval, grouped by creator, source and geometry type.     | Annotation creation time; describe it as retained content, not immutable historical output.                      | Separate manual, AI-assisted, imported and interpolated results; never give imported/AI output automatic manual-work credit.                                                    |
| Quality             | First-review pass rate: first completed review passes / all first completed reviews.                                            | Cohort selected by first review time. Pending review is outside the denominator. Show numerator and denominator. | Only complete, unambiguous task review histories qualify; unknown history is counted separately. A retry, resubmission or reopen cannot turn a failed first review into a pass. |
| Rework              | Rejected review rounds and reason distribution; unique affected tasks separately.                                               | Review decision event time, attributed to that round's recorded contributor.                                     | Read immutable outcomes, not the task's mutable latest reject reason.                                                                                                           |
| Review work         | Approve/reject decisions by actual audit actor; unique reviewed tasks separately.                                               | Decision time. Multiple rounds may increase decisions but not unique task count.                                 | A high approval ratio is not a reviewer quality score.                                                                                                                          |
| Efficiency context  | Recorded annotation/review session time, then median duration for comparable task types once collection is qualified.           | Session intersection with the selected interval; work types separated.                                           | Existing telemetry is incomplete. Display unknown/missing coverage; disable tasks/hour rankings until collection and deduplication are verified.                                |
| Load                | Current assigned pending/in-progress/review tasks; unassigned backlog shown at project level.                                   | Current state at `as_of`, independent of the historical period filter.                                           | Label as current snapshot. Age since submission is review waiting time, not active review time.                                                                                 |

Member totals are not automatically additive: a task may have several contributors or several approvals across a reassignment. Task count, object count, video keyframe count and logical track count remain distinct units. Compare within project, work type and compatible modality; do not introduce an unexplained weighted workload score.

First-pass quality does not measure ground-truth accuracy. Ground truth evaluation, multi-annotator agreement, payroll, formal performance grades, attendance tracking and configurable scoring weights are outside this proposal.

### Evidence and data collection

Reuse current workflow audit actions and `TaskEvent.kind`; do not create a parallel event platform. New successful submit/review events must preserve project, task, actual actor, contributor snapshot, result and a stable review-round reference in the same transaction as the workflow change. Inspect every submission path, including segmented video, before exposing historical quality as complete. Reuse stable existing event IDs as evidence identifiers.

For legacy events, resolve project through a trustworthy task relation or recorded project ID and retain only authorized evidence. Do not infer a missing contributor from today's assignee. Do not backfill first-pass percentages from `reopened_count`. Responses distinguish recorded/reconstructed/unknown coverage, and rates remain null if their denominator cannot be established.

Qualifying time collection requires closing the final task interval, binding time to the previous task/project/account/work type, stopping hidden/idle accumulation, and deduplicating retried events. This is a separate slice; the management table remains usable with recorded time labeled incomplete. No timesheet or payroll guarantee is made.

Before exposing collected time, ingestion must derive the project from the authorized task, reject a conflicting client project, validate kind/time/duration bounds, reject future or inaccessible intervals, and make duplicate client event IDs idempotent. Old client-reported project IDs cannot establish trustworthy attribution on their own. If this prerequisite is not delivered in the baseline, return null with `unverified_collection` and omit aggregate hours; do not simply display the old values with a small disclaimer.

## API and ownership boundaries

Reuse `services/dashboard_stats.py` for shared metric computation and the existing dashboard hooks/CSV machinery. Add `GET /projects/{project_id}/performance/members`, `GET /projects/{project_id}/performance/members/{user_id}`, `GET /projects/{project_id}/performance/members/{user_id}/events`, and `GET /projects/{project_id}/performance/export`. All call the same resolved scope and metric service. Do not silently redefine existing public dashboard fields; migrate existing consumers to corrected named fields deliberately.

List/export parameters are `from`, `to`, `timezone`, `work_type=annotation|review`, `account_status=all|active|inactive`, `include_historical=false`, `q`, `sort`, `cursor` and `limit`; detail/evidence accept the same date/work-type scope and use the path member ID. The baseline has no batch parameter. Return `project_role`, `account_status`, `is_owner`, nullable `member_since`, source coverage and explicit metric units. Treat the owner as an owner row even without `ProjectMember`.

The member-list response includes resolved scope, `as_of`, source coverage, project totals, paginated members and per-metric numerators/denominators. Page size defaults to 50 with a maximum of 100; sort keys are a server whitelist. Unsupported/invalid dates, work types and fields return validation errors. CSV carries raw values, units, scope and coverage, with the same field/formula-injection protections as existing export code.

Team data remains available only to the project owner and super admin, using the strict ownership rule from `_resolve_people_scope`. General project membership is not enough. Ordinary annotators/reviewers retain their self-only view; do not widen permissions merely by adding a Members tab. Member roster, evidence, count, export and query cache keys all respect these boundaries.

```text
Project Data Manager frame
  +-- Data query state ------> existing visible-task/read-model services
  +-- Members query state ---> shared project performance calculations
  |                              +-- task/annotation snapshots
  |                              +-- scoped workflow audit + session events
  +-- Explicit task selection -> action preview -> existing domain commands/jobs
```

No new service process, dependency, API credential, external account, CLI or environment variable is required. This is a multi-file change (more than eight files), including API contracts, UI, regression tests and documentation.

## Bulk actions

ADR-0047 currently specifies a read-only Data Manager. The confirmed bulk-operation scope intentionally extends that product boundary: keep entity read models read-only, and route task actions to their owning domain services. Record an ADR amendment alongside the first write capability.

### First delivery: explicit task selection

- Tasks only. Offer row selection and current-page selection; preserve explicit IDs across pagination in the same query, with a hard first-delivery cap of 200 tasks. Search/filter/view/project/account changes clear selection atomically; sorting alone preserves selected IDs.
- Do not provide 'select all matching' in this slice. Show both selected and matching totals so users cannot mistake the page for the complete result set.
- Preview contains action, exact task IDs/count, applicable parameters, eligible/skipped/failed counts and per-task reasons. Confirm operates only on those IDs and rechecks current authorization, task state, admin locks and applicable edit/review locks.
- If previewed eligibility or assignment changes before apply, reject as stale and require a refreshed preview; never silently apply to newly matching tasks.
- Reuse existing async job status/progress for exports and prediction runs. Handle retry without duplicate jobs, preserve failed-item details and refetch only affected data after success.

| Action                      | Existing foundation                                            | Required task-scope work                                                                                                                                                                                                                              |
| --------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Assign annotator / reviewer | Batch distribution preview/apply and assignment validation.    | Introduce task-level preview/apply using shared validators; validate active account and project role. Preserve batch policy and task workflow invariants. Never turn selected tasks into whole-batch reassignment.                                    |
| Export annotations          | Project/batch export jobs and `services/exporting/service.py`. | Carry explicit task IDs through API, job and loader; intersect project/visible scope at API and worker boundaries. Preserve format-specific scene/track packaging rules; unsupported partial formats must fail explicitly rather than widening scope. |
| Run AI preannotation        | `POST /projects/{id}/preannotate` accepts `task_ids`.          | Validate every ID belongs to this project and permitted task/batch state before enqueue, and revalidate in worker execution. Retain the existing configuration form and conservative overwrite policy.                                                |

The existing task-ID preannotation worker query is not sufficient authorization: it currently filters by task IDs without the complete project/state scope. Closing that gap is a prerequisite for exposing the Data Manager action, not a separate platform-hardening project.

Do not add bulk approve, reject, force-complete, delete or prediction cleanup to Data Manager in the first delivery. Their workflow/quality/destruction semantics need dedicated review and existing batch entry points remain available.

### Subsequent capability: all matching tasks

This requires a server-side selection snapshot, not iteration over browser pages. Resolve the authorized saved query into immutable IDs, bind the selection to actor/project/query and expiration, then preview and apply with fresh permission/state checks. Set explicit workload limits and use existing jobs for large actions. It is a separate product increment, not a prerequisite for explicit selection; no snapshot service is introduced now.

## Delivery slices and relative effort

| Slice                                   | Independently useful result                                                                                                                                                                                 | Relative effort / risk                                                                                                     |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 1. Project member baseline              | Project-pinned member table, complete current roster, current load, scoped successful submission/review evidence, matching CSV, no composite score. Unknown quality/time remains explicitly unavailable.    | Medium; metric and permission contracts dominate. Prioritize this slice because it directly serves the user's stated need. |
| 2. Data and overview layout             | Shared section frame, collapsed views, task thumbnails/gallery, clearer columns/detail, current-state overview and supported drill-down. Existing saved URLs continue working.                              | Medium; primarily frontend, with reused thumbnail and query APIs.                                                          |
| 3. Task operations                      | Explicit task-ID selection, task assignment preview/apply, scoped export, validated task preannotation and visible job results. Ship each action as its own coherent sub-change with its acceptance checks. | Large; assignment and export currently have batch/project scope.                                                           |
| 4. Quality and efficiency qualification | Correct first-review cohorts and rework attribution, complete workflow capture across modalities, improved session capture/deduplication and coverage-aware trends.                                         | Large; history gaps and video/workflow differences are the main risk.                                                      |

The interface mockup depicts the target state; first-review and time columns only become populated when their stated coverage rules hold. The baseline must not wait for slice 4 to provide useful output/load visibility. Explicit task operations do not depend on future all-matching selection.

## Acceptance and implementation handoff

### Primary files

- Data UI: `ProjectDataManagerPage.tsx`, `data-manager/EntityDataManagerLens.tsx`, `DataManagerLensTabs.tsx`, `DataManagerOverview.tsx`, `DataManagerAnalyticsPanel.tsx`, detail sheets and `dataManagerUrlState.ts`.
- Member UI and shared callers: `Admin/AdminPeoplePage.tsx`, `Me/MyPerformancePage.tsx`, `api/dashboard.ts`, `hooks/useDashboard.ts`, and project route/navigation owners.
- API/data: `api/v1/dashboard/admin.py`, `services/dashboard_stats.py`, `api/v1/data_manager.py`, `api/v1/task_views.py`, task lifecycle/review and segmented-video workflow, existing assignment/export/preannotation services.
- Time qualification: `useSessionStats.ts`, `useWorkbenchShellModel.tsx`, event ingestion schemas/API/model and focused event tests. Load the Workbench-state skill before modifying ownership/lifecycle.
- Regenerate changed OpenAPI/API types using the repository scripts. Any additive migration must preserve existing records; use the runtime skill for isolated environment and worker refresh.

### Meaningful checks

1. Owner versus super admin versus ordinary member; guessed project/member IDs; a project admin who is only a member of another owner's project; hidden batches; CSV and timeline scope. Preserve the self-only route without exposing team aggregates to ordinary members.
2. Active member with zero events; viewer; owner without a membership row; disabled/departed contributor; a person doing both annotation and review.
3. Submit without review; reject then resubmit then approve; repeated approve after reopen; reassignment; audit history missing/expired; imported/AI/segmented-video contribution; distinct project total versus summed member totals.
4. UTC/local midnight, partial current day, exact exclusive end bound, equal-length comparison window, list/detail/CSV agreement.
5. Task/project mismatch and inaccessible-task ingestion; future/negative/inconsistent intervals; task interval closure; project/account/work-type switch; background/idle pause; duplicate client IDs; missing time coverage and zero denominator.
6. Existing OR expressions, same-object filters, invalid numeric drafts, saved-view dirty state, URL back/forward, account change with requests in flight, raw-value chart drill-down, theme refresh without chart remount.
7. Partial-batch selection; foreign-project IDs; stale preview; locked/ineligible tasks; disabled assignee; role mismatch; duplicate apply/job retry; scoped export and scene/track format rejection; worker revalidation.
8. Desktop 1440/1024 and narrow 390 layouts, both themes, keyboard navigation, loading/error/empty states, thumbnail failure and detail focus restoration.

Run appropriate focused checks for the delivered slice, not the entire future plan:

```sh
rtk pnpm --filter @anno/web test src/pages/Projects/ProjectDataManagerPage.flow.test.tsx src/pages/Projects/data-manager src/pages/Admin/AdminPeoplePage.test.tsx
rtk pnpm --filter @anno/web typecheck
rtk pnpm --filter @anno/web lint:css-tokens
rtk pnpm --filter @anno/web lint
rtk pnpm openapi:export
rtk pnpm codegen
rtk git diff --check
```

Backend acceptance uses the existing pytest framework, starting with `test_dashboard_people_project_scope.py`, `test_dashboard_admin_people_export.py`, `test_dashboard_my_performance.py`, `test_task_events_batch.py`, `test_batch_assignment_guards.py` and affected export/Data Manager tests. Run from `apps/api` with `rtk uv run pytest ...` only after verifying a disposable `TEST_DATABASE_URL` through the runtime/test configuration. Browser acceptance uses the installed project agent-browser workflow; `e2e/tests/filter-data-manager.spec.ts` provides existing filter coverage. Test listing alone is not browser execution.

For performance, preserve server-side aggregation and pagination; query member metrics by grouped SQL instead of N queries per member. Compare representative large-project query plans and latency with the pre-change baseline in an isolated database. Add specific indexes only for a measured query problem; no speculative analytics warehouse or new cache tier.

### Documentation and rollback

Update `docs-site/user-guide/projects/data-manager.md`, project entry documentation, member/self-performance guidance, `docs-site/dev/reference/data-manager-query.md`, filtering/permission reference and `CHANGELOG.md` Unreleased as affected slices ship. Update API docs/README and generated artifacts when contracts change. Record the read-only-boundary extension in ADR-0047 or a linked superseding ADR. Do not claim new behavior in user documentation before implementation.

UI/read API changes can be reverted without altering annotation data. Retain additive capture fields and historical events during rollback rather than erasing them. Completed assignments require an explicit validated inverse assignment; queued jobs use their existing cancellation behavior. No plan claims a frontend rollback undoes previously executed operations.

## External design references

- [HumanSignal member performance](https://docs.humansignal.com/guide/dashboard_annotator.html): project/member/date context, separate annotation and review work, distinguish absent data from zero, and expose detailed activity.
- [CVAT analytics](https://docs.cvat.ai/docs/qa-analytics/analytics/): keep analytics in the relevant project/task context and distinguish stage time from output. This plan does not introduce CVAT as a dependency.
- [TanStack Virtual fixed examples](https://tanstack.com/virtual/latest/docs/framework/react/examples/fixed): reuse installed headless virtualization for large entity lists; retain the small paginated task table.

These references inform interaction choices. They do not establish the correctness of this project's metric definitions; local workflow evidence and regression checks do.

## Planning verification

The repository implementation, current documentation, relevant archived plans and the checked-in Data Manager screenshot were inspected. A separate interaction mockup uses explicitly fictional data and does not read or mutate application data. Application behavior and backend tests are not claimed as validated by that mockup.

## Implementation tracking

All implementation worktrees start from local base `b1dd26a40c0b63a0b2c6ad870f4cf5a727c794b6`. The parent owns final integration, project navigation, documentation, generated contracts, live browser checks and requirement-by-requirement acceptance.

| Worker         | Isolated branch              | Owned implementation                                                       |
| -------------- | ---------------------------- | -------------------------------------------------------------------------- |
| metrics_audit  | worktree-agent-dm-metrics    | Project performance API, metric queries, workflow audit coverage and tests |
| members_ui     | worktree-agent-dm-members-ui | Member table/detail, scoped URL/API hooks and tests                        |
| browser_layout | worktree-agent-dm-browser    | Data Manager frame, overview, browsing, gallery, task selection and tests  |
| bulk_audit     | worktree-agent-dm-actions    | Task action previews, assignment/export/preannotation scope and UI         |
| time_capture   | worktree-agent-dm-time       | Qualified session collection, authenticated ingestion and idempotency      |

Baseline evidence: the parent isolated test-mode database reached migration `0167`; the existing Data Manager and project people-scope tests passed (22 tests). This establishes the pre-change baseline only.

## Outcome

- Landed application work through `8f4d6dbf`, including parent integration `dda9533a`, effective assignment and CSV scope `317d5b39`, navigation `54a72444`, export/review facts `d2dbbe87` and `e97461ae`, collector recovery `4318208d` and `e639e416`, metric scope `2e988e62` and `70f4b3ae`, and membership revocation `e29e9844`.
- Release milestone: Not yet determined. No version bump, push, deployment or remote CI run was requested or performed.
- User documentation: `docs-site/user-guide/projects/data-manager.md`, `docs-site/user-guide/projects/index.md`, and `README.md`.
- API documentation: `docs-site/api/guides/projects.md`, `docs-site/api/guides/auth.md`, and the regenerated `apps/api/openapi.snapshot.json` / frontend API types.
- Developer documentation: `docs-site/dev/concepts/project-performance.md`, `docs-site/dev/concepts/visibility-and-permissions.md`, `docs-site/dev/concepts/scheduler-and-task-dispatch.md`, and `docs-site/dev/reference/data-manager-query.md`.
- ADR: amended `docs/adr/0047-data-manager-entity-read-model.md` to keep queries read-only and task commands independently authorized.
- CHANGELOG: Unreleased entries cover browsing, project member performance and exact-task operations.

### Delivered behavior

- Overview / Data / Members retain legacy Data URLs, saved nested filters, independent section scopes, dirty guards, and task/object/track locations. Browsing adds thumbnails, a gallery, collapsible view groups, narrow-screen scrolling and keyboard-safe selection. Point-cloud previews use a labeled placeholder.
- Members include the owner, inactive and zero-activity members, with an explicit historical-contributor option. Tables, details, evidence and CSV share project/date/timezone/work-type scope. CSV carries units, raw values, coverage and resolved boundaries.
- Submissions exclude skips. Reviewers receive decision credit; annotation contributors receive snapshot-based outcome credit. First-review contributor snapshots are saved before audit retention, later rounds do not borrow the first-round snapshot, and legacy video submission gaps mark coverage partial.
- Session collection validates task/project/user relationships and interval bounds, unions overlapping qualified intervals in SQL, preserves client UUID idempotency, and recovers valid records from mixed stale batches. Queue loss markers remain unverified through both API and worker persistence; recorded time is not attendance or proof of human work.
- Assignment preview/apply, export and preannotation accept explicit task scopes up to 200. Effective task overrides and batch defaults agree across browsing, filters, backlog, lifecycle and locking. Removed members lose task and job access without rewriting historical attribution.
- New selected-task export jobs build fresh artifacts; only retries of the same durable job reuse the export cache. This deliberately avoids a partial dependency fingerprint for predictions, media and scene content.

### Parent acceptance evidence

| Check                      | Result                                                                                                                                                                                                                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Backend primary regression | 262 cases executed. 255 passed initially; seven fixture failures were corrected by adding real reviewer membership or refreshing server-generated timestamps in the shared test session. All seven then passed in the 116-case permission/task/filter regression. No remaining failure is being treated as passed. |
| Frontend regression        | 120 tests passed across 18 suites; the subsequent keyboard/ARIA changes passed the focused 18-test Data Manager flow/frame run.                                                                                                                                                                                    |
| Browser E2E                | 10/10 passed: member access/CSV, independent analytics scope, nested expressions, saved-view persistence, cursor totals, restricted visibility and invalid drafts.                                                                                                                                                 |
| Live layout and keyboard   | Inspected 1440, 1024 and 390 layouts and both themes. The final mobile Members page had viewport/body/page width 390 and three loaded member rows. Gallery Space toggles its checkbox without opening task details. No browser console errors were observed.                                                       |
| Accessibility              | Data Manager gallery ARIA, region labeling and keyboard defects were fixed. Remaining light-theme axe contrast findings are two existing global-header nodes (workspace label and shortcut hint); gradient contrast checks remain incomplete. This is not a full WCAG certification.                               |
| Query volume               | 2,000 tasks / 20,000 intervals: member query used 9 SQL statements and took 1.217 s locally; evidence used 11 SQL statements and returned 5 cursor-paged rows. This is a synthetic local sample, not a production capacity claim.                                                                                  |
| Real export worker         | Jobs `86c60393-e321-4303-bba8-314a2da81aa2` and `5e0ecae5-a520-4611-9659-02f37c0a3279` completed; each downloaded AAP JSON ZIP contained only `T-E2E-FILTER-I-SAME`. Both new jobs reported `cache_hit=false`; repeating an idempotency key returned its original job.                                             |
| Real preannotation worker  | Job `3a598c12-111d-4548-aebd-8d07fa7f7220` completed for one selected task using the repository protocol stub through the actual CPU worker. This validates dispatch/scope/protocol behavior, not real-model inference.                                                                                            |
| Runtime                    | Parent E2E schema is `0170`; fresh ordinary and maintenance workers were checked for registration and exact queue subscriptions. The ordinary worker includes `export` and `ml.cpu`; maintenance consumes only `maintenance`. Retained preview is on Web 3101 / API 8101.                                          |
| Static checks              | Web typecheck, lint, CSS token lint, build and bundle budgets passed. Two existing ESLint warnings remain. OpenAPI export/codegen, docs build, image manifest/orphan checks and `git diff --check` passed.                                                                                                         |
| Media                      | Four final captures passed (Data, filter picker, light/dark Members), followed by the affected Hero derivative. Capture entries record the actual source commit and dirty-state facts; unrelated provenance was preserved. Only changed assets receive fresh agent visual review.                                  |

Detailed local logs, diagnostic scripts, runtime ZIPs and screenshots are retained under ignored `output/data-manager-plan/`. The checked-in user guide contains the published captures. Repository-wide media audit still reports older/coarsely watched assets outside this task; their reviews were not blanket-renewed.

### Integration and cleanup audit

Eleven task-created Orca worktrees and their task branches were removed after clean-state, idle-terminal and patch-equivalence checks. Used worker test resources and the parent's disposable test mode were destroyed with their exact ownership confirmations. The parent's retained E2E preview and existing development-mode data remain available.

Two workers violated the explicit database isolation instructions. The earlier time worker temporarily applied and reversed additive columns on the primary database; the parent subsequently verified primary schema `0167`. Shared `annotation_test` was also used incorrectly, including a later test run after an initial restoration. With no other sessions and zero task/time rows, the parent restored its exact temporary schema changes to `0167` and verified both databases again. The final read-only primary check showed 50 tasks and 98 time records; no business-row deletion was performed by the restoration. These checks are not a claim of a full database-content audit.

One worker explicitly skipped an unavailable Prettier hook. The parent did not rely on that worker's gate result: formatting, normal integrated commit hooks, focused backend tests and the final frontend checks were completed locally.

### Scope limits

No scoped implementation blocker remains. Full-matching selection, bulk deletion/approval, formal grades, attendance/anti-cheat, task-difficulty calibration and comparable-type efficiency baselines remain outside the confirmed increment. Historical gaps remain visible instead of being backfilled from current ownership. Production deployment, real-model qualification and remote CI are separate work.
