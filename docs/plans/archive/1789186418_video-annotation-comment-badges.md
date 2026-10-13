# Video annotation comment badges

> Status: completed
> Date: 2026-09-12
> Baseline: originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`
> Implementation: isolated gpt-5.6-luna workers with max reasoning; root owns review and acceptance

## Scope and contract

Extend existing annotation comment badges to saved video single-frame and track annotations. Reuse the task-level sparse count API and account preference workbench.common.showAnnotationComments (default true); retain 1-9 and 9+ presentation with exact accessible counts.

Use current resolved frame geometry from frameViews: single-frame objects show only on their source frame; tracks follow their visible current-frame geometry including interpolation/held data. Hidden/outside objects and unloaded collaboration segments have no canvas badge. Counts represent all active comments for the saved annotation UUID, including resolved comments, not per-frame unread counts. Historical comments with no frame anchor remain useful at object/track scope; do not invent a source frame.

Click/keyboard activation opens the annotation discussion through existing guarded selection/reveal/request ownership, does not mutate geometry and respects pending video/Mask edits. Keep screen size constant through pan/zoom and suppress interaction during unsafe drawing/playback operations. Support the existing vector and raster-mask video paths rather than bbox alone.

## Ownership and dependencies

Worker video_comments owns VideoWorkbench, VideoKonvaStage and video badge layer/helper/tests, WorkbenchStageHost, useWorkbenchShellModel and related bridge tests, settings descriptions/tests where the image-only wording becomes stale. It is sole shell-model writer, including the sibling image Issue capture/selection bridge agreed with issue_pins. Reuse image badge UI/helpers where appropriate without redesigning image behavior. CommentsPanel and draft payload fields belong to task_mentions; do not edit them concurrently.

## Acceptance

Cover single-frame versus track, interpolated/held geometry, hidden/outside objects, separate same-class IDs, video mask bounds, preference on/off, source task/account switches, pending edit guards, pointer and keyboard activation. Live acceptance verifies current video task counts, saved frame and track comment badges, moving between frames and pan/zoom, then removes only agent-created data. Run focused Vitest with limited workers plus integrated web typecheck/lint/build.

## Rollback and risk

No new API or database migration. Disable with the existing preference or revert frontend code without touching comments. Main risks are inconsistent geometry coordinates and bypassing video editing guards. No 3D badges or video task drawing is included.

## Outcome

- Implemented and accepted on 2026-09-12, with no release milestone assigned and no push.
- All implementation workers used gpt-5.6-luna with max reasoning in separate Orca worktrees verified against originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`. Repository setup linked only matching Node dependencies to the originating checkout; Python environments and generated types stayed local.
- Root reviewed and integrated worker commits, including corrections for cancelled/stale image navigation, cached preference data, task-comment pagination during background refresh, no-op PATCH mention preservation, reply rejection and invisible keypoints.
- Integrated checks passed: 388 frontend tests across 23 files; 58 API tests in `annotation_discussion_20260911_test`; 6 real-commit HTTP tests in the separate verified `annotation_discussion_1789188981_notifications_commit_test`; 4 Python SDK OpenAPI contract tests. The four initially skipped transaction tests were subsequently executed successfully in that separate database.
- Web production build/typecheck, ESLint and CSS tokens, OpenAPI check/codegen, documentation generation/build and diff checks passed. Two unrelated existing ESLint warnings, existing bundle-size/docs syntax-highlighting notices and Konva layer-count warnings remain. The docs-impact advisory matched broader scheduler/tracker paths whose behavior was not changed; affected discussion/API/notification documentation was updated.
- Additive migration `0167` was tested before application to the verified development database serving port 3100 through API port 8100. No database reset or worker/service replacement occurred.
- Browser acceptance used temporary Orca pages. Four agent-created records (one single-frame comment, one self-mention task comment, one associated image Issue and one cleared-association Issue) were removed through existing APIs after matching source, ID, author, task and content. Original image/video annotations, Issue states and comment identities matched the pre-acceptance snapshot; the preference returned to true and drafts were empty. No message was sent to another real member.
- Official documentation: `README.md`, `CHANGELOG.md` Unreleased, `docs-site/user-guide/workbench/discussion.md`, `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/reference/notifications.md`, `docs-site/user-guide/reference/settings.md`, `docs-site/user-guide/review/index.md`, `docs-site/api/guides/tasks-and-annotations.md`, `docs-site/api/guides/auth.md`, `docs-site/dev/concepts/audit-and-notifications.md`, and generated settings guidance.

- Worker commit: `7569ed2bf1c98f82aa371d7839197362051d3905`; dependency `606afdf4` was cherry-picked in the video worktree as `4941b49f8533d2bcae31af7ec59eea8cba17a0dc` before its typecheck.
- Live single-frame and track badges appeared separately and opened their corresponding annotation scopes. Advancing beyond the single-frame source removed only its badge; the track badge followed its later-frame geometry. Hit targets remained 40x40 through zoom. Preference false immediately hid cached badges without hiding comments, survived reload, and true restored badges.
- Real frame derivation, interpolation/outside, masks, keypoints and cached preference behavior have automated coverage. Live browser media used the existing bbox image/video tasks; no new collaborative segment or raster fixture was created. Video badges temporarily hide during active drags and resume on settled geometry. No new count endpoint, preference or 3D badge was added.
