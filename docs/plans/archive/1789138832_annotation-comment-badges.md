# Annotation comment badges and user preference

> Status: completed
> Date: 2026-09-11
> Coordinator: main agent; implementation workers: gpt-5.6-luna, max reasoning
> Baseline: local commit `e416d1c9` on `feat/workbench_260911`

## Outcome and scope

Saved annotations with active comments show a neutral comment badge directly on the image canvas. Users can discover and open an annotation's discussion without first selecting it. A user-level Workbench preference controls the badges and defaults to enabled.

This phase covers image vector annotations and raster masks. Video track and 3D indicators, per-user unread state, and new realtime broadcast infrastructure are outside this phase. Counts represent all non-deleted comments, including resolved comments; they are not unread counts. Existing local mutation invalidation and refetch on focus cover freshness.

## Frozen API and preference contract

- Add `GET /tasks/{task_id}/discussion/annotation-counts`, returning `{ "counts": { "annotation-uuid": 3 } }`.
- Verify task visibility with the existing task discussion permission owner. Group authoritative active `annotation_comments` by annotation ID for the task's available saved annotations. Do not count feedback mirrors or infer totals from paginated client rows. Return sparse positive counts; unavailable annotations receive no badge.
- Use one task-level query, not one request per annotation. Web query keys remain below the existing `task-discussion` task prefix and include project/user identity, so comment mutation invalidation applies. Query disabled when the preference is off or stage is not image.
- Add `workbench.common.showAnnotationComments: boolean = true` to server preference schema and web defaults/type. Store through existing account Workbench preferences, not localStorage or project rendering settings. Missing historic values default true; explicit false survives patch/read/reload and account switching.
- Add an appearance setting labelled `标注评论提示`, describing image-canvas comment counts and click-to-open behavior. The switch affects only canvas badges; discussion content, counts, writes and drawings remain usable.

## Frozen frontend interfaces

- Badges web worker adds `useAnnotationCommentCounts(taskId, projectId, enabled)` in `apps/web/src/hooks/useAnnotationCommentCounts.ts`, exposing the normal query result whose data matches the response above. Client method lives in `api/discussion.ts`.
- `ImageWorkbench` and `ImageStage` receive optional `annotationCommentCounts?: Record<string, number>` and `onOpenAnnotationComments?: (annotationId: string) => void` props.
- ImageStage consumes the existing Workbench config's `common.showAnnotationComments`; it does not invent another preference owner.
- Drawing/coordinator worker is the sole writer of `useWorkbenchShellModel.tsx`, CommentsPanel and DiscussionPanel. It calls the new hook for image tasks when the preference is enabled, passes counts/callback through image editor props, and adds a consumable task-owned request for opening an annotation's comment list.
- Badge clicks use the existing guarded annotation selection and Workbench discussion reveal owner, then activate Comments and annotation reading scope. Do not fabricate a comment ID or bypass pending Mask/video edit guards. Existing notification-specific comment focus remains separate and functional.

## Badge behavior

- Show a neutral comment bubble/count for every visible saved annotation with a positive count, independent of whether its category label is hidden until selection.
- Counts 1–9 are exact, larger counts render `9+`; tooltip/accessibility text reports the exact count and annotation identity. Zero hides the badge.
- Keep the badge's screen size constant through zoom, follow live/moving geometry, include raster-mask bounds, and avoid resize handles. Hidden objects have no badge.
- Badge activation opens discussion and must not start a drawing, drag, or double selection. Provide an accessible keyboard activation surface rather than relying on a pointer-only canvas target.
- Reuse established overlay/geometry/semantic styling patterns. Do not add a general collision-layout engine. Prioritize selected/hovered objects when badges overlap.
- Failure/loading must not manufacture zero counts. Preserve available data through refetch without exposing another task/user's cache.

## Parallel packages

See [task comment drawing](1789138832_task-comment-drawing.md) for the shared package ownership. API owns schema/route/preferences; badges web owns rendering/client/settings; drawing web owns shared discussion and shell wiring. All implementation is performed by isolated gpt-5.6-luna max workers. Coordinator owns integration, review, browser acceptance, official docs, CHANGELOG and outcomes.

## Acceptance

- API: two same-class annotations count separately; more than 50 comments still count accurately; deleted comments, unavailable annotations, cross-task/project and permission checks; no mirrored double counting; preference default true and explicit false round-trip.
- Web: rendering zero/1/9/10 counts; hidden labels versus hidden annotations; vector and raster-mask geometry; live movement and pan/zoom; click propagation and keyboard activation; summary invalidation after create/delete; preference off hides badges without hiding comments.
- Browser: comment two different saved annotations, verify their separate indicators, click into each discussion, delete the final comment and confirm disappearance; toggle the account preference off/on, reload and read back persistence. Verify task/annotation drafts remain isolated and no accidental geometry edit occurs.
- Main coordinator runs focused suites, web typecheck/lint/CSS tokens, OpenAPI and settings documentation generation, docs checks/build, and `git diff --check` after integration. API tests use a verified disposable database. Acceptance mutations affect only agent-created records and a reversible user setting.

## Risks and rollback

Counts must be authoritative; a loaded-page scan misses older comments. Overlay positions must follow current geometry rather than lagging server data. Existing annotation visibility/rendering should remain the source of visible objects. The preference permits users to suppress dense-canvas indicators. This feature adds a read API and an optional preference, no new table or service; it can be disabled without touching comments. Across API/web/tests/artifacts, the scope exceeds eight files.

## Outcome

- Implemented and accepted on 2026-09-12; no release milestone was assigned.
- All implementation used gpt-5.6-luna with max reasoning in isolated worktrees verified against originating local HEAD `e416d1c9`. The coordinator reviewed and integrated each commit. Interrupted workers resumed existing edits. After manifest/lockfile equality checks, worker dependency symlinks were corrected to the originating checkout's installed dependencies; no install modified the primary checkout.
- Backend commits: `1a14a467`, `f05f56d4`. Drawing/coordinator: `0f41ed89`, `8cb558a2`. Badges: `32c80a65`, `7a4118ff`. Acceptance correction: `ba1988ea` moves the Workbench BUG opener into Topbar so it cannot cover Send.
- Coordinator checks passed: 182 frontend tests across 17 files; 93 API/preference/OpenAPI tests in the new disposable `annotation_discussion_20260911_test` database; 4 SDK contract checks; final web build/typecheck; ESLint/CSS tokens; docs generation/build. Two unrelated existing lint warnings remain.
- Migration `0166` was applied only after verifying the intended development database and task. Live API reload/count/preference readback passed. No database reset, release or push occurred.
- Official documentation: `docs-site/user-guide/workbench/discussion.md`, `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/workbench/index.md`, `docs-site/api/guides/tasks-and-annotations.md`, `docs-site/api/guides/auth.md`, `docs-site/dev/concepts/audit-and-notifications.md`, `README.md`, and `CHANGELOG.md` Unreleased. Related feedback-entry references and generated settings/routes were synchronized.
- Live acceptance used a separate Orca tab for the requested image task. Five agent-created comments were deleted through existing UI/APIs after verifying their identities and contents. Comment totals returned to zero, the three original annotation geometries were unchanged, the preference was restored to true, and draft/recovery state was empty.
- Remaining scope: video/3D task drawing or canvas badges, unread tracking and new realtime broadcasts are outside these plans. Raster-mask and non-square rotated-anchor behavior have focused test coverage; live browser acceptance used the supplied bbox image task.

- Badge acceptance: comments on separate annotations produced separate indicators without reload. Mouse and keyboard activation opened the correct scope; CSS hover priority and constant 40px hit targets were observed. Pan moved badges by the same 50×30 CSS pixels as the image. Preference false survived reload while comments stayed readable; true restored badges. Deleting an annotation's final comment immediately removed its badge.
