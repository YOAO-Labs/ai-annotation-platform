# Task comment member mentions

> Status: completed
> Date: 2026-09-12
> Baseline: originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`
> Implementation: isolated gpt-5.6-luna workers with max reasoning; root owns review and acceptance

## Scope and contract

Task comments support project-member mentions on image, video and point-cloud tasks. Reuse the existing Mention structure, member picker, discussion draft owner, notification service and source-aware navigation. Annotation comments remain authoritative in annotation_comments; native task comments remain in annotation_feedbacks. No object references, attachment expansion, Issue reply mentions or release changes are included.

- Add an additive mentions JSONB field with an empty historic/default value to feedback storage/create/output and the mixed task feed. Accept nonempty mentions for native root task comments; reject unsupported destinations explicitly.
- Enable task draft/input/submission/render support without mixing task and annotation drafts. Keep immutable submission ownership and preserve content after failures.
- Reuse project membership and recipient task-visibility checks. Add `feedback.comment_mentioned`, targeting the native task comment, with one notification per eligible recipient, excluding the actor and publishing only after commit.
- Register notification preferences and labels. Clicking the notification reveals the original task comment through existing source-aware discussion navigation, including pagination and unavailable/deleted-target handling.

## Ownership and dependencies

Worker task_mentions owns affected API model/schema/routes/services, additive migration, API tests/OpenAPI snapshot, web feedback client, draft capabilities, CommentInput/CommentsPanel and tests, notification navigation/preferences and tests. It does not edit useWorkbenchShellModel, issue UI, video renderer or official docs. Root owns official docs and integrated verification. API changes needed by sibling image object association are coordinated through this worker.

## Acceptance

Test task mentions round-trip and rendering; old text/drawing comments; member validation; deduplicated notifications and self exclusion; inaccessible/deactivated recipients; failed transaction has no published notification; notification navigation across tasks/pages and deleted targets; independent task/annotation drafts. Run API tests only against a verified disposable database, regenerate API artifacts, and exercise the live composer and navigation without sending unsolicited messages to other users. A self mention can validate browser persistence while notification delivery uses isolated test accounts/fixtures.

## Rollback and risk

One additive schema migration; a code rollback retains the column and stored mentions. Do not drop data. Main risks are lost structured mentions, incorrect recipients and routing a task comment as an Issue. No new dependency or service.

## Outcome

- Implemented and accepted on 2026-09-12, with no release milestone assigned and no push.
- All implementation workers used gpt-5.6-luna with max reasoning in separate Orca worktrees verified against originating local HEAD `b162707b64903a56765fb1d436032f05a8a8e9a8`. Repository setup linked only matching Node dependencies to the originating checkout; Python environments and generated types stayed local.
- Root reviewed and integrated worker commits, including corrections for cancelled/stale image navigation, cached preference data, task-comment pagination during background refresh, no-op PATCH mention preservation, reply rejection and invisible keypoints.
- Integrated checks passed: 388 frontend tests across 23 files; 58 API tests in `annotation_discussion_20260911_test`; 6 real-commit HTTP tests in the separate verified `annotation_discussion_1789188981_notifications_commit_test`; 4 Python SDK OpenAPI contract tests. The four initially skipped transaction tests were subsequently executed successfully in that separate database.
- Web production build/typecheck, ESLint and CSS tokens, OpenAPI check/codegen, documentation generation/build and diff checks passed. Two unrelated existing ESLint warnings, existing bundle-size/docs syntax-highlighting notices and Konva layer-count warnings remain. The docs-impact advisory matched broader scheduler/tracker paths whose behavior was not changed; affected discussion/API/notification documentation was updated.
- Additive migration `0167` was tested before application to the verified development database serving port 3100 through API port 8100. No database reset or worker/service replacement occurred.
- Browser acceptance used temporary Orca pages. Four agent-created records (one single-frame comment, one self-mention task comment, one associated image Issue and one cleared-association Issue) were removed through existing APIs after matching source, ID, author, task and content. Original image/video annotations, Issue states and comment identities matched the pre-acceptance snapshot; the preference returned to true and drafts were empty. No message was sent to another real member.
- Official documentation: `README.md`, `CHANGELOG.md` Unreleased, `docs-site/user-guide/workbench/discussion.md`, `docs-site/user-guide/workbench/settings.md`, `docs-site/user-guide/reference/notifications.md`, `docs-site/user-guide/reference/settings.md`, `docs-site/user-guide/review/index.md`, `docs-site/api/guides/tasks-and-annotations.md`, `docs-site/api/guides/auth.md`, `docs-site/dev/concepts/audit-and-notifications.md`, and generated settings guidance.

- Worker commits: `cd4dcf4646c5845ab9fd9f8ef0d068587a5a3e84`, `11a2e757e16bcefc31bd9d13233c17f7929899e2`.
- Task member selection and chip insertion were exercised in the browser without sending to the selected real member. A self mention created through the app client persisted and rendered as a mention; its task-comment deep link focused the original row, and the same link reported unavailable after cleanup. Recipient delivery, mute/visibility and commit/Redis failure boundaries were verified with isolated test users and a mocked publish transport, including observation from a second PostgreSQL connection.
- PATCH accepts an explicit updated mention map only for native task roots. A changed body without a map clears old offsets; an identical body preserves them; edits do not notify again. Issue reply mentions and annotation-object references remain outside this scope.
