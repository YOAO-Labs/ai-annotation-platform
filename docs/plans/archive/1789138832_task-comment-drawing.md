# Task comment drawing

> Status: completed
> Date: 2026-09-11
> Coordinator: main agent; implementation workers: gpt-5.6-luna, max reasoning
> Baseline: local commit `e416d1c9` on `feat/workbench_260911`

## Outcome and scope

An image task can receive a comment containing text, drawing, or both without an annotation selection. Both popup drawing and direct drawing on the image remain available for the task destination. Existing annotation comments, Issue replies, attachments, mentions, video anchors, and selection-following drafts retain their contracts.

This phase covers image tasks, including raster-mask tasks. Task-level video drawing and 3D drawing are outside this phase because they require additional media-location contracts. There is no new package, service, setting, or release version for task drawing.

## Frozen contract

- Add nullable `canvas_drawing` JSONB to `annotation_feedbacks` in one additive Alembic migration. Existing rows remain null; do not migrate annotation-comment sources or their mirrors.
- Reuse the existing `CanvasDrawing` schema in feedback create/output. Accept a nonempty drawing only for a native root task comment (`kind=comment`, `anchor_type=task`, no parent) in an accessible image task. Unsupported destinations/media reject a supplied drawing explicitly.
- A native task comment requires text, an already supported attachment, or nonempty drawing. Empty-body PATCH must retain validity for an existing drawing-only comment. Do not extend attachment upload/download or mentions to task comments.
- Feedback routes and the mixed task discussion feed serialize the field. Existing annotation comments remain authoritative in `annotation_comments`.
- Web `AnnotationFeedback` and `CreateFeedbackPayload` gain optional nullable `canvas_drawing`, typed with the existing comment drawing type.
- Use `targetCapabilities.canvasDrawing` for drawing controls, independently of attachment/mention capability. The task composer only receives drawing capability on image tasks; video/3D task comments remain text-only.
- Task and annotation drafts retain separate identities. Selection follows a saved annotation; clearing selection restores the task destination. Never move draft contents or retarget in-flight drawing/submission results.
- Extend live-canvas origin validation, begin/complete/cancel behavior, and five-minute recovery to task targets while preserving owner/project/task/request checks and reading valid existing annotation recovery records. No annotation is required to start or recover a task drawing.
- Task drawings render in the discussion list and the existing hover/pin image preview. Drawing cancellation and switching destinations cannot leave an unusable active transaction.

## Parallel packages and write ownership

The companion [annotation comment badges plan](1789138832_annotation-comment-badges.md) shares API and coordinator boundaries.

| Package                     | Worker scope                                                                                                                                                                     | Dependencies                    |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| API                         | Both plans' API models/schemas/services/routes, one drawing migration, API tests and OpenAPI snapshot; user preference schema                                                    | Baseline                        |
| Drawing web and coordinator | `api/feedbacks.ts`; `CommentInput`, `CommentsPanel`, `DiscussionPanel`; discussion draft/recovery/workbench state; sole writer of `useWorkbenchShellModel.tsx`; associated tests | Frozen API and badge interfaces |
| Badges web                  | Companion plan's counts hook/client, ImageStage/ImageWorkbench, overlay, auth preference type/default, settings definition and tests                                             | Frozen API contract             |

Each modifying worker uses its own Orca child worktree from the baseline, with a `worktree-agent-*` branch, and returns a real commit plus paths and checks. Workers do not edit shared CHANGELOG, official docs, or plan files; the coordinator incorporates those during integration. Workers communicate interface corrections before edits. The main agent owns review, integration, runtime refresh, official documentation and final acceptance.

## Implementation steps

1. API worker adds storage, validated create/read behavior, drawing-only body validation and contract tests. The new field is additive; no historical data rewrite is needed.
2. Drawing web worker updates capabilities, feedback serialization and preview, then broadens the existing immutable canvas transaction and recovery paths to task destinations. This worker also handles the companion badge navigation/coordinator wiring so no other worker edits the shell owner.
3. Coordinator integrates the API package before frontend packages, regenerates ignored client types, inspects the complete diff, and asks the originating worker to correct review findings.
4. Coordinator refreshes only the verified development API/database serving port 3100, after first exercising migration/tests against a verified disposable test database. No database reset, production deployment or push is authorized.
5. Update discussion user guidance, API guidance, developer ownership documentation, README API notes, and CHANGELOG Unreleased. Append the actual outcome to both plans.

## Acceptance

- API: text-only compatibility; drawing-only and mixed comments; empty payload rejection; unsupported task media/Issue/reply rejection; wrong task/project/permission denial; create and mixed-feed round-trip; editing body on drawing-only records; additive migration upgrade and one migration head.
- Web: task/annotation A/B isolation; clear selection returns to task; popup/live mutual exclusion; cancel and late completions; switch task and SPA return; old and new recovery identities; account replacement; rendering task drawings from feedback rows.
- Browser: on the user-provided image task, deselect all annotations, use popup and direct drawing, save a task comment and read it back, verify overlay alignment after pan/zoom, switch between task and annotation drafts, and clean only agent-created acceptance records.
- Checks: focused pytest in a verified disposable database; focused Vitest suites; web typecheck, lint and CSS tokens; OpenAPI export/check and codegen; relevant docs generation/build; `git diff --check`.

## Risks and rollback

The main risk is target confusion while selection/task/account changes during drawing. Keep immutable origins and the existing transaction owner; do not replace them with current selection lookups at completion. A code rollback can retain the nullable column and its data, though an older client will not render newly stored task drawings. Do not drop the column as a routine rollback. This plan spans more than eight implementation/test/artifact files, with no new service or external credentials.

## Acceptance correction

Live acceptance found the existing corner-revealed BUG FAB covering the comment Send button. The drawing/coordinator worker also moves the fullscreen Workbench BUG entry into the existing Topbar action area and removes its floating duplicate in `App.tsx`, preserving the drawer owner and non-Workbench FAB. This bounded correction keeps pointer submission usable; it adds no preference or service.

## Outcome

- Implemented and accepted on 2026-09-12; no release milestone was assigned.
- All implementation used gpt-5.6-luna with max reasoning in isolated worktrees verified against originating local HEAD `e416d1c9`. The coordinator reviewed and integrated each commit. Interrupted workers resumed existing edits. After manifest/lockfile equality checks, worker dependency symlinks were corrected to the originating checkout's installed dependencies; no install modified the primary checkout.
- Backend commits: `1a14a467`, `f05f56d4`. Drawing/coordinator: `0f41ed89`, `8cb558a2`. Badges: `32c80a65`, `7a4118ff`. Acceptance correction: `ba1988ea` moves the Workbench BUG opener into Topbar so it cannot cover Send.
- Coordinator checks passed: 182 frontend tests across 17 files; 93 API/preference/OpenAPI tests in the new disposable `annotation_discussion_20260911_test` database; 4 SDK contract checks; final web build/typecheck; ESLint/CSS tokens; docs generation/build. Two unrelated existing lint warnings remain.
- Migration `0166` was applied only after verifying the intended development database and task. Live API reload/count/preference readback passed. No database reset, release or push occurred.
- Official documentation: `docs-site/user-guide/workbench/discussion.md`, `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/workbench/index.md`, `docs-site/api/guides/tasks-and-annotations.md`, `docs-site/api/guides/auth.md`, `docs-site/dev/concepts/audit-and-notifications.md`, `README.md`, and `CHANGELOG.md` Unreleased. Related feedback-entry references and generated settings/routes were synchronized.
- Live acceptance used a separate Orca tab for the requested image task. Five agent-created comments were deleted through existing UI/APIs after verifying their identities and contents. Comment totals returned to zero, the three original annotation geometries were unchanged, the preference was restored to true, and draft/recovery state was empty.
- Remaining scope: video/3D task drawing or canvas badges, unread tracking and new realtime broadcasts are outside these plans. Raster-mask and non-square rotated-anchor behavior have focused test coverage; live browser acceptance used the supplied bbox image task.

- Drawing acceptance: popup drawing with text and drawing-only live submission round-tripped at task scope; task and annotation live drawings survived reload under their original destinations. Completed task drawings appeared in composing and hover/pinned previews and stayed aligned through pan/zoom. Deselecting returned Send to the task.
