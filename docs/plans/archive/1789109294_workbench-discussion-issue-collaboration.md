# Workbench discussion and Issue collaboration

> Status: completed; G1, G2 and G3 implemented and accepted on 2026-09-11.
> Created: 2026-09-11. Requested outcome: a detailed implementation draft with parallel work packages.
> Audited base: `4fc6edd413dd248dafd1625b243c88735b46c375` on `feat/workbench_260911`.
> Implementation base: `11cd8ddd89e454691bfe5b2fe860afce8a484492`; workers use `gpt-5.6-luna` with `max` reasoning in isolated worktrees. The coordinator owns integration, acceptance and review.
> Delivery batches below are dependency groups, not release milestones. No version assignment, deployment, push, or business-data mutation is included in this planning task.

## 1. Goal and recommended scope

Make the existing Workbench discussion panel useful before an annotation exists, explicit about the object being discussed, and capable of completing an Issue conversation without losing the annotation context.

The product model is: comments exchange information; Issues track a concrete problem and its resolution; history records previous actions. Retain the existing compact annotation workspace and separate Mask quality review. Product BUG reports and task approval/rejection continue through their existing owners.

Deliver three independently usable batches:

1. **G1 — Usable task discussion:** task text comments, explicit reading/sending scope, reliable drafts, correct comment pagination, improved Issue entry/filtering, accurate unresolved counts/pin completeness, and one content scroll area.
2. **G2 — Issue conversation:** full thread reading/replies, accessible detail navigation, and previous/next unresolved navigation using existing image/video locators and G1 counts.
3. **G3 — Collaboration notifications:** Issue reply/status notifications and notifications for existing annotation-comment mentions, with permission-checked navigation to the actual conversation.

The minimum option is task text comments, explicit send targets, draft protection, and single-area scrolling. The recommended G1 also fixes pagination and entry semantics because newly usable task discussions must remain readable beyond the first page. It does not depend on G2 or G3.

**Load-bearing assumption:** the main users are annotators and reviewers collaborating around a task, rather than operating a project ticket system. If assignment, due dates, approvals, or a project inbox becomes the primary need, that deserves a separate workflow design; it must not silently expand these packages.

### Success criteria

- An accessible task with no annotations accepts a task comment without creating a fake annotation or selecting a prediction.
- Reading scope and sending destination remain visible and independent; a nonempty draft never changes its destination implicitly.
- Drafts survive tab changes, collapse/hide, dock/float/redock, task A → B → A, and ordinary SPA navigation within the same authenticated browser-tab session.
- Pagination includes every relevant authoritative comment once; filters and counts describe the server result, not just loaded rows.
- Both task-only and located Issues open a readable detail view, support replies, and expose only permitted actions.
- Image/video location, source-frame confirmation, changed-object fallback, and task-switch guards retain their existing semantics.
- The original 272px-wide panel, bottom dock, floating panel, light/dark themes, keyboard input, and Chinese IME remain usable.

## 2. Evidence and constraints

### Browser observations from the preceding assessment

Chrome was launched and connected through the browser extension. After the user logged in, the supplied image Workbench was inspected. The current task had zero annotations and zero Issues. The comments tab displayed `请先选中一个标注后再评论`; the Issue tab exposed `记录任务级问题`, whose image-task dialog still showed normalized x/y inputs. The history tab loaded real historical entries. The dialog was cancelled and the comments tab restored; no comment, annotation, or Issue was submitted.

This verified empty states, task-Issue entry, and history, not populated threads, uploads, video navigation, or write success. No console errors were observed during those checks; unrelated Konva layer warnings were present. Network response coverage was not captured. The running development page used the primary checkout, so later acceptance must verify which checkout serves the tested build.

### Current implementation evidence

| Area                     | Evidence                                                                                                                                                                    | Consequence for this plan                                                                                         |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Task comments            | `apps/web/src/pages/Workbench/shell/CommentsPanel.tsx` retains the task-feedback submit branch but explicitly disables its input                                            | Reuse the task write route; text-only capability must be explicit                                                 |
| Comment source ownership | `apps/api/app/api/v1/annotation_comments.py` writes authoritative legacy comments and mirrors creation into feedbacks; updates/deletion do not fully synchronize the mirror | Read original annotation comments; do not migrate or read mirrors as the source of truth                          |
| Mixed pagination         | `CommentsPanel.tsx` merges paged annotation comments with a single task-feedback page                                                                                       | Introduce a correctly ordered server read model instead of independently appending browser pages                  |
| Comment provenance       | Annotation source-chip callbacks exist in `CommentsPanel.tsx` but are omitted by `DiscussionPanel.tsx`; attachment URLs use the current panel annotation ID                 | Wire source navigation and use each row's identity for operations/downloads                                       |
| Draft lifetime           | `CommentInput.tsx` stores text in DOM and attachments/drawings in component state; tab/collapse rendering can unmount it                                                    | Put structured drafts outside the presentation lifetime and guard late completions                                |
| Existing layout behavior | Dockview preserves many hidden/reparented panels; `workbench-layout.spec.ts` already checks draft retention                                                                 | Extend existing guarantees; do not describe every dock operation as currently destructive                         |
| Issue list               | `DiscussionIssuesTab.tsx` filters only already-loaded rows, despite server `status` support                                                                                 | Send status in the query and reset the correct cursor                                                             |
| Replies                  | `POST /feedbacks/{id}/replies` and `useReplyFeedback` exist, but no complete thread GET exists                                                                              | Add a read contract before exposing reply UI                                                                      |
| Counts and pins          | `useIssuePins.ts` derives badges/pins from one feedback page                                                                                                                | Correct list pagination alone cannot justify total badges or complete pin coverage                                |
| Permissions              | Reviewer feedback PATCH accepts a payload containing status and forwards other fields too; list controls do not consistently reflect action permissions                     | Enforce field-level rules and project/task visibility on the server; project UI capabilities from the same policy |
| Navigation               | `useIssuePins.ts`, `useVideoIssueNavigation.ts`, and `IssueCreateModal.tsx` already own frame readiness, context capture, cancellation, and late-response guards            | Reuse these owners; do not create another seek or creation state machine                                          |
| Notifications            | Existing `NotificationService` has persistence, preferences, Redis/WS, and read state; discussion routes currently write audit events, not notifications                    | G3 is a real integration task, not merely exposing an existing @ notification                                     |
| Other caller             | `apps/web/src/pages/Review/ReviewWorkbench.tsx` renders annotation-only `CommentsPanel` without a task ID                                                                   | Preserve its public component behavior through an adapter                                                         |

Relevant decisions: [feedback ADR](../../adr/archive/0027-annotation-feedback-unified-table.md), [current source-ownership documentation](../../../docs-site/dev/concepts/audit-and-notifications.md), [shared Markdown authoring](1789022616_shared-markdown-authoring.md), [Workbench lifecycle guidance](../../../.agents/skills/aap-workbench-state/references/lifecycle.md), and [parallel-work rules](../../../.agents/references/parallel-work.md). Current source overrides historical migration plans. Workbench comments retain their structured mention offsets and drawing/attachment contract; the shared Markdown editor is not adopted here.

The [CVAT manual review workflow](https://docs.cvat.ai/docs/qa-analytics/manual-qa/), checked during the preceding assessment, supports the transferable pattern of located Issues, replies, resolution/reopening, and next/previous navigation. Its role model and persistence are not imported.

## 3. Product and interaction contract

### 3.1 Panel structure and reading scope

- Keep panel registry ID `discussion` and existing saved layouts. The user-facing dock title becomes `讨论`; tabs are `评论`, `问题`, `历史`. Preserve the conditional `Mask 质检` tab and its explicit activation requests.
- Comments default to `本任务全部讨论` on task entry. Offer `仅任务留言` and, when one persisted annotation is selected, `当前标注`.
- Selecting a shape does not automatically replace the task feed. In annotation scope, selection follows the new persisted annotation, while the composer follows the separate rules below.
- With no eligible selected annotation, annotation scope returns to the task feed with a visible explanation. Prediction IDs, temporary shapes, and multi-selection are never sent to annotation-comment endpoints.
- History shows its task/annotation scope explicitly. It stays a read-only audit view; no new activity ledger is invented.
- Use one flexible content scroll area. Keep the comments composer and Issue-reply composer outside that scroll area. Remove the old 240px list cap only in the docked/fill presentation; preserve the direct ReviewWorkbench caller's bounded presentation.
- Use existing local UI adapters, Lucide icons, semantic theme tokens and compact type sizes. Tabs have associated panels, accessible names, keyboard movement, and focus visibility. Narrow tab rows may scroll horizontally when Mask QC is present.

Compact comments layout:

```text
讨论
评论     问题     历史
范围：本任务全部讨论 v
--------------------------------
Comment list / loading / retry
(one flexible scrolling area)
--------------------------------
发送到：当前任务 v
留言输入区
                         发送
```

### 3.2 Sending destinations and capabilities

| Destination        | Initial capability                                             | Required context                                        |
| ------------------ | -------------------------------------------------------------- | ------------------------------------------------------- |
| Task comment       | Plain text                                                     | Accessible project and task                             |
| Annotation comment | Existing text, mentions, attachments, drawing and video anchor | One real persisted annotation; retain source identity   |
| Issue reply        | Plain text                                                     | Accessible active root Issue; validated task and thread |

Entering a task defaults the composer to that task's saved draft. Offer an explicit `评论选中标注` action when a persisted annotation is selected. Explicitly switching the send target saves the old target's draft and restores the new target's own draft; it never moves the old content. Reading filters do not alter send targets.

An annotation draft retains its original ID and source label after another annotation is selected. A deleted/inaccessible target becomes unsendable with an explanation and an explicit return-to-task action. It must not silently become a task comment. Unsupported mention/upload/drawing controls are hidden by capability and unsupported payload fields are rejected before submission, not dropped silently.

Task and Issue attachments, task/Issue mentions, and rich-text migration are outside G1–G3. Existing annotation attachments remain supported, including in the mixed feed. Resolve downloads through the row's original annotation ID, never the panel's current/null annotation ID. Existing task-feedback attachment metadata without a supported download lifecycle is shown as unavailable rather than generating an invalid URL.

### 3.3 Draft, submission and focus ownership

Freeze these concepts before implementation work is dispatched; use structured data, not raw editor HTML:

- **Session owner:** authenticated browser-tab session ID and user ID, with project-specific draft namespaces. It survives Workbench route unmounts as well as task loading and tab/dock presentation changes, and is disposed on logout/account replacement or app unload.
- **Target key:** task ID plus destination kind and annotation/root-Issue ID where applicable. Serialize an unambiguous tuple.
- **Draft:** text, structured mentions, attachment references, drawing, captured video anchor, draft revision, target label, pending/error state.
- **Submission snapshot:** immutable owner, target, revision, payload and request identity captured before the first await.

Instantiate a lightweight discussion-session provider above authenticated route outlets in `apps/web/src/App.tsx`, with draft data created lazily on first use. It must not eagerly import Workbench renderers or the editor. The Workbench and standalone ReviewWorkbench consume project/task namespaces through compatible adapters. In-memory drafts survive task A → B → A and ordinary SPA navigation away and back, including browser back. No new general router blocker or router migration is required. Text-draft persistence after browser reload/close is not promised; register a dirty `beforeunload` prompt while such drafts exist, where supported. Logout/account replacement clears the session; an expired or replaced authentication owner cannot submit or consume late results.

The existing canvas-drawing recovery is a narrower, already shipped exception: `useCanvasDraftPersistence.ts` stores active drawing shapes in sessionStorage for five minutes. Preserve that TTL/reload capability with a versioned namespace bound to user, project, task and annotation, and validate the target before restoring into a new in-memory session. Do not key reload recovery by the ephemeral session ID. Ownerless old-format records are not automatically imported into another account/composer; leave unexpired records untouched until their existing expiry rather than guessing an owner. New scoped records are cleared on their owner's logout. Neither text drafts nor account layout preferences adopt this storage format.

Before switching tasks or unmounting the Workbench route, synchronously save the original target's active drawing to its in-memory draft and update its five-minute recovery slot, then release the canvas; never serialize old shapes under the new task ID. On return, prefer the in-memory draft and use a valid recovery record only when no memory draft exists. Leaving the Workbench route for more than five minutes within the same authenticated SPA session must not discard the in-memory drawing. `beginCanvasDraft` accepts the originating composer owner/target/request ID. Completion carries that identity, and consumption clears only the matching result ID. The current `pendingResult` must not be consumed just because another input mounted.

The editor may hydrate when its target identity changes or on remount, but must not rewrite its DOM on every keystroke. Preserve the caret during reparenting/remount when that composer actually had focus; background updates do not autofocus.

- IME composition never submits. Enter submits only outside composition; Shift+Enter inserts a newline. The handler itself checks uploading/busy/in-flight state, not just the button.
- At most one write is in flight for one target. Snapshot payloads cannot change while a request is pending.
- Successful submission clears only the same unchanged draft revision. A late success must not clear a newer draft or another task's editor.
- Failure retains all structured content and exposes a target-local retry action. A transport failure does not auto-retry writes: exactly-once submission is not claimed without a server idempotency contract.
- Upload and live-drawing results carry their origin owner/target. A late result updates its original draft or is discarded after session disposal; it never attaches to the currently visible target by accident.
- Capture the video annotation anchor when the draft first gains content/drawing, or on explicit location update. Playback does not silently change a nonempty draft's anchor.
- Clear transient hover/composition overlays when their owner is inactive, while retaining the underlying draft.
- Global annotation shortcuts remain isolated during text input, mention picking, modal interaction and reply editing. Preserve existing lock and navigation guards; comments do not grant annotation-edit permission.

### 3.4 Issue creation, list and detail

- `新建问题` offers `任务问题` and `在画布选点` for supported image/video stages. Task-only creation always uses explicit task intent on images and video; it shows no editable x/y inputs. Pixel creation requires an actual confirmed point/frame and displays a human-readable location summary.
- Reuse the current modal snapshot/request guards and video pause/source-frame logic. Preserve captured viewport/time-window/object-version context and frame-range validation.
- Default Issue status is `未解决`; `全部`, `已解决`, and `搁置` remain available. Keep `open/resolved/wont_fix` in storage. Switching filters uses server filtering and a fresh cursor.
- Retain project scope only where it already exists: video Issue navigation. This plan does not add project-wide image/3D navigation.
- G1 preserves current card-location behavior while improving entry/filtering. For a pin whose Issue is filtered out/not loaded, explicitly switch the list to an including scope and `全部`, then page until the ID is found, exhausted, or cancelled; show loading/unavailable state and highlight the row. This preserves reverse pin navigation before a detail view exists. G2 adds a detail view for all Issues, including task-only Issues. Card title/open action opens detail; a separate `定位` action moves the canvas. Existing video tests must keep all original navigation assertions with the revised action selector.
- A pin request opens the referenced detail even if the Issue is not in the loaded page or does not match the current filter. Preserve the filter and show the detail's out-of-filter context.
- Detail shows root description, severity/status, location, paged replies, permitted actions and a back-to-list action. Preserve list scroll position. Resolving an Issue does not unexpectedly close its open detail when it leaves the unresolved list.
- New replies post to the root. Existing nested replies appear as a flat conversation with parent context where needed. Loaded replies render chronologically; `加载更早回复` prepends older pages without jumping the reader.
- Previous/next unresolved navigation walks the server-filtered newest-first sequence, fetching the next page at boundaries. No wrap at the first/last item; disable unavailable directions. Retain the current detail's `(created_at,id)` ordering anchor. If it is unresolved, find its ID; if it is resolved/shelved or becomes resolved, use its insertion point in the open sequence so `下一条` continues to the next older open Issue and `上一条` to the nearest newer one. Paging stops on a found neighbor or exhausted cursor, with visible loading/cancellation. A detail outside the current task/project scope offers an explicit switch to its scope before sequential navigation.
- Reviewers can resolve/reopen according to the enforced policy; annotators without that permission reply with the correction result for review. No new workflow status or implied assignment is introduced.
- Deletion uses existing soft deletion with explicit UI confirmation. A deleted root makes its thread unavailable; surviving descendants do not become standalone task comments.

## 4. API and data contracts

All paths in this section are relative to `/api/v1`. They are proposed additions; existing routes remain supported.

### 4.1 Correct comment read model — API-A

Add `GET /tasks/{task_id}/discussion/page` with `scope=all|task|annotation` (default `all`), optional `annotation_id`, `limit` (default 50, range 1–200), and opaque `cursor`.

`scope=annotation` requires an annotation belonging to the task; other scopes reject an annotation parameter. Contradictions return 422. Verify project/task visibility before querying, and preserve existing historical-comment visibility for deleted annotations while exposing unavailable navigation.

Response fields:

| Field             | Contract                                                                                        |
| ----------------- | ----------------------------------------------------------------------------------------------- |
| `items[].source`  | `annotation_comment` or `feedback`                                                              |
| `items[].data`    | The source's existing full response, retaining mentions/drawing/anchor/attachment fields        |
| `items[].actions` | Computed `edit`, `change_status`, `delete`, `reply` booleans; never authorization by themselves |
| `next_cursor`     | Null only when the matching ordered result is exhausted                                         |
| `total`           | Exact count for the current scope at response time; not a cross-request snapshot                |

Read from `annotation_comments` joined to the task's annotations plus native feedback task-comment roots (`kind=comment`, `anchor_type=task`, no parent). Exclude annotation feedback mirrors, Issue replies, product BUGs, and task rejection mirrors. Use SQLAlchemy `UNION ALL` over identifiers/order keys and batched source hydration. Do not load every row or merge independently paged feeds in the browser.

Global order is `(created_at DESC, source DESC, id DESC)`. Client identity is `(source, data.id)`, including the case where source tables contain equal UUIDs. Fetch `limit + 1`. A versioned cursor contains the order tuple and task/scope/annotation binding. Reject encoded cursors longer than 2048 characters, validate decoded fields, and return 400 for malformed/mismatched cursors. Refresh shows new comments; edits/deletes must not duplicate or skip unchanged rows.

The existing annotation/task-comment endpoints and all writes remain. Route mutations by provenance: annotation comments through `/comments/{id}` and their existing annotation create route; native task comments through `/feedbacks`. This is a read-model addition, not a database/view migration or new authoritative store.

### 4.2 Feedback queries, counts and threads — API-B

Extend `GET /feedbacks` additively:

- `root_only=false` by default for old callers; the Workbench explicitly requests `true` for Issue lists.
- Reuse current `status` filtering before pagination.
- `include_counts=false` by default. When requested, include exact `total` for the query and `status_counts` (`open`, `resolved`, `wont_fix`) under the same visibility/scope/root filters but without the current status filter.
- Preserve existing descending `(created_at, id)` order and valid legacy cursors. Malformed cursors return 400. Frontend query keys include every filter and count mode.
- Return computed `actions` with feedback records. Existing payload fields and enum values remain unchanged.

Add `GET /feedbacks/{root_feedback_id}/thread?limit=50&cursor=...` returning `root`, `items` (replies), `next_cursor`, and `total` (active replies). Limit range is 1–200. Require an active root, verify its task visibility, and include existing descendants through a scoped recursive query. Preserve immediate `thread_parent_id` values; traverse deleted intermediate replies to reach surviving children, while omitting deleted content. Validate project/task consistency and guard cyclic ancestry.

Thread reply paging uses `(created_at DESC, id DESC)` and a root-bound cursor. The frontend reverses the fetched window for chronological rendering. Deleted/inaccessible roots return an unavailable-resource response consistent with existing visibility helpers. A filtered list is never the only way to fetch a root.

Keep `POST /feedbacks/{id}/replies`; the new UI posts to a root. Existing descendant targets remain compatible only after validating active root ancestry and visibility. Apply the same parent/anchor validation when `POST /feedbacks` includes a parent, so it cannot bypass the reply route. Preserve stale Mask/point-cloud quality-anchor restrictions. Root soft deletion does not physically delete descendants.

For both `root_only=true` and legacy `root_only=false`, lists/counts omit active descendants whose root is deleted, inaccessible or invalid. Direct edit/status/delete/reply requests on that unavailable thread return 404; no orphan-cleanup endpoint is introduced. An inactive intermediate reply under an active accessible root does not hide its surviving descendants from the thread. Reject cross-task/cyclic parent chains before any write. Text-only task comments and replies reject whitespace-only bodies with 422; preserve existing supported attachment-only contracts for legacy callers rather than blanket-requiring text on rich annotation comments.

### 4.3 Actions and authorization

Freeze the action names in `schemas/discussion_actions.py` and a pure `services/discussion_actions.py` policy helper, both coordinator-owned during contract setup. Its `discussion_actions` function accepts the source/kind and named booleans `is_author`, `is_admin`, `is_reviewer`, `is_accessible`, `can_reply`, returning the four action booleans. Routers compute current visibility/active-root/quality-anchor prerequisites with existing access helpers before calling it. API-A/API-B reuse it for affordances and mutation permission checks instead of copying policy logic; no new role or deployment service is introduced.

| Source/action                                   | Rule                                                                                       |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Annotation comment edit/delete/resolve          | Existing author or authorized administrator policy                                         |
| Native feedback body/title/severity edit/delete | Author or administrator authorized for that project and task                               |
| Issue status                                    | Above actors plus an authorized reviewer                                                   |
| Issue reply                                     | Active actor who can access the root/task, subject to existing quality-anchor restrictions |
| Task text creation                              | Existing authorized task-visible actors; no annotation lock is invented                    |

Every mutation rechecks visibility and permissions; disabled UI is not sufficient. Reviewer status-only permission cannot authorize body/title/severity fields in the same PATCH. Reject the entire forbidden mixed-field request with 403. Native task-comment `reply` affordance remains false in this product scope even though feedback infrastructure supports generic replies. API capabilities and frontend affordances must not expose Issue threads for mirrored annotation comments.

### 4.4 Query completeness and navigation consumers

One web data owner maintains feedback query keys and invalidation. Create/reply/status/delete invalidate affected lists, counts, detail/replies and pin consumers. Comment mutations also invalidate the new task discussion feed and existing annotation-only consumers; optimistic deletion retains correct rollback behavior.

G1 Issue lists use server status filtering and `status_counts.open` consistently in the tab/FAB. Show loading/unknown while the count request is pending/failed, then the exact server count. G2 reuses this already functional count; it is not a prerequisite for making G1 badges accurate.

For current-task image/video pins, page root Issues without a status filter and derive pins from the fetched sequence. Keep already loaded pins visible, expose loading/retry until every page is read, and cancel on task replacement. Only after exhaustion may the UI imply complete pin coverage. Do not fetch all project Issues just to draw the current task. Virtualize/reduce metadata only if measured volume requires a separate rendering optimization; that is not part of this contract.

No new database table or migration is required by these contracts. API-B records query-plan evidence on representative disposable data, including parent-thread lookup and root counts. A required performance index must be a separately reviewed additive migration owned by API-B and applied through the runtime workflow; no backfill, materialized counter, or background process is preplanned.

## 5. Notification increment — G3

G3 is optional after G2 and is independently mergeable as a complete backend-plus-navigation increment. It does not add another notification system.

| Event                          | Recipients                                                                                                                    | Destination                           |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| `feedback.reply_created`       | Active root Issue author, excluding actor, only while able to access the task                                                 | Root Issue thread and new reply       |
| `feedback.status_changed`      | Active root author and distinct active reply authors, excluding actor, filtered by current task access                        | Root Issue detail with updated status |
| `annotation.comment_mentioned` | Existing validated mention recipients on annotation-comment creation, excluding actor and duplicates, filtered by task access | Original annotation and comment       |

Persist through `NotificationService`, reuse per-type mute preferences and global read/unread behavior, and add labels to settings and the notification popover. Do not claim thread unread state from notification unread counts. No email, external messaging, watchers, assignee, or per-thread read cursor is added.

Feedback notifications use `target_type=feedback`, `target_id=root_issue_id`; payload carries project/task IDs, source kind, actor name and reply ID where applicable, without copying the discussion body. Annotation notifications use `target_type=annotation_comment`, original comment ID and project/task/annotation IDs. IDs are hints until authorization is rechecked.

The current notification service publishes before the enclosing commit. For these new call sites, add `defer_publish=false` to both `notify` and `notify_many`; the batch method passes it through to every single-recipient call. New discussion callers set it true, collect only the notification rows actually inserted by that request, commit, then pass that collection to an existing-service `publish_committed` method using best-effort error handling. No shared/global pending queue is introduced. Existing callers retain their default behavior. Before commit or after a failed commit there is zero new-event WS delivery. Redis failure after commit must not turn a successful comment into a failed write. No retry daemon or exactly-once push claim is added.

Popover navigation uses the existing task lookup and Workbench URL builders. Before navigation, resolve current task visibility; then pass `discussion=issues&issue=<root UUID>&reply=<reply UUID>` or `discussion=comments&focus=<annotation UUID>&comment=<UUID>` to the correct annotate/review route. Reuse the existing `focus` parameter rather than adding a competing annotation parameter. The Workbench consumes these as explicit navigation requests, loads the referenced record independently of list pagination, opens the discussion panel and highlights it. Malformed/deleted/inaccessible targets show a specific error without navigating to a different task. Existing guards protect in-progress annotation work. Do not add an external URL redirect field.

For a requested reply, refresh the cached thread first, then page with its root-bound cursor until that reply is found or exhausted, validating root membership. Expose cancellable progress and a specific unavailable state for a deleted reply or UUID from another thread; do not treat a cached first page as proof of absence. Consume deep-link activation once per navigation identity; a later explicit navigation may activate the same target again.

For locating an old annotation comment from a notification, reuse its annotation-scoped read route and page until found with cancellation; an unavailable/deleted comment gets an explicit state. This avoids inventing a second mutable identity for an old comment. The UI may show bounded-progress loading while finding older records; no loaded-page-only failure is acceptable.

## 6. Parallel work packages and ownership

These are planned assignments, not agents already authorized to implement. Package IDs below are the dependency identifiers. Component development may use typed fixtures after contract setup; production code must not ship fixture data or pretend an unavailable API succeeded.

Path shorthand in package cards: `shell/`, `state/`, and `layout/` are under `apps/web/src/pages/Workbench/`; `api/`, `hooks/`, `components/`, and `pages/` in frontend cards are under `apps/web/src/`; backend `tests/` and shortened Python paths are under `apps/api/`. New paths are explicit planned deliverables.

Shared web interface baseline: `CommentInput` receives the immutable `target`, current `draft`/revision, `capabilities` and origin-bound live-canvas result; `onDraftChange` reports an origin key plus structured replacement, and `onSubmit` accepts the immutable submission snapshot and returns its request Promise. The draft store owns revision assignment and compare-before-clear; the editor never clears a different target. Preserve existing public annotation-only props through the adapter. The query family is `task-discussion`, keyed by user/project/task/scope/annotation; mutations carry source and originating task/annotation IDs. Freeze this query-key builder in `state/discussionTypes.ts` at C0 so F3 can invalidate the family without depending on F2's implementation.

### C0 — Contract baseline and integration ownership

- **Owner:** coordinator. **Depends on:** accepted scope of this draft.
- Freeze sections 3–5, source/action names, cursor semantics, composer callback ownership, and the file reservation table below. Add only the small shared Python action schema/policy and TypeScript discussion target/draft/query-key types needed by both workers; no placeholder route/component.
- Own `state/discussionTypes.ts`, `apps/api/app/schemas/discussion_actions.py`, `apps/api/app/services/discussion_actions.py`, and `apps/api/tests/test_discussion_actions.py`. Keep asynchronous resource visibility in existing access helpers; workers must not independently create competing permission modules.
- Capture current local HEAD and actual environment paths. Prepare isolated worktrees from that HEAD for modifying agents; give absolute paths and exact write scopes. No application version change.
- **Done:** baseline types compile and ownership/contracts are available to every worker. This is implementation setup, not a research/spike phase or a user-facing release.

### A1 — Authoritative task-comment read API

- **Depends on:** C0. **Parallel with:** A2, F1, F2.
- **Exclusive code:** `apps/api/app/api/v1/annotation_comments.py`, `apps/api/app/schemas/annotation_comment.py`, new `apps/api/app/services/task_discussion.py`, new `tests/test_task_discussion_page.py`, relevant annotation-comment tests.
- Implement section 4.1 without changing legacy write ownership/mirror behavior. Preserve complete source payloads; produce advisory actions and accurate totals. Apply the shared access policy to affected legacy list/create/patch/delete/upload-init/download paths by resolving comment → annotation → actual task → project; do not trust a supplied annotation/task pair or check account activity alone.
- **Acceptance:** at least three interleaved pages; 50+ task comments; equal timestamps and equal UUIDs across sources; updated/deleted legacy comment with stale mirror; Issue reply exclusion; invalid/cross-scope cursors; assigned-away task denial through new and old routes; direct legacy attachment access and cross-task parameter denial; fidelity of mentions/drawings/anchors.
- **Handoff:** route/schema contract, test command/output, query plan on representative data, changed paths and commit. Shared API snapshot belongs to the coordinator.

### A2 — Feedback roots, thread API and permissions

- **Depends on:** C0. **Parallel with:** A1, F1, F2. Keep all feedback router/schema/service edits in this package.
- **Exclusive code:** `apps/api/app/api/v1/annotation_feedbacks.py`, `apps/api/app/schemas/annotation_feedback.py`, `apps/api/app/services/feedback.py`, new `tests/test_feedback_threads.py`, new `tests/test_feedback_permissions.py`, `tests/test_annotation_feedbacks.py`, affected video feedback API tests.
- Implement sections 4.2–4.3: root filter, optional counts, full thread read, parent validation, action capabilities and field-level permissions. Retain valid old list clients and nested-thread read compatibility.
- **Acceptance:** matching Issues beyond page one; counts by visibility/scope; nested and soft-deleted ancestry; parent/task mismatch; reviewer status-plus-body denial; no write through deleted root; old pixel/video/Mask/point-cloud anchors unchanged.
- **Handoff:** A2 can land as additive API functionality before reply UI. Report query-plan results and whether an additive index is justified; do not let another worker edit these files concurrently.

### F1 — Session drafts and safe composer

- **Depends on:** C0. **Parallel with:** A1, A2, F2.
- **Exclusive code:** new `state/useDiscussionDraftStore.ts`, `state/DiscussionDraftProvider.tsx` and their tests; `shell/CommentInput.tsx`; a small serialization/hydration helper if needed; `shell/__tests__/CommentInput.test.tsx`.
- Retain one contenteditable implementation. Make it consume controlled target/draft state and report origin-bound changes. Preserve a backward-compatible annotation-only adapter until all callers are connected. Do not migrate to MDXEditor or persist arbitrary HTML.
- Implement section 3.3, target capability gates, composition/submission guards and upload/drawing origin guards. The store must have a scope-controlled lifetime, not process-global cross-user drafts.
- **Acceptance:** annotation and task A → B → A; tab unmount/remount; IME; rapid Enter/click; failure; late success after newer edit; late upload/drawing to original owner; unsendable deleted target; no repeated DOM hydration/caret jumps.
- **Handoff:** exact composer props and callbacks, store creation/disposal contract, standalone adapter example expressed as interface documentation, tests and commit. No edits to `CommentsPanel` or shell-model wiring.

### F2 — Comment feed, source cards and explicit scope

- **Depends on:** C0 for development; A1, F1 and F3 for final integration. **Parallel with:** A1, A2, F1/F3 using agreed contracts.
- **Exclusive code:** `shell/CommentsPanel.tsx`; new `api/discussion.ts`, `hooks/useTaskDiscussion.ts`, `shell/CommentsPanel.test.tsx`; `hooks/useAnnotationComments.ts` and its tests for new-feed invalidation; `components/AnnotationHistoryTimeline.tsx` and its CSS module/tests for explicit fill/bounded presentation.
- Implement read-scope and send-target separation, mixed-source rendering/mutation/download routing, task text entry, exact paging, empty/loading/error/retry states and fixed composer. Preserve annotation-only callers and existing mention/drawing previews.
- Do not edit `api/feedbacks.ts` or `hooks/useFeedbacks.ts`; consume F3's exports. Do not create another feedback client or feed merge fallback.
- **Acceptance:** unsupported targets never create annotation requests; task-only versus all versus annotation scope; source-chip selection; attachment URL uses row identity; 50+ task comments; permission-aware actions; errors are distinct from empty results; standalone bounded history unchanged.

### F3 — Issue entry, query integration and complete counts/pins

- **Depends on:** C0 for development; A2 for counts/actions/root filtering. **Parallel with:** F1/F2, as slots allow.
- **Exclusive code:** `api/feedbacks.ts`, `hooks/useFeedbacks.ts` and tests; `shell/DiscussionIssuesTab.tsx` and new tests; `shell/IssueCreateModal.tsx` and its tests; `state/useIssuePins.ts` and its tests.
- Implement server status filtering, explicit task/pixel creation intent, local mutation errors/pending states, root-aware query keys/invalidation, unknown versus exact badges, and current-task pin paging/completeness.
- Expose callbacks for task creation/pixel entry and exact count state to the coordinator. Keep the existing create-modal owner guards. Do not edit `useWorkbenchShellModel.tsx` or fork video navigation.
- **Acceptance:** image task entry contains no coordinates; pixel entry preserves actual point/frame; next-page-only matches load; failed status/delete is visible; late pin responses cannot alter a new task; counts never equal a partial loaded list by accident.
- F3 is the sole owner of the issue list before handing those files to F4; the two packages must not modify them concurrently.

### I1 — G1 Workbench wiring, documentation and acceptance

- **Owner:** coordinator. **Depends on:** A1, A2, F1, F2, F3.
- **Exclusive hotspots:** `apps/web/src/App.tsx`, `shell/DiscussionPanel.tsx`, `shell/WorkbenchLayout.tsx`, `state/useWorkbenchShellModel.tsx`, `state/useWorkbenchState.ts`, `state/useCanvasDraftPersistence.ts`, its existing `state/__tests__/useCanvasDraftPersistence.test.ts`, `pages/Review/ReviewWorkbench.tsx`, `layout/workbenchPanelRegistry.ts` display label, integration tests and shared documentation/artifacts.
- Mount the lightweight session owner above authenticated route presentation; wire eligible selected annotation, source labels/select callbacks, explicit task/pixel entry, live-drawing origin, browser-unload warning and lifecycle disposal. Update canvas begin/complete/consume identity and scoped five-minute drawing recovery as specified in section 3.3. Preserve panel ID/layout schema and conditional Mask QC behavior.
- Add accessible tab/panel associations and keyboard routing. Confirm no payload path accepts AI prediction/temporary IDs.
- Update the existing layout E2E expectation that no selection disables commenting: task text now works, while assertions rejecting `/annotations/pred-*` remain.
- **G1 exit:** task/annotation discussion works with complete paging and draft protection, Issue entry/filtering is usable, original layout/drawing/location flows pass, and relevant docs/API artifacts are updated. Reply UI and notifications need not exist yet.

### F4 — Issue detail, replies and sequential navigation

- **Depends on:** G1 and A2. **Parallel with:** preparation of independent notification UI N2; never with F3 on shared issue files.
- **Exclusive code:** `shell/DiscussionIssuesTab.tsx` (ownership transferred from F3); new `shell/DiscussionIssueDetail.tsx` and tests; `state/useActiveIssueStore.ts` and tests; new `hooks/useIssueThread.ts`; new `state/useIssueSequence.ts` and tests.
- Use the F1 composer in text-only reply mode. Read roots independently of loaded lists; implement ordered reply pages, back/list position, preserved out-of-filter detail, reply failure/draft handling and next/previous unresolved sequencing.
- API additions to `api/feedbacks.ts` and shared invalidation changes remain a serialized follow-up by the web data owner, integrated before F4 completes.
- **Acceptance:** task-only Issue detail; root beyond first page; filtered-out pin; 50+ replies; old nested replies; resolving while detail remains open; removed root; permission failure; sequence page boundary and cancellation; same-thread late reply responses.

### I2 — G2 location wiring and acceptance

- **Owner:** coordinator. **Depends on:** F4.
- Wire explicit Issue-detail activation separately from location using existing active-Issue/video owners. Any required `useVideoIssueNavigation.ts` changes and its tests are coordinator-owned.
- Preserve confirmed source frames, viewport/timeline restoration, object-changed fallback, cross-task permission checks, last-request-wins and Mask/annotation draft protection.
- Extend `video-issue-frame.spec.ts` / `video-issue-context.spec.ts` only where action semantics changed; retain their original frame/readiness assertions. Add thread lifecycle checks in the discussion E2E suite.
- **G2 exit:** an Issue can be opened, discussed, located, resolved/reopened and revisited with complete thread reading, regardless of list pagination. G3 may never ship without breaking this batch.

### N1 — Discussion notification events

- **Depends on:** A1/A2 ownership released and G2 navigation contract. **Parallel with:** N2. Emits no production event until integrated with N2/I3.
- **Exclusive code:** discussion notification call sites in `annotation_comments.py` / `annotation_feedbacks.py` (ownership transferred); `services/notification.py`; `api/v1/notifications.py`; new `tests/test_discussion_notifications.py`; relevant notification tests.
- Implement section 5 event/recipient policy, mute preferences, deduplication within an event, current access checks, and deferred publish for the new call sites.
- **Acceptance:** actor exclusion; repeated mentions/participants; muted or deactivated recipient; assigned-away task; zero publish before commit/after failed commit; `notify_many` honors defer; legacy default publish behavior unchanged; committed notification with Redis unavailable; no notification sent for failed reply or mirror creation. Use a separately owned disposable real-commit integration case to prove that another DB connection can read a notification before its post-commit push; SAVEPOINT-bound fixtures alone do not prove that property.

### N2 — Notification display and navigation

- **Depends on:** G2 contract for development; N1 for live event integration. **Parallel with:** N1.
- **Exclusive code:** `components/shell/NotificationsPopover.tsx`, its helpers/navigation tests, `pages/Settings/SettingsPage.tsx` notification labels/tests. Shared Workbench route-builder changes are coordinator-owned.
- Render the three event types and support feedback/comment targets in existing filters. Re-fetch target visibility and construct a typed Workbench navigation request; do not trust arbitrary payload URLs.
- **Acceptance:** correct project/task/mode; root/reply/comment identity; deleted or inaccessible targets; user switch during lookup; mute preference labels; existing task/batch/BUG navigation unchanged.

### I3 — G3 deep-link integration and acceptance

- **Owner:** coordinator. **Depends on:** N1, N2 and G2.
- Consume validated discussion URL requests in the Workbench, reveal the right tab, load/highlight the referenced record, and reuse existing guarded task/location navigation. The URL request must not be reapplied on every background render.
- **G3 exit:** a real permitted reply/status/mention produces one appropriate durable notification per recipient/event, opens the correct discussion when clicked, and respects existing notification read/mute behavior. WS failure does not invalidate a successfully committed comment.

## 7. Dependency graph and practical schedule

```text
                         C0: frozen contracts
               +------------+------------+------------+
               v            v            v            v
              A1           A2           F1           F2 development
               |            |            |            |
               |            +----> F3 ---+------------+
               +-------------------------+------------+
                                         v
                                  I1: G1 accepted
                                         |
                                        F4
                                         |
                                  I2: G2 accepted
                                    /           \
                                   N1           N2
                                    \           /
                                  I3: G3 accepted
```

The graph shows integration dependencies; F2 can develop against frozen typed contracts, but final integration requires A1/F1/F3. Thus F3 → F2 acceptance is an explicit edge even though their development overlaps. F3 depends on A2 for its live API. F4 depends on A2, F1, and the G1 handoff. Notification event production and navigation ship together at I3.

Acceptance dependency table (the authoritative dispatch/merge order):

| Package | Prerequisites for acceptance                                 |
| ------- | ------------------------------------------------------------ |
| C0      | Accepted implementation scope                                |
| A1      | C0                                                           |
| A2      | C0                                                           |
| F1      | C0                                                           |
| F2      | A1, F1, F3                                                   |
| F3      | A2                                                           |
| I1 / G1 | A1, A2, F1, F2, F3                                           |
| F4      | I1, plus serialized data-client additions by the coordinator |
| I2 / G2 | F4                                                           |
| N1      | I2; former backend owners have released their files          |
| N2      | I2 for development; N1 for live integration                  |
| I3 / G3 | N1, N2                                                       |

With one coordinator and three workers, use these scheduling waves:

| Wave                 | Worker 1                     | Worker 2                                | Worker 3                             | Coordinator                                                   |
| -------------------- | ---------------------------- | --------------------------------------- | ------------------------------------ | ------------------------------------------------------------- |
| Contract baseline    | —                            | —                                       | —                                    | C0; artifact and file reservations                            |
| First parallel wave  | A1                           | A2                                      | F1                                   | Prepare integration/browser fixtures and documentation deltas |
| Second parallel wave | F2                           | F3                                      | Read-only contract/regression review | Integrate completed commits and wire I1                       |
| G1 acceptance        | Focused fixes in owned files | Focused fixes in owned files            | Read-only acceptance review          | I1 and serialized live E2E                                    |
| Issue conversation   | F4                           | Bounded read-only API/navigation review | —                                    | Serialized data-client additions and I2                       |
| Notifications        | N1                           | N2                                      | Read-only regression review          | I3                                                            |

Do not create idle modifying agents merely to fill slots. Read-only review can share the checkout; modifying agents require isolated worktrees. Backend tests sharing one database and E2E sharing one database/bucket/port set run serially even when development is parallel.

Planning estimate, including focused tests and integration: G1 about 5–8 person-days, G2 about 3–5, G3 about 2–4. These are uncertain effort estimates, not elapsed-time promises or release dates. API contract work and integration acceptance are the critical path; raw worker count will not eliminate them.

### Shared-file and integration rules

- The coordinator alone owns App/provider wiring, `DiscussionPanel`, `WorkbenchLayout`, `useWorkbenchShellModel`, `useWorkbenchState`, canvas recovery identity, ReviewWorkbench bridge, shared action/target schemas/policy, `apps/web/src/utils/workbenchNavigation.ts` and tests, root README/changelog, API snapshot, SDK coverage metadata, shared E2E fixtures and final guide pages.
- A1/A2 have disjoint backend router/schema/service scopes. Notification N1 receives those files only after their earlier owners finish.
- F1 never edits CommentsPanel; F2 never edits CommentInput or feedback client/hooks; F3/F4 have sequential ownership of the Issue list.
- If an interface requires a shared-file edit, the worker submits the exact required delta to the coordinator. Do not resolve a collision by creating a second store, client, policy, or seek controller.
- Worker handoffs include a real commit on their isolated branch, changed paths, test evidence, exact documentation changes needed, and outstanding integration points. Keep work-in-progress worker commits private; finalized integration commits include the corresponding documentation/changelog changes before they are published. Do not publish a partial batch as accepted.
- Integrate against the current local branch, including unpushed commits; recheck drift before dispatch and integration. Squash/amend integration units as needed so final code and affected documentation land together. Commit/push/release actions occur only when later authorized.
- Remove only clean, integrated worktrees and merged/equivalent worker branches created for this task. Preserve unresolved work. Use the Orca CLI for Orca-managed state rather than creating unmanaged parallel state alongside it.

## 8. Verification plan

### 8.1 Behavior matrix

| Category             | Required cases                                                                                                                                                                                                                                                                                            |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Task comments        | No annotations; prediction/multi-selection; empty-body rejection; create/readback/refresh; more than 50 roots; no Issue reply leakage                                                                                                                                                                     |
| Source compatibility | Interleaved sources and identical keys; old edited/deleted/resolved comment versus stale mirror; attachment/mention/drawing/video-anchor fidelity; standalone ReviewWorkbench                                                                                                                             |
| Draft safety         | Annotation A/B and task A/B/A; comments/history/issues; collapse/hide/dock/float/redock; failed send; late send/upload/drawing; newer draft revision; account change; SPA navigation/back retains drafts; browser refresh warns; scoped five-minute canvas recovery and ownerless legacy-record isolation |
| Input/accessibility  | Chinese IME, Enter/Shift+Enter, duplicate triggers, mention picker, focus return, no canvas shortcuts while typing, labeled editor and keyboard tabs                                                                                                                                                      |
| Issues               | Task-only and pixel creation; image task dialog without coordinates; actual video frame/range; status match only after page one; partial pins visibly loading; exact counts                                                                                                                               |
| Thread lifecycle     | Root outside loaded/filter result; 50+ replies; nested descendants; deleted intermediary/root; invalid parent ownership; empty/inaccessible detail; status change while detail remains open                                                                                                               |
| Authorization        | All relevant author/admin/reviewer/annotator roles with real membership/batch fixtures; transferred task; mixed-field reviewer PATCH; direct parent create bypass; stale QC target                                                                                                                        |
| Navigation           | G1 filtered-out/unloaded pin recovery; explicit locate; previous/next page boundaries; resolved detail insertion point; rapid clicks; cancelled task switch; original source-frame readiness and viewport/time-window restore; changed/deleted annotation                                                 |
| Notifications        | Recipient policy, mute/dedup/self exclusion, current access, rollback, Redis failure after commit, old reply beyond page one, new reply after cached detail, reply from another root, correct deep link, deleted target and user-switch races                                                             |
| Presentation         | 272px dock and approximately original 351px content height; tall dock; bottom dock; floating; light/dark; conditional Mask QC tabs; no duplicated content scrollbar                                                                                                                                       |

A browser screenshot alone does not prove draft ownership, persistence or source-frame location. Use response assertions and readback for actual writes, with current console and relevant API failure evidence. Do not weaken existing layout/video assertions to make changed UI pass.

### 8.2 Commands

Run commands after the respective files exist. New test filenames below are planned deliverables, not tests already present or passed.

From `apps/api`, after verifying a disposable DB target:

```sh
.venv/bin/python -m pytest -p no:cacheprovider tests/test_discussion_actions.py tests/test_task_discussion_page.py tests/test_annotation_comments_paged.py tests/test_comment_polish.py
.venv/bin/python -m pytest -p no:cacheprovider tests/test_feedback_threads.py tests/test_feedback_permissions.py tests/test_annotation_feedbacks.py tests/test_video_feedback_context.py tests/test_video_feedback_context_api.py
.venv/bin/python -m pytest -p no:cacheprovider tests/test_discussion_notifications.py tests/test_notifications.py
```

From the repository root, select the completed package's frontend suites:

```sh
pnpm --filter @anno/web test src/pages/Workbench/state/useDiscussionDraftStore.test.ts src/pages/Workbench/shell/__tests__/CommentInput.test.tsx src/pages/Workbench/shell/CommentsPanel.test.tsx src/hooks/useAnnotationComments.test.tsx
pnpm --filter @anno/web test src/pages/Workbench/state/DiscussionDraftProvider.test.tsx src/pages/Workbench/state/__tests__/useCanvasDraftPersistence.test.ts src/utils/workbenchNavigation.test.ts
pnpm --filter @anno/web test src/pages/Workbench/shell/DiscussionPanel.test.tsx src/pages/Workbench/shell/DiscussionIssuesTab.test.tsx src/pages/Workbench/shell/IssueCreateModal.test.tsx src/pages/Workbench/state/useIssuePins.test.tsx
pnpm --filter @anno/web test src/pages/Workbench/shell/DiscussionIssueDetail.test.tsx src/pages/Workbench/state/useIssueSequence.test.ts src/pages/Workbench/state/useVideoIssueNavigation.test.tsx src/pages/Workbench/state/videoIssueContext.test.ts
pnpm --filter @anno/web test src/components/shell/NotificationsPopover.test.tsx src/components/shell/NotificationsPopover.navigation.test.tsx src/hooks/__tests__/useNotificationSocket.test.tsx
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm lint:python
git diff --check
```

Coordinator-owned E2E after integration:

```sh
pnpm --filter @anno/web exec playwright test e2e/tests/workbench-discussion.spec.ts e2e/tests/workbench-layout.spec.ts --project=chromium
pnpm --filter @anno/web exec playwright test e2e/tests/video-issue-frame.spec.ts e2e/tests/video-issue-context.spec.ts --project=chromium
```

`workbench-discussion.spec.ts` is a planned new functional suite. Put screenshot baseline assertions in the existing extended visual configuration, not the functional suite. Use the documented Chrome/WebCodecs qualification mode only when claiming precise-frame qualification; do not infer pixel accuracy from a ready label. No unrelated renderer benchmark is required merely for discussion changes.

After API integration, one coordinator runs:

```sh
pnpm openapi:export
pnpm openapi:check
pnpm codegen
pnpm --filter @anno/web build
pnpm docs:build
git diff --check
```

Review new watched endpoints in `packages/python-sdk/api-coverage.toml`; classify Workbench-only reads as `excluded` with reason `workbench-internal`, not falsely covered. Run the SDK's `tests/test_openapi_contract.py` in its local package environment under the SDK contract skill. SDK methods/CLI/TUI expansion and SDK/application version changes are not part of this plan.

### 8.3 Environment and cleanup

- Prefer the checkout's local Python virtual environment. At audit time, `.env`, root `node_modules`, and `apps/web/node_modules` are symlinks into the primary checkout; `apps/api/.venv` is local. No dependency installation is expected. Inspect links before any installation/configuration change.
- The pytest fixture derives `annotation_test` from the migration connection unless overridden, and runs Alembic. Confirm the effective target through a read-only `SELECT current_database(), current_user` and establish disposability before invoking tests. Never print credentials or treat a `_test` name alone as proof of ownership.
- Local Playwright defaults currently use a disposable `annotation_e2e` database, web port 3001 and API port 8010, with `reuseExistingServer=false`. Recheck the actual config. Do not seed the user's development page on port 3000.
- Parallel live suites need independent databases, ports and MinIO fixture buckets. Otherwise keep one E2E worker and serialize. Preserve test fixture cleanup and exact object-prefix cleanup after interrupted runs.
- After each run, remove only artifacts created by that run: test reports/traces/screenshots, temporary fixtures, newly generated caches and temporary build output. Preserve tracked approved baselines, pre-existing files, and required API-generated artifacts. Do not drop a pre-existing test database just to remove retained migrations.
- Confirm HMR/API reload serves the integrated checkout. These packages do not modify Celery jobs; no worker restart is presumed. If implementation adds worker work or an index migration, apply the runtime skill to that actual change.

## 9. Documentation and completion

### Implementation evidence

- G3 was accepted on 2026-09-11. The integrated notification API suites passed 20 tests, including both reply creation routes, recipient access/mute/self-exclusion, historical and nested participants, unchanged status, and independent-connection verification of the real HTTP commit boundary. Failed commits emitted no notification and retained no business rows; Redis failure after commit preserved a successful HTTP response and durable notification.
- G3 frontend checks passed 133 tests across navigation, active-target validation, notification resolution/UI, detail/comment focus, panel ownership and guarded video selection. Six unique browser cases passed across serialized runs: WebSocket delivery with an old reply beyond page one and a new reply after a warm cached thread; an old original annotation mention with mute/deleted-target checks; reviewer status notification and permitted reopen; invalid/cross-project URLs; and browser-back video task switches with both approval and rejection. The invalid-history batch case and the video guard cases passed again after the final admission fix. StrictMode highlight loss, scope fallback races, and premature batch-driven canvas unmount were reproduced before their fixes.
- A final shared-query review reproduced false cursor-cycle errors when an active reader refreshed the first page while notification navigation requested an older page. Both Issue and annotation-comment regressions failed before the fix and passed afterward; actual repeated cursors still stop traversal. The expanded navigation and thread suites passed 27 tests using real QueryClient observers. The final task-admission and cancellation review reported no additional findings.
- Final web and docs builds, web lint/CSS-token checks, typechecking, and OpenAPI consistency passed. Lint reported only two existing warnings outside the changed files. A dedicated browser served the production build and verified notification-to-reply focus/highlight, API readback of notification read state, and a 272px-wide panel without horizontal overflow in light/dark themes. No page errors or failed API resource responses were observed in that interaction. Screenshots and the temporary fixture were removed afterward. Cross-segment comment reading is covered by resolver/navigation/component checks, not a new collaborative-video browser qualification.
- G2 was accepted on 2026-09-11. Integrated thread, sequence, draft and video-navigation suites passed 127 tests. Follow-up regressions passed 20 thread/detail tests for native `comment` replies and 24 panel/activation tests for task changes while the Issues tab is unmounted, true Workbench unmount, StrictMode and newer navigation requests. The native reply-kind mismatch and stale cross-task detail replay were reproduced before their fixes.
- G2 browser acceptance passed four new discussion cases covering 53 replies with nested ancestry, failed pagination and reply retry, filtered-out pins beyond the first root page, sequence pagination with a resolved insertion point and restored list position, and late POST ownership across two Issue drafts. Eighteen unique video frame/context cases passed across serialized runs, including the existing pixel and context checks, a discussion-keyboard guard, and a direct-tab task-switch regression. These are functional browser checks, not a new renderer qualification. Test-created fixtures, screenshots, traces and reports were removed after inspection. Notification delivery and deep-link activation remain G3 work.
- G1 was accepted on 2026-09-11. Integrated frontend checks passed 85 Issue/draft-lifecycle tests and 48 comment/layout tests; the final popup-origin and selection-independent restore changes passed 40 composer/panel tests. Web typechecking, CSS-token checks, web/docs builds, OpenAPI consistency and four SDK contract tests passed. A dedicated browser against the task-owned database verified a native task comment, a coordinate-free image-task Issue with exact open count, task A → B → A drafts, and a 272px-wide compact panel in light/dark themes.
- G1 browser acceptance passed all four discussion cases, all four layout cases and all sixteen existing video Issue frame/context cases across serialized runs. Discussion cases covered a failed-write retry, more than fifty interleaved comments, authenticated attachment download with byte equality, late POST ownership, and unsaved popup strokes across tabs and SPA return with exact points and original-annotation API readback. Popup regressions were first reproduced on the old implementation. Video assertions retained actual source-frame pixels, context restoration, cancellation and permission checks; this is functional regression evidence, not a new renderer qualification.
- The integrated task-discussion, feedback-thread, permission and existing comment/video API suites passed 117 tests in a task-owned disposable database before the root-query optimization.
- A temporary transaction containing 20 tasks, 2,000 root Issues and 8,000 replies exposed unnecessary recursive ancestry traversal for `root_only` counts. Three `EXPLAIN (ANALYZE, BUFFERS)` runs measured 172.643–178.213 ms before the change and 0.233–0.317 ms after querying validated roots directly. The selected task's open count remained 66. This is local synthetic query evidence, not a production latency guarantee.
- The same fixture's parent-thread lookup returned four replies in 2.731–3.005 ms. No index migration was required for these measured paths. Both measurement transactions were rolled back and the original feedback row count was verified afterward.

The coordinator updates documentation with each accepted integration unit, keeping user-facing documentation about shipped behavior rather than proposed phases:

| Change                          | Documentation/artifacts                                                                                                             |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| G1 task discussion/scope/drafts | `docs-site/user-guide/workbench/index.md`; new `docs-site/user-guide/workbench/discussion.md` linked from it; Unreleased changelog  |
| G2 Issue replies/location       | Same discussion guide; `docs-site/user-guide/review/index.md`; relevant video navigation guide; Unreleased changelog                |
| API reads/threads/actions       | `docs-site/api/guides/tasks-and-annotations.md`, root `README.md`, `apps/api/openapi.snapshot.json`; local generated frontend types |
| Source and state ownership      | `docs-site/dev/concepts/audit-and-notifications.md`; document authoritative-source boundaries and in-session drafts                 |
| G3 notification behavior        | Discussion guide, notification settings guidance, developer notification concept page, Unreleased changelog                         |

The full plan affects more than eight files across API, web components/state, contracts, tests and documentation. It adds no package dependency, paid account, third-party API, deployment service, global configuration knob, or feature flag. Existing Postgres/Redis/MinIO support integration tests; access to a disposable test stack is an implementation prerequisite, not already verified by this planning session.

Rollback is code-level and additive: G3 can be removed while G2 conversations remain; G2 UI can be rolled back while reply records remain readable through the thread API; G1 UI can be rolled back without deleting task comments or changing legacy annotation rows. Prefer retaining additive read routes until all consumers are reverted. Already delivered notifications are historical data, not deleted during rollback. Any justified index migration has its own reviewed downgrade; no plan step depends on a destructive data rollback.

### Explicit follow-up scope

- **Identity migration:** platform/API owner separately plans legacy-comment convergence, reconciliation and rollback. This plan never reads stale mirrors as authoritative.
- **Task/Issue attachments and mentions:** collaboration/API owner separately defines upload/download ownership, retention, structured mention storage and notification semantics. G1/G2 text-only controls remain complete without them.
- **Project ticket management:** product/workflow owner decides assignees, watchers, deadlines, bulk actions and per-thread unread state only if there is demonstrated demand. No field or endpoint is reserved here.
- **Cross-device drafts, full-text search, comment-to-Issue conversion and rich Markdown:** separate product decisions; no hidden placeholders or partially exposed controls ship for them.
- **3D quality workflow:** remains with its existing quality Issue owners; this plan does not translate pixel locators into point-cloud locators.

After each batch is implemented, append an Outcome section recording actual commits, accepted behavior, verification evidence, documentation paths and remaining scope. Do not mark a batch complete merely because component mocks pass or worker commits exist. Draft validation itself consists of dependency/file-ownership review, local path checks, Markdown formatting and `git diff --check`; it does not count as application acceptance.

## Outcome

- G1 delivers authoritative task discussion, independent reading/sending scopes, session-owned drafts, source-correct attachments, Issue entry/filter/count improvements, and one content scroll owner. Integration commits include `0f8410f9`, `aa3e98d1` and `4580ec01` after the API and component packages listed in Git history.
- G2 delivers complete Issue threads and replies, action permissions, explicit location, and paginated previous/next unresolved navigation. Integrated through `1793faff`, `da39ecf5` and `9284147c`.
- G3 delivers durable reply/status/annotation-mention notifications with post-commit publication, existing mute/read behavior and validated one-shot discussion navigation. Notification packages landed as `7291b187`, `8bad9b88` and `3d1be350`; `6869532b` adds Workbench activation, regression coverage and documentation. The final follow-up preserves paging through a shared background refresh.
- User documentation: `docs-site/user-guide/workbench/discussion.md`, the Workbench index/video guide, review guide, and notification/settings references. API documentation: `docs-site/api/guides/tasks-and-annotations.md` and `README.md`. Developer documentation: `docs-site/dev/concepts/audit-and-notifications.md`. Unreleased changelog entries cover all three batches.
- Additive API snapshots and local generated frontend types are synchronized. Workbench-only SDK coverage exclusions and four SDK contract tests were verified during G1; G3 adds no API schema change. No dependency, schema migration, Celery worker, or version change was required.
- Release milestone: not determined. Local implementation and acceptance only; no push, deployment, release, or remote CI result is claimed.
- Remaining work within G1-G3: none. The separately scoped product ideas above remain out of scope. Browser reload/close text-draft persistence, task/Issue attachments and mentions, per-thread unread state, and email notifications are not promised.
