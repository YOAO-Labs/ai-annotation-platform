# Data Manager browsing and saved annotation visibility

> Status: implemented and locally verified.
> Reviewed against the current checkout on 2026-09-15.
> The user explicitly confirmed that the Data content may exceed one viewport and use page scrolling. No release milestone is assigned.
> Follow-up decision: split the task list's mixed feedback column into unresolved issues and comments.

## Outcome and classification

| Request                                        | Classification                                               | Recommended result                                                                                | Relative effort / risk                                              |
| ---------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Longer task/object lists                       | Accepted usability improvement                               | Natural-height Data content with page scrolling; retain task pagination and entity virtualization | Medium; scroll ownership and cursor loading                         |
| Annotation image preview                       | Accepted capability gap                                      | A shared, read-only image preview in the existing task and object detail sheets                   | Medium; geometry alignment, masks and asynchronous ownership        |
| Annotated images/objects in member performance | Existing backend capability with a presentation and unit gap | Show saved content before submission in member rows and project totals; add an actual-image count | Small to medium; attribution and counting semantics                 |
| Separate unresolved issues and comments        | Confirmed counting and presentation correction               | Two task columns aligned with the Workbench's issue and comment totals                            | Small to medium; mixed-source comments and saved-view compatibility |

The minimal solution is page scrolling, click-to-open annotation previews, two saved-content metrics in the existing performance surface, and distinct issue/comment columns. This plan uses that solution. It adds no service, database table, migration, environment variable, third-party account or dependency.

The four slices are independently mergeable. Recommended implementation order: scrolling, issue/comment counts, saved-content metrics, then image preview. Slice numbers remain stable for references.

## Original verified baseline

- `DataManagerFrame.tsx:34`, `DataManagerLensTabs.tsx:27`, and both result owners constrain Data to `h-full` / `min-h-0` / `overflow-hidden`. In the supplied screenshot the task table starts about halfway down the view and shows roughly five complete rows.
- The task table currently renders 50 tasks per page. The object/track table uses `useVirtualizer`, a table-local scroll ref, 46-pixel row estimates, and cursor loading near the last rendered item (`EntityDataManagerLens.tsx:515`).
- `TaskPreview` only renders `Thumbnail` (`ProjectDataManagerPage.tsx:1880`). Both `TaskMatchesSheet` and `EntityDetailSheet` currently display metadata rather than annotation geometry.
- `Thumbnail.module.css` uses `object-fit: cover`; overlaying image coordinates directly on that cropped thumbnail would misalign annotations.
- `_annotation_activity()` already counts `contributed_tasks` and `retained_objects` without a submit-state condition (`services/project_performance.py:804`). The member table omits them; member detail and CSV already expose them.
- `contributed_tasks` includes all actual task file types. A project labelled as an image project can contain video tasks, so relabelling this metric as images is incorrect. Task building copies the data item's file type (`services/dataset.py:119`), and the existing test seed explicitly includes video tasks in an image project.

The previous completed plan and current user guide deliberately specify a fixed viewport for Data. The user's new, confirmed requirement replaces that layout decision for the Data section; update the current guide during implementation and preserve the old plan as history. ADR 0047's read-only entity and visibility boundaries still apply.

## Slice 1: longer Data content with page scrolling

### Behavior

- Let the task/object content extend below the viewport. Scrolling moves the page's title, summary and query controls out of the way, allowing the list to occupy the visible page.
- Use one vertical scroll owner for the Data content. Do not substitute a larger fixed-height inner table that leaves the same nested vertical scrolling problem.
- Keep task pagination at 50 rows; the current page flows vertically and its pagination remains reachable after the final row. The gallery follows the same page flow.
- Preserve object/track cursor pagination and virtualization. They observe the Data page scroll owner rather than an independently scrolling short table.
- Preserve horizontal access to all columns and column/header alignment. Keep result headers sticky while their results are visible; ensure horizontal overflow wrappers do not accidentally become the vertical sticky owner.
- Retain the current view sidebar, filters, selection rules and saved URLs. Project Overview and Members retain their own existing scrolling behavior.
- Switching query, sort, saved view or entity scope resets to the result start. Loading another cursor page preserves the viewport position. Task page changes return to the result start. An empty result retains the existing useful empty-state height.

### Implementation ownership

Use a Data-specific scroll ref owned by `ProjectDataManagerPage` and attached by `DataManagerFrame`; pass it to the task and entity result owners. Give the frame natural-height inner content for `section=data` and remove the descendant height/overflow restrictions that currently clip it. Keep this change local to Data Manager; `AppShell` already provides the application's viewport shell.

For entities, update `getScrollElement`, result-start measurement, row offsets and reset logic together. Use the existing virtualizer's `scrollMargin` for content above the list; remeasure when summaries, query controls or analytics change height. Keep stable entity keys, the existing row estimate/overscan and the existing next-cursor contract.

```text
ProjectDataManagerPage (Data scroll ref)
  -> DataManagerFrame (scroll owner and natural-height content)
  -> DataManagerLensTabs
       -> task table/gallery (50-row page)
       -> EntityDataManagerLens (virtual rows and cursor loading)
```

Primary targets: `ProjectDataManagerPage.tsx`, `data-manager/DataManagerFrame.tsx`, `DataManagerLensTabs.tsx`, `EntityDataManagerLens.tsx`, and `EntityDataManagerLens.module.css`.

### Acceptance

- At the supplied screenshot's approximately 1580 x 890 viewport, the page can scroll the controls away and expose substantially more rows; the task page's 50th row and pagination are reachable without a separate short vertical table scrollbar.
- Object results still load row 101 and later through page scrolling, without duplicates, blank virtual areas or fetching the entire collection at once.
- Expanding analytics, adding multi-line filters, switching list/gallery, changing tabs and resizing do not corrupt offsets or hide the result area.
- Validate 1440 x 900, 1024 x 768 and 390 x 844, both themes, keyboard access and wide-column horizontal scrolling.

## Slice 2: saved annotations in member performance

### Metric contract

All saved-content metrics use the current project and the applied member scope. Eligible records are active, non-cancelled `Annotation` records with `from <= created_at < to`, attributed by `Annotation.user_id`. They do not require task submission or approval. Local unsaved edits and `AnnotationDraft` records are not counted.

| UI label   | API metric                                  | Definition                                                                                              |
| ---------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 已标注图片 | `annotated_images`, unit `images`           | Distinct task IDs with at least one eligible annotation, restricted to actual `Task.file_type == image` |
| 已标注对象 | Existing `retained_objects`, unit `objects` | Number of eligible retained annotation records                                                          |
| 贡献任务   | Existing `contributed_tasks`, unit `tasks`  | Existing all-modality distinct task count; retain it in detail and export                               |

Use an explicit table/tooltip explanation: these are records created in the selected interval that are still retained now. Deletion or cancellation can reduce them. They are saved content rather than a permanent historical production ledger.

- Several objects on one image count as one image and several objects. Repeated saves and edits do not create additional counts.
- An empty image with no retained annotation counts as zero annotated images, even if its task was completed; its workflow result remains represented by submission/review metrics.
- An image/Scene frame task counts once. Video files and point-cloud tasks do not count as images, regardless of the project's label. The same physical media linked as different tasks counts by task, not by file checksum.
- Each member may count an image they contributed to; project image totals must use an independent distinct-task aggregation rather than summing member rows or the current member page.
- Preserve `retained_objects` semantics across modalities: compact video tracks count as one retained record, while Scene per-frame instances count separately. State this in the definition and retain the geometry breakdown.
- Preserve source distinctions: manual, accepted AI, imported and interpolated records remain distinguishable in detail. Pending predictions do not count. The metric is not labelled as pure manual labor.
- Attribution stays with the recorded creator/acceptor/importer. Reassignment and another member editing an existing object do not transfer the original count. Imported records may retain original creation timestamps.
- Moving yesterday's existing boxes today does not create today's new output. The selected date range remains authoritative; this change does not silently turn the date-filtered metrics into all-time totals.

### API and UI changes

1. Extend the existing grouped annotation query with an actual-image distinct count. Reuse its eligibility predicates for independently calculated project totals; do not materialize all annotation IDs in Python.
2. Add `annotated_images` to member metrics and add `annotated_images` plus `retained_objects` to `PerformanceTotals`. Extend the backend and frontend metric-unit literals with `images`. Keep all existing metric keys and meanings.
3. In the annotation-work view, place image/object columns before submission counts and show the two saved-content values in the project's summary. Update loading rows and table spans. Keep review-work columns and existing submit/review trend definitions.
4. Add sorting by `annotated_images` and `retained_objects` to the service and the member URL-state allowlist, preserving stable UUID tie-breaking and scope-aware pagination.
5. Show the image count alongside retained content in member detail. Add it to `_CSV_METRICS`; reuse the existing exported object count rather than adding a duplicate column.
6. Refreshing or reopening the page after a successful annotation save retrieves the new counts. Preserve existing account/project query ownership; no new polling loop or telemetry collector is needed.

Primary targets:

- `apps/api/app/services/project_performance.py`
- `apps/api/app/schemas/project_performance.py`
- `apps/web/src/api/projectPerformance.ts`
- `apps/web/src/pages/Projects/data-manager/ProjectMembersPerformance.tsx`
- `apps/web/src/pages/Projects/data-manager/projectMembersPerformanceUrlState.ts`
- The affected OpenAPI snapshot and generated API types.

### Acceptance

- Save two objects on one image without submitting: member row, member detail, project total and CSV show images 1 / objects 2 / submitted 0.
- Delete one object: images 1 / objects 1. Delete or cancel the last object: images 0 / objects 0. Repeated saves do not increase counts.
- Two members each create an object on the same image: each has images 1, project images 1 and objects 2.
- A video task inside an image-labelled project cannot inflate image counts. The existing all-modality task/object metrics retain their contract.
- Cover date boundaries, edits across dates, task reassignment, source distinctions, zero-activity members, historical contributors, independent totals under pagination, sorting, URL restoration, matching CSV and authorization.

## Slice 3: annotation image preview

### Interaction and supported content

- Reuse task row/thumbnail/gallery clicks to open `TaskMatchesSheet`; add a large preview above its existing matched-object details.
- Add the same preview to `EntityDetailSheet` for image objects and highlight the selected object in the full image.
- Default to showing saved annotations, with a simple show/hide toggle for comparing the original image. Preview interaction is read-only; the existing workbench link remains the editing entry.
- First delivery covers ordinary images and the current task of Scene images: bounding boxes, rotated boxes, polygons with holes/multiple parts, polylines, keypoints with configured skeletons, and actual raster masks.
- The full-image preview shows the task's formal annotations. Existing matching-result metadata remains separately labelled; pending AI/tracker candidates are not silently rendered as accepted annotations.
- Videos, video trajectories and point-cloud/multi-camera projections keep their current metadata and workbench navigation. Dense annotation overlays on every tiny table thumbnail, automatic object cropping and an additional preview service are outside this delivery.

### Data and rendering

Create `data-manager/DataManagerAnnotationPreview.tsx`, shared by the two existing sheets. Load its rendering module lazily so opening the Data list alone does not initialize a workbench canvas.

- Read only the currently opened task's media and annotations. Reuse the task read API and the existing `/tasks/{id}/annotations/page` endpoint, adding the missing client adapter with `AbortSignal`. Use its existing 200-item page size.
- Render the first page progressively and show an explicit loaded count and a load-more control while another cursor exists. When an object was selected, continue through cursors until it is found or the task's annotation pages are exhausted; never infer deletion from absence on the first page.
- Keep Data Manager entity query responses free of raw geometry, in accordance with ADR 0047. Existing annotation endpoints enforce `annotations:read` and task visibility.
- Fit the complete image inside the preview. Image pixels and annotation geometry must share the same scale and offset, including letterboxing. Reuse the existing preview/overview URL helper for large images.
- Reuse `ImageStageShapes`, the annotation visual helpers and image raster-mask descriptors/rendering. Disable editing, transforms, dragging and mutation handlers. Pass the current project's class colors and skeleton configuration explicitly; do not depend on whichever project the Workbench last stored globally.
- Preserve rotated geometry, polygon holes, z-order and mask pixels. A mask loading failure must be indicated; a bounding rectangle is not a substitute for a mask.
- Cache/isolate by authenticated owner, project and task. A task switch, account/project change or sheet close cancels obsolete requests and prevents late media/geometry from appearing in a newer preview. Extend the existing mask-content read adapter to accept cancellation and release preview-owned mask/image resources on close.
- On reopen, revalidate the current task's saved annotations. Handle missing media, expired URLs, unavailable dimensions, inaccessible/deleted entities and partial page/mask failures explicitly. A media URL failure may re-fetch task metadata once for a fresh signed URL; retain an actionable retry after failure.

Primary targets: the new shared preview and its focused tests; `TaskMatchesSheet.tsx`, `EntityDetailSheet.tsx`, `api/tasks.ts`, the preview query hook, `ImageStageShapes.tsx` and its explicit color/config dependencies, plus `api/rasterMasks.ts` and the existing mask read lifecycle where cancellation is forwarded.

### Acceptance

- Image and geometry align for landscape/portrait images, letterboxing, rotated boxes, polygon holes, keypoint skeletons and raster masks. Switching between projects uses the correct class colors.
- Selected objects are highlighted even when absent from the first 200 annotations. A partially loaded preview is visibly partial.
- Previewing and toggling overlays issue no annotation mutations, task claims, locks or workflow changes.
- Opening the task list alone triggers no per-row geometry/mask reads. Opening one preview reads only that task; closing or switching prevents obsolete content from rendering.
- Confirm empty, loading, partial-failure, deleted/inaccessible object, URL-expiry and large-image states; verify keyboard access and both themes in a real browser.

## Slice 4: separate unresolved issues and comments

### Confirmed behavior

Replace the task list's `反馈` column with two independently selectable columns, `未解决问题` and `评论`. Show both in the default task view and use the same labels in gallery summaries.

| Column     | Definition                                                                                                            | Exclusions                                                                                                |
| ---------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| 未解决问题 | Active, valid root issue records associated with the task: `kind=issue`, `thread_parent_id IS NULL`, `status=open`    | Replies, ordinary comments, resolved/shelved/deleted issues, bugs and task rejection records              |
| 评论       | The current task's complete Workbench comment-feed total: active native task comments plus active annotation comments | Issue replies, issue main posts, feedback mirrors of annotation comments, bugs and task rejection records |

Both values are current task-wide counts, independent of the member-performance date range and of the number of rows loaded in a discussion panel. Comments are the total currently retained ordinary comments, not unread messages or unresolved items. Deleting a comment decreases its count. An old comment attached to an unavailable annotation follows the same retention rules as the Workbench comment feed.

Example: one open issue, two replies to it, three task comments and one annotation comment display `未解决问题 1 / 评论 4`. Resolving the issue changes the display to `0 / 4`; replies never inflate either column. Reopening the issue changes it back to `1 / 4`.

### Existing owners and implementation

The existing `_unresolved_feedback_count_sq()` in `data_management/task_filters.py:1065` counts every active open feedback row, without filtering kind or thread parent. It therefore mixes issues, replies and other record types. This is a counting correction as well as a column-label change.

The Workbench already has the desired distinction:

- Issues use `FeedbackService`'s valid root relation with `kind=issue`, `root_only=true` and `status=open` (`DiscussionIssuesTab.tsx:114`).
- Comments use `services/task_discussion.py`'s authoritative mixed-source relation. `_identifier_selects()` combines `annotation_comments` with native root task comments in `annotation_feedbacks`; it deliberately excludes mirrored annotation comments and issue replies. `DiscussionPanel.tsx:170` reads that complete feed's total.

Implementation steps:

1. Reuse/extract these SQL predicates in their existing service owners so Data Manager can count them by task. Preserve the complete feed's source distinction and deleted-annotation behavior; counting only `kind=comment` in the feedback table would both miss annotation comments and count issue replies.
2. Add task projection fields `unresolved_issue_count` and `comment_count`, and corresponding schema columns, sort fields and numeric filters `issue.unresolved_count` / `discussion.comment_count`. Compute counts in SQL under the visible-task scope; do not call the discussion endpoint separately for every task row or hydrate whole comment threads.
3. Retain the old `unresolved_feedback_count` column/sort key and `feedback.unresolved_count` filter as compatibility aliases to the corrected issue count. Normalize an explicitly saved old feedback column to the new issue column when restoring it; do not rewrite saved views in the database. Existing customized column lists retain their other selections and may opt into the new comment column.
4. Keep the built-in view key `feedback-open` stable, relabel it `有未解决问题`, and use the corrected issue predicate. Replace the task quick filter `有反馈` with `有未解决问题`; expose comment-count filtering through the normal filter picker.
5. Align Data Manager's issue summary and issue-related drill-down with the corrected root count. Where an object-level issue count is displayed, apply the same root-issue definition restricted to that annotation. This slice adds the new comment column to task results; it does not introduce a separate object discussion browser.
6. Update labels, projection types and affected API artifacts. Existing feedback status/type query capabilities remain separate from the two new column meanings; do not repurpose a general feedback-status filter as a comment-status filter.

Primary targets: `apps/api/app/services/task_discussion.py`, `services/feedback.py`, `services/data_management/task_filters.py`, `views.py`, `schema.py` and `entities.py`; task response schemas and `api/v1/task_views.py`; `apps/web/src/api/taskViews.ts`, `ProjectDataManagerPage.tsx` and affected summary/URL compatibility callers.

### Acceptance

- Verify the `1 issue + 2 issue replies + 4 ordinary comments` example above against both Workbench totals and Data Manager task columns.
- Resolve, shelve, reopen or delete the issue; ordinary comment counts remain unchanged. Deleted roots and their surviving replies do not leave a phantom unresolved issue.
- Native task comments and annotation comments each count once. A mirrored annotation comment does not count twice; a reply to an issue never counts as an ordinary task comment.
- Cover comments on unavailable annotations, empty tasks, bugs, rejection records, historical feedback rows and cross-project/cross-batch visibility.
- Verify default and customized columns, old saved-view/URL aliases, both new sorts, numeric filters, the built-in unresolved-issue view and the summary drill-down.
- Verify task-query request counts and the absence of per-task discussion HTTP requests. Counts cover the full task discussion, including comments outside the currently loaded page.

## Verification and documentation handoff

This implementation crosses more than eight files once schemas, client types, tests and documentation are included. Keep the four slices independently reviewable and preserve unrelated work.

Use the existing checkout's Python virtual environment. Before database/browser tests, read `aap-runtime`, inspect `pnpm dev:worktree -- doctor`, and verify the intended disposable test/E2E environment. Read the project `agent-browser` skill before live browser acceptance; prefer the installed CLI and inspect network and console evidence.

Focused verification commands after the corresponding changes:

```bash
pnpm --filter @anno/web test src/pages/Projects/ProjectDataManagerPage.flow.test.tsx src/pages/Projects/data-manager/EntityDataManagerLens.test.tsx src/pages/Projects/data-manager/DataManagerFrame.test.tsx
pnpm --filter @anno/web test src/pages/Projects/data-manager/ProjectMembersPerformance.test.tsx src/pages/Projects/data-manager/projectMembersPerformanceUrlState.test.ts src/api/projectPerformance.test.ts
pnpm --filter @anno/web test src/pages/Projects/data-manager/DataManagerAnnotationPreview.test.tsx
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint:css-tokens
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_project_performance.py tests/test_project_performance_http.py'
pnpm openapi:export
pnpm codegen
pnpm dev:worktree -- exec --mode e2e -- pnpm --filter @anno/web test:e2e e2e/tests/filter-data-manager.spec.ts e2e/tests/data-manager-management.spec.ts --project=chromium
git diff --check
```

Regenerate API artifacts before the final typecheck. Update existing E2E scroll actions to scroll the new page owner. Add the saved-but-unsubmitted, separate issue/comment counts and preview acceptance paths to the existing management tests; extend shared shape tests if their public input contract changes. For slice 4, extend the existing task-view and task-discussion tests to compare the two read surfaces and verify saved-view compatibility. After each test run, clean only that run's intermediate files, reports, downloads and seeded temporary data, using the test environment's ownership-aware cleanup. Do not reset an existing manual E2E environment or remove user data.

Documentation in each implementation commit:

- `docs-site/user-guide/projects/data-manager.md`: page scrolling, preview interaction, saved-content definitions and the separate issue/comment columns with an example.
- `docs-site/user-guide/workbench/discussion.md`: cross-link the matching Data Manager issue/comment definitions.
- `docs-site/dev/concepts/project-performance.md`: actual-image count, retained record semantics, attribution and independent totals.
- Relevant `docs-site/api/` performance contract reference and `README.md` API documentation reference when the additive API fields land.
- Top `CHANGELOG.md` Unreleased entries describing user-visible results; no version bump.

Rollback requires reverting the corresponding UI/API addition and generated artifacts, without modifying annotation data. No worker code or persistent write model changes are planned.

## Main assumptions and limits

This plan assumes that “annotated” means an actual saved, retained annotation and that the currently selected date interval remains meaningful. If the desired metric is instead all-time output or credit for editing another person's old object, the existing creation-based records cannot establish that history; that would be a different metric and a separate scope decision. The UI must state the chosen definition.

## Outcome

- Implemented all four slices: page scrolling, saved-content performance metrics, read-only annotation previews, and separate issue/comment counts.
- Preview lifecycle regression coverage includes delayed container measurement, per-tool colors, hidden masks, cancellation on close, cached reopen and StrictMode, partial page failures, concurrent revalidation, stable-URL media retries, read-only pyramid refresh, and deferred mask visibility.
- Local verification covers frontend regression tests, API tests on an owned disposable database, Chromium E2E, and live narrow/wide viewport and dark-theme checks. Remote CI and release qualification are not claimed.
- User documentation: `docs-site/user-guide/projects/data-manager.md` and `docs-site/user-guide/workbench/discussion.md`.
- API/developer documentation: `docs-site/api/guides/projects.md`, `docs-site/dev/concepts/project-performance.md`, `docs-site/dev/reference/data-manager-query.md`, and the README API reference.
- CHANGELOG: Unreleased entries added. Release milestone: not yet determined.
- Remaining work: no required implementation work; remote integration and release remain separate maintainer actions.

Source inspection and the user's screenshot establish the current gaps. No live browser acceptance or application tests have been run for this planning-only task.

Official implementation references: [TanStack Virtual scroll element and scroll margin](https://tanstack.com/virtual/latest/docs/api/virtualizer), [CSS sticky positioning and scrolling ancestors](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/position). These support reusing the existing virtualizer and correctly associating sticky headers with the new scroll owner.
