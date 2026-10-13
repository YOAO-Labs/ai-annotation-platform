# Issue pin consistency and image object association

> Status: completed
> Date: 2026-09-12
> Baseline: originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`
> Implementation: isolated gpt-5.6-luna workers with max reasoning; root owns review and acceptance

## Scope and contract

Image and video Issue pins share severity/state colors, readable symbols, screen-constant sizing and highlight rules. Open info is informational blue, open warn is caution, open blocker is danger red; resolved is positive and wont_fix is muted. Selection uses an outer emphasis. Historical null severity retains caution. Ordinary blocker severity does not change task submission or review workflow.

Image pixel Issues capture the active saved selected annotation at creation, show the associated class/short ID and allow clearing association before submission. Pixel location survives clearing association. Video receives the same clear-association affordance; any associated video object metadata must also be cleared consistently. Existing unassociated Issues remain readable.

Opening Issue details must not unexpectedly move the canvas. Explicit locate follows the existing navigation owner and selects an available associated image object with the editing guard; unavailable objects preserve the discussion and pixel location. Render an object label in list/detail so the relationship is visible after save. New associations must belong to the same project/task and refer to available objects.

## Ownership and interfaces

Worker issue_pins owns image IssueLayer, VideoKonvaIssueLayer, a minimal shared visual helper if useful, useIssuePins, IssueCreateModal, DiscussionIssuesTab/DiscussionIssueDetail and associated tests. It does not edit the shared shell model, video workbench/stage wrappers, comments or API.

Worker video_comments is sole writer of useWorkbenchShellModel and performs the small image association bridge requested by issue_pins. The agreed callbacks are `captureImageContext?: () => Pick<IssuePinAnchor, "annotationId" | "annotationLabel"> | null` and `selectImageAnnotation?: (annotationId: string, isCurrent: () => boolean) => Promise<boolean>`. The hook supplies its current owner/request check; false cancels positioning, while true permits positioning after selection or when the associated object is unavailable. Recheck source/task/account ownership after awaits. Any API validation adjustment belongs to task_mentions. No second state owner.

## Acceptance

Cover open severity colors, resolved/wont_fix precedence, null compatibility, matching screen sizes under zoom, canvas pointer behavior, existing accessible list/detail/locate controls and theme colors. Konva attributes alone do not establish DOM accessibility. Test selected image capture, unselected capture, clear association while preserving pixel, immutable context on selection/task switch, video metadata consistency, explicit locate, cancellation and missing-object fallback. Live review uses separate browser tabs and only temporary agent-created records, cleaned by identity afterward.

## Rollback and risk

Frontend changes reuse existing pixel plus annotation_id storage, so no migration is needed for this item. Main risks are late selection changes retargeting an Issue and click propagation editing annotations. No workflow gating or annotation geometry changes are included.

## Outcome

- Implemented and accepted on 2026-09-12, with no release milestone assigned and no push.
- All implementation workers used gpt-5.6-luna with max reasoning in separate Orca worktrees verified against originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`. Repository setup linked only matching Node dependencies to the originating checkout; Python environments and generated types stayed local.
- Root reviewed and integrated worker commits, including corrections for cancelled/stale image navigation, cached preference data, task-comment pagination during background refresh, no-op PATCH mention preservation, reply rejection and invisible keypoints.
- Integrated checks passed: 388 frontend tests across 23 files; 58 API tests in `annotation_discussion_20260911_test`; 6 real-commit HTTP tests in the separate verified `annotation_discussion_1789188981_notifications_commit_test`; 4 Python SDK OpenAPI contract tests. The four initially skipped transaction tests were subsequently executed successfully in that separate database.
- Web production build/typecheck, ESLint and CSS tokens, OpenAPI check/codegen, documentation generation/build and diff checks passed. Two unrelated existing ESLint warnings, existing bundle-size/docs syntax-highlighting notices and Konva layer-count warnings remain. The docs-impact advisory matched broader scheduler/tracker paths whose behavior was not changed; affected discussion/API/notification documentation was updated.
- Additive migration `0167` was tested before application to the verified development database serving port 3100 through API port 8100. No database reset or worker/service replacement occurred.
- Browser acceptance used temporary Orca pages. Four agent-created records (one single-frame comment, one self-mention task comment, one associated image Issue and one cleared-association Issue) were removed through existing APIs after matching source, ID, author, task and content. Original image/video annotations, Issue states and comment identities matched the pre-acceptance snapshot; the preference returned to true and drafts were empty. No message was sent to another real member.
- Official documentation: `README.md`, `CHANGELOG.md` Unreleased, `docs-site/user-guide/workbench/discussion.md`, `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/reference/notifications.md`, `docs-site/user-guide/reference/settings.md`, `docs-site/user-guide/review/index.md`, `docs-site/api/guides/tasks-and-annotations.md`, `docs-site/api/guides/auth.md`, `docs-site/dev/concepts/audit-and-notifications.md`, and generated settings guidance.

- Worker commits: `606afdf4742ae2dabefdbbe65a421d094c6cfd5a`, `a0aaa1ef9ee481672408bf55230218efd495adbe`; shared shell bridge landed with the video worker.
- Live video showed a red unresolved blocker and a green resolved check; image/video use the same screen-size/color/symbol rules. Image creation captured the saved bus annotation and displayed its short ID. Opening details retained the train selection; explicit locate selected bus. Clearing object association preserved the confirmed pixel and saved annotation_id=null. Ordinary blocker severity still does not gate task submission.
- Existing accessible list/detail/locate controls expose state and severity; Konva attributes are not treated as proof of DOM accessibility. Pending/stale selection and video metadata clearing have focused regression coverage.
