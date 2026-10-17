# Continue SDK API coverage through agent workflows

> Status: implemented and locally validated on 2026-10-17 under the continuing SDK request.
> Base: `7ed68a5a667c43058917bdec5a9ee809cde052b1`, the separately committed offline discovery/export follow-up.

## Outcome contract

Extend the SDK and CLI by coherent public workflows, keeping the generated offline catalog and full API inventory current. This batch adds notifications, feedback follow-up, and saved pipeline/preannotation operations. Preserve existing SDK methods and CLI output, optional extras, server authorization, resource relationships, and the independent SDK/application release policy. No push, release, deployment, API/worker change, or automatic recurring task is requested.

The user asked to commit existing changes first. That checkpoint is complete as `7ed68a5a6`; the worktree was clean and locally ahead of `origin/main` by two commits before starting this batch. The previous 508 SDK tests and installation/documentation checks remain valid for that checkpoint.

## Selection and rolling backlog

The baseline contains 568 operations: 196 covered, 167 intentionally excluded, and 205 uncovered. A read-only inventory recommends Data Explorer as another strong next workflow: 11 data-manager and 8 task-view operations. This batch first closes the previously identified agent loop: discover pending work through notifications, read and act on feedback threads, and explicitly prepare/dispatch inference.

| Batch            | Scope                                                                             | Current decision                                                                                             |
| ---------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Current          | Notifications 8, feedback extensions 4, pipeline library/apply/preannotation 7    | Implement and verify 19 operations                                                                           |
| Next candidate   | Data Explorer and saved task views, 19 operations                                 | Trace query schemas, preview/apply revisions, selection limits, and export permissions before implementation |
| Later candidates | Video/tracker jobs, project templates, annotation discussions                     | Assess externally useful workflows and stable contracts individually                                         |
| Deferred         | Account/group/invitation administration; executor/workbench/maintenance internals | Retain explicit classifications until a concrete external workflow justifies changing them                   |

This is a prioritized backlog, not a release schedule or an assertion that every endpoint should become an SDK operation. Reassess exclusions individually: the six formerly excluded routes in this batch now have a concrete external workflow. Never reclassify a route as covered without a real call site and meaningful request/response tests.

## Current batch and public surface

### Notifications: eight uncovered operations

- `GET /api/v1/notifications` and `GET /api/v1/notifications/unread-count`.
- `POST /api/v1/notifications/{notification_id}/read` and `POST /api/v1/notifications/mark-all-read`.
- `DELETE /api/v1/notifications/{notification_id}` and `POST /api/v1/notifications/clear-read`.
- `GET /api/v1/notification-preferences` and `PUT /api/v1/notification-preferences`.

Expose `client.notifications` and `aap notifications`. Reuse the existing transport and typed models, with bounded list queries (server range 1–100), current-account ownership, and future-compatible notification types/payloads. Preference updates preserve omitted channels; explicit null channels and an empty patch are rejected before HTTP. Server knowledge of notification types remains authoritative. Read acknowledgements are not permission grants, and marking an already read/unknown notification can return 404. Deletion/clear-read CLI actions require the existing explicit confirmation convention.

### Feedback: four reconsidered exclusions

- `GET /api/v1/feedbacks/{root_feedback_id}/thread`.
- `POST /api/v1/feedbacks/{feedback_id}/replies`.
- `PATCH /api/v1/feedbacks/{feedback_id}`.
- `DELETE /api/v1/feedbacks/{feedback_id}`.

Extend the existing `client.feedbacks`, keeping its create/list APIs compatible, and add a discoverable `aap feedbacks` group for these operations. Typed thread pages retain root, reply items, cursor and total. Patches serialize explicit fields including explicit nulls where the API accepts them; avoid inventing a CAS revision or idempotency contract. Replies do not accept canvas drawings or mentions and never retry automatically after an ambiguous result. The server revalidates project/task/root/annotation relationships, active account state, quality-anchor freshness and caller-specific actions. Soft-delete CLI requires confirmation; status transitions and edits continue to use server permissions. Do not treat a readable thread as permission to modify it.

### Pipelines and preannotation: five uncovered and two reconsidered operations

- `GET`/`POST /api/v1/project-pipelines`.
- `GET`/`PUT`/`DELETE /api/v1/project-pipelines/{pipeline_id}`.
- `POST /api/v1/projects/{project_id}/pipelines/apply`.
- `POST /api/v1/projects/{project_id}/preannotate`.

Expose `client.pipelines` and `aap pipelines`. Saved library operations distinguish private, organization and public ownership. Lists are unpaginated at the API; describe that honestly and support its scope/owner filters without inventing a server limit. Partial updates preserve omission and explicit ownership clearing. Create, move, default replacement and public scope changes require current server permissions.

`apply` clones a visible saved template into a target project and optionally switches its default; it does not run inference. There is no standalone validate/dry-run endpoint. Server structural validation, target-project backend enablement and dispatch-time capability checks are distinct boundaries.

Typed stages/requests expose actual API fields and basic bounds without implementing a second execution engine. Root identity is `parent_stage is None`, not stage number zero. Keep open stage configuration dictionaries where the API owns their schema; leave graph/capability qualification to the existing server validator rather than offering a false local preflight.

`preannotate` returns the actual job acknowledgement (currently HTTP 200), including optional warnings/replay fields. It accepts an explicit optional `Idempotency-Key`; preserve the exact key/body on replay and never generate a replacement or automatically retry a write. Bulk and workbench execution scopes remain distinct, with explicit task/project/batch relationships and server state/lock checks. CLI execution requires confirmation; submission does not implicitly wait or claim completion. Existing `jobs.get/wait` observes the returned job ID; add the missing `aap jobs get` wrapper for that existing covered GET, without adding another API operation.

## Integration ownership

Use three isolated Orca worktrees based on local committed `7ed68a5a6`:

- Notifications worker: new notification resource/model/CLI modules and focused tests. No edits to aggregate Client, root CLI, public exports, manifest/catalog, documentation or versions.
- Feedback worker: existing Feedbacks class, feedback model additions, new feedback CLI and focused tests. Preserve unrelated Client/model code; parent owns aggregate imports and CLI registration.
- Pipeline worker: new pipeline resource/model/CLI modules, optional per-request headers in the existing transport, and focused tests. No edits to aggregate Client, root CLI, public exports, manifest/catalog, documentation or versions.
- Parent: aggregate/public/CLI wiring, full inventory classifications, generation/tests counts, operation notes, product docs/changelogs, integration validation and cleanup.

Workers return real local commits, focused verification, and exact limitations. They may reuse the originating SDK environment read-only with an explicit child `PYTHONPATH`, rather than install into a shared environment. Integrate sequentially; no worker pushes. The new follow-up remains separately reviewable unless another commit instruction arrives.

Expected classification after all real call sites land: **568 = 215 covered + 161 excluded + 192 uncovered**, zero planned. This changes 13 uncovered and six formerly excluded operations; it adds no API routes.

## Acceptance and documentation

- Verify UUID and typed JSON serialization, omission/null behavior, bounds, server envelopes/statuses, cursor direction and scope-specific constraints.
- Assert no mutation on malformed requests or rejected confirmation; assert a POST is not retried on 409/422 or an ambiguous network failure. Check notification preference merge semantics and feedback reply/patch restrictions.
- Verify confirmation-protected pipeline execution, unchanged idempotency headers/body, apply as clone, and job acknowledgement distinct from completion. Test a nonzero stage root and preserve dynamic stage configuration.
- Preserve structured errors, stdin bodies, core-only imports, installed CLI entry points, and generated schema descriptions. New operation descriptions state current authorization/relationship and idempotency limits without inferring permissions from HTTP verbs.
- Regenerate/check the packaged catalog and account for every snapshot operation. Update the full coverage guard and description count expectations from actual source declarations.
- Run focused checks while integrating, then the full SDK suite, Ruff, `git diff --check`, wheel/sdist build and fresh installed core/CLI smoke. Verify TUI imports if aggregate imports affect its optional-extra boundary; no TUI feature changes are planned.
- Update package README/changelog, root Unreleased changelog, SDK reference/CLI/Cookbook/agent guidance and a dedicated workflow page if needed. Keep plans/handoffs in English and product docs in Chinese.

No disposable application database or executor is required for client-only mock/loopback checks. State clearly that fixtures do not prove live application/GPU execution or remote CI success. Do not repeat the previous terminal-rendering acceptance without a changed TUI behavior or a new failure.

## Outcome

The requested commit-first checkpoint is `7ed68a5a667c43058917bdec5a9ee809cde052b1`; nothing was pushed. This subsequent batch is integrated in the originating checkout and remains a separate uncommitted diff. SDK release `0.20.0` and application compatibility target `0.26.1` remain unchanged.

Integrated worker commits:

- Notifications: `fb8729b58b6a2ce3b12ccd53f17237de479ef1e9`.
- Feedback: `fcacc1f2a0b01d10880909b45c76668b1033ff9c`.
- Pipelines/preannotation and per-request transport headers: `4f79ae2bd76a78194d8d69aab4077b135619875b`.

All 19 new operation classifications were derived from actual HTTP call sites: 13 formerly uncovered and six formerly excluded. The inventory is **568 = 215 covered + 161 excluded + 192 uncovered**, zero planned. The coverage guard now scans all SDK source modules so another resource module cannot silently escape call-site accounting. The generated catalog exposes 219 SDK method descriptions, including existing multi-request helpers.

Parent integration wired Client/public model/root CLI exports, added the existing Job GET as `aap jobs get`, and attached operation notes for account ownership, thread cursors/nulls, clone-versus-dispatch, task scopes and idempotency limits. A real nonempty pipeline CLI list revealed that shared output handling needed to serialize typed collections; it now uses Pydantic's existing JSON conversion. The registered-root regressions exercise nonempty responses, confirmation refusal without HTTP, stdin, unchanged body/key/authentication headers, 409 errors without repeated POST, and partial preference updates.

Older-dependency acceptance found two compatibility boundaries: Typer 0.12.5 cannot build CLI Literal parameters, and Pydantic 2.5.3 reserves broad `model_` fields. Scope CLI validation now uses the existing string/explicit-choice pattern; the two pipeline request bases use the same narrow protected method namespaces already adopted by other SDK models. No dependency bound or release version was changed.

Local verification:

- Full SDK suite: **577 passed** on Python 3.11.14, including existing TUI behavior checks.
- Ruff 0.15.22 check/format, Prettier, generated catalog `--check`, and `git diff --check` passed.
- Docs build passed; frontmatter checked 175 files, navigation covered 178 pages, and 23 Python documentation snippets parsed. Existing syntax-highlighter and bundle-size advisories remain unrelated to this change.
- `uv build` produced wheel and sdist. Fresh core/CLI/TUI installations passed outside the checkout; core discovery imports without CLI/TUI dependencies and all 219 method descriptions expand without HTTP. The packaged English guide and catalog are present.
- Current dependency smoke resolved Pydantic 2.14.1, Typer 0.27.3 and Textual 8.2.8. Another installed CLI using Pydantic 2.5.3, Typer 0.12.5 and Click 8.1.8 passed the same discovery, nonempty pipeline list, confirmation, stdin, header/body, acknowledgement and JSON 409 checks. These are installation smoke checks, not full suites for every dependency combination.
- Installed CLI checks used ephemeral loopback HTTP fixtures, with both CLI dependency combinations. They verify actual console entry points and transport serialization, not live application/GPU execution.

Artifacts remain under `packages/python-sdk/dist/`:

- Wheel SHA-256: `18dc468261b838fa98c1c0bf05360dbbf867812efb86cdda2d5c3ff66dd57277`.
- Sdist SHA-256: `6a211936d8d3df29f310c38862c1031a6e0b7a85e5b8e6d2eb8c907659962a7b`.

Product documentation is in `docs-site/dev/sdk/workflows.md`, the SDK/CLI references, quickstart and agent guide, package README/changelog and root README/Unreleased changelog. The next candidate remains the 19 Data Explorer/task-view operations; their query/revision/export contracts need an implementation-specific pass before the next batch.

The three clean task-created worker worktrees were removed through Orca after integration and validation, retaining their unmerged source branches. Temporary installation environments, loopback servers and integration scratch files were cleaned up. The originating SDK environment and distribution artifacts remain; the follow-up diff is unstaged.

No live API/database, backend/executor/GPU, browser or remote CI acceptance was performed. No API/worker/runtime code changed, so no application restart or migration was required. The remaining 192 uncovered and 161 excluded operations are explicitly accounted for; route coverage does not establish full business-semantic parity or runtime availability.
